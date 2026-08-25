"""Batch orchestration and statistics for PatchFox SWE-bench experiments.

This module deliberately treats :mod:`patchfox.evaluation.swebench_runner` as
the only patch-generation implementation.  It selects instances, schedules the
existing single-instance adapter, invokes the official SWE-bench harness, and
summarizes the artifacts that those two systems produced.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .run_evidence import RunEvidence
from .swebench_runner import (
    APPROVAL_POLICIES,
    DEFAULT_DATASET,
    DEFAULT_SPLIT,
    EXECUTION_MODES,
    SWEbenchRunConfig,
    SWEbenchRunResult,
    run_swebench_instance,
)

DEFAULT_EXCLUDED_INSTANCES = ("sympy__sympy-20590",)
DEFAULT_EXPERIMENT_ROOT = Path("artifacts/swebench_experiments")
PROGRESS_STATES = frozenset({"pending", "running", "completed", "failed"})

CSV_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "image_digest",
    "generation_status",
    "official_status",
    "resolved",
    "model_patch_bytes",
    "changed_paths",
    "stop_reason",
    "tool_steps",
    "model_calls",
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "read_file_calls",
    "search_calls",
    "run_shell_calls",
    "patch_file_calls",
    "write_file_calls",
    "first_change_step",
    "verification_after_change",
    "max_consecutive_explore_steps",
    "repeated_source_read_count",
    "overlapping_read_count",
    "duplicate_source_filtered_count",
    "recent_source_filtered_count",
    "patchfox_wall_time_seconds",
    "adapter_wall_time_seconds",
    "error_type",
    "error_message",
)


@dataclass(frozen=True)
class DatasetCatalog:
    """In-memory dataset rows plus an optional Hub revision.

    Rows are adapter-internal only.  They are never serialized by this module,
    and the existing single-instance adapter remains responsible for reducing a
    row to its agent-visible public fields.
    """

    rows: tuple[Mapping[str, Any], ...]
    revision: str | None = None


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    num_instances: int
    seed: int = 42
    dataset: str = DEFAULT_DATASET
    split: str = DEFAULT_SPLIT
    excluded_instances: tuple[str, ...] = DEFAULT_EXCLUDED_INSTANCES
    artifact_root: Path = DEFAULT_EXPERIMENT_ROOT
    workspace_root: Path = Path("/tmp/patchfox-swebench")
    execution_mode: str = "swebench-image"
    provider: str | None = None
    model: str | None = None
    config_path: Path | None = None
    base_url: str | None = None
    max_steps: int = 60
    max_new_tokens: int | None = 8192
    approval: str = "auto"
    sandbox: str = "required"
    sandbox_backend: str = "auto"
    generation_workers: int = 2
    eval_workers: int = 4
    swebench_python: str = sys.executable

    @property
    def experiment_dir(self) -> Path:
        return Path(self.artifact_root).resolve() / self.experiment_id


@dataclass(frozen=True)
class ExperimentRunResult:
    experiment_dir: Path
    predictions_path: Path
    summary_path: Path | None
    harness_returncode: int | None


CatalogLoader = Callable[[str, str], DatasetCatalog]
InstanceRunner = Callable[..., SWEbenchRunResult]
SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_dataset_catalog(dataset: str, split: str) -> DatasetCatalog:
    """Load Verified once for deterministic selection and adapter reuse."""

    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "SWE-bench experiments require the optional 'datasets' package; "
            "install it with: python -m pip install -e '.[swebench]'"
        ) from exc

    loaded = load_dataset(dataset, split=split)
    rows = tuple(dict(row) for row in loaded)
    revision = None
    try:
        from huggingface_hub import HfApi

        revision = str(HfApi().dataset_info(dataset).sha or "") or None
    except Exception:  # noqa: BLE001 - revision is optional diagnostics.
        revision = None
    return DatasetCatalog(rows=rows, revision=revision)


def deterministic_instance_order(
    instance_ids: Iterable[str], *, seed: int, excluded_instances: Iterable[str]
) -> list[str]:
    """Sort then shuffle the full eligible population exactly once."""

    excluded = {str(value) for value in excluded_instances}
    eligible = sorted({str(value) for value in instance_ids} - excluded)
    random.Random(int(seed)).shuffle(eligible)
    return eligible


def prepare_selection(
    config: ExperimentConfig,
    *,
    catalog: DatasetCatalog | None = None,
    catalog_loader: CatalogLoader = load_dataset_catalog,
    force_new_selection: bool = False,
) -> tuple[dict[str, Any], DatasetCatalog | None]:
    """Create or validate selection_order.json and return the current prefix."""

    experiment_dir = config.experiment_dir
    experiment_dir.mkdir(parents=True, exist_ok=True)
    path = experiment_dir / "selection_order.json"
    excluded = sorted(set(config.excluded_instances))

    if path.exists() and not force_new_selection:
        selection = _read_json(path)
        conflicts = _selection_conflicts(selection, config, excluded)
        if conflicts:
            joined = ", ".join(conflicts)
            raise ValueError(
                f"existing experiment selection conflicts on: {joined}; "
                "use --force-new-selection to replace it"
            )
        order = list(selection.get("shuffled_instance_order") or [])
        if config.num_instances > len(order):
            raise ValueError(
                f"num_instances={config.num_instances} exceeds eligible_count={len(order)}"
            )
        selection["num_instances"] = config.num_instances
        selection["selected_instance_ids"] = order[: config.num_instances]
        _atomic_write_json(path, selection)
        return selection, catalog

    if catalog is None:
        catalog = catalog_loader(config.dataset, config.split)
    ids = [str(row.get("instance_id", "")) for row in catalog.rows]
    if any(not instance_id for instance_id in ids):
        raise ValueError("dataset contains a row without instance_id")
    order = deterministic_instance_order(
        ids, seed=config.seed, excluded_instances=excluded
    )
    if config.num_instances > len(order):
        raise ValueError(
            f"num_instances={config.num_instances} exceeds eligible_count={len(order)}"
        )
    selection = {
        "schema_version": 1,
        "dataset": config.dataset,
        "split": config.split,
        "dataset_revision": catalog.revision,
        "seed": config.seed,
        "excluded_instances": excluded,
        "eligible_count": len(order),
        "shuffled_instance_order": order,
        "num_instances": config.num_instances,
        "selected_instance_ids": order[: config.num_instances],
    }
    _atomic_write_json(path, selection)
    return selection, catalog


def _selection_conflicts(
    selection: Mapping[str, Any], config: ExperimentConfig, excluded: list[str]
) -> list[str]:
    conflicts = []
    expected = {
        "dataset": config.dataset,
        "split": config.split,
        "seed": config.seed,
        "excluded_instances": excluded,
    }
    for key, value in expected.items():
        current = selection.get(key)
        if key == "excluded_instances":
            current = sorted(current or [])
        if current != value:
            conflicts.append(key)
    return conflicts


def prepare_experiment_config(
    config: ExperimentConfig,
    selection: Mapping[str, Any],
    *,
    force_new_selection: bool = False,
) -> dict[str, Any]:
    path = config.experiment_dir / "experiment_config.json"
    existing = _read_json(path) if path.exists() else {}
    if existing and not force_new_selection:
        conflicts = _config_conflicts(existing, config)
        if conflicts:
            raise ValueError(
                "existing experiment config conflicts on: " + ", ".join(conflicts)
            )

    created_at = str(existing.get("created_at") or utc_now())
    git_commit, git_dirty = _git_identity()
    payload = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "created_at": created_at,
        "updated_at": utc_now(),
        "patchfox_git_commit": git_commit,
        "patchfox_git_dirty": git_dirty,
        "dataset": config.dataset,
        "split": config.split,
        "dataset_revision": selection.get("dataset_revision"),
        "seed": config.seed,
        "num_instances": config.num_instances,
        "excluded_instances": sorted(set(config.excluded_instances)),
        "selected_instance_ids": list(selection["selected_instance_ids"]),
        "provider": config.provider,
        "model": config.model,
        "max_steps": config.max_steps,
        "max_new_tokens": config.max_new_tokens,
        "execution_mode": config.execution_mode,
        "approval": config.approval,
        "sandbox": config.sandbox,
        "sandbox_backend": config.sandbox_backend,
        "workspace_root": str(Path(config.workspace_root).resolve()),
        "artifact_root": str(Path(config.artifact_root).resolve()),
        "config_path": str(Path(config.config_path).resolve())
        if config.config_path
        else None,
        "base_url": config.base_url,
        "generation_workers": config.generation_workers,
        "eval_workers": config.eval_workers,
        "swebench_package_version": _swebench_version(config.swebench_python),
        "swebench_python_executable": _resolved_executable(config.swebench_python),
        "system": _system_info(),
    }
    _atomic_write_json(path, payload)
    return payload


def _config_conflicts(
    existing: Mapping[str, Any], config: ExperimentConfig
) -> list[str]:
    immutable = {
        "experiment_id": config.experiment_id,
        "dataset": config.dataset,
        "split": config.split,
        "seed": config.seed,
        "excluded_instances": sorted(set(config.excluded_instances)),
        "provider": config.provider,
        "model": config.model,
        "max_steps": config.max_steps,
        "max_new_tokens": config.max_new_tokens,
        "execution_mode": config.execution_mode,
        "approval": config.approval,
        "sandbox": config.sandbox,
        "sandbox_backend": config.sandbox_backend,
        "config_path": str(Path(config.config_path).resolve())
        if config.config_path
        else None,
        "base_url": config.base_url,
    }
    conflicts = []
    for key, value in immutable.items():
        current = existing.get(key)
        if key == "excluded_instances":
            current = sorted(current or [])
        if current != value:
            conflicts.append(key)
    return conflicts


def initialize_progress(
    experiment_dir: Path, selected_ids: Sequence[str], *, reset: bool = False
) -> dict[str, Any]:
    path = Path(experiment_dir) / "progress.json"
    progress = {} if reset or not path.exists() else _read_json(path)
    instances = dict(progress.get("instances") or {})
    for instance_id in selected_ids:
        row = dict(instances.get(instance_id) or {})
        status = str(row.get("status") or "pending")
        if status not in PROGRESS_STATES:
            status = "pending"
        row.setdefault("attempts", 0)
        row["status"] = status
        instances[instance_id] = row
    payload = {
        "schema_version": 1,
        "updated_at": utc_now(),
        "instances": instances,
    }
    _atomic_write_json(path, payload)
    return payload


def run_generation(
    config: ExperimentConfig,
    selected_ids: Sequence[str],
    *,
    catalog: DatasetCatalog,
    resume: bool,
    rerun_failed: bool,
    runner: InstanceRunner = run_swebench_instance,
) -> dict[str, Any]:
    """Run selected instances concurrently while persisting every transition."""

    progress_path = config.experiment_dir / "progress.json"
    progress = initialize_progress(config.experiment_dir, selected_ids)
    lock = threading.Lock()

    def persist() -> None:
        progress["updated_at"] = utc_now()
        _atomic_write_json(progress_path, progress)

    scheduled: list[str] = []
    for instance_id in selected_ids:
        row = progress["instances"][instance_id]
        status = row["status"]
        if rerun_failed:
            if status == "failed":
                scheduled.append(instance_id)
            continue
        if resume and _valid_completed_artifact(config.experiment_dir, instance_id):
            row["status"] = "completed"
            row.setdefault("completed_at", utc_now())
            persist()
            continue
        if resume and status == "failed":
            continue
        scheduled.append(instance_id)

    def execute(instance_id: str) -> tuple[str, str]:
        with lock:
            row = progress["instances"][instance_id]
            attempt = int(row.get("attempts", 0)) + 1
            row.update(
                {
                    "status": "running",
                    "attempts": attempt,
                    "started_at": utc_now(),
                    "completed_at": None,
                    "error": None,
                }
            )
            persist()

        run_config = SWEbenchRunConfig(
            instance_id=instance_id,
            dataset=config.dataset,
            split=config.split,
            run_id="generation",
            workspace_root=(
                Path(config.workspace_root).resolve()
                / config.experiment_id
                / f"attempt-{attempt}-{uuid.uuid4().hex[:8]}"
            ),
            artifact_root=config.experiment_dir,
            provider=config.provider,
            model=config.model,
            config_path=config.config_path,
            base_url=config.base_url,
            max_steps=config.max_steps,
            max_new_tokens=config.max_new_tokens,
            approval=config.approval,
            sandbox=config.sandbox,
            sandbox_backend=config.sandbox_backend,
            execution_mode=config.execution_mode,
        )
        try:
            result = runner(
                run_config,
                dataset_loader=lambda _dataset, _split: catalog.rows,
            )
            _materialize_evidence(result)
            completed = (
                result.returncode == 0
                and str(result.metadata.get("status")) == "completed"
            )
            status = "completed" if completed else "failed"
            error = result.metadata.get("error") if not completed else None
            returncode = int(result.returncode)
        except Exception as exc:  # noqa: BLE001 - one instance must not stop the batch.
            status = "failed"
            returncode = 1
            error = {"type": type(exc).__name__, "message": str(exc)}
            _write_generation_failure(config, instance_id, error)

        with lock:
            row = progress["instances"][instance_id]
            row.update(
                {
                    "status": status,
                    "completed_at": utc_now(),
                    "returncode": returncode,
                    "error": error,
                }
            )
            persist()
        return instance_id, status

    executor = ThreadPoolExecutor(max_workers=max(1, config.generation_workers))
    futures: list[Future[tuple[str, str]]] = []
    try:
        futures = [executor.submit(execute, instance_id) for instance_id in scheduled]
        for future in as_completed(futures):
            future.result()
    except BaseException:
        for future in futures:
            future.cancel()
        with lock:
            persist()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return progress


def _valid_completed_artifact(experiment_dir: Path, instance_id: str) -> bool:
    artifact_dir = Path(experiment_dir) / "generation" / instance_id
    metadata = _read_json_if_exists(artifact_dir / "metadata.json")
    prediction = _read_json_if_exists(artifact_dir / "prediction.json")
    return bool(
        metadata.get("status") == "completed"
        and prediction.get("instance_id") == instance_id
        and isinstance(prediction.get("model_patch"), str)
        and isinstance(prediction.get("model_name_or_path"), str)
    )


def _write_generation_failure(
    config: ExperimentConfig, instance_id: str, error: Mapping[str, Any]
) -> None:
    artifact_dir = config.experiment_dir / "generation" / instance_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prediction = {
        "instance_id": instance_id,
        "model_name_or_path": config.model or "patchfox/unknown",
        "model_patch": "",
    }
    metadata = {
        "schema_version": 1,
        "status": "error",
        "instance_id": instance_id,
        "dataset": config.dataset,
        "split": config.split,
        "provider": config.provider,
        "model": config.model,
        "error": dict(error),
    }
    _atomic_write_json(artifact_dir / "prediction.json", prediction)
    _atomic_write_json(artifact_dir / "metadata.json", metadata)
    (artifact_dir / "patch.diff").write_text("", encoding="utf-8")
    (artifact_dir / "stdout.log").write_text("", encoding="utf-8")
    (artifact_dir / "stderr.log").write_text(
        f"{error.get('type', 'Error')}: {error.get('message', '')}\n",
        encoding="utf-8",
    )


def _materialize_evidence(result: SWEbenchRunResult) -> None:
    if result.workspace is None or not Path(result.workspace).exists():
        return
    evidence = RunEvidence.latest(result.workspace)
    target = Path(result.artifact_dir) / "evidence"
    copies = (
        (evidence.trace_path, "trace.jsonl"),
        (evidence.report_path, "report.json"),
        (evidence.task_state_path, "task_state.json"),
        (evidence.session_path, "session.json"),
        (evidence.session_event_path, "session.events.jsonl"),
    )
    for source, name in copies:
        if source and source.exists():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)


def build_predictions(
    experiment_dir: Path,
    selected_ids: Sequence[str],
    *,
    default_model: str | None,
) -> Path:
    """Write exactly one official-shaped row per selected instance, in order."""

    rows = []
    for instance_id in selected_ids:
        path = Path(experiment_dir) / "generation" / instance_id / "prediction.json"
        row = _read_json_if_exists(path)
        if (
            row.get("instance_id") != instance_id
            or not isinstance(row.get("model_patch"), str)
            or not isinstance(row.get("model_name_or_path"), str)
        ):
            row = {
                "instance_id": instance_id,
                "model_name_or_path": default_model or "patchfox/unknown",
                "model_patch": "",
            }
        rows.append(
            {
                "instance_id": instance_id,
                "model_name_or_path": str(row["model_name_or_path"]),
                "model_patch": str(row["model_patch"]),
            }
        )
    path = Path(experiment_dir) / "predictions.jsonl"
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")
    return path


def run_official_evaluation(
    config: ExperimentConfig,
    selected_ids: Sequence[str],
    predictions_path: Path,
    *,
    subprocess_runner: SubprocessRunner = subprocess.run,
) -> tuple[int, dict[str, Any]]:
    """Invoke the official harness in a controlled result directory."""

    evaluation_dir = config.experiment_dir / "evaluation"
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    command = [
        _resolved_executable(config.swebench_python),
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        config.dataset,
        "--split",
        config.split,
        "--predictions_path",
        str(Path(predictions_path).resolve()),
        "--instance_ids",
        *selected_ids,
        "--run_id",
        config.experiment_id,
        "--max_workers",
        str(config.eval_workers),
    ]
    started_at = utc_now()
    completed = subprocess_runner(
        command,
        cwd=evaluation_dir,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    stdout = str(completed.stdout or "")
    stderr = str(completed.stderr or "")
    log_text = stdout
    if stderr:
        log_text += "\n" if log_text and not log_text.endswith("\n") else ""
        log_text += "[stderr]\n" + stderr
    (evaluation_dir / "official_eval.log").write_text(log_text, encoding="utf-8")
    run_metadata = {
        "schema_version": 1,
        "started_at": started_at,
        "completed_at": utc_now(),
        "command": command,
        "cwd": str(evaluation_dir.resolve()),
        "returncode": int(completed.returncode),
    }
    _atomic_write_json(evaluation_dir / "harness_run.json", run_metadata)
    results = discover_official_results(
        evaluation_dir,
        selected_ids,
        run_id=config.experiment_id,
        stdout=stdout,
    )
    results["harness_returncode"] = int(completed.returncode)
    _atomic_write_json(evaluation_dir / "official_results.json", results)
    return int(completed.returncode), results


def discover_official_results(
    evaluation_dir: Path,
    selected_ids: Sequence[str],
    *,
    run_id: str,
    stdout: str = "",
) -> dict[str, Any]:
    """Read official aggregate/per-instance reports without re-grading tests."""

    evaluation_dir = Path(evaluation_dir)
    statuses: dict[str, dict[str, Any]] = {
        instance_id: {"official_status": "unavailable", "resolved": None}
        for instance_id in selected_ids
    }
    source_files: list[str] = []

    aggregate = _find_aggregate_report(evaluation_dir, run_id)
    if aggregate:
        payload = _read_json(aggregate)
        source_files.append(str(aggregate.resolve()))
        _apply_aggregate_report(statuses, payload)

    for report_path in evaluation_dir.rglob("report.json"):
        payload = _read_json_if_exists(report_path)
        matched = False
        for instance_id in selected_ids:
            item = payload.get(instance_id)
            if isinstance(item, Mapping) and isinstance(item.get("resolved"), bool):
                resolved = bool(item["resolved"])
                statuses[instance_id] = {
                    "official_status": "resolved" if resolved else "unresolved",
                    "resolved": resolved,
                }
                matched = True
        if matched:
            source_files.append(str(report_path.resolve()))

    if stdout:
        pattern = re.compile(
            r"Result for\s+([^:\s]+):\s+resolved:\s*(True|False)", re.IGNORECASE
        )
        for match in pattern.finditer(stdout):
            instance_id, raw = match.groups()
            if instance_id in statuses and statuses[instance_id]["resolved"] is None:
                resolved = raw.lower() == "true"
                statuses[instance_id] = {
                    "official_status": "resolved" if resolved else "unresolved",
                    "resolved": resolved,
                }

    return {
        "schema_version": 1,
        "run_id": run_id,
        "source_files": sorted(set(source_files)),
        "per_instance": statuses,
    }


def _find_aggregate_report(evaluation_dir: Path, run_id: str) -> Path | None:
    candidates = []
    for path in evaluation_dir.rglob("*.json"):
        if path.name in {"harness_run.json", "official_results.json"}:
            continue
        payload = _read_json_if_exists(path)
        if isinstance(payload.get("resolved_ids"), list):
            score = 2 if run_id in path.name else 1
            candidates.append((score, path.stat().st_mtime, path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _apply_aggregate_report(
    statuses: dict[str, dict[str, Any]], payload: Mapping[str, Any]
) -> None:
    mappings = (
        ("resolved_ids", "resolved", True),
        ("unresolved_ids", "unresolved", False),
        ("empty_patch_ids", "empty_patch", None),
        ("error_ids", "error", None),
        ("incomplete_ids", "incomplete", None),
    )
    for key, status, resolved in mappings:
        for instance_id in payload.get(key) or []:
            if instance_id in statuses:
                statuses[instance_id] = {
                    "official_status": status,
                    "resolved": resolved,
                }
    for instance_id in payload.get("completed_ids") or []:
        if (
            instance_id in statuses
            and statuses[instance_id]["official_status"] == "unavailable"
        ):
            statuses[instance_id] = {
                "official_status": "completed",
                "resolved": None,
            }


def generate_statistics(
    config: ExperimentConfig,
    selected_ids: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build per-instance rows and aggregate metrics from persisted evidence."""

    official_path = config.experiment_dir / "evaluation" / "official_results.json"
    official = _read_json_if_exists(official_path).get("per_instance") or {}
    rows = [
        _per_instance_row(config.experiment_dir, instance_id, official.get(instance_id))
        for instance_id in selected_ids
    ]
    summary = _aggregate_rows(rows)
    _write_per_instance_csv(config.experiment_dir / "per_instance_results.csv", rows)
    _atomic_write_json(config.experiment_dir / "experiment_summary.json", summary)
    (config.experiment_dir / "experiment_summary.md").write_text(
        _summary_markdown(summary), encoding="utf-8"
    )
    return summary, rows


