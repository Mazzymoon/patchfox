"""Workbench acceptance: actual subprocess/runtime, persistent queue and evidence."""

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from patchfox.workbench.app import create_app
from patchfox.workbench.artifacts import (
    WorkbenchLock,
    capture_diff,
    git,
    repository_head,
)
from patchfox.workbench.demo import DEMO_PROMPT, create_demo
from patchfox.workbench.service import Settings, WorkbenchService
from patchfox.workbench.store import TaskStore


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHFOX_HOME", str(tmp_path / "config"))
    return Settings(
        data_dir=tmp_path / "data",
        repositories=[create_demo(tmp_path / "source")],
        test_argv=["python", "-m", "unittest", "-v"],
        demo=True,
        timeout=30,
    )


def submit(client, **overrides):
    payload = {
        "repository_id": 0,
        "title": "Shipping boundary",
        "prompt": DEMO_PROMPT,
        "max_steps": 10,
    }
    payload.update(overrides)
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def wait_done(client, task_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get("/api/tasks/" + task_id).json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(0.1)
    pytest.fail(f"Task did not finish: {task}")


def test_offline_task_runs_actual_runtime_with_before_after_tests(settings):
    source = settings.repositories[0]
    original = (source / "pricing.py").read_text()
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        submitted = submit(client)
        task = wait_done(client, submitted["id"])
        assert task["status"] == "succeeded", task
        assert task["tests"]["before"]["exit_code"] == 1
        assert task["tests"]["after"]["exit_code"] == 0
        assert task["test_status"] == "passed"
        assert task["changed_paths"] == ["pricing.py"]
        assert task["runtime"]["tool_steps"] == 2
        assert task["runtime"]["stop_reason"] == "final_answer_returned"
        diff = client.get(f"/api/tasks/{task['id']}/artifacts/diff").text
        assert "-    return 0 if total > 100 else 10" in diff
        assert "+    return 0 if total >= 100 else 10" in diff
        assert ".patchfox" not in diff
        assert "FAILED" in client.get(f"/api/tasks/{task['id']}/artifacts/before").text
        assert "OK" in client.get(f"/api/tasks/{task['id']}/artifacts/after").text
        evidence = Path(task["workspace"]) / ".patchfox" / "runs" / task["run_id"]
        assert (evidence / "report.json").is_file()
        trace = [
            json.loads(line)
            for line in (evidence / "trace.jsonl").read_text().splitlines()
        ]
        assert any(
            event.get("event") == "tool_executed" and event.get("name") == "patch_file"
            for event in trace
        )
    assert (source / "pricing.py").read_text() == original
    assert not git(source, "status", "--porcelain").stdout.strip()
    # A new app instance reopens SQLite and finds the same completed result.
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        assert client.get("/api/tasks/" + task["id"]).json()["status"] == "succeeded"


def add_task(service, settings):
    return service.store.create(
        title="test",
        prompt=DEMO_PROMPT,
        repository=settings.repositories[0],
        base_commit=repository_head(settings.repositories[0]),
        max_steps=10,
        test_argv=settings.test_argv,
        mode="demo",
    )


def test_claim_is_atomic_and_restart_does_not_replay_running_task(settings):
    service = WorkbenchService(settings)
    first = add_task(service, settings)
    second = add_task(service, settings)
    with ThreadPoolExecutor(max_workers=4) as executor:
        claimed = list(executor.map(lambda _: service.store.claim(), range(4)))
    assert sorted(task["id"] for task in claimed if task) == sorted(
        [first["id"], second["id"]]
    )
    pending = add_task(service, settings)
    service.store.recover()
    assert service.store.get(first["id"])["status"] == "interrupted"
    assert service.store.get(second["id"])["status"] == "interrupted"
    assert service.store.get(pending["id"])["status"] == "queued"


def test_service_lock_prevents_second_scheduler(settings):
    TaskStore(settings.data_dir)
    first, second = WorkbenchLock(settings.data_dir), WorkbenchLock(settings.data_dir)
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_diff_includes_staged_unstaged_new_and_deleted_files(settings):
    repo = settings.repositories[0]
    base = repository_head(repo)
    (repo / "pricing.py").write_text("# staged change\n", encoding="utf-8")
    git(repo, "add", "pricing.py")
    (repo / "pricing.py").write_text("# staged and unstaged\n", encoding="utf-8")
    (repo / "new.txt").write_text("new file\n", encoding="utf-8")
    (repo / "test_pricing.py").unlink()
    (repo / ".patchfox").mkdir()
    (repo / ".patchfox" / "private.json").write_text("{}")
    patch, changed = capture_diff(repo, base)
    assert "staged and unstaged" in patch
    assert "new file" in patch
    assert "deleted file mode" in patch
    assert sorted(changed) == ["new.txt", "pricing.py", "test_pricing.py"]
    assert "private.json" not in patch


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        ("failed", "model_error", "failed"),
        ("stopped", "step_limit_reached", "stopped"),
        (
            "completed",
            "final_answer_returned",
            "failed",
        ),  # unchanged bug: independent tests fail
    ],
)
def test_exit_zero_does_not_override_runtime_or_verification_failure(
    settings, monkeypatch, status, reason, expected
):
    service = WorkbenchService(settings)
    task = add_task(service, settings)
    task = service.store.claim()
    real_command = service.command

    def command(argv, cwd, log, timeout, *, runtime=False):
        if not runtime:
            return real_command(argv, cwd, log, timeout, runtime=runtime)
        root = Path(cwd) / ".patchfox"
        session = root / "sessions"
        run = root / "runs" / "main"
        session.mkdir(parents=True)
        run.mkdir(parents=True)
        (session / f"wb_{task['id']}.events.jsonl").write_text(
            json.dumps({"event": "turn_started", "run_id": "main"}) + "\n"
        )
        (run / "report.json").write_text(
            json.dumps({"status": status, "stop_reason": reason})
        )
        # An unrelated worker report must never be mistaken for the main run.
        (root / "runs" / "worker").mkdir()
        (root / "runs" / "worker" / "report.json").write_text(
            json.dumps({"status": "completed", "stop_reason": "final_answer_returned"})
        )
        return {"status": "passed", "exit_code": 0}

    monkeypatch.setattr(service, "command", command)
    service.execute(task)
    result = service.detail(task["id"])
    assert result["status"] == expected
    assert result["run_id"] == "main"
    assert result["stop_reason"] == reason


