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
        service_id="bailian",
        opt_in="NEURO_CODE_RUN_LIVE_BAILIAN",
        api_key_env="DASHSCOPE_API_KEY",
        protocol_env="NEURO_CODE_LIVE_BAILIAN_PROTOCOL",
        default_protocol="openai-chat",
        base_url_env="NEURO_CODE_LIVE_BAILIAN_BASE_URL",
        base_urls_by_protocol={
            "openai-chat": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "openai-responses": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "anthropic-messages": "https://dashscope.aliyuncs.com/apps/anthropic",
        },
        model_env="NEURO_CODE_LIVE_BAILIAN_MODEL",
        default_model="qwen3.7-plus",
    )


@pytest.mark.asyncio
async def test_bailian_live_text_tool_roundtrip() -> None:
    profile = _profile()
    provider = create_provider(profile)
    await run_text_probe(provider, "bailian")
    await run_tool_roundtrip(provider, "Bailian")
