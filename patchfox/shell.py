"""Local shell capability used by ``run_shell``.

The agent loop depends on this small interface instead of Python's implicit
``shell=True`` selection.  That keeps command dialect explicit and lets a
Windows installation use Git Bash, PowerShell, or CMD without requiring WSL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SHELL_BACKENDS = {"auto", "bash", "powershell", "cmd"}
_PROCESS_PATH = os.environ.get("PATH", "")


class ShellBackend(Protocol):
    """Capability boundary for executing one local shell command."""

    @property
    def name(self) -> str: ...

    @property
    def executable(self) -> str: ...

    @property
    def prompt_hint(self) -> str: ...

    def run(self, command: str, *, cwd, env, timeout: int): ...


@dataclass(frozen=True)
class ShellConfig:
    backend: str = "auto"


class LocalShellBackend:
    """Run commands through one explicit local shell executable."""

    def __init__(
        self,
        name: str,
        executable: str,
        *,
        run_process: Callable | None = None,
    ):
        if name not in SHELL_BACKENDS - {"auto"}:
            raise ValueError(f"unsupported shell backend: {name}")
        self._name = name
        self._executable = str(executable)
        self._run_process = run_process

    @property
    def name(self) -> str:
        return self._name

    @property
    def executable(self) -> str:
        return self._executable

    @property
    def prompt_hint(self) -> str:
        if self.name == "bash":
            return (
                "Shell backend: Bash (POSIX syntax). Use POSIX quoting and tools; "
                "this can be Git Bash on Windows and does not imply WSL."
            )
        if self.name == "powershell":
            return (
                "Shell backend: PowerShell. Use PowerShell quoting and cmdlets; "
                "do not assume POSIX utilities such as head or tail exist."
            )
        return (
            "Shell backend: Windows CMD. Use cmd.exe quoting and built-ins; "
            "do not emit Bash or PowerShell syntax."
        )

    def argv(self, command: str) -> list[str]:
        if self.name == "bash":
            return [self.executable, "-lc", command]
        if self.name == "powershell":
            return [
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ]
        return [self.executable, "/d", "/s", "/c", command]

    def run(self, command: str, *, cwd, env, timeout: int):
        runner = self._run_process or subprocess.run
        return runner(
            self.argv(str(command)),
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def metadata(self) -> dict[str, str]:
        return {"name": self.name, "executable": self.executable}


def resolve_shell_config(values) -> ShellConfig:
    shell = dict((values or {}).get("shell", {}) or {})
    backend = str(shell.get("backend", "auto") or "auto").strip().lower()
    if backend not in SHELL_BACKENDS:
        raise ValueError(f"shell.backend must be one of {sorted(SHELL_BACKENDS)}")
    return ShellConfig(backend=backend)


def resolve_shell_backend(
    preferred: str = "auto",
    *,
    platform_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
    run_process: Callable | None = None,
) -> LocalShellBackend:
    """Resolve an explicit shell, preferring Git Bash on Windows in auto mode."""

    backend = str(preferred or "auto").strip().lower()
    if backend not in SHELL_BACKENDS:
        raise ValueError(f"shell backend must be one of {sorted(SHELL_BACKENDS)}")
    platform_name = platform_name or sys.platform
    which = which or shutil.which
    environ = os.environ if environ is None else environ
    use_process_path = environ is os.environ
    is_windows = platform_name.startswith("win")

    if backend == "auto":
        candidates = (
            (
                "bash",
                _find_bash(
                    which,
                    environ,
                    windows=True,
                    use_process_path=use_process_path,
                ),
            ),
            ("powershell", which("pwsh") or which("powershell")),
            ("cmd", _find_cmd(which, environ)),
        ) if is_windows else (
            ("bash", which("bash") or which("sh")),
        )
        for name, executable in candidates:
            if executable:
                return LocalShellBackend(name, executable, run_process=run_process)
        raise RuntimeError("no supported local shell is available")

    if backend == "bash":
        executable = _find_bash(
            which,
            environ,
            windows=is_windows,
            use_process_path=use_process_path,
        )
    elif backend == "powershell":
        executable = which("pwsh") or which("powershell")
    else:
        executable = _find_cmd(which, environ)
    if not executable:
        raise RuntimeError(f"requested shell backend is unavailable: {backend}")
    return LocalShellBackend(backend, executable, run_process=run_process)


def _find_bash(
    which,
    environ: Mapping[str, str],
    *,
    windows: bool,
    use_process_path: bool,
) -> str | None:
    explicit = str(environ.get("PATCHFOX_BASH_PATH", "") or "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    if windows:
        git = which("git")
        if not git and use_process_path:
            git = shutil.which("git", path=_PROCESS_PATH)
        if git:
            git_root = Path(git).resolve().parent.parent
            for relative in ("bin/bash.exe", "usr/bin/bash.exe"):
                candidate = git_root / relative
                if candidate.is_file():
                    return str(candidate)
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            base = environ.get(variable)
            if not base:
                continue
            candidate = Path(base) / "Git" / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)

    executable = which("bash") or (None if windows else which("sh"))
    if executable and not _is_windows_wsl_launcher(executable, environ):
        return executable
    return None


def _is_windows_wsl_launcher(executable: str, environ: Mapping[str, str]) -> bool:
    windows_dir = environ.get("WINDIR") or environ.get("SystemRoot")
    if not windows_dir:
        return False
    try:
        return Path(executable).resolve() == (Path(windows_dir) / "System32" / "bash.exe").resolve()
    except OSError:
        return False


def _find_cmd(which, environ: Mapping[str, str]) -> str | None:
    configured = environ.get("ComSpec") or which("cmd")
    if configured:
        return configured
    roots = {
        Path(sys.executable).anchor,
        Path(os.__file__).anchor,
    }
    for drive_root in filter(None, roots):
        candidate = Path(drive_root) / "Windows" / "System32" / "cmd.exe"
        if candidate.is_file():
            return str(candidate)
    return None
