"""Interactive setup and explicit migration for PatchFox configuration."""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable
from pathlib import Path

from . import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    ENV_PROVIDER,
    PROVIDER_DEFAULTS,
    _parse_env_line,
    normalize_provider_name,
    resolve_provider_config,
)
from .store import config_paths, config_summary, save_provider

if os.sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


def configuration_missing(args) -> list[str]:
    config = resolve_provider_config(
        getattr(args, "provider", None),
        start=getattr(args, "_project_root", getattr(args, "cwd", ".")),
        config_path=getattr(args, "config", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
    )
    missing = []
    if not config.api_key:
        missing.append("API key")
    if not config.base_url:
        missing.append("base URL")
    if not config.model:
        missing.append("model")
    return missing


def run_text_setup(
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
    output_fn: Callable[[str], None] = print,
) -> str:
    output_fn("PatchFox first-time setup")
    output_fn("Credentials are stored separately in ~/.patchfox/auth.json.")
    provider = _ask(input_fn, "Provider", "deepseek").lower()
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    protocol = _ask(
        input_fn,
        "Protocol (openai/anthropic)",
        str(defaults.get("protocol", "openai")),
    ).lower()
    base_url = _ask(input_fn, "Base URL", str(defaults.get("base_url", "")))
    model = _ask(input_fn, "Model", str(defaults.get("model", "")))
    api_key = secret_fn(f"API key for {provider}: ").strip()
    config_path, _ = save_provider(
        provider,
        protocol=protocol,
        base_url=base_url,
        model=model,
        api_key=api_key,
        make_default=True,
    )
    output_fn(f"Saved PatchFox configuration to {config_path}")
    return provider


def render_config_show(start: str | Path = ".") -> str:
    summary = config_summary(start)
    lines = [
        f"config home: {summary['config_home']}",
        f"global config: {summary['global_config']}",
        f"auth file: {summary['auth_file']}",
        f"projects file: {summary['projects_file']}",
        f"project: {summary['project']}",
        f"provider: {summary['provider']}",
        f"protocol: {summary['protocol']}",
        f"base URL: {summary['base_url']}",
        f"model: {summary['model']}",
        "API key: configured" if summary["api_key_configured"] else "API key: missing",
        "sources: "
        + ", ".join(
            f"{key}={value}" for key, value in summary.get("sources", {}).items()
        ),
    ]
    lines.extend(f"warning: {warning}" for warning in summary["warnings"])
    return "\n".join(lines)


def legacy_migration_candidate(start: str | Path) -> dict | None:
    """Read legacy settings without mutating the process environment."""

    root = Path(start).resolve()
    paths = config_paths()
    sources: list[Path] = []
    merged: dict[str, str] = {}

    for path in (paths.legacy_config, root / ".patchfox.toml"):
        if not path.exists():
            continue
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        provider = normalize_provider_name(
            str(data.get("default_provider", data.get("provider", "")) or "")
        )
        if not provider:
            provider = "openai"
        section = {}
        providers = data.get("providers", {})
        if isinstance(providers, dict) and isinstance(providers.get(provider), dict):
            section.update(providers[provider])
        if isinstance(data.get(provider), dict):
            section.update(data[provider])
        for key in ("protocol", "base_url", "model", "api_key"):
            value = section.get(key, data.get(key, ""))
            if value:
                merged[key] = str(value)
        merged["provider"] = provider
        sources.append(path)

    env_path = root / ".env"
    if env_path.exists():
        env_values: dict[str, str] = {}
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                parsed = _parse_env_line(line)
                if parsed:
                    env_values[parsed[0]] = parsed[1]
        except (OSError, ValueError):
            env_values = {}
        provider = normalize_provider_name(
            env_values.get(ENV_PROVIDER, merged.get("provider", ""))
        )
        names = {
            "openai": ("OPENAI", "PATCHFOX_OPENAI"),
            "anthropic": ("ANTHROPIC", "PATCHFOX_ANTHROPIC"),
            "deepseek": ("DEEPSEEK", "PATCHFOX_DEEPSEEK"),
        }.get(provider, (provider.upper(), f"PATCHFOX_{provider.upper()}"))
        lookup = {
            "api_key": [ENV_API_KEY, *(f"{prefix}_API_KEY" for prefix in names)],
            "base_url": [
                ENV_BASE_URL,
                *(f"{prefix}_API_BASE" for prefix in names),
                *(f"{prefix}_BASE_URL" for prefix in names),
            ],
            "model": [ENV_MODEL, *(f"{prefix}_MODEL" for prefix in names)],
            "protocol": ["PATCHFOX_PROTOCOL"],
        }
        found = False
        for key, env_names in lookup.items():
            for name in env_names:
                if env_values.get(name):
                    merged[key] = env_values[name]
                    found = True
                    break
        if found or env_values.get(ENV_PROVIDER):
            merged["provider"] = provider
            sources.append(env_path)

    if not sources:
        return None
    provider = normalize_provider_name(merged.get("provider", "openai"))
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    return {
        "sources": sources,
        "provider": provider,
        "protocol": merged.get("protocol", str(defaults.get("protocol", ""))),
        "base_url": merged.get("base_url", str(defaults.get("base_url", ""))),
        "model": merged.get("model", str(defaults.get("model", ""))),
        "api_key": merged.get("api_key", ""),
    }


def confirm_and_migrate_legacy(
    start: str | Path,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> bool:
    candidate = legacy_migration_candidate(start)
    if not candidate:
        return False
    output_fn("Legacy PatchFox configuration detected:")
    for path in candidate["sources"]:
        output_fn(f"  source: {path}")
    output_fn(f"  provider: {candidate['provider']}")
    output_fn(f"  model: {candidate['model'] or '-'}")
    output_fn(
        "  API key: present" if candidate["api_key"] else "  API key: missing"
    )
    answer = input_fn("Import it into ~/.patchfox now? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        output_fn("Migration skipped; no files were changed.")
        return False
    if not candidate["api_key"]:
        output_fn(
            "Legacy settings do not contain an API key; continuing with first-time setup."
        )
        return False
    save_provider(
        candidate["provider"],
        protocol=candidate["protocol"],
        base_url=candidate["base_url"],
        model=candidate["model"],
        api_key=candidate["api_key"],
        make_default=True,
    )
    output_fn(f"Imported legacy configuration into {config_paths().home}")
    return True


def _ask(input_fn: Callable[[str], str], label: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input_fn(f"{label}{suffix}: ").strip()
    return value or default
