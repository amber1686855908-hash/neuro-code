from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _PROJECT_ROOT / "src"
_PACKAGE_ROOT = _SOURCE_ROOT / "neuro_code"

_DOMAIN = "domain"
_APPLICATION = "application"
_PORTS = "ports"
_INFRASTRUCTURE = "infrastructure"
_INTERFACES = "interfaces"
_BOOTSTRAP = "bootstrap"
_SHARED = "shared"

_ALL_LAYERS = frozenset(
    {
        _DOMAIN,
        _APPLICATION,
        _PORTS,
        _INFRASTRUCTURE,
        _INTERFACES,
        _BOOTSTRAP,
        _SHARED,
    }
)

_ALLOWED_TARGET_LAYERS = {
    _DOMAIN: frozenset({_DOMAIN, _SHARED}),
    _APPLICATION: frozenset({_APPLICATION, _PORTS, _DOMAIN, _SHARED}),
    _PORTS: frozenset({_PORTS, _DOMAIN, _SHARED}),
    _INFRASTRUCTURE: frozenset({_INFRASTRUCTURE, _PORTS, _DOMAIN, _SHARED}),
    _INTERFACES: frozenset({_INTERFACES, _APPLICATION, _PORTS, _DOMAIN, _SHARED}),
    _BOOTSTRAP: _ALL_LAYERS,
    _SHARED: frozenset({_SHARED}),
}

_EXACT_LAYERS = {
    "neuro_code": _SHARED,
    "neuro_code.__main__": _INTERFACES,
    "neuro_code.application.permissions.broker": _APPLICATION,
    "neuro_code.application.permissions.service": _APPLICATION,
    "neuro_code.application.permissions.policy": _APPLICATION,
    "neuro_code.bootstrap.configuration": _BOOTSTRAP,
    "neuro_code.domain.permissions.bash_commands": _DOMAIN,
    "neuro_code.domain.sessions.search": _DOMAIN,
}

# More-specific canonical prefixes must precede their parents.
_PREFIX_LAYERS = (
    ("neuro_code.application.ports", _PORTS),
    ("neuro_code.application.permissions", _PORTS),
    ("neuro_code.application", _APPLICATION),
    ("neuro_code.infrastructure", _INFRASTRUCTURE),
    ("neuro_code.interfaces", _INTERFACES),
    ("neuro_code.bootstrap", _BOOTSTRAP),
    ("neuro_code.shared", _SHARED),
    ("neuro_code.domain", _DOMAIN),
)


@dataclass(frozen=True, order=True, slots=True)
class Dependency:
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class TemporaryViolation:
    source: str
    target: str
    reason: str

    @property
    def dependency(self) -> Dependency:
        return Dependency(self.source, self.target)


@dataclass(frozen=True, slots=True)
class NarrowBootstrapEdge:
    source: str
    target: str
    reason: str

    @property
    def dependency(self) -> Dependency:
        return Dependency(self.source, self.target)


@dataclass(frozen=True, slots=True)
class DynamicImportIssue:
    path: Path
    source: str
    line: int
    detail: str


@dataclass(frozen=True, slots=True)
class DynamicImportScan:
    targets: frozenset[str]
    issues: tuple[DynamicImportIssue, ...]


_CANONICAL_ENTRYPOINT_EDGES: tuple[NarrowBootstrapEdge, ...] = (
    NarrowBootstrapEdge(
        "neuro_code.__main__",
        "neuro_code.bootstrap.entrypoints",
        "The canonical Python module entry point invokes the bootstrap launcher.",
    ),
)

_COMPATIBILITY_FACADE_EDGES: tuple[NarrowBootstrapEdge, ...] = ()

# This set is the immutable Stage 0 ceiling. The active allowlist below may only become a
# subset of it. Expanding this set requires an explicit architecture-baseline decision.
_FROZEN_INITIAL_VIOLATION_KEYS = frozenset(
    {
        Dependency("neuro_code.acp", "neuro_code.adapters.mcp_stdio"),
        Dependency("neuro_code.acp", "neuro_code.workspace"),
        Dependency("neuro_code.application", "neuro_code.adapters.background_tasks"),
        Dependency("neuro_code.application", "neuro_code.adapters.instruction_discovery"),
        Dependency("neuro_code.application", "neuro_code.adapters.sandbox"),
        Dependency("neuro_code.application", "neuro_code.adapters.skill_discovery"),
        Dependency("neuro_code.application", "neuro_code.adapters.sqlite_session"),
        Dependency("neuro_code.application", "neuro_code.providers"),
        Dependency("neuro_code.application", "neuro_code.tools"),
        Dependency("neuro_code.application", "neuro_code.workspace"),
        Dependency("neuro_code.cli", "neuro_code.adapters.background_tasks"),
        Dependency("neuro_code.cli", "neuro_code.adapters.provider_catalog"),
        Dependency("neuro_code.cli", "neuro_code.adapters.provider_settings"),
        Dependency("neuro_code.cli", "neuro_code.adapters.rust_session"),
        Dependency("neuro_code.cli", "neuro_code.adapters.sandbox"),
        Dependency("neuro_code.cli", "neuro_code.adapters.sqlite_session"),
        Dependency("neuro_code.cli", "neuro_code.adapters.ui_preferences"),
        Dependency("neuro_code.cli", "neuro_code.providers"),
        Dependency("neuro_code.cli", "neuro_code.workspace"),
        Dependency("neuro_code.config", "neuro_code.adapters.provider_settings"),
        Dependency("neuro_code.ports.approval", "neuro_code.permissions"),
        Dependency("neuro_code.runtime.agent", "neuro_code.tools.registry"),
        Dependency("neuro_code.runtime.agent", "neuro_code.workspace_changes"),
        Dependency("neuro_code.runtime.conversation", "neuro_code.workspace"),
        Dependency("neuro_code.runtime.terminal_sessions", "neuro_code.adapters.posix_pty"),
        Dependency("neuro_code.runtime.terminal_sessions", "neuro_code.adapters.windows_pty"),
        Dependency("neuro_code.runtime.terminal_sessions", "neuro_code.workspace"),
    }
)

