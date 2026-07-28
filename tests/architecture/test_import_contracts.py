from __future__ import annotations

import ast
import dataclasses
import importlib
import importlib.util
import inspect
import subprocess
import sys
import tomllib
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_EXPECTED_IMPORTS = (
    ("neuro_code", "__version__"),
    ("neuro_code.acp", "NeuroCodeAcpAgent"),
    ("neuro_code.acp", "serve_acp"),
    ("neuro_code.adapters.sqlite_session", "SqliteSessionStore"),
    ("neuro_code.application", "ApplicationSettings"),
    (
        "neuro_code.configuration.managed_provider_settings",
        "load_managed_provider_settings",
    ),
    ("neuro_code.application.runtime.agent", "AgentRunResult"),
    ("neuro_code.application.runtime.agent", "AgentRuntime"),
    ("neuro_code.application.runtime.approval", "ApprovalHandler"),
    ("neuro_code.application.runtime.approval", "SessionApprovalBroker"),
    ("neuro_code.application.runtime.conversation", "AgentConversation"),
    (
        "neuro_code.application.runtime.instruction_tracker",
        "InstructionTracker",
    ),
    ("neuro_code.application.runtime.skill_tracker", "SkillTracker"),
    (
        "neuro_code.application.runtime.profile_conversation",
        "ConversationBinding",
    ),
    (
        "neuro_code.application.runtime.profile_conversation",
        "ProfileConversationController",
    ),
    (
        "neuro_code.application.runtime.terminal_sessions",
        "LocalInteractiveTerminalManager",
    ),
    (
        "neuro_code.application.runtime.terminal_sessions",
        "LocalInteractiveTerminalSession",
    ),
    (
        "neuro_code.application.runtime.background_task_reminders",
        "format_background_task_completion_reminder",
    ),
    ("neuro_code.application.settings", "ApplicationSettings"),
    ("neuro_code.shared.async_utils", "run_blocking"),
    ("neuro_code.shared.errors", "ConfigurationError"),
    ("neuro_code.shared.redaction", "redact_sensitive_text"),
    ("neuro_code.application.ports", "ToolCollection"),
    ("neuro_code.application.ports", "WorkspaceChangeObserver"),
    ("neuro_code.application.ports", "WorkspaceIdentity"),
    ("neuro_code.application.ports", "WorkspacePathResolver"),
    ("neuro_code.application.ports.tools", "ToolCollection"),
    ("neuro_code.application.ports.workspace", "WorkspaceIdentity"),
    ("neuro_code.application.ports.workspace", "WorkspacePathResolver"),
    ("neuro_code.application.ports.workspace_changes", "WorkspaceChangeCheckpoint"),
    ("neuro_code.application.ports.workspace_changes", "WorkspaceChangeReport"),
    ("neuro_code.bootstrap", "ApplicationComposition"),
    ("neuro_code.bootstrap.composition", "ApplicationComposition"),
    ("neuro_code.bootstrap.composition", "WorkspaceChangeObserverFactory"),
    ("neuro_code.bootstrap.entrypoints", "main"),
    ("neuro_code.cli", "build_parser"),
    ("neuro_code.config", "AppConfig"),
    ("neuro_code.config", "ProviderProfile"),
    ("neuro_code.config", "load_config"),
    ("neuro_code.providers", "create_provider"),
    ("neuro_code.providers", "create_routed_provider"),
    ("neuro_code.providers.openai_responses", "OpenAIResponsesProvider"),
    ("neuro_code.tools", "ToolRegistry"),
    ("neuro_code.tools", "default_tool_registry"),
    ("neuro_code.tui", "NeuroCodeApp"),
)

_COMPATIBILITY_IDENTITY_EXPORTS = (
    (
        "neuro_code.application",
        "neuro_code.application.settings",
        ("ApplicationSettings",),
    ),
    (
        "neuro_code.application.ports",
        "neuro_code.application.ports.tools",
        ("ToolCollection",),
    ),
)


def test_key_compatibility_imports_remain_available() -> None:
    missing: list[str] = []
    for module_name, attribute_name in _EXPECTED_IMPORTS:
        module = importlib.import_module(module_name)
        if not hasattr(module, attribute_name):
            missing.append(f"{module_name}:{attribute_name}")
    assert not missing, "missing compatibility imports:\n" + "\n".join(
        f"  - {entry}" for entry in missing
    )


