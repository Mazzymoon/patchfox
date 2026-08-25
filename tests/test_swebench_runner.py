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
    load_swebench_record,
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
    assert not hasattr(instance, "image")
    assert not hasattr(instance, "eval_script")
    assert not hasattr(instance, "hints_text")

    record = load_swebench_record(
        "SWE-bench/SWE-bench_Verified",
        "test",
        INSTANCE_ID,
        dataset_loader=lambda dataset, split: [row],
    )
    assert record.image == "swebench/PRIVATE_IMAGE_SECRET:latest"
    assert not hasattr(record.instance, "image")


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
    assert args.execution_mode == "host"
    help_text = parser.format_help()
    assert "risky tools without human confirmation" in help_text
    assert "fails closed" in help_text


def test_swebench_image_mode_uses_ephemeral_container_and_off_inner_sandbox(
    tmp_path, monkeypatch
):
    docker = FakeDocker()
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(_config(tmp_path), execution_mode="swebench-image"),
        dataset_loader=lambda dataset, split: [_dataset_row()],
        docker_client=docker,
    )

    assert result.returncode == 0
    assert "diff --git a/brand_new.py b/brand_new.py" in result.prediction[
        "model_patch"
    ]
    assert ".patchfox" not in result.prediction["model_patch"]
    assert docker.removed is True
    assert docker.prompt == "Fix the public issue."
    assert "PRIVATE_IMAGE_SECRET" not in docker.prompt
    assert "GOLD_PATCH_SECRET" not in docker.prompt
    assert "TEST_PATCH_SECRET" not in docker.prompt
    assert "EVAL_SCRIPT_SECRET" not in docker.prompt
    assert "HINT_SECRET" not in docker.prompt

    create = docker.command_starting_with("create")
    forbidden = {"--privileged", "--cap-add", "--mount", "--volume", "-v"}
    assert forbidden.isdisjoint(create)
    assert all("docker.sock" not in item for item in create)

    agent_command = docker.patchfox_command()
    assert _option_value(agent_command, "--approval") == "auto"
    assert _option_value(agent_command, "--sandbox") == "off"
    assert _option_value(agent_command, "--sandbox-backend") == "none"
    assert "required" not in agent_command
    assert "bubblewrap" not in agent_command
    assert "TOP_SECRET_API_KEY" not in " ".join(agent_command)


def test_swebench_image_mode_separates_runtime_and_testbed_python(
    tmp_path, monkeypatch
):
    docker = FakeDocker()
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(_config(tmp_path), execution_mode="swebench-image"),
        dataset_loader=lambda dataset, split: [_dataset_row()],
        docker_client=docker,
    )

    runtime_install = docker.command_containing("--target")
    assert "/opt/miniconda3/bin/python" in runtime_install
    assert "/opt/patchfox-runtime" in runtime_install
    editable_install = docker.command_ending_with(
        ["python", "-m", "pip", "install", "-e", "."]
    )
    assert editable_install
    agent_call = docker.call_containing("patchfox")
    assert "PATH=/opt/miniconda3/envs/testbed/bin:" in " ".join(
        agent_call["args"]
    )
    assert result.metadata["python_executable"] == (
        "/opt/miniconda3/envs/testbed/bin/python"
    )
    assert result.metadata["python_version"] == "3.9.20"
    assert result.metadata["patchfox_runtime_python"] == (
        "/opt/miniconda3/bin/python"
    )
    assert result.metadata["patchfox_runtime_python_version"] == "3.11.9"


def test_swebench_image_metadata_records_real_outer_boundary(
    tmp_path, monkeypatch
):
    docker = FakeDocker()
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(
            _config(tmp_path),
            execution_mode="swebench-image",
            sandbox="required",
            sandbox_backend="bubblewrap",
        ),
        dataset_loader=lambda dataset, split: [_dataset_row()],
        docker_client=docker,
    )

    metadata = result.metadata
    assert metadata["execution_mode"] == "swebench-image"
    assert metadata["image"] == "swebench/PRIVATE_IMAGE_SECRET:latest"
    assert metadata["image_digest"] == (
        "swebench/PRIVATE_IMAGE_SECRET@sha256:abc123"
    )
    assert metadata["container_id"] == "container-123"
    assert metadata["initial_image_head"] == "abc123"
    assert metadata["approval"] == "auto"
    assert metadata["outer_sandbox"] == "docker"
    assert metadata["inner_sandbox"] == "off"
    assert metadata["sandbox"] == "off"
    assert metadata["sandbox_backend"] == "none"
    assert metadata["effective_path"].startswith(
        "/opt/miniconda3/envs/testbed/bin:"
    )
    assert metadata["patchfox_git_commit"]
    assert metadata["container_cleanup"] == "completed"
    assert metadata["evidence"]["manifest"]["patchfox_evidence_available"] is True


