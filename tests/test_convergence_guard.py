import json

from patchfox.core.task_state import TaskState
from patchfox.evaluation.swebench_experiment import _aggregate_rows
from tests.test_runtime_progress import build_agent, record_tool, start_progress


def _reach_hard_guard(agent, state):
    for _ in range(25):
        record_tool(agent, state, "search", {"pattern": "needle", "path": "."})


def test_exploration_before_hard_threshold_is_not_restricted(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    for _ in range(24):
        record_tool(agent, state, "search")

    for name in ("read_file", "search", "list_files", "run_shell"):
        assert agent.convergence_guard_decision(name, {}).allowed
    assert state.runtime_progress["hard_tool_gating_active"] is False


def test_hard_guard_rejects_search_and_list_files_with_distinct_metrics(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    _reach_hard_guard(agent, state)

    search = agent.convergence_guard_decision("search", {"pattern": "next"})
    listing = agent.convergence_guard_decision("list_files", {"path": "."})

    assert not search.allowed
    assert not listing.allowed
    assert search.reason == "exploration_budget_exhausted"
    assert "Make the smallest justified edit" in search.message
    assert state.runtime_progress["hard_tool_gating_active"] is True
    assert state.runtime_progress["hard_tool_gating_trigger_step"] == 25
    assert state.runtime_progress["hard_tool_rejection_count"] == 2
    assert state.runtime_progress["hard_search_rejection_count"] == 1
    assert state.runtime_progress["hard_list_rejection_count"] == 1
    assert state.runtime_progress["convergence_guard_event_counts"] == {
        "activated": 1,
        "rejected": 2,
    }


def test_hard_guard_allows_two_targeted_reads_then_rejects_third(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    _reach_hard_guard(agent, state)

    assert agent.convergence_guard_decision("read_file", {"path": "README.md"}).allowed
    assert agent.convergence_guard_decision("read_file", {"path": "README.md"}).allowed
    third = agent.convergence_guard_decision("read_file", {"path": "README.md"})

    assert not third.allowed
    assert state.runtime_progress["hard_targeted_read_used"] == 2
    assert state.runtime_progress["hard_targeted_read_remaining"] == 0
    assert state.runtime_progress["hard_read_rejection_count"] == 1


def test_guard_allows_edits_and_shell_but_tool_policy_remains_in_force(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    _reach_hard_guard(agent, state)

    for name in ("patch_file", "write_file", "run_shell"):
        assert agent.convergence_guard_decision(name, {}).allowed
    result = agent.run_tool("run_shell", {"command": "rg needle"})

    assert "run_shell is not for ordinary workspace search" in result
    assert agent._last_tool_result_metadata["security_event_type"] == "tool_policy"
    assert state.runtime_progress["hard_tool_rejection_count"] == 0


def test_workspace_change_lifts_guard_and_resets_targeted_read_allowance(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    _reach_hard_guard(agent, state)
    agent.convergence_guard_decision("read_file", {"path": "README.md"})
    agent.convergence_guard_decision("read_file", {"path": "README.md"})
    record_tool(agent, state, "patch_file", workspace_changed=True)

    assert state.runtime_progress["current_phase"] == "VERIFY"
    assert state.runtime_progress["hard_tool_gating_active"] is False
    assert state.runtime_progress["hard_targeted_read_used"] == 0
    assert state.runtime_progress["hard_targeted_read_remaining"] == 2
    assert agent.convergence_guard_decision("search", {"pattern": "again"}).allowed


def test_guard_rejection_is_strategy_evidence_not_security_event(tmp_path):
    agent = build_agent(tmp_path)
    state = start_progress(agent)
    _reach_hard_guard(agent, state)

    result = agent.run_tool("search", {"pattern": "blocked", "path": "."})
    metadata = agent._last_tool_result_metadata
    trace = [
        json.loads(line)
        for line in agent.run_store.trace_path(state).read_text(encoding="utf-8").splitlines()
    ]

    assert "Exploration budget exhausted" in result
    assert metadata["tool_status"] == "rejected"
    assert metadata["rejection_source"] == "convergence_guard"
    assert metadata["reason"] == "exploration_budget_exhausted"
    assert metadata["security_event_type"] == ""
    decision = next(
        event
        for event in trace
        if event["event"] == "governance_decision"
        and event["decision_type"] == "convergence_guard"
    )
    assert decision["decision_type"] == "convergence_guard"
    assert decision["security_event_type"] == ""
    assert any(event["event"] == "convergence_guard_rejected" for event in trace)
    report = agent.build_report(state)
    assert report["hard_search_rejection_count"] == 1
    assert report["convergence_guard_event_counts"]["rejected"] == 1


def test_engine_records_guard_rejection_in_tool_trace(tmp_path):
    outputs = [
        f'<tool>{{"name":"search","args":{{"pattern":"needle-{index}","path":"."}}}}</tool>'
        for index in range(26)
    ] + ["<final>Done.</final>"]
    agent = build_agent(tmp_path, outputs=outputs, max_steps=30)

    assert agent.ask("Fix the bug") == "Done."
    trace = [
        json.loads(line)
        for line in (agent.current_run_dir / "trace.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    tools = [event for event in trace if event["event"] == "tool_executed"]

    assert len(tools) == 26
    assert tools[-1]["tool_status"] == "rejected"
    assert tools[-1]["rejection_source"] == "convergence_guard"
    assert tools[-1]["security_event_type"] == ""
    assert tools[-1]["convergence_controller"]["hard_search_rejection_count"] == 1


def test_guard_failure_fails_open_and_legacy_state_remains_compatible(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    state = start_progress(agent)

    def fail_guard(*_args, **_kwargs):
        raise RuntimeError("guard failed")

    monkeypatch.setattr("patchfox.core.runtime_progress_runtime.decide_guard", fail_guard)
    assert "README.md" in agent.run_tool("search", {"pattern": "demo", "path": "."})
    assert state.runtime_progress["convergence_controller_errors"][-1]["stage"] == "tool_guard"

    legacy = state.to_dict()
    for key in (
        "hard_tool_gating_active",
        "hard_tool_gating_trigger_step",
        "hard_tool_rejection_count",
        "hard_search_rejection_count",
        "hard_list_rejection_count",
        "hard_read_rejection_count",
        "hard_targeted_read_used",
        "hard_targeted_read_remaining",
    ):
        legacy.pop(key)
    restored = TaskState.from_dict(legacy)
    assert restored.runtime_progress["hard_tool_gating_active"] is False
    assert restored.runtime_progress["hard_targeted_read_remaining"] == 2


def test_experiment_summary_aggregates_guard_metrics():
    summary = _aggregate_rows(
        [
            {
                "generation_status": "completed",
                "official_status": "unavailable",
                "resolved": None,
                "model_patch_bytes": 0,
                "stop_reason": "",
                "final_answer_present": False,
                "changed_instance": False,
                "hard_tool_gating_trigger_step": 25,
                "hard_tool_rejection_count": 3,
                "hard_targeted_read_used": 2,
            }
        ]
    )

    metrics = summary["p3_tool_convergence_guard"]
    assert metrics["hard_tool_gating_instance_count"] == 1
    assert metrics["hard_tool_gating_instance_rate"] == 1.0
    assert metrics["total_hard_tool_rejections"] == 3
    assert metrics["mean_hard_tool_rejections"] == 3
    assert metrics["mean_hard_targeted_reads_used"] == 2
