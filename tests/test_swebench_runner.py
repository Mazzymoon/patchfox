import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from patchfox.evaluation.swebench_runner import (
    PatchFoxInvocation,
    SWEbenchRunConfig,
    build_arg_parser,
    collect_model_patch,
    invoke_patchfox_cli,
    load_swebench_instance,
    prepare_git_workspace,
    run_swebench_instance,
)

INSTANCE_ID = "owner__repo-123"


def test_instance_metadata_reader_keeps_only_public_issue_fields():
    row = _dataset_row()

    instance = load_swebench_instance(
        "SWE-bench/SWE-bench_Verified",
        "test",
        INSTANCE_ID,
        dataset_loader=lambda dataset, split: [row],
    )

    assert instance.instance_id == INSTANCE_ID
    assert instance.repo == "owner/repo"
    assert instance.base_commit == "abc123"
    assert instance.problem_statement == "Fix the public issue."
    assert not hasattr(instance, "patch")
    assert not hasattr(instance, "test_patch")
    assert not hasattr(instance, "FAIL_TO_PASS")
    assert not hasattr(instance, "PASS_TO_PASS")


def test_prepare_workspace_checks_out_exact_clean_base_commit(tmp_path):
    origin, base_commit = _make_origin(tmp_path)
    _commit_file(origin, "later.txt", "later\n", "later")
    instance = _instance(base_commit)
    destination = tmp_path / "work" / "repo"

    prepare_git_workspace(
        instance, destination, clone_url_resolver=lambda repo: str(origin)
    )

    assert _git(destination, "rev-parse", "HEAD").stdout.strip() == base_commit
    assert not (destination / "later.txt").exists()
    assert _git(destination, "status", "--porcelain").stdout == ""


def test_run_generates_official_prediction_without_leaking_gold_fields(tmp_path):
    origin, base_commit = _make_origin(tmp_path)
    seen = {}

    def invoke(workspace, prompt, session_id, config):
        seen["prompt"] = prompt
        (workspace / "brand_new.py").write_text("VALUE = 42\n", encoding="utf-8")
        _write_evidence(workspace, session_id)
        return PatchFoxInvocation(
            returncode=0,
            stdout="PatchFox finished\n",
            stderr="",
            wall_time_seconds=1.25,
        )

    result = run_swebench_instance(
        _config(tmp_path),
        dataset_loader=lambda dataset, split: [_dataset_row(base_commit)],
        patchfox_invoker=invoke,
        clone_url_resolver=lambda repo: str(origin),
    )

    assert seen["prompt"] == "Fix the public issue."
    assert "GOLD_PATCH_SECRET" not in seen["prompt"]
    assert "TEST_PATCH_SECRET" not in seen["prompt"]
    assert "FAIL_TEST_SECRET" not in seen["prompt"]
    assert "PASS_TEST_SECRET" not in seen["prompt"]
    assert set(result.prediction) == {
        "instance_id",
        "model_name_or_path",
        "model_patch",
    }
    assert result.prediction["instance_id"] == INSTANCE_ID
    assert result.prediction["model_name_or_path"] == "test-model"
    patch = result.prediction["model_patch"]
    assert "diff --git a/brand_new.py b/brand_new.py" in patch
    assert "new file mode" in patch
    assert "+VALUE = 42" in patch
    assert ".patchfox/" not in patch

    prediction_path = result.artifact_dir / "prediction.json"
    assert json.loads(prediction_path.read_text(encoding="utf-8")) == result.prediction
    lines = result.predictions_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [result.prediction]
    assert (result.artifact_dir / "patch.diff").read_text(encoding="utf-8") == patch
    assert (result.artifact_dir / "stdout.log").read_text(
        encoding="utf-8"
    ) == "PatchFox finished\n"
    assert result.metadata["approval"] == "auto"
    assert result.metadata["sandbox"] == "required"
    assert result.metadata["sandbox_backend"] == "auto"


def test_default_patchfox_cli_uses_unattended_fail_closed_controls(
    tmp_path, monkeypatch
):
    command = _capture_patchfox_command(tmp_path, monkeypatch, _config(tmp_path))

    assert _option_value(command, "--approval") == "auto"
    assert _option_value(command, "--sandbox") == "required"
    assert _option_value(command, "--sandbox-backend") == "auto"
    assert "--non-interactive" in command
    assert "never" not in command


