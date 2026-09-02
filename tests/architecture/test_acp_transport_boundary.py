from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ACP_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp" / "agent.py"
_TRANSPORT_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp" / "transport.py"

_MOVED_SYMBOLS = (
    "_build_acp_router",
    "_AcpSdkConnection",
    "_WebSocketWriter",
    "ACP_STDIO_BUFFER_LIMIT_BYTES",
)

_FORBIDDEN_TRANSPORT_IMPORTS = (
    "neuro_code.bootstrap",
    "neuro_code.infrastructure",
    "neuro_code.providers",
    "neuro_code.stores",
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


def test_transport_symbols_have_one_canonical_owner_and_agent_identity() -> None:
    agent = importlib.import_module("neuro_code.interfaces.acp.agent")
    canonical = importlib.import_module("neuro_code.interfaces.acp.transport")

    for name in _MOVED_SYMBOLS:
        assert getattr(agent, name) is getattr(canonical, name)

    assert agent.stdio_streams is canonical.stdio_streams
    for name in _MOVED_SYMBOLS[0:3]:
        assert getattr(canonical, name).__module__ == canonical.__name__
    assert canonical.serve_stdio.__module__ == canonical.__name__
    assert canonical.serve_websocket.__module__ == canonical.__name__

    transport_tree = ast.parse(
        _TRANSPORT_PATH.read_text(encoding="utf-8"), filename=str(_TRANSPORT_PATH)
    )
    agent_tree = ast.parse(_ACP_PATH.read_text(encoding="utf-8"), filename=str(_ACP_PATH))
    assert set(_MOVED_SYMBOLS).issubset(_defined_names(transport_tree))
    assert not _defined_names(agent_tree).intersection(_MOVED_SYMBOLS)


def test_transport_has_no_reverse_or_concrete_application_dependency() -> None:
    tree = ast.parse(_TRANSPORT_PATH.read_text(encoding="utf-8"), filename=str(_TRANSPORT_PATH))
    imported_modules = _imported_modules(tree)
    for forbidden in _FORBIDDEN_TRANSPORT_IMPORTS:
        assert not any(
            module == forbidden or module.startswith(f"{forbidden}.") for module in imported_modules
        )
    assert "neuro_code.application" not in imported_modules
    assert "neuro_code.shared.errors" in imported_modules

    source = _TRANSPORT_PATH.read_text(encoding="utf-8")
    assert "PermissionManager" not in source
    assert "SessionApprovalBroker" not in source
    assert "_sessions" not in source
    assert "pending_approval" not in source


def test_transport_receives_agent_or_factory_and_agent_keeps_protocol_ownership() -> None:
    canonical = importlib.import_module("neuro_code.interfaces.acp.transport")
    agent_tree = ast.parse(_ACP_PATH.read_text(encoding="utf-8"), filename=str(_ACP_PATH))
    transport_tree = ast.parse(
        _TRANSPORT_PATH.read_text(encoding="utf-8"), filename=str(_TRANSPORT_PATH)
    )

    stdio_parameters = inspect.signature(canonical.serve_stdio).parameters
    websocket_parameters = inspect.signature(canonical.serve_websocket).parameters
    assert tuple(stdio_parameters)[:1] == ("agent",)
    assert tuple(websocket_parameters)[:1] == ("agent_factory",)
    assert "service" not in stdio_parameters
    assert "service" not in websocket_parameters

    assert "class NeuroCodeAcpAgent" in _ACP_PATH.read_text(encoding="utf-8")
    assert "class NeuroCodeAcpAgent" not in ast.unparse(transport_tree)
    agent_source = _ACP_PATH.read_text(encoding="utf-8")
    assert "asyncio.StreamReader" not in agent_source
    assert "from websockets" not in agent_source
    assert "build_agent_router" not in agent_source
    assert "await _serve_stdio(" in agent_source
    assert "await _serve_websocket(" in agent_source
    assert "NeuroCodeAcpAgent" in ast.unparse(agent_tree)


def test_session_runtime_remains_the_canonical_per_session_owner() -> None:
    agent = importlib.import_module("neuro_code.interfaces.acp.agent")
    runtime = importlib.import_module("neuro_code.interfaces.acp.session")
    assert agent._AcpSession is runtime.AcpSessionRuntime
