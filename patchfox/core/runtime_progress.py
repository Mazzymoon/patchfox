"""Event-driven explore/modify/verify convergence control."""

from __future__ import annotations

from dataclasses import dataclass

from .verification import classify_verification_command

PHASE_EXPLORE = "EXPLORE"
PHASE_CONVERGE = "CONVERGE"
PHASE_MODIFY = "MODIFY"
PHASE_VERIFY = "VERIFY"
CONVERGENCE_PHASES = (
    PHASE_EXPLORE,
    PHASE_CONVERGE,
    PHASE_MODIFY,
    PHASE_VERIFY,
)

EXPLORE_TOOLS = frozenset({"read_file", "search", "list_files", "run_shell"})
POST_CHANGE_EXPLORE_TOOLS = frozenset({"read_file", "search", "list_files"})
SOFT_HINT_ID = "soft-convergence"
HARD_HINT_ID = "hard-convergence"
VERIFY_HINT_ID = "verify-after-change"

PROGRESS_STATE_DEFAULTS = {
    # Existing P1 fields remain stable for old evidence readers.
    "consecutive_explore_steps": 0,
    "steps_since_workspace_change": 0,
    "first_change_step": None,
    "verification_after_change": False,
    "max_consecutive_explore_steps": 0,
    "post_change_explore_steps": 0,
    "repeated_source_read_count": 0,
    "overlapping_read_count": 0,
    "phase_hint": "explore",
    # P2 controller state and observability.
    "steps_since_last_change": 0,
    "steps_since_last_change_peak": 0,
    "has_changed_workspace": False,
    "current_phase": PHASE_EXPLORE,
    "phase_transitions": [],
    "last_phase_reason": "",
    "convergence_trigger_count": 0,
    "first_convergence_step": None,
    "hard_convergence_triggered": False,
    "hard_convergence_step": None,
    "soft_convergence_hint_pending": False,
    "soft_convergence_hint_injected": False,
    "patch_file_calls": 0,
    "write_file_calls": 0,
    "verification_failure_count": 0,
    "convergence_controller_errors": [],
}


def default_runtime_progress_state():
    state = dict(PROGRESS_STATE_DEFAULTS)
    state["phase_transitions"] = []
    state["convergence_controller_errors"] = []
    return state


def runtime_progress_state_from_dict(data):
    data = dict(data or {})
    state = default_runtime_progress_state()
    for key, default in PROGRESS_STATE_DEFAULTS.items():
        value = data.get(key, default)
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value or 0)
        elif default is None:
            value = None if value is None else int(value)
        elif isinstance(default, list):
            value = list(value or [])
        else:
            value = str(value)
        state[key] = value

    # Old P1 task states used only steps_since_workspace_change/phase_hint.
    if "steps_since_last_change" not in data:
        state["steps_since_last_change"] = int(
            data.get("steps_since_workspace_change", 0) or 0
        )
    if "steps_since_last_change_peak" not in data:
        state["steps_since_last_change_peak"] = state["steps_since_last_change"]
    if "has_changed_workspace" not in data:
        state["has_changed_workspace"] = bool(
            state["first_change_step"] is not None or data.get("changed_paths")
        )
    if state["current_phase"] not in CONVERGENCE_PHASES:
        state["current_phase"] = (
            PHASE_VERIFY if state["has_changed_workspace"] else PHASE_EXPLORE
        )
    return state


@dataclass(frozen=True)
class RuntimeProgressConfig:
    relevant_memory_source_cooldown_steps: int = 3
    convergence_explore_threshold: int = 15
    hard_convergence_explore_threshold: int = 25
    # Retained for API compatibility; P2 deliberately does not use remaining-step
    # warnings or a delayed verification threshold as extra interventions.
    convergence_verify_threshold: int = 5
    convergence_remaining_steps_warning: int = 15
    convergence_remaining_steps_urgent: int = 8