def test_explicit_runtime_controls_are_passed_to_patchfox(tmp_path, monkeypatch):
    config = replace(
        _config(tmp_path),
        approval="never",
        sandbox="best_effort",
        sandbox_backend="bubblewrap",
    )

    command = _capture_patchfox_command(tmp_path, monkeypatch, config)

    assert _option_value(command, "--approval") == "never"
    assert _option_value(command, "--sandbox") == "best_effort"
    assert _option_value(command, "--sandbox-backend") == "bubblewrap"


def test_runner_cli_exposes_runtime_controls_with_safe_defaults():
    parser = build_arg_parser()
    args = parser.parse_args(
        ["--instance-id", INSTANCE_ID, "--run-id", "smoke-001"]
    )

    assert args.approval == "auto"
    assert args.sandbox == "required"
    assert args.sandbox_backend == "auto"
    help_text = parser.format_help()
    assert "risky tools without human confirmation" in help_text
    assert "fails closed" in help_text


def test_metadata_records_explicit_runtime_controls(tmp_path):
    config = replace(
        _config(tmp_path),
        approval="never",
        sandbox="off",
        sandbox_backend="none",
    )

    result = run_swebench_instance(
        config,
        dataset_loader=lambda dataset, split: [],
    )

    assert result.metadata["approval"] == "never"
    assert result.metadata["sandbox"] == "off"
    assert result.metadata["sandbox_backend"] == "none"


def test_new_untracked_file_is_included_but_patchfox_state_is_excluded(tmp_path):
    origin, base_commit = _make_origin(tmp_path)
    workspace = prepare_git_workspace(
        _instance(base_commit),
        tmp_path / "checkout",
        clone_url_resolver=lambda repo: str(origin),
    )
    (workspace / "new.txt").write_text("new content\n", encoding="utf-8")
    state = workspace / ".patchfox" / "runs" / "run-1"
    state.mkdir(parents=True)
    (state / "trace.jsonl").write_text("secret trace\n", encoding="utf-8")

    patch = collect_model_patch(workspace)

    assert "diff --git a/new.txt b/new.txt" in patch
    assert "+new content" in patch
    assert ".patchfox" not in patch


