"""Cross-platform execution for benchmark verifier commands."""

import re
import shlex
import subprocess
import sys

from ..shell import resolve_shell_backend


def run_verifier(command, *, cwd, timeout=120):
    """Run a verifier without assuming that ``python3`` exists on Windows."""

    try:
        tokens = shlex.split(str(command), posix=True)
    except ValueError:
        tokens = []
    if tokens and tokens[0] in {"python", "python3"}:
        return subprocess.run(
            [sys.executable, *tokens[1:]],
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    backend = resolve_shell_backend()
    command = _replace_windows_python3(str(command), backend)
    return backend.run(command, cwd=cwd, env=None, timeout=timeout)


def _replace_windows_python3(command, backend):
    if not sys.platform.startswith("win") or not re.search(r"\bpython3(?=\s)", command):
        return command
    if backend.name == "bash":
        executable = shlex.quote(sys.executable)
    elif backend.name == "powershell":
        executable = "& '" + sys.executable.replace("'", "''") + "'"
    else:
        executable = subprocess.list2cmdline([sys.executable])
    return re.sub(r"\bpython3(?=\s)", lambda _match: executable, command)