def _per_instance_row(
    experiment_dir: Path,
    instance_id: str,
    official: Mapping[str, Any] | None,
) -> dict[str, Any]:
    artifact_dir = Path(experiment_dir) / "generation" / instance_id
    metadata = _read_json_if_exists(artifact_dir / "metadata.json")
    prediction = _read_json_if_exists(artifact_dir / "prediction.json")
    report = _read_json_if_exists(artifact_dir / "evidence" / "report.json")
    task_state = _read_json_if_exists(artifact_dir / "evidence" / "task_state.json")
    if not task_state:
        task_state = dict(report.get("task_state") or {})
    trace = _read_jsonl(artifact_dir / "evidence" / "trace.jsonl")
    evidence = dict(metadata.get("evidence") or {})
    usage = dict(evidence.get("usage") or {})
    instance = dict(metadata.get("instance") or {})
    error = dict(metadata.get("error") or {})
    changed_paths = (
        task_state.get("changed_paths")
        or (report.get("artifact_graph") or {}).get("changed_paths")
        or evidence.get("changed_paths")
        or []
    )
    tools = [
        str(event.get("name") or event.get("tool_name") or event.get("tool") or "")
        for event in trace
        if event.get("event") == "tool_executed"
    ]
    memory_events = [
        event for event in trace if event.get("event") == "memory.retrieval"
    ]
    official = dict(official or {})

    def task_metric(name: str) -> Any:
        if name in task_state:
            return task_state[name]
        progress = task_state.get("runtime_progress") or {}
        return progress.get(name)

    patch = prediction.get("model_patch")
    patch_bytes = metadata.get("model_patch_bytes")
    if patch_bytes is None and isinstance(patch, str):
        patch_bytes = len(patch.encode("utf-8"))

    return {
        "instance_id": instance_id,
        "repo": instance.get("repo"),
        "base_commit": instance.get("base_commit"),
        "image_digest": metadata.get("image_digest"),
        "generation_status": (
            "completed" if metadata.get("status") == "completed" else "failed"
        ),
        "official_status": official.get("official_status", "unavailable"),
        "resolved": official.get("resolved"),
        "model_patch_bytes": _number_or_none(patch_bytes),
        "changed_paths": list(changed_paths),
        "stop_reason": report.get("stop_reason") or evidence.get("stop_reason"),
        "tool_steps": _first_number(
            evidence.get("tool_steps"), report.get("tool_steps")
        ),
        "model_calls": _first_number(
            usage.get("model_call_count"),
            _event_count_or_none(trace, "model_parsed"),
        ),
        "input_tokens": _number_or_none(usage.get("input_tokens")),
        "cached_tokens": _number_or_none(usage.get("cached_tokens")),
        "output_tokens": _number_or_none(usage.get("output_tokens")),
        "read_file_calls": _tool_count_or_none(tools, "read_file", trace),
        "search_calls": _tool_count_or_none(tools, "search", trace),
        "run_shell_calls": _tool_count_or_none(tools, "run_shell", trace),
        "patch_file_calls": _tool_count_or_none(tools, "patch_file", trace),
        "write_file_calls": _tool_count_or_none(tools, "write_file", trace),
        "first_change_step": _number_or_none(task_metric("first_change_step")),
        "verification_after_change": _bool_or_none(
            task_metric("verification_after_change")
        ),
        "max_consecutive_explore_steps": _number_or_none(
            task_metric("max_consecutive_explore_steps")
        ),
        "repeated_source_read_count": _number_or_none(
            task_metric("repeated_source_read_count")
        ),
        "overlapping_read_count": _number_or_none(
            task_metric("overlapping_read_count")
        ),
        "duplicate_source_filtered_count": _sum_event_field_or_none(
            memory_events, "duplicate_source_filtered_count"
        ),
        "recent_source_filtered_count": _sum_event_field_or_none(
            memory_events, "recent_source_filtered_count"
        ),
        "patchfox_wall_time_seconds": _number_or_none(
            metadata.get("patchfox_wall_time_seconds")
        ),
        "adapter_wall_time_seconds": _number_or_none(
            metadata.get("adapter_wall_time_seconds")
        ),
        "error_type": error.get("type"),
        "error_message": error.get("message"),
        "changed_instance": bool(changed_paths)
        or task_metric("first_change_step") is not None,
        "final_answer_present": bool(
            report.get("final_answer")
            or task_state.get("final_answer")
            or (report.get("stop_reason") == "final_answer_returned")
        ),
    }


