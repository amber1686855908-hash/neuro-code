from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pygrok_build.errors import ConfigurationError

_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai-compatible": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "GEMINI_API_KEY"),
}


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str = "default"
    kind: str = "openai-compatible"
    model: str = "grok-4-latest"
    base_url: str = "https://api.x.ai/v1"
    api_key_env: str = "XAI_API_KEY"
    timeout_seconds: float = 120.0
    max_output_tokens: int = 8192

    def api_key(self, environ: Mapping[str, str] | None = None) -> str:
        source = os.environ if environ is None else environ
        value = source.get(self.api_key_env, "").strip()
        if not value:
            raise ConfigurationError(
                f"model credential is missing; set environment variable {self.api_key_env}"
            )
        return value


@dataclass(frozen=True, slots=True)
class AppConfig:
    cwd: Path
    state_dir: Path
    provider: ProviderConfig
    loaded_files: tuple[Path, ...] = ()

    def redacted_dict(self) -> dict[str, Any]:
        return {
            "cwd": str(self.cwd),
            "state_dir": str(self.state_dir),
            "provider": {
                "name": self.provider.name,
                "kind": self.provider.kind,
                "model": self.provider.model,
                "base_url": self.provider.base_url,
                "api_key_env": self.provider.api_key_env,
                "credential_configured": bool(os.environ.get(self.provider.api_key_env)),
                "timeout_seconds": self.provider.timeout_seconds,
                "max_output_tokens": self.provider.max_output_tokens,
            },
            "loaded_files": [str(path) for path in self.loaded_files],
        }


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            loaded = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"cannot load configuration {path}: {error}") from error
    return loaded


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _string(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def load_config(
    cwd: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> AppConfig:
    env = os.environ if environ is None else environ
    resolved_cwd = (cwd or Path.cwd()).expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser().resolve()
    state_dir = Path(env.get("PYGROK_HOME", resolved_home / ".pygrok-build")).expanduser()

    candidates = (
        resolved_home / ".grok" / "config.toml",
        state_dir / "config.toml",
        resolved_cwd / ".pygrok-build" / "config.toml",
    )
    data: dict[str, Any] = {}
    loaded_files: list[Path] = []
    for candidate in candidates:
        if candidate.is_file():
            data = _deep_merge(data, _read_toml(candidate))
            loaded_files.append(candidate)

    provider_data = data.get("provider", {}).get("default", {})
    if not isinstance(provider_data, Mapping):
        raise ConfigurationError("[provider.default] must be a TOML table")

    # Grok-compatible custom model fallback. Native provider configuration wins.
    legacy_model = data.get("model", {}).get("default", {})
    if not isinstance(legacy_model, Mapping):
        legacy_model = {}

    kind = _string(provider_data.get("kind"), "openai-compatible")
    default_base_url, default_api_key_env = _PROVIDER_DEFAULTS.get(
        kind, _PROVIDER_DEFAULTS["openai-compatible"]
    )
    legacy_fallback = legacy_model if kind == "openai-compatible" else {}
    env_key_value = provider_data.get(
        "api_key_env", legacy_fallback.get("env_key", default_api_key_env)
    )
    if isinstance(env_key_value, list):
        env_key_value = next(
            (item for item in env_key_value if isinstance(item, str)), default_api_key_env
        )

    raw_model = env.get("PYGROK_MODEL", provider_data.get("model", legacy_fallback.get("model")))
    if kind in {"anthropic", "gemini"} and not (isinstance(raw_model, str) and raw_model.strip()):
        raise ConfigurationError(f"provider kind {kind!r} requires an explicit model")
    raw_timeout = provider_data.get("timeout_seconds", 120.0)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise ConfigurationError("provider timeout_seconds must be a number")
    raw_max_output_tokens = provider_data.get("max_output_tokens", 8192)
    if isinstance(raw_max_output_tokens, bool) or not isinstance(raw_max_output_tokens, int):
        raise ConfigurationError("provider max_output_tokens must be an integer")

    provider = ProviderConfig(
        name="default",
        kind=kind,
        model=_string(raw_model, "grok-4-latest"),
        base_url=_string(
            env.get(
                "PYGROK_BASE_URL",
                provider_data.get("base_url", legacy_fallback.get("base_url")),
            ),
            default_base_url,
        ).rstrip("/"),
        api_key_env=_string(env_key_value, default_api_key_env),
        timeout_seconds=float(raw_timeout),
        max_output_tokens=raw_max_output_tokens,
    )
    if provider.timeout_seconds <= 0:
        raise ConfigurationError("provider timeout_seconds must be positive")
    if provider.max_output_tokens <= 0:
        raise ConfigurationError("provider max_output_tokens must be positive")

    return AppConfig(
        cwd=resolved_cwd,
        state_dir=state_dir.resolve(),
        provider=provider,
        loaded_files=tuple(loaded_files),
    )


def override_provider(
    config: AppConfig,
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> AppConfig:
    provider = replace(
        config.provider,
        model=model or config.provider.model,
        base_url=(base_url or config.provider.base_url).rstrip("/"),
    )
    return replace(config, provider=provider)
