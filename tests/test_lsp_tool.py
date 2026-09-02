from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from neuro_code.application.ports.configuration import AppConfig
from neuro_code.application.ports.lsp import LanguageServerProfile
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.lsp.manager import LanguageServerManager
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox
from neuro_code.infrastructure.tools.lsp import LspTool

_FIXTURE = Path(__file__).parent / "fixtures" / "fake_lsp_server.py"


class LspToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_uses_the_canonical_filesystem_plan_and_stays_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "main.py"
            path.write_text("value\n", encoding="utf-8")
            config = AppConfig(
                cwd=root,
                state_dir=root / ".state",
                providers={},
                default_provider=None,
                selected_provider=None,
                language_servers={
                    "fake": LanguageServerProfile(
                        name="fake",
                        language="python",
                        command=(sys.executable, str(_FIXTURE), "--mode", "normal"),
                        extensions=(".py",),
                    )
                },
                sandbox_profile=SandboxProfile.OFF,
            )
            manager = LanguageServerManager(
                config=config,
                local_process_sandbox=ProcessTreeLocalProcessSandbox(),
                workspace_root=root,
            )
            tool = LspTool(manager)
            arguments = {"operation": "hover", "path": "main.py", "line": 1, "column": 1}
            plan = tool.prepare_filesystem_targets(arguments, ToolContext(root))
            self.assertIsNotNone(plan)
            assert plan is not None
            result = await tool.execute(
                arguments,
                ToolContext(root, filesystem_access_plan=plan),
            )
            self.assertFalse(result.is_error)
            self.assertIn("fake hover", result.content)
            self.assertEqual(path.read_text(encoding="utf-8"), "value\n")
            await manager.close()

    async def test_unconfigured_service_returns_typed_error_projection(self) -> None:
        tool = LspTool()
        result = await tool.execute({"operation": "status"}, ToolContext(Path.cwd()))
        self.assertTrue(result.is_error)
        self.assertEqual(result.metadata["error_kind"], "not_configured")  # type: ignore[index]

    def test_write_operations_do_not_enter_the_stable_schema(self) -> None:
        properties = LspTool().definition.input_schema["properties"]
        self.assertNotIn("rename", properties)
        self.assertNotIn("format", properties)
        self.assertNotIn("applyEdit", properties)


if __name__ == "__main__":
    unittest.main()
