from __future__ import annotations

import unittest

import httpx

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.domain.provider_catalog import ProviderCatalogError, ProviderConnectionSpec
from neuro_code.infrastructure.providers.provider_catalog import HttpProviderCatalog
from neuro_code.shared.errors import ConfigurationError


class ProviderCatalogTests(unittest.IsolatedAsyncioTestCase):
    policy = HttpClientPolicy(trust_env=False)

    async def test_openai_catalog_uses_bearer_auth_and_bounded_sorted_models(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://api.deepseek.com/models")
            self.assertEqual(request.headers["authorization"], "Bearer secret-key")
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "deepseek-reasoner"},
                        {"id": "deepseek-chat"},
                        {"id": "deepseek-chat"},
                    ]
                },
            )

        catalog = HttpProviderCatalog(transport=httpx.MockTransport(handler))
        result = await catalog.discover_models(
            ProviderConnectionSpec(
                protocol="openai-chat",
                base_url="https://api.deepseek.com",
                api_key="secret-key",
            ),
            http_policy=self.policy,
        )

        self.assertEqual(result.models, ("deepseek-chat", "deepseek-reasoner"))
        self.assertFalse(result.truncated)

    async def test_operation_suffix_is_removed_before_openai_model_discovery(self) -> None:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(200, json={"data": []})

        catalog = HttpProviderCatalog(transport=httpx.MockTransport(handler))
        await catalog.discover_models(
            ProviderConnectionSpec(
                protocol="openai-responses",
                base_url="https://provider.invalid/v1/responses",
                api_key="secret-key",
            ),
            http_policy=self.policy,
        )

        self.assertEqual(requested, ["https://provider.invalid/v1/models"])

    async def test_anthropic_catalog_uses_native_path_and_headers(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://api.anthropic.com/v1/models")
            self.assertEqual(request.headers["x-api-key"], "anthropic-secret")
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, json={"data": [{"id": "claude-fixture"}]})

        result = await HttpProviderCatalog(transport=httpx.MockTransport(handler)).discover_models(
            ProviderConnectionSpec(
                protocol="anthropic-messages",
                base_url="https://api.anthropic.com",
                api_key="anthropic-secret",
            ),
            http_policy=self.policy,
        )

        self.assertEqual(result.models, ("claude-fixture",))

    async def test_gemini_catalog_normalizes_names_and_filters_non_generation_models(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                str(request.url),
                "https://generativelanguage.googleapis.com/v1beta/models",
            )
            self.assertEqual(request.headers["x-goog-api-key"], "gemini-secret")
            self.assertNotIn("gemini-secret", str(request.url))
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-fixture",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/embedding-fixture",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ]
                },
            )

        result = await HttpProviderCatalog(transport=httpx.MockTransport(handler)).discover_models(
            ProviderConnectionSpec(
                protocol="gemini-generate-content",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key="gemini-secret",
            ),
            http_policy=self.policy,
        )

        self.assertEqual(result.models, ("gemini-fixture",))

    async def test_catalog_is_truncated_to_two_hundred_models(self) -> None:
        catalog = HttpProviderCatalog(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"data": [{"id": f"model-{index:03d}"} for index in range(250)]},
                )
            )
        )

        result = await catalog.discover_models(
            ProviderConnectionSpec(
                protocol="openai-chat",
                base_url="https://provider.invalid/v1",
                api_key="secret-key",
            ),
            http_policy=self.policy,
        )

        self.assertEqual(len(result.models), 200)
        self.assertTrue(result.truncated)

    async def test_http_failures_are_classified_without_reading_sensitive_bodies(self) -> None:
        cases = ((401, "authentication"), (404, "endpoint"), (429, "rate_limit"), (503, "server"))
        for status, kind in cases:
            with self.subTest(status=status):
                catalog = HttpProviderCatalog(
                    transport=httpx.MockTransport(
                        lambda request, status=status: httpx.Response(
                            status,
                            text="must-not-leak-secret-key",
                        )
                    )
                )
                with self.assertRaises(ProviderCatalogError) as raised:
                    await catalog.discover_models(
                        ProviderConnectionSpec(
                            protocol="openai-chat",
                            base_url="https://provider.invalid/v1",
                            api_key="secret-key",
                        ),
                        http_policy=self.policy,
                    )
                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(raised.exception.status_code, status)
                self.assertNotIn("must-not-leak", str(raised.exception))

    async def test_network_failure_redacts_api_key_and_proxy_url(self) -> None:
        proxy_url = "http://proxy-user:proxy-password@127.0.0.1:8080"

        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                f"offline secret-key through {proxy_url}",
                request=request,
            )

        catalog = HttpProviderCatalog(transport=httpx.MockTransport(fail))
        policy = HttpClientPolicy(
            trust_env=False,
            redaction_values=(proxy_url, "proxy-user", "proxy-password"),
        )
        with self.assertRaises(ProviderCatalogError) as raised:
            await catalog.discover_models(
                ProviderConnectionSpec(
                    protocol="openai-chat",
                    base_url="https://provider.invalid/v1",
                    api_key="secret-key",
                ),
                http_policy=policy,
            )

        rendered = str(raised.exception)
        self.assertEqual(raised.exception.kind, "network")
        self.assertNotIn("secret-key", rendered)
        self.assertNotIn("proxy-password", rendered)
        self.assertIn("[REDACTED]", rendered)

    async def test_invalid_and_oversized_catalogs_fail_closed(self) -> None:
        cases = (
            (httpx.Response(200, text="not json"), "invalid_response"),
            (
                httpx.Response(200, json={"data": [{"id": "bad\nmodel"}]}),
                "invalid_response",
            ),
            (
                httpx.Response(
                    200,
                    content=b"{}",
                    headers={"content-length": "1048577"},
                ),
                "response_too_large",
            ),
        )
        for response, kind in cases:
            with self.subTest(kind=kind):
                catalog = HttpProviderCatalog(
                    transport=httpx.MockTransport(lambda request, response=response: response)
                )
                with self.assertRaises(ProviderCatalogError) as raised:
                    await catalog.discover_models(
                        ProviderConnectionSpec(
                            protocol="openai-chat",
                            base_url="https://provider.invalid/v1",
                            api_key="secret-key",
                        ),
                        http_policy=self.policy,
                    )
                self.assertEqual(raised.exception.kind, kind)

    def test_connection_spec_validates_and_hides_credentials(self) -> None:
        spec = ProviderConnectionSpec(
            protocol="openai-chat",
            base_url="https://provider.invalid/v1/",
            api_key="secret-key",
        )
        self.assertEqual(spec.base_url, "https://provider.invalid/v1")
        self.assertNotIn("secret-key", repr(spec))

        with self.assertRaises(ConfigurationError):
            ProviderConnectionSpec(
                protocol="openai-chat",
                base_url="https://user:password@provider.invalid/v1",
                api_key="secret-key",
            )
