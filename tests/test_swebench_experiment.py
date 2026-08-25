import json
import subprocess
import threading
from pathlib import Path

import pytest

from patchfox.evaluation.swebench_experiment import (
    DEFAULT_EXCLUDED_INSTANCES,
    DatasetCatalog,
    ExperimentConfig,
    build_predictions,
    deterministic_instance_order,
    discover_official_results,
    generate_statistics,
    prepare_experiment_config,
    prepare_selection,
    run_experiment,
    run_generation,
)
from patchfox.evaluation.swebench_runner import SWEbenchRunResult


def _catalog(size=60):
    rows = [
        {
            "instance_id": f"owner__repo-{index:03d}",
            "repo": "owner/repo",
            "base_commit": f"base-{index}",
            "problem_statement": f"Fix issue {index}",
            "image": f"image:{index}",
            "patch": "PRIVATE GOLD",
            "test_patch": "PRIVATE TEST",
        }
        for index in range(size)
    ]
    rows.append(
        {
            "instance_id": DEFAULT_EXCLUDED_INSTANCES[0],
            "repo": "sympy/sympy",
            "base_commit": "debug-base",
            "problem_statement": "debug",
            "image": "debug-image",
        }
    )
    return DatasetCatalog(tuple(rows), revision="dataset-sha")


def _config(tmp_path, *, num_instances=3, **overrides):
    values = {
        "experiment_id": "exp-seed42",
        "num_instances": num_instances,
        "artifact_root": tmp_path / "artifacts",
        "workspace_root": tmp_path / "workspaces",
        "provider": "fake-provider",
        "model": "fake-model",
        "generation_workers": 1,
        "swebench_python": "fake-swebench-python",
    }
    values.update(overrides)
    return ExperimentConfig(**values)


