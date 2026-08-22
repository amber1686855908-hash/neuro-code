from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neuro_code.application.permissions.policy import PermissionDecision, PermissionEffect
from neuro_code.application.ports.lsp import (
    LanguageServerProfile,
    LspError,
    LspFailureKind,
    LspOperation,
    LspRequest,
)
from neuro_code.configuration.app import AppConfig
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.lsp.manager import LanguageServerManager
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox

_FIXTURE = Path(__file__).parent / "fixtures" / "fake_lsp_server.py"


def _config(root: Path, mode: str = "normal") -> AppConfig:
    profile = LanguageServerProfile(
        name="fake",
        language="python",
        command=(sys.executable, str(_FIXTURE), "--mode", mode),
        extensions=(".py",),
    )
    return AppConfig(
        cwd=root,
        state_dir=root / ".state",
        providers={},
        default_provider=None,
        selected_provider=None,
        sandbox_profile=SandboxProfile.OFF,
        language_servers={"fake": profile},
    )


class LspServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.path = self.root / "main.py"
        self.path.write_text("汉😀x\n", encoding="utf-8")

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    async def _manager(self, mode: str = "normal") -> LanguageServerManager:
        return LanguageServerManager(
            config=_config(self.root, mode),
            local_process_sandbox=ProcessTreeLocalProcessSandbox(),
            workspace_root=self.root,
        )

    async def test_semantic_operations_are_projected_and_bounded(self) -> None:
        manager = await self._manager()
        try:
            status = await manager.execute(LspRequest(LspOperation.STATUS))
            self.assertEqual(status.payload["profiles"].__class__, list)

            definition = await manager.execute(
                LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
            )
            self.assertEqual(definition.payload["locations"][0]["path"], "main.py")  # type: ignore[index]
            self.assertEqual(definition.payload["omitted_count"], 1)
            self.assertEqual(definition.payload["position_encoding"], "utf-16")

            hover = await manager.execute(
                LspRequest(LspOperation.HOVER, path=self.path, line=1, column=1)
            )
            self.assertNotIn("<script>", hover.payload["hover"])
            self.assertIn("fake hover", hover.payload["hover"])

            symbols = await manager.execute(
                LspRequest(LspOperation.DOCUMENT_SYMBOLS, path=self.path)
            )
            self.assertEqual(symbols.payload["symbols"][0]["name"], "fakeSymbol")  # type: ignore[index]

            workspace_symbols = await manager.execute(
                LspRequest(LspOperation.WORKSPACE_SYMBOLS, query="fake")
            )
            self.assertEqual(workspace_symbols.payload["locations"][0]["name"], "fakeSymbol")  # type: ignore[index]

            diagnostics = await manager.execute(
                LspRequest(LspOperation.DIAGNOSTICS, path=self.path)
            )
            self.assertEqual(
                diagnostics.payload["diagnostics"][0]["message"], "fake pull diagnostic"
            )  # type: ignore[index]

            self.path.write_text("changed\n", encoding="utf-8")
            await manager.execute(
                LspRequest(LspOperation.REFERENCES, path=self.path, line=1, column=1)
            )
            ready = await manager.execute(LspRequest(LspOperation.STATUS))
            profile_status = ready.payload["profiles"][0]  # type: ignore[index]
            self.assertEqual(profile_status["state"], "ready")  # type: ignore[index]
        finally:
            await manager.close()

    async def test_server_requests_cannot_apply_workspace_edits(self) -> None:
        original = self.path.read_text(encoding="utf-8")
        manager = await self._manager("apply-edit")
        try:
            await manager.execute(
                LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
            )
            self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        finally:
            await manager.close()

    async def test_client_server_requests_and_stderr_are_bounded(self) -> None:
        manager = await self._manager("server-request-all")
        try:
            await manager.execute(
                LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
            )
            status = await manager.execute(LspRequest(LspOperation.STATUS))
            self.assertEqual(status.payload["profiles"][0]["state"], "ready")  # type: ignore[index]
        finally:
            await manager.close()

        manager = LanguageServerManager(
            config=_config(self.root, "stderr-spam"),
            local_process_sandbox=ProcessTreeLocalProcessSandbox(),
            workspace_root=self.root,
            redaction_values=("stderr-noise",),
        )
        try:
            await manager.execute(
                LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
            )
            status = await manager.execute(LspRequest(LspOperation.STATUS))
            stderr = status.payload["profiles"][0]["stderr"]  # type: ignore[index]
            self.assertLessEqual(len(stderr.encode("utf-8")), 4 * 1024)  # type: ignore[union-attr]
            self.assertNotIn("stderr-noise", stderr)  # type: ignore[operator]
        finally:
            await manager.close()

    async def test_pull_diagnostics_falls_back_to_bounded_publish_wait(self) -> None:
        manager = await self._manager("no-publish")
        try:
            with patch(
                "neuro_code.infrastructure.lsp.manager.LSP_DIAGNOSTIC_WAIT_SECONDS",
                0.01,
            ):
                diagnostics = await manager.execute(
                    LspRequest(LspOperation.DIAGNOSTICS, path=self.path)
                )
            self.assertEqual(diagnostics.payload["diagnostics"], [])
        finally:
            await manager.close()

    async def test_protocol_failure_modes_are_typed(self) -> None:
        for mode in ("malformed-header", "oversized"):
            manager = await self._manager(mode)
            try:
                with self.assertRaises(LspError) as raised:
                    await manager.execute(
                        LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
                    )
                self.assertEqual(raised.exception.kind, LspFailureKind.PROTOCOL_ERROR)
            finally:
                await manager.close()

    async def test_crash_status_and_restart_cooldown_are_bounded(self) -> None:
        manager = await self._manager("crash")
        try:
            with self.assertRaises(LspError) as raised:
                await manager.execute(
                    LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
                )
            self.assertEqual(raised.exception.kind, LspFailureKind.SERVER_CRASH)
            status = await manager.execute(LspRequest(LspOperation.STATUS))
            self.assertEqual(status.payload["profiles"][0]["state"], "crashed")  # type: ignore[index]
            with self.assertRaises(LspError) as cooldown:
                await manager.execute(
                    LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
                )
            self.assertEqual(cooldown.exception.kind, LspFailureKind.SERVER_CRASH)
            await asyncio.sleep(0.55)
            with self.assertRaises(LspError):
                await manager.execute(
                    LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
                )
        finally:
            await manager.close()

    async def test_configuration_and_capability_failures_are_typed(self) -> None:
        manager = LanguageServerManager(
            config=AppConfig(
                cwd=self.root,
                state_dir=self.root / ".state",
                providers={},
                default_provider=None,
                selected_provider=None,
                sandbox_profile=SandboxProfile.OFF,
            ),
            local_process_sandbox=ProcessTreeLocalProcessSandbox(),
            workspace_root=self.root,
        )
        with self.assertRaises(LspError) as not_configured:
            await manager.execute(
                LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
            )
        self.assertEqual(not_configured.exception.kind, LspFailureKind.NOT_CONFIGURED)
        await manager.close()

        minimal = await self._manager("minimal")
        try:
            with self.assertRaises(LspError) as unsupported:
                await minimal.execute(
                    LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
                )
            self.assertEqual(unsupported.exception.kind, LspFailureKind.UNSUPPORTED_CAPABILITY)
        finally:
            await minimal.close()

    async def test_direct_service_rejects_outside_and_invalid_documents(self) -> None:
        manager = await self._manager()
        outside = self.root.parent / "outside.py"
        outside.write_text("outside\n", encoding="utf-8")
        try:
            with self.assertRaises(LspError) as escaped:
                await manager.execute(
                    LspRequest(LspOperation.DEFINITION, path=outside, line=1, column=1)
                )
            self.assertEqual(escaped.exception.kind, LspFailureKind.SECURITY_FILTERED)

            invalid = self.root / "invalid.py"
            invalid.write_bytes(b"\xff\xfe")
            with self.assertRaises(LspError) as invalid_error:
                await manager.execute(LspRequest(LspOperation.DIAGNOSTICS, path=invalid))
            self.assertEqual(invalid_error.exception.kind, LspFailureKind.DOCUMENT_ERROR)
        finally:
            outside.unlink(missing_ok=True)
            await manager.close()

    async def test_visibility_policy_can_filter_server_locations(self) -> None:
        class DenyPolicy:
            def decide_targets(self, tool_name, targets, *, side_effecting):
                return PermissionDecision(PermissionEffect.DENY, "test deny")

        manager = await self._manager()
        try:
            result = await manager.execute(
                LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1),
                visibility_policy=DenyPolicy(),
            )
            self.assertEqual(result.payload["locations"], [])
            self.assertEqual(result.payload["omitted_count"], 2)
        finally:
            await manager.close()

    async def test_malformed_protocol_becomes_typed_failure(self) -> None:
        manager = await self._manager("malformed-json")
        try:
            with self.assertRaises(LspError) as raised:
                await manager.execute(
                    LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
                )
            self.assertEqual(raised.exception.kind, LspFailureKind.PROTOCOL_ERROR)
        finally:
            await manager.close()

    async def test_explicit_restart_is_bounded_and_lazy(self) -> None:
        manager = await self._manager()
        try:
            await manager.execute(
                LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
            )
            restarted = await manager.execute(LspRequest(LspOperation.RESTART, profile="fake"))
            self.assertEqual(restarted.payload["state"], "stopped")
            status = await manager.execute(LspRequest(LspOperation.STATUS))
            self.assertEqual(status.payload["profiles"][0]["state"], "stopped")  # type: ignore[index]
        finally:
            await manager.close()


if __name__ == "__main__":
    unittest.main()