class RuntimeProgress:
    """Maintain the P2 convergence state machine from real tool evidence."""

    def __init__(self, config=None):
        self.config = config or RuntimeProgressConfig()
        self._recent_reads = {}

    def start_turn(self, task_state):
        self._recent_reads = {}
        state = task_state.runtime_progress
        state["phase_hint"] = "explore"
        state["current_phase"] = PHASE_EXPLORE
        if not state["phase_transitions"]:
            state["phase_transitions"].append(
                {
                    "from": None,
                    "to": PHASE_EXPLORE,
                    "step": 0,
                    "reason": "turn_started",
                }
            )

    def record_tool(self, task_state, name, args, metadata):
        """Reduce one executed tool event and return trace-ready controller events."""

        name = str(name or "")
        args = dict(args or {})
        metadata = dict(metadata or {})
        step = int(task_state.tool_steps)
        status = str(metadata.get("tool_status", ""))
        workspace_changed = bool(metadata.get("workspace_changed", False))
        state = task_state.runtime_progress
        events = []

        if name == "patch_file":
            state["patch_file_calls"] += 1
        elif name == "write_file":
            state["write_file_calls"] += 1

        had_change = bool(
            state["has_changed_workspace"]
            or state["first_change_step"] is not None
            or task_state.changed_paths
        )
        verification_class = (
            classify_verification_command(str(args.get("command", "")))
            if name == "run_shell"
            else ""
        )

        if workspace_changed:
            state["has_changed_workspace"] = True
            if state["first_change_step"] is None:
                state["first_change_step"] = step
            state["consecutive_explore_steps"] = 0
            state["steps_since_workspace_change"] = 0
            state["steps_since_last_change"] = 0
            state["post_change_explore_steps"] = 0
            state["verification_after_change"] = False
            state["soft_convergence_hint_pending"] = False
            transition = self._transition(
                state, PHASE_VERIFY, step, "workspace_changed"
            )
            if transition:
                events.append(transition)
        else:
            state["steps_since_workspace_change"] += 1
            state["steps_since_last_change"] += 1
            state["steps_since_last_change_peak"] = max(
                state["steps_since_last_change_peak"],
                state["steps_since_last_change"],
            )
            if name in EXPLORE_TOOLS:
                state["consecutive_explore_steps"] += 1
                state["max_consecutive_explore_steps"] = max(
                    state["max_consecutive_explore_steps"],
                    state["consecutive_explore_steps"],
                )
            else:
                state["consecutive_explore_steps"] = 0

            if had_change and name in POST_CHANGE_EXPLORE_TOOLS:
                state["post_change_explore_steps"] += 1
            elif had_change:
                state["post_change_explore_steps"] = 0

        if not state["has_changed_workspace"]:
            events.extend(self._apply_pre_change_thresholds(state, step))

        if had_change or workspace_changed:
            verification_event = self._apply_verification_result(
                state,
                step=step,
                command_class=verification_class,
                status=status,
            )
            if verification_event:
                events.append(verification_event)

        if name == "read_file" and status == "ok":
            self._record_successful_read(task_state, args, step)
        return {
            "events": events,
            "snapshot": convergence_report_fields(state),
        }

    def _apply_pre_change_thresholds(self, state, step):
        events = []
        explore_steps = int(state["consecutive_explore_steps"])
        soft_threshold = int(self.config.convergence_explore_threshold)
        hard_threshold = int(self.config.hard_convergence_explore_threshold)

        if explore_steps >= soft_threshold and state["first_convergence_step"] is None:
            state["convergence_trigger_count"] += 1
            state["first_convergence_step"] = step
            state["soft_convergence_hint_pending"] = True
            transition = self._transition(
                state, PHASE_CONVERGE, step, "soft_threshold_reached"
            )
            if transition:
                events.append(transition)

        if explore_steps >= hard_threshold and not state["hard_convergence_triggered"]:
            state["convergence_trigger_count"] += 1
            state["hard_convergence_triggered"] = True
            state["hard_convergence_step"] = step
            state["soft_convergence_hint_pending"] = False
            transition = self._transition(
                state, PHASE_MODIFY, step, "hard_threshold_reached"
            )
            if transition:
                events.append(transition)
        return events

    def _apply_verification_result(self, state, *, step, command_class, status):
        if not command_class:
            return None
        passed = status in {"", "ok"}
        if passed:
            state["verification_after_change"] = True
            return {
                "event": "convergence_verification",
                "step": step,
                "result": "passed",
                "command_class": command_class,
                "current_phase": state["current_phase"],
            }
        state["verification_after_change"] = False
        state["verification_failure_count"] += 1
        transition = self._transition(state, PHASE_MODIFY, step, "verification_failed")
        if transition:
            transition["command_class"] = command_class
            return transition
        return {
            "event": "convergence_verification",
            "step": step,
            "result": "failed",
            "command_class": command_class,
            "current_phase": state["current_phase"],
        }

    @staticmethod
    def _transition(state, target, step, reason):
        source = state["current_phase"]
        if source == target:
            state["last_phase_reason"] = reason
            state["phase_hint"] = _legacy_phase_hint(target)
            return None
        transition = {
            "from": source,
            "to": target,
            "step": int(step),
            "reason": str(reason),
        }
        state["current_phase"] = target
        state["last_phase_reason"] = str(reason)
        state["phase_hint"] = _legacy_phase_hint(target)
        state["phase_transitions"].append(transition)
        return {
            "event": "convergence_phase_transition",
            **transition,
            "current_phase": target,
        }

    def prompt_hint(self, task_state, max_steps):
        if task_state is None:
            return _inactive_prompt_context(max_steps)
        state = task_state.runtime_progress
        remaining_steps = max(0, int(max_steps) - int(task_state.tool_steps))
        phase = state["current_phase"]
        kind = ""
        hint_id = ""
        message = ""

        if (
            phase == PHASE_CONVERGE
            and state["soft_convergence_hint_pending"]
            and not state["soft_convergence_hint_injected"]
        ):
            kind = "soft_convergence"
            hint_id = SOFT_HINT_ID
            message = (
                "Convergence checkpoint: stop broadening the search. Before taking "
                "more actions, summarize the most likely root cause, identify the "
                "most likely file/function to change, and state the smallest viable "
                "fix. Continue exploration only for one concrete fact needed to "
                "implement that specific fix."
            )
        elif phase == PHASE_MODIFY:
            kind = "hard_convergence"
            hint_id = HARD_HINT_ID
            if state["last_phase_reason"] == "verification_failed":
                message = (
                    "Verification failed. Return to the smallest concrete code change "
                    "that addresses the failure, then verify again. One directly "
                    "related read/lookup is allowed; do not restart broad exploration."
                )
            else:
                message = (
                    "Hard convergence: prioritize implementing the most likely "
                    "specific fix now. Do not continue a broad read/search loop. One "
                    "directly related read or location lookup is allowed before the "
                    "edit when necessary. Do not invent a patch without a concrete "
                    "target."
                )
        elif phase == PHASE_VERIFY and not state["verification_after_change"]:
            kind = "verify_after_change"
            hint_id = VERIFY_HINT_ID
            message = (
                "The workspace has changed. Run the most relevant test, lint, compile, "
                "or build check for that change now. If it fails, use the failure to "
                "make another focused modification instead of returning to broad "
                "exploration."
            )

        return {
            "active": bool(message),
            "kind": kind,
            "hint_id": hint_id,
            "text": f"[Convergence Controller]\n- {message}" if message else "",
            "remaining_steps": remaining_steps,
            "phase_hint": state["phase_hint"],
            "current_phase": phase,
            "convergence_trigger_count": state["convergence_trigger_count"],
        }

    def mark_prompt_injected(self, task_state, hint_id):
        hint_id = str(hint_id or "")
        state = task_state.runtime_progress
        if not hint_id:
            return None
        if hint_id == SOFT_HINT_ID:
            if not state["soft_convergence_hint_pending"]:
                return None
            state["soft_convergence_hint_pending"] = False
            state["soft_convergence_hint_injected"] = True
        return {
            "event": "convergence_hint_injected",
            "hint_id": hint_id,
            "kind": {
                SOFT_HINT_ID: "soft_convergence",
                HARD_HINT_ID: "hard_convergence",
                VERIFY_HINT_ID: "verify_after_change",
            }.get(hint_id, ""),
            "step": int(task_state.tool_steps),
            "current_phase": state["current_phase"],
        }

    def recent_sources(self, current_step):
        current_step = int(current_step)
        cooldown = max(0, int(self.config.relevant_memory_source_cooldown_steps))
        return {
            path
            for path, read in self._recent_reads.items()
            if current_step - int(read["step"]) < cooldown
        }

    def _record_successful_read(self, task_state, args, step):
        path = str(args.get("path", "")).strip()
        if not path:
            return
        previous = self._recent_reads.get(path)
        window = max(0, int(self.config.relevant_memory_source_cooldown_steps))
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