_TEMPORARY_ALLOWLIST: tuple[TemporaryViolation, ...] = ()


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(_SOURCE_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _layer(module: str) -> str | None:
    exact = _EXACT_LAYERS.get(module)
    if exact is not None:
        return exact
    for prefix, assigned in _PREFIX_LAYERS:
        if module == prefix or module.startswith(f"{prefix}."):
            return assigned
    return None


def _source_modules() -> dict[str, Path]:
    return {_module_name(path): path for path in _PACKAGE_ROOT.rglob("*.py")}


_TUI_CANONICAL_CLASS_OWNERS = {
    "NeuroCodeApp": "neuro_code.interfaces.tui.app",
    "TuiUserInteraction": "neuro_code.interfaces.tui.interaction",
    "CollapsingPulseAnimation": "neuro_code.interfaces.tui.state",
    "TranscriptEntry": "neuro_code.interfaces.tui.state",
    "ToolFeedbackState": "neuro_code.interfaces.tui.state",
    "ToolActivityGroupState": "neuro_code.interfaces.tui.state",
    "ProviderSettingsSubmission": "neuro_code.interfaces.tui.state",
    "MenuOptionButton": "neuro_code.interfaces.tui.widgets",
    "AssistantMarkdown": "neuro_code.interfaces.tui.widgets",
    "ConversationMessage": "neuro_code.interfaces.tui.widgets",
    "AssistantMessage": "neuro_code.interfaces.tui.widgets",
    "PromptInput": "neuro_code.interfaces.tui.widgets",
    "ToolFeedbackMessage": "neuro_code.interfaces.tui.widgets",
    "TranscriptCopyScreen": "neuro_code.interfaces.tui.screens.transcript",
    "SettingsScreen": "neuro_code.interfaces.tui.screens.settings",
    "LanguageSettingsScreen": "neuro_code.interfaces.tui.screens.settings",
    "NetworkProxySettingsScreen": "neuro_code.interfaces.tui.screens.settings",
    "BackgroundWakeSettingsScreen": "neuro_code.interfaces.tui.screens.settings",
    "ProviderSettingsScreen": "neuro_code.interfaces.tui.screens.provider",
    "ProviderSetupApp": "neuro_code.interfaces.tui.screens.provider",
    "ReasoningEffortScreen": "neuro_code.interfaces.tui.screens.selection",
    "PermissionApprovalScreen": "neuro_code.interfaces.tui.screens.selection",
    "ProviderSelectionScreen": "neuro_code.interfaces.tui.screens.selection",
    "SessionSelectionScreen": "neuro_code.interfaces.tui.screens.selection",
    "TurnControllerMixin": "neuro_code.interfaces.tui.controllers.turns",
    "ToolActivityEventsMixin": "neuro_code.interfaces.tui.controllers.tool_activity.events",
    "ToolActivityInspectorMixin": "neuro_code.interfaces.tui.controllers.tool_activity.inspector",
    "ToolActivityPresentationMixin": "neuro_code.interfaces.tui.controllers.tool_activity.presentation",
    "CommandControllerMixin": "neuro_code.interfaces.tui.controllers.commands",
    "PreferencesControllerMixin": "neuro_code.interfaces.tui.controllers.preferences",
    "ProviderControllerMixin": "neuro_code.interfaces.tui.controllers.provider",
    "SessionControllerMixin": "neuro_code.interfaces.tui.controllers.session",
    "PlanControllerMixin": "neuro_code.interfaces.tui.controllers.plans",
    "TaskControllerMixin": "neuro_code.interfaces.tui.controllers.tasks",
    "BackgroundControllerMixin": "neuro_code.interfaces.tui.controllers.background",
    "TranscriptControllerMixin": "neuro_code.interfaces.tui.controllers.transcript",
    "RuntimeControllerMixin": "neuro_code.interfaces.tui.controllers.runtime",
}

_CLI_CANONICAL_FUNCTION_OWNERS = {
    "_add_acp_arguments": "neuro_code.interfaces.cli.parser",
    "_add_run_arguments": "neuro_code.interfaces.cli.parser",
    "_subagent_steps": "neuro_code.interfaces.cli.parser",
    "_application_settings": "neuro_code.interfaces.cli.settings",
    "_execution_control_mode": "neuro_code.interfaces.cli.settings",
    "_normalize_rule": "neuro_code.interfaces.cli.settings",
    "_rules": "neuro_code.interfaces.cli.settings",
    "_completion_script": "neuro_code.interfaces.cli.inspection",
    "_instruction_lines": "neuro_code.interfaces.cli.inspection",
    "_plain_config": "neuro_code.interfaces.cli.inspection",
    "_provider_rows": "neuro_code.interfaces.cli.inspection",
    "_skill_lines": "neuro_code.interfaces.cli.inspection",
    "_version_payload": "neuro_code.interfaces.cli.inspection",
    "_run_acp": "neuro_code.interfaces.cli.dispatch",
    "build_parser": "neuro_code.interfaces.cli.parser",
    "run": "neuro_code.interfaces.cli.dispatch",
    "run_agent": "neuro_code.interfaces.cli.agent",
    "run_completions_command": "neuro_code.interfaces.cli.inspection",
    "run_inspect_command": "neuro_code.interfaces.cli.inspection",
    "run_providers_command": "neuro_code.interfaces.cli.inspection",
    "run_version_command": "neuro_code.interfaces.cli.inspection",
    "export_session": "neuro_code.interfaces.cli.session_io",
    "import_session": "neuro_code.interfaces.cli.session_io",
    "run_subagent": "neuro_code.interfaces.cli.subagents",
    "run_subagent_lifecycle": "neuro_code.interfaces.cli.subagents",
    "run_sessions_command": "neuro_code.interfaces.cli.sessions",
}

_CLI_COMMAND_MODULES = frozenset(
    {
        "neuro_code.interfaces.cli.agent",
        "neuro_code.interfaces.cli.contracts",
        "neuro_code.interfaces.cli.dispatch",
        "neuro_code.interfaces.cli.inspection",
        "neuro_code.interfaces.cli.interaction",
        "neuro_code.interfaces.cli.parser",
        "neuro_code.interfaces.cli.session_io",
        "neuro_code.interfaces.cli.sessions",
        "neuro_code.interfaces.cli.settings",
        "neuro_code.interfaces.cli.subagents",
    }
)

_BOOTSTRAP_COMPOSITION_MIXIN_OWNERS = {
    "neuro_code.bootstrap.composition_bindings": (
        "CompositionBindingMixin",
        frozenset(
            {
                "create_local_process_sandbox",
                "create_binding",
                "config_for_session_resume",
            }
        ),
    ),
    "neuro_code.bootstrap.composition_services": (
        "CompositionServicesMixin",
        frozenset(
            {
                "create_worktree_service",
                "create_workspace_checkpoint_service",
                "create_result_adoption_service",
                "create_tool_output_artifact_service",
                "bind_provider_controller",
                "bind_session_selection_controller",
                "bind_plan_execution_controller",
                "bind_plan_scheduling_controller",
                "bind_queued_plan_execution_controller",
                "session_service",
                "session_summary_queries",
            }
        ),
    ),
    "neuro_code.bootstrap.composition_subagents": (
        "CompositionSubagentMixin",
        frozenset(
            {
                "subagent_global_policy",
                "create_read_only_subagent_service",
                "create_writable_subagent_service",
                "create_subagent_scheduler",
                "create_read_only_subagent_application_service",
                "create_subagent_relationship_query_service",
                "create_subagent_relationship_lifecycle_service",
                "bind_subagent_executor",
            }
        ),
    ),
    "neuro_code.bootstrap.composition_workflows": (
        "CompositionWorkflowMixin",
        frozenset(
            {
                "create_task_dag_service",
                "create_leader_service",
                "create_model_planning_service",
                "create_task_dag_replan_service",
                "create_agent_swarm_service",
                "create_ultracode_delegation_service",
            }
        ),
    ),
    "neuro_code.bootstrap.composition_discovery": (
        "CompositionDiscoveryMixin",
        frozenset(
            {
                "instruction_result",
                "skill_result",
                "default_instruction_discovery",
                "default_skill_discovery",
                "rediscover_instructions",
                "rediscover_skills",
            }
        ),
    ),
    "neuro_code.bootstrap.composition_lifecycle": (
        "CompositionLifecycleMixin",
        frozenset({"open", "close"}),
    ),
}

_BOOTSTRAP_COMPOSITION_PRIVATE_HELPER_OWNERS = {
    "_without_main_inline_web_search": "neuro_code.bootstrap.composition_bindings",
    "_without_main_inline_web_fetch": "neuro_code.bootstrap.composition_bindings",
}


def _top_level_classes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _class_method_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names.update(
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return names


def _top_level_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_tui_canonical_class_ownership_is_unique() -> None:
    modules = _source_modules()
    definitions: dict[str, list[str]] = {}
    for module, path in modules.items():
        if not module.startswith("neuro_code.interfaces.tui"):
            continue
        for name in _top_level_classes(path):
            definitions.setdefault(name, []).append(module)

    for name, expected_module in _TUI_CANONICAL_CLASS_OWNERS.items():
        assert definitions.get(name) == [expected_module]


def test_tui_app_and_controller_method_ownership_is_disjoint() -> None:
    modules = _source_modules()
    app_methods = _class_method_names(modules["neuro_code.interfaces.tui.app"])
    controller_methods: dict[str, set[str]] = {}
    for module, path in modules.items():
        if not module.startswith("neuro_code.interfaces.tui.controllers."):
            continue
        for name in _class_method_names(path):
            controller_methods.setdefault(name, set()).add(module)

    assert app_methods.isdisjoint(controller_methods)
    duplicates = {name: owners for name, owners in controller_methods.items() if len(owners) > 1}
    assert not duplicates


def test_tui_app_defines_only_the_app_and_lifecycle_helper() -> None:
    modules = _source_modules()
    path = modules["neuro_code.interfaces.tui.app"]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == {"NeuroCodeApp"}
    assert {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } == {"_read_terminal_size"}


def test_tui_controllers_do_not_import_the_app_module() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    for module, path in modules.items():
        if not module.startswith("neuro_code.interfaces.tui.controllers."):
            continue
        imports = _internal_imports(path, source=module, known_modules=known_modules)
        assert "neuro_code.interfaces.tui.app" not in imports


def test_cli_canonical_function_ownership_is_unique() -> None:
    modules = _source_modules()
    definitions: dict[str, list[str]] = {}
    for module, path in modules.items():
        if not module.startswith("neuro_code.interfaces.cli"):
            continue
        for name in _top_level_function_names(path):
            definitions.setdefault(name, []).append(module)

    for name, expected_module in _CLI_CANONICAL_FUNCTION_OWNERS.items():
        assert definitions.get(name) == [expected_module]


def test_cli_app_is_only_a_facade() -> None:
    modules = _source_modules()
    app_path = modules["neuro_code.interfaces.cli.app"]
    assert _top_level_classes(app_path) == set()
    assert _top_level_function_names(app_path) == set()


def test_cli_parser_registration_is_owned_by_parser_module() -> None:
    modules = _source_modules()
    cli_modules = {
        module
        for module in modules
        if module.startswith("neuro_code.interfaces.cli.")
        and module
        not in {
            "neuro_code.interfaces.cli.parser",
            "neuro_code.interfaces.cli.dispatch",
        }
    }
    for module in cli_modules:
        tree = ast.parse(modules[module].read_text(encoding="utf-8"), filename=str(modules[module]))
        registrations = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"add_argument", "add_parser", "add_subparsers", "parse_args"}
        ]
        assert not registrations, f"CLI parser construction/parsing leaked into {module}"

    dispatch_tree = ast.parse(
        modules["neuro_code.interfaces.cli.dispatch"].read_text(encoding="utf-8"),
        filename=str(modules["neuro_code.interfaces.cli.dispatch"]),
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "parse_args"
        for node in ast.walk(dispatch_tree)
    )


def test_cli_command_modules_do_not_import_the_facade() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    for module in _CLI_COMMAND_MODULES:
        imports = _runtime_internal_imports(
            modules[module],
            source=module,
            known_modules=known_modules,
        )
        assert "neuro_code.interfaces.cli.app" not in imports, module