def test_patchfox_failure_writes_empty_patch_and_error_metadata(tmp_path):
    origin, base_commit = _make_origin(tmp_path)

    def failing_invoke(workspace, prompt, session_id, config):
        (workspace / "partial.py").write_text("partial = True\n", encoding="utf-8")
        _write_evidence(workspace, session_id, status="failed")
        return PatchFoxInvocation(
            returncode=9,
            stdout="partial output\n",
            stderr="provider failed\n",
            wall_time_seconds=0.5,
        )

    result = run_swebench_instance(
        _config(tmp_path),
        dataset_loader=lambda dataset, split: [_dataset_row(base_commit)],
        patchfox_invoker=failing_invoke,
        clone_url_resolver=lambda repo: str(origin),
    )

    assert result.returncode == 1
    assert result.prediction["model_patch"] == ""
    assert (result.artifact_dir / "patch.diff").read_text(encoding="utf-8") == ""
    metadata = json.loads(
        (result.artifact_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["status"] == "error"
    assert metadata["failure_phase"] == "patchfox"
    assert metadata["patchfox_returncode"] == 9
    assert metadata["error"]["type"] == "RuntimeError"
    assert "return code 9" in metadata["error"]["message"]


def test_run_evidence_is_associated_with_the_prediction(tmp_path):
    origin, base_commit = _make_origin(tmp_path)

    def invoke(workspace, prompt, session_id, config):
        _write_evidence(workspace, session_id)
        return PatchFoxInvocation(returncode=0)

    result = run_swebench_instance(
        _config(tmp_path),
        dataset_loader=lambda dataset, split: [_dataset_row(base_commit)],
        patchfox_invoker=invoke,
        clone_url_resolver=lambda repo: str(origin),
    )

    evidence = result.metadata["evidence"]
    manifest = evidence["manifest"]
    assert manifest["patchfox_evidence_available"] is True
    assert Path(manifest["patchfox_report_path"]).is_file()
    assert Path(manifest["patchfox_trace_path"]).is_file()
    assert Path(manifest["patchfox_task_state_path"]).is_file()
    assert Path(manifest["patchfox_session_path"]).name.startswith("swebench-")
    assert evidence["status"] == "completed"
    assert evidence["tool_names"] == ["write_file"]
    assert evidence["usage"] == {
        "input_tokens": 100,
        "cached_tokens": 10,
        "output_tokens": 20,
        "usage_source": "actual",
        "model_call_count": 1,
    }
    assert evidence["tool_steps"] == 1


def _config(tmp_path):
    return SWEbenchRunConfig(
        instance_id=INSTANCE_ID,
        run_id="smoke-001",
        workspace_root=tmp_path / "workspaces",
        artifact_root=tmp_path / "artifacts" / "swebench",
        provider="test-provider",
        model="test-model",
        max_steps=30,
    )


def _dataset_row(base_commit="abc123"):
    return {
        "instance_id": INSTANCE_ID,
        "repo": "owner/repo",
        "base_commit": base_commit,
        "problem_statement": "Fix the public issue.",
        "patch": "GOLD_PATCH_SECRET",
        "test_patch": "TEST_PATCH_SECRET",
        "FAIL_TO_PASS": ["FAIL_TEST_SECRET"],
        "PASS_TO_PASS": ["PASS_TEST_SECRET"],
    }


def _capture_patchfox_command(tmp_path, monkeypatch, config):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        prompt_path = Path(_option_value(command, "--prompt-file"))
        assert prompt_path.read_text(encoding="utf-8") == "Fix the public issue."
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "patchfox.evaluation.swebench_runner.subprocess.run", fake_run
    )
    invocation = invoke_patchfox_cli(
        workspace,
        "Fix the public issue.",
        "swebench-smoke-001-owner__repo-123",
        config,
    )

    assert invocation.returncode == 0
    return captured["command"]


def _option_value(command, option):
    return command[command.index(option) + 1]


def _instance(base_commit):
    return load_swebench_instance(
        "dataset",
        "test",
        INSTANCE_ID,
        dataset_loader=lambda dataset, split: [_dataset_row(base_commit)],
    )


def _make_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init")
    _git(origin, "config", "user.email", "patchfox-tests@example.invalid")
    _git(origin, "config", "user.name", "PatchFox Tests")
    base_commit = _commit_file(origin, "base.txt", "base\n", "base")
    return origin, base_commit


def _commit_file(repo, relative, content, message):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relative)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_evidence(workspace, session_id, status="completed"):
    run_dir = workspace / ".patchfox" / "runs" / "run-1"
    sessions = workspace / ".patchfox" / "sessions"
    run_dir.mkdir(parents=True)
    sessions.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "status": status,
                "stop_reason": "final_answer_returned"
                if status == "completed"
                else "model_error",
                "tool_steps": 1,
                "attempts": 1,
                "artifact_graph": {"changed_paths": ["brand_new.py"]},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "task_state.json").write_text(
        json.dumps({"status": status, "changed_paths": ["brand_new.py"]}),
        encoding="utf-8",
    )
    events = [
        {
            "event": "prompt_built",
            "prompt_metadata": {"context_usage": {"total_estimated_tokens": 95}},
        },
        {
            "event": "model_parsed",
            "completion_metadata": {
                "provider_protocol": "openai",
                "provider_model": "test-model",
                "input_tokens": 100,
                "cached_tokens": 10,
                "output_tokens": 20,
            },
        },
        {"event": "tool_executed", "name": "write_file", "duration_ms": 5},
        {"event": "run_finished", "run_duration_ms": 1000},
    ]
    (run_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    (sessions / f"{session_id}.json").write_text(
        json.dumps(
            {
                "history": [
                    {"role": "user", "content": "Fix the public issue."},
                    {"role": "assistant", "content": "Done."},
                ]
            }
        ),
        encoding="utf-8",
    )
    (sessions / f"{session_id}.events.jsonl").write_text(
        json.dumps({"event": "session_saved"}) + "\n", encoding="utf-8"
    )


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
