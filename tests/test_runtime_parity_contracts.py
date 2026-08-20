from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

from neuro_code.application.acp.contracts import (
    AcpMcpQuery,
    AcpMcpQueryError,
    AcpReadOnlySubagentQuery,
    AcpReadOnlySubagentQueryError,
    AcpSessionCommandQuery,
    AcpSubagentLifecycleQuery,
    AcpSubagentLifecycleQueryError,
    AcpToolOutputArtifactQuery,
    AcpToolOutputArtifactQueryError,
)
from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.mcp import (
    McpPrompt,
    McpPromptMessage,
    McpResource,
    McpResourceContent,
    McpResourceTemplate,
)
from neuro_code.application.ports.model import ModelCapabilitySet, ModelToolPolicy
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)
from neuro_code.application.runtime.tool_scheduler import ToolBatchExecutionError, ToolScheduler
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SyntheticReason,
    ToolCall,
)
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.conversation.request import ModelRequestSnapshot, context_fingerprints
from neuro_code.domain.tools import ToolDefinition, ToolExecutionMode
from neuro_code.infrastructure.providers.catalog_cache import PersistentProviderCatalog
from neuro_code.infrastructure.providers.resilience import ResilientModelProvider
from neuro_code.shared.errors import ConfigurationError, ProviderError