def test_bootstrap_composition_has_disjoint_canonical_owners() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    root_path = modules["neuro_code.bootstrap.composition"]
    root_tree = ast.parse(root_path.read_text(encoding="utf-8"), filename=str(root_path))
    root_classes = [node for node in root_tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in root_classes] == ["ApplicationComposition"]
    assert _class_method_names(root_path) == {"__init__"}
    assert _top_level_function_names(root_path) == set()

    contract_path = modules["neuro_code.bootstrap.composition_contracts"]
    contract_tree = ast.parse(
        contract_path.read_text(encoding="utf-8"), filename=str(contract_path)
    )
    contract_class = next(
        node
        for node in contract_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CompositionRootMixin"
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in contract_class.body
    )
    assert not _runtime_internal_imports(
        contract_path,
        source="neuro_code.bootstrap.composition_contracts",
        known_modules=known_modules,
    )

    method_owners: dict[str, list[str]] = {}
    for module, (class_name, expected_methods) in _BOOTSTRAP_COMPOSITION_MIXIN_OWNERS.items():
        path = modules[module]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        expected_classes = {class_name}
        if module == "neuro_code.bootstrap.composition_workflows":
            expected_classes.add("CompositionTaskDagWritableWorkerFactory")
        assert set(classes) == expected_classes
        actual_methods = {
            node.name
            for node in classes[class_name].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert actual_methods == expected_methods
        for method in actual_methods:
            method_owners.setdefault(method, []).append(module)

        assert "neuro_code.bootstrap.composition" not in _runtime_internal_imports(
            path,
            source=module,
            known_modules=known_modules,
        )

    assert not {method: owners for method, owners in method_owners.items() if len(owners) > 1}
    assert _top_level_function_names(modules["neuro_code.bootstrap.composition_bindings"]) == set(
        _BOOTSTRAP_COMPOSITION_PRIVATE_HELPER_OWNERS
    )
    helper_owners: dict[str, list[str]] = {}
    for module, path in modules.items():
        for name in _top_level_function_names(path):
            if name in _BOOTSTRAP_COMPOSITION_PRIVATE_HELPER_OWNERS:
                helper_owners.setdefault(name, []).append(module)
    assert helper_owners == {
        name: [owner] for name, owner in _BOOTSTRAP_COMPOSITION_PRIVATE_HELPER_OWNERS.items()
    }

    workflow_factory = ast.parse(
        modules["neuro_code.bootstrap.composition_workflows"].read_text(encoding="utf-8")
    )
    factory = next(
        node
        for node in workflow_factory.body
        if isinstance(node, ast.ClassDef) and node.name == "CompositionTaskDagWritableWorkerFactory"
    )
    assert {
        node.name
        for node in factory.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } == {"__init__", "create"}

    canonical_class_names = {
        "ApplicationComposition",
        "CompositionTaskDagWritableWorkerFactory",
    }
    class_owners: dict[str, list[str]] = {}
    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in canonical_class_names:
                class_owners.setdefault(node.name, []).append(module)
    assert class_owners == {
        "ApplicationComposition": ["neuro_code.bootstrap.composition"],
        "CompositionTaskDagWritableWorkerFactory": ["neuro_code.bootstrap.composition_workflows"],
    }


def _resolve_import_base(node: ast.ImportFrom, *, package: str) -> str | None:
    if node.level == 0:
        return node.module
    relative_name = f"{'.' * node.level}{node.module or ''}"
    return importlib.util.resolve_name(relative_name, package)


class _DynamicImportVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        path: Path,
        source: str,
        package: str,
        known_modules: frozenset[str],
    ) -> None:
        self._path = path
        self._source = source
        self._package = package
        self._known_modules = known_modules
        self._scopes: list[dict[str, str | None]] = [{}]
        self.targets: set[str] = set()
        self.issues: list[DynamicImportIssue] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
            if alias.name == "pkgutil" or (
                alias.name.startswith("importlib.") and alias.asname is not None
            ):
                resolved = alias.name
            elif alias.name == "importlib" or alias.name.startswith("importlib."):
                resolved = "importlib"
            else:
                resolved = None
            self._bind(local_name, resolved)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0 or node.module not in {
            "importlib",
            "importlib.util",
            "importlib.metadata",
            "pkgutil",
        }:
            for alias in node.names:
                self._bind(alias.asname or alias.name, None)
            return

        assert node.module is not None
        for alias in node.names:
            local_name = alias.asname or alias.name
            self._bind(local_name, f"{node.module}.{alias.name}")

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._shadow(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._shadow(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._shadow(node.target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._shadow(node.target)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scopes.append({argument.arg: None for argument in node.args.args})
        self.visit(node.body)
        self._scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        self._scopes.append({})
        for statement in node.body:
            self.visit(statement)
        self._scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        resolved = self._resolve(node.func)
        if resolved in {"importlib.import_module", "__import__"}:
            self._record_module_load(node, operation=resolved)
        elif resolved == "importlib.util.find_spec":
            self._record_find_spec(node)
        elif resolved in {
            "importlib.util.spec_from_file_location",
            "importlib.util.module_from_spec",
            "importlib.util.LazyLoader",
            "importlib.metadata.entry_points",
            "pkgutil.iter_modules",
            "pkgutil.walk_packages",
        }:
            self._issue(node, f"unsupported dynamic module-loading API: {resolved}")
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "load_module",
            "exec_module",
        }:
            self._issue(
                node,
                f"unsupported dynamic module-loading API: {ast.unparse(node.func)}",
            )
        elif self._is_importlib_getattr_loader(node.func):
            self._issue(node, "unsupported dynamic module-loading API via getattr(importlib, ...)")
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        arguments = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        scope = {argument.arg: None for argument in arguments}
        if node.args.vararg is not None:
            scope[node.args.vararg.arg] = None
        if node.args.kwarg is not None:
            scope[node.args.kwarg.arg] = None
        self._scopes.append(scope)
        for statement in node.body:
            self.visit(statement)
        self._scopes.pop()

    def _record_module_load(self, node: ast.Call, *, operation: str) -> None:
        target = self._constant_argument(node, names={"name"})
        if target is None:
            expression = self._target_expression(node, names={"name"})
            self._issue(
                node,
                f"{operation} requires a statically resolvable module target expression: "
                f"{expression}",
            )
            return
        resolved = self._resolve_dynamic_target(node, target, operation=operation)
        if resolved is None:
            return
        if resolved == "neuro_code" or resolved.startswith("neuro_code."):
            if resolved not in self._known_modules:
                self._issue(node, f"{operation} references unknown internal module {resolved!r}")
                return
            self.targets.add(resolved)

    def _record_find_spec(self, node: ast.Call) -> None:
        operation = "importlib.util.find_spec"
        target = self._constant_argument(node, names={"name"})
        if target is None:
            expression = self._target_expression(node, names={"name"})
            self._issue(
                node,
                f"{operation} requires a statically resolvable module target expression: "
                f"{expression}",
            )
            return
        resolved = self._resolve_dynamic_target(node, target, operation=operation)
        if resolved is None:
            return
        if resolved == "neuro_code" or resolved.startswith("neuro_code."):
            self._issue(node, f"{operation} does not allow internal module probing: {resolved!r}")

    def _resolve_dynamic_target(
        self,
        node: ast.Call,
        target: str,
        *,
        operation: str,
    ) -> str | None:
        if not target.startswith("."):
            return target
        package = self._package_argument(node)
        if package is None:
            self._issue(
                node,
                f"{operation} cannot resolve relative target {target!r}: "
                "package must be a static string or __package__",
            )
            return None
        try:
            return importlib.util.resolve_name(target, package)
        except (ImportError, ValueError):
            self._issue(
                node,
                f"{operation} cannot resolve relative target {target!r} from package {package!r}",
            )
            return None

    def _package_argument(self, node: ast.Call) -> str | None:
        candidate: ast.AST | None = node.args[1] if len(node.args) > 1 else None
        if candidate is None:
            candidate = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "package"),
                None,
            )
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
        if isinstance(candidate, ast.Name) and candidate.id == "__package__":
            return self._package
        return None

    def _constant_argument(self, node: ast.Call, *, names: set[str]) -> str | None:
        candidate = self._target_argument(node, names=names)
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
        return None

    def _target_expression(self, node: ast.Call, *, names: set[str]) -> str:
        candidate = self._target_argument(node, names=names)
        return ast.unparse(candidate) if candidate is not None else "<missing>"

    @staticmethod
    def _target_argument(node: ast.Call, *, names: set[str]) -> ast.AST | None:
        candidate: ast.AST | None = node.args[0] if node.args else None
        if candidate is None:
            candidate = next(
                (keyword.value for keyword in node.keywords if keyword.arg in names),
                None,
            )
        return candidate

    def _is_importlib_getattr_loader(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or self._resolve(node.func) != "getattr":
            return False
        if len(node.args) < 2:
            return False
        base = self._resolve(node.args[0])
        return base is not None and base.startswith("importlib")

    def _resolve(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            binding = self._lookup(node.id)
            if binding is not None:
                return binding
            if node.id == "__import__" and self._is_unbound(node.id):
                return "__import__"
            if node.id == "getattr" and self._is_unbound(node.id):
                return "getattr"
            return None
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            return f"{base}.{node.attr}" if base is not None else None
        return None

    def _bind(self, name: str, value: str | None) -> None:
        self._scopes[-1][name] = value

    def _shadow(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, None)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._shadow(element)

    def _lookup(self, name: str) -> str | None:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return None

    def _is_unbound(self, name: str) -> bool:
        return not any(name in scope for scope in self._scopes)

    def _issue(self, node: ast.AST, detail: str) -> None:
        self.issues.append(
            DynamicImportIssue(
                path=self._path,
                source=self._source,
                line=node.lineno,
                detail=detail,
            )
        )


def _dynamic_import_scan(
    tree: ast.AST,
    *,
    path: Path,
    source: str,
    package: str,
    known_modules: frozenset[str],
) -> DynamicImportScan:
    visitor = _DynamicImportVisitor(
        path=path,
        source=source,
        package=package,
        known_modules=known_modules,
    )
    visitor.visit(tree)
    return DynamicImportScan(frozenset(visitor.targets), tuple(visitor.issues))


def _render_dynamic_import_issues(issues: tuple[DynamicImportIssue, ...]) -> str:
    return "\n".join(
        f"  - {issue.path}:{issue.line} ({issue.source}): {issue.detail}" for issue in issues
    )


def _internal_imports(
    path: Path,
    *,
    source: str,
    known_modules: frozenset[str],
) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = source if path.name == "__init__.py" else source.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name for alias in node.names if alias.name.startswith("neuro_code")
            )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = _resolve_import_base(node, package=package)
        if base is None or not base.startswith("neuro_code"):
            continue
        imported.add(base)
        for alias in node.names:
            candidate = f"{base}.{alias.name}"
            if candidate in known_modules:
                imported.add(candidate)
    dynamic = _dynamic_import_scan(
        tree,
        path=path,
        source=source,
        package=package,
        known_modules=known_modules,
    )
    imported.update(dynamic.targets)
    return imported


class _RuntimeImportVisitor(ast.NodeVisitor):
    """Collect runtime internal imports while ignoring TYPE_CHECKING-only edges."""

    def __init__(self, *, source: str, path: Path, known_modules: frozenset[str]) -> None:
        self._source = source
        self._path = path
        self._known_modules = known_modules
        self.imports: set[str] = set()

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_test(node.test):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.update(
            alias.name
            for alias in node.names
            if alias.name.startswith("neuro_code") and alias.name in self._known_modules
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        package = (
            self._source if self._path.name == "__init__.py" else self._source.rpartition(".")[0]
        )
        base = _resolve_import_base(node, package=package)
        if base is None or not base.startswith("neuro_code"):
            return
        if base in self._known_modules:
            self.imports.add(base)
        self.imports.update(
            candidate
            for alias in node.names
            if (candidate := f"{base}.{alias.name}") in self._known_modules
        )

    @staticmethod
    def _is_type_checking_test(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
            isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
        )


def _runtime_internal_imports(
    path: Path,
    *,
    source: str,
    known_modules: frozenset[str],
) -> set[str]:
    visitor = _RuntimeImportVisitor(source=source, path=path, known_modules=known_modules)
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return visitor.imports


def _forbidden_dependencies() -> set[Dependency]:
    modules = _source_modules()
    known_modules = frozenset(modules)
    violations: set[Dependency] = set()
    for source, path in modules.items():
        source_layer = _layer(source)
        assert source_layer is not None, f"unclassified source module: {source}"
        for target in _internal_imports(path, source=source, known_modules=known_modules):
            target_layer = _layer(target)
            assert target_layer is not None, f"unclassified imported module: {target}"
            if target_layer not in _ALLOWED_TARGET_LAYERS[source_layer]:
                violations.add(Dependency(source, target))
    return violations


def test_runtime_production_import_graph_is_acyclic() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    graph = {
        source: _runtime_internal_imports(
            path,
            source=source,
            known_modules=known_modules,
        )
        for source, path in modules.items()
    }
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[tuple[str, ...]] = []

    def visit(source: str) -> None:
        state[source] = 1
        stack.append(source)
        for target in graph[source]:
            target_state = state.get(target, 0)
            if target_state == 0:
                visit(target)
            elif target_state == 1:
                start = stack.index(target)
                cycles.append((*stack[start:], target))
        stack.pop()
        state[source] = 2

    for source in sorted(graph):
        if state.get(source, 0) == 0:
            visit(source)

    assert not cycles, "runtime import cycles detected:\n" + "\n".join(
        f"  - {' -> '.join(cycle)}" for cycle in cycles
    )


def _render_dependencies(dependencies: set[Dependency]) -> str:
    return "\n".join(
        f"  - {dependency.source} -> {dependency.target}" for dependency in sorted(dependencies)
    )


def test_all_owned_modules_have_an_architecture_layer() -> None:
    unknown = sorted(module for module in _source_modules() if _layer(module) is None)
    assert not unknown, "unclassified Neuro Code modules:\n" + "\n".join(
        f"  - {module}" for module in unknown
    )


def test_source_package_top_level_is_architecture_only() -> None:
    source_files = {
        path.name for path in _PACKAGE_ROOT.iterdir() if path.is_file() and path.suffix == ".py"
    }
    source_packages = {
        path.name
        for path in _PACKAGE_ROOT.iterdir()
        if path.is_dir() and path.name != "__pycache__" and any(path.rglob("*.py"))
    }
    assert source_files == {"__init__.py", "__main__.py"}
    assert source_packages == {
        "application",
        "bootstrap",
        "domain",
        "infrastructure",
        "interfaces",
        "shared",
    }


def test_application_configuration_port_has_no_concrete_input_loading() -> None:
    """Configuration ports must remain explicit-input contracts, not adapters."""

    path = _PACKAGE_ROOT / "application" / "ports" / "configuration.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imported_modules.isdisjoint({"os", "tomllib"})
    assert "importlib.util" not in imported_from

    concrete_calls = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            ast.unparse(node.func) in {"find_spec", "importlib.util.find_spec"}
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"cwd", "home", "expanduser", "resolve"}
            )
        )
    }
    assert not concrete_calls, "configuration port performs concrete input loading: " + ", ".join(
        sorted(concrete_calls)
    )


