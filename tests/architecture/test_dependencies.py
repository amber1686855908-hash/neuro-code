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
_CONFIGURATION = "configuration"

_ALL_LAYERS = frozenset(
    {
        _DOMAIN,
        _APPLICATION,
        _PORTS,
        _INFRASTRUCTURE,
        _INTERFACES,
        _BOOTSTRAP,
        _SHARED,
        _CONFIGURATION,
    }
)

_ALLOWED_TARGET_LAYERS = {
    _DOMAIN: frozenset({_DOMAIN, _SHARED}),
    _APPLICATION: frozenset({_APPLICATION, _PORTS, _DOMAIN, _SHARED, _CONFIGURATION}),
    _PORTS: frozenset({_PORTS, _DOMAIN, _SHARED}),
    _INFRASTRUCTURE: frozenset({_INFRASTRUCTURE, _PORTS, _DOMAIN, _SHARED, _CONFIGURATION}),
    _INTERFACES: frozenset({_INTERFACES, _APPLICATION, _PORTS, _DOMAIN, _SHARED, _CONFIGURATION}),
    _BOOTSTRAP: _ALL_LAYERS,
    _SHARED: frozenset({_SHARED}),
    # The mixed legacy config module is deliberately transitional. It may be consumed by
    # existing layers, but it must not reach into concrete infrastructure itself.
    _CONFIGURATION: frozenset({_CONFIGURATION, _PORTS, _DOMAIN, _SHARED}),
}

_EXACT_LAYERS = {
    "neuro_code": _SHARED,
    "neuro_code.__main__": _INTERFACES,
    "neuro_code.acp": _INTERFACES,
    # The parser is a pure, fail-closed permission rule; its file move is deferred.
    "neuro_code.bash_commands": _DOMAIN,
    "neuro_code.cli": _INTERFACES,
    "neuro_code.config": _CONFIGURATION,
    "neuro_code.permissions": _APPLICATION,
    "neuro_code.tui": _INTERFACES,
    "neuro_code.tui_commands": _INTERFACES,
    "neuro_code.tui_text": _INTERFACES,
    "neuro_code.workspace": _INFRASTRUCTURE,
    "neuro_code.workspace_changes": _INFRASTRUCTURE,
}

