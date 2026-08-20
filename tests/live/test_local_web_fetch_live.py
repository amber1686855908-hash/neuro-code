from __future__ import annotations

import os

import pytest

from neuro_code.application.ports.web_fetch import WebFetchRequest
from neuro_code.infrastructure.web_fetch.local import LocalWebFetcher


@pytest.mark.live
@pytest.mark.asyncio
async def test_local_web_fetch_live_smoke_without_provider_credentials() -> None:
    if os.environ.get("NEURO_CODE_LIVE_WEB_FETCH") != "1":
        pytest.skip("local web fetch live test requires NEURO_CODE_LIVE_WEB_FETCH=1")
    result = await LocalWebFetcher().fetch(WebFetchRequest("https://example.com/"))
    assert result.status_code == 200
    assert result.media_type in {"text/html", "application/xhtml+xml"}
    assert result.content
