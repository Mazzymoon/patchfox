import json

from patchfox import PatchFox, SessionStore, WorkspaceContext
from patchfox.core.runtime_progress import RuntimeProgressConfig
from patchfox.core.task_state import TaskState
from patchfox.testing import ScriptedModelClient


def build_agent(tmp_path, *, outputs=None, max_steps=50, config=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return PatchFox(
        model_client=ScriptedModelClient(outputs or []),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".patchfox" / "sessions"),
        approval_policy="auto",
        max_steps=max_steps,
        runtime_progress_config=config,
    )


def start_progress(agent, request="Fix the bug"):
    state = TaskState.create("task-progress", request, run_id="run-progress")
    agent.current_task_state = state
    agent.start_runtime_progress(state)
    return state


def record_tool(agent, state, name, args=None, **metadata):
    state.record_tool(name)
    result_metadata = {
        "tool_status": "ok",
        "workspace_changed": False,
        **metadata,
    }
    agent.record_runtime_progress_after_tool(state, name, args or {}, result_metadata)


def test_fifteen_explore_steps_inject_soft_hint_without_replacing_request(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    original_tools = tuple(agent.available_tools())

    for _ in range(15):
        record_tool(agent, state, "search", {"pattern": "bug", "path": "."})

    prompt, metadata = agent._build_prompt_and_metadata("Fix the bug")

    assert "[Convergence Controller]" in prompt
    assert "most likely root cause" in prompt
    assert "most likely file/function" in prompt
    assert "smallest viable fix" in prompt
    assert prompt.rstrip().endswith("Current user request:\nFix the bug")
    assert metadata["current_request"]["text"] == "Fix the bug"
    assert metadata["runtime_progress"]["kind"] == "soft_convergence"
    assert state.to_dict()["current_phase"] == "CONVERGE"
    assert state.to_dict()["first_convergence_step"] == 15
    assert state.to_dict()["convergence_trigger_count"] == 1
    assert agent.approval_policy == "auto"
    assert tuple(agent.available_tools()) == original_tools


def test_agent_loop_injects_soft_hint_once_after_fifteenth_explore_step(tmp_path):
    outputs = [
        f'<tool>{{"name":"search","args":{{"pattern":"bug-{index}","path":"."}}}}</tool>'
        for index in range(16)
    ] + ["<final>Done.</final>"]
    agent = build_agent(tmp_path, outputs=outputs, max_steps=40)

    assert agent.ask("Fix the bug") == "Done."

    assert "[Convergence Controller]" not in agent.model_client.prompts[14]
    assert "[Convergence Controller]" in agent.model_client.prompts[15]
    assert "most likely root cause" in agent.model_client.prompts[15]
    assert "[Convergence Controller]" not in agent.model_client.prompts[16]
    hint_events = [
        event
        for event in json.loads(
            "["
            + ",".join((agent.current_run_dir / "trace.jsonl").read_text().splitlines())
            + "]"
        )
        if event["event"] == "convergence_hint_injected"
    ]
    assert len(hint_events) == 1
    assert hint_events[0]["kind"] == "soft_convergence"
    trace_events = json.loads(
        "["
        + ",".join((agent.current_run_dir / "trace.jsonl").read_text().splitlines())
        + "]"
    )
    transition = next(
        event
        for event in trace_events
        if event["event"] == "convergence_phase_transition"
        and event["to"] == "CONVERGE"
    )
    assert transition["step"] == 15
    fifteenth_tool = [
        event for event in trace_events if event["event"] == "tool_executed"
    ][14]
    assert fifteenth_tool["convergence_controller"]["current_phase"] == "CONVERGE"
    report = json.loads((agent.current_run_dir / "report.json").read_text())
    assert report["convergence_trigger_count"] == 1
    assert report["first_convergence_step"] == 15
    assert agent.current_task_state.to_dict()["max_consecutive_explore_steps"] == 16


def test_fourteen_explore_steps_do_not_inject_hint(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)

    for _ in range(14):
        record_tool(agent, state, "read_file", {"path": "README.md"})

    context = agent.runtime_progress_context()

    assert context["active"] is False
    assert context["text"] == ""
    assert state.to_dict()["current_phase"] == "EXPLORE"
    assert state.to_dict()["convergence_trigger_count"] == 0


def test_twenty_fifth_explore_step_triggers_hard_convergence(tmp_path):
    agent = build_agent(tmp_path, max_steps=60)
    state = start_progress(agent)

    for _ in range(25):
        record_tool(agent, state, "list_files", {"path": "."})

    context = agent.runtime_progress_context()

    assert context["kind"] == "hard_convergence"
    assert context["remaining_steps"] == 35
    assert "prioritize implementing" in context["text"]
    assert "One directly related read" in context["text"]
    snapshot = state.to_dict()
    assert snapshot["current_phase"] == "MODIFY"
    assert snapshot["hard_convergence_triggered"] is True
    assert snapshot["hard_convergence_step"] == 25
    assert snapshot["convergence_trigger_count"] == 2


def test_workspace_change_resets_explore_and_records_first_change(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    for _ in range(4):
        record_tool(agent, state, "search")

    record_tool(
        agent,
        state,
        "patch_file",
        {"path": "README.md"},
        workspace_changed=True,
        affected_paths=["README.md"],
    )

    assert state.to_dict()["consecutive_explore_steps"] == 0
    assert state.to_dict()["first_change_step"] == 5
    assert state.to_dict()["steps_since_last_change"] == 0
    assert state.to_dict()["has_changed_workspace"] is True
    assert state.to_dict()["verification_after_change"] is False
    assert state.to_dict()["phase_hint"] == "verify/finalize"
    assert state.to_dict()["current_phase"] == "VERIFY"


def test_workspace_change_immediately_prioritizes_verification(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    record_tool(agent, state, "write_file", workspace_changed=True)

    context = agent.runtime_progress_context()

    assert context["kind"] == "verify_after_change"
    assert "most relevant test" in context["text"]


def test_successful_shell_after_change_marks_verified(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    record_tool(agent, state, "patch_file", workspace_changed=True)

    record_tool(
        agent,
        state,
        "run_shell",
        {"command": "pytest -q"},
        workspace_changed=True,
    )

    assert state.to_dict()["verification_after_change"] is True
    assert state.to_dict()["current_phase"] == "VERIFY"
    assert agent.runtime_progress_context()["active"] is False


def test_failed_verification_returns_to_modify(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    record_tool(agent, state, "patch_file", workspace_changed=True)

    record_tool(
        agent,
        state,
        "run_shell",
        {"command": "pytest -q"},
        tool_status="error",
    )

    snapshot = state.to_dict()
    assert snapshot["current_phase"] == "MODIFY"
    assert snapshot["verification_after_change"] is False
    assert snapshot["verification_failure_count"] == 1
    assert "Verification failed" in agent.runtime_progress_context()["text"]


def test_hard_convergence_does_not_generate_a_patch(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    original = (tmp_path / "README.md").read_text(encoding="utf-8")

    for _ in range(25):
        record_tool(agent, state, "search")

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == original
    assert state.to_dict()["patch_file_calls"] == 0
    assert state.to_dict()["write_file_calls"] == 0
    assert state.to_dict()["first_change_step"] is None


def test_change_before_soft_threshold_has_no_convergence_intervention(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    for _ in range(10):
        record_tool(agent, state, "search")

    record_tool(agent, state, "patch_file", workspace_changed=True)

    snapshot = state.to_dict()
    assert snapshot["convergence_trigger_count"] == 0
    assert snapshot["first_convergence_step"] is None
    assert snapshot["hard_convergence_triggered"] is False
    assert snapshot["current_phase"] == "VERIFY"


def test_controller_failure_is_recorded_and_does_not_create_patch(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    original = (tmp_path / "README.md").read_text(encoding="utf-8")

    def fail_controller(*_args, **_kwargs):
        raise RuntimeError("controller reducer failed")

    agent.runtime_progress.record_tool = fail_controller
    record_tool(agent, state, "search")

    errors = state.to_dict()["convergence_controller_errors"]
    assert errors[-1]["stage"] == "record_tool"
    assert errors[-1]["error_type"] == "RuntimeError"
    assert "controller reducer failed" in errors[-1]["error_message"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == original
    trace = [
        json.loads(line)
        for line in agent.run_store.trace_path(state).read_text().splitlines()
    ]
    assert trace[-1]["event"] == "convergence_controller_error"


def test_read_cooldown_expires_and_repeated_ranges_are_observable(tmp_path):
    config = RuntimeProgressConfig(relevant_memory_source_cooldown_steps=3)
    agent = build_agent(tmp_path, config=config)
    state = start_progress(agent)
    record_tool(
        agent,
        state,
        "read_file",
        {"path": "./README.md", "start": 1, "end": 120},
    )

    assert agent.relevant_memory_excluded_sources() == {"README.md"}
    record_tool(agent, state, "search")
    record_tool(
        agent,
        state,
        "read_file",
        {"path": "README.md", "start": 1, "end": 200},
    )
    assert state.to_dict()["repeated_source_read_count"] == 1
    assert state.to_dict()["overlapping_read_count"] == 1
    record_tool(agent, state, "search")
    record_tool(agent, state, "search")
    record_tool(agent, state, "search")

    assert agent.relevant_memory_excluded_sources() == set()


def test_progress_fields_are_persisted_in_task_state_and_report(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    record_tool(agent, state, "read_file", {"path": "README.md"})
    record_tool(agent, state, "read_file", {"path": "README.md"})
    record_tool(agent, state, "patch_file", workspace_changed=True)
    record_tool(agent, state, "run_shell")

    snapshot = state.to_dict()
    restored = TaskState.from_dict(json.loads(json.dumps(snapshot)))
    report = agent.build_report(state)

    for field in (
        "convergence_trigger_count",
        "first_convergence_step",
        "hard_convergence_triggered",
        "hard_convergence_step",
        "phase_transitions",
        "steps_since_last_change_peak",
        "first_change_step",
        "verification_after_change",
        "patch_file_calls",
        "write_file_calls",
        "max_consecutive_explore_steps",
    ):
        assert restored.to_dict()[field] == snapshot[field]
        assert report["task_state"][field] == snapshot[field]
        assert report[field] == snapshot[field]
    assert (
        restored.to_dict()["repeated_source_read_count"]
        == snapshot["repeated_source_read_count"]
    )
    assert (
        report["task_state"]["repeated_source_read_count"]
        == snapshot["repeated_source_read_count"]
    )
