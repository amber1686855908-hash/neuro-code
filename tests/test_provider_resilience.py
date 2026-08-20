from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import ModelCapabilitySet, ModelToolPolicy
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelEvent, ModelTextDelta
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.infrastructure.providers.catalog_cache import PersistentProviderCatalog
from neuro_code.infrastructure.providers.resilience import ResilientModelProvider
from neuro_code.shared.errors import ProviderError


class _Provider:
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = None
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def stream(
        self,
        context: ModelContext,
        tools: tuple[object, ...],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError("temporary network failure")
        yield ModelTextDelta("ok")


class _Catalog:
    def __init__(self) -> None:
        self.calls = 0
        self.failure: ProviderCatalogError | None = None

    async def discover_models(
        self,
        spec: ProviderConnectionSpec,
        *,
        http_policy: HttpClientPolicy,
    ) -> ProviderCatalogResult:
        del spec, http_policy
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return ProviderCatalogResult(("cached-model",))


class ProviderResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_stops_after_first_output_and_exposes_safe_health(self) -> None:
        provider = _Provider(failures=1)
        resilient = ResilientModelProvider(provider, max_attempts=2, backoff_seconds=0)
        events = [
            event
            async for event in resilient.stream(
                ModelContext((Message(Role.USER, "hello"),)),
                (),
            )
        ]
        self.assertEqual(
            [event.text for event in events if isinstance(event, ModelTextDelta)], ["ok"]
        )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(resilient.health.successes, 1)
        self.assertNotIn("api", json.dumps(resilient.health.to_dict()))

    async def test_catalog_falls_back_to_a_persisted_result_on_network_failure(self) -> None:
        delegate = _Catalog()
        policy = HttpClientPolicy(trust_env=False)
        spec = ProviderConnectionSpec(
            protocol="openai-chat",
            base_url="https://provider.invalid/v1",
            api_key="secret-key",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            cached = PersistentProviderCatalog(delegate, path)
            fresh = await cached.discover_models(spec, http_policy=policy)
            self.assertEqual(fresh.models, ("cached-model",))
            self.assertNotIn("secret-key", path.read_text(encoding="utf-8"))

            delegate.failure = ProviderCatalogError("network", detail="offline")
            fallback = await cached.discover_models(spec, http_policy=policy)
            self.assertEqual(fallback.models, ("cached-model",))
            self.assertEqual(delegate.calls, 2)


if __name__ == "__main__":
    unittest.main()
