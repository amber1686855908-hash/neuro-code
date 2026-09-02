from __future__ import annotations

import ast
import importlib
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLIENT_IO_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp" / "client_io.py"
_ACP_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp" / "agent.py"

_MOVED_SYMBOLS = (
    "_AcpClientTerminalTask",
    "_AcpClientTerminal",
    "_AcpClientFileSystem",
    "_client_terminal_command",
    "_client_terminal_cwd",
    "_client_terminal_limits",
    "_client_terminal_background_limits",
    "_client_terminal_wait_seconds",
    "_client_terminal_id",
    "_client_terminal_task_id",
    "_client_terminal_exit_status",
    "MAX_CLIENT_FILE_BYTES",
    "MAX_CLIENT_TERMINAL_COMMAND_BYTES",
    "MAX_CLIENT_TERMINAL_ARGUMENTS",
    "MAX_CLIENT_TERMINAL_ARGUMENT_BYTES",
    "MAX_CLIENT_TERMINAL_ARGUMENT_TOTAL_BYTES",
    "MAX_CLIENT_TERMINAL_ID_BYTES",
    "MAX_CLIENT_TERMINAL_SIGNAL_BYTES",
    "MAX_CLIENT_TERMINAL_TASKS",
    "MAX_CLIENT_TERMINAL_RETAINED_TASKS",
)


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def test_client_io_is_canonical_and_agent_aliases_are_identity_preserving() -> None:
    agent = importlib.import_module("neuro_code.interfaces.acp.agent")
    canonical = importlib.import_module("neuro_code.interfaces.acp.client_io")

    for name in _MOVED_SYMBOLS:
        assert getattr(agent, name) is getattr(canonical, name)

    assert agent.MAX_CLIENT_TERMINAL_OUTPUT_BYTES is canonical.MAX_CLIENT_TERMINAL_OUTPUT_BYTES
    assert agent.ClientTerminalResult is canonical.ClientTerminalResult

    for name in _MOVED_SYMBOLS[:3] + _MOVED_SYMBOLS[3:11]:
        assert getattr(canonical, name).__module__ == canonical.__name__


def test_client_io_has_no_legacy_or_concrete_dependency() -> None:
    tree = ast.parse(_CLIENT_IO_PATH.read_text(encoding="utf-8"), filename=str(_CLIENT_IO_PATH))
    imported_modules = _imported_modules(tree)

    assert "neuro_code.acp" not in imported_modules
    assert not any(
        module == "neuro_code.bootstrap" or module.startswith("neuro_code.bootstrap.")
        for module in imported_modules
    )
    assert not any(
        module == "neuro_code.infrastructure" or module.startswith("neuro_code.infrastructure.")
        for module in imported_modules
    )
    assert not any(
        module == "neuro_code.providers" or module.startswith("neuro_code.providers.")
        for module in imported_modules
    )

    defined = _defined_names(tree)
    assert set(_MOVED_SYMBOLS).issubset(defined)


def test_agent_retains_only_client_io_import_aliases_and_capability_ownership() -> None:
    agent_tree = ast.parse(_ACP_PATH.read_text(encoding="utf-8"), filename=str(_ACP_PATH))
    defined = _defined_names(agent_tree)

    assert not defined.intersection(_MOVED_SYMBOLS)
    assert "_client_file_system" in defined
    assert "_client_terminal" in defined
    assert "initialize" in defined
