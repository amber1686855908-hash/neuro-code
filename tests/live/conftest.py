from __future__ import annotations

import os

import pytest

from neuro_code.application.ports.model import ModelProvider
from neuro_code.config import ProviderProfile
from neuro_code.providers import create_provider


def _environment_value(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@pytest.fixture(scope="session")
def deepseek_profile() -> ProviderProfile:
    if not _environment_value("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY is not configured")
    proxy_mode = _environment_value("NEURO_CODE_LIVE_PROXY_MODE", "environment")
    return ProviderProfile(
        name="live-deepseek",
        protocol="openai-chat",
        model=_environment_value("NEURO_CODE_LIVE_DEEPSEEK_MODEL", "deepseek-chat"),
        base_url=_environment_value(
            "NEURO_CODE_LIVE_DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ),
        api_key_env="DEEPSEEK_API_KEY",
        timeout_seconds=120,
        max_output_tokens=1_024,
        proxy_mode=proxy_mode,
        proxy_url_env="NEURO_CODE_LIVE_PROXY_URL" if proxy_mode == "explicit" else None,
    )


@pytest.fixture(scope="session")
def deepseek_provider(deepseek_profile: ProviderProfile) -> ModelProvider:
    return create_provider(deepseek_profile)