def test_forbidden_layer_directions_are_explicitly_guarded() -> None:
    forbidden_prefixes = {
        "neuro_code.interfaces": (
            "neuro_code.infrastructure",
            "neuro_code.bootstrap",
        ),
        "neuro_code.application": (
            "neuro_code.infrastructure",
            "neuro_code.interfaces",
            "neuro_code.bootstrap",
        ),
        "neuro_code.domain": (
            "neuro_code.application",
            "neuro_code.infrastructure",
            "neuro_code.interfaces",
            "neuro_code.bootstrap",
        ),
        "neuro_code.infrastructure": (
            "neuro_code.interfaces",
            "neuro_code.bootstrap",
        ),
    }
    modules = _source_modules()
    known_modules = frozenset(modules)
    violations: set[Dependency] = set()
    for source, path in modules.items():
        for source_prefix, target_prefixes in forbidden_prefixes.items():
            if source != source_prefix and not source.startswith(f"{source_prefix}."):
                continue
            imports = _internal_imports(path, source=source, known_modules=known_modules)
            for target in imports:
                if any(
                    target == target_prefix or target.startswith(f"{target_prefix}.")
                    for target_prefix in target_prefixes
                ):
                    violations.add(Dependency(source, target))
    assert not violations, "forbidden layer directions detected:\n" + _render_dependencies(
        violations
    )


def test_temporary_allowlist_can_only_shrink_from_the_stage_zero_baseline() -> None:
    active = [entry.dependency for entry in _TEMPORARY_ALLOWLIST]
    assert not active, "all temporary architecture allowlist entries must be removed"
    assert len(active) == len(set(active)), "temporary architecture allowlist contains duplicates"
    assert all(entry.reason.strip() for entry in _TEMPORARY_ALLOWLIST), (
        "every temporary architecture violation requires a reason"
    )
    additions = set(active) - _FROZEN_INITIAL_VIOLATION_KEYS
    assert not additions, (
        "the Stage 0 architecture allowlist may only shrink; unexpected additions:\n"
        f"{_render_dependencies(additions)}"
    )


def _scan_dynamic_import_snippet(source: str) -> DynamicImportScan:
    return _dynamic_import_scan(
        ast.parse(source, filename="dynamic_snippet.py"),
        path=Path("src/neuro_code/application/dynamic_snippet.py"),
        source="neuro_code.application.dynamic_snippet",
        package="neuro_code.application",
        known_modules=frozenset(
            {
                "neuro_code",
                "neuro_code.application",
                "neuro_code.application.dynamic_snippet",
                "neuro_code.application.sibling",
                "neuro_code.adapters",
                "neuro_code.adapters.foo",
            }
        ),
    )


def test_dynamic_import_scanner_resolves_importlib_aliases_and_relative_targets() -> None:
    scan = _scan_dynamic_import_snippet(
        "import importlib as il\n"
        "import importlib.util as util\n"
        "from importlib import import_module as load_module\n"
        "from importlib.util import find_spec as probe\n"
        "first = il.import_module('neuro_code.adapters.foo')\n"
        "second = load_module('.sibling', 'neuro_code.application')\n"
        "third = load_module('.sibling', __package__)\n"
        "external = load_module('external_package')\n"
        "optional = util.find_spec('socksio')\n"
        "also_optional = probe('another_external_package')\n"
    )

    assert scan.targets == {
        "neuro_code.adapters.foo",
        "neuro_code.application.sibling",
    }
    assert not scan.issues


def test_dynamic_import_scanner_resolves_dunder_import() -> None:
    scan = _scan_dynamic_import_snippet(
        "internal = __import__('neuro_code.adapters.foo')\n"
        "external = __import__('external_package')\n"
    )

    assert scan.targets == {"neuro_code.adapters.foo"}
    assert not scan.issues


def test_dynamic_import_scanner_reports_module_load_errors_with_operation_and_location() -> None:
    scan = _scan_dynamic_import_snippet(
        "import importlib\n"
        "name = 'neuro_code.adapters.foo'\n"
        "importlib.import_module(name)\n"
        "importlib.import_module('neuro_code.' + 'adapters.foo')\n"
        "importlib.import_module(f'neuro_code.{name}')\n"
        "__import__(name)\n"
        "importlib.import_module('.sibling')\n"
        "importlib.import_module('..sibling', 'neuro_code')\n"
        "importlib.import_module('neuro_code.adapters.missing')\n"
    )

    assert [issue.detail for issue in scan.issues] == [
        "importlib.import_module requires a statically resolvable module target expression: name",
        "importlib.import_module requires a statically resolvable module target expression: "
        "'neuro_code.' + 'adapters.foo'",
        "importlib.import_module requires a statically resolvable module target expression: "
        "f'neuro_code.{name}'",
        "__import__ requires a statically resolvable module target expression: name",
        "importlib.import_module cannot resolve relative target '.sibling': package must be a "
        "static string or __package__",
        "importlib.import_module cannot resolve relative target '..sibling' from package "
        "'neuro_code'",
        "importlib.import_module references unknown internal module 'neuro_code.adapters.missing'",
    ]
    rendered = _render_dynamic_import_issues(scan.issues)
    for issue in scan.issues:
        assert (
            f"{issue.path}:{issue.line} (neuro_code.application.dynamic_snippet): {issue.detail}"
        ) in rendered


def test_dynamic_import_scanner_rejects_internal_and_unresolved_find_spec() -> None:
    scan = _scan_dynamic_import_snippet(
        "from importlib.util import find_spec\n"
        "find_spec('socksio')\n"
        "find_spec('neuro_code.adapters.foo')\n"
        "find_spec(name)\n"
    )

    assert [issue.detail for issue in scan.issues] == [
        "importlib.util.find_spec does not allow internal module probing: "
        "'neuro_code.adapters.foo'",
        "importlib.util.find_spec requires a statically resolvable module target expression: name",
    ]