class FakeRunner:
    def __init__(self, *, failures=(), empty=(), interrupt_on=None, detailed=False):
        self.failures = set(failures)
        self.empty = set(empty)
        self.interrupt_on = interrupt_on
        self.detailed = detailed
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, config, *, dataset_loader):
        with self._lock:
            self.calls.append(config.instance_id)
        rows = list(dataset_loader(config.dataset, config.split))
        row = next(item for item in rows if item["instance_id"] == config.instance_id)
        if config.instance_id == self.interrupt_on:
            raise KeyboardInterrupt("simulated disconnect")
        if config.instance_id in self.failures:
            raise RuntimeError("simulated generation failure")

        artifact_dir = Path(config.artifact_root) / config.run_id / config.instance_id
        workspace = (
            Path(config.workspace_root) / config.run_id / config.instance_id / "repo"
        )
        evidence_dir = workspace / ".patchfox" / "runs" / "run-1"
        evidence_dir.mkdir(parents=True)
        patch = "" if config.instance_id in self.empty else "diff --git a/a b/a\n"
        task_state = {
            "status": "completed",
            "stop_reason": "final_answer_returned",
            "changed_paths": [] if not patch else ["a.py"],
            "final_answer": "Done",
        }
        if self.detailed:
            task_state.update(
                {
                    "first_change_step": 3,
                    "verification_after_change": True,
                    "max_consecutive_explore_steps": 2,
                    "repeated_source_read_count": 1,
                    "overlapping_read_count": 1,
                }
            )
        report = {
            "status": "completed",
            "stop_reason": "final_answer_returned",
            "final_answer": "Done",
            "tool_steps": 4,
            "task_state": task_state,
        }
        events = [
            {"event": "model_parsed"},
            {"event": "tool_executed", "name": "read_file"},
            {"event": "tool_executed", "name": "search"},
            {"event": "tool_executed", "name": "patch_file"},
            {"event": "tool_executed", "name": "run_shell"},
        ]
        if self.detailed:
            events.append(
                {
                    "event": "memory.retrieval",
                    "duplicate_source_filtered_count": 2,
                    "recent_source_filtered_count": 1,
                }
            )
        (evidence_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (evidence_dir / "task_state.json").write_text(
            json.dumps(task_state), encoding="utf-8"
        )
        (evidence_dir / "trace.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        prediction = {
            "instance_id": config.instance_id,
            "model_name_or_path": config.model,
            "model_patch": patch,
        }
        metadata = {
            "status": "completed",
            "instance_id": config.instance_id,
            "instance": {
                "repo": row["repo"],
                "base_commit": row["base_commit"],
            },
            "image_digest": "sha256:image",
            "model_patch_bytes": len(patch.encode()),
            "patchfox_wall_time_seconds": 10,
            "adapter_wall_time_seconds": 12,
            "error": None,
            "evidence": {
                "stop_reason": "final_answer_returned",
                "changed_paths": task_state["changed_paths"],
                "tool_steps": 4,
                "usage": {
                    "model_call_count": 1,
                    "input_tokens": 100,
                    "cached_tokens": 10,
                    "output_tokens": 20,
                },
            },
        }
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "prediction.json").write_text(
            json.dumps(prediction), encoding="utf-8"
        )
        (artifact_dir / "metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        (artifact_dir / "patch.diff").write_text(patch, encoding="utf-8")
        (artifact_dir / "stdout.log").write_text("fake stdout", encoding="utf-8")
        (artifact_dir / "stderr.log").write_text("", encoding="utf-8")
        return SWEbenchRunResult(
            returncode=0,
            prediction=prediction,
            metadata=metadata,
            artifact_dir=artifact_dir,
            predictions_path=artifact_dir.parent / "predictions.jsonl",
            workspace=workspace,
        )


def _loader(catalog):
    return lambda _dataset, _split: catalog


def test_deterministic_full_shuffle_makes_ten_a_prefix_of_fifty():
    ids = [f"id-{index:03d}" for index in range(100)]
    first = deterministic_instance_order(ids, seed=42, excluded_instances={"id-005"})
    second = deterministic_instance_order(
        reversed(ids), seed=42, excluded_instances={"id-005"}
    )

    assert first == second
    assert first[:10] == first[:50][:10]
    assert "id-005" not in first


def test_selection_is_reused_for_expansion_and_rejects_conflicts(tmp_path):
    catalog = _catalog()
    ten = _config(tmp_path, num_instances=10)
    selection_10, _ = prepare_selection(ten, catalog=catalog)
    selection_50, _ = prepare_selection(_config(tmp_path, num_instances=50))

    assert (
        selection_10["selected_instance_ids"]
        == selection_50["selected_instance_ids"][:10]
    )
    assert DEFAULT_EXCLUDED_INSTANCES[0] not in selection_50["shuffled_instance_order"]
    assert selection_50["dataset_revision"] == "dataset-sha"
    with pytest.raises(ValueError, match="seed"):
        prepare_selection(_config(tmp_path, num_instances=50, seed=7))
    with pytest.raises(ValueError, match="excluded_instances"):
        prepare_selection(
            _config(
                tmp_path,
                num_instances=50,
                excluded_instances=(*DEFAULT_EXCLUDED_INSTANCES, "owner__repo-001"),
            )
        )


def test_resume_skips_every_completed_valid_artifact(tmp_path):
    catalog = _catalog(3)
    config = _config(tmp_path)
    selection, _ = prepare_selection(config, catalog=catalog)
    ids = selection["selected_instance_ids"]
    first = FakeRunner()
    run_generation(
        config, ids, catalog=catalog, resume=False, rerun_failed=False, runner=first
    )
    progress_path = config.experiment_dir / "progress.json"
    progress = json.loads(progress_path.read_text())
    progress["instances"][ids[0]]["status"] = "running"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    second = FakeRunner()
    run_generation(
        config, ids, catalog=catalog, resume=True, rerun_failed=False, runner=second
    )

    assert second.calls == []
    persisted = json.loads(progress_path.read_text())
    assert all(persisted["instances"][item]["status"] == "completed" for item in ids)


def test_failure_does_not_block_and_rerun_failed_runs_only_failure(tmp_path):
    catalog = _catalog(3)
    config = _config(tmp_path)
    selection, _ = prepare_selection(config, catalog=catalog)
    ids = selection["selected_instance_ids"]
    first = FakeRunner(failures={ids[1]})
    progress = run_generation(
        config, ids, catalog=catalog, resume=False, rerun_failed=False, runner=first
    )

    assert first.calls == ids
    assert progress["instances"][ids[0]]["status"] == "completed"
    assert progress["instances"][ids[1]]["status"] == "failed"
    assert progress["instances"][ids[2]]["status"] == "completed"

    retry = FakeRunner()
    run_generation(
        config, ids, catalog=catalog, resume=True, rerun_failed=True, runner=retry
    )
    assert retry.calls == [ids[1]]


def test_empty_and_failed_predictions_are_in_stable_selected_order(tmp_path):
    catalog = _catalog(3)
    config = _config(tmp_path)
    selection, _ = prepare_selection(config, catalog=catalog)
    ids = selection["selected_instance_ids"]
    runner = FakeRunner(failures={ids[1]}, empty={ids[2]})
    run_generation(
        config, ids, catalog=catalog, resume=False, rerun_failed=False, runner=runner
    )
    predictions = build_predictions(
        config.experiment_dir, ids, default_model=config.model
    )
    rows = [json.loads(line) for line in predictions.read_text().splitlines()]

    assert [row["instance_id"] for row in rows] == ids
    assert rows[1]["model_patch"] == ""
    assert rows[2]["model_patch"] == ""


def test_generation_only_never_invokes_harness_and_writes_no_secret(
    tmp_path, monkeypatch
):
    secret = "should-not-appear-in-artifacts"
    monkeypatch.setenv("PATCHFOX_API_KEY", secret)
    config = _config(tmp_path, num_instances=2)

    def forbidden_harness(*_args, **_kwargs):
        raise AssertionError("harness was called")

    result = run_experiment(
        config,
        generation=True,
        evaluation=False,
        stats=False,
        catalog_loader=_loader(_catalog(2)),
        runner=FakeRunner(),
        subprocess_runner=forbidden_harness,
    )

    assert result.harness_returncode is None
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in result.experiment_dir.rglob("*")
        if path.is_file()
    )
    assert secret not in artifact_text


