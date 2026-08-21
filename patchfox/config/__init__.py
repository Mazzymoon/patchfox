"""Project-local configuration helpers."""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..features.sandbox import resolve_sandbox_config as resolve_sandbox_values
from ..shell import ShellConfig, resolve_shell_config as resolve_shell_values
from .store import (
    config_paths,
    project_selection,
    provider_api_key,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - covered on Python 3.10 by dependency resolution
    import tomli as tomllib  # type: ignore[no-redef]


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_PROVIDER = "openai"
DEFAULT_CONFIG_PATH = config_paths().config
PROJECT_CONFIG_NAME = ".patchfox.toml"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    protocol: str
    api_key: str
    base_url: str
    model: str
    supports_vision: bool = False
    vision_provider: str = ""
    warnings: tuple[str, ...] = ()
    sources: tuple[tuple[str, str], ...] = ()


PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {
        "protocol": "openai",
        "base_url": "https://www.right.codes/codex/v1",
        "model": "gpt-5.4",
        "supports_vision": True,
    },
    "anthropic": {
        "protocol": "anthropic",
        "base_url": "https://www.right.codes/claude/v1",
        "model": "claude-sonnet-4-6",
        "supports_vision": True,
    },
    "deepseek": {
        "protocol": "anthropic",
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-pro",
        "supports_vision": False,
        "vision_provider": "openai",
    },
}

PROVIDER_ALIASES = {
    "gpt": "openai",
    "claude": "anthropic",
}

PROTOCOLS = {"openai", "anthropic"}

PROVIDER_MAX_TOKENS: dict[str, int] = {
    "openai": 8192,
    "anthropic": 32000,
    "deepseek": 8192,
}
DEFAULT_MAX_TOKENS_FALLBACK = 4096


def default_max_tokens_for_provider(provider: str | None) -> int:
    if not provider:
        return DEFAULT_MAX_TOKENS_FALLBACK
    key = PROVIDER_ALIASES.get(provider, provider)
    return PROVIDER_MAX_TOKENS.get(key, DEFAULT_MAX_TOKENS_FALLBACK)

ENV_PROVIDER = "PATCHFOX_PROVIDER"
ENV_API_KEY = "PATCHFOX_API_KEY"
ENV_BASE_URL = "PATCHFOX_BASE_URL"
ENV_MODEL = "PATCHFOX_MODEL"
ENV_VISION_PROVIDER = "PATCHFOX_VISION_PROVIDER"
ENV_VISION_API_KEY = "PATCHFOX_VISION_API_KEY"
ENV_VISION_BASE_URL = "PATCHFOX_VISION_API_BASE"
ENV_VISION_BASE_URL_ALT = "PATCHFOX_VISION_BASE_URL"
ENV_VISION_MODEL = "PATCHFOX_VISION_MODEL"
ENV_VISION_TIMEOUT = "PATCHFOX_VISION_TIMEOUT"

PROVIDER_ENV_NAMES = {
    "openai": {
        "api_key": ("OPENAI_API_KEY",),
        "base_url": ("OPENAI_API_BASE", "OPENAI_BASE_URL"),
        "model": ("OPENAI_MODEL",),
    },
    "anthropic": {
        "api_key": (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "RIGHT_CODES_API_KEY",
            "OPENAI_API_KEY",
        ),
        "base_url": ("ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL"),
        "model": ("ANTHROPIC_MODEL",),
    },
    "deepseek": {
        "api_key": ("DEEPSEEK_API_KEY",),
        "base_url": ("DEEPSEEK_API_BASE", "DEEPSEEK_BASE_URL"),
        "model": ("DEEPSEEK_MODEL",),
    },
}