def test_compatibility_exports_preserve_object_identity() -> None:
    mismatches: list[str] = []
    for old_module_name, canonical_module_name, export_names in _COMPATIBILITY_IDENTITY_EXPORTS:
        old_module = importlib.import_module(old_module_name)
        canonical_module = importlib.import_module(canonical_module_name)
        for export_name in export_names:
            if getattr(old_module, export_name) is not getattr(canonical_module, export_name):
                mismatches.append(f"{old_module_name}:{export_name}")
    assert not mismatches, "compatibility exports changed object identity:\n" + "\n".join(
        f"  - {entry}" for entry in mismatches
    )


def test_removed_configuration_compatibility_exports_are_not_public() -> None:
    canonical = importlib.import_module("neuro_code.configuration.managed_provider_settings")
    adapter = importlib.import_module("neuro_code.adapters.provider_settings")
    config = importlib.import_module("neuro_code.config")

    assert canonical.__all__ == ["load_managed_provider_settings"]
    assert canonical.load_managed_provider_settings.__module__ == canonical.__name__
    assert adapter.__all__ == ["JsonProviderSettingsStore"]
    assert not hasattr(adapter, "load_managed_provider_settings")
    assert not hasattr(config, "load_managed_provider_settings")
    assert not hasattr(config, "ProviderConfig")
    assert config.ProviderProfile.__module__ == "neuro_code.config"
    assert config.AppConfig.__module__ == "neuro_code.config"


def test_permissions_expose_policy_without_approval_contract_reexports() -> None:
    permissions = importlib.import_module("neuro_code.permissions")
    contracts = importlib.import_module("neuro_code.application.permissions.contracts")
    approval_port = importlib.import_module("neuro_code.application.ports.approval")
    policy_names = (
        "PermissionEffect",
        "PermissionMode",
        "PermissionRule",
        "PermissionDecision",
        "PermissionManager",
    )
    contract_names = (
        "PermissionApproval",
        "PermissionApprovalKind",
        "PermissionRequest",
        "build_permission_request",
    )

    assert all(hasattr(permissions, name) for name in policy_names)
    assert contracts.__all__ == list(contract_names)
    assert not any(hasattr(permissions, name) for name in contract_names)
    assert approval_port.PermissionApprover.__module__ == "neuro_code.application.ports.approval"

    script = """
def import_approval() -> None:
    from neuro_code.permissions import PermissionApproval

def import_approval_kind() -> None:
    from neuro_code.permissions import PermissionApprovalKind

def import_request() -> None:
    from neuro_code.permissions import PermissionRequest

def import_request_builder() -> None:
    from neuro_code.permissions import build_permission_request

for import_contract in (
    import_approval,
    import_approval_kind,
    import_request,
    import_request_builder,
):
    try:
        import_contract()
    except ImportError:
        pass
    else:
        raise AssertionError(f"removed root approval contract was importable: {import_contract}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_source_and_tests_do_not_statically_import_removed_root_approval_contracts() -> None:
    contract_names = frozenset(
        {
            "PermissionApproval",
            "PermissionApprovalKind",
            "PermissionRequest",
            "build_permission_request",
        }
    )
    for root in (_PROJECT_ROOT / "src" / "neuro_code", _PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "neuro_code.permissions":
                    continue
                imported_names = {alias.name for alias in node.names}
                assert not imported_names & contract_names, path

    permissions_path = _PROJECT_ROOT / "src" / "neuro_code" / "permissions.py"
    permissions_tree = ast.parse(permissions_path.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "neuro_code.application.permissions.contracts"
        for node in ast.walk(permissions_tree)
    )


def test_application_package_retains_settings_without_composition_facade() -> None:
    application = importlib.import_module("neuro_code.application")
    settings = application.ApplicationSettings
    canonical_settings = importlib.import_module("neuro_code.application.settings")
    assert settings is canonical_settings.ApplicationSettings
    assert settings.__module__ == "neuro_code.application.settings"
    assert application.__all__ == ["ApplicationSettings"]
    assert not any(
        hasattr(application, attribute)
        for attribute in (
            "ApplicationComposition",
            "BackgroundSupervisorFactory",
            "InstructionDiscoveryFactory",
            "ProcessSandboxEnforcer",
            "ProviderFactory",
            "SessionStoreFactory",
            "ShellSandboxFactory",
            "SkillDiscoveryFactory",
        )
    )
    assert Path(application.__file__).name == "__init__.py"
    assert not (_PROJECT_ROOT / "src" / "neuro_code" / "application.py").exists()
    assert importlib.import_module("neuro_code.application.ports")
    assert importlib.import_module("neuro_code.application.permissions")


def test_permissions_can_initialize_before_the_tui_module() -> None:
    script = """
