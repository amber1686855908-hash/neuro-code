from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import neuro_code.configuration.app as config_module
from neuro_code.application.execution_policy import ExecutionProfile
from neuro_code.application.memory.compaction_runtime import ContextCompactionRuntimeGate
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionMode,
    PermissionRule,
)
from neuro_code.application.permissions.service import ToolApprovalService
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.background_tasks import (
    BackgroundTaskManager,
    BackgroundTaskSupervisor,
)
from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider
from neuro_code.application.ports.sandbox import LocalProcessSandbox
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.sessions import GetSessionSummaryRequest, SessionApplicationService
from neuro_code.application.sessions.summary import SessionSummaryQueryService
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.workflows import IsolatedSubagentExecutionService, SubagentCapabilitySet
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.configuration.app import AppConfig, ProviderProfile
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelEvent
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.sessions import SessionSummary
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.providers.gemini_interactions import (
    GeminiInteractionsProvider,
)
from neuro_code.infrastructure.providers.openai_compatible import OpenAICompatibleProvider
from neuro_code.infrastructure.workspace.paths import workspaces_match
from neuro_code.shared.errors import ConfigurationError, ToolError
from tests.fakes import EmptyWorkspaceChangeObserver


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


class ApplicationProviderFixture:
    provider_name = "fixture"
    model_name = "fixture-model"

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]:
        del context, tools
        if False:
            yield


class ApplicationCapabilityProviderFixture(ApplicationProviderFixture):
    def __init__(self, capabilities: ModelCapabilitySet) -> None:
        self.capabilities = capabilities


class ApplicationToolFixture:
    side_effecting = True

    def __init__(self, name: str) -> None:
        self.definition = ToolDefinition(
            name,
            "Application composition fixture",
            {"type": "object", "properties": {}},
        )

    async def execute(self, arguments: object, context: object) -> ToolResult:
        del arguments, context
        return ToolResult("ok")


class ApplicationTaskScopeFixture:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.closed = False

    async def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.shutdown_calls += 1


class ApplicationSupervisorFixture:
    def __init__(self) -> None:
        self.scopes: list[ApplicationTaskScopeFixture] = []
        self.local_process_sandboxes: list[LocalProcessSandbox | None] = []
        self.shutdown_calls = 0

    def open_scope(
        self,
        *,
        local_process_sandbox: LocalProcessSandbox | None = None,
    ) -> BackgroundTaskManager:
        self.local_process_sandboxes.append(local_process_sandbox)
        scope = ApplicationTaskScopeFixture()
        self.scopes.append(scope)
        return cast(BackgroundTaskManager, scope)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        for scope in self.scopes:
            await scope.shutdown()


class OrderedSessionStoreFixture:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def peek_session_sandbox_profile(
        self,
        session_id: str,
    ) -> SandboxProfile | None:
        del session_id
        self._calls.append("peek session sandbox")
        return SandboxProfile.WORKSPACE

    async def initialize(self) -> None:
        self._calls.append("initialize session store")


class ApplicationCompositionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _write_config(state: Path) -> None:
        state.mkdir(exist_ok=True)
        (state / "config.toml").write_text(
            """
[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"
context_window_tokens = 131072

[providers.alternate]
protocol = "openai-chat"
model = "alternate-model"
base_url = "https://alternate.invalid/v1"
api_key_env = "FIXTURE_KEY"
context_window_tokens = 65536
""",
            encoding="utf-8",
        )

    async def test_idle_lsp_service_does_not_delay_composition_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                )
                await application.create_binding()
                self.assertEqual(len(application._lsp_services), 1)
                await asyncio.wait_for(application.close(), timeout=1.0)
                self.assertEqual(application._lsp_services, set())

    async def test_capability_bound_child_uses_exact_registry_and_context_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                )
                try:
                    capabilities = SubagentCapabilitySet.from_runtime(
                        tool_names=(
                            "read_file",
                            "read_files",
                            "list_dir",
                            "list_tree",
                            "glob",
                            "grep",
                            "grep_many",
                            "skill",
                        ),
                        cwd=root,
                        sandbox_profile=SandboxProfile.OFF,
                        enable_background_tasks=False,
                        max_steps=3,
                    )
                    binding = await application.create_binding(capabilities=capabilities)
                    self.assertEqual(binding.capabilities, capabilities)
                    self.assertEqual(
                        frozenset(binding.runner._runtime._tools.names()),
                        capabilities.allowed_tool_names,
                    )
                    self.assertIsNone(binding.runner._runtime._tool_context.background_tasks)
                finally:
                    await application.close()

    @staticmethod
    def _write_gemini_web_config(
        state: Path,
        *,
        mode: str,
        main_model: str,
        include_search_route: bool,
    ) -> None:
        state.mkdir(exist_ok=True)
        route = '\n[routing.web_search]\nprofile = "search"\n' if include_search_route else ""
        search_profile = (
            (
                "\n[providers.search]\n"
                'protocol = "gemini-interactions"\n'
                'service_id = "google-ai-studio"\n'
                'model = "gemini-3.6-flash"\n'
                'base_url = "https://generativelanguage.googleapis.com/v1beta"\n'
                'api_key_env = "SEARCH_KEY"\n'
                'builtin_tools = ["google_search"]\n'
                'proxy_mode = "direct"\n'
            )
            if include_search_route
            else ""
        )
        (state / "config.toml").write_text(
            f"""
[web_search]
mode = "{mode}"

[routing]
default = "main"
{route}
[providers.main]
protocol = "gemini-interactions"
service_id = "google-ai-studio"
model = "{main_model}"
base_url = "https://generativelanguage.googleapis.com/v1beta"
api_key_env = "GEMINI_KEY"
builtin_tools = ["google_search"]
proxy_mode = "direct"
{search_profile}
""",
            encoding="utf-8",
        )

    async def test_gemini_inline_and_auto_sidecar_paths_are_composition_aware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_gemini_web_config(
                state,
                mode="inline",
                main_model="gemini-3.6-flash",
                include_search_route=False,
            )
            inline_calls: list[ProviderProfile] = []

            def inline_factory(config: AppConfig, failover: bool) -> ModelProvider:
                del failover
                inline_calls.append(config.provider)
                return ApplicationCapabilityProviderFixture(
                    GeminiInteractionsProvider.implementation_capabilities(
                        model=config.provider.model,
                        builtin_tools=config.provider.builtin_tools,
                    )
                )

            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "GEMINI_KEY": "gemini-key",
                },
                clear=True,
            ):
                inline_application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=inline_factory,
                )
                inline_binding = await inline_application.create_binding()
                self.assertNotIn("web_search", inline_binding.runner._runtime._tools.names())
                self.assertEqual(len(inline_calls), 1)
                self.assertEqual(inline_calls[0].builtin_tools, ("google_search",))
                await inline_application.close()

            self._write_gemini_web_config(
                state,
                mode="auto",
                main_model="gemini-2.5-flash",
                include_search_route=True,
            )
            sidecar_calls: list[ProviderProfile] = []

            def sidecar_factory(config: AppConfig, failover: bool) -> ModelProvider:
                del failover
                sidecar_calls.append(config.provider)
                return ApplicationCapabilityProviderFixture(
                    GeminiInteractionsProvider.implementation_capabilities(
                        model=config.provider.model,
                        builtin_tools=config.provider.builtin_tools,
                    )
                )

            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "GEMINI_KEY": "gemini-key",
                    "SEARCH_KEY": "search-key",
                },
                clear=True,
            ):
                sidecar_application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=sidecar_factory,
                )
                sidecar_binding = await sidecar_application.create_binding()
                self.assertIn("web_search", sidecar_binding.runner._runtime._tools.names())
                self.assertEqual(
                    [profile.builtin_tools for profile in sidecar_calls],
                    [("google_search",), ()],
                )
                await sidecar_application.close()

    async def test_china_main_profiles_use_local_web_search_sidecar_and_local_fetch(self) -> None:
        cases = (
            ("kimi", "kimi", "kimi-k2.6", "KIMI_KEY"),
            ("glm", "glm", "glm-5.3", "GLM_KEY"),
            ("minimax", "minimax", "MiniMax-M3", "MINIMAX_KEY"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            for service_id, dialect, model, api_key_env in cases:
                (state / "config.toml").write_text(
                    f'''
[web_search]
mode = "sidecar"

[web_fetch]
mode = "local"

[routing]
default = "main"

[routing.web_search]
profile = "search"

[providers.main]
service_id = "{service_id}"
protocol = "openai-chat"
dialect = "{dialect}"
model = "{model}"
base_url = "https://provider.invalid/v1"
api_key_env = "{api_key_env}"
proxy_mode = "direct"

[providers.search]
service_id = "google-ai-studio"
protocol = "gemini-interactions"
model = "gemini-3.6-flash"
base_url = "https://generativelanguage.googleapis.com/v1beta"
api_key_env = "SEARCH_KEY"
builtin_tools = ["google_search"]
proxy_mode = "direct"
''',
                    encoding="utf-8",
                )
                calls: list[ProviderProfile] = []

                def provider_factory(
                    config: AppConfig,
                    failover: bool,
                    calls: list[ProviderProfile] = calls,
                ) -> ModelProvider:
                    del failover
                    calls.append(config.provider)
                    return ApplicationCapabilityProviderFixture(
                        OpenAICompatibleProvider.implementation_capabilities(
                            dialect=config.provider.dialect,
                        )
                    )

                with patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        api_key_env: "main-key",
                        "SEARCH_KEY": "search-key",
                    },
                    clear=True,
                ):
                    application = await ApplicationComposition.open(
                        ApplicationSettings(cwd=root),
                        provider_factory=provider_factory,
                    )
                    binding = await application.create_binding()
                    try:
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(calls[0].service_id, service_id)
                        self.assertEqual(calls[0].dialect, dialect)
                        self.assertIn("web_search", binding.runner._runtime._tools.names())
                        self.assertIn("web_fetch", binding.runner._runtime._tools.names())
                    finally:
                        await application.close()

    def test_application_settings_default_to_finalize_terminal(self) -> None:
        settings = ApplicationSettings()

        self.assertIs(settings.execution_control_mode, ExecutionControlMode.FINALIZE_TERMINAL)
        self.assertIs(settings.execution_profile, ExecutionProfile.NORMAL)
        self.assertEqual(settings.max_steps, 48)
        self.assertEqual(settings.execution_budget.max_model_calls, 48)
        self.assertEqual(settings.execution_budget.max_tool_rounds, 48)
        self.assertEqual(settings.execution_budget.max_tool_calls, 192)
        self.assertIs(settings.reasoning_effort, ReasoningEffort.HIGH)

    def test_application_settings_select_deep_or_legacy_step_budget(self) -> None:
        deep = ApplicationSettings(execution_profile=ExecutionProfile.DEEP)
        compatibility = ApplicationSettings(
            execution_profile=ExecutionProfile.DEEP,
            max_steps=60,
        )

        self.assertEqual(deep.max_steps, 96)
        self.assertEqual(deep.execution_budget.max_tool_rounds, 96)
        self.assertEqual(deep.execution_budget.max_tool_calls, 384)
        self.assertEqual(compatibility.max_steps, 60)
        self.assertEqual(compatibility.execution_budget.max_tool_rounds, 60)
        self.assertEqual(compatibility.execution_budget.max_tool_calls, 240)

    def test_application_settings_can_select_observe_only(self) -> None:
        settings = ApplicationSettings(execution_control_mode=ExecutionControlMode.OBSERVE_ONLY)

        self.assertIs(settings.execution_control_mode, ExecutionControlMode.OBSERVE_ONLY)

    async def test_composition_passes_execution_control_mode_to_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            environment = {
                "HOME": str(root),
                "NEURO_CODE_HOME": str(state),
                "FIXTURE_KEY": "fixture-key",
            }
            with patch.dict("os.environ", environment, clear=True):
                default_application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                )
                self.assertIsInstance(
                    default_application.session_service,
                    SessionApplicationService,
                )
                default_binding = await default_application.create_binding()
                self.assertIs(
                    default_binding.runner._runtime._execution_control_mode,
                    ExecutionControlMode.FINALIZE_TERMINAL,
                )
                self.assertIsInstance(
                    default_binding.runner._runtime._compaction_runtime_gate,
                    ContextCompactionRuntimeGate,
                )
                provider_window = (
                    default_binding.runner._runtime._loop_runner._provider_context_window
                )
                self.assertIsNotNone(provider_window)
                assert provider_window is not None
                self.assertEqual(provider_window.provider_name, "fixture")
                self.assertEqual(provider_window.model_name, "fixture-model")
                self.assertEqual(provider_window.capacity_tokens, 131_072)
                self.assertEqual(
                    (
                        default_binding.runner._runtime._execution_budget.max_model_calls,
                        default_binding.runner._runtime._execution_budget.max_tool_rounds,
                        default_binding.runner._runtime._execution_budget.max_tool_calls,
                    ),
                    (48, 48, 192),
                )
                await default_application.close()

                observe_application = await ApplicationComposition.open(
                    ApplicationSettings(
                        cwd=root,
                        execution_control_mode=ExecutionControlMode.OBSERVE_ONLY,
                    ),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                )
                observe_binding = await observe_application.create_binding()
                self.assertIs(
                    observe_binding.runner._runtime._execution_control_mode,
                    ExecutionControlMode.OBSERVE_ONLY,
                )
                self.assertIsInstance(
                    observe_binding.runner._runtime._compaction_runtime_gate,
                    ContextCompactionRuntimeGate,
                )
                await observe_application.close()

    async def test_composition_injects_the_canonical_local_process_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            process_sandbox = cast(LocalProcessSandbox, object())
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                    local_process_sandbox_factory=lambda profile, cwd, state_dir: process_sandbox,
                )
                try:
                    binding = await application.create_binding()
                    self.assertIs(
                        binding.runner._runtime._tool_context.local_process_sandbox,
                        process_sandbox,
                    )
                finally:
                    await application.close()

    async def test_composition_shares_one_process_launcher_with_its_background_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            process_sandbox = cast(LocalProcessSandbox, object())
            supervisor = ApplicationSupervisorFixture()
            factory_calls: list[tuple[SandboxProfile, Path, Path]] = []

            def process_sandbox_factory(
                profile: SandboxProfile,
                cwd: Path,
                state_dir: Path,
            ) -> LocalProcessSandbox:
                factory_calls.append((profile, cwd, state_dir))
                return process_sandbox

            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor, supervisor
                    ),
                    local_process_sandbox_factory=process_sandbox_factory,
                )
                try:
                    binding = await application.create_binding()
                    self.assertEqual(
                        factory_calls,
                        [
                            (
                                SandboxProfile.OFF,
                                _canonical_path(root),
                                _canonical_path(state),
                            )
                        ],
                    )
                    self.assertIs(supervisor.local_process_sandboxes[0], process_sandbox)
                    self.assertIs(
                        binding.runner._runtime._tool_context.local_process_sandbox,
                        process_sandbox,
                    )
                finally:
                    await application.close()

    async def test_read_only_subagent_binding_has_only_inspection_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                )
                binding = await application.create_binding(
                    max_steps=3,
                    allowed_tool_names=("read_file", "list_dir", "grep", "skill"),
                    enable_background_tasks=False,
                )
                runtime = binding.runner._runtime
                self.assertEqual(
                    runtime._tools.names(),
                    ("read_file", "list_dir", "grep", "skill"),
                )
                self.assertIsNone(runtime._tool_context.background_tasks)
                self.assertEqual(
                    (
                        runtime._execution_budget.max_model_calls,
                        runtime._execution_budget.max_tool_rounds,
                        runtime._execution_budget.max_tool_calls,
                    ),
                    (3, 3, 12),
                )
                self.assertIsInstance(
                    application.create_read_only_subagent_service(),
                    IsolatedSubagentExecutionService,
                )
                await application.close()

    async def test_open_create_and_close_own_shared_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()

            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: cast(
                        ModelProvider,
                        ApplicationProviderFixture(),
                    ),
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                )
                binding = await application.create_binding(reasoning_effort=ReasoningEffort.LOW)
                self.assertEqual(binding.runner.reasoning_effort, ReasoningEffort.LOW)
                self.assertTrue(workspaces_match(application.config.cwd, root))
                await application.close()
                await application.close()

                with self.assertRaises(RuntimeError):
                    await application.create_binding()

        self.assertEqual(supervisor.shutdown_calls, 1)
        self.assertEqual(supervisor.scopes[0].shutdown_calls, 1)

    async def test_open_preserves_preflight_and_resource_creation_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            calls: list[str] = []
            store = OrderedSessionStoreFixture(calls)
            original_load_config = config_module.load_config
            original_override_provider = config_module.override_provider
            original_override_sandbox = config_module.override_sandbox
            original_pin_resumed_sandbox = config_module.pin_resumed_sandbox

            def load_config(cwd: Path | None) -> AppConfig:
                calls.append("load config")
                return original_load_config(cwd)

            def override_sandbox(config: AppConfig, sandbox: str | None) -> AppConfig:
                calls.append("override sandbox")
                return original_override_sandbox(config, sandbox)

            def override_provider(
                config: AppConfig,
                *,
                provider: str | None = None,
                model: str | None = None,
                base_url: str | None = None,
            ) -> AppConfig:
                calls.append("override provider")
                return original_override_provider(
                    config,
                    provider=provider,
                    model=model,
                    base_url=base_url,
                )

            def pin_resumed_sandbox(
                config: AppConfig,
                saved_profile: SandboxProfile | None,
            ) -> AppConfig:
                calls.append("pin resumed sandbox")
                return original_pin_resumed_sandbox(config, saved_profile)

            def store_factory(path: Path) -> OrderedSessionStoreFixture:
                del path
                calls.append("create session store")
                return store

            def instruction_discovery_factory() -> object:
                calls.append("create instruction discovery")
                return object()

            def skill_discovery_factory() -> object:
                calls.append("create skill discovery")
                return object()

            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                with (
                    patch("neuro_code.configuration.app.load_config", side_effect=load_config),
                    patch(
                        "neuro_code.configuration.app.override_sandbox",
                        side_effect=override_sandbox,
                    ),
                    patch(
                        "neuro_code.configuration.app.override_provider",
                        side_effect=override_provider,
                    ),
                    patch(
                        "neuro_code.configuration.app.pin_resumed_sandbox",
                        side_effect=pin_resumed_sandbox,
                    ),
                ):
                    application = await ApplicationComposition.open(
                        ApplicationSettings(
                            cwd=root,
                            sandbox="workspace",
                            resume_id="saved-session",
                        ),
                        store_factory=store_factory,
                        background_supervisor_factory=ApplicationSupervisorFixture,
                        instruction_discovery_factory=instruction_discovery_factory,
                        skill_discovery_factory=skill_discovery_factory,
                    )
                await application.close()

        self.assertEqual(
            calls,
            [
                "load config",
                "override sandbox",
                "override provider",
                "create session store",
                "peek session sandbox",
                "pin resumed sandbox",
                "initialize session store",
                "create instruction discovery",
                "create skill discovery",
            ],
        )

    async def test_failed_binding_closes_its_background_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()

            def fail_provider(config: AppConfig, failover: bool) -> ModelProvider:
                del config, failover
                raise RuntimeError("composition failed")

            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=fail_provider,
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                )
                with self.assertRaisesRegex(RuntimeError, "composition failed"):
                    await application.create_binding()
                await application.close()

        self.assertEqual(supervisor.scopes[0].shutdown_calls, 1)

    async def test_workspace_change_observer_factory_is_lazy_and_per_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()
            created: list[EmptyWorkspaceChangeObserver] = []

            def observer_factory() -> EmptyWorkspaceChangeObserver:
                observer = EmptyWorkspaceChangeObserver()
                created.append(observer)
                return observer

            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                    workspace_change_observer_factory=observer_factory,
                )
                self.assertEqual(created, [])
                await application.create_binding()
                await application.create_binding()
                await application.close()

        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])

    async def test_workspace_change_observer_factory_failure_closes_its_background_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()

            def fail_observer_factory() -> EmptyWorkspaceChangeObserver:
                raise RuntimeError("workspace observer factory failed")

            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: ApplicationProviderFixture(),
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                    workspace_change_observer_factory=fail_observer_factory,
                )
                with self.assertRaisesRegex(RuntimeError, "workspace observer factory failed"):
                    await application.create_binding()
                await application.close()

        self.assertEqual(supervisor.scopes[0].shutdown_calls, 1)

    async def test_additional_tools_force_interactive_approval_and_reject_collisions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(
                        cwd=root,
                        permission_mode=PermissionMode.BYPASS,
                        permission_rules=(PermissionRule(PermissionEffect.DENY, "remote_deny"),),
                    ),
                    provider_factory=lambda config, failover: cast(
                        ModelProvider,
                        ApplicationProviderFixture(),
                    ),
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                )
                binding = await application.create_binding(
                    approver=cast(PermissionApprover, object()),
                    additional_tools=(
                        ApplicationToolFixture("remote_ask"),
                        ApplicationToolFixture("remote_deny"),
                    ),
                )
                runtime = cast(Any, cast(Any, binding.runner)._runtime)
                self.assertIsInstance(runtime._approver, ToolApprovalService)
                ask = runtime._permissions.decide(
                    "remote_ask",
                    {},
                    side_effecting=True,
                )
                denied = runtime._permissions.decide(
                    "remote_deny",
                    {},
                    side_effecting=True,
                )
                self.assertEqual(ask.effect, PermissionEffect.ASK)
                self.assertEqual(denied.effect, PermissionEffect.DENY)

                with self.assertRaisesRegex(ToolError, "duplicate"):
                    await application.create_binding(
                        additional_tools=(ApplicationToolFixture("read_file"),)
                    )
                self.assertEqual(len(supervisor.scopes), 1)
                await application.close()

    async def test_resume_configuration_restores_provider_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: cast(
                        ModelProvider,
                        ApplicationProviderFixture(),
                    ),
                )
                restored_id = await application.store.create_session(
                    str(root),
                    "alternate",
                    "saved-model",
                )
                restored = await application.config_for_session_resume(restored_id)
                self.assertEqual(restored.selected_provider, "alternate")
                self.assertEqual(restored.provider.model, "saved-model")

                legacy_id = await application.store.create_session(
                    str(root),
                    "removed-profile",
                    "legacy-model",
                )
                legacy = await application.config_for_session_resume(legacy_id)
                self.assertEqual(
                    legacy.selected_provider,
                    application.config.selected_provider,
                )

                wrong_workspace = await application.store.create_session(
                    str(root / "other"),
                    "fixture",
                    "fixture-model",
                )
                with self.assertRaisesRegex(ConfigurationError, "workspace"):
                    await application.config_for_session_resume(wrong_workspace)

                wrong_sandbox = await application.store.create_session(
                    str(root),
                    "fixture",
                    "fixture-model",
                    sandbox_profile=SandboxProfile.WORKSPACE,
                )
                with self.assertRaisesRegex(ConfigurationError, "sandbox"):
                    await application.config_for_session_resume(wrong_sandbox)

                missing_affinity = await application.store.create_session(
                    str(root),
                    "removed-profile",
                    "legacy-model",
                    "profile-v1:missing",
                )
                with self.assertRaisesRegex(ConfigurationError, "provider"):
                    await application.config_for_session_resume(missing_affinity)
                await application.close()

    async def test_resume_configuration_uses_session_application_summary_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root),
                    provider_factory=lambda config, failover: cast(
                        ModelProvider,
                        ApplicationProviderFixture(),
                    ),
                )
                session_id = await application.store.create_session(
                    str(root),
                    "fixture",
                    "fixture-model",
                )
                requests: list[GetSessionSummaryRequest] = []
                original = SessionSummaryQueryService.get_session_summary

                async def capture(
                    service: SessionSummaryQueryService,
                    request: GetSessionSummaryRequest,
                ) -> SessionSummary:
                    requests.append(request)
                    return await original(service, request)

                with patch.object(SessionSummaryQueryService, "get_session_summary", new=capture):
                    await application.config_for_session_resume(session_id)

                self.assertEqual(requests, [GetSessionSummaryRequest(session_id)])
                await application.close()

    async def test_enabled_profile_does_not_reexec_the_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()
            with patch.dict(
                "os.environ",
                {
                    "HOME": str(root),
                    "NEURO_CODE_HOME": str(state),
                    "FIXTURE_KEY": "fixture-key",
                },
                clear=True,
            ):
                application = await ApplicationComposition.open(
                    ApplicationSettings(cwd=root, sandbox="workspace"),
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                )
                await application.close()

        self.assertEqual(supervisor.shutdown_calls, 1)


if __name__ == "__main__":
    unittest.main()