def _aggregate_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    completed = sum(row["generation_status"] == "completed" for row in rows)
    failed = total - completed
    non_empty = sum(
        (_number_or_none(row.get("model_patch_bytes")) or 0) > 0 for row in rows
    )
    empty = total - non_empty
    official_completed = sum(
        row.get("official_status") in {"resolved", "unresolved"} for row in rows
    )
    resolved = sum(row.get("resolved") is True for row in rows)
    unresolved = sum(row.get("resolved") is False for row in rows)
    step_limit = sum(row.get("stop_reason") == "step_limit_reached" for row in rows)
    final_answer = sum(bool(row.get("final_answer_present")) for row in rows)
    changed = [row for row in rows if row.get("changed_instance")]
    verified = sum(row.get("verification_after_change") is True for row in changed)
    verification_rate = (
        None
        if not changed
        or any(row.get("verification_after_change") is None for row in changed)
        else _rate(verified, len(changed))
    )

    def values(name: str) -> list[float]:
        return [
            float(row[name])
            for row in rows
            if _number_or_none(row.get(name)) is not None
        ]

    def stats(name: str) -> tuple[float | None, float | None, float | None]:
        found = values(name)
        return _sum(found), _mean(found), _median(found)

    input_total, input_mean, input_median = stats("input_tokens")
    output_total, output_mean, output_median = stats("output_tokens")
    cached_total, _, _ = stats("cached_tokens")
    patchfox_total, patchfox_mean, patchfox_median = stats("patchfox_wall_time_seconds")
    adapter_total, adapter_mean, _ = stats("adapter_wall_time_seconds")
    repeated_total, repeated_mean, _ = stats("repeated_source_read_count")
    overlap_total, overlap_mean, _ = stats("overlapping_read_count")
    duplicate_total, _, _ = stats("duplicate_source_filtered_count")
    recent_total, _, _ = stats("recent_source_filtered_count")

    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "results": {
            "total_instances": total,
            "generation_completed": completed,
            "generation_failed": failed,
            "non_empty_patch_count": non_empty,
            "empty_patch_count": empty,
            "empty_patch_rate": _rate(empty, total),
            "official_completed": official_completed,
            "official_resolved": resolved,
            "official_unresolved": unresolved,
            "resolved_rate": _rate(resolved, official_completed),
            "step_limit_count": step_limit,
            "step_limit_rate": _rate(step_limit, total),
            "final_answer_count": final_answer,
            "final_answer_rate": _rate(final_answer, total),
        },
        "tokens": {
            "total_input_tokens": input_total,
            "mean_input_tokens": input_mean,
            "median_input_tokens": input_median,
            "total_output_tokens": output_total,
            "mean_output_tokens": output_mean,
            "median_output_tokens": output_median,
            "total_cached_tokens": cached_total,
        },
        "agent_behavior": {
            "mean_tool_steps": _mean(values("tool_steps")),
            "median_tool_steps": _median(values("tool_steps")),
            "mean_model_calls": _mean(values("model_calls")),
            "median_model_calls": _median(values("model_calls")),
            "mean_read_file_calls": _mean(values("read_file_calls")),
            "mean_search_calls": _mean(values("search_calls")),
            "mean_run_shell_calls": _mean(values("run_shell_calls")),
            "mean_patch_file_calls": _mean(values("patch_file_calls")),
            "mean_first_change_step": _mean(values("first_change_step")),
            "median_first_change_step": _median(values("first_change_step")),
            "changed_instance_rate": _rate(len(changed), total),
            "verification_after_change_rate": verification_rate,
        },
        "p1_memory_convergence": {
            "mean_max_consecutive_explore_steps": _mean(
                values("max_consecutive_explore_steps")
            ),
            "total_repeated_source_read_count": repeated_total,
            "mean_repeated_source_read_count": repeated_mean,
            "total_overlapping_read_count": overlap_total,
            "mean_overlapping_read_count": overlap_mean,
            "total_duplicate_source_filtered_count": duplicate_total,
            "total_recent_source_filtered_count": recent_total,
        },
        "time": {
            "total_patchfox_wall_time": patchfox_total,
            "mean_patchfox_wall_time": patchfox_mean,
            "median_patchfox_wall_time": patchfox_median,
            "total_adapter_wall_time": adapter_total,
            "mean_adapter_wall_time": adapter_mean,
        },
    }


