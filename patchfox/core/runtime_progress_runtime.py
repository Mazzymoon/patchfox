"""Runtime boundary for the event-driven convergence controller."""

from .runtime_progress import (
    convergence_report_fields,
    record_convergence_controller_error,
)


class RuntimeProgressRuntimeMixin:
    """Connect RuntimeProgress to trace/persistence without growing Agent Loop."""

    def start_runtime_progress(self, task_state):
        try:
            self.runtime_progress.start_turn(task_state)
        except Exception as exc:  # noqa: BLE001 - controller must fail open with evidence.
            self._record_convergence_controller_error(task_state, "start_turn", exc)

    def record_runtime_progress_after_tool(self, task_state, name, args, metadata):
        progress_args = dict(args or {})
        if name == "read_file" and progress_args.get("path"):
            progress_args["path"] = self.memory.canonical_path(progress_args["path"])
        try:
            result = self.runtime_progress.record_tool(
                task_state, name, progress_args, metadata
            )
            for event in result.get("events", []):
                event = dict(event)
                event_name = event.pop("event", "convergence_controller_event")
                self.emit_trace(task_state, event_name, event)
            return dict(result.get("snapshot") or {})
        except Exception as exc:  # noqa: BLE001 - controller must fail open with evidence.
            self._record_convergence_controller_error(task_state, "record_tool", exc)
            return convergence_report_fields(task_state.runtime_progress)

    def runtime_progress_context(self):
        try:
            return self.runtime_progress.prompt_hint(
                self.current_task_state, self.max_steps
            )
        except Exception as exc:  # noqa: BLE001 - controller must fail open with evidence.
            task_state = self.current_task_state
            if task_state is not None:
                self._record_convergence_controller_error(
                    task_state, "prompt_hint", exc
                )
            return {
                "active": False,
                "kind": "",
                "hint_id": "",
                "text": "",
                "remaining_steps": max(
                    0,
                    int(self.max_steps)
                    - int(getattr(task_state, "tool_steps", 0) or 0),
                ),
                "phase_hint": "",
                "current_phase": "",
                "convergence_trigger_count": 0,
            }

    def mark_runtime_progress_prompt_injected(self, task_state, hint_id):
        try:
            event = self.runtime_progress.mark_prompt_injected(task_state, hint_id)
            if event:
                event = dict(event)
                event_name = event.pop("event")
                self.emit_trace(task_state, event_name, event)
                self.run_store.write_task_state(task_state)
            return event
        except Exception as exc:  # noqa: BLE001 - controller must fail open with evidence.
            self._record_convergence_controller_error(
                task_state, "mark_prompt_injected", exc
            )
            return None

    def _record_convergence_controller_error(self, task_state, stage, error):
        payload = record_convergence_controller_error(
            task_state, stage=stage, error=error
        )
        try:
            self.emit_trace(task_state, "convergence_controller_error", payload)
            self.run_store.write_task_state(task_state)
        except Exception as persistence_error:  # noqa: BLE001 - retain in-memory evidence.
            payload["persistence_error"] = {
                "error_type": type(persistence_error).__name__,
                "error_message": str(persistence_error),
            }
        return payload

    def relevant_memory_excluded_sources(self):
        task_state = self.current_task_state
        if task_state is None:
            return set()
        return self.runtime_progress.recent_sources(task_state.tool_steps)