def test_swebench_image_allows_setup_commit_and_diffs_from_initial_head(
    tmp_path, monkeypatch
):
    setup_head = "5efaed257c694e4452b4c8361aae1cd9cdefd6d1"
    base_commit = "cffd4e0f86fefd4802349a9f9b19ed70934ea354"
    docker = FakeDocker(initial_image_head=setup_head)
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(_config(tmp_path), execution_mode="swebench-image"),
        dataset_loader=lambda dataset, split: [_dataset_row(base_commit)],
        docker_client=docker,
    )

    assert result.returncode == 0
    assert result.metadata["instance"]["base_commit"] == base_commit
    assert result.metadata["initial_image_head"] == setup_head
    assert result.metadata["base_commit_is_ancestor_of_initial_head"] is True
    diff_command = docker.command_containing("--no-ext-diff")
    assert setup_head in diff_command
    assert base_commit not in diff_command
    assert "SETUP_COMMIT_CHANGE" not in result.prediction["model_patch"]
    clean_checks = [
        call
        for call in docker.calls
        if call["args"][-3:] == ["git", "status", "--porcelain"]
    ]
    assert len(clean_checks) == 2


def test_swebench_image_rejects_dirty_initial_worktree(tmp_path, monkeypatch):
    docker = FakeDocker(initial_worktree_dirty=True)
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(_config(tmp_path), execution_mode="swebench-image"),
        dataset_loader=lambda dataset, split: [_dataset_row()],
        docker_client=docker,
    )

    assert result.returncode == 1
    assert result.prediction["model_patch"] == ""
    assert "initial image left /testbed dirty" in result.metadata["error"]["message"]
    assert docker.removed is True


def test_swebench_image_ancestor_diagnostic_does_not_fail_closed(
    tmp_path, monkeypatch
):
    docker = FakeDocker(ancestor_returncode=1)
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(_config(tmp_path), execution_mode="swebench-image"),
        dataset_loader=lambda dataset, split: [_dataset_row()],
        docker_client=docker,
    )

    assert result.returncode == 0
    assert result.metadata["base_commit_is_ancestor_of_initial_head"] is False


def test_swebench_image_agent_failure_cleans_container_and_writes_empty_patch(
    tmp_path, monkeypatch
):
    docker = FakeDocker(agent_returncode=9)
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(_config(tmp_path), execution_mode="swebench-image"),
        dataset_loader=lambda dataset, split: [_dataset_row()],
        docker_client=docker,
    )

    assert result.returncode == 1
    assert result.prediction["model_patch"] == ""
    assert docker.removed is True
    assert result.metadata["patchfox_returncode"] == 9
    assert result.metadata["container_cleanup"] == "completed"
    assert (result.artifact_dir / "patch.diff").read_text(encoding="utf-8") == ""


def test_swebench_image_setup_exception_still_cleans_container(
    tmp_path, monkeypatch
):
    docker = FakeDocker(fail_runtime_install=True)
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(_config(tmp_path), execution_mode="swebench-image"),
        dataset_loader=lambda dataset, split: [_dataset_row()],
        docker_client=docker,
    )

    assert result.returncode == 1
    assert result.prediction["model_patch"] == ""
    assert docker.removed is True
    assert result.metadata["container_cleanup"] == "completed"
    assert result.metadata["failure_phase"] == "image_generation"
    assert "runtime install failed" in result.metadata["error"]["message"]


def test_swebench_image_create_exception_attempts_named_cleanup(
    tmp_path, monkeypatch
):
    docker = FakeDocker(fail_create=True)
    _mock_provider(monkeypatch)

    result = run_swebench_instance(
        replace(_config(tmp_path), execution_mode="swebench-image"),
        dataset_loader=lambda dataset, split: [_dataset_row()],
        docker_client=docker,
    )

    assert result.returncode == 1
    assert result.prediction["model_patch"] == ""
    assert docker.removed is True
    assert result.metadata["container_cleanup"] == "completed"
    assert "docker create failed" in result.metadata["error"]["message"]


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
        "image": "swebench/PRIVATE_IMAGE_SECRET:latest",
        "patch": "GOLD_PATCH_SECRET",
        "test_patch": "TEST_PATCH_SECRET",
        "eval_script": "EVAL_SCRIPT_SECRET",
        "FAIL_TO_PASS": ["FAIL_TEST_SECRET"],
        "PASS_TO_PASS": ["PASS_TEST_SECRET"],
        "hints_text": "HINT_SECRET",
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