def test_dynamic_import_scanner_rejects_unsupported_module_loading_apis() -> None:
    scan = _scan_dynamic_import_snippet(
        "import importlib\n"
        "import importlib.metadata as metadata\n"
        "import pkgutil\n"
        "importlib.util.spec_from_file_location('module', 'module.py')\n"
        "importlib.util.module_from_spec(spec)\n"
        "importlib.util.LazyLoader(loader)\n"
        "loader.load_module('module')\n"
        "loader.exec_module(module)\n"
        "metadata.entry_points()\n"
        "pkgutil.iter_modules()\n"
        "pkgutil.walk_packages()\n"
        "getattr(importlib, 'import_module')('neuro_code.adapters.foo')\n"
    )

    assert [issue.detail for issue in scan.issues] == [
        "unsupported dynamic module-loading API: importlib.util.spec_from_file_location",
        "unsupported dynamic module-loading API: importlib.util.module_from_spec",
        "unsupported dynamic module-loading API: importlib.util.LazyLoader",
        "unsupported dynamic module-loading API: loader.load_module",
        "unsupported dynamic module-loading API: loader.exec_module",
        "unsupported dynamic module-loading API: importlib.metadata.entry_points",
        "unsupported dynamic module-loading API: pkgutil.iter_modules",
        "unsupported dynamic module-loading API: pkgutil.walk_packages",
        "unsupported dynamic module-loading API via getattr(importlib, ...)",
    ]


def test_dynamic_import_scanner_ignores_comments_and_plain_strings() -> None:
    scan = _scan_dynamic_import_snippet(
        "# importlib.import_module('neuro_code.adapters.foo')\n"
        'description = "from importlib import import_module"\n'
        "example = \"__import__('neuro_code.adapters.foo')\"\n"
    )

    assert not scan.targets
    assert not scan.issues


def test_current_production_tree_has_no_dynamic_import_violations() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    issues: list[DynamicImportIssue] = []
    config_scans: dict[str, DynamicImportScan] = {}

    for source, path in modules.items():
        package = source if path.name == "__init__.py" else source.rpartition(".")[0]
        scan = _dynamic_import_scan(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
            path=path,
            source=source,
            package=package,
            known_modules=known_modules,
        )
        issues.extend(scan.issues)
        if source == "neuro_code.bootstrap.configuration":
            config_scans[source] = scan

    assert set(config_scans) == {"neuro_code.bootstrap.configuration"}
    for config_scan in config_scans.values():
        assert not config_scan.targets
        assert not config_scan.issues
    assert not issues, "dynamic import architecture violations:\n" + _render_dynamic_import_issues(
        tuple(issues)
    )


def test_managed_provider_settings_reader_is_canonical_and_consumed_privately() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    canonical = "neuro_code.infrastructure.providers.managed_provider_settings"
    implementation = "neuro_code.infrastructure.providers.provider_settings"
    config = "neuro_code.bootstrap.configuration"
    implementation_tree = ast.parse(modules[implementation].read_text(encoding="utf-8"))
    config_tree = ast.parse(modules[config].read_text(encoding="utf-8"))
    implementation_imports = _internal_imports(
        modules[implementation],
        source=implementation,
        known_modules=known_modules,
    )
    config_imports = _internal_imports(
        modules[config],
        source=config,
        known_modules=known_modules,
    )
    assert canonical in implementation_imports
    assert canonical in config_imports
    assert not (_PACKAGE_ROOT / "adapters" / "provider_settings.py").exists()
    for consumer_tree in (implementation_tree, config_tree):
        loader_imports = [
            alias
            for node in consumer_tree.body
            if isinstance(node, ast.ImportFrom) and node.module == canonical
            for alias in node.names
            if alias.name == "load_managed_provider_settings"
        ]
        assert len(loader_imports) == 1
        assert loader_imports[0].asname == "_load_managed_provider_settings"
    assert not {
        module
        for module in implementation_imports | config_imports
        if module == "neuro_code.adapters" or module.startswith("neuro_code.adapters.")
    }


def test_agent_runtime_uses_the_canonical_tool_service() -> None:
    modules = _source_modules()
    source = "neuro_code.application.runtime.agent"
    imports = _internal_imports(
        modules[source],
        source=source,
        known_modules=frozenset(modules),
    )
    assert "neuro_code.application.ports.tools" in imports
    assert not {
        module
        for module in imports
        if module == "neuro_code.tools" or module.startswith("neuro_code.tools.")
    }


def test_runtime_compatibility_package_is_removed() -> None:
    assert not (_PACKAGE_ROOT / "runtime").exists()
    assert importlib.util.find_spec("neuro_code.runtime") is None


def test_ports_compatibility_package_is_removed() -> None:
    assert not (_PACKAGE_ROOT / "ports").exists()
    assert importlib.util.find_spec("neuro_code.ports") is None


def test_shared_compatibility_modules_are_removed() -> None:
    legacy_modules = ("async_utils", "errors", "redaction")
    for module in legacy_modules:
        assert not (_PACKAGE_ROOT / f"{module}.py").exists()
        assert importlib.util.find_spec(f"neuro_code.{module}") is None


def test_canonical_shared_modules_are_the_only_shared_implementations() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.shared",
        "neuro_code.shared.async_utils",
        "neuro_code.shared.errors",
        "neuro_code.shared.limits",
        "neuro_code.shared.redaction",
        "neuro_code.shared.ui_language",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.shared")
    } == canonical_modules
    assert not {
        module
        for module in modules
        if module in {"neuro_code.async_utils", "neuro_code.errors", "neuro_code.redaction"}
    }


def test_canonical_ports_are_the_only_port_modules() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.application.ports",
        "neuro_code.application.ports.approval",
        "neuro_code.application.ports.agent_swarm",
        "neuro_code.application.ports.background_tasks",
        "neuro_code.application.ports.client_filesystem",
        "neuro_code.application.ports.client_terminal",
        "neuro_code.application.ports.checkpoints",
        "neuro_code.application.ports.configuration",
        "neuro_code.application.ports.http",
        "neuro_code.application.ports.instructions",
        "neuro_code.application.ports.leader",
        "neuro_code.application.ports.lsp",
        "neuro_code.application.ports.mcp",
        "neuro_code.application.ports.model",
        "neuro_code.application.ports.model_planning",
        "neuro_code.application.ports.parent_context_relay",
        "neuro_code.application.ports.task_dag",
        "neuro_code.application.ports.task_dag_replan",
        "neuro_code.application.ports.task_dag_recovery",
        "neuro_code.application.ports.task_dag_result_relay",
        "neuro_code.application.ports.provider_catalog",
        "neuro_code.application.ports.provider_dialects",
        "neuro_code.application.ports.provider_services",
        "neuro_code.application.ports.provider_settings",
        "neuro_code.application.ports.result_adoption",
        "neuro_code.application.ports.routing",
        "neuro_code.application.ports.sandbox",
        "neuro_code.application.ports.skills",
        "neuro_code.application.ports.storage",
        "neuro_code.application.ports.terminal",
        "neuro_code.application.ports.tool_pipeline",
        "neuro_code.application.ports.tools",
        "neuro_code.application.ports.ultracode",
        "neuro_code.application.ports.user_interaction",
        "neuro_code.application.ports.web_fetch",
        "neuro_code.application.ports.ui_preferences",
        "neuro_code.application.ports.web_search",
        "neuro_code.application.ports.workspace",
        "neuro_code.application.ports.workspace_changes",
        "neuro_code.application.ports.windows_sandbox",
        "neuro_code.application.ports.worktree",
        "neuro_code.application.ports.writable_subagent",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.application.ports")
    } == canonical_modules
    assert not {
        module
        for module in modules
        if module == "neuro_code.ports" or module.startswith("neuro_code.ports.")
    }


def test_canonical_persistence_modules_are_the_only_persistence_implementations() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.infrastructure.persistence",
        "neuro_code.infrastructure.persistence.sqlite_session_agent_swarm",
        "neuro_code.infrastructure.persistence.output_artifacts",
        "neuro_code.infrastructure.persistence.checkpoint_artifacts",
        "neuro_code.infrastructure.persistence.managed_worktrees",
        "neuro_code.infrastructure.persistence.workspace_checkpoints",
        "neuro_code.infrastructure.persistence.rust_session",
        "neuro_code.infrastructure.persistence.sqlite_session",
        "neuro_code.infrastructure.persistence.sqlite_session_connection",
        "neuro_code.infrastructure.persistence.sqlite_session_constants",
        "neuro_code.infrastructure.persistence.sqlite_session_core",
        "neuro_code.infrastructure.persistence.sqlite_session_dag",
        "neuro_code.infrastructure.persistence.sqlite_session_dag_replan",
        "neuro_code.infrastructure.persistence.sqlite_session_leader",
        "neuro_code.infrastructure.persistence.sqlite_session_model_planning",
        "neuro_code.infrastructure.persistence.sqlite_session_plans",
        "neuro_code.infrastructure.persistence.sqlite_session_result_adoption",
        "neuro_code.infrastructure.persistence.sqlite_session_schema",
        "neuro_code.infrastructure.persistence.sqlite_session_subagents",
        "neuro_code.infrastructure.persistence.sqlite_session_turns",
        "neuro_code.infrastructure.persistence.sqlite_session_ultracode",
        "neuro_code.infrastructure.persistence.ui_preferences",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.infrastructure.persistence")
    } == canonical_modules


