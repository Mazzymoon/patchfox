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
    agent.record_runtime_progress_after_tool(
        state, name, args or {}, result_metadata
    )


def test_eight_explore_steps_inject_soft_hint_without_replacing_request(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    original_tools = tuple(agent.available_tools())

    for _ in range(8):
        record_tool(agent, state, "search", {"pattern": "bug", "path": "."})

    prompt, metadata = agent._build_prompt_and_metadata("Fix the bug")

    assert "[Runtime Progress]" in prompt
    assert "many read-only exploration steps" in prompt
    assert prompt.rstrip().endswith("Current user request:\nFix the bug")
    assert metadata["current_request"]["text"] == "Fix the bug"
    assert metadata["runtime_progress"]["kind"] == "explore_convergence"
    assert agent.approval_policy == "auto"
    assert tuple(agent.available_tools()) == original_tools


def test_agent_loop_injects_hint_on_model_call_after_eighth_explore_step(tmp_path):
    outputs = [
        f'<tool>{{"name":"search","args":{{"pattern":"bug-{index}","path":"."}}}}</tool>'
        for index in range(8)
    ] + ["<final>Done.</final>"]
    agent = build_agent(tmp_path, outputs=outputs, max_steps=30)

    assert agent.ask("Fix the bug") == "Done."

    assert "[Runtime Progress]" not in agent.model_client.prompts[7]
    assert "[Runtime Progress]" in agent.model_client.prompts[8]
    assert "exploration steps" in agent.model_client.prompts[8]
    assert agent.current_task_state.to_dict()[
        "max_consecutive_explore_steps"
    ] == 8


def test_explore_below_threshold_does_not_inject_hint(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)

    for _ in range(7):
        record_tool(agent, state, "read_file", {"path": "README.md"})

    context = agent.runtime_progress_context()

    assert context["active"] is False
    assert context["text"] == ""


def test_remaining_step_warning_is_stronger_before_explore_threshold(tmp_path):
    agent = build_agent(tmp_path, max_steps=20)
    state = start_progress(agent)

    for _ in range(5):
        record_tool(agent, state, "list_files", {"path": "."})

    context = agent.runtime_progress_context()

    assert context["kind"] == "step_warning"
    assert context["remaining_steps"] == 15
    assert "Steps are limited" in context["text"]


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
    assert state.to_dict()["verification_after_change"] is False
    assert state.to_dict()["phase_hint"] == "verify/finalize"


def test_five_post_change_reads_trigger_verify_hint(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    record_tool(agent, state, "write_file", workspace_changed=True)

    for _ in range(5):
        record_tool(agent, state, "read_file", {"path": "README.md"})

    context = agent.runtime_progress_context()

    assert context["kind"] == "verify_change"
    assert "verification command" in context["text"]


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
    assert state.to_dict()["phase_hint"] == "finalize"
    assert agent.runtime_progress_context()["active"] is False


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
        "first_change_step",
        "verification_after_change",
        "max_consecutive_explore_steps",
        "repeated_source_read_count",
    ):
        assert restored.to_dict()[field] == snapshot[field]
        assert report["task_state"][field] == snapshot[field]
