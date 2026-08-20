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
        service_id="qianfan",
        opt_in="NEURO_CODE_RUN_LIVE_QIANFAN",
        api_key_env="QIANFAN_API_KEY",
        protocol_env="NEURO_CODE_LIVE_QIANFAN_PROTOCOL",
        default_protocol="openai-chat",
        base_url_env="NEURO_CODE_LIVE_QIANFAN_BASE_URL",
        base_urls_by_protocol={
            "openai-chat": "https://qianfan.baidubce.com/v2",
            "openai-responses": "https://qianfan.baidubce.com/v2",
            "anthropic-messages": "https://qianfan.baidubce.com/anthropic",
        },
        model_env="NEURO_CODE_LIVE_QIANFAN_MODEL",
        default_model="deepseek-v3.2",
    )


@pytest.mark.asyncio
async def test_qianfan_live_text_tool_roundtrip() -> None:
    profile = _profile()
    provider = create_provider(profile)
    await run_text_probe(provider, "qianfan")
    await run_tool_roundtrip(provider, "Qianfan")
