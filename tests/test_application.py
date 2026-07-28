from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import neuro_code.config as config_module
from neuro_code.application.ports.approval import PermissionApprover
from neuro_code.application.ports.background_tasks import (
    BackgroundTaskManager,
    BackgroundTaskSupervisor,
)
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.settings import ApplicationSettings
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.config import AppConfig
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelEvent
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.permissions import (
    PermissionEffect,
    PermissionMode,
    PermissionRule,
)
from neuro_code.shared.errors import ConfigurationError, ToolError
from neuro_code.workspace import workspaces_match
from tests.fakes import EmptyWorkspaceChangeObserver


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
        self.shutdown_calls = 0

    def open_scope(self) -> BackgroundTaskManager:
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
        state.mkdir()
        (state / "config.toml").write_text(
            """
[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"

[providers.alternate]
protocol = "openai-chat"
model = "alternate-model"
base_url = "https://alternate.invalid/v1"
api_key_env = "FIXTURE_KEY"
""",
            encoding="utf-8",
        )

    async def test_open_create_and_close_own_shared_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()
            enforced: list[tuple[SandboxProfile, Path, Path, tuple[str, ...]]] = []

            def enforce(
                profile: SandboxProfile,
                cwd: Path,
                state_dir: Path,
                command: Sequence[str],
            ) -> None:
                enforced.append((profile, cwd, state_dir, tuple(command)))

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
                        launch_command=("neuro-code", "agent"),
                    ),
                    provider_factory=lambda config, failover: cast(
                        ModelProvider,
                        ApplicationProviderFixture(),
                    ),
                    shell_sandbox_factory=lambda profile, cwd, state_dir: None,
                    process_sandbox_enforcer=enforce,
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                )
                binding = await application.create_binding(reasoning_effort=ReasoningEffort.LOW)
                self.assertEqual(binding.runner.reasoning_effort, ReasoningEffort.LOW)
                self.assertTrue(workspaces_match(application.config.cwd, root))
                self.assertEqual(enforced[0][0], SandboxProfile.OFF)
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

            def enforce(*args: object) -> None:
                del args
                calls.append("enforce process sandbox")

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
                    patch("neuro_code.config.load_config", side_effect=load_config),
                    patch(
                        "neuro_code.config.override_sandbox",
                        side_effect=override_sandbox,
                    ),
                    patch(
                        "neuro_code.config.override_provider",
                        side_effect=override_provider,
                    ),
                    patch(
                        "neuro_code.config.pin_resumed_sandbox",
                        side_effect=pin_resumed_sandbox,
                    ),
                ):
                    application = await ApplicationComposition.open(
                        ApplicationSettings(
                            cwd=root,
                            sandbox="workspace",
                            resume_id="saved-session",
                            launch_command=("neuro-code", "agent"),
                        ),
                        process_sandbox_enforcer=enforce,
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
                "enforce process sandbox",
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
                    process_sandbox_enforcer=lambda *args: None,
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
                    process_sandbox_enforcer=lambda *args: None,
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
                    process_sandbox_enforcer=lambda *args: None,
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
                    process_sandbox_enforcer=lambda *args: None,
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
                    process_sandbox_enforcer=lambda *args: None,
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

    async def test_failed_open_closes_the_process_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()
            sandbox_preflight_calls = 0

            def enforce(*args: object) -> None:
                nonlocal sandbox_preflight_calls
                del args
                sandbox_preflight_calls += 1

            with (
                patch.dict(
                    "os.environ",
                    {
                        "HOME": str(root),
                        "NEURO_CODE_HOME": str(state),
                        "FIXTURE_KEY": "fixture-key",
                    },
                    clear=True,
                ),
                self.assertRaisesRegex(ValueError, "launch command"),
            ):
                await ApplicationComposition.open(
                    ApplicationSettings(cwd=root, sandbox="workspace"),
                    process_sandbox_enforcer=enforce,
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                )

        self.assertEqual(supervisor.shutdown_calls, 1)
        self.assertEqual(sandbox_preflight_calls, 0)


if __name__ == "__main__":
    unittest.main()
