from pathlib import Path
from types import SimpleNamespace

import pytest

from patchfox.config import resolve_project_shell_config
from patchfox.shell import (
    LocalShellBackend,
    ShellConfig,
    resolve_shell_backend,
    resolve_shell_config,
)


def test_windows_auto_prefers_git_bash_without_wsl(tmp_path):
    git_root = tmp_path / "Git"
    git = git_root / "cmd" / "git.exe"
    bash = git_root / "bin" / "bash.exe"
    git.parent.mkdir(parents=True)
    bash.parent.mkdir(parents=True)
    git.write_bytes(b"")
    bash.write_bytes(b"")

    def fake_which(name):
        return str(git) if name == "git" else None

    backend = resolve_shell_backend(
        "auto", platform_name="win32", which=fake_which, environ={}
    )

    assert backend.name == "bash"
    assert Path(backend.executable) == bash
    assert backend.argv("printf '%s' ok") == [str(bash), "-lc", "printf '%s' ok"]


def test_windows_auto_ignores_wsl_launcher_and_falls_back_to_powershell(tmp_path):
    windows = tmp_path / "Windows"
    wsl_bash = windows / "System32" / "bash.exe"
    pwsh = tmp_path / "pwsh.exe"

    def fake_which(name):
        return {
            "bash": str(wsl_bash),
            "pwsh": str(pwsh),
        }.get(name)

    backend = resolve_shell_backend(
        "auto",
        platform_name="win32",
        which=fake_which,
        environ={"WINDIR": str(windows)},
    )

    assert backend.name == "powershell"
    assert backend.executable == str(pwsh)


def test_local_shell_executes_an_argv_without_implicit_shell():
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    backend = LocalShellBackend("powershell", "pwsh", run_process=fake_run)
    result = backend.run("Get-Location", cwd="repo", env={"PATH": "bin"}, timeout=7)

    assert result.stdout == "ok\n"
    assert captured["argv"] == [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-Location",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 7


def test_shell_config_validation_and_project_override(tmp_path):
    assert resolve_shell_config({}) == ShellConfig()
    with pytest.raises(ValueError, match="shell.backend"):
        resolve_shell_config({"shell": {"backend": "wsl"}})

    (tmp_path / ".patchfox.toml").write_text(
        "[shell]\nbackend = 'powershell'\n", encoding="utf-8"
    )
    assert resolve_project_shell_config(start=tmp_path).backend == "powershell"
    assert resolve_project_shell_config(start=tmp_path, backend="cmd").backend == "cmd"


def test_prompt_hint_documents_the_selected_dialect():
    assert "POSIX" in LocalShellBackend("bash", "bash").prompt_hint
    assert "PowerShell" in LocalShellBackend("powershell", "pwsh").prompt_hint
    assert "CMD" in LocalShellBackend("cmd", "cmd.exe").prompt_hint