def test_missing_report_is_not_success_and_process_timeout_is_recorded(
    settings, monkeypatch
):
    service = WorkbenchService(settings)
    add_task(service, settings)
    task = service.store.claim()
    monkeypatch.setattr(
        service,
        "agent_command",
        lambda *args: [sys.executable, "-c", "print('no report')"],
    )
    service.execute(task)
    assert service.store.get(task["id"])["status"] == "failed"
    assert "没有本任务" in service.store.get(task["id"])["error"]
    result = service.command(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        settings.data_dir,
        settings.data_dir / "timeout.log",
        0.2,
    )
    assert result["status"] == "timed_out"


def test_request_validation_origin_and_repository_boundaries(settings):
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        payload = {"repository_id": 0, "title": "test", "prompt": "fix"}
        assert (
            client.post(
                "/api/tasks", json=payload, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.get("/api/tasks", headers={"Host": "evil.example"}).status_code
            == 400
        )
        assert (
            client.post(
                "/api/tasks",
                content=json.dumps(payload),
                headers={"Content-Type": "text/plain"},
            ).status_code
            == 415
        )
        assert (
            client.post("/api/tasks", json={**payload, "repository_id": 99}).status_code
            == 400
        )
        assert (
            client.post("/api/tasks", json={**payload, "prompt": " "}).status_code
            == 422
        )
        assert (
            client.post("/api/tasks", json={**payload, "repository": "C:/"}).status_code
            == 422
        )
        assert client.get("/api/tasks/unknown").status_code == 404
        (settings.repositories[0] / "pricing.py").write_text("local uncommitted work")
        response = client.post("/api/tasks", json=payload)
        assert response.status_code == 409
        assert "未提交" in response.json()["detail"]


def test_live_command_calls_existing_cli_with_no_credentials_in_args(settings):
    service = WorkbenchService(settings)
    task = add_task(service, settings)
    task.update(mode="live", provider="deepseek", model="configured-model")
    command = service.agent_command(task, Path("workspace"), Path("prompt.txt"))
    assert command[:3] == [sys.executable, "-m", "patchfox"]
    assert command[command.index("--approval") + 1] == "auto"
    assert command[command.index("--session-id") + 1] == "wb_" + task["id"]
    assert "--non-interactive" in command
    assert "--api-key" not in command


def test_real_cli_path_works_with_local_provider_endpoint(settings, monkeypatch):
    """Exercise the production CLI, configuration and HTTP provider adapter offline."""
    outputs = iter(
        [
            '<tool>{"name":"read_file","args":{"path":"pricing.py"}}</tool>',
            '<tool>{"name":"patch_file","args":{"path":"pricing.py","old_text":"total > 100","new_text":"total >= 100"}}</tool>',
            "<final>Fixed shipping threshold.</final>",
        ]
    )
    seen = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, payload["model"]))
            data = json.dumps({"output_text": next(outputs)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("PATCHFOX_PROVIDER", "openai")
        monkeypatch.setenv("PATCHFOX_MODEL", "workbench-local-test")
        monkeypatch.setenv("PATCHFOX_API_KEY", "dummy-local-test-key")
        monkeypatch.setenv(
            "PATCHFOX_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1"
        )
        settings.demo = False
        settings.provider = "openai"
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            task = wait_done(client, submit(client)["id"])
            assert task["status"] == "succeeded", task
            assert task["test_status"] == "passed"
            assert task["runtime"]["tool_steps"] == 2
            assert task["mode"] == "live"
        assert seen == [("/v1/responses", "workbench-local-test")] * 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
