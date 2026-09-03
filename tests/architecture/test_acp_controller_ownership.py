from __future__ import annotations

import ast
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ACP_ROOT = _PROJECT_ROOT / "src" / "neuro_code" / "interfaces" / "acp"

_CANONICAL_CLASS_OWNERS = {
    "AcpConnectionState": "negotiation.py",
    "AcpExtensionController": "extensions.py",
    "AcpMcpController": "mcp.py",
    "AcpPromptController": "prompt.py",
    "AcpSessionLifecycleController": "session_lifecycle.py",
    "AcpSessionRegistry": "session_registry.py",
}

_FORBIDDEN_IMPORT_PREFIXES = (
    "neuro_code.bootstrap",
    "neuro_code.infrastructure",
    "neuro_code.interfaces.acp.agent",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_classes(path: Path) -> set[str]:
    return {node.name for node in _tree(path).body if isinstance(node, ast.ClassDef)}


def _top_level_functions(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _assigned_self_attributes(path: Path) -> set[str]:
    attributes: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = (node.target,)
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                attributes.add(target.attr)
    return attributes


def test_acp_controller_classes_have_one_canonical_owner() -> None:
    definitions: dict[str, list[str]] = {}
    for path in _ACP_ROOT.glob("*.py"):
        for name in _top_level_classes(path):
            definitions.setdefault(name, []).append(path.name)

    for name, filename in _CANONICAL_CLASS_OWNERS.items():
        assert definitions.get(name) == [filename]


def test_acp_error_factory_has_one_canonical_owner() -> None:
    definitions: dict[str, list[str]] = {}
    for path in _ACP_ROOT.glob("*.py"):
        for name in _top_level_functions(path):
            definitions.setdefault(name, []).append(path.name)

    assert definitions.get("invalid_params") == ["errors.py"]
    assert not definitions.get("_invalid_params")


def test_agent_is_only_facade_class_and_does_not_reown_controller_state() -> None:
    agent_path = _ACP_ROOT / "agent.py"
    assert _top_level_classes(agent_path) == {"NeuroCodeAcpAgent"}
    assert not _assigned_self_attributes(agent_path) & {
        "_client",
        "_client_capabilities",
        "_client_info",
        "_sessions",
        "_pending_session_tasks",
        "_registry_lock",
        "_list_cursors",
        "_list_cursor_lock",
        "_shutting_down",
        "_mcp_tools",
        "_prompt_task",
        "_pending_approval_id",
    }


def test_acp_controllers_have_no_reverse_or_concrete_layer_imports() -> None:
    for filename in _CANONICAL_CLASS_OWNERS.values():
        imported = _imported_modules(_ACP_ROOT / filename)
        for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
            assert not any(
                module == forbidden or module.startswith(f"{forbidden}.") for module in imported
            ), (filename, forbidden, sorted(imported))