def convergence_report_fields(state):
    """Return the stable controller evidence contract for reports/tool traces."""

    state = dict(state or {})
    return {
        "convergence_trigger_count": state.get("convergence_trigger_count"),
        "first_convergence_step": state.get("first_convergence_step"),
        "hard_convergence_triggered": state.get("hard_convergence_triggered"),
        "hard_convergence_step": state.get("hard_convergence_step"),
        "phase_transitions": list(state.get("phase_transitions") or []),
        "steps_since_last_change": state.get("steps_since_last_change"),
        "steps_since_last_change_peak": state.get("steps_since_last_change_peak"),
        "first_change_step": state.get("first_change_step"),
        "has_changed_workspace": state.get("has_changed_workspace"),
        "verification_after_change": state.get("verification_after_change"),
        "current_phase": state.get("current_phase"),
        "patch_file_calls": state.get("patch_file_calls"),
        "write_file_calls": state.get("write_file_calls"),
        "max_consecutive_explore_steps": state.get("max_consecutive_explore_steps"),
        "verification_failure_count": state.get("verification_failure_count"),
        "convergence_controller_errors": list(
            state.get("convergence_controller_errors") or []
        ),
    }


def record_convergence_controller_error(task_state, *, stage, error):
    payload = {
        "stage": str(stage),
        "error_type": type(error).__name__,
        "error_message": str(error),
        "step": int(task_state.tool_steps),
    }
    task_state.runtime_progress.setdefault("convergence_controller_errors", []).append(
        payload
    )
    return payload


def _inactive_prompt_context(max_steps):
    return {
        "active": False,
        "kind": "",
        "hint_id": "",
        "text": "",
        "remaining_steps": int(max_steps),
        "phase_hint": "",
        "current_phase": "",
        "convergence_trigger_count": 0,
    }


def _legacy_phase_hint(phase):
    return {
        PHASE_EXPLORE: "explore",
        PHASE_CONVERGE: "converge",
        PHASE_MODIFY: "edit",
        PHASE_VERIFY: "verify/finalize",
    }[phase]
