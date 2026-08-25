"""Single-instance SWE-bench patch generation adapter.

This module deliberately stops at patch generation.  The generated prediction is
intended to be graded later by the official SWE-bench Docker harness.
"""

from __future__ import annotations

import argparse
import hashlib
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
from ..features.sandbox.config import SANDBOX_BACKENDS, SANDBOX_MODES
from .context_cost import extract_usage_from_artifacts
from .harnessbench import build_adapter_metadata
from .metrics import aggregate_run_artifacts
from .run_evidence import RunEvidence

DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_SPLIT = "test"
DEFAULT_ARTIFACT_ROOT = Path("artifacts/swebench")
APPROVAL_POLICIES = ("ask", "auto", "never")
EXECUTION_MODES = ("host", "swebench-image")
CONTAINER_WORKSPACE = "/testbed"
CONTAINER_RUNTIME_PYTHON = "/opt/miniconda3/bin/python"
CONTAINER_PATCHFOX_SOURCE = "/opt/patchfox-src"
CONTAINER_PATCHFOX_RUNTIME = "/opt/patchfox-runtime"
CONTAINER_INPUT_DIR = "/opt/patchfox-input"
TESTBED_PATH = (
    "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
PRIVATE_DATASET_FIELDS = frozenset(
    {
        "image",
        "patch",
        "test_patch",
        "eval_script",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "fail_to_pass",
        "pass_to_pass",
        "hints_text",
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
class SWEbenchDatasetRecord:
    """Public agent input plus execution-only adapter metadata."""

    instance: SWEbenchInstance
    image: str = ""


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
    approval: str = "auto"
    sandbox: str = "required"
    sandbox_backend: str = "auto"
    execution_mode: str = "host"
    docker_executable: str = "docker"
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


class DockerCLI:
    """Small Docker CLI boundary so adapter tests never need a daemon."""

    def __init__(self, executable: str = "docker"):
        self.executable = str(executable)

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [self.executable, *map(str, args)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            env=dict(env) if env is not None else None,
        )
        if check and completed.returncode != 0:
            command = " ".join(map(str, args[:4]))
            raise RuntimeError(
                f"docker {command} failed: {completed.stderr.strip()}"
            )
        return completed


def load_swebench_instance(
    dataset: str,
    split: str,
    instance_id: str,
    *,
    dataset_loader: DatasetLoader | None = None,
) -> SWEbenchInstance:
    """Load one row and immediately reduce it to non-answer issue metadata."""

    return load_swebench_record(
        dataset,
        split,
        instance_id,
        dataset_loader=dataset_loader,
    ).instance


def load_swebench_record(
    dataset: str,
    split: str,
    instance_id: str,
    *,
    dataset_loader: DatasetLoader | None = None,
) -> SWEbenchDatasetRecord:
    """Load public prompt data and keep the image adapter-internal."""

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
        return SWEbenchDatasetRecord(
            instance=SWEbenchInstance(**values),
            image=str(row.get("image", "") or "").strip(),
        )
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
    docker_client: DockerCLI | None = None,
) -> SWEbenchRunResult:
    """Generate one SWE-bench prediction and persist adapter/evidence metadata."""

    _validate_component("instance_id", config.instance_id)
    _validate_component("run_id", config.run_id)
    if config.execution_mode not in EXECUTION_MODES:
        raise ValueError(f"execution_mode must be one of {EXECUTION_MODES}")
    image_mode = config.execution_mode == "swebench-image"
    actual_approval = "auto" if image_mode else config.approval
    actual_sandbox = "off" if image_mode else config.sandbox
    actual_sandbox_backend = "none" if image_mode else config.sandbox_backend
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
        "execution_mode": config.execution_mode,
        "approval": actual_approval,
        "sandbox": actual_sandbox,
        "sandbox_backend": actual_sandbox_backend,
        "outer_sandbox": "docker" if image_mode else "none",
        "inner_sandbox": "off" if image_mode else actual_sandbox,
        "max_steps": config.max_steps,
        "max_new_tokens": config.max_new_tokens,
        "token_budget_scope": "per_model_call"
        if config.max_new_tokens
        else "runtime_default",
    }
    if image_mode:
        metadata.update(
            {
                "image": "",
                "image_digest": "",
                "image_id": "",
                "container_id": "",
                "container_workspace": CONTAINER_WORKSPACE,
                "initial_image_head": "",
                "effective_path": TESTBED_PATH,
                "python_executable": "",
                "python_version": "",
                "patchfox_git_commit": _patchfox_git_commit(),
            }
        )

    try:
        record = load_swebench_record(
            config.dataset,
            config.split,
            config.instance_id,
            dataset_loader=dataset_loader,
        )
        instance = record.instance
        metadata["instance"] = {
            "instance_id": instance.instance_id,
            "repo": instance.repo,
            "base_commit": instance.base_commit,
        }
        if image_mode:
            if not record.image:
                raise ValueError(
                    f"SWE-bench instance {instance.instance_id!r} has no image field"
                )
            metadata["image"] = record.image

        phase = "provider_resolution"
        provider_config = None
        if image_mode or patchfox_invoker is None or not effective_model:
            provider_config = _resolve_runtime_provider(config)
            effective_provider = provider_config.name
            effective_model = provider_config.model
        metadata["provider"] = effective_provider
        metadata["model"] = effective_model
        prediction = _prediction(config.instance_id, effective_model, "")

        workspace = (
            Path(config.workspace_root).resolve()
            / config.run_id
            / config.instance_id
            / "repo"
        )
        metadata["workspace"] = str(workspace)
        session_id = _session_id(config.run_id, config.instance_id)

        if image_mode:
            phase = "image_generation"
            invocation, model_patch = _run_in_swebench_image(
                config=config,
                instance=instance,
                image=record.image,
                problem_statement=instance.problem_statement,
                session_id=session_id,
                provider_config=provider_config,
                evidence_workspace=workspace,
                metadata=metadata,
                docker=docker_client or DockerCLI(config.docker_executable),
            )
        else:
            phase = "workspace_prepare"
            prepare_git_workspace(
                instance, workspace, clone_url_resolver=clone_url_resolver
            )
            phase = "patchfox"
            invoker = patchfox_invoker or invoke_patchfox_cli
            invocation_config = replace(
                config,
                provider=effective_provider or None,
                model=effective_model or None,
            )
            invocation = invoker(
                workspace,
                instance.problem_statement,
                session_id,
                invocation_config,
            )
            model_patch = ""

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

        if not image_mode:
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


def _run_in_swebench_image(
    *,
    config: SWEbenchRunConfig,
    instance: SWEbenchInstance,
    image: str,
    problem_statement: str,
    session_id: str,
    provider_config: Any,
    evidence_workspace: Path,
    metadata: dict[str, Any],
    docker: DockerCLI,
) -> tuple[PatchFoxInvocation, str]:
    """Run the unchanged PatchFox CLI inside an ephemeral official image."""

    evidence_workspace = Path(evidence_workspace).resolve()
    if evidence_workspace.exists():
        raise FileExistsError(f"workspace already exists: {evidence_workspace}")
    evidence_workspace.mkdir(parents=True)
    metadata["patchfox_git_commit"] = _patchfox_git_commit()
    container_id = ""
    container_name = ""
    container_started = False
    evidence_exported = False
    outcome: tuple[PatchFoxInvocation, str] | None = None
    primary_error: Exception | None = None

    try:
        docker.run(["pull", image])
        inspect = docker.run(["image", "inspect", image])
        image_info = _parse_image_inspect(inspect.stdout, image)
        metadata["image_digest"] = image_info["digest"]
        metadata["image_id"] = image_info["id"]

        container_name = _container_name(config.run_id, instance.instance_id)
        created = docker.run(
            [
                "create",
                "--name",
                container_name,
                "--entrypoint",
                "/bin/sh",
                image_info["id"],
                "-lc",
                "tail -f /dev/null",
            ]
        )
        container_id = created.stdout.strip()
        if not container_id:
            raise RuntimeError("docker create returned an empty container id")
        metadata["container_id"] = container_id
        docker.run(["start", container_id])
        container_started = True

        initial_head = _docker_exec(
            docker,
            container_id,
            ["git", "rev-parse", "HEAD"],
            workdir=CONTAINER_WORKSPACE,
        ).stdout.strip()
        metadata["initial_image_head"] = initial_head
        if not _same_commit(initial_head, instance.base_commit):
            raise RuntimeError(
                "official image HEAD does not match dataset base_commit: "
                f"{initial_head} != {instance.base_commit}"
            )
        _require_clean_container_workspace(docker, container_id, "initial image")

        runtime_info = _container_python_info(
            docker, container_id, CONTAINER_RUNTIME_PYTHON
        )
        if tuple(runtime_info["version_info"][:2]) < (3, 10):
            raise RuntimeError(
                f"PatchFox runtime Python must be >=3.10, got {runtime_info['version']}"
            )
        metadata["patchfox_runtime_python"] = runtime_info["executable"]
        metadata["patchfox_runtime_python_version"] = runtime_info["version"]

        _install_patchfox_runtime(docker, container_id)
        _docker_exec(
            docker,
            container_id,
            ["python", "-m", "pip", "install", "-e", "."],
            workdir=CONTAINER_WORKSPACE,
            public_env={"PATH": TESTBED_PATH},
        )
        _require_clean_container_workspace(docker, container_id, "editable install")

        testbed_info = _container_python_info(
            docker,
            container_id,
            "python",
            public_env={"PATH": TESTBED_PATH},
        )
        metadata["effective_path"] = TESTBED_PATH
        metadata["python_executable"] = testbed_info["executable"]
        metadata["python_version"] = testbed_info["version"]

        invocation = _invoke_patchfox_in_container(
            docker=docker,
            container_id=container_id,
            problem_statement=problem_statement,
            session_id=session_id,
            config=config,
            provider_config=provider_config,
        )
        evidence_exported = _export_container_evidence(
            docker, container_id, evidence_workspace
        )
        model_patch = (
            collect_container_model_patch(
                docker, container_id, base_ref=initial_head
            )
            if invocation.returncode == 0
            else ""
        )
        outcome = (invocation, model_patch)
    except Exception as exc:  # noqa: BLE001 - cleanup must run for every failure.
        primary_error = exc
    finally:
        if container_started and not evidence_exported:
            evidence_exported = _export_container_evidence(
                docker, container_id, evidence_workspace
            )
        cleanup_target = container_id or container_name
        if cleanup_target:
            cleanup = docker.run(["rm", "-f", cleanup_target], check=False)
            metadata["container_cleanup"] = (
                "completed" if cleanup.returncode == 0 else "failed"
            )
            if cleanup.returncode != 0:
                metadata["container_cleanup_error"] = cleanup.stderr.strip()
                if primary_error is None:
                    primary_error = RuntimeError(
                        f"failed to remove generation container {cleanup_target}: "
                        f"{cleanup.stderr.strip()}"
                    )

    if primary_error is not None:
        raise primary_error
    if outcome is None:  # pragma: no cover - defensive invariant.
        raise RuntimeError("SWE-bench image run produced no outcome")
    return outcome


def _install_patchfox_runtime(docker: DockerCLI, container_id: str) -> None:
    source_root = Path(__file__).resolve().parents[2]
    _docker_exec(
        docker,
        container_id,
        ["mkdir", "-p", CONTAINER_PATCHFOX_SOURCE, CONTAINER_PATCHFOX_RUNTIME],
        user="root",
    )
    docker.run(
        [
            "cp",
            str(source_root / "pyproject.toml"),
            f"{container_id}:{CONTAINER_PATCHFOX_SOURCE}/pyproject.toml",
        ]
    )
    docker.run(
        [
            "cp",
            str(source_root / "patchfox"),
            f"{container_id}:{CONTAINER_PATCHFOX_SOURCE}/patchfox",
        ]
    )
    _docker_exec(
        docker,
        container_id,
        [
            CONTAINER_RUNTIME_PYTHON,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--target",
            CONTAINER_PATCHFOX_RUNTIME,
            CONTAINER_PATCHFOX_SOURCE,
        ],
        user="root",
    )


def _invoke_patchfox_in_container(
    *,
    docker: DockerCLI,
    container_id: str,
    problem_statement: str,
    session_id: str,
    config: SWEbenchRunConfig,
    provider_config: Any,
) -> PatchFoxInvocation:
    prompt_path: Path | None = None
    start = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as handle:
            handle.write(problem_statement)
            prompt_path = Path(handle.name)
        _docker_exec(
            docker,
            container_id,
            ["mkdir", "-p", CONTAINER_INPUT_DIR],
            user="root",
        )
        container_prompt = f"{CONTAINER_INPUT_DIR}/problem_statement.txt"
        docker.run(["cp", str(prompt_path), f"{container_id}:{container_prompt}"])

        container_config = ""
        if config.config_path:
            container_config = f"{CONTAINER_INPUT_DIR}/config.toml"
            docker.run(
                ["cp", str(Path(config.config_path).resolve()), f"{container_id}:{container_config}"]
            )

        command = [
            CONTAINER_RUNTIME_PYTHON,
            "-m",
            "patchfox",
            "--cwd",
            CONTAINER_WORKSPACE,
            "--prompt-file",
            container_prompt,
            "--session-id",
            session_id,
            "--non-interactive",
            "--approval",
            "auto",
            "--sandbox",
            "off",
            "--sandbox-backend",
            "none",
            "--max-steps",
            str(config.max_steps),
            "--provider",
            provider_config.name,
            "--model",
            provider_config.model,
        ]
        if container_config:
            command.extend(["--config", container_config])
        if provider_config.base_url:
            command.extend(["--base-url", provider_config.base_url])
        if config.max_new_tokens is not None:
            command.extend(["--max-new-tokens", str(config.max_new_tokens)])

        secret_env = {}
        if provider_config.api_key:
            secret_env["PATCHFOX_API_KEY"] = provider_config.api_key
        completed = _docker_exec(
            docker,
            container_id,
            command,
            workdir=CONTAINER_WORKSPACE,
            public_env={
                "HOME": "/root",
                "PATH": TESTBED_PATH,
                "PYTHONPATH": CONTAINER_PATCHFOX_RUNTIME,
                "PATCHFOX_PROTOCOL": provider_config.protocol,
                "PYTHONUNBUFFERED": "1",
            },
            secret_env=secret_env,
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


def collect_container_model_patch(
    docker: DockerCLI, container_id: str, *, base_ref: str = "HEAD"
) -> str:
    raw = _docker_exec(
        docker,
        container_id,
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        workdir=CONTAINER_WORKSPACE,
    ).stdout
    untracked = [
        path
        for path in raw.split("\0")
        if path and not _is_patchfox_state_path(path)
    ]
    if untracked:
        _docker_exec(
            docker,
            container_id,
            ["git", "--literal-pathspecs", "add", "--intent-to-add", "--", *untracked],
            workdir=CONTAINER_WORKSPACE,
        )
    return _docker_exec(
        docker,
        container_id,
        [
            "git",
            "diff",
            "--binary",
            "--no-ext-diff",
            base_ref,
            "--",
            ".",
            ":(exclude).patchfox",
            ":(exclude).patchfox/**",
        ],
        workdir=CONTAINER_WORKSPACE,
    ).stdout


def _docker_exec(
    docker: DockerCLI,
    container_id: str,
    command: Sequence[str],
    *,
    workdir: str | None = None,
    user: str | None = None,
    public_env: Mapping[str, str] | None = None,
    secret_env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = ["exec"]
    if workdir:
        args.extend(["--workdir", workdir])
    if user:
        args.extend(["--user", user])
    for name, value in (public_env or {}).items():
        args.extend(["--env", f"{name}={value}"])
    process_env = dict(os.environ)
    for name, value in (secret_env or {}).items():
        process_env[name] = value
        args.extend(["--env", name])
    args.append(container_id)
    args.extend(map(str, command))
    return docker.run(args, check=check, env=process_env)


def _container_python_info(
    docker: DockerCLI,
    container_id: str,
    python: str,
    *,
    public_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    script = (
        "import json,platform,sys; "
        "print(json.dumps({'executable':sys.executable,'version':platform.python_version(),"
        "'version_info':list(sys.version_info[:3])}))"
    )
    completed = _docker_exec(
        docker,
        container_id,
        [python, "-c", script],
        public_env=public_env,
    )
    try:
        return dict(json.loads(completed.stdout.strip()))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid Python diagnostics: {completed.stdout!r}") from exc


def _require_clean_container_workspace(
    docker: DockerCLI, container_id: str, phase: str
) -> None:
    status = _docker_exec(
        docker,
        container_id,
        ["git", "status", "--porcelain"],
        workdir=CONTAINER_WORKSPACE,
    ).stdout
    if status.strip():
        raise RuntimeError(f"{phase} left /testbed dirty: {status.strip()}")


def _export_container_evidence(
    docker: DockerCLI, container_id: str, evidence_workspace: Path
) -> bool:
    completed = docker.run(
        ["cp", f"{container_id}:{CONTAINER_WORKSPACE}/.patchfox", str(evidence_workspace)],
        check=False,
    )
    return completed.returncode == 0


def _parse_image_inspect(raw: str, image: str) -> dict[str, str]:
    try:
        values = json.loads(raw)
        info = values[0]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid docker image inspect output for {image!r}") from exc
    repo_digests = info.get("RepoDigests") or []
    image_id = str(info.get("Id", ""))
    digest = str(repo_digests[0] if repo_digests else info.get("Id", ""))
    if not digest or not image_id:
        raise RuntimeError(f"docker image inspect returned no identity for {image!r}")
    return {"digest": digest, "id": image_id}


def _container_name(run_id: str, instance_id: str) -> str:
    raw = f"patchfox-sweb-{run_id}-{instance_id}".lower()
    safe = "".join(char if char.isalnum() or char in "_.-" else "-" for char in raw)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:100]}-{digest}"


def _same_commit(left: str, right: str) -> bool:
    left = str(left).strip().lower()
    right = str(right).strip().lower()
    return bool(left and right and (left.startswith(right) or right.startswith(left)))


def _patchfox_git_commit() -> str:
    try:
        return _run_git(
            ["rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2]
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - diagnostics must not block generation.
        return "unknown"


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
            config.approval,
            "--sandbox",
            config.sandbox,
            "--sandbox-backend",
            config.sandbox_backend,
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
    parser.add_argument(
        "--execution-mode",
        choices=EXECUTION_MODES,
        default="host",
        help=(
            "Execution environment. 'host' keeps the required Bubblewrap path; "
            "'swebench-image' runs PatchFox inside the official instance container "
            "with Docker as the outer sandbox and the inner sandbox off."
        ),
    )
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument(
        "--approval",
        choices=APPROVAL_POLICIES,
        default="auto",
        help=(
            "Approval policy passed to PatchFox. 'auto' permits risky tools without "
            "human confirmation, while ToolPolicy, workspace path checks, and the "
            "selected execution boundary still apply. swebench-image always uses auto."
        ),
    )
    parser.add_argument(
        "--sandbox",
        choices=sorted(SANDBOX_MODES),
        default="required",
        help=(
            "Sandbox mode for run_shell. 'required' fails closed when the configured "
            "sandbox backend is unavailable. Host mode only; swebench-image forces "
            "the inner sandbox off and uses Docker as its outer boundary."
        ),
    )
    parser.add_argument(
        "--sandbox-backend",
        choices=sorted(SANDBOX_BACKENDS),
        default="auto",
        help=(
            "Existing PatchFox sandbox backend selector. 'auto' selects an available "
            "supported backend. Host mode only."
        ),
    )
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
        approval=args.approval,
        sandbox=args.sandbox,
        sandbox_backend=args.sandbox_backend,
        execution_mode=args.execution_mode,
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


def _resolve_runtime_provider(config: SWEbenchRunConfig):
    return resolve_provider_config(
        config.provider,
        start=Path.cwd(),
        config_path=config.config_path,
        model=config.model,
        base_url=config.base_url,
    )


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
