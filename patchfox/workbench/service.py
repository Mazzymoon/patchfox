"""Single-user scheduler: SQLite queue -> isolated checkout -> existing CLI."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import WorkbenchLock, capture_diff, read_json, runtime_evidence
from .store import TaskStore, now


@dataclass
class Settings:
    data_dir: Path
    repositories: list[Path]
    test_argv: list[str] = field(default_factory=list)
    demo: bool = False
    provider: str | None = None
    model: str | None = None
    timeout: int = 900
    test_timeout: int = 120


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def terminate_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


class WorkbenchService:
    def __init__(self, settings):
        self.settings = settings
        self.store = TaskStore(settings.data_dir)
        self.lock = WorkbenchLock(self.store.root)
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.lock.acquire()
        try:
            self.store.recover()
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._loop, name="patchfox-workbench", daemon=True
            )
            self.thread.start()
        except Exception:
            self.lock.release()
            raise

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)
        # The scheduler releases the lock in its finally block, never while it is active.

    def task_dir(self, task_id):
        return self.store.root / "tasks" / task_id

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                task = self.store.claim()
                if task:
                    self.execute(task)
                else:
                    self.stop_event.wait(0.25)
        finally:
            self.lock.release()

    def command(self, argv, cwd, log_path, timeout, *, runtime=False):
        started = time.monotonic()
        env = dict(os.environ)
        env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        # Verification code does not need provider credentials.
        if not runtime:
            from patchfox.cli import DEFAULT_SECRET_ENV_NAMES

            for key in list(env):
                if key.upper() in DEFAULT_SECRET_ENV_NAMES or any(
                    word in key.upper()
                    for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
                ):
                    env.pop(key, None)
        env["PATH"] = (
            str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        )
        options = (
            {
                "creationflags": subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
            }
            if os.name == "nt"
            else {"start_new_session": True}
        )
        result = {
            "argv": argv,
            "started_at": now(),
            "status": "error",
            "exit_code": None,
        }
        process = None
        try:
            with Path(log_path).open("wb") as log:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    **options,
                )
                while process.poll() is None:
                    if self.stop_event.wait(0.1):
                        terminate_tree(process)
                        result["status"] = "interrupted"
                        break
                    if time.monotonic() - started > timeout:
                        terminate_tree(process)
                        result["status"] = "timed_out"
                        break
                else:
                    result["status"] = "passed" if process.returncode == 0 else "failed"
                result["exit_code"] = process.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            result["error"] = str(exc)
        finally:
            if process is not None and process.poll() is None:
                terminate_tree(process)
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    def agent_command(self, task, workspace, prompt):
        module = "patchfox.workbench.demo" if task["mode"] == "demo" else "patchfox"
        args = [
            sys.executable,
            "-m",
            module,
            "--cwd",
            str(workspace),
            "--prompt-file",
            str(prompt),
            "--session-id",
            "wb_" + task["id"],
            "--max-steps",
            str(task["max_steps"]),
        ]
        if task["mode"] != "demo":
            args += [
                "--non-interactive",
                "--approval",
                "auto",
                "--no-auto-dream",
                "--sandbox",
                "best_effort",
            ]
            for option in ("provider", "model"):
                if task[option]:
                    args += ["--" + option, task[option]]
        return args

    def execute(self, task):
        root = self.task_dir(task["id"])
        workspace = root / "workspace"
        status, error, stop_reason, run_id = "failed", "", "", ""
        test_status, exit_code = "not_run", None
        tests = {"before": {"status": "not_run"}, "after": {"status": "not_run"}}
        try:
            root.mkdir(parents=True, exist_ok=False)
            write_json(root / "request.json", task)
            prompt = root / "prompt.txt"
            prompt.write_text(task["prompt"], encoding="utf-8")
            setup = self.command(
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--",
                    task["repository"],
                    str(workspace),
                ],
                root,
                root / "setup.log",
                60,
            )
            if setup["status"] != "passed":
                raise RuntimeError("创建任务副本失败，请查看 setup 日志。")
            checkout = self.command(
                ["git", "checkout", "--detach", task["base_commit"]],
                workspace,
                root / "checkout.log",
                60,
            )
            if checkout["status"] != "passed":
                raise RuntimeError("无法检出提交基线，请重新提交任务。")
            # Keep runtime files out of Git diff even if the source .gitignore lacks this entry.
            with (workspace / ".git" / "info" / "exclude").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("\n.patchfox/\n")
            argv = list(task["test_argv"])
            if argv and argv[0] in {"python", "python3"}:
                argv[0] = sys.executable
            if argv:
                self.store.update(task["id"], phase="baseline")
                tests["before"] = self.command(
                    argv,
                    workspace,
                    root / "tests-before.log",
                    self.settings.test_timeout,
                )
                write_json(root / "tests.json", tests)
            if self.stop_event.is_set():
                raise InterruptedError("工作台正在停止。")
            self.store.update(task["id"], phase="agent")
            outcome = self.command(
                self.agent_command(task, workspace, prompt),
                workspace,
                root / "agent.log",
                self.settings.timeout,
                runtime=True,
            )
            write_json(root / "execution.json", outcome)
            exit_code = outcome["exit_code"]
            run_id, state, report = runtime_evidence(root, task["id"])
            stop_reason = report.get("stop_reason") or state.get("stop_reason", "")
            if outcome["status"] == "interrupted":
                raise InterruptedError("工作台停止，任务已中断，已有产物保留。")
            if outcome["status"] != "passed":
                raise RuntimeError(
                    outcome.get("error") or "Agent 执行失败或超时，请查看执行日志。"
                )
            if not report:
                raise RuntimeError(
                    "CLI 已退出，但没有本任务的 Runtime 报告，不能判定成功。"
                )
            if argv:
                self.store.update(task["id"], phase="verification", run_id=run_id)
                tests["after"] = self.command(
                    argv,
                    workspace,
                    root / "tests-after.log",
                    self.settings.test_timeout,
                )
                test_status = tests["after"]["status"]
                write_json(root / "tests.json", tests)
            if self.stop_event.is_set():
                raise InterruptedError("验收期间服务停止，已有产物保留。")
            if report.get("status") == "failed" or stop_reason == "model_error":
                status, error = "failed", "Runtime 报告执行失败：" + stop_reason
            elif (
                report.get("status") != "completed"
                or stop_reason != "final_answer_returned"
            ):
                status, error = "stopped", "Agent 提前停止：" + stop_reason
            elif argv and test_status != "passed":
                status, error = "failed", "Agent 已返回，独立验收命令未通过。"
            else:
                status = "succeeded"
        except InterruptedError as exc:
            status, error = "interrupted", str(exc)
        except Exception as exc:  # noqa: BLE001 - persist failures at the task boundary
            status, error = "failed", str(exc)
        finally:
            if (workspace / ".git").exists():
                try:
                    patch, changed = capture_diff(workspace, task["base_commit"])
                    (root / "patch.diff").write_text(patch, encoding="utf-8")
                    write_json(root / "changes.json", {"paths": changed})
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    error += " Diff 导出失败：" + str(exc)
                    if status == "succeeded":
                        status = "failed"
            if root.is_dir():
                write_json(root / "tests.json", tests)
            self.store.update(
                task["id"],
                status=status,
                phase="finished",
                finished_at=now(),
                run_id=run_id or None,
                stop_reason=stop_reason or None,
                error=error or None,
                agent_exit_code=exit_code,
                test_status=test_status,
            )

    def detail(self, task_id):
        task = self.store.get(task_id)
        if task is None:
            return None
        root = self.task_dir(task_id)
        run_id, state, report = runtime_evidence(root, task_id)
        task.update(
            artifact_dir=str(root),
            workspace=str(root / "workspace"),
            session_id="wb_" + task_id,
            run_id=run_id or task["run_id"],
            runtime={
                "status": state.get("status"),
                "tool_steps": state.get("tool_steps", 0),
                "last_tool": state.get("last_tool", ""),
                "stop_reason": report.get("stop_reason")
                or state.get("stop_reason", ""),
                "final_answer": report.get("final_answer")
                or state.get("final_answer", ""),
            },
            tests=read_json(root, "tests.json"),
            changed_paths=read_json(root, "changes.json").get("paths", []),
        )
        return task