def _write_per_instance_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            serialized = dict(row)
            serialized["changed_paths"] = json.dumps(
                row.get("changed_paths") or [], ensure_ascii=False
            )
            writer.writerow(serialized)


def _summary_markdown(summary: Mapping[str, Any]) -> str:
    lines = ["# SWE-bench Experiment Summary", ""]
    for section, metrics in summary.items():
        if section in {"schema_version", "generated_at"}:
            continue
        lines.extend([f"## {section.replace('_', ' ').title()}", ""])
        for name, value in metrics.items():
            rendered = "unavailable" if value is None else str(value)
            lines.append(f"- {name}: {rendered}")
        lines.append("")
    return "\n".join(lines)


def run_experiment(
    config: ExperimentConfig,
    *,
    generation: bool = True,
    evaluation: bool = True,
    stats: bool = True,
    resume: bool = False,
    rerun_failed: bool = False,
    force_new_selection: bool = False,
    catalog_loader: CatalogLoader = load_dataset_catalog,
    runner: InstanceRunner = run_swebench_instance,
    subprocess_runner: SubprocessRunner = subprocess.run,
) -> ExperimentRunResult:
    """Execute selected phases without changing single-instance semantics."""

    if config.num_instances <= 0:
        raise ValueError("num_instances must be positive")
    if config.generation_workers <= 0 or config.eval_workers <= 0:
        raise ValueError("worker counts must be positive")
    if config.execution_mode not in EXECUTION_MODES:
        raise ValueError(f"unsupported execution_mode: {config.execution_mode}")
    if config.approval not in APPROVAL_POLICIES:
        raise ValueError(f"unsupported approval policy: {config.approval}")

    config_path = config.experiment_dir / "experiment_config.json"
    selection_path = config.experiment_dir / "selection_order.json"
    if not generation and not selection_path.exists():
        raise FileNotFoundError(
            "evaluation-only/stats-only requires an existing selection_order.json"
        )
    if (
        generation
        and config_path.exists()
        and not (resume or rerun_failed or force_new_selection)
    ):
        raise FileExistsError(
            "experiment already exists; pass --resume to continue without rerunning "
            "completed instances"
        )

    catalog = catalog_loader(config.dataset, config.split) if generation else None
    selection, catalog = prepare_selection(
        config,
        catalog=catalog,
        catalog_loader=catalog_loader,
        force_new_selection=force_new_selection,
    )
    selected_ids = list(selection["selected_instance_ids"])
    prepare_experiment_config(
        config, selection, force_new_selection=force_new_selection
    )
    initialize_progress(config.experiment_dir, selected_ids, reset=force_new_selection)

    if generation:
        if catalog is None:
            catalog = catalog_loader(config.dataset, config.split)
        run_generation(
            config,
            selected_ids,
            catalog=catalog,
            resume=resume,
            rerun_failed=rerun_failed,
            runner=runner,
        )

    predictions_path = build_predictions(
        config.experiment_dir, selected_ids, default_model=config.model
    )
    harness_returncode = None
    if evaluation:
        harness_returncode, _ = run_official_evaluation(
            config,
            selected_ids,
            predictions_path,
            subprocess_runner=subprocess_runner,
        )
    summary_path = None
    if stats:
        generate_statistics(config, selected_ids)
        summary_path = config.experiment_dir / "experiment_summary.json"
    return ExperimentRunResult(
        experiment_dir=config.experiment_dir,
        predictions_path=predictions_path,
        summary_path=summary_path,
        harness_returncode=harness_returncode,
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not Path(path).exists():
        return {}
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _number_or_none(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _first_number(*values: Any) -> int | float | None:
    for value in values:
        number = _number_or_none(value)
        if number is not None:
            return number
    return None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _event_count_or_none(
    events: Sequence[Mapping[str, Any]], event_name: str
) -> int | None:
    if not events:
        return None
    return sum(event.get("event") == event_name for event in events)


def _tool_count_or_none(
    tools: Sequence[str], name: str, trace: Sequence[Mapping[str, Any]]
) -> int | None:
    if not trace:
        return None
    return sum(tool == name for tool in tools)


def _sum_event_field_or_none(
    events: Sequence[Mapping[str, Any]], field: str
) -> int | float | None:
    values = [_number_or_none(event.get(field)) for event in events if field in event]
    return _sum([float(value) for value in values if value is not None])


def _sum(values: Sequence[float]) -> int | float | None:
    if not values:
        return None
    result = sum(values)
    return int(result) if float(result).is_integer() else result


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _git_identity() -> tuple[str | None, bool | None]:
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=True,
            ).stdout.strip()
        )
        return commit or None, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def _resolved_executable(executable: str) -> str:
    expanded = str(Path(executable).expanduser())
    resolved = shutil.which(expanded)
    return resolved or expanded