def test_sqlite_session_facade_only_composes_disjoint_canonical_owners() -> None:
    modules = _source_modules()
    facade_path = modules["neuro_code.infrastructure.persistence.sqlite_session"]
    tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in classes] == ["SqliteSessionStore"]
    store = classes[0]
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in store.body)

    mixin_modules = {
        module: path
        for module, path in modules.items()
        if module.startswith("neuro_code.infrastructure.persistence.sqlite_session_")
    }
    method_owners: dict[str, list[str]] = {}
    for module, path in mixin_modules.items():
        module_tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in module_tree.body:
            if not isinstance(node, ast.ClassDef) or not node.name.endswith("Mixin"):
                continue
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_owners.setdefault(member.name, []).append(module)

    assert not {name: owners for name, owners in method_owners.items() if len(owners) > 1}


def test_canonical_background_manager_is_the_only_manager_implementation() -> None:
    modules = _source_modules()
    assert {
        module for module in modules if module == "neuro_code.infrastructure.background_tasks"
    } == {"neuro_code.infrastructure.background_tasks"}


def test_canonical_mcp_modules_are_the_only_mcp_implementations() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.infrastructure.mcp",
        "neuro_code.infrastructure.mcp.http",
        "neuro_code.infrastructure.mcp.stdio",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.infrastructure.mcp")
    } == canonical_modules


def test_canonical_workspace_modules_are_the_only_workspace_implementations() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.infrastructure.workspace",
        "neuro_code.infrastructure.workspace.changes",
        "neuro_code.infrastructure.workspace.checkpoints",
        "neuro_code.infrastructure.workspace.instructions",
        "neuro_code.infrastructure.workspace.paths",
        "neuro_code.infrastructure.workspace.projection",
        "neuro_code.infrastructure.workspace.skills",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.infrastructure.workspace")
    } == canonical_modules


def test_canonical_provider_modules_are_the_only_provider_implementations() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.infrastructure.providers",
        "neuro_code.infrastructure.providers.anthropic",
        "neuro_code.infrastructure.providers.binding",
        "neuro_code.infrastructure.providers.catalog_cache",
        "neuro_code.infrastructure.providers.failover",
        "neuro_code.infrastructure.providers.failure_conformance",
        "neuro_code.infrastructure.providers.failure_policy",
        "neuro_code.infrastructure.providers.gemini",
        "neuro_code.infrastructure.providers.gemini_interactions",
        "neuro_code.infrastructure.providers.image_references",
        "neuro_code.infrastructure.providers.openai_compatible",
        "neuro_code.infrastructure.providers.openai_responses",
        "neuro_code.infrastructure.providers.provider_catalog",
        "neuro_code.infrastructure.providers.provider_settings",
        "neuro_code.infrastructure.providers.managed_provider_settings",
        "neuro_code.infrastructure.providers.hosted_web_search",
        "neuro_code.infrastructure.providers.resilience",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.infrastructure.providers")
    } == canonical_modules


def test_canonical_tool_modules_are_the_only_tool_implementations() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.infrastructure.tools",
        "neuro_code.infrastructure.tools.background_tasks",
        "neuro_code.infrastructure.tools.bash",
        "neuro_code.infrastructure.tools.client_terminal",
        "neuro_code.infrastructure.tools.filesystem",
        "neuro_code.infrastructure.tools.filesystem_discovery",
        "neuro_code.infrastructure.tools.filesystem_mutation",
        "neuro_code.infrastructure.tools.filesystem_output",
        "neuro_code.infrastructure.tools.filesystem_read",
        "neuro_code.infrastructure.tools.filesystem_search",
        "neuro_code.infrastructure.tools.filesystem_security",
        "neuro_code.infrastructure.tools.interaction",
        "neuro_code.infrastructure.tools.lsp",
        "neuro_code.infrastructure.tools.plans",
        "neuro_code.infrastructure.tools.registry",
        "neuro_code.infrastructure.tools.skills",
        "neuro_code.infrastructure.tools.workspace_diff",
        "neuro_code.infrastructure.tools.web_fetch",
        "neuro_code.infrastructure.tools.web_search",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.infrastructure.tools")
    } == canonical_modules


def test_filesystem_facade_contains_no_implementation() -> None:
    modules = _source_modules()
    facade_path = modules["neuro_code.infrastructure.tools.filesystem"]
    tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))

    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )


def test_filesystem_path_security_has_one_canonical_owner() -> None:
    modules = _source_modules()
    owners: dict[str, list[str]] = {}
    for module, path in modules.items():
        if not module.startswith("neuro_code.infrastructure.tools"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "_ensure_no_link_components",
                "_is_link_like",
            }:
                owners.setdefault(node.name, []).append(module)

    assert owners == {
        "_ensure_no_link_components": ["neuro_code.infrastructure.tools.filesystem_security"],
        "_is_link_like": ["neuro_code.infrastructure.tools.filesystem_security"],
    }


def test_tool_registry_assembles_filesystem_tools_from_canonical_modules() -> None:
    modules = _source_modules()
    path = modules["neuro_code.infrastructure.tools.registry"]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "neuro_code.infrastructure.tools.filesystem" not in imported_modules
    assert {
        "neuro_code.infrastructure.tools.filesystem_discovery",
        "neuro_code.infrastructure.tools.filesystem_mutation",
        "neuro_code.infrastructure.tools.filesystem_read",
        "neuro_code.infrastructure.tools.filesystem_search",
    }.issubset(imported_modules)


def test_production_modules_do_not_import_the_removed_ports_package() -> None:
    for source, path in _source_modules().items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        imported_modules.update(
            "neuro_code.ports"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "neuro_code"
            and any(alias.name == "ports" for alias in node.names)
        )
        assert not {
            module
            for module in imported_modules
            if module == "neuro_code.ports" or module.startswith("neuro_code.ports.")
        }, source


def test_production_modules_do_not_import_the_removed_shared_modules() -> None:
    legacy_modules = frozenset(
        {
            "neuro_code.async_utils",
            "neuro_code.errors",
            "neuro_code.redaction",
        }
    )
    for source, path in _source_modules().items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        imported_modules.update(
            f"neuro_code.{alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "neuro_code"
            for alias in node.names
            if f"neuro_code.{alias.name}" in legacy_modules
        )
        assert not imported_modules & legacy_modules, source


def test_canonical_runtime_modules_are_the_only_runtime_implementations() -> None:
    modules = _source_modules()
    compatibility_facades = {
        "neuro_code.application.runtime.approval",
        "neuro_code.application.runtime.conversation",
        "neuro_code.application.runtime.instruction_tracker",
        "neuro_code.application.runtime.profile_conversation",
        "neuro_code.application.runtime.skill_tracker",
        "neuro_code.application.runtime.terminal_sessions",
    }
    canonical_modules = {
        "neuro_code.application.runtime.background_task_reminders",
        "neuro_code.application.runtime.agent",
        "neuro_code.application.runtime.agent_loop",
        "neuro_code.application.runtime.context_builder",
        "neuro_code.application.runtime.event_recorder",
        "neuro_code.application.runtime.finalization",
        "neuro_code.application.runtime.model_step",
        "neuro_code.application.runtime.process_liveness",
        "neuro_code.application.runtime.supervision",
        "neuro_code.application.runtime.tool_scheduler",
        "neuro_code.application.runtime.tool_pipeline",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.application.runtime.")
    } - compatibility_facades == canonical_modules
    assert not {
        module
        for module in modules
        if module == "neuro_code.runtime" or module.startswith("neuro_code.runtime.")
    }

    expected_classes = {
        "neuro_code.application.runtime.agent": {"AgentRuntime"},
        "neuro_code.application.runtime.agent_loop": {
            "AgentLoopRunner",
            "AgentRunResult",
            "_ScheduledToolOutcome",
        },
        "neuro_code.application.runtime.context_builder": {"ContextBuilder"},
        "neuro_code.application.runtime.event_recorder": {"TurnEventRecorder"},
        "neuro_code.application.runtime.model_step": {
            "ModelStepProcessor",
            "ModelStepResult",
        },
        "neuro_code.application.sessions.terminal_sessions": {
            "LocalInteractiveTerminalManager",
            "LocalInteractiveTerminalSession",
            "_TerminalOutputRing",
        },
        "neuro_code.application.sessions.conversation": {"AgentConversation"},
        "neuro_code.application.runtime.tool_pipeline": {
            "ToolExecutor",
            "ToolObservationBuilder",
        },
    }
    for module, class_names in expected_classes.items():
        tree = ast.parse(modules[module].read_text(encoding="utf-8"))
        assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == class_names


def test_canonical_memory_modules_are_the_only_memory_implementations() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.application.memory.compaction",
        "neuro_code.application.memory.compaction_service",
        "neuro_code.application.memory.compaction_runtime",
        "neuro_code.application.memory.compaction_trigger",
        "neuro_code.application.memory.instruction_tracker",
        "neuro_code.application.memory.skill_tracker",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.application.memory.")
    } == canonical_modules

    expected_classes = {
        "neuro_code.application.memory.compaction": {
            "CompactionContextUsage",
            "CompactionResumeRebuilder",
            "CompactionResumeResult",
            "ContextCompactionDecision",
            "ContextCompactionPlan",
            "ContextCompactionPlanner",
            "ContextCompactionPolicy",
            "ContextSummaryGenerationResult",
            "ContextSummaryInput",
            "ContextSummaryInputBuilder",
            "ContextSummaryItem",
            "ContextSummaryRequest",
            "ContextSummarySourceKind",
            "ProviderContextSummaryGenerator",
            "ProviderContextWindow",
        },
        "neuro_code.application.memory.compaction_service": {
            "ContextCompactionApplicationService",
            "ContextCompactionPersistenceResult",
            "PersistContextCompactionRequest",
        },
        "neuro_code.application.memory.compaction_runtime": {
            "ContextCompactionBoundaryDecision",
            "ContextCompactionCommandResult",
            "ContextCompactionCommandStatus",
            "ContextCompactionExecutionRecordPolicy",
            "ContextCompactionRuntimeAssessment",
            "ContextCompactionRuntimeBoundary",
            "ContextCompactionRuntimeBudget",
            "ContextCompactionRuntimeFailureHandling",
            "ContextCompactionRuntimeFailureKind",
            "ContextCompactionRuntimeFailureProjection",
            "ContextCompactionRuntimeGate",
            "ContextCompactionRuntimeRequest",
            "ContextCompactionRuntimeResult",
            "ContextCompactionSafePoint",
            "ContextCompactionTimeoutError",
            "ContextCompactionTurnProjection",
        },
        "neuro_code.application.memory.compaction_trigger": {
            "ContextCompactionTriggerAssessment",
            "ContextCompactionTriggerMode",
            "ContextCompactionTriggerRequest",
            "ContextCompactionTriggerResult",
            "ContextCompactionTriggerService",
        },
        "neuro_code.application.memory.instruction_tracker": {"InstructionTracker"},
        "neuro_code.application.memory.skill_tracker": {"SkillTracker"},
    }
    for module, class_names in expected_classes.items():
        tree = ast.parse(modules[module].read_text(encoding="utf-8"))
        assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == class_names