def test_evaluation_only_never_runs_generation_and_uses_official_report(tmp_path):
    config = _config(tmp_path, num_instances=2)
    initial = run_experiment(
        config,
        generation=True,
        evaluation=False,
        stats=False,
        catalog_loader=_loader(_catalog(2)),
        runner=FakeRunner(),
    )
    ids = [
        json.loads(line)["instance_id"]
        for line in initial.predictions_path.read_text().splitlines()
    ]

    def fake_harness(command, **kwargs):
        report = {
            "completed_ids": ids,
            "resolved_ids": [ids[0]],
            "unresolved_ids": [ids[1]],
            "empty_patch_ids": [],
            "error_ids": [],
            "incomplete_ids": [],
        }
        (Path(kwargs["cwd"]) / f"fake-model.{config.experiment_id}.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "official stdout", "")

    result = run_experiment(
        config,
        generation=False,
        evaluation=True,
        stats=False,
        resume=True,
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generation was called")
        ),
        subprocess_runner=fake_harness,
    )
    official = json.loads(
        (result.experiment_dir / "evaluation" / "official_results.json").read_text()
    )

    assert official["per_instance"][ids[0]]["resolved"] is True
    assert official["per_instance"][ids[1]]["resolved"] is False
    assert official["source_files"]


def test_stats_only_runs_neither_generation_nor_harness(tmp_path):
    config = _config(tmp_path, num_instances=1)
    run_experiment(
        config,
        generation=True,
        evaluation=False,
        stats=False,
        catalog_loader=_loader(_catalog(1)),
        runner=FakeRunner(detailed=True),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("external stage was called")

    result = run_experiment(
        config,
        generation=False,
        evaluation=False,
        stats=True,
        resume=True,
        runner=forbidden,
        subprocess_runner=forbidden,
    )

    assert result.summary_path and result.summary_path.exists()


def test_stats_only_without_existing_selection_does_not_load_dataset(tmp_path):
    config = _config(tmp_path, num_instances=1)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("external stage was called")

    with pytest.raises(FileNotFoundError, match="selection_order"):
        run_experiment(
            config,
            generation=False,
            evaluation=False,
            stats=True,
            catalog_loader=forbidden,
            runner=forbidden,
            subprocess_runner=forbidden,
        )


def test_metrics_aggregate_per_instance_evidence_and_missing_values(tmp_path):
    config = _config(tmp_path, num_instances=2)
    run_experiment(
        config,
        generation=True,
        evaluation=False,
        stats=False,
        catalog_loader=_loader(_catalog(2)),
        runner=FakeRunner(detailed=True),
    )
    selection = json.loads((config.experiment_dir / "selection_order.json").read_text())
    ids = selection["selected_instance_ids"]
    second_evidence = config.experiment_dir / "generation" / ids[1] / "evidence"
    (second_evidence / "trace.jsonl").unlink()
    task = json.loads((second_evidence / "task_state.json").read_text())
    for field in (
        "first_change_step",
        "verification_after_change",
        "max_consecutive_explore_steps",
        "repeated_source_read_count",
        "overlapping_read_count",
    ):
        task.pop(field)
    task["changed_paths"] = []
    (second_evidence / "task_state.json").write_text(json.dumps(task), encoding="utf-8")
    report = json.loads((second_evidence / "report.json").read_text())
    report["task_state"] = task
    (second_evidence / "report.json").write_text(json.dumps(report), encoding="utf-8")

    summary, rows = generate_statistics(config, ids)

    assert rows[0]["first_change_step"] == 3
    assert rows[1]["first_change_step"] is None
    assert rows[1]["read_file_calls"] is None
    assert summary["tokens"]["total_input_tokens"] == 200
    assert summary["agent_behavior"]["mean_first_change_step"] == 3
    assert summary["agent_behavior"]["median_first_change_step"] == 3
    assert (
        summary["p1_memory_convergence"]["total_duplicate_source_filtered_count"] == 2
    )


def test_all_missing_metrics_stay_unavailable(tmp_path):
    config = _config(tmp_path, num_instances=1)
    selection, _ = prepare_selection(config, catalog=_catalog(1))
    ids = selection["selected_instance_ids"]
    artifact = config.experiment_dir / "generation" / ids[0]
    artifact.mkdir(parents=True)
    (artifact / "metadata.json").write_text(
        json.dumps({"status": "error", "error": {"type": "X", "message": "bad"}}),
        encoding="utf-8",
    )
    (artifact / "prediction.json").write_text(
        json.dumps(
            {
                "instance_id": ids[0],
                "model_name_or_path": "fake",
                "model_patch": "",
            }
        ),
        encoding="utf-8",
    )

    summary, rows = generate_statistics(config, ids)

    assert rows[0]["input_tokens"] is None
    assert summary["tokens"]["mean_input_tokens"] is None
    assert summary["tokens"]["median_input_tokens"] is None
    assert summary["p1_memory_convergence"]["total_repeated_source_read_count"] is None


def test_official_stdout_is_only_a_fallback(tmp_path):
    instance_id = "owner__repo-001"
    result = discover_official_results(
        tmp_path,
        [instance_id],
        run_id="run",
        stdout=f"Result for {instance_id}: resolved: True\n",
    )
    assert result["per_instance"][instance_id] == {
        "official_status": "resolved",
        "resolved": True,
    }


def test_config_conflict_rejects_resume(tmp_path):
    config = _config(tmp_path, num_instances=1)
    selection, _ = prepare_selection(config, catalog=_catalog(1))
    prepare_experiment_config(config, selection)

    with pytest.raises(ValueError, match="model"):
        prepare_experiment_config(
            _config(tmp_path, num_instances=1, model="different-model"), selection
        )


def test_interrupt_keeps_completed_progress_on_disk(tmp_path):
    catalog = _catalog(3)
    config = _config(tmp_path)
    selection, _ = prepare_selection(config, catalog=catalog)
    ids = selection["selected_instance_ids"]
    runner = FakeRunner(interrupt_on=ids[1])

    with pytest.raises(KeyboardInterrupt):
        run_generation(
            config,
            ids,
            catalog=catalog,
            resume=False,
            rerun_failed=False,
            runner=runner,
        )

    progress = json.loads((config.experiment_dir / "progress.json").read_text())
    assert progress["instances"][ids[0]]["status"] == "completed"
    assert progress["instances"][ids[1]]["status"] == "running"
