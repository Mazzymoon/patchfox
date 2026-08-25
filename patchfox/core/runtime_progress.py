"""Lightweight progress signals for explore/edit/verify convergence."""

from __future__ import annotations

from dataclasses import dataclass

EXPLORE_TOOLS = frozenset({"read_file", "search", "list_files", "run_shell"})
POST_CHANGE_EXPLORE_TOOLS = frozenset({"read_file", "search", "list_files"})
PROGRESS_STATE_DEFAULTS = {
    "consecutive_explore_steps": 0,
    "steps_since_workspace_change": 0,
    "first_change_step": None,
    "verification_after_change": False,
    "max_consecutive_explore_steps": 0,
    "post_change_explore_steps": 0,
    "repeated_source_read_count": 0,
    "overlapping_read_count": 0,
    "phase_hint": "",
}


def default_runtime_progress_state():
    return dict(PROGRESS_STATE_DEFAULTS)


def runtime_progress_state_from_dict(data):
    state = default_runtime_progress_state()
    for key, default in PROGRESS_STATE_DEFAULTS.items():
        value = data.get(key, default)
        if default is False:
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        elif default is None:
            value = None if value is None else int(value)
        else:
            value = str(value)
        state[key] = value
    return state


@dataclass(frozen=True)
class RuntimeProgressConfig:
    relevant_memory_source_cooldown_steps: int = 3
    convergence_explore_threshold: int = 8
    convergence_verify_threshold: int = 5
    convergence_remaining_steps_warning: int = 15
    convergence_remaining_steps_urgent: int = 8


class RuntimeProgress:
    """Track small, explainable convergence signals for one active turn."""

    def __init__(self, config=None):
        self.config = config or RuntimeProgressConfig()
        self._recent_reads = {}

    def start_turn(self, task_state):
        self._recent_reads = {}
        task_state.runtime_progress["phase_hint"] = "explore"

    def record_tool(self, task_state, name, args, metadata):
        name = str(name or "")
        args = dict(args or {})
        metadata = dict(metadata or {})
        step = int(task_state.tool_steps)
        status = str(metadata.get("tool_status", ""))
        workspace_changed = bool(metadata.get("workspace_changed", False))
        state = task_state.runtime_progress
        had_change = state["first_change_step"] is not None or bool(
            task_state.changed_paths
        )

        if workspace_changed:
            if state["first_change_step"] is None:
                state["first_change_step"] = step
            state["consecutive_explore_steps"] = 0
            state["steps_since_workspace_change"] = 0
            state["post_change_explore_steps"] = 0
            state["verification_after_change"] = False
            state["phase_hint"] = "verify/finalize"
            if had_change and name == "run_shell" and status == "ok":
                state["verification_after_change"] = True
                state["phase_hint"] = "finalize"
        else:
            state["steps_since_workspace_change"] += 1
            if name in EXPLORE_TOOLS:
                state["consecutive_explore_steps"] += 1
                state["max_consecutive_explore_steps"] = max(
                    state["max_consecutive_explore_steps"],
                    state["consecutive_explore_steps"],
                )
            else:
                state["consecutive_explore_steps"] = 0

            if had_change and name == "run_shell" and status == "ok":
                state["verification_after_change"] = True
                state["post_change_explore_steps"] = 0
                state["consecutive_explore_steps"] = 0
                state["phase_hint"] = "finalize"
            elif had_change and name in POST_CHANGE_EXPLORE_TOOLS:
                state["post_change_explore_steps"] += 1
                state["phase_hint"] = "verify/finalize"
            elif had_change:
                state["post_change_explore_steps"] = 0

        if name == "read_file" and status == "ok":
            self._record_successful_read(task_state, args, step)

    def recent_sources(self, current_step):
        current_step = int(current_step)
        cooldown = max(
            0, int(self.config.relevant_memory_source_cooldown_steps)
        )
        return {
            path
            for path, read in self._recent_reads.items()
            if current_step - int(read["step"]) < cooldown
        }

    def prompt_hint(self, task_state, max_steps):
        if task_state is None:
            return {
                "active": False,
                "kind": "",
                "text": "",
                "remaining_steps": int(max_steps),
                "phase_hint": "",
            }
        remaining_steps = max(0, int(max_steps) - int(task_state.tool_steps))
        state = task_state.runtime_progress
        changed = state["first_change_step"] is not None or bool(
            task_state.changed_paths
        )
        kind = ""
        message = ""

        if not changed and remaining_steps <= int(
            self.config.convergence_remaining_steps_urgent
        ):
            kind = "urgent_edit"
            message = (
                "No workspace change exists yet. Unless clearly blocked, make the "
                "smallest viable fix now and verify it."
            )
            state["phase_hint"] = "edit"
        elif not changed and remaining_steps <= int(
            self.config.convergence_remaining_steps_warning
        ):
            kind = "step_warning"
            message = (
                "Steps are limited and no workspace change exists. Prioritize a "
                "minimal edit and verification over repeated exploration."
            )
            state["phase_hint"] = "edit"
        elif not changed and state["consecutive_explore_steps"] >= int(
            self.config.convergence_explore_threshold
        ):
            kind = "explore_convergence"
            message = (
                "You have taken many read-only exploration steps without changing "
                "the workspace. If evidence is sufficient, summarize the likely root "
                "cause, make the smallest fix, and verify it; explore further only for "
                "missing critical evidence."
            )
            state["phase_hint"] = "edit"
        elif (
            changed
            and not state["verification_after_change"]
            and state["post_change_explore_steps"]
            >= int(self.config.convergence_verify_threshold)
        ):
            kind = "verify_change"
            message = (
                "Code has changed. Run the most relevant verification command now, "
                "then use its result to decide whether to edit again or finish."
            )
            state["phase_hint"] = "verify/finalize"

        return {
            "active": bool(message),
            "kind": kind,
            "text": f"[Runtime Progress]\n- {message}" if message else "",
            "remaining_steps": remaining_steps,
            "phase_hint": state["phase_hint"],
        }

    def _record_successful_read(self, task_state, args, step):
        path = str(args.get("path", "")).strip()
        if not path:
            return
        previous = self._recent_reads.get(path)
        window = max(
            0, int(self.config.relevant_memory_source_cooldown_steps)
        )
        if previous and step - int(previous["step"]) <= window:
            task_state.runtime_progress["repeated_source_read_count"] += 1
            current_range = self._read_range(args)
            if self._ranges_overlap(previous["range"], current_range):
                task_state.runtime_progress["overlapping_read_count"] += 1
        self._recent_reads[path] = {
            "step": step,
            "range": self._read_range(args),
        }

    @staticmethod
    def _read_range(args):
        return int(args.get("start", 1)), int(args.get("end", 200))

    @staticmethod
    def _ranges_overlap(left, right):
        return max(left[0], right[0]) <= min(left[1], right[1])
