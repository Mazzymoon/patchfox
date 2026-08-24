"""Single-instance SWE-bench patch generation adapter.

This module deliberately stops at patch generation.  The generated prediction is
intended to be graded later by the official SWE-bench Docker harness.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ..config import resolve_provider_config
from .context_cost import extract_usage_from_artifacts
from .harnessbench import build_adapter_metadata
from .metrics import aggregate_run_artifacts
from .run_evidence import RunEvidence

DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_SPLIT = "test"
DEFAULT_ARTIFACT_ROOT = Path("artifacts/swebench")
PRIVATE_DATASET_FIELDS = frozenset(
    {
        "patch",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "fail_to_pass",
        "pass_to_pass",
    }
)


@dataclass(frozen=True)
class SWEbenchInstance:
    """The only dataset fields allowed to cross into patch generation."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str


@dataclass(frozen=True)
class PatchFoxInvocation:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    wall_time_seconds: float = 0.0


@dataclass(frozen=True)
class SWEbenchRunConfig:
    instance_id: str
    dataset: str = DEFAULT_DATASET
    split: str = DEFAULT_SPLIT
    run_id: str = ""
    workspace_root: Path = Path("/tmp/patchfox-swebench")
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    provider: str | None = None
    model: str | None = None
    config_path: Path | None = None
    base_url: str | None = None
    max_steps: int = 30
    max_new_tokens: int | None = None
    python_executable: str = sys.executable


@dataclass(frozen=True)
class SWEbenchRunResult:
    returncode: int
    prediction: dict[str, str]
    metadata: dict[str, Any]
    artifact_dir: Path
    predictions_path: Path
    workspace: Path | None


DatasetLoader = Callable[[str, str], Iterable[Mapping[str, Any]]]
PatchFoxInvoker = Callable[[Path, str, str, SWEbenchRunConfig], PatchFoxInvocation]
CloneURLResolver = Callable[[str], str]


def load_swebench_instance(
    dataset: str,
    split: str,
    instance_id: str,
    *,
    dataset_loader: DatasetLoader | None = None,
) -> SWEbenchInstance:
    """Load one row and immediately reduce it to non-answer issue metadata."""

    loader = dataset_loader or _huggingface_dataset_loader
    for row in loader(dataset, split):
        if str(row.get("instance_id", "")) != instance_id:
            continue
        values = {
            key: str(row.get(key, "") or "")
            for key in ("instance_id", "repo", "base_commit", "problem_statement")
        }
        missing = [key for key, value in values.items() if not value.strip()]
        if missing:
            raise ValueError(
                f"SWE-bench instance {instance_id!r} is missing: {', '.join(missing)}"
            )
        return SWEbenchInstance(**values)
    raise LookupError(
        f"instance_id {instance_id!r} was not found in {dataset!r} split {split!r}"
    )


def prepare_git_workspace(
    instance: SWEbenchInstance,
    destination: Path,
    *,
    clone_url_resolver: CloneURLResolver | None = None,
) -> Path:
    """Clone and checkout an instance at a verified clean base commit."""

    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"workspace already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolver = clone_url_resolver or github_clone_url
    _run_git(["clone", "--no-checkout", resolver(instance.repo), str(destination)])
    _run_git(["checkout", "--detach", instance.base_commit], cwd=destination)

    head = _run_git(["rev-parse", "HEAD"], cwd=destination).stdout.strip()
    expected = _run_git(
        ["rev-parse", f"{instance.base_commit}^{{commit}}"], cwd=destination
    ).stdout.strip()
    if head != expected:
        raise RuntimeError(
            f"checkout mismatch for {instance.instance_id}: expected {expected}, got {head}"
        )
    status = _run_git(["status", "--porcelain"], cwd=destination).stdout
    if status.strip():
        raise RuntimeError(
            f"initial git working tree is not clean for {instance.instance_id}"
        )
    return destination


def collect_model_patch(workspace: Path) -> str:
    """Return a binary-capable diff that also includes untracked files."""

    workspace = Path(workspace).resolve()
    raw = _run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"],
        cwd=workspace,
        text=False,
    ).stdout
    untracked = [
        os.fsdecode(item)
        for item in raw.split(b"\0")
        if item and not _is_patchfox_state_path(os.fsdecode(item))
    ]
    if untracked:
        _run_git(
            ["--literal-pathspecs", "add", "--intent-to-add", "--", *untracked],
            cwd=workspace,
        )
    return _run_git(
        [
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
            ".",
            ":(exclude).patchfox",
            ":(exclude).patchfox/**",
        ],
        cwd=workspace,
    ).stdout