def test_canonical_session_modules_are_the_only_session_implementations() -> None:
    modules = _source_modules()
    canonical_modules = {
        "neuro_code.application.sessions",
        "neuro_code.application.sessions.binding",
        "neuro_code.application.sessions.catalog",
        "neuro_code.application.sessions.contracts",
        "neuro_code.application.sessions.event_queries",
        "neuro_code.application.sessions.execution_queries",
        "neuro_code.application.sessions.lifecycle",
        "neuro_code.application.sessions.item_queries",
        "neuro_code.application.sessions.profile_conversation",
        "neuro_code.application.sessions.recovery",
        "neuro_code.application.sessions.selection",
        "neuro_code.application.sessions.service",
        "neuro_code.application.sessions.summary",
        "neuro_code.application.sessions.subagent_queries",
        "neuro_code.application.sessions.subagent_lifecycle",
        "neuro_code.application.sessions.task_queries",
        "neuro_code.application.sessions.turns",
        "neuro_code.application.sessions.conversation",
        "neuro_code.application.sessions.terminal_sessions",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.application.sessions")
    } == canonical_modules

    expected_classes = {
        "neuro_code.application.sessions.contracts": {
            "InteractionModeSelectionResult",
            "ReasoningEffortSelectionResult",
            "SessionOption",
            "SessionSelectionResult",
        },
        "neuro_code.application.sessions.selection": {
            "SessionSelectionController",
            "SessionSelectionService",
        },
        "neuro_code.application.sessions.profile_conversation": {
            "ProfileConversationController",
        },
        "neuro_code.application.sessions.recovery": {
            "TurnInputForRetry",
            "TurnRecoveryInspection",
            "TurnRecoveryService",
        },
        "neuro_code.application.sessions.binding": {
            "ConversationRunner",
            "ConversationBinding",
            "ConversationBindingResourceScope",
        },
        "neuro_code.application.sessions.catalog": {
            "ListSessionsPageRequest",
            "ListSessionsRequest",
            "SearchSessionsRequest",
            "SessionCatalogApplicationService",
            "SessionInspection",
            "SessionSearchInspection",
            "SessionSearchInspectionPage",
        },
        "neuro_code.application.sessions.lifecycle": {
            "DeleteSessionRequest",
            "ForkSessionRequest",
            "ImportSessionRequest",
            "RenameSessionRequest",
            "SessionLifecycleController",
            "SessionLifecycleService",
            "StartSessionRequest",
        },
        "neuro_code.application.sessions.task_queries": {
            "GetSessionTaskRequest",
            "ListSessionTasksRequest",
            "SessionTaskQueryController",
            "SessionTaskQueryService",
        },
        "neuro_code.application.sessions.summary": {
            "GetSessionSummaryRequest",
            "SessionSummaryQueryController",
            "SessionSummaryQueryService",
        },
        "neuro_code.application.sessions.subagent_queries": {
            "GetSubagentRelationshipRequest",
            "ListSubagentRelationshipsRequest",
            "SubagentRelationshipAction",
            "SubagentRelationshipProjection",
            "SubagentRelationshipQueryController",
            "SubagentRelationshipQueryService",
        },
        "neuro_code.application.sessions.execution_queries": {
            "LoadExecutionRecordRequest",
            "LoadExecutionRecordsRequest",
            "SessionExecutionQueryController",
            "SessionExecutionQueryService",
        },
        "neuro_code.application.sessions.item_queries": {
            "LoadSessionItemsRequest",
            "SessionItemQueryController",
            "SessionItemQueryService",
        },
        "neuro_code.application.sessions.event_queries": {
            "LoadSessionEventsRequest",
            "SessionEventQueryController",
            "SessionEventQueryService",
        },
        "neuro_code.application.sessions.service": {
            "BindSessionAliasRequest",
            "ExportSessionRequest",
            "GetOrCreateSessionAliasRequest",
            "LoadSessionPlanRequest",
            "ListPlanCommentsRequest",
            "ResolveSessionAliasRequest",
            "ResumeSessionRequest",
            "SessionApplicationService",
            "SessionExport",
        },
        "neuro_code.application.sessions.turns": {
            "RunTurnRequest",
            "SessionTurnRunner",
            "SessionTurnService",
        },
        "neuro_code.application.sessions.terminal_sessions": {
            "LocalInteractiveTerminalManager",
            "LocalInteractiveTerminalSession",
            "_TerminalOutputRing",
        },
        "neuro_code.application.sessions.conversation": {"AgentConversation"},
    }
    for module, class_names in expected_classes.items():
        tree = ast.parse(modules[module].read_text(encoding="utf-8"))
        assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == class_names


def test_production_modules_do_not_import_the_removed_runtime_package() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    for source, path in modules.items():
        imports = _internal_imports(path, source=source, known_modules=known_modules)
        assert not {
            module
            for module in imports
            if module == "neuro_code.runtime" or module.startswith("neuro_code.runtime.")
        }, source


