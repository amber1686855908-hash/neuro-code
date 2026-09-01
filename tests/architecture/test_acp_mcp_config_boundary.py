from __future__ import annotations

import ast
import importlib
from pathlib import Path

from neuro_code.application.acp.contracts import MAX_MCP_SERVERS

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MCP_CONFIG_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp" / "mcp_config.py"
_ACP_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "acp.py"

_MOVED_SYMBOLS = (
    "McpServer",
    "_mcp_string",
    "_mcp_server_configurations",
    "_mcp_http_url",
    "_mcp_http_headers",
    "MAX_MCP_SERVER_NAME_BYTES",
    "MAX_MCP_COMMAND_BYTES",
    "MAX_MCP_ARGUMENTS",
    "MAX_MCP_ARGUMENT_BYTES",
    "MAX_MCP_ARGUMENT_TOTAL_BYTES",
    "MAX_MCP_ENVIRONMENT_VARIABLES",
    "MAX_MCP_ENVIRONMENT_NAME_BYTES",
    "MAX_MCP_ENVIRONMENT_VALUE_BYTES",
    "MAX_MCP_ENVIRONMENT_TOTAL_BYTES",
    "MAX_MCP_URL_BYTES",
    "MAX_MCP_HTTP_HEADERS",
    "MAX_MCP_HTTP_HEADER_NAME_BYTES",
    "MAX_MCP_HTTP_HEADER_VALUE_BYTES",
    "MAX_MCP_HTTP_HEADER_TOTAL_BYTES",
    "MAX_MCP_CONFIGURATION_BYTES",
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


def test_mcp_config_is_canonical_and_legacy_aliases_preserve_identity() -> None:
    legacy = importlib.import_module("neuro_code.acp")
    canonical = importlib.import_module("neuro_code.interfaces.acp.mcp_config")

    for name in _MOVED_SYMBOLS:
        assert getattr(legacy, name) is getattr(canonical, name)

    for name in _MOVED_SYMBOLS[1:5]:
        assert getattr(canonical, name).__module__ == canonical.__name__

    assert canonical.MAX_MCP_SERVERS is MAX_MCP_SERVERS
    assert legacy.MAX_MCP_SERVERS is MAX_MCP_SERVERS


def test_mcp_config_has_no_reverse_or_concrete_dependency() -> None:
    tree = ast.parse(_MCP_CONFIG_PATH.read_text(encoding="utf-8"), filename=str(_MCP_CONFIG_PATH))
    imported_modules = _imported_modules(tree)

    assert "neuro_code.acp" not in imported_modules
    for forbidden in ("neuro_code.bootstrap", "neuro_code.infrastructure", "neuro_code.providers"):
        assert not any(
            module == forbidden or module.startswith(f"{forbidden}.") for module in imported_modules
        )
    assert "neuro_code.interfaces.acp.serialization" in imported_modules


def test_mcp_configuration_definitions_are_absent_from_legacy_module() -> None:
    canonical_tree = ast.parse(
        _MCP_CONFIG_PATH.read_text(encoding="utf-8"), filename=str(_MCP_CONFIG_PATH)
    )
    legacy_tree = ast.parse(_ACP_PATH.read_text(encoding="utf-8"), filename=str(_ACP_PATH))

    assert set(_MOVED_SYMBOLS).issubset(_defined_names(canonical_tree))
    assert not _defined_names(legacy_tree).intersection(_MOVED_SYMBOLS)


def test_agent_supplies_protected_environment_and_retains_live_mcp_ownership() -> None:
    source = _ACP_PATH.read_text(encoding="utf-8")

    assert "self._service.protected_environment_variables" in source
    assert "self._open_mcp_tools" in source
    assert "self._mcp_sampling_handler" in source
    assert "self._mcp_elicitation_handler" in source