import neuro_code.permissions
import neuro_code.tui
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_application_ports_does_not_load_composition_or_infrastructure() -> None:
    script = """
import sys

import neuro_code.application.ports

disallowed = [
    name
    for name in sys.modules
    if name == "neuro_code.bootstrap"
    or name.startswith("neuro_code.bootstrap.")
    or name == "neuro_code.adapters"
    or name.startswith("neuro_code.adapters.")
    or name == "neuro_code.providers"
    or name.startswith("neuro_code.providers.")
]
assert not disallowed, disallowed
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_application_does_not_load_bootstrap() -> None:
    script = """
import sys

import neuro_code.application

assert not [
    name
    for name in sys.modules
    if name == "neuro_code.bootstrap" or name.startswith("neuro_code.bootstrap.")
]
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_canonical_runtime_modules_are_independently_importable() -> None:
    canonical_modules = (
        "neuro_code.application.runtime.background_task_reminders",
        "neuro_code.application.runtime.agent",
        "neuro_code.application.runtime.approval",
        "neuro_code.application.runtime.conversation",
        "neuro_code.application.runtime.instruction_tracker",
        "neuro_code.application.runtime.profile_conversation",
        "neuro_code.application.runtime.skill_tracker",
        "neuro_code.application.runtime.terminal_sessions",
    )
    for module in canonical_modules:
        script = f"""
import importlib
import sys

importlib.import_module({module!r})
assert not [
    name
    for name in sys.modules
    if name == "neuro_code.runtime" or name.startswith("neuro_code.runtime.")
]
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def test_removed_runtime_package_cannot_be_imported() -> None:
    script = """
import importlib
import importlib.util

assert importlib.util.find_spec("neuro_code.runtime") is None
try:
    importlib.import_module("neuro_code.runtime")
except ModuleNotFoundError as error:
    assert error.name == "neuro_code.runtime"
else:
    raise AssertionError("removed runtime package was importable")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_canonical_port_modules_are_independently_importable() -> None:
    canonical_modules = (
        "neuro_code.application.ports",
        "neuro_code.application.ports.approval",
        "neuro_code.application.ports.background_tasks",
        "neuro_code.application.ports.http",
        "neuro_code.application.ports.instructions",
        "neuro_code.application.ports.model",
        "neuro_code.application.ports.provider_catalog",
        "neuro_code.application.ports.provider_settings",
        "neuro_code.application.ports.sandbox",
        "neuro_code.application.ports.skills",
        "neuro_code.application.ports.storage",
        "neuro_code.application.ports.terminal",
        "neuro_code.application.ports.tools",
        "neuro_code.application.ports.ui_preferences",
        "neuro_code.application.ports.workspace",
        "neuro_code.application.ports.workspace_changes",
    )
    for module in canonical_modules:
        script = f"""
import importlib
import sys

importlib.import_module({module!r})
assert not [
    name
    for name in sys.modules
    if name == "neuro_code.ports" or name.startswith("neuro_code.ports.")
]
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def test_removed_ports_package_cannot_be_imported() -> None:
    script = """
import importlib
import importlib.util

assert importlib.util.find_spec("neuro_code.ports") is None
try:
    importlib.import_module("neuro_code.ports")
except ModuleNotFoundError as error:
    assert error.name == "neuro_code.ports"
else:
    raise AssertionError("removed ports package was importable")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_canonical_shared_modules_are_independently_importable() -> None:
    script = """
import importlib
import sys

errors = importlib.import_module("neuro_code.shared.errors")
async_utils = importlib.import_module("neuro_code.shared.async_utils")
redaction = importlib.import_module("neuro_code.shared.redaction")
assert all(
    error.__module__ == "neuro_code.shared.errors"
    for error in (
        errors.ConfigurationError,
        errors.NeuroCodeError,
        errors.PermissionDenied,
        errors.ProviderError,
        errors.SandboxError,
        errors.SessionError,
        errors.TerminalError,
        errors.ToolError,
    )
)
assert async_utils.run_blocking.__module__ == "neuro_code.shared.async_utils"
assert redaction.redact_sensitive_text.__module__ == "neuro_code.shared.redaction"
assert not [
    name
    for name in sys.modules
    if name in {"neuro_code.async_utils", "neuro_code.errors", "neuro_code.redaction"}
]
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_removed_shared_modules_cannot_be_imported() -> None:
    script = """
