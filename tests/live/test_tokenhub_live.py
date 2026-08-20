from __future__ import annotations

import pytest
from tests.live.platform_helpers import (
    platform_profile,
    run_text_probe,
    run_tool_roundtrip,
)

from neuro_code.infrastructure.providers import create_provider

pytestmark = pytest.mark.live


def _profile():
    return platform_profile(
        service_id="tokenhub",
        opt_in="NEURO_CODE_RUN_LIVE_TOKENHUB",
        api_key_env="TOKENHUB_API_KEY",
        protocol_env="NEURO_CODE_LIVE_TOKENHUB_PROTOCOL",
        default_protocol="openai-chat",
        base_url_env="NEURO_CODE_LIVE_TOKENHUB_BASE_URL",
        base_urls_by_protocol={
            "openai-chat": "https://tokenhub.tencentmaas.com/v1",
            "openai-responses": "https://tokenhub.tencentmaas.com/v1",
            "anthropic-messages": "https://tokenhub.tencentmaas.com/v1",
        },
        model_env="NEURO_CODE_LIVE_TOKENHUB_MODEL",
        default_model="glm-5.3",
    )


@pytest.mark.asyncio
async def test_tokenhub_live_text_tool_roundtrip() -> None:
    profile = _profile()
    provider = create_provider(profile)
    await run_text_probe(provider, "tokenhub")
    await run_tool_roundtrip(provider, "TokenHub")
