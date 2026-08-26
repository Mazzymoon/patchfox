"""Pre-edit tool guard for the P3 convergence controller.

This is strategy control, not a security policy: permissions and ToolPolicy run
first, and guard denials deliberately carry no security event classification.
"""

from __future__ import annotations

from dataclasses import dataclass

from .governance import record_governance_decision

EXPLORATION_BUDGET_MESSAGE = (
    "Exploration budget exhausted. Make the smallest justified edit. "
    "One targeted read may remain."
)


@dataclass(frozen=True)
class ConvergenceGuardDecision:
    allowed: bool
    reason: str = ""
    message: str = ""


def decide(task_state, name, config):
    """Apply P3 only after P2 hard convergence and before the first edit."""

    state = task_state.runtime_progress
    if state.get("has_changed_workspace"):
        return ConvergenceGuardDecision(True)
    hard_limit = int(config.hard_convergence_explore_threshold)
    hard_reached = bool(state.get("hard_convergence_triggered")) or (
        int(state.get("consecutive_explore_steps", 0) or 0) >= hard_limit
    )
    if not hard_reached:
        return ConvergenceGuardDecision(True)
    if name in {"patch_file", "write_file", "run_shell"}:
        return ConvergenceGuardDecision(True)
    if name == "read_file":
        remaining = int(state.get("hard_targeted_read_remaining", 0) or 0)
        if remaining > 0:
            state["hard_targeted_read_used"] += 1
            state["hard_targeted_read_remaining"] = remaining - 1
            _increment_event(state, "targeted_read_allowed")
            return ConvergenceGuardDecision(True)
        return _reject(state, name)
    if name in {"search", "list_files"}:
        return _reject(state, name)
    return ConvergenceGuardDecision(True)


def _reject(state, name):
    state["hard_tool_rejection_count"] += 1
    counter = {
        "search": "hard_search_rejection_count",
        "list_files": "hard_list_rejection_count",
        "read_file": "hard_read_rejection_count",
    }[name]
    state[counter] += 1
    _increment_event(state, "rejected")
    return ConvergenceGuardDecision(
        False,
        reason="exploration_budget_exhausted",
        message=EXPLORATION_BUDGET_MESSAGE,
    )


def _increment_event(state, event):
    counts = state.setdefault("convergence_guard_event_counts", {})
    counts[event] = int(counts.get(event, 0) or 0) + 1


def reject_tool_call(agent, tool, name, args):
    """Return a guard result string, or ``None`` when execution may continue."""

    decision = agent.convergence_guard_decision(name, args)
    if decision.allowed:
        return None
    agent._last_tool_result_metadata = {
        "tool_status": "rejected",
        "tool_error_code": decision.reason,
        "security_event_type": "",
        "risk_level": "high" if tool.risky else "low",
        "read_only": tool.read_only,
        "affected_paths": [],
        "workspace_changed": False,
        "diff_summary": [],
        "rejection_source": "convergence_guard",
        "reason": decision.reason,
        "convergence_guard_reason": decision.reason,
    }
    record_governance_decision(
        agent,
        name,
        args,
        decision="deny",
        reason_code=decision.reason,
        decision_type="convergence_guard",
        original_reason=decision.reason,
        source="convergence_guard",
    )
    agent.record_process_note_for_tool(name, agent._last_tool_result_metadata)
    return decision.message
