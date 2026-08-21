"""User-scoped PatchFox configuration and credential storage.

Provider endpoints are trusted user configuration.  Project repositories may
select a configured provider and model, but they never get to supply an
endpoint or a credential.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if os.sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 dependency
    import tomli as tomllib  # type: ignore[no-redef]


CONFIG_HOME_ENV = "PATCHFOX_HOME"
CONFIG_VERSION = 1
_USER_HOME = Path.home()


@dataclass(frozen=True)
class ConfigPaths:
    home: Path
    config: Path
    auth: Path
    projects: Path
    legacy_config: Path


def config_paths() -> ConfigPaths:
    configured = os.environ.get(CONFIG_HOME_ENV, "").strip()
    home = (
        Path(configured).expanduser()
        if configured
        else _USER_HOME / ".patchfox"
    )
    return ConfigPaths(
        home=home,
        config=home / "config.toml",
        auth=home / "auth.json",
        projects=home / "projects.json",
        legacy_config=_USER_HOME / ".config" / "patchfox" / "config.toml",
    )


def normalize_project_path(path: str | Path) -> str:
    """Return a stable project key, including case folding on Windows."""

    resolved = os.path.abspath(os.path.realpath(os.fspath(Path(path).expanduser())))
    return os.path.normcase(os.path.normpath(resolved))


def load_global_config() -> dict[str, Any]:
    path = config_paths().config
    if not path.exists():
        return {"version": CONFIG_VERSION, "default_provider": "", "providers": {}}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid PatchFox global config {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"could not read PatchFox global config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid PatchFox global config {path}: expected a table")
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError(f"invalid PatchFox global config {path}: providers must be a table")
    return {
        "version": int(data.get("version", CONFIG_VERSION)),
        "default_provider": str(
            data.get("default_provider", data.get("provider", "")) or ""
        ),
        "providers": {
            str(name).strip().lower(): dict(section)
            for name, section in providers.items()
            if isinstance(section, dict)
        },
    }


def load_auth() -> dict[str, Any]:
    return _load_json(
        config_paths().auth,
        {"version": CONFIG_VERSION, "providers": {}},
        "credential file",
    )


def load_projects() -> dict[str, Any]:
    return _load_json(
        config_paths().projects,
        {"version": CONFIG_VERSION, "projects": {}},
        "project selection file",
    )


def provider_api_key(provider: str) -> str:
    auth = load_auth()
    providers = auth.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError(
            f"invalid PatchFox credential file {config_paths().auth}: providers must be an object"
        )
    entry = providers.get(str(provider).strip().lower(), {})
    return str(entry.get("api_key", "") or "") if isinstance(entry, dict) else ""


def project_selection(path: str | Path) -> dict[str, str]:
    data = load_projects()
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError(
            f"invalid PatchFox project selection file {config_paths().projects}: projects must be an object"
        )
    selection = projects.get(normalize_project_path(path), {})
    if not isinstance(selection, dict):
        return {}
    return {
        key: str(selection.get(key, "") or "")
        for key in ("provider", "model")
        if selection.get(key)
    }


def save_project_selection(
    path: str | Path, provider: str, model: str
) -> Path:
    data = load_projects()
    data["version"] = CONFIG_VERSION
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("projects.json contains an invalid projects value")
    projects[normalize_project_path(path)] = {
        "provider": str(provider).strip().lower(),
        "model": str(model).strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return _atomic_write_json(config_paths().projects, data, private=False)


def save_provider(
    provider: str,
    *,
    protocol: str,
    base_url: str,
    model: str,
    api_key: str,
    make_default: bool = True,
) -> tuple[Path, Path]:
    name = str(provider).strip().lower()
    if not name:
        raise ValueError("provider name cannot be empty")
    protocol = str(protocol).strip().lower()
    if protocol not in {"openai", "anthropic"}:
        raise ValueError("protocol must be 'openai' or 'anthropic'")
    if not str(base_url).strip():
        raise ValueError("base URL cannot be empty")
    if not str(model).strip():
        raise ValueError("model cannot be empty")
    if not str(api_key).strip():
        raise ValueError("API key cannot be empty")

    config = load_global_config()
    config["version"] = CONFIG_VERSION
    providers = config.setdefault("providers", {})
    providers[name] = {
        "protocol": protocol,
        "base_url": str(base_url).strip(),
        "model": str(model).strip(),
    }
    if make_default or not config.get("default_provider"):
        config["default_provider"] = name

    auth = load_auth()
    auth["version"] = CONFIG_VERSION
    auth_providers = auth.setdefault("providers", {})
    if not isinstance(auth_providers, dict):
        raise ValueError("auth.json contains an invalid providers value")
    auth_providers[name] = {"api_key": str(api_key).strip()}

    config_path = _atomic_write_toml(config_paths().config, config)
    auth_path = _atomic_write_json(config_paths().auth, auth, private=True)
    return config_path, auth_path


def config_summary(start: str | Path = ".") -> dict[str, Any]:
    from . import resolve_provider_config

    resolved = resolve_provider_config(start=start)
    paths = config_paths()
    return {
        "config_home": str(paths.home),
        "global_config": str(paths.config),
        "auth_file": str(paths.auth),
        "projects_file": str(paths.projects),
        "project": normalize_project_path(start),
        "provider": resolved.name,
        "protocol": resolved.protocol,
        "base_url": resolved.base_url,
        "model": resolved.model,
        "api_key_configured": bool(resolved.api_key),
        "sources": dict(resolved.sources),
        "warnings": list(resolved.warnings),
    }


def legacy_sources(start: str | Path) -> list[Path]:
    candidates = [
        config_paths().legacy_config,
        Path(start).resolve() / ".env",
        Path(start).resolve() / ".patchfox.toml",
    ]
    return [path for path in candidates if path.exists()]


def _load_json(path: Path, default: dict[str, Any], label: str) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid PatchFox {label} {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"could not read PatchFox {label} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid PatchFox {label} {path}: expected an object")
    return data


def _atomic_write_json(path: Path, data: dict[str, Any], *, private: bool) -> Path:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return _atomic_write(path, text, private=private)


def _atomic_write_toml(path: Path, data: dict[str, Any]) -> Path:
    lines = [f"version = {int(data.get('version', CONFIG_VERSION))}"]
    default_provider = str(data.get("default_provider", "") or "")
    if default_provider:
        lines.append(f"default_provider = {_toml_string(default_provider)}")
    for name in sorted(data.get("providers", {})):
        section = data["providers"][name]
        if not isinstance(section, dict):
            continue
        lines.extend(["", f"[providers.{_toml_key(name)}]"])
        for key in (
            "protocol",
            "base_url",
            "model",
            "supports_vision",
            "vision_provider",
        ):
            if key not in section:
                continue
            value = section[key]
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            else:
                lines.append(f"{key} = {_toml_string(str(value))}")
    return _atomic_write(path, "\n".join(lines) + "\n", private=False)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    if value.replace("-", "_").isalnum():
        return value
    return _toml_string(value)


def _atomic_write(path: Path, text: str, *, private: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
        os.replace(temp_path, path)
        if private:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    return path
