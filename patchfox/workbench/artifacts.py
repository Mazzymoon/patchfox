"""Bounded file reads and Git snapshots for an isolated task checkout."""

import json
import os
import subprocess
from pathlib import Path

TEXT_LIMIT = 192_000


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def repository_head(repo):
    repo = Path(repo).expanduser().resolve()
    root = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != repo:
        raise ValueError("请选择 Git 仓库根目录。")
    if git(repo, "status", "--porcelain", "--untracked-files=normal").stdout.strip():
        raise ValueError(
            "原仓库有未提交修改；请先提交，工作台只从已提交的干净基线创建任务。"
        )
    if (repo / ".gitmodules").exists():
        raise ValueError("第一版暂不支持 Git submodules。")
    if git(repo, "ls-files", ".patchfox").stdout.strip():
        raise ValueError("请将 .patchfox 运行数据移出 Git 跟踪后再提交任务。")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def capture_diff(workspace, base_commit):
    raw = git(workspace, "ls-files", "--others", "--exclude-standard", "-z").stdout
    untracked = [p for p in raw.split("\0") if p and not p.startswith(".patchfox/")]
    # Only the disposable task checkout's index is changed.
    for path in untracked:
        git(workspace, "--literal-pathspecs", "add", "--intent-to-add", "--", path)
    args = [base_commit, "--", ".", ":(exclude).patchfox", ":(exclude).patchfox/**"]
    patch = git(workspace, "diff", "--binary", "--no-ext-diff", *args).stdout
    paths = git(workspace, "diff", "--name-only", "-z", *args).stdout.split("\0")
    return patch, [p for p in paths if p]


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact path escapes task directory")
    return path


def read_text(root, relative, *, tail=False):
    path = safe_path(root, relative)
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        if tail:
            handle.seek(max(0, path.stat().st_size - TEXT_LIMIT))
        data = handle.read(TEXT_LIMIT)
    value = data.decode("utf-8", errors="replace")
    if path.stat().st_size > TEXT_LIMIT:
        note = "\n[页面仅展示部分内容，完整文件保存在任务目录]\n"
        value = note + value if tail else value + note
    return value


def read_json(root, relative):
    path = safe_path(root, relative)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}  # A live runtime file may not have completed its atomic replace yet.


def runtime_evidence(root, task_id):
    relative = f"workspace/.patchfox/sessions/wb_{task_id}.events.jsonl"
    path = safe_path(root, relative)
    run_id = ""
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "turn_started":
                    run_id = event.get("run_id", "")
                    break
    if not run_id or Path(run_id).name != run_id or "/" in run_id or "\\" in run_id:
        return "", {}, {}
    prefix = f"workspace/.patchfox/runs/{run_id}"
    return (
        run_id,
        read_json(root, prefix + "/task_state.json"),
        read_json(root, prefix + "/report.json"),
    )


class WorkbenchLock:
    """OS-released lock: one scheduler owns a data directory, including after crashes."""

    def __init__(self, root):
        self.path = Path(root) / "service.lock"
        self.handle = None

    def acquire(self):
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(
                "此数据目录已有工作台运行；请勿使用多个 Uvicorn worker。"
            ) from exc

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None