def _mock_provider(monkeypatch):
    provider = SimpleNamespace(
        name="openai",
        model="test-model",
        protocol="openai",
        base_url="https://provider.example.invalid/v1",
        api_key="TOP_SECRET_API_KEY",
    )
    monkeypatch.setattr(
        "patchfox.evaluation.swebench_runner._resolve_runtime_provider",
        lambda config: provider,
    )


class FakeDocker:
    def __init__(
        self,
        *,
        agent_returncode=0,
        fail_runtime_install=False,
        fail_create=False,
        initial_image_head="abc123",
        initial_worktree_dirty=False,
        ancestor_returncode=0,
    ):
        self.agent_returncode = agent_returncode
        self.fail_runtime_install = fail_runtime_install
        self.fail_create = fail_create
        self.initial_image_head = initial_image_head
        self.initial_worktree_dirty = initial_worktree_dirty
        self.ancestor_returncode = ancestor_returncode
        self.status_calls = 0
        self.calls = []
        self.prompt = ""
        self.removed = False

    def run(self, args, *, check=True, env=None):
        args = [str(item) for item in args]
        self.calls.append({"args": args, "env": dict(env or {})})
        returncode = 0
        stdout = ""
        stderr = ""

        if args[:2] == ["image", "inspect"]:
            stdout = json.dumps(
                [
                    {
                        "Id": "sha256:image-id",
                        "RepoDigests": [
                            "swebench/PRIVATE_IMAGE_SECRET@sha256:abc123"
                        ],
                    }
                ]
            )
        elif args and args[0] == "create":
            if self.fail_create:
                raise RuntimeError("docker create failed")
            stdout = "container-123\n"
        elif args and args[0] == "rm":
            self.removed = True
        elif args and args[0] == "cp":
            self._handle_cp(args)
        elif args and args[0] == "exec":
            command = self._exec_command(args)
            if command[-3:] == ["git", "rev-parse", "HEAD"]:
                stdout = f"{self.initial_image_head}\n"
            elif command[-3:] == ["git", "status", "--porcelain"]:
                self.status_calls += 1
                stdout = (
                    " M setup.py\n"
                    if self.initial_worktree_dirty and self.status_calls == 1
                    else ""
                )
            elif "merge-base" in command:
                returncode = self.ancestor_returncode
            elif "import json,platform,sys" in " ".join(command):
                if command[0] == "/opt/miniconda3/bin/python":
                    stdout = json.dumps(
                        {
                            "executable": "/opt/miniconda3/bin/python",
                            "version": "3.11.9",
                            "version_info": [3, 11, 9],
                        }
                    )
                else:
                    stdout = json.dumps(
                        {
                            "executable": "/opt/miniconda3/envs/testbed/bin/python",
                            "version": "3.9.20",
                            "version_info": [3, 9, 20],
                        }
                    )
            elif "--target" in command:
                if self.fail_runtime_install:
                    raise RuntimeError("runtime install failed")
            elif "patchfox" in command:
                returncode = self.agent_returncode
                stdout = "PatchFox finished\n" if returncode == 0 else ""
                stderr = "provider failed\n" if returncode else ""
            elif command[-5:] == [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ]:
                stdout = "brand_new.py\0.patchfox/runs/private.json\0"
            elif "diff" in command:
                stdout = (
                    "diff --git a/brand_new.py b/brand_new.py\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n"
                    "+++ b/brand_new.py\n"
                    "@@ -0,0 +1 @@\n"
                    "+VALUE = 42\n"
                )

        completed = SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if check and returncode != 0:
            raise RuntimeError(stderr or "fake docker command failed")
        return completed

    def _handle_cp(self, args):
        source, destination = args[1], args[2]
        if destination.endswith(":/opt/patchfox-input/problem_statement.txt"):
            self.prompt = Path(source).read_text(encoding="utf-8")
        if source.endswith(":/testbed/.patchfox"):
            session_id = "swebench-smoke-001-owner__repo-123"
            _write_evidence(Path(destination), session_id)

    @staticmethod
    def _exec_command(args):
        return args[args.index("container-123") + 1 :]

    def command_starting_with(self, value):
        return next(call["args"] for call in self.calls if call["args"][0] == value)

    def call_containing(self, value):
        return next(call for call in self.calls if value in call["args"])

    def command_containing(self, value):
        return self.call_containing(value)["args"]

    def command_ending_with(self, suffix):
        return next(
            call["args"]
            for call in self.calls
            if call["args"][-len(suffix) :] == suffix
        )

    def patchfox_command(self):
        args = self.call_containing("patchfox")["args"]
        return self._exec_command(args)


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