# More-specific canonical prefixes must precede their parents.
_PREFIX_LAYERS = (
    ("neuro_code.application.ports", _PORTS),
    ("neuro_code.application.permissions", _PORTS),
    ("neuro_code.application", _APPLICATION),
    ("neuro_code.configuration", _CONFIGURATION),
    ("neuro_code.infrastructure", _INFRASTRUCTURE),
    ("neuro_code.interfaces", _INTERFACES),
    ("neuro_code.bootstrap", _BOOTSTRAP),
    ("neuro_code.shared", _SHARED),
    ("neuro_code.domain", _DOMAIN),
    ("neuro_code.adapters", _INFRASTRUCTURE),
    ("neuro_code.providers", _INFRASTRUCTURE),
    ("neuro_code.tools", _INFRASTRUCTURE),
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


def _render_dependencies(dependencies: set[Dependency]) -> str:
    return "\n".join(
        f"  - {dependency.source} -> {dependency.target}" for dependency in sorted(dependencies)
    )


def test_all_owned_modules_have_an_architecture_layer() -> None:
    unknown = sorted(module for module in _source_modules() if _layer(module) is None)
    assert not unknown, "unclassified Neuro Code modules:\n" + "\n".join(
        f"  - {module}" for module in unknown
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
    config_scan: DynamicImportScan | None = None

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
        if source == "neuro_code.config":
            config_scan = scan

    assert config_scan is not None
    assert not config_scan.targets
    assert not config_scan.issues
    assert not issues, "dynamic import architecture violations:\n" + _render_dynamic_import_issues(
        tuple(issues)
    )


def test_managed_provider_settings_reader_is_canonical_and_consumed_privately() -> None:
    modules = _source_modules()
    known_modules = frozenset(modules)
    canonical = "neuro_code.configuration.managed_provider_settings"
    adapter = "neuro_code.adapters.provider_settings"
    config = "neuro_code.config"
    canonical_tree = ast.parse(modules[canonical].read_text(encoding="utf-8"))
    adapter_tree = ast.parse(modules[adapter].read_text(encoding="utf-8"))
    config_tree = ast.parse(modules[config].read_text(encoding="utf-8"))
    canonical_imports = _internal_imports(
        modules[canonical],
        source=canonical,
        known_modules=known_modules,
    )
    adapter_imports = _internal_imports(
        modules[adapter],
        source=adapter,
        known_modules=known_modules,
    )
    config_imports = _internal_imports(
        modules[config],
        source=config,
        known_modules=known_modules,
    )
    reader_bindings = {
        "_SCHEMA_VERSION",
        "_METADATA_NAME",
        "_CREDENTIALS_NAME",
        "_MAX_FILE_BYTES",
        "_SUPPORTED_PROTOCOLS",
        "_SUPPORTED_DIALECTS",
        "_read_json",
        "_mapping",
        "load_managed_provider_settings",
    }
    canonical_bindings = {
        node.name
        for node in canonical_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    canonical_bindings.update(
        target.id
        for node in canonical_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    )
    adapter_bindings = {
        node.name
        for node in adapter_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    adapter_bindings.update(
        target.id
        for node in adapter_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    )

    assert reader_bindings <= canonical_bindings
    assert not reader_bindings & adapter_bindings
    assert canonical in adapter_imports
    assert canonical in config_imports
    assert not {
        module
        for module in config_imports
        if module == "neuro_code.adapters" or module.startswith("neuro_code.adapters.")
    }
    for consumer_tree in (adapter_tree, config_tree):
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
        for module in canonical_imports
        if module
        in {
            "neuro_code.adapters",
            "neuro_code.bootstrap",
            "neuro_code.config",
            "neuro_code.providers",
        }
        or module.startswith(
            (
                "neuro_code.adapters.",
                "neuro_code.bootstrap.",
                "neuro_code.providers.",
            )
        )
    }


def test_agent_runtime_no_longer_depends_on_the_concrete_tool_registry() -> None:
    dependency = Dependency("neuro_code.application.runtime.agent", "neuro_code.tools.registry")
    assert dependency not in _forbidden_dependencies()
    assert dependency not in {entry.dependency for entry in _TEMPORARY_ALLOWLIST}


def test_agent_runtime_no_longer_depends_on_concrete_workspace_change_observation() -> None:
    dependency = Dependency("neuro_code.application.runtime.agent", "neuro_code.workspace_changes")
    assert dependency not in _forbidden_dependencies()
    assert dependency not in {entry.dependency for entry in _TEMPORARY_ALLOWLIST}


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
        "neuro_code.shared.redaction",
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
        "neuro_code.application.ports.background_tasks",
        "neuro_code.application.ports.client_filesystem",
        "neuro_code.application.ports.client_terminal",
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
    }
    assert {
        module for module in modules if module.startswith("neuro_code.application.ports")
    } == canonical_modules
    assert not {
        module
        for module in modules
        if module == "neuro_code.ports" or module.startswith("neuro_code.ports.")
    }


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
    canonical_modules = {
        "neuro_code.application.runtime.background_task_reminders",
        "neuro_code.application.runtime.agent",
        "neuro_code.application.runtime.approval",
        "neuro_code.application.runtime.conversation",
        "neuro_code.application.runtime.instruction_tracker",
        "neuro_code.application.runtime.profile_conversation",
        "neuro_code.application.runtime.skill_tracker",
        "neuro_code.application.runtime.terminal_sessions",
    }
    assert {
        module for module in modules if module.startswith("neuro_code.application.runtime.")
    } == canonical_modules
    assert not {
        module
        for module in modules
        if module == "neuro_code.runtime" or module.startswith("neuro_code.runtime.")
    }

    expected_classes = {
        "neuro_code.application.runtime.agent": {"AgentRunResult", "AgentRuntime"},
        "neuro_code.application.runtime.approval": {"SessionApprovalBroker"},
        "neuro_code.application.runtime.conversation": {"AgentConversation"},
        "neuro_code.application.runtime.instruction_tracker": {"InstructionTracker"},
        "neuro_code.application.runtime.profile_conversation": {
            "ConversationBinding",
            "ConversationRunner",
            "InteractionModeSelectionResult",
            "ProfileConversationController",
            "ProviderOption",
            "ProviderSelectionResult",
            "ReasoningEffortSelectionResult",
            "SessionOption",
            "SessionSelectionResult",
        },
        "neuro_code.application.runtime.skill_tracker": {"SkillTracker"},
        "neuro_code.application.runtime.terminal_sessions": {
            "LocalInteractiveTerminalManager",
            "LocalInteractiveTerminalSession",
            "_TerminalOutputRing",
        },
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
            "neuro_code.application.runtime.background_task_reminders",
        },
        "neuro_code.application.runtime.conversation": {
            "neuro_code.application.runtime.agent",
        },
        "neuro_code.application.runtime.profile_conversation": {
            "neuro_code.application.runtime.agent",
        },
        "neuro_code.bootstrap.composition": {
            "neuro_code.application.runtime.agent",
            "neuro_code.application.runtime.conversation",
            "neuro_code.application.runtime.instruction_tracker",
            "neuro_code.application.runtime.profile_conversation",
            "neuro_code.application.runtime.skill_tracker",
        },
        "neuro_code.bootstrap.entrypoints": {
            "neuro_code.application.runtime.approval",
            "neuro_code.application.runtime.profile_conversation",
        },
        "neuro_code.acp": {
            "neuro_code.application.runtime.approval",
            "neuro_code.application.runtime.profile_conversation",
        },
        "neuro_code.tui": {
            "neuro_code.application.runtime.agent",
            "neuro_code.application.runtime.approval",
            "neuro_code.application.runtime.profile_conversation",
        },
    }

    for source, canonical_imports in expected_imports.items():
        imports = _internal_imports(
            modules[source],
            source=source,
            known_modules=known_modules,
        )
        assert canonical_imports <= imports

    dependency = Dependency(
        "neuro_code.application.runtime.terminal_sessions", "neuro_code.workspace"
    )
    assert dependency not in _forbidden_dependencies()
    assert dependency not in {entry.dependency for entry in _TEMPORARY_ALLOWLIST}


def test_terminal_manager_no_longer_depends_on_concrete_platform_adapters() -> None:
    dependencies = {
        Dependency(
            "neuro_code.application.runtime.terminal_sessions",
            "neuro_code.adapters.posix_pty",
        ),
        Dependency(
            "neuro_code.application.runtime.terminal_sessions",
            "neuro_code.adapters.windows_pty",
        ),
    }
    actual = _forbidden_dependencies()
    allowlist = {entry.dependency for entry in _TEMPORARY_ALLOWLIST}
    assert not dependencies & actual
    assert not dependencies & allowlist


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