import importlib
import importlib.util

for module in ("neuro_code.async_utils", "neuro_code.errors", "neuro_code.redaction"):
    assert importlib.util.find_spec(module) is None
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as error:
        assert error.name == module
    else:
        raise AssertionError(f"removed shared module was importable: {module}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_responses_provider_uses_the_canonical_module_without_xai_wrapper() -> None:
    assert not (_PROJECT_ROOT / "src" / "neuro_code" / "providers" / "xai_responses.py").exists()
    script = """
import importlib
import importlib.util

canonical = importlib.import_module("neuro_code.providers.openai_responses")
providers = importlib.import_module("neuro_code.providers")

assert canonical.__all__ == ["OpenAIResponsesProvider"]
assert canonical.OpenAIResponsesProvider.__module__ == canonical.__name__
assert providers.OpenAIResponsesProvider is canonical.OpenAIResponsesProvider
assert "XAIResponsesProvider" not in vars(providers)
assert importlib.util.find_spec("neuro_code.providers.xai_responses") is None
try:
    importlib.import_module("neuro_code.providers.xai_responses")
except ModuleNotFoundError as error:
    assert error.name == "neuro_code.providers.xai_responses"
else:
    raise AssertionError("removed xAI Responses provider module was importable")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_source_and_tests_do_not_statically_import_removed_xai_responses_module() -> None:
    legacy_module = "neuro_code.providers.xai_responses"
    for root in (_PROJECT_ROOT / "src" / "neuro_code", _PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif module is not None:
                    names = (module,)
                else:
                    continue
                if isinstance(node, ast.ImportFrom) and node.module == "neuro_code.providers":
                    names += tuple(
                        f"neuro_code.providers.{alias.name}"
                        for alias in node.names
                        if alias.name == "xai_responses"
                    )
                assert legacy_module not in names, path


def test_tests_do_not_statically_import_the_removed_runtime_package() -> None:
    for path in (_PROJECT_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif module is not None:
                names = (module,)
            else:
                continue
            assert not any(
                name == "neuro_code.runtime" or name.startswith("neuro_code.runtime.")
                for name in names
            ), path


def test_tests_do_not_statically_import_the_removed_ports_package() -> None:
    for path in (_PROJECT_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif module is not None:
                names = (module,)
            else:
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "neuro_code":
                names += tuple("neuro_code.ports" for alias in node.names if alias.name == "ports")
        assert not any(
            name == "neuro_code.ports" or name.startswith("neuro_code.ports.") for name in names
        ), path


def test_tests_do_not_statically_import_the_removed_shared_modules() -> None:
    legacy_modules = frozenset(
        {
            "neuro_code.async_utils",
            "neuro_code.errors",
            "neuro_code.redaction",
        }
    )
    for path in (_PROJECT_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif module is not None:
                names = (module,)
            else:
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "neuro_code":
                names += tuple(
                    f"neuro_code.{alias.name}"
                    for alias in node.names
                    if f"neuro_code.{alias.name}" in legacy_modules
                )
            assert not set(names) & legacy_modules, path


def test_application_runtime_has_no_aggregate_api() -> None:
    application_runtime = importlib.import_module("neuro_code.application.runtime")
    aggregate_names = {
        "AgentConversation",
        "AgentRunResult",
        "AgentRuntime",
        "ApprovalHandler",
        "ConversationBinding",
        "InstructionTracker",
        "LocalInteractiveTerminalManager",
        "LocalInteractiveTerminalSession",
        "ProfileConversationController",
        "SessionApprovalBroker",
        "SkillTracker",
    }
    assert not aggregate_names & vars(application_runtime).keys()


def test_canonical_runtime_public_types_keep_module_paths_and_metadata() -> None:
    runtime_modules = {
        "neuro_code.application.runtime.agent": ("AgentRuntime", "AgentRunResult"),
        "neuro_code.application.runtime.approval": ("SessionApprovalBroker",),
        "neuro_code.application.runtime.conversation": ("AgentConversation",),
        "neuro_code.application.runtime.instruction_tracker": ("InstructionTracker",),
        "neuro_code.application.runtime.skill_tracker": ("SkillTracker",),
        "neuro_code.application.runtime.terminal_sessions": (
            "LocalInteractiveTerminalManager",
            "LocalInteractiveTerminalSession",
        ),
    }
    for module_name, type_names in runtime_modules.items():
        module = importlib.import_module(module_name)
        for type_name in type_names:
            assert getattr(module, type_name).__module__ == module_name

    profile = importlib.import_module("neuro_code.application.runtime.profile_conversation")
    assert profile.__all__ == [
        "ConversationBinding",
        "InteractionModeSelectionResult",
        "ProfileConversationController",
        "ProviderOption",
        "ProviderSelectionResult",
        "ReasoningEffortSelectionResult",
        "SessionOption",
        "SessionSelectionResult",
    ]
    binding_fields = dataclasses.fields(profile.ConversationBinding)
    assert tuple(field.name for field in binding_fields) == (
        "runner",
        "provider",
        "background_tasks",
    )
    assert profile.ConversationBinding.__dataclass_params__.frozen
    assert profile.ConversationBinding.__slots__ == (
        "runner",
        "provider",
        "background_tasks",
    )
    assert profile.ConversationBinding.__match_args__ == (
        "runner",
        "provider",
        "background_tasks",
    )
    assert not profile.ConversationRunner._is_runtime_protocol

    terminal = importlib.import_module("neuro_code.application.runtime.terminal_sessions")
    signature = inspect.signature(terminal.LocalInteractiveTerminalManager)
    assert signature.parameters["workspace_path_resolver"].default is inspect.Parameter.empty
    assert signature.parameters["platform"].default is inspect.Parameter.empty


def test_importing_workspace_change_port_does_not_load_filesystem_implementation() -> None:
    script = """
import sys

import neuro_code.application.ports.workspace_changes

assert "neuro_code.workspace_changes" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_workspace_identity_port_does_not_load_filesystem_implementation() -> None:
    script = """
import sys

import neuro_code.application.ports.workspace

assert "neuro_code.workspace" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_canonical_terminal_manager_does_not_load_concrete_implementations() -> None:
    script = """
import sys

import neuro_code.application.runtime.terminal_sessions

disallowed = [
    name
    for name in sys.modules
    if name == "neuro_code.workspace"
    or name == "neuro_code.adapters.posix_pty"
    or name == "neuro_code.adapters.windows_pty"
]
assert not disallowed, disallowed
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_console_script_targets_remain_unchanged() -> None:
    pyproject = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"] == {
        "neuro": "neuro_code.bootstrap.entrypoints:main",
        "neuro-code": "neuro_code.bootstrap.entrypoints:main",
    }