def test_application_runtime_package_remains_minimal() -> None:
    module = "neuro_code.application.runtime"
    tree = ast.parse(_source_modules()[module].read_text(encoding="utf-8"))
    assert not [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert not [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    ]


def test_canonical_runtime_consumers_use_explicit_submodules() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    expected_imports = {
        "neuro_code.application.runtime.agent": {
            "neuro_code.application.runtime.agent_loop",
            "neuro_code.application.runtime.context_builder",
            "neuro_code.application.runtime.tool_pipeline",
        },
        "neuro_code.application.sessions.conversation": {
            "neuro_code.application.runtime.agent",
            "neuro_code.application.sessions.execution_queries",
            "neuro_code.application.sessions.item_queries",
            "neuro_code.application.sessions.summary",
            "neuro_code.application.sessions.task_queries",
        },
        "neuro_code.application.sessions.catalog": {
            "neuro_code.application.sessions.execution_queries",
        },
        "neuro_code.application.tools.service": {
            "neuro_code.application.sessions.event_queries",
        },
        "neuro_code.application.sessions.profile_conversation": {
            "neuro_code.application.runtime.agent",
            "neuro_code.application.sessions.binding",
            "neuro_code.application.sessions.contracts",
        },
        "neuro_code.application.runtime.agent_loop": {
            "neuro_code.application.sessions.lifecycle",
            "neuro_code.application.sessions.task_queries",
        },
        "neuro_code.application.acp.service": {
            "neuro_code.application.sessions.catalog",
            "neuro_code.application.sessions.lifecycle",
            "neuro_code.application.sessions.service",
            "neuro_code.application.sessions.summary",
            "neuro_code.application.tools.service",
        },
        "neuro_code.application.providers.service": {
            "neuro_code.application.providers.contracts",
        },
        "neuro_code.bootstrap.composition": {
            "neuro_code.application.ports.background_tasks",
            "neuro_code.application.ports.configuration",
            "neuro_code.application.ports.instructions",
            "neuro_code.application.ports.skills",
            "neuro_code.application.ports.storage",
            "neuro_code.application.sessions",
            "neuro_code.application.sessions.summary",
            "neuro_code.application.settings",
            "neuro_code.bootstrap.composition_bindings",
            "neuro_code.bootstrap.composition_discovery",
            "neuro_code.bootstrap.composition_lifecycle",
            "neuro_code.bootstrap.composition_services",
            "neuro_code.bootstrap.composition_subagents",
            "neuro_code.bootstrap.composition_workflows",
            "neuro_code.bootstrap.factories",
            "neuro_code.infrastructure.lsp.manager",
            "neuro_code.infrastructure.workspace.paths",
        },
        "neuro_code.bootstrap.cli": {
            "neuro_code.application.permissions.broker",
            "neuro_code.application.providers.contracts",
            "neuro_code.application.sessions.binding",
            "neuro_code.application.sessions.lifecycle",
            "neuro_code.application.sessions.profile_conversation",
            "neuro_code.application.sessions.service",
            "neuro_code.application.tools.service",
        },
        "neuro_code.interfaces.acp.agent": {
            "neuro_code.interfaces.acp.extensions",
            "neuro_code.interfaces.acp.mcp",
            "neuro_code.interfaces.acp.negotiation",
            "neuro_code.interfaces.acp.prompt",
            "neuro_code.interfaces.acp.session_lifecycle",
            "neuro_code.interfaces.acp.session_registry",
        },
        "neuro_code.interfaces.acp.extensions": {
            "neuro_code.application.acp.contracts",
            "neuro_code.application.acp.service",
            "neuro_code.application.ports.tools",
            "neuro_code.application.sessions.subagent_queries",
            "neuro_code.interfaces.acp.errors",
            "neuro_code.interfaces.acp.mcp",
            "neuro_code.interfaces.acp.negotiation",
            "neuro_code.interfaces.acp.serialization",
            "neuro_code.interfaces.acp.session_registry",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.acp.mcp": {
            "neuro_code.application.acp.contracts",
            "neuro_code.application.acp.service",
            "neuro_code.application.ports.mcp",
            "neuro_code.interfaces.acp.errors",
            "neuro_code.interfaces.acp.mcp_config",
            "neuro_code.interfaces.acp.negotiation",
            "neuro_code.interfaces.acp.serialization",
            "neuro_code.interfaces.acp.session_registry",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.acp.negotiation": {
            "neuro_code.application.acp.service",
            "neuro_code.application.ports.client_filesystem",
            "neuro_code.application.ports.client_terminal",
            "neuro_code.interfaces.acp.client_io",
        },
        "neuro_code.interfaces.acp.prompt": {
            "neuro_code.application.acp.service",
            "neuro_code.application.permissions.contracts",
            "neuro_code.application.sessions.binding",
            "neuro_code.application.sessions.turns",
            "neuro_code.interfaces.acp.content",
            "neuro_code.interfaces.acp.errors",
            "neuro_code.interfaces.acp.negotiation",
            "neuro_code.interfaces.acp.serialization",
            "neuro_code.interfaces.acp.session",
            "neuro_code.interfaces.acp.session_registry",
            "neuro_code.interfaces.acp.updates",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.acp.session_lifecycle": {
            "neuro_code.application.acp.contracts",
            "neuro_code.application.acp.service",
            "neuro_code.application.permissions.broker",
            "neuro_code.application.permissions.contracts",
            "neuro_code.application.ports.client_filesystem",
            "neuro_code.application.ports.client_terminal",
            "neuro_code.application.sessions.binding",
            "neuro_code.interfaces.acp.errors",
            "neuro_code.interfaces.acp.mcp",
            "neuro_code.interfaces.acp.negotiation",
            "neuro_code.interfaces.acp.session",
            "neuro_code.interfaces.acp.session_registry",
            "neuro_code.interfaces.acp.updates",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.acp.session_registry": {
            "neuro_code.application.acp.service",
            "neuro_code.domain.sessions",
            "neuro_code.interfaces.acp.errors",
            "neuro_code.interfaces.acp.serialization",
            "neuro_code.interfaces.acp.session",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.tui.app": {
            "neuro_code.interfaces.tui.contracts",
            "neuro_code.interfaces.tui.controllers.background",
            "neuro_code.interfaces.tui.controllers.commands",
            "neuro_code.interfaces.tui.controllers.plans",
            "neuro_code.interfaces.tui.controllers.preferences",
            "neuro_code.interfaces.tui.controllers.provider",
            "neuro_code.interfaces.tui.controllers.runtime",
            "neuro_code.interfaces.tui.controllers.session",
            "neuro_code.interfaces.tui.controllers.tasks",
            "neuro_code.interfaces.tui.controllers.tool_activity.events",
            "neuro_code.interfaces.tui.controllers.tool_activity.inspector",
            "neuro_code.interfaces.tui.controllers.tool_activity.presentation",
            "neuro_code.interfaces.tui.controllers.transcript",
            "neuro_code.interfaces.tui.controllers.turns",
            "neuro_code.interfaces.tui.interaction",
            "neuro_code.interfaces.tui.screens",
            "neuro_code.interfaces.tui.state",
            "neuro_code.interfaces.tui.widgets",
        },
        "neuro_code.interfaces.cli.app": {
            "neuro_code.interfaces.cli.dispatch",
            "neuro_code.interfaces.cli.parser",
        },
        "neuro_code.interfaces.cli.agent": {
            "neuro_code.application.runtime.agent",
            "neuro_code.application.sessions.service",
            "neuro_code.application.sessions.turns",
            "neuro_code.interfaces.cli.contracts",
            "neuro_code.interfaces.cli.interaction",
            "neuro_code.interfaces.cli.serialization",
            "neuro_code.interfaces.cli.settings",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.cli.contracts": {
            "neuro_code.application.ports.configuration",
            "neuro_code.application.ports.storage",
            "neuro_code.application.settings",
            "neuro_code.application.tools.service",
            "neuro_code.domain.sessions",
            "neuro_code.domain.workspace.instructions",
            "neuro_code.domain.workspace.skills",
        },
        "neuro_code.interfaces.cli.dispatch": {
            "neuro_code.interfaces.cli.agent",
            "neuro_code.interfaces.cli.contracts",
            "neuro_code.interfaces.cli.inspection",
            "neuro_code.interfaces.cli.parser",
            "neuro_code.interfaces.cli.session_io",
            "neuro_code.interfaces.cli.sessions",
            "neuro_code.interfaces.cli.settings",
            "neuro_code.interfaces.cli.subagents",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.cli.inspection": {
            "neuro_code.application.ports.configuration",
            "neuro_code.interfaces.cli.contracts",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.cli.interaction": {
            "neuro_code.application.ports.user_interaction",
        },
        "neuro_code.interfaces.cli.parser": {
            "neuro_code.application.execution_policy",
            "neuro_code.application.ports.tools",
            "neuro_code.application.runtime.supervision",
            "neuro_code.application.sessions.subagent_lifecycle",
            "neuro_code.application.workflows.subagent",
            "neuro_code.domain.conversation.reasoning",
            "neuro_code.domain.sandbox.models",
        },
        "neuro_code.interfaces.cli.session_io": {
            "neuro_code.application.sessions.lifecycle",
            "neuro_code.application.sessions.service",
            "neuro_code.interfaces.cli.contracts",
            "neuro_code.interfaces.cli.serialization",
        },
        "neuro_code.interfaces.cli.settings": {
            "neuro_code.application.execution_policy",
            "neuro_code.application.permissions.policy",
            "neuro_code.application.runtime.supervision",
            "neuro_code.application.settings",
            "neuro_code.domain.conversation.reasoning",
            "neuro_code.interfaces.cli.parser",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.cli.subagents": {
            "neuro_code.application.sessions.subagent_lifecycle",
            "neuro_code.application.settings",
            "neuro_code.application.workflows.subagent",
            "neuro_code.interfaces.cli.contracts",
            "neuro_code.interfaces.cli.serialization",
            "neuro_code.interfaces.cli.settings",
            "neuro_code.shared.errors",
        },
        "neuro_code.interfaces.cli.serialization": {
            "neuro_code.application.sessions.catalog",
            "neuro_code.application.tools.service",
        },
        "neuro_code.interfaces.cli.sessions": {
            "neuro_code.application.memory.compaction_runtime",
            "neuro_code.application.ports.storage",
            "neuro_code.application.ports.tools",
            "neuro_code.application.runtime.agent",
            "neuro_code.application.sessions.catalog",
            "neuro_code.application.sessions.lifecycle",
            "neuro_code.application.sessions.recovery",
            "neuro_code.application.settings",
            "neuro_code.application.tools.service",
            "neuro_code.application.ports.configuration",
            "neuro_code.interfaces.cli.serialization",
            "neuro_code.shared.errors",
        },
    }

    for source, canonical_imports in expected_imports.items():
        imports = _internal_imports(
            modules[source],
            source=source,
            known_modules=known_modules,
        )
        assert canonical_imports <= imports


def test_terminal_manager_uses_platform_ports_without_adapter_facades() -> None:
    modules = _source_modules()
    source = "neuro_code.application.sessions.terminal_sessions"
    imports = _internal_imports(
        modules[source],
        source=source,
        known_modules=frozenset(modules),
    )
    assert "neuro_code.application.ports.terminal" in imports
    assert not {
        module
        for module in imports
        if module == "neuro_code.adapters" or module.startswith("neuro_code.adapters.")
    }


def test_canonical_bootstrap_entrypoint_dependency_is_exact_and_present() -> None:
    actual = _forbidden_dependencies()
    allowlist = {entry.dependency for entry in _TEMPORARY_ALLOWLIST}
    dependencies = {entry.dependency for entry in _CANONICAL_ENTRYPOINT_EDGES}
    assert dependencies == {
        Dependency("neuro_code.__main__", "neuro_code.bootstrap.entrypoints"),
    }
    assert all(entry.reason.strip() for entry in _CANONICAL_ENTRYPOINT_EDGES), (
        "every canonical bootstrap entrypoint edge requires a reason"
    )
    assert dependencies <= actual, (
        "canonical bootstrap entrypoint dependencies must remain present:\n"
        f"{_render_dependencies(dependencies - actual)}"
    )
    assert not dependencies & allowlist, (
        "canonical bootstrap entrypoint dependencies must not expand the temporary allowlist"
    )


def test_bootstrap_compatibility_facades_are_absent() -> None:
    actual = _forbidden_dependencies()
    allowlist = {entry.dependency for entry in _TEMPORARY_ALLOWLIST}
    dependencies = {entry.dependency for entry in _COMPATIBILITY_FACADE_EDGES}
    assert not dependencies
    assert not dependencies & actual
    assert not dependencies & allowlist


def test_forbidden_dependencies_match_explicit_bootstrap_edge_categories() -> None:
    actual = _forbidden_dependencies()
    allowed = {entry.dependency for entry in _TEMPORARY_ALLOWLIST}
    entrypoint_dependencies = {entry.dependency for entry in _CANONICAL_ENTRYPOINT_EDGES}
    facade_dependencies = {entry.dependency for entry in _COMPATIBILITY_FACADE_EDGES}
    assert not entrypoint_dependencies & facade_dependencies
    expected = entrypoint_dependencies | facade_dependencies
    unexpected = actual - allowed - expected
    stale = allowed - actual
    assert not unexpected, (
        f"unregistered architecture violations:\n{_render_dependencies(unexpected)}"
    )
    assert not stale, (
        f"stale architecture allowlist entries must be removed:\n{_render_dependencies(stale)}"
    )
    assert actual == entrypoint_dependencies | facade_dependencies