def run_swebench_instance(
    config: SWEbenchRunConfig,
    *,
    dataset_loader: DatasetLoader | None = None,
    patchfox_invoker: PatchFoxInvoker | None = None,
    clone_url_resolver: CloneURLResolver | None = None,
) -> SWEbenchRunResult:
    """Generate one SWE-bench prediction and persist adapter/evidence metadata."""

    _validate_component("instance_id", config.instance_id)
    _validate_component("run_id", config.run_id)
    artifact_dir = (
        Path(config.artifact_root).resolve() / config.run_id / config.instance_id
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = artifact_dir.parent / "predictions.jsonl"
    workspace: Path | None = None
    stdout = ""
    stderr = ""
    phase = "dataset_load"
    started = time.monotonic()
    effective_provider = config.provider or ""
    effective_model = config.model or ""
    prediction = _prediction(config.instance_id, effective_model, "")
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "status": "error",
        "instance_id": config.instance_id,
        "dataset": config.dataset,
        "split": config.split,
        "run_id": config.run_id,
        "approval": "never",
        "max_steps": config.max_steps,
        "max_new_tokens": config.max_new_tokens,
        "token_budget_scope": "per_model_call"
        if config.max_new_tokens
        else "runtime_default",
    }

    try:
        instance = load_swebench_instance(
            config.dataset,
            config.split,
            config.instance_id,
            dataset_loader=dataset_loader,
        )
        metadata["instance"] = {
            "instance_id": instance.instance_id,
            "repo": instance.repo,
            "base_commit": instance.base_commit,
        }

        phase = "provider_resolution"
        if patchfox_invoker is None or not effective_model:
            effective_provider, effective_model = _resolve_runtime_identity(config)
        metadata["provider"] = effective_provider
        metadata["model"] = effective_model
        prediction = _prediction(config.instance_id, effective_model, "")

        phase = "workspace_prepare"
        workspace = (
            Path(config.workspace_root).resolve()
            / config.run_id
            / config.instance_id
            / "repo"
        )
        prepare_git_workspace(
            instance, workspace, clone_url_resolver=clone_url_resolver
        )
        metadata["workspace"] = str(workspace)

        phase = "patchfox"
        session_id = _session_id(config.run_id, config.instance_id)
        invoker = patchfox_invoker or invoke_patchfox_cli
        invocation_config = replace(
            config, provider=effective_provider or None, model=effective_model or None
        )
        invocation = invoker(
            workspace, instance.problem_statement, session_id, invocation_config
        )
        stdout = invocation.stdout
        stderr = invocation.stderr
        metadata["patchfox_returncode"] = invocation.returncode
        metadata["patchfox_wall_time_seconds"] = invocation.wall_time_seconds
        metadata["evidence"] = _safe_collect_evidence(
            workspace, session_id=session_id, returncode=invocation.returncode
        )
        if invocation.returncode != 0:
            raise RuntimeError(
                f"PatchFox exited with return code {invocation.returncode}"
            )

        phase = "patch_collect"
        model_patch = collect_model_patch(workspace)
        prediction = _prediction(config.instance_id, effective_model, model_patch)
        metadata["status"] = "completed"
        metadata["model_patch_bytes"] = len(model_patch.encode("utf-8"))
        metadata["error"] = None
        returncode = 0
    except Exception as exc:  # noqa: BLE001 - always emit an official-shaped row.
        returncode = 1
        prediction = _prediction(config.instance_id, effective_model, "")
        metadata["status"] = "error"
        metadata["failure_phase"] = phase
        metadata["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if stderr:
            stderr = f"{stderr.rstrip()}\n{type(exc).__name__}: {exc}\n"
        else:
            stderr = f"{type(exc).__name__}: {exc}\n"
        if workspace and workspace.exists() and "evidence" not in metadata:
            session_id = _session_id(config.run_id, config.instance_id)
            metadata["evidence"] = _safe_collect_evidence(
                workspace,
                session_id=session_id,
                returncode=int(metadata.get("patchfox_returncode", returncode)),
            )

    metadata["adapter_wall_time_seconds"] = round(time.monotonic() - started, 6)
    _write_outputs(
        artifact_dir=artifact_dir,
        predictions_path=predictions_path,
        prediction=prediction,
        metadata=metadata,
        stdout=stdout,
        stderr=stderr,
    )
    return SWEbenchRunResult(
        returncode=returncode,
        prediction=prediction,
        metadata=metadata,
        artifact_dir=artifact_dir,
        predictions_path=predictions_path,
        workspace=workspace,
    )


def invoke_patchfox_cli(
    workspace: Path,
    problem_statement: str,
    session_id: str,
    config: SWEbenchRunConfig,
) -> PatchFoxInvocation:
    """Invoke the existing CLI one-shot path in an isolated child process."""

    prompt_path: Path | None = None
    start = time.monotonic()
    try:
        workspace_parent = workspace.parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="swebench-prompt-",
            dir=workspace_parent,
            delete=False,
        ) as handle:
            handle.write(problem_statement)
            prompt_path = Path(handle.name)

        command = [
            config.python_executable,
            "-m",
            "patchfox",
            "--cwd",
            str(workspace),
            "--prompt-file",
            str(prompt_path),
            "--session-id",
            session_id,
            "--non-interactive",
            "--approval",
            "never",
            "--max-steps",
            str(config.max_steps),
        ]
        if config.config_path:
            command.extend(["--config", str(Path(config.config_path).resolve())])
        if config.provider:
            command.extend(["--provider", config.provider])
        if config.model:
            command.extend(["--model", config.model])
        if config.base_url:
            command.extend(["--base-url", config.base_url])
        if config.max_new_tokens is not None:
            command.extend(["--max-new-tokens", str(config.max_new_tokens)])
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[2],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        return PatchFoxInvocation(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            wall_time_seconds=round(time.monotonic() - start, 6),
        )
    finally:
        if prompt_path:
            prompt_path.unlink(missing_ok=True)


