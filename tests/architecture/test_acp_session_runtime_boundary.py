from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ACP_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "acp.py"
_SESSION_PATH = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp" / "session.py"

_FORBIDDEN_SESSION_IMPORTS = (
    "neuro_code.acp",
    "neuro_code.bootstrap",
    "neuro_code.infrastructure",
    "neuro_code.providers",
    "neuro_code.stores",
)

_SESSION_STATE_FIELDS = {
    "binding",
    "mcp_tools",
    "mcp_tool_names",
    "client_terminal",
    "internal_session_id",
    "prompt_task",
    "mapper",
    "pending_approval_id",
    "cancel_requested",
    "closing",
    "closed",
    "state_lock",
    "cleanup_lock",
}


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _defined_classes(tree: ast.AST) -> set[str]:
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _session_attribute_writes(tree: ast.AST) -> set[str]:
    writes: set[str] = set()
    for node in ast.walk(tree):
        targets: tuple[ast.expr, ...]
        if isinstance(node, ast.Assign):
            targets = tuple(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = (node.target,)
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in {"session", "active", "parent_session"}
                and target.attr in _SESSION_STATE_FIELDS
            ):
                writes.add(target.attr)
    return writes


def test_session_runtime_is_canonical_and_legacy_identity_is_preserved() -> None:
    legacy = importlib.import_module("neuro_code.acp")
    canonical = importlib.import_module("neuro_code.interfaces.acp.session")

    assert legacy._AcpSession is canonical.AcpSessionRuntime
    assert canonical.AcpSessionRuntime.__module__ == canonical.__name__
    assert "_AcpSession" not in getattr(canonical, "__all__", ())

    tree = ast.parse(_ACP_PATH.read_text(encoding="utf-8"), filename=str(_ACP_PATH))
    assert "_AcpSession" not in _defined_classes(tree)


def test_session_runtime_has_no_reverse_or_concrete_dependency() -> None:
    tree = ast.parse(_SESSION_PATH.read_text(encoding="utf-8"), filename=str(_SESSION_PATH))
    imported_modules = _imported_modules(tree)
    for forbidden in _FORBIDDEN_SESSION_IMPORTS:
        assert not any(
            module == forbidden or module.startswith(f"{forbidden}.") for module in imported_modules
        )

    canonical = importlib.import_module("neuro_code.interfaces.acp.session")
    parameters = inspect.signature(canonical.AcpSessionRuntime).parameters
    assert "agent" not in parameters
    assert "service" not in parameters
    assert "context" not in parameters
    assert "AcpSessionContext" not in _defined_classes(tree)
    assert not any(name.endswith("Mixin") for name in _defined_classes(tree))


def test_agent_keeps_registry_and_connection_capability_ownership() -> None:
    source = _ACP_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ACP_PATH))
    agent = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NeuroCodeAcpAgent"
    )
    assert agent.name == "NeuroCodeAcpAgent"
    assert "_client_capabilities" in source
    assert "_client_info" in source
    assert "_sessions: dict[str, AcpSessionRuntime]" in source
    assert "_registry_lock" in source
    assert _session_attribute_writes(tree) == set()


def test_runtime_owns_locks_and_binding_close_is_the_cleanup_authority() -> None:
    source = _SESSION_PATH.read_text(encoding="utf-8")
    assert '"_state_lock"' in source
    assert '"_cleanup_lock"' in source
    assert "await asyncio.shield(binding.close())" in source
    assert "background_tasks.shutdown" not in source
