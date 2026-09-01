from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from neuro_code.application.ports.lsp import (
    LanguageServerProfile,
    LspError,
    LspFailureKind,
    LspFailurePhase,
    LspOperation,
    LspOperationResult,
    LspRequest,
    LspStatus,
)
from neuro_code.configuration.app import AppConfig
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.lsp.manager import (
    MAX_LSP_RESULT_ITEMS,
    MAX_LSP_SYMBOL_DEPTH,
    LanguageServerManager,
    _Route,
)
from neuro_code.infrastructure.lsp.positions import PositionEncoding
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox


def _resolved_path(value: str) -> Path:
    return Path(value).resolve(strict=False)


def _profile() -> LanguageServerProfile:
    return LanguageServerProfile(
        name="python",
        language="python",
        command=("python-lsp", "--stdio"),
        extensions=(".PY",),
        root_markers=("pyproject.toml",),
        environment={"LSP_SECRET": "secret"},
    )


def _config(root: Path, profiles: dict[str, LanguageServerProfile] | None = None) -> AppConfig:
    selected = profiles if profiles is not None else {"python": _profile()}
    return AppConfig(
        cwd=root,
        state_dir=root / ".state",
        providers={},
        default_provider=None,
        selected_provider=None,
        sandbox_profile=SandboxProfile.OFF,
        language_servers=selected,
    )


class LspPortContractTests(unittest.TestCase):
    def test_profile_and_request_values_are_bounded(self) -> None:
        profile = _profile()
        self.assertEqual(profile.extensions, (".py",))
        self.assertEqual(dict(profile.environment), {"LSP_SECRET": "secret"})
        invalid_profiles = (
            {"name": "", "language": "python", "command": ("server",)},
            {"name": "python", "language": "", "command": ("server",)},
            {"name": "python", "language": "python", "command": ()},
            {"name": "python", "language": "python", "command": ("server", "\x00")},
            {
                "name": "python",
                "language": "python",
                "command": ("server",),
                "extensions": ("py",),
            },
            {
                "name": "python",
                "language": "python",
                "command": ("server",),
                "root_markers": ("",),
            },
            {
                "name": "python",
                "language": "python",
                "command": ("server",),
                "environment": {"BAD=NAME": "value"},
            },
        )
        for values in invalid_profiles:
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                LanguageServerProfile(**values)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            LanguageServerProfile(name="python", language="python", command=("server",), enabled=1)  # type: ignore[arg-type]

        invalid_requests = (
            {"operation": "definition"},
            {"operation": LspOperation.DEFINITION, "path": Path("relative.py")},
            {"operation": LspOperation.DEFINITION, "line": 0},
            {"operation": LspOperation.DEFINITION, "column": True},
            {"operation": LspOperation.WORKSPACE_SYMBOLS, "query": ""},
            {"operation": LspOperation.WORKSPACE_SYMBOLS, "profile": ""},
            {"operation": LspOperation.STATUS, "max_results": 0},
            {"operation": LspOperation.STATUS, "max_results": True},
        )
        for values in invalid_requests:
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                LspRequest(**values)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            LspRequest(LspOperation.WORKSPACE_SYMBOLS, query="x" * 5_000)
        self.assertEqual(LspRequest(LspOperation.STATUS).max_results, 200)

    def test_result_status_and_error_values_are_immutable_and_typed(self) -> None:
        result = LspOperationResult(LspOperation.STATUS, {"profiles": []})
        with self.assertRaises(TypeError):
            result.payload["profiles"] = ["changed"]  # type: ignore[index]
        with self.assertRaises(TypeError):
            LspOperationResult("status", {})  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as directory:
            workspace_root = _resolved_path(directory)
            status = LspStatus(workspace_root, "python", "python", "ready")
            self.assertEqual(status.capabilities, ())
            invalid_status_root = workspace_root
        with self.assertRaises(ValueError):
            LspStatus(Path("relative"), None, None, "ready")
        with self.assertRaises(ValueError):
            LspStatus(invalid_status_root, None, None, "")
        with self.assertRaises(ValueError):
            LspStatus(invalid_status_root, None, None, "ready", restart_count=-1)
        with self.assertRaises(TypeError):
            LspStatus(  # type: ignore[arg-type]
                invalid_status_root,
                None,
                None,
                "ready",
                restart_count=True,
            )

        error = LspError(
            "x" * 2_000,
            kind=LspFailureKind.PROTOCOL_ERROR,
            phase=LspFailurePhase.REQUEST,
            retryable=True,
        )
        self.assertEqual(len(str(error)), 1_000)
        self.assertTrue(error.retryable)


class LspProjectionContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = _resolved_path(self.temporary.name)
        self.path = self.root / "main.py"
        self.other = self.root / "other.py"
        self.path.write_text("汉😀x\n", encoding="utf-8")
        self.other.write_text("other\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        self.manager = LanguageServerManager(
            config=_config(self.root),
            local_process_sandbox=ProcessTreeLocalProcessSandbox(),
            workspace_root=self.root,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.close()
        self.temporary.cleanup()

    @staticmethod
    def _range() -> dict[str, object]:
        return {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 4},
        }

    async def test_location_projection_handles_invalid_bounded_and_symbol_forms(self) -> None:
        valid = {"uri": self.path.as_uri(), "range": self._range()}
        second = {"uri": self.other.as_uri(), "range": self._range()}
        projected = await self.manager._project_locations(
            {
                "result": [
                    None,
                    1,
                    {"uri": "https://example.test/x", "range": self._range()},
                    valid,
                    second,
                ]
            },
            source_text=self.path.read_text(encoding="utf-8"),
            workspace_root=self.root,
            policy=None,
            max_results=1,
            encoding=PositionEncoding.UTF16,
            source_path=self.path,
        )
        self.assertEqual(len(projected["locations"]), 1)  # type: ignore[arg-type]
        self.assertEqual(projected["omitted_count"], 4)

        target = await self.manager._project_locations(
            {
                "result": [
                    {
                        "name": "😀" * 400,
                        "location": {
                            "targetUri": self.other.as_uri(),
                            "targetRange": self._range(),
                            "targetSelectionRange": self._range(),
                        },
                    }
                ]
            },
            source_text="",
            workspace_root=self.root,
            policy=None,
            max_results=2,
            encoding=PositionEncoding.UTF16,
            workspace_symbols=True,
        )
        location = target["locations"][0]  # type: ignore[index]
        self.assertLessEqual(len(location["name"].encode("utf-8")), 512)  # type: ignore[index]

        empty = await self.manager._project_locations(
            {"result": None},
            source_text="",
            workspace_root=self.root,
            policy=None,
            max_results=1,
            encoding=PositionEncoding.UTF16,
        )
        self.assertEqual(empty, {"locations": [], "omitted_count": 0})

    async def test_hover_diagnostic_and_symbol_projection_fail_closed(self) -> None:
        hover = self.manager._project_hover(
            {
                "result": {
                    "contents": [{"value": "<b>ok</b>"}, "javascript:bad", {"value": "&amp;"}]
                }
            },
            "text\n",
            PositionEncoding.UTF16,
        )
        self.assertNotIn("<b>", hover["hover"])
        self.assertIn("[unsafe URI omitted]", hover["hover"])
        self.assertIn("&", hover["hover"])
        self.assertEqual(
            self.manager._project_hover({"result": None}, "text\n", PositionEncoding.UTF16),
            {"hover": None},
        )

        valid_diagnostic = self.manager._project_diagnostic(
            {
                "message": "😀" * 2_000,
                "range": self._range(),
                "severity": 1,
                "source": "fake",
                "code": 7,
            },
            "text\n",
            encoding=PositionEncoding.UTF16,
        )
        assert valid_diagnostic is not None
        self.assertLessEqual(len(valid_diagnostic["message"].encode("utf-8")), 2_000)  # type: ignore[union-attr]
        self.assertIsNone(
            self.manager._project_diagnostic({}, "text\n", encoding=PositionEncoding.UTF16)
        )
        self.assertIsNone(
            self.manager._project_diagnostic(
                {"message": "bad", "range": {}},
                "text\n",
                encoding=PositionEncoding.UTF16,
            )
        )

        symbols, omitted = self.manager._project_symbols(
            {
                "result": [
                    {
                        "name": "valid",
                        "kind": 12,
                        "range": self._range(),
                        "children": [{"name": "child", "range": self._range()}],
                    },
                    {"name": "bad", "range": {}},
                    3,
                ]
            },
            "text\n",
            encoding=PositionEncoding.UTF16,
        )
        self.assertEqual(symbols[0]["children"][0]["name"], "child")  # type: ignore[index]
        self.assertEqual(omitted, 2)
        self.assertEqual(
            self.manager._project_symbols(None, "text\n", encoding=PositionEncoding.UTF16), ([], 0)
        )
        self.assertEqual(
            self.manager._project_symbols(
                {"result": "bad"}, "text\n", encoding=PositionEncoding.UTF16
            ),
            ([], 1),
        )

        deep: dict[str, object] = {"name": "deep", "range": self._range()}
        for _ in range(MAX_LSP_SYMBOL_DEPTH + 1):
            deep = {"name": "parent", "range": self._range(), "children": [deep]}
        _symbols, deep_omitted = self.manager._project_symbols(
            [deep], "text\n", encoding=PositionEncoding.UTF16
        )
        self.assertGreater(deep_omitted, 0)
        many = [
            {"name": str(index), "range": self._range()}
            for index in range(MAX_LSP_RESULT_ITEMS + 1)
        ]
        _symbols, many_omitted = self.manager._project_symbols(
            many, "text\n", encoding=PositionEncoding.UTF16
        )
        self.assertEqual(many_omitted, 1)

    async def test_profile_selection_restart_and_optional_reads_are_safe(self) -> None:
        self.assertIsNotNone(self.manager._select_profile(LspRequest(LspOperation.STATUS)))
        second = LanguageServerProfile(
            name="other",
            language="other",
            command=("other-lsp",),
            extensions=(".py",),
        )
        marked_manager = LanguageServerManager(
            config=_config(self.root, {"python": _profile(), "other": second}),
            local_process_sandbox=ProcessTreeLocalProcessSandbox(),
            workspace_root=self.root,
        )
        try:
            selected = marked_manager._select_profile(
                LspRequest(LspOperation.DEFINITION, path=self.path, line=1, column=1)
            )
            self.assertEqual(selected.name, "python")
        finally:
            await marked_manager.close()
        missing = LspRequest(LspOperation.STATUS, profile="missing")
        with self.assertRaises(LspError) as missing_error:
            self.manager._select_profile(missing)
        self.assertEqual(missing_error.exception.kind, LspFailureKind.PROFILE_NOT_FOUND)

        missing_text = await self.manager._read_optional_text(self.root / "missing.py")
        self.assertIsNone(missing_text)
        invalid = self.root / "invalid.py"
        invalid.write_bytes(b"\xff")
        self.assertIsNone(await self.manager._read_optional_text(invalid))

        restarted = await self.manager.execute(LspRequest(LspOperation.RESTART, profile="python"))
        self.assertEqual(restarted.payload["state"], "stopped")
        with self.assertRaises(LspError):
            self.manager._require_capability(_Route(_profile()), "definitionProvider")
