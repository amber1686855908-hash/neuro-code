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
        service_id="ark",
        opt_in="NEURO_CODE_RUN_LIVE_ARK",
        api_key_env="ARK_API_KEY",
        protocol_env="NEURO_CODE_LIVE_ARK_PROTOCOL",
        default_protocol="openai-responses",
        base_url_env="NEURO_CODE_LIVE_ARK_BASE_URL",
        base_urls_by_protocol={
            "openai-chat": "https://ark.cn-beijing.volces.com/api/v3",
            "openai-responses": "https://ark.cn-beijing.volces.com/api/v3",
        },
        model_env="NEURO_CODE_LIVE_ARK_MODEL",
        default_model="doubao-seed-2-0-lite-260215",
    )


@pytest.mark.asyncio
async def test_ark_live_text_tool_roundtrip() -> None:
    profile = _profile()
    provider = create_provider(profile)
    await run_text_probe(provider, "ark")
    await run_tool_roundtrip(provider, "Ark")
