from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import cast
from unittest.mock import patch

from neuro_code.application import ApplicationComposition, ApplicationSettings
from neuro_code.config import AppConfig
from neuro_code.domain.model_context import ModelContext
from neuro_code.domain.model_events import ModelEvent
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.tools import ToolDefinition
from neuro_code.ports.background_tasks import (
    BackgroundTaskManager,
    BackgroundTaskSupervisor,
)
from neuro_code.ports.model import ModelProvider
from neuro_code.workspace import workspaces_match


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

    async def test_failed_open_closes_the_process_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            self._write_config(state)
            supervisor = ApplicationSupervisorFixture()

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
                    process_sandbox_enforcer=lambda *args: None,
                    background_supervisor_factory=lambda: cast(
                        BackgroundTaskSupervisor,
                        supervisor,
                    ),
                )

        self.assertEqual(supervisor.shutdown_calls, 1)


if __name__ == "__main__":
    unittest.main()