LEGACY_ENV_NAMES = {
    "openai": {
        "api_key": ("PATCHFOX_OPENAI_API_KEY", "OPENAI_API_KEY"),
        "base_url": ("PATCHFOX_OPENAI_API_BASE", "OPENAI_API_BASE", "OPENAI_BASE_URL"),
        "model": ("PATCHFOX_OPENAI_MODEL", "OPENAI_MODEL"),
    },
    "anthropic": {
        "api_key": (
            "PATCHFOX_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
            "PATCHFOX_RIGHT_CODES_API_KEY",
            "RIGHT_CODES_API_KEY",
            "PATCHFOX_OPENAI_API_KEY",
            "OPENAI_API_KEY",
        ),
        "base_url": (
            "PATCHFOX_ANTHROPIC_API_BASE",
            "ANTHROPIC_API_BASE",
            "ANTHROPIC_BASE_URL",
        ),
        "model": ("PATCHFOX_ANTHROPIC_MODEL", "ANTHROPIC_MODEL"),
    },
    "deepseek": {
        "api_key": ("PATCHFOX_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
        "base_url": (
            "PATCHFOX_DEEPSEEK_API_BASE",
            "DEEPSEEK_API_BASE",
            "DEEPSEEK_BASE_URL",
        ),
        "model": ("PATCHFOX_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"),
    },
}


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    if "=" not in line:
        raise ValueError(f"invalid .env line: {line}")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise ValueError(f"invalid .env variable name: {name}")
    return name, _strip_quotes(value)


def find_project_env(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        env_path = path / ".env"
        if env_path.exists():
            return env_path
    return None


def find_project_config(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        config_path = path / PROJECT_CONFIG_NAME
        if config_path.exists():
            return config_path
    return None


def load_project_env(start, override=True):
    env_path = find_project_env(start)
    if env_path is None:
        return {}
    loaded = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
        if override or name not in os.environ:
            os.environ[name] = value
    return loaded


def provider_env(name, legacy_names=(), default=""):
    for env_name in (name, *legacy_names):
        value = os.environ.get(env_name)
        if value:
            return value
    return default


def resolve_provider_config(
    provider: str | None = None,
    *,
    start: str | Path = ".",
    config_path: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    vision_provider: str | None = None,
) -> ProviderConfig:
    file_values = _load_config_values(start=start, explicit_path=config_path)
    selection = project_selection(start)

    requested_provider = (
        provider
        or os.environ.get(ENV_PROVIDER)
        or selection.get("provider")
        or file_values["project_top"].get("provider")
        or file_values["top"].get("default_provider")
        or file_values["top"].get("provider")
        or DEFAULT_PROVIDER
    )
    provider_name = normalize_provider_name(requested_provider)
    profile_values = _profile_values(file_values["providers"], provider_name)
    default_values = dict(PROVIDER_DEFAULTS.get(provider_name, {}))
    selection_model = (
        selection.get("model", "")
        if not selection.get("provider")
        or normalize_provider_name(selection.get("provider")) == provider_name
        else ""
    )
    project_provider = file_values["project_top"].get("provider", "")
    project_model = (
        file_values["project_top"].get("model", "")
        if not project_provider
        or normalize_provider_name(project_provider) == provider_name
        else ""
    )

    protocol = _first_value(
        None,
        os.environ.get("PATCHFOX_PROTOCOL"),
        profile_values.get("protocol"),
        default_values.get("protocol"),
    )
    protocol = _validate_protocol(protocol, provider_name)

    env_values = _env_values(provider_name, protocol)
    resolved_model = _first_value(
        model,
        os.environ.get(ENV_MODEL),
        env_values.get("model"),
        selection_model,
        project_model,
        profile_values.get("model"),
        default_values.get("model"),
    )
    resolved_base_url = _first_value(
        base_url,
        os.environ.get(ENV_BASE_URL),
        env_values.get("base_url"),
        profile_values.get("base_url"),
        default_values.get("base_url"),
    )
    stored_api_key = provider_api_key(provider_name)
    resolved_api_key = _first_value(
        api_key,
        os.environ.get(ENV_API_KEY),
        env_values.get("api_key"),
        stored_api_key,
        "",
    )
    supports_vision = _bool_value(
        _first_present(
            profile_values.get("supports_vision"),
            default_values.get("supports_vision"),
            False,
        )
    )
    resolved_vision_provider = _first_value(
        vision_provider,
        os.environ.get(ENV_VISION_PROVIDER),
        profile_values.get("vision_provider"),
        default_values.get("vision_provider"),
        "",
    )
    raw_profile = file_values["providers"].get(provider_name, {})
    provider_source = (
        "command line"
        if provider
        else "process environment"
        if os.environ.get(ENV_PROVIDER)
        else "projects.json"
        if selection.get("provider")
        else "project .patchfox.toml"
        if file_values["project_top"].get("provider")
        else "global config"
        if file_values["top"].get("default_provider")
        or file_values["top"].get("provider")
        else "built-in default"
    )
    model_source = (
        "command line"
        if model
        else "process environment"
        if os.environ.get(ENV_MODEL) or env_values.get("model")
        else "projects.json"
        if selection_model
        else "project .patchfox.toml"
        if project_model
        else "global config"
        if raw_profile.get("model")
        else "built-in default"
    )
    base_url_source = (
        "command line"
        if base_url
        else "process environment"
        if os.environ.get(ENV_BASE_URL) or env_values.get("base_url")
        else "global config"
        if raw_profile.get("base_url")
        else "built-in default"
    )
    api_key_source = (
        "command line"
        if api_key
        else "process environment"
        if os.environ.get(ENV_API_KEY) or env_values.get("api_key")
        else "auth.json"
        if stored_api_key
        else "missing"
    )
    protocol_source = (
        "process environment"
        if os.environ.get("PATCHFOX_PROTOCOL")
        else "global config"
        if raw_profile.get("protocol")
        else "built-in default"
    )

    return ProviderConfig(
        name=provider_name,
        protocol=protocol,
        api_key=str(resolved_api_key or ""),
        base_url=str(resolved_base_url or ""),
        model=str(resolved_model or ""),
        supports_vision=supports_vision,
        vision_provider=normalize_provider_name(resolved_vision_provider) if resolved_vision_provider else "",
        warnings=tuple(file_values.get("warnings", ())),
        sources=(
            ("provider", provider_source),
            ("protocol", protocol_source),
            ("base_url", base_url_source),
            ("model", model_source),
            ("api_key", api_key_source),
        ),
    )


def resolve_vision_provider_config(
    provider: str | None = None,
    *,
    start: str | Path = ".",
    config_path: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ProviderConfig:
    """Resolve a provider profile for image inspection calls.

    Vision calls often need a different endpoint from the main text provider.
    For example, a project can use DeepSeek for normal tool planning but route
    image inspection to an official OpenAI-compatible vision endpoint. These
    overrides intentionally apply only to the vision client, so existing
    OpenAI-compatible text profiles can keep pointing at right.codes or another
    proxy.
    """

    resolved_model = _first_value(
        model,
        os.environ.get(ENV_VISION_MODEL),
    )
    resolved_base_url = _first_value(
        base_url,
        os.environ.get(ENV_VISION_BASE_URL),
        os.environ.get(ENV_VISION_BASE_URL_ALT),
    )
    resolved_api_key = _first_value(
        api_key,
        os.environ.get(ENV_VISION_API_KEY),
    )
    return resolve_provider_config(
        provider,
        start=start,
        config_path=config_path,
        model=resolved_model or None,
        base_url=resolved_base_url or None,
        api_key=resolved_api_key or None,
    )


def resolve_project_sandbox_config(
    *,
    start: str | Path = ".",
    config_path: str | None = None,
    mode: str | None = None,
    backend: str | None = None,
):
    file_values = _load_config_values(start=start, explicit_path=config_path)
    values = {"sandbox": dict(file_values.get("sandbox", {}) or {})}
    if mode:
        values["sandbox"]["mode"] = mode
    if backend:
        values["sandbox"]["backend"] = backend
    return resolve_sandbox_values(values)


def resolve_project_shell_config(
    *,
    start: str | Path = ".",
    config_path: str | None = None,
    backend: str | None = None,
) -> ShellConfig:
    file_values = _load_config_values(start=start, explicit_path=config_path)
    values = {"shell": dict(file_values.get("shell", {}) or {})}
    if backend:
        values["shell"]["backend"] = backend
    return resolve_shell_values(values)


def normalize_provider_name(provider: str | None) -> str:
    normalized = (provider or DEFAULT_PROVIDER).strip().lower()
    return PROVIDER_ALIASES.get(normalized, normalized)


def _load_config_values(start: str | Path, explicit_path: str | None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "top": {},
        "project_top": {},
        "providers": {},
        "sandbox": {},
        "shell": {},
        "warnings": [],
    }
    if explicit_path:
        _merge_config_values(
            values, _read_config_file(Path(explicit_path).expanduser())
        )
        return values

    global_path = config_paths().config
    if global_path.exists():
        _merge_config_values(values, _read_config_file(global_path))

    project_path = find_project_config(start)
    if project_path and project_path.exists():
        project_values = _read_config_file(project_path)
        values["project_top"].update(
            {
                key: value
                for key, value in project_values.get("top", {}).items()
                if key in {"provider", "model"}
            }
        )
        values["sandbox"].update(project_values.get("sandbox", {}))
        values["shell"].update(project_values.get("shell", {}))
        unsafe = bool(project_values.get("providers")) or bool(
            project_values.get("unsafe_provider_keys")
        )
        if unsafe:
            values["warnings"].append(
                f"ignored unsafe provider credentials/endpoints in project config {project_path}; "
                "project files may only select provider and model"
            )
    return values


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid PatchFox config file {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"could not read PatchFox config file {path}: {exc}") from exc

    values: dict[str, Any] = {
        "top": {},
        "providers": {},
        "sandbox": {},
        "shell": {},
        "unsafe_provider_keys": [],
    }
    if "provider" in data:
        values["top"]["provider"] = data["provider"]
    if "default_provider" in data:
        values["top"]["default_provider"] = data["default_provider"]
    if "model" in data:
        values["top"]["model"] = data["model"]
    values["unsafe_provider_keys"] = [
        key for key in ("api_key", "base_url", "protocol") if key in data
    ]

    providers = data.get("providers", {})
    if isinstance(providers, dict):
        for name, section in providers.items():
            if isinstance(section, dict):
                values["providers"][normalize_provider_name(str(name))] = dict(section)

    sandbox = data.get("sandbox", {})
    if isinstance(sandbox, dict):
        values["sandbox"] = dict(sandbox)

    shell = data.get("shell", {})
    if isinstance(shell, dict):
        values["shell"] = dict(shell)

    for name in ("openai", "anthropic", "deepseek"):
        section = data.get(name, {})
        if isinstance(section, dict):
            values["providers"].setdefault(name, {}).update(section)
    return values


def _merge_config_values(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    target["top"].update(incoming.get("top", {}))
    target["sandbox"].update(incoming.get("sandbox", {}))
    target["shell"].update(incoming.get("shell", {}))
    for name, section in incoming.get("providers", {}).items():
        target["providers"].setdefault(name, {}).update(section)


def _profile_values(
    providers: dict[str, dict[str, Any]], provider_name: str
) -> dict[str, Any]:
    values = dict(PROVIDER_DEFAULTS.get(provider_name, {}))
    values.update(providers.get(provider_name, {}))
    return values


def _load_legacy_env_values(start: str | Path) -> dict[str, str]:
    env_path = find_project_env(start)
    if env_path is None:
        return {}
    loaded = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is not None:
            loaded[parsed[0]] = parsed[1]
    return loaded


def _env_values(provider_name: str, protocol: str) -> dict[str, str]:
    values: dict[str, str] = {}
    sources = [
        PROVIDER_ENV_NAMES.get(provider_name, {}),
        LEGACY_ENV_NAMES.get(provider_name, {}),
    ]
    if provider_name == protocol:
        sources.extend(
            (
                PROVIDER_ENV_NAMES.get(protocol, {}),
                LEGACY_ENV_NAMES.get(protocol, {}),
            )
        )
    for source in sources:
        for key, names in source.items():
            value = _first_env(names)
            if value and key not in values:
                values[key] = value
    return values


def _legacy_values(
    provider_name: str, protocol: str, env_values: dict[str, str]
) -> dict[str, str]:
    values: dict[str, str] = {}
    sources = [LEGACY_ENV_NAMES.get(provider_name, {})]
    if provider_name == protocol:
        sources.append(LEGACY_ENV_NAMES.get(protocol, {}))
    for source in sources:
        for key, names in source.items():
            value = _first_mapping_value(env_values, names)
            if value and key not in values:
                values[key] = value
    return values


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _first_mapping_value(values: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = values.get(name)
        if value:
            return value
    return ""


def _first_value(*values):
    for value in values:
        if value:
            return value
    return ""


def _first_present(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def _bool_value(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _validate_protocol(protocol: Any, provider_name: str) -> str:
    normalized = str(protocol or "").strip().lower()
    if normalized not in PROTOCOLS:
        raise ValueError(
            f"provider {provider_name!r} uses unsupported protocol: {protocol!r}"
        )
    return normalized
