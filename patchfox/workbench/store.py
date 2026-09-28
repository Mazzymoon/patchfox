"""SQLite is the task index/queue; runtime evidence stays in files."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
                repository TEXT NOT NULL, base_commit TEXT NOT NULL,
                max_steps INTEGER NOT NULL, test_argv TEXT NOT NULL,
                mode TEXT NOT NULL, provider TEXT, model TEXT,
                status TEXT NOT NULL, phase TEXT NOT NULL,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                run_id TEXT, stop_reason TEXT, error TEXT,
                agent_exit_code INTEGER, test_status TEXT NOT NULL DEFAULT 'not_run'
            )""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        data = dict(row)
        data["test_argv"] = json.loads(data["test_argv"])
        return data

    def create(
        self,
        *,
        title,
        prompt,
        repository,
        base_commit,
        max_steps,
        test_argv,
        mode,
        provider=None,
        model=None,
    ):
        task_id = uuid4().hex
        with self.connect() as db:
            db.execute(
                """INSERT INTO tasks
                (id,title,prompt,repository,base_commit,max_steps,test_argv,mode,
                 provider,model,status,phase,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'queued','queued',?)""",
                (
                    task_id,
                    title,
                    prompt,
                    str(repository),
                    base_commit,
                    max_steps,
                    json.dumps(test_argv),
                    mode,
                    provider,
                    model,
                    now(),
                ),
            )
        return self.get(task_id)

    def get(self, task_id):
        with self.connect() as db:
            return self.decode(
                db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )

    def list(self):
        with self.connect() as db:
            return [
                self.decode(row)
                for row in db.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100"
                )
            ]

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE tasks SET status='running', phase='preparing', started_at=? WHERE id=?",
                (now(), row["id"]),
            )
        return self.get(row["id"])

    def update(self, task_id, **fields):
        allowed = {
            "status",
            "phase",
            "finished_at",
            "run_id",
            "stop_reason",
            "error",
            "agent_exit_code",
            "test_status",
        }
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid task update")
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET "
                + ",".join(f"{key}=?" for key in fields)
                + " WHERE id=?",
                (*fields.values(), task_id),
            )

    def recover(self):
        # Only called while holding the process-wide workbench lock.
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET status='interrupted', phase='finished', finished_at=?,
                error='服务中断；保留已有产物，请检查后重新提交。'
                WHERE status='running'""",
                (now(),),
            )