class RuntimeParityContractTests(unittest.TestCase):
    def test_mcp_port_models_project_metadata_and_reject_invalid_content(self) -> None:
        resource = McpResource(
            "server",
            "name",
            "fixture://resource",
            title="Title",
            description="Description",
            mime_type="text/plain",
            size=4,
        )
        template = McpResourceTemplate("server", "template", "fixture://{name}")
        prompt = McpPrompt("server", "prompt", arguments=({"name": "topic", "required": True},))
        message = McpPromptMessage("user", {"type": "text", "text": "hello"})
        content = McpResourceContent("fixture://resource", "text/plain", text="hello")

        self.assertEqual(resource.to_dict()["mimeType"], "text/plain")
        self.assertEqual(template.to_dict()["uriTemplate"], "fixture://{name}")
        self.assertEqual(prompt.to_dict()["arguments"][0]["name"], "topic")
        self.assertEqual(message.to_dict()["role"], "user")
        self.assertEqual(content.to_dict()["text"], "hello")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            McpResourceContent("fixture://bad")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            McpResourceContent("fixture://bad", text="a", blob="AQI=")
        with self.assertRaisesRegex(ValueError, "role"):
            McpPromptMessage("system", {})

    def test_acp_mcp_and_subagent_queries_are_strictly_bounded(self) -> None:
        queries = (
            AcpMcpQuery.from_payload({"sessionId": "s", "operation": "list"}),
            AcpMcpQuery.from_payload(
                {"sessionId": "s", "operation": "read_resource", "uri": "fixture://r"}
            ),
            AcpMcpQuery.from_payload(
                {
                    "sessionId": "s",
                    "operation": "get_prompt",
                    "name": "prompt",
                    "arguments": {"topic": "test"},
                }
            ),
            AcpMcpQuery.from_payload({"sessionId": "s", "operation": "refresh"}),
        )
        self.assertEqual(
            [query.operation for query in queries],
            ["list", "read_resource", "get_prompt", "refresh"],
        )
        self.assertEqual(AcpSessionCommandQuery.from_payload({"sessionId": "s"}).session_id, "s")
        subagent = AcpReadOnlySubagentQuery.from_payload(
            {"sessionId": "s", "prompt": "inspect", "maxSteps": 2}
        )
        self.assertEqual(subagent.max_steps, 2)
        invalid_payloads = (
            ({"sessionId": "s", "operation": "read_resource"}, "uri_required"),
            (
                {"sessionId": "s", "operation": "list", "uri": "fixture://r"},
                "operation_arguments_unsupported",
            ),
            (
                {"sessionId": "s", "operation": "get_prompt", "name": "p", "arguments": {"x": 1}},
                "argument_invalid",
            ),
            ({"sessionId": "s", "operation": "list", "extra": True}, "mcp_query_field_unsupported"),
        )
        for payload, reason in invalid_payloads:
            with self.subTest(reason=reason), self.assertRaisesRegex(AcpMcpQueryError, reason):
                AcpMcpQuery.from_payload(payload)
        with self.assertRaisesRegex(AcpReadOnlySubagentQueryError, "prompt_invalid"):
            AcpReadOnlySubagentQuery.from_payload({"sessionId": "s", "prompt": "\x00"})

        invalid_mcp_constructors = (
            (lambda: AcpMcpQuery(" ", "list"), "session_id_invalid"),
            (lambda: AcpMcpQuery("s", "invalid"), "operation_invalid"),
            (lambda: AcpMcpQuery("s", "read_resource"), "uri_required"),
            (lambda: AcpMcpQuery("s", "get_prompt"), "name_required"),
            (
                lambda: AcpMcpQuery("s", "list", uri="fixture://r"),
                "operation_arguments_unsupported",
            ),
            (
                lambda: AcpMcpQuery(
                    "s", "read_resource", uri="fixture://r", arguments=(("x", "y"),)
                ),
                "operation_arguments_unsupported",
            ),
            (
                lambda: AcpMcpQuery("s", "list", arguments=(("x", "y"),)),
                "operation_arguments_unsupported",
            ),
            (lambda: AcpMcpQuery("s", "list", arguments=(("x", 1),)), "argument_invalid"),
            (lambda: AcpMcpQuery("s", "list", arguments=(("x", "\x00"),)), "argument_invalid"),
            (
                lambda: AcpMcpQuery(
                    "s", "get_prompt", name="p", arguments=(("x", "y"), ("x", "z"))
                ),
                "argument_invalid",
            ),
            (lambda: AcpMcpQuery("s", "list", uri=""), "uri_invalid"),
            (lambda: AcpMcpQuery("s", "get_prompt", name=""), "name_invalid"),
            (
                lambda: AcpMcpQuery("s", "list", arguments=(("x", "x" * 8_200),)),
                "arguments_too_large",
            ),
            (
                lambda: AcpMcpQuery("s", "list", arguments=tuple((str(i), "v") for i in range(33))),
                "arguments_too_many",
            ),
        )
        for constructor, reason in invalid_mcp_constructors:
            with self.subTest(reason=reason), self.assertRaisesRegex(AcpMcpQueryError, reason):
                constructor()
        for payload, reason in (
            (None, "mcp_query_invalid"),
            ({"sessionId": 1, "operation": "list"}, "session_id_invalid"),
            ({"sessionId": "s", "operation": 1}, "operation_invalid"),
            ({"sessionId": "s", "operation": "list", "uri": 1}, "uri_invalid"),
            ({"sessionId": "s", "operation": "get_prompt", "name": 1}, "name_invalid"),
            ({"sessionId": "s", "operation": "list", "arguments": []}, "arguments_invalid"),
            (
                {"sessionId": "s", "operation": "list", "arguments": {"x": 1}},
                "argument_invalid",
            ),
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(AcpMcpQueryError, reason):
                AcpMcpQuery.from_payload(payload)  # type: ignore[arg-type]

        for payload, reason in (
            ({}, "session_command_invalid"),
            ({"sessionId": 1}, "session_id_invalid"),
            ({"sessionId": "\x00"}, "session_id_invalid"),
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(AcpMcpQueryError, reason):
                AcpSessionCommandQuery.from_payload(payload)

        for payload, reason in (
            (None, "subagent_query_invalid"),
            ({"sessionId": "s", "prompt": "p", "extra": True}, "subagent_query_field_unsupported"),
            ({"sessionId": 1, "prompt": "p"}, "session_id_invalid"),
            ({"sessionId": "s", "prompt": 1}, "prompt_invalid"),
            ({"sessionId": "s", "prompt": "p", "maxSteps": True}, "max_steps_invalid"),
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(AcpReadOnlySubagentQueryError, reason),
            ):
                AcpReadOnlySubagentQuery.from_payload(payload)  # type: ignore[arg-type]
        for constructor, reason in (
            (lambda: AcpReadOnlySubagentQuery("\x00", "p"), "session_id_invalid"),
            (lambda: AcpReadOnlySubagentQuery("s", "bad\x01"), "prompt_invalid"),
            (lambda: AcpReadOnlySubagentQuery("s", "p", 0), "max_steps_invalid"),
        ):
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(AcpReadOnlySubagentQueryError, reason),
            ):
                constructor()

        with self.assertRaisesRegex(AcpSubagentLifecycleQueryError, "lifecycle_query_invalid"):
            AcpSubagentLifecycleQuery.from_payload(None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(AcpSubagentLifecycleQueryError, "session_id_invalid"):
            AcpSubagentLifecycleQuery.from_payload(
                {"sessionId": 1, "taskId": "t", "action": "resume"}
            )
        with self.assertRaisesRegex(AcpSubagentLifecycleQueryError, "task_id_invalid"):
            AcpSubagentLifecycleQuery.from_payload(
                {"sessionId": "s", "taskId": 1, "action": "resume"}
            )
        with self.assertRaisesRegex(AcpSubagentLifecycleQueryError, "action_invalid"):
            AcpSubagentLifecycleQuery.from_payload({"sessionId": "s", "taskId": "t", "action": 1})

        artifact_id = "a" * 32
        self.assertEqual(AcpToolOutputArtifactQuery.from_payload({"sessionId": "s"}).limit, 100)
        self.assertEqual(
            AcpToolOutputArtifactQuery.from_payload(
                {"sessionId": "s", "artifactId": artifact_id, "maxBytes": 10}
            ).max_bytes,
            10,
        )
        for payload, reason in (
            (None, "artifact_query_invalid"),
            ({"sessionId": "s", "unsupported": True}, "artifact_query_field_unsupported"),
            ({"sessionId": 1}, "session_id_invalid"),
            ({"sessionId": "s", "artifactId": 1}, "artifact_id_invalid"),
            ({"sessionId": "s", "maxBytes": 10}, "max_bytes_requires_artifact_id"),
            (
                {"sessionId": "s", "artifactId": artifact_id, "limit": 1},
                "limit_only_applies_to_artifact_list",
            ),
            ({"sessionId": "s", "limit": True}, "artifact_limit_invalid"),
            (
                {"sessionId": "s", "artifactId": artifact_id, "maxBytes": True},
                "artifact_max_bytes_invalid",
            ),
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(AcpToolOutputArtifactQueryError, reason),
            ):
                AcpToolOutputArtifactQuery.from_payload(payload)  # type: ignore[arg-type]

    def test_request_snapshot_is_reconstructable_without_exposing_payload(self) -> None:
        items = (
            Message(
                Role.USER,
                content_parts=(
                    ContentPart.from_text("hello"),
                    ContentPart.from_audio("AQ==", "audio/wav"),
                ),
            ),
            Message(Role.USER, "runtime", synthetic_reason=SyntheticReason.RUNTIME_BUDGET),
            PreservedContextItem(
                kind=ContextItemKind.BACKEND_TOOL_CALL,
                payload={"type": "backend_tool_call", "name": "search"},
            ),
        )
        context = ModelContext(items, reasoning_effort=ReasoningEffort.HIGH)
        tool = ToolDefinition("read", "Read", {}, execution_mode=ToolExecutionMode.PARALLEL)
        snapshot = ModelRequestSnapshot.build(
            context=context,
            tools=(tool,),
            provider="fixture",
            model="model",
            context_affinity="fixture:model",
            step=1,
            reasoning_effort=ReasoningEffort.HIGH,
            request_id="request-fixed",
        )
        snapshot.verify_reconstruction(context=context, tools=(tool,))
        event_data = snapshot.to_event_data()
        self.assertTrue(event_data["payload_omitted"])
        event_json = json.dumps(event_data)
        self.assertNotIn("request_payload", event_json)
        self.assertNotIn("hello", event_json)
        self.assertNotIn("AQ==", event_json)
        self.assertEqual(context_fingerprints(items).dynamic, snapshot.dynamic_context_fingerprint)
        with self.assertRaisesRegex(ValueError, "reconstruction mismatch"):
            snapshot.verify_reconstruction(
                context=ModelContext.from_messages((Message(Role.USER, "changed"),)), tools=(tool,)
            )


class _Provider:
    provider_name = "fixture"
    model_name = "model"
    context_affinity = None
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def stream(
        self,
        context: ModelContext,
        tools: tuple[ToolDefinition, ...],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[object]:
        del context, tools, tool_policy
        self.calls += 1
        raise self.error
        yield


class RuntimeParityResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_authentication_errors_are_not_retried(self) -> None:
        provider = _Provider(ProviderError("authentication failed"))
        resilient = ResilientModelProvider(provider, max_attempts=3, backoff_seconds=0)
        with self.assertRaisesRegex(ProviderError, "authentication"):
            await anext(resilient.stream(ModelContext.from_messages(()), ()))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(resilient.health.last_error_type, "ProviderError")

    async def test_provider_circuit_opens_after_repeated_failures(self) -> None:
        provider = _Provider(TimeoutError("timeout"))
        resilient = ResilientModelProvider(
            provider,
            max_attempts=1,
            backoff_seconds=0,
            failure_threshold=2,
            cooldown_seconds=10,
        )
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                await anext(resilient.stream(ModelContext.from_messages(()), ()))
        self.assertTrue(resilient.health.circuit_open)
        with self.assertRaisesRegex(ProviderError, "circuit"):
            await anext(resilient.stream(ModelContext.from_messages(()), ()))
        with patch(
            "neuro_code.infrastructure.providers.resilience.monotonic",
            return_value=resilient._opened_until + 1,
        ):
            self.assertFalse(resilient.health.circuit_open)

    async def test_provider_configuration_error_is_not_retryable(self) -> None:
        provider = _Provider(ConfigurationError("bad configuration"))
        resilient = ResilientModelProvider(provider, max_attempts=3, backoff_seconds=0)
        with self.assertRaises(ConfigurationError):
            await anext(resilient.stream(ModelContext.from_messages(()), ()))
        self.assertEqual(provider.calls, 1)

    async def test_provider_retry_backoff_is_bounded_before_final_failure(self) -> None:
        provider = _Provider(TimeoutError("timeout"))
        resilient = ResilientModelProvider(provider, max_attempts=2, backoff_seconds=0.001)
        with self.assertRaises(TimeoutError):
            await anext(resilient.stream(ModelContext.from_messages(()), ()))
        self.assertEqual(provider.calls, 2)

    async def test_catalog_rejects_invalid_cache_and_non_network_fallback(self) -> None:
        class Catalog:
            def __init__(self) -> None:
                self.error: ProviderCatalogError | None = None

            async def discover_models(
                self,
                spec: ProviderConnectionSpec,
                *,
                http_policy: HttpClientPolicy,
            ) -> ProviderCatalogResult:
                del spec, http_policy
                if self.error is not None:
                    raise self.error
                return ProviderCatalogResult(("model",))

        delegate = Catalog()
        spec = ProviderConnectionSpec(
            protocol="openai-chat",
            base_url="https://provider.invalid/v1",
            api_key="secret",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text("not-json", encoding="utf-8")
            catalog = PersistentProviderCatalog(delegate, path)
            await catalog.discover_models(spec, http_policy=HttpClientPolicy(trust_env=False))
            delegate.error = ProviderCatalogError("auth", detail="unauthorized")
            with self.assertRaises(ProviderCatalogError):
                await catalog.discover_models(spec, http_policy=HttpClientPolicy(trust_env=False))
            path.write_text(
                json.dumps({"schema_version": 1, "entries": {"bad": "entry"}}),
                encoding="utf-8",
            )
            delegate.error = ProviderCatalogError("network", detail="offline")
            with self.assertRaises(ProviderCatalogError):
                await catalog.discover_models(spec, http_policy=HttpClientPolicy(trust_env=False))

    async def test_catalog_cache_ttl_validation_staleness_and_eviction(self) -> None:
        class Catalog:
            async def discover_models(
                self,
                spec: ProviderConnectionSpec,
                *,
                http_policy: HttpClientPolicy,
            ) -> ProviderCatalogResult:
                del spec, http_policy
                return ProviderCatalogResult(("fresh",))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            spec = ProviderConnectionSpec(
                protocol="openai-chat",
                base_url="https://provider.invalid/v1",
                api_key="fixture-key",
            )
            with self.assertRaises(ValueError):
                PersistentProviderCatalog(Catalog(), path, ttl_seconds=0)
            catalog = PersistentProviderCatalog(Catalog(), path, ttl_seconds=1)
            catalog._write(spec, ProviderCatalogResult(("cached",)))
            payload = json.loads(path.read_text(encoding="utf-8"))
            saved_at = payload["entries"][catalog._key(spec)]["saved_at"]
            with patch(
                "neuro_code.infrastructure.providers.catalog_cache.time.time",
                return_value=saved_at + 2,
            ):
                self.assertIsNone(catalog._cached(spec))
            key = catalog._key(spec)
            for entry in (
                "invalid",
                {"saved_at": "now", "models": []},
                {"saved_at": saved_at, "models": ["model"], "truncated": "yes"},
                {"saved_at": saved_at, "models": [1]},
                {"saved_at": saved_at, "models": [""]},
            ):
                path.write_text(
                    json.dumps({"schema_version": 1, "entries": {key: entry}}),
                    encoding="utf-8",
                )
                self.assertIsNone(catalog._cached(spec))
            catalog.max_entries = 1
            catalog._write(spec, ProviderCatalogResult(("one",)))
            second = ProviderConnectionSpec(
                protocol="openai-chat",
                base_url="https://provider.invalid/v2",
                api_key="fixture-key",
            )
            catalog._write(second, ProviderCatalogResult(("two",)))
            self.assertEqual(
                len(json.loads(path.read_text(encoding="utf-8"))["entries"]),
                1,
            )

    def test_provider_module_lazy_exports_are_bounded(self) -> None:
        import neuro_code.infrastructure.providers as providers

        for name in (
            "HttpProviderCatalog",
            "PersistentProviderCatalog",
            "ResilientModelProvider",
            "ProviderHealth",
            "JsonProviderSettingsStore",
            "AnthropicProvider",
            "FailoverModelProvider",
            "GeminiProvider",
            "GeminiInteractionsProvider",
            "OpenAICompatibleProvider",
            "OpenAIResponsesProvider",
            "ProviderCandidate",
        ):
            self.assertIsNotNone(providers.__getattr__(name))
        with self.assertRaises(AttributeError):
            providers.__getattr__("not_" + "a_provider")

    def test_provider_resilience_configuration_and_health_edges_are_bounded(self) -> None:
        provider = _Provider(TimeoutError("timeout"))
        for keyword, value in (
            ("max_attempts", 0),
            ("backoff_seconds", -1),
            ("failure_threshold", 0),
            ("cooldown_seconds", 0),
        ):
            with self.subTest(keyword=keyword), self.assertRaises(ValueError):
                ResilientModelProvider(provider, **{keyword: value})  # type: ignore[arg-type]
        resilient = ResilientModelProvider(provider, max_attempts=1, backoff_seconds=0)
        self.assertEqual(resilient.provider_name, "fixture")
        self.assertEqual(resilient.model_name, "model")
        self.assertIsNone(resilient.context_affinity)
        self.assertEqual(resilient.capabilities, ModelCapabilitySet.all_unknown())
        self.assertEqual(resilient.health.to_dict()["attempts"], 0)


class RuntimeParityAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_scheduler_rejects_unknown_tool_and_empty_input(self) -> None:
        class Tools:
            def get(self, name: str) -> None:
                del name
                return None

            def definitions(self) -> tuple[ToolDefinition, ...]:
                return ()

        scheduler = ToolScheduler(Tools())
        self.assertEqual(await scheduler.run((), lambda call, isolated: asyncio.sleep(0)), ())

        async def fail(call: ToolCall, isolated: bool) -> str:
            del call, isolated
            raise ValueError("missing")

        with self.assertRaisesRegex(ToolBatchExecutionError, "missing"):
            await scheduler.run(
                (ToolCall("id", "missing", {}),),
                fail,
            )


if __name__ == "__main__":
    unittest.main()