def _swebench_version(executable: str) -> str | None:
    try:
        completed = subprocess.run(
            [
                _resolved_executable(executable),
                "-c",
                "import importlib.metadata as m; print(m.version('swebench'))",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _system_info() -> dict[str, Any]:
    ram_total = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        match = re.search(r"^MemTotal:\s+(\d+)\s+kB", meminfo.read_text(), re.MULTILINE)
        if match:
            ram_total = int(match.group(1)) * 1024
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "ram_total_bytes": ram_total,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a reproducible PatchFox SWE-bench batch experiment."
    )
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--num-instances", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--split", default=None)
    parser.add_argument("--artifact-root", default=str(DEFAULT_EXPERIMENT_ROOT))
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--execution-mode", choices=EXECUTION_MODES, default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--max-new-tokens", type=int, default=None)
    parser.add_argument("--approval", choices=APPROVAL_POLICIES, default=None)
    parser.add_argument("--sandbox", default=None)
    parser.add_argument("--sandbox-backend", default=None)
    parser.add_argument("--generation-workers", type=int, default=None)
    parser.add_argument("--eval-workers", type=int, default=None)
    parser.add_argument("--swebench-python", default=None)
    parser.add_argument("--exclude-instance", action="append", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--force-new-selection", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--generation-only", action="store_true")
    modes.add_argument("--evaluation-only", action="store_true")
    modes.add_argument("--stats-only", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> ExperimentConfig:
    artifact_root = Path(args.artifact_root)
    existing_path = (
        artifact_root.resolve() / args.experiment_id / "experiment_config.json"
    )
    existing = _read_json_if_exists(existing_path)

    def choose(name: str, default: Any) -> Any:
        value = getattr(args, name)
        if value is not None:
            return value
        return existing.get(name, default)

    explicit_excluded = args.exclude_instance
    if explicit_excluded is None and existing:
        excluded = tuple(existing.get("excluded_instances") or ())
    else:
        excluded = tuple(
            sorted(set(DEFAULT_EXCLUDED_INSTANCES) | set(explicit_excluded or ()))
        )
    num_instances = choose("num_instances", None)
    if num_instances is None:
        raise ValueError("--num-instances is required for a new experiment")
    return ExperimentConfig(
        experiment_id=args.experiment_id,
        num_instances=int(num_instances),
        seed=int(choose("seed", 42)),
        dataset=str(choose("dataset", DEFAULT_DATASET)),
        split=str(choose("split", DEFAULT_SPLIT)),
        excluded_instances=excluded,
        artifact_root=artifact_root,
        workspace_root=Path(choose("workspace_root", "/tmp/patchfox-swebench")),
        execution_mode=str(choose("execution_mode", "swebench-image")),
        provider=choose("provider", None),
        model=choose("model", None),
        config_path=(
            Path(args.config_path)
            if args.config_path
            else (
                Path(existing["config_path"]) if existing.get("config_path") else None
            )
        ),
        base_url=args.base_url
        if args.base_url is not None
        else existing.get("base_url"),
        max_steps=int(choose("max_steps", 60)),
        max_new_tokens=choose("max_new_tokens", 8192),
        approval=str(choose("approval", "auto")),
        sandbox=str(choose("sandbox", "required")),
        sandbox_backend=str(choose("sandbox_backend", "auto")),
        generation_workers=int(choose("generation_workers", 2)),
        eval_workers=int(choose("eval_workers", 4)),
        swebench_python=str(
            args.swebench_python
            or existing.get("swebench_python_executable")
            or sys.executable
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        config = _config_from_args(args)
        generation = not (args.evaluation_only or args.stats_only)
        evaluation = not (args.generation_only or args.stats_only)
        stats = not (args.generation_only or args.evaluation_only)
        result = run_experiment(
            config,
            generation=generation,
            evaluation=evaluation,
            stats=stats,
            resume=args.resume,
            rerun_failed=args.rerun_failed,
            force_new_selection=args.force_new_selection,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"experiment: {result.experiment_dir}")
    print(f"predictions: {result.predictions_path}")
    if result.summary_path:
        print(f"summary: {result.summary_path}")
    return 0 if result.harness_returncode in (None, 0) else result.harness_returncode


__all__ = [
    "CSV_FIELDS",
    "DEFAULT_EXCLUDED_INSTANCES",
    "DatasetCatalog",
    "ExperimentConfig",
    "ExperimentRunResult",
    "build_predictions",
    "deterministic_instance_order",
    "discover_official_results",
    "generate_statistics",
    "main",
    "prepare_selection",
    "run_experiment",
    "run_generation",
    "run_official_evaluation",
]