def github_clone_url(repo: str) -> str:
    repo = str(repo).strip()
    if repo.startswith(("https://", "http://", "git@", "ssh://", "file://")):
        return repo
    return f"https://github.com/{repo.removesuffix('.git')}.git"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one PatchFox SWE-bench prediction (no grading)."
    )
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workspace-root", default="/tmp/patchfox-swebench")
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=None,
        help="Existing PatchFox per-model-call output token limit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = SWEbenchRunConfig(
        instance_id=args.instance_id,
        dataset=args.dataset,
        split=args.split,
        run_id=args.run_id,
        workspace_root=Path(args.workspace_root),
        artifact_root=Path(args.artifact_root),
        provider=args.provider,
        model=args.model,
        config_path=Path(args.config_path) if args.config_path else None,
        base_url=args.base_url,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
    )
    result = run_swebench_instance(config)
    print(json.dumps(result.prediction, ensure_ascii=False))
    print(f"prediction: {result.predictions_path}", file=sys.stderr)
    return result.returncode


def _huggingface_dataset_loader(
    dataset: str, split: str
) -> Iterable[Mapping[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "SWE-bench loading requires the optional 'datasets' package; "
            "install it with: python -m pip install datasets"
        ) from exc
    return load_dataset(dataset, split=split, streaming=True)


def _resolve_runtime_identity(config: SWEbenchRunConfig) -> tuple[str, str]:
    provider_config = resolve_provider_config(
        config.provider,
        start=Path.cwd(),
        config_path=config.config_path,
        model=config.model,
        base_url=config.base_url,
    )
    return provider_config.name, provider_config.model


def _collect_evidence(
    workspace: Path, *, session_id: str, returncode: int
) -> dict[str, Any]:
    manifest = build_adapter_metadata(
        workspace, session_id=session_id, returncode=returncode
    )
    evidence = RunEvidence.latest(workspace)
    summary: dict[str, Any] = {
        "manifest": manifest,
        "status": evidence.status(),
        "stop_reason": evidence.stop_reason(),
        "changed_paths": evidence.changed_paths(),
        "tool_names": evidence.tool_names(),
        "metrics": aggregate_run_artifacts(workspace / ".patchfox" / "runs"),
    }
    if evidence.report_path and evidence.trace_path:
        row = extract_usage_from_artifacts(
            evidence.report_path,
            evidence.trace_path,
            task_id=session_id,
            layer="swebench",
            variant="patchfox",
            repeat=0,
            pricing=None,
        )
        summary["usage"] = asdict(row.usage)
        summary["tool_steps"] = row.tool_steps
        summary["attempts"] = row.attempts
    return summary


def _safe_collect_evidence(
    workspace: Path, *, session_id: str, returncode: int
) -> dict[str, Any]:
    try:
        return _collect_evidence(
            workspace, session_id=session_id, returncode=returncode
        )
    except Exception as exc:  # noqa: BLE001 - evidence must not suppress a prediction.
        return {
            "collection_error": {"type": type(exc).__name__, "message": str(exc)},
            "manifest": {
                "returncode": int(returncode),
                "workspace": str(Path(workspace).resolve()),
                "patchfox_evidence_available": False,
            },
        }


def _prediction(instance_id: str, model: str, patch: str) -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "model_name_or_path": model or "patchfox/unknown",
        "model_patch": patch,
    }


def _write_outputs(
    *,
    artifact_dir: Path,
    predictions_path: Path,
    prediction: Mapping[str, str],
    metadata: Mapping[str, Any],
    stdout: str,
    stderr: str,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prediction_text = json.dumps(prediction, ensure_ascii=False)
    (artifact_dir / "prediction.json").write_text(
        prediction_text + "\n", encoding="utf-8"
    )
    (artifact_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (artifact_dir / "patch.diff").write_text(
        str(prediction.get("model_patch", "")), encoding="utf-8"
    )
    (artifact_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (artifact_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    predictions_path.write_text(prediction_text + "\n", encoding="utf-8")


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else os.fsdecode(completed.stderr)
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr.strip()}")
    return completed


def _is_patchfox_state_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == ".patchfox" or normalized.startswith(".patchfox/")


def _session_id(run_id: str, instance_id: str) -> str:
    return f"swebench-{run_id}-{instance_id}".replace("/", "_").replace("\\", "_")


def _validate_component(name: str, value: str) -> None:
    value = str(value or "")
    if not value or value in {".", ".."} or any(char in value for char in "/\\"):
        raise ValueError(f"{name} must be a non-empty path component")


if __name__ == "__main__":
    raise SystemExit(main())
