from __future__ import annotations

import unittest

import httpx

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalogError,
    ProviderConnectionSpec,
)
from neuro_code.application.ports.provider_services import ModelCatalogStrategy
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

    async def test_kimi_and_minimax_use_their_official_v1_model_catalogs(self) -> None:
        cases = (
            ("kimi", "kimi", "https://api.moonshot.ai/v1/models", "kimi-k2.6"),
            ("minimax", "minimax", "https://api.minimaxi.com/v1/models", "MiniMax-M3"),
        )
        for service_id, dialect, endpoint, model in cases:
            with self.subTest(service_id=service_id):

                def handler(
                    request: httpx.Request,
                    endpoint: str = endpoint,
                    model: str = model,
                ) -> httpx.Response:
                    self.assertEqual(str(request.url), endpoint)
                    self.assertEqual(request.headers["authorization"], "Bearer china-secret")
                    return httpx.Response(200, json={"data": [{"id": model}]})

                result = await HttpProviderCatalog(
                    transport=httpx.MockTransport(handler)
                ).discover_models(
                    ProviderConnectionSpec(
                        protocol="openai-chat",
                        dialect=dialect,
                        service_id=service_id,
                        catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
                        base_url=endpoint.removesuffix("/models"),
                        api_key="china-secret",
                    ),
                    http_policy=self.policy,
                )
                self.assertEqual(result.models, (model,))

    async def test_glm_static_catalog_does_not_invent_a_remote_models_endpoint(self) -> None:
        catalog = HttpProviderCatalog(
            transport=httpx.MockTransport(
                lambda request: self.fail("static GLM discovery must not make a request")
            )
        )
        with self.assertRaises(ProviderCatalogError) as raised:
            await catalog.discover_models(
                ProviderConnectionSpec(
                    protocol="openai-chat",
                    dialect="glm",
                    service_id="glm",
                    catalog_strategy=ModelCatalogStrategy.STATIC,
                    base_url="https://open.bigmodel.cn/api/paas/v4",
                    api_key="china-secret",
                ),
                http_policy=self.policy,
            )
        self.assertEqual(raised.exception.kind, "manual_only")

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

    async def test_platform_catalog_paths_follow_selected_protocol_and_endpoint(self) -> None:
        cases = (
            (
                "qianfan-openai",
                ProviderConnectionSpec(
                    protocol="openai-responses",
                    service_id="qianfan",
                    base_url="https://qianfan.baidubce.com/v2",
                    api_key="qianfan-secret",
                    catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
                ),
                "https://qianfan.baidubce.com/v2/models",
                {"authorization": "Bearer qianfan-secret"},
            ),
            (
                "bailian-openai",
                ProviderConnectionSpec(
                    protocol="openai-chat",
                    service_id="bailian",
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    api_key="bailian-secret",
                    catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
                ),
                "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
                {"authorization": "Bearer bailian-secret"},
            ),
            (
                "tokenhub-responses",
                ProviderConnectionSpec(
                    protocol="openai-responses",
                    service_id="tokenhub",
                    base_url="https://tokenhub.tencentmaas.com/v1",
                    api_key="tokenhub-secret",
                    catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
                ),
                "https://tokenhub.tencentmaas.com/v1/models",
                {"authorization": "Bearer tokenhub-secret"},
            ),
        )
        for name, spec, endpoint, expected_headers in cases:
            with self.subTest(name=name):

                def handler(
                    request: httpx.Request,
                    endpoint: str = endpoint,
                    expected_headers: dict[str, str] = expected_headers,
                    model_name: str = f"{name}-model",
                ) -> httpx.Response:
                    self.assertEqual(str(request.url), endpoint)
                    for header, value in expected_headers.items():
                        self.assertEqual(request.headers[header], value)
                    return httpx.Response(200, json={"data": [{"id": model_name}]})

                result = await HttpProviderCatalog(
                    transport=httpx.MockTransport(handler)
                ).discover_models(spec, http_policy=self.policy)
                self.assertEqual(result.models, (f"{name}-model",))

    async def test_static_platform_catalog_is_manual_only(self) -> None:
        catalog = HttpProviderCatalog(
            transport=httpx.MockTransport(
                lambda request: self.fail("Ark static discovery must not make a request")
            )
        )
        with self.assertRaises(ProviderCatalogError) as raised:
            await catalog.discover_models(
                ProviderConnectionSpec(
                    protocol="openai-chat",
                    service_id="ark",
                    base_url="https://ark.cn-beijing.volces.com/api/v3",
                    api_key="ark-secret",
                    catalog_strategy=ModelCatalogStrategy.STATIC,
                ),
                http_policy=self.policy,
            )
        self.assertEqual(raised.exception.kind, "manual_only")

        for base_url in (
            "https://qianfan.baidubce.com/anthropic",
            "https://dashscope.aliyuncs.com/apps/anthropic",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ProviderCatalogError) as raised:
                    await catalog.discover_models(
                        ProviderConnectionSpec(
                            protocol="anthropic-messages",
                            base_url=base_url,
                            api_key="platform-secret",
                            catalog_strategy=ModelCatalogStrategy.MANUAL_ONLY,
                        ),
                        http_policy=self.policy,
                    )
                self.assertEqual(raised.exception.kind, "manual_only")

    async def test_explicit_catalog_strategy_overrides_protocol_default(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://example.invalid/models")
            self.assertEqual(request.headers["x-api-key"], "example-secret")
            return httpx.Response(200, json={"data": [{"id": "example-model"}]})

        result = await HttpProviderCatalog(transport=httpx.MockTransport(handler)).discover_models(
            ProviderConnectionSpec(
                protocol="anthropic-messages",
                base_url="https://example.invalid",
                api_key="example-secret",
                service_id="example-cn",
                catalog_strategy=ModelCatalogStrategy.OPENAI_COMPATIBLE,
            ),
            http_policy=self.policy,
        )

        self.assertEqual(result.models, ("example-model",))

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

    async def test_gemini_interactions_catalog_uses_the_same_read_only_gemini_endpoint(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                str(request.url),
                "https://generativelanguage.googleapis.com/v1/models",
            )
            self.assertEqual(request.headers["x-goog-api-key"], "gemini-secret")
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-interactions-fixture",
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
                protocol="gemini-interactions",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key="gemini-secret",
            ),
            http_policy=self.policy,
        )

        self.assertEqual(result.models, ("gemini-interactions-fixture",))

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