def test_python_module_entrypoint_uses_the_canonical_bootstrap_launcher() -> None:
    path = _PROJECT_ROOT / "src" / "neuro_code" / "__main__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports_bootstrap_main = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "neuro_code.bootstrap.entrypoints"
        and any(alias.name == "main" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imports_bootstrap_main


def test_importing_cli_does_not_load_bootstrap_or_concrete_infrastructure() -> None:
    script = """
import sys

import neuro_code.cli as cli

assert not hasattr(cli, "main")

disallowed = [
    name
    for name in sys.modules
    if name == "neuro_code.bootstrap"
    or name.startswith("neuro_code.bootstrap.")
    or name == "neuro_code.adapters"
    or name.startswith("neuro_code.adapters.")
    or name == "neuro_code.providers"
    or name.startswith("neuro_code.providers.")
]
assert not disallowed, disallowed
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_acp_does_not_load_bootstrap_or_selected_concrete_dependencies() -> None:
    script = """
import sys

import neuro_code.acp

disallowed = [
    name
    for name in sys.modules
    if name == "neuro_code.bootstrap"
    or name.startswith("neuro_code.bootstrap.")
    or name == "neuro_code.adapters.mcp_stdio"
    or name == "neuro_code.adapters.sqlite_session"
    or name == "neuro_code.providers"
    or name.startswith("neuro_code.providers.")
]
assert not disallowed, disallowed
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_acp_uses_only_its_narrow_application_service() -> None:
    path = _PROJECT_ROOT / "src" / "neuro_code" / "acp.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    serve_functions = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve_acp"
    ]
    assert len(serve_functions) == 1
    serve_function = serve_functions[0]
    assert len(serve_function.args.args) == 1
    assert ast.unparse(serve_function.args.args[0].annotation) == "AcpApplicationService"
    assert "neuro_code.bootstrap.composition" not in imports
    assert "neuro_code.bootstrap.entrypoints" not in imports
    assert "neuro_code.adapters.mcp_stdio" not in imports
    assert "neuro_code.workspace" not in imports
    assert "self._application" not in source
    assert "self._service.config" not in source
    assert "self._service.store" not in source
