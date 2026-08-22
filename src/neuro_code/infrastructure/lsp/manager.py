"""Application-independent LSP routing, document sync, and safe projection."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import os
import re
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neuro_code.application.ports.lsp import (
    MAX_LSP_RESULT_ITEMS,
    LanguageServerProfile,
    LanguageServerService,
    LspError,
    LspFailureKind,
    LspFailurePhase,
    LspOperation,
    LspOperationResult,
    LspRequest,
    LspResultVisibilityPolicy,
)
from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessLifecycleCapability,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessSandbox,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemTargetRequest,
)
from neuro_code.infrastructure.lsp.client import (
    LSP_DIAGNOSTIC_WAIT_SECONDS,
    LSP_REQUEST_TIMEOUT_SECONDS,
    LspClient,
)
from neuro_code.infrastructure.lsp.positions import (
    PositionEncoding,
    model_range_from_lsp,
    to_lsp_position,
)
from neuro_code.infrastructure.lsp.uri import display_path, file_uri_from_path, path_from_file_uri
from neuro_code.infrastructure.workspace.paths import resolve_filesystem_access_targets
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from neuro_code.configuration.app import AppConfig

MAX_LSP_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_LSP_HOVER_BYTES = 16 * 1024
MAX_LSP_SYMBOL_DEPTH = 8
MAX_LSP_SYMBOL_NAME_BYTES = 512
MAX_LSP_DIAGNOSTICS = 200
MAX_LSP_STDERR_STATUS_BYTES = 4 * 1024
MAX_LSP_RESTARTS = 3
LSP_RESTART_COOLDOWN_SECONDS = 0.5

_HTML_TAG = re.compile(r"<[^>]{0,512}>")
_COMMAND_URI = re.compile(r"(?i)(?:command|javascript|data):[^\s)]+")


def _resolve_executable(command: str) -> str | None:
    executable = shutil.which(command)
    if executable is not None:
        return executable
    candidate = Path(command)
    if not candidate.is_file():
        return None
    try:
        return str(candidate.resolve(strict=True))
    except (OSError, RuntimeError):
        return None


def _bounded_utf8_text(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", "ignore")


@dataclass(slots=True)
class _DocumentState:
    uri: str
    version: int
    fingerprint: str
    text: str


@dataclass(slots=True)
class _Route:
    profile: LanguageServerProfile
    client: LspClient | None = None
    restart_count: int = 0
    last_restart_at: float = 0.0
    last_error: LspError | None = None
    documents: dict[Path, _DocumentState] = field(default_factory=dict)


class LanguageServerManager(LanguageServerService):
    """Own lazy, workspace-scoped LSP sessions without a global singleton."""

    def __init__(
        self,
        *,
        config: AppConfig,
        local_process_sandbox: LocalProcessSandbox,
        workspace_root: Path,
        additional_workspace_roots: tuple[Path, ...] = (),
        redaction_values: tuple[str, ...] = (),
    ) -> None:
        self._config = config
        self._sandbox = local_process_sandbox
        self._workspace_root = workspace_root.expanduser().resolve(strict=False)
        self._additional_roots = tuple(
            path.expanduser().resolve(strict=False) for path in additional_workspace_roots
        )
        self._redaction_values = redaction_values
        self._routes: dict[str, _Route] = {}
        self._closed = False
        self._lock = asyncio.Lock()
        self._visibility_policy: LspResultVisibilityPolicy | None = None

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def set_visibility_policy(self, policy: LspResultVisibilityPolicy) -> None:
        self._visibility_policy = policy

    async def execute(
        self,
        request: LspRequest,
        *,
        visibility_policy: LspResultVisibilityPolicy | None = None,
    ) -> LspOperationResult:
        if self._closed:
            raise LspError(
                "LSP application service is closed",
                kind=LspFailureKind.SERVER_CRASH,
                phase=LspFailurePhase.REQUEST,
            )
        policy = visibility_policy or self._visibility_policy
        if request.operation is LspOperation.STATUS:
            return LspOperationResult(request.operation, self._status_payload())
        if request.operation is LspOperation.RESTART:
            return await self._restart(request)
        profile = self._select_profile(request)
        route = await self._get_route(profile)
        if request.operation in {
            LspOperation.DEFINITION,
            LspOperation.REFERENCES,
            LspOperation.HOVER,
            LspOperation.DOCUMENT_SYMBOLS,
            LspOperation.DIAGNOSTICS,
        }:
            if request.path is None:
                raise LspError(
                    "LSP operation requires a path",
                    kind=LspFailureKind.DOCUMENT_ERROR,
                    phase=LspFailurePhase.REQUEST,
                )
            text, document = await self._sync_document(route, request.path)
            if request.operation is LspOperation.DIAGNOSTICS:
                return await self._diagnostics(route, profile, request.path, text, document)
            if request.operation is LspOperation.DOCUMENT_SYMBOLS:
                self._require_capability(route, "documentSymbolProvider")
                return await self._document_symbols(route, profile, request.path, text, document)
            if request.line is None or request.column is None:
                raise LspError(
                    "LSP semantic navigation requires line and column",
                    kind=LspFailureKind.DOCUMENT_ERROR,
                    phase=LspFailurePhase.REQUEST,
                )
            assert route.client is not None
            position = to_lsp_position(
                text,
                line=request.line,
                column=request.column,
                encoding=route.client.position_encoding,
            )
            params = {
                "textDocument": {"uri": document.uri},
                "position": position,
            }
            method = {
                LspOperation.DEFINITION: "textDocument/definition",
                LspOperation.REFERENCES: "textDocument/references",
                LspOperation.HOVER: "textDocument/hover",
            }.get(request.operation)
            if method is None:
                raise LspError(
                    "LSP operation is not supported by this service",
                    kind=LspFailureKind.UNSUPPORTED_OPERATION,
                    phase=LspFailurePhase.REQUEST,
                )
            self._require_capability(
                route,
                {
                    LspOperation.DEFINITION: "definitionProvider",
                    LspOperation.REFERENCES: "referencesProvider",
                    LspOperation.HOVER: "hoverProvider",
                }[request.operation],
            )
            raw_result = await route.client.request(
                method,
                params,
                budget_seconds=LSP_REQUEST_TIMEOUT_SECONDS,
            )
            result = raw_result if isinstance(raw_result, Mapping) else {"result": raw_result}
            if request.operation is LspOperation.HOVER:
                payload = self._project_hover(result, text, route.client.position_encoding)
            else:
                payload = await self._project_locations(
                    result,
                    source_text=text,
                    workspace_root=self._workspace_root,
                    policy=policy,
                    max_results=request.max_results,
                    encoding=route.client.position_encoding,
                    source_path=request.path,
                )
            payload.update(
                {
                    "operation": request.operation.value,
                    "server": profile.name,
                    "position_encoding": route.client.position_encoding.value,
                }
            )
            return LspOperationResult(request.operation, payload)
        if request.operation is LspOperation.WORKSPACE_SYMBOLS:
            if request.query is None:
                raise LspError(
                    "workspace_symbols requires a query",
                    kind=LspFailureKind.DOCUMENT_ERROR,
                    phase=LspFailurePhase.REQUEST,
                )
            assert route.client is not None
            self._require_capability(route, "workspaceSymbolProvider")
            raw_result = await route.client.request(
                "workspace/symbol",
                {"query": request.query},
                budget_seconds=LSP_REQUEST_TIMEOUT_SECONDS,
            )
            result = raw_result if isinstance(raw_result, Mapping) else {"result": raw_result}
            payload = await self._project_locations(
                result,
                source_text="",
                workspace_root=self._workspace_root,
                policy=policy,
                max_results=request.max_results,
                workspace_symbols=True,
                encoding=route.client.position_encoding,
                source_path=None,
            )
            payload.update(
                {
                    "operation": request.operation.value,
                    "server": profile.name,
                    "query": request.query,
                    "position_encoding": route.client.position_encoding.value,
                }
            )
            return LspOperationResult(request.operation, payload)
        raise LspError(
            f"unsupported LSP operation: {request.operation.value}",
            kind=LspFailureKind.UNSUPPORTED_OPERATION,
            phase=LspFailurePhase.REQUEST,
        )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            routes = tuple(self._routes.values())
        await asyncio.gather(
            *(self._close_route(route) for route in routes if route.client is not None),
            return_exceptions=True,
        )

    async def _close_route(self, route: _Route) -> None:
        client = route.client
        if client is None:
            return
        for document in tuple(route.documents.values()):
            with contextlib.suppress(BaseException):
                await client.notify(
                    "textDocument/didClose",
                    {"textDocument": {"uri": document.uri}},
                )
        await client.close()

    def _select_profile(self, request: LspRequest) -> LanguageServerProfile:
        profiles = self._config.language_servers
        if request.profile is not None:
            profile = profiles.get(request.profile)
            if profile is None or not profile.enabled:
                raise LspError(
                    f"LSP profile is unavailable: {request.profile}",
                    kind=LspFailureKind.PROFILE_NOT_FOUND,
                    phase=LspFailurePhase.CONFIGURATION,
                )
            return profile
        candidates = [profile for profile in profiles.values() if profile.enabled]
        if request.path is not None:
            suffix = request.path.suffix.casefold()
            matching = [profile for profile in candidates if suffix in profile.extensions]
            if matching:
                candidates = matching
            marked = [
                profile
                for profile in candidates
                if profile.root_markers and self._root_markers_match(request.path, profile)
            ]
            if marked:
                candidates = marked
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise LspError(
                "no explicit language-server profile is configured",
                kind=LspFailureKind.NOT_CONFIGURED,
                phase=LspFailurePhase.CONFIGURATION,
            )
        names = ", ".join(sorted(profile.name for profile in candidates)[:8])
        raise LspError(
            f"LSP profile is ambiguous; choose one of: {names}",
            kind=LspFailureKind.PROFILE_NOT_FOUND,
            phase=LspFailurePhase.CONFIGURATION,
        )

    def _root_markers_match(self, path: Path, profile: LanguageServerProfile) -> bool:
        current = path.parent
        workspace_root = self._workspace_root
        while True:
            try:
                if any((current / marker).exists() for marker in profile.root_markers):
                    return True
            except OSError:
                return False
            if current == workspace_root or workspace_root not in current.parents:
                return False
            current = current.parent

    async def _get_route(self, profile: LanguageServerProfile) -> _Route:
        async with self._lock:
            route = self._routes.setdefault(profile.name, _Route(profile))
            client = route.client
            if client is not None and client.alive:
                return route
            if (
                client is not None
                and route.last_error is None
                and client.terminal_error is not None
            ):
                route.last_error = client.terminal_error
                route.last_restart_at = time.monotonic()
                route.restart_count += 1
            if route.last_error is not None:
                if route.restart_count >= MAX_LSP_RESTARTS:
                    raise route.last_error
                if time.monotonic() - route.last_restart_at < LSP_RESTART_COOLDOWN_SECONDS:
                    raise LspError(
                        "LSP server restart cooldown is active",
                        kind=LspFailureKind.SERVER_CRASH,
                        phase=LspFailurePhase.STARTUP,
                        retryable=True,
                    ) from route.last_error
            await self._start_route(route)
            return route

    async def _start_route(self, route: _Route) -> None:
        executable = await run_blocking(_resolve_executable, route.profile.command[0])
        if executable is None:
            error = LspError(
                f"configured LSP executable was not found: {route.profile.command[0]}",
                kind=LspFailureKind.EXECUTABLE_NOT_FOUND,
                phase=LspFailurePhase.STARTUP,
            )
            route.last_error = error
            raise error
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        environment.update(route.profile.environment)
        authorized = frozenset(route.profile.environment)
        mode = LocalWorkspaceAccessMode.READ_ONLY
        roots = (
            LocalWorkspaceAccess(self._workspace_root, mode),
            *(LocalWorkspaceAccess(root, mode) for root in self._additional_roots),
        )
        request = SandboxedProcessRequest.exec(
            executable,
            route.profile.command[1:],
            purpose=LocalProcessPurpose.LSP_SERVER,
            cwd=self._workspace_root,
            sandbox_profile=self._config.sandbox_profile,
            filesystem_policy=LocalProcessFilesystemPolicy(
                roots,
                private_home=self._config.sandbox_profile.enabled,
                private_temporary_directory=self._config.sandbox_profile.enabled,
            ),
            network_policy=(
                LocalProcessNetworkPolicy.ISOLATED
                if self._config.sandbox_profile.restricts_child_network
                else LocalProcessNetworkPolicy.INHERIT
            ),
            environment_policy=LocalProcessEnvironmentPolicy(
                environment,
                explicitly_authorized_names=authorized,
            ),
            stdio_mode=LocalProcessStdioMode.PROTOCOL,
            lifecycle=LocalProcessLifecycle(
                required_capability=LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT,
                termination_grace_seconds=0.5,
                force_wait_seconds=2.0,
            ),
        )
        process = None
        client: LspClient | None = None
        try:
            process = await asyncio.wait_for(
                self._sandbox.spawn(request),
                timeout=10.0,
            )
            client = LspClient(process, workspace_root=self._workspace_root)
            await asyncio.wait_for(client.initialize(), timeout=20.0)
        except TimeoutError as error:
            if client is not None:
                await client.close()
            elif process is not None:
                with contextlib.suppress(BaseException):
                    await process.terminate(grace_seconds=0.5)
            failure = LspError(
                "LSP server startup or initialization timed out",
                kind=LspFailureKind.STARTUP_TIMEOUT,
                phase=LspFailurePhase.STARTUP,
                retryable=True,
            )
            route.last_error = failure
            route.last_restart_at = time.monotonic()
            route.restart_count += 1
            raise failure from error
        except LspError as error:
            if client is not None:
                await client.close()
            elif process is not None:
                with contextlib.suppress(BaseException):
                    await process.terminate(grace_seconds=0.5)
            route.last_error = error
            route.last_restart_at = time.monotonic()
            route.restart_count += 1
            raise
        except (OSError, ValueError) as error:
            if client is not None:
                await client.close()
            elif process is not None:
                with contextlib.suppress(BaseException):
                    await process.terminate(grace_seconds=0.5)
            failure = LspError(
                f"LSP server failed to start: {type(error).__name__}",
                kind=LspFailureKind.SERVER_CRASH,
                phase=LspFailurePhase.STARTUP,
                retryable=True,
            )
            route.last_error = failure
            route.last_restart_at = time.monotonic()
            route.restart_count += 1
            raise failure from error
        else:
            route.client = client
            route.last_error = None
            route.last_restart_at = time.monotonic()

    async def _sync_document(
        self,
        route: _Route,
        path: Path,
    ) -> tuple[str, _DocumentState]:
        canonical_path = await run_blocking(path.resolve, strict=False)
        if path != canonical_path:
            raise LspError(
                "LSP input path changed through a link-like component",
                kind=LspFailureKind.SECURITY_FILTERED,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            )
        roots = (self._workspace_root, *self._additional_roots)
        if not any(canonical_path == root or canonical_path.is_relative_to(root) for root in roots):
            raise LspError(
                "LSP input path is outside the configured workspace roots",
                kind=LspFailureKind.SECURITY_FILTERED,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            )
        uri = file_uri_from_path(path)
        if uri is None:
            raise LspError(
                "LSP input path cannot be represented as a safe file URI",
                kind=LspFailureKind.INVALID_URI,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            )
        try:
            encoded = await run_blocking(path.read_bytes)
        except OSError as error:
            raise LspError(
                f"LSP document read failed: {type(error).__name__}",
                kind=LspFailureKind.DOCUMENT_ERROR,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            ) from error
        if len(encoded) > MAX_LSP_DOCUMENT_BYTES:
            raise LspError(
                "LSP document exceeds the bounded synchronization limit",
                kind=LspFailureKind.DOCUMENT_ERROR,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            )
        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LspError(
                "LSP document is not UTF-8",
                kind=LspFailureKind.DOCUMENT_ERROR,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            ) from error
        fingerprint = hashlib.sha256(encoded).hexdigest()
        current = route.documents.get(path)
        assert route.client is not None
        if current is None:
            current = _DocumentState(uri, 1, fingerprint, text)
            route.documents[path] = current
            await route.client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": route.profile.language,
                        "version": current.version,
                        "text": text,
                    }
                },
            )
        elif current.fingerprint != fingerprint:
            current.version += 1
            current.fingerprint = fingerprint
            current.text = text
            await route.client.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": current.version},
                    "contentChanges": [{"text": text}],
                },
            )
        return text, current

    async def _diagnostics(
        self,
        route: _Route,
        profile: LanguageServerProfile,
        path: Path,
        text: str,
        document: _DocumentState,
    ) -> LspOperationResult:
        assert route.client is not None
        provider = route.client.capabilities.get("diagnosticProvider")
        if provider is not None and provider is not False:
            raw_result = await route.client.request(
                "textDocument/diagnostic",
                {"textDocument": {"uri": document.uri}},
                budget_seconds=LSP_REQUEST_TIMEOUT_SECONDS,
            )
            result = raw_result if isinstance(raw_result, Mapping) else {}
            raw = result.get("items", [])
        else:
            await route.client.wait_for_diagnostics(
                document.uri,
                budget_seconds=LSP_DIAGNOSTIC_WAIT_SECONDS,
            )
            raw = route.client.diagnostics(document.uri)
        diagnostics: list[dict[str, object]] = []
        omitted = 0
        if isinstance(raw, list):
            for item in raw:
                projected = self._project_diagnostic(
                    item,
                    text,
                    encoding=route.client.position_encoding,
                )
                if projected is None:
                    omitted += 1
                elif len(diagnostics) < MAX_LSP_DIAGNOSTICS:
                    diagnostics.append(projected)
                else:
                    omitted += 1
        return LspOperationResult(
            LspOperation.DIAGNOSTICS,
            {
                "operation": LspOperation.DIAGNOSTICS.value,
                "server": profile.name,
                "path": display_path(path, self._workspace_root),
                "version": document.version,
                "diagnostics": diagnostics,
                "omitted_count": omitted,
            },
        )

    async def _document_symbols(
        self,
        route: _Route,
        profile: LanguageServerProfile,
        path: Path,
        text: str,
        document: _DocumentState,
    ) -> LspOperationResult:
        assert route.client is not None
        raw_result = await route.client.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": document.uri}},
            budget_seconds=LSP_REQUEST_TIMEOUT_SECONDS,
        )
        result = raw_result if isinstance(raw_result, Mapping) else {"result": raw_result}
        symbols, omitted = self._project_symbols(
            result.get("result", result),
            text,
            encoding=route.client.position_encoding,
        )
        return LspOperationResult(
            LspOperation.DOCUMENT_SYMBOLS,
            {
                "operation": LspOperation.DOCUMENT_SYMBOLS.value,
                "server": profile.name,
                "path": display_path(path, self._workspace_root),
                "symbols": symbols,
                "omitted_count": omitted,
            },
        )

    async def _project_locations(
        self,
        result: Mapping[str, Any],
        *,
        source_text: str,
        workspace_root: Path,
        policy: LspResultVisibilityPolicy | None,
        max_results: int,
        encoding: PositionEncoding,
        workspace_symbols: bool = False,
        source_path: Path | None = None,
    ) -> dict[str, object]:
        raw = result.get("result", result)
        if raw is None:
            raw_items: list[object] = []
        elif isinstance(raw, list):
            raw_items = raw
        else:
            raw_items = [raw]
        locations: list[dict[str, object]] = []
        omitted = 0
        for item in raw_items:
            location = (
                item.get("location") if isinstance(item, dict) and workspace_symbols else item
            )
            if not isinstance(location, dict):
                omitted += 1
                continue
            uri = location.get("uri") or location.get("targetUri")
            range_value = location.get("range") or location.get("targetRange")
            selected_uri = location.get("targetSelectionRange") or range_value
            path = path_from_file_uri(
                uri,
                (workspace_root, *self._additional_roots),
            )
            if path is None:
                omitted += 1
                continue
            if policy is not None and not self._visible_to_policy(path, policy):
                omitted += 1
                continue
            text = (
                source_text
                if source_path is not None and path == source_path
                else await self._read_optional_text(path)
            )
            if text is None:
                omitted += 1
                continue
            projected_range = model_range_from_lsp(
                text,
                selected_uri,
                encoding=encoding,
            )
            if projected_range is None:
                omitted += 1
                continue
            projected: dict[str, object] = {
                "path": display_path(path, workspace_root),
                "range": projected_range,
            }
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                projected["name"] = _bounded_utf8_text(item["name"], MAX_LSP_SYMBOL_NAME_BYTES)
            if len(locations) < max_results:
                locations.append(projected)
            else:
                omitted += 1
        return {"locations": locations, "omitted_count": omitted}

    def _visible_to_policy(self, path: Path, policy: LspResultVisibilityPolicy) -> bool:
        try:
            plan = resolve_filesystem_access_targets(
                "lsp",
                self._workspace_root,
                (
                    FilesystemTargetRequest(
                        str(path),
                        FilesystemAccessOperation.READ,
                        must_exist=False,
                        reject_link_like=True,
                    ),
                ),
                additional_workspace_roots=self._additional_roots,
            )
        except (OSError, RuntimeError, ValueError, ToolError):
            return False
        decision = policy.decide_targets("lsp", plan.targets, side_effecting=False)
        return bool(getattr(decision, "allowed", False)) and getattr(
            getattr(decision, "effect", None), "value", getattr(decision, "effect", None)
        ) not in {"ask", "deny"}

    async def _read_optional_text(self, path: Path) -> str | None:
        try:
            encoded = await run_blocking(path.read_bytes)
            if len(encoded) > MAX_LSP_DOCUMENT_BYTES:
                return None
            return encoded.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _project_hover(
        self,
        result: Mapping[str, Any],
        text: str,
        encoding: object,
    ) -> dict[str, object]:
        raw = result.get("result", result)
        if not isinstance(raw, dict):
            return {"hover": None}
        contents = self._safe_hover_text(raw.get("contents"))
        projected: dict[str, object] = {"hover": contents}
        range_value = raw.get("range")
        if range_value is not None:
            projected_range = model_range_from_lsp(
                text,
                range_value,
                encoding=encoding,  # type: ignore[arg-type]
            )
            if projected_range is not None:
                projected["range"] = projected_range
        return projected

    @staticmethod
    def _safe_hover_text(value: object) -> str:
        parts: list[str] = []
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            if isinstance(value.get("value"), str) or (
                isinstance(value.get("language"), str) and isinstance(value.get("value"), str)
            ):
                parts.append(value["value"])
        elif isinstance(value, list):
            for item in value:
                rendered = LanguageServerManager._safe_hover_text(item)
                if rendered:
                    parts.append(rendered)
        rendered = "\n\n".join(parts)
        rendered = _HTML_TAG.sub("", rendered)
        rendered = _COMMAND_URI.sub("[unsafe URI omitted]", rendered)
        rendered = html.unescape(rendered).replace("\x00", "�")
        encoded = rendered.encode("utf-8")
        if len(encoded) > MAX_LSP_HOVER_BYTES:
            rendered = encoded[:MAX_LSP_HOVER_BYTES].decode("utf-8", "ignore")
            rendered += "\n[hover truncated]"
        return rendered

    @staticmethod
    def _project_diagnostic(
        value: object,
        text: str,
        *,
        encoding: PositionEncoding,
    ) -> dict[str, object] | None:
        if not isinstance(value, dict) or not isinstance(value.get("message"), str):
            return None
        projected_range = model_range_from_lsp(
            text,
            value.get("range"),
            encoding=encoding,
        )
        if projected_range is None:
            return None
        result: dict[str, object] = {
            "message": _bounded_utf8_text(value["message"], 2_000),
            "range": projected_range,
        }
        severity = value.get("severity")
        if isinstance(severity, int) and not isinstance(severity, bool) and 1 <= severity <= 4:
            result["severity"] = severity
        for key in ("source", "code"):
            if isinstance(value.get(key), str | int):
                result[key] = _bounded_utf8_text(str(value[key]), 200)
        return result

    @classmethod
    def _project_symbols(
        cls,
        value: object,
        text: str,
        *,
        encoding: PositionEncoding,
    ) -> tuple[list[dict[str, object]], int]:
        raw = value.get("result", value) if isinstance(value, dict) else value
        if not isinstance(raw, list):
            return [], 1 if raw is not None else 0
        omitted = 0
        total = 0

        def project(item: object, depth: int) -> dict[str, object] | None:
            nonlocal omitted, total
            if depth > MAX_LSP_SYMBOL_DEPTH or total >= MAX_LSP_RESULT_ITEMS:
                omitted += 1
                return None
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                omitted += 1
                return None
            location = item.get("location")
            location_range = location.get("range") if isinstance(location, dict) else None
            range_value = item.get("range") or location_range
            projected_range = model_range_from_lsp(
                text,
                range_value,
                encoding=encoding,
            )
            if projected_range is None:
                omitted += 1
                return None
            total += 1
            result: dict[str, object] = {
                "name": _bounded_utf8_text(item["name"], MAX_LSP_SYMBOL_NAME_BYTES),
                "kind": item.get("kind", 0),
                "range": projected_range,
            }
            children = item.get("children")
            if isinstance(children, list):
                projected_children: list[dict[str, object]] = []
                for child in children:
                    child_result = project(child, depth + 1)
                    if child_result is not None:
                        projected_children.append(child_result)
                if projected_children:
                    result["children"] = projected_children
            return result

        projected = [item for item in (project(item, 0) for item in raw) if item is not None]
        return projected, omitted

    async def _restart(self, request: LspRequest) -> LspOperationResult:
        profile = self._select_profile(request)
        async with self._lock:
            route = self._routes.get(profile.name)
        if route is not None and route.client is not None:
            await self._close_route(route)
            route.client = None
            route.documents.clear()
        return LspOperationResult(
            LspOperation.RESTART,
            {
                "operation": LspOperation.RESTART.value,
                "profile": profile.name,
                "state": "stopped",
                "restart_count": route.restart_count if route is not None else 0,
            },
        )

    def _status_payload(self) -> dict[str, object]:
        statuses: list[dict[str, object]] = []
        for profile in sorted(self._config.language_servers.values(), key=lambda item: item.name):
            route = self._routes.get(profile.name)
            client = route.client if route is not None else None
            error = route.last_error if route is not None else None
            if error is None and client is not None:
                error = client.terminal_error
            stderr = client.stderr_text[-MAX_LSP_STDERR_STATUS_BYTES:] if client is not None else ""
            if stderr:
                stderr = redact_sensitive_text(stderr, explicit_values=self._redaction_values)
            if client is not None and client.alive:
                state = "ready"
            elif error is not None and error.kind in {
                LspFailureKind.SERVER_CRASH,
                LspFailureKind.PROTOCOL_ERROR,
            }:
                state = "crashed"
            else:
                state = "stopped"
            statuses.append(
                {
                    "workspace": str(self._workspace_root),
                    "profile": profile.name,
                    "language": profile.language,
                    "state": state,
                    "capabilities": sorted(client.capabilities) if client is not None else [],
                    "last_error": str(error)[:1_000] if error is not None else None,
                    "error_kind": error.kind.value if error is not None else None,
                    "restart_count": route.restart_count if route is not None else 0,
                    "stderr": stderr,
                }
            )
        return {"operation": LspOperation.STATUS.value, "profiles": statuses}

    @staticmethod
    def _require_capability(route: _Route, capability: str) -> None:
        client = route.client
        if (
            client is None
            or client.capabilities.get(capability) is None
            or client.capabilities.get(capability) is False
        ):
            raise LspError(
                f"configured LSP server does not advertise {capability}",
                kind=LspFailureKind.UNSUPPORTED_CAPABILITY,
                phase=LspFailurePhase.INITIALIZATION,
            )


__all__ = [
    "LSP_RESTART_COOLDOWN_SECONDS",
    "MAX_LSP_DIAGNOSTICS",
    "MAX_LSP_DOCUMENT_BYTES",
    "MAX_LSP_HOVER_BYTES",
    "MAX_LSP_RESTARTS",
    "MAX_LSP_STDERR_STATUS_BYTES",
    "MAX_LSP_SYMBOL_DEPTH",
    "MAX_LSP_SYMBOL_NAME_BYTES",
    "LanguageServerManager",
]
