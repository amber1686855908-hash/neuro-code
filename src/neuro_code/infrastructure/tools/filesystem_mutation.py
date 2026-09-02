"""Workspace mutation tool adapters.

This module owns structured patch parsing/application, exact result-adoption
writes, and search/replace mutation. Every operation uses the shared
filesystem-security target policy; there is no second write boundary here.

工作区修改工具适配器. 本模块拥有结构化补丁解析/应用、精确结果采纳写入
以及 search/replace 修改; 所有操作都使用统一文件系统安全边界.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.result_adoption import WorkspaceMutationRequest
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessPlan,
    FilesystemTargetRequest,
)
from neuro_code.domain.checkpoints import WorkspaceFileEntry
from neuro_code.domain.result_adoption import ResultAdoptionOperation
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.tools.filesystem_security import (
    _display_path,
    _ensure_no_link_components,
    _is_primary_workspace_path,
    _prepare_local_targets,
    _require_string,
    _resolve_path,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError

MAX_APPLY_PATCH_BYTES = 2 * 1024 * 1024
MAX_APPLY_PATCH_FILE_BYTES = 8 * 1024 * 1024

_PATCH_HEADER_PREFIXES = (
    "*** Add File: ",
    "*** Update File: ",
    "*** Delete File: ",
)
_PATCH_HUNK_HEADER = re.compile(r"^(?:@@|@@(?: -(\d+)(?:,\d+)?)?(?: \+\d+(?:,\d+)?)? @@(?:.*))$")


@dataclass(frozen=True, slots=True)
class _PatchHunk:
    old_start: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PatchOperation:
    kind: str
    path: str
    move_to: str | None = None
    add_lines: tuple[str, ...] = ()
    hunks: tuple[_PatchHunk, ...] = ()


def _patch_path(header: str, prefix: str) -> str:
    path = header[len(prefix) :].strip()
    if not path or "\x00" in path:
        raise ToolError("patch file path must be a non-empty text path")
    return path


def _parse_patch_hunk_header(header: str) -> int:
    match = _PATCH_HUNK_HEADER.match(header)
    if match is None:
        raise ToolError("malformed patch hunk header")
    return int(match.group(1) or "1")


def _parse_patch(patch: str) -> tuple[_PatchOperation, ...]:
    if len(patch.encode("utf-8")) > MAX_APPLY_PATCH_BYTES:
        raise ToolError(f"patch must be at most {MAX_APPLY_PATCH_BYTES} bytes")
    lines = patch.splitlines()
    if len(lines) < 2 or lines[0].strip() != "*** Begin Patch":
        raise ToolError("patch must start with *** Begin Patch")
    if "*** End Patch" not in lines:
        raise ToolError("patch must end with *** End Patch")
    operations: list[_PatchOperation] = []
    index = 1
    while index < len(lines):
        header = lines[index]
        if header == "*** End Patch":
            if any(line.strip() for line in lines[index + 1 :]):
                raise ToolError("unexpected content after *** End Patch")
            break
        prefix = next((value for value in _PATCH_HEADER_PREFIXES if header.startswith(value)), None)
        if prefix is None:
            raise ToolError(f"unexpected patch line {index + 1}")
        path = _patch_path(header, prefix)
        kind = prefix.removeprefix("*** ").removesuffix(" File: ").lower()
        index += 1
        if kind == "add":
            added: list[str] = []
            while index < len(lines) and not lines[index].startswith("*** "):
                line = lines[index]
                if not line.startswith("+"):
                    raise ToolError(f"add-file content line {index + 1} must start with +")
                added.append(line[1:])
                index += 1
            operations.append(_PatchOperation("add", path, add_lines=tuple(added)))
            continue
        if kind == "delete":
            if index < len(lines) and not lines[index].startswith("*** "):
                raise ToolError(f"delete-file operation at line {index + 1} has unexpected content")
            operations.append(_PatchOperation("delete", path))
            continue

        move_to: str | None = None
        if index < len(lines) and lines[index].startswith("*** Move to: "):
            move_to = _patch_path(lines[index], "*** Move to: ")
            index += 1
        hunks: list[_PatchHunk] = []
        while index < len(lines) and not lines[index].startswith("*** "):
            header_line = lines[index]
            if not header_line.startswith("@@"):
                raise ToolError(f"update-file content at line {index + 1} must start with @@")
            old_start = _parse_patch_hunk_header(header_line)
            index += 1
            hunk_lines: list[str] = []
            while (
                index < len(lines)
                and not lines[index].startswith("@@")
                and not lines[index].startswith("*** ")
            ):
                hunk_line = lines[index]
                if hunk_line == "\\ No newline at end of file":
                    index += 1
                    continue
                if not hunk_line or hunk_line[0] not in " +-":
                    raise ToolError(f"malformed patch hunk line {index + 1}")
                hunk_lines.append(hunk_line)
                index += 1
            if not hunk_lines:
                raise ToolError(f"patch hunk at line {index + 1} must not be empty")
            hunks.append(_PatchHunk(old_start, tuple(hunk_lines)))
        if not hunks and move_to is None:
            raise ToolError(f"update operation for {path!r} has no hunks")
        operations.append(_PatchOperation("update", path, move_to=move_to, hunks=tuple(hunks)))
    if not operations:
        raise ToolError("patch must contain at least one file operation")
    return tuple(operations)


def _apply_patch_hunks(original: str, hunks: tuple[_PatchHunk, ...]) -> str:
    if len(original.encode("utf-8")) > MAX_APPLY_PATCH_FILE_BYTES:
        raise ToolError(f"target file exceeds {MAX_APPLY_PATCH_FILE_BYTES} bytes")
    newline = "\r\n" if "\r\n" in original else "\n"
    original_lines = original.splitlines()
    had_final_newline = original.endswith(("\n", "\r"))
    current = list(original_lines)
    for hunk in hunks:
        old_lines = tuple(line[1:] for line in hunk.lines if line[0] in " -")
        new_lines = tuple(line[1:] for line in hunk.lines if line[0] in " +")
        if not old_lines:
            # A hunk containing only ``+`` lines is a valid insertion.  The
            # location is still validated against the bounded old-line index,
            # but no existing text is required to match.
            expected = min(max(hunk.old_start, 0), len(current))
            if expected > len(current):
                raise ToolError("patch hunk insertion point is outside the current file")
            current[expected:expected] = new_lines
            continue
        expected = min(max(hunk.old_start - 1, 0), len(current))
        candidates = (
            [expected] if current[expected : expected + len(old_lines)] == list(old_lines) else []
        )
        if not candidates:
            candidates = [
                offset
                for offset in range(len(current) - len(old_lines) + 1)
                if tuple(current[offset : offset + len(old_lines)]) == old_lines
            ]
        if len(candidates) != 1:
            raise ToolError("patch hunk does not match the current file exactly")
        offset = candidates[0]
        current[offset : offset + len(old_lines)] = new_lines
    rendered = newline.join(current)
    return rendered + (newline if had_final_newline else "")


def _add_file_content(lines: tuple[str, ...]) -> str:
    return "\n".join(lines) + ("\n" if lines else "")


class ApplyPatchTool:
    """Apply a validated multi-file patch as one local filesystem mutation."""

    definition = ToolDefinition(
        name="apply_patch",
        description=(
            "Apply a structured workspace patch that may add, update, delete, or move files. "
            "Use it for structural, multi-hunk, or multi-file edits; use search_replace only "
            "for one known exact replacement. Patch content is validated before any write."
        ),
        input_schema={
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        patch = _require_string(arguments, "patch")
        operations = _parse_patch(patch)
        requests: list[FilesystemTargetRequest] = []
        for operation in operations:
            if operation.kind == "add":
                access_operation = FilesystemAccessOperation.CREATE
                must_exist = False
            elif operation.kind == "delete":
                access_operation = FilesystemAccessOperation.DELETE
                must_exist = True
            elif operation.move_to is not None:
                access_operation = FilesystemAccessOperation.MOVE
                must_exist = True
            else:
                access_operation = FilesystemAccessOperation.UPDATE
                must_exist = True
            requests.append(
                FilesystemTargetRequest(
                    operation.path,
                    access_operation,
                    must_exist=must_exist,
                )
            )
            if operation.move_to is not None:
                requests.append(
                    FilesystemTargetRequest(
                        operation.move_to,
                        FilesystemAccessOperation.MOVE,
                        must_exist=False,
                    )
                )
        return _prepare_local_targets("apply_patch", context, tuple(requests))

    def workspace_target_paths(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        """Return patch source and destination paths without touching the workspace."""

        patch = arguments.get("patch")
        if not isinstance(patch, str):
            return ()
        try:
            operations = _parse_patch(patch)
        except ToolError:
            return ()
        paths: list[str] = []
        for operation in operations:
            paths.append(operation.path)
            if operation.move_to is not None:
                paths.append(operation.move_to)
        return tuple(dict.fromkeys(paths))

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        patch = _require_string(arguments, "patch")
        operations = _parse_patch(patch)
        if not context.sandbox_profile.workspace_writable:
            raise ToolError(
                f"sandbox profile {context.sandbox_profile.value!r} prohibits workspace edits"
            )
        resolved = self._resolve_operations(operations, context)
        preflight = self._instruction_preflight(resolved, context)
        if preflight is not None:
            path, instructions_text = preflight
            return ToolResult(
                "I discovered project instructions in the target directory "
                f"that you haven't seen yet ({_display_path(context, path)}). "
                "Please review them before proceeding with the write. "
                "Re-issue the command if you wish to proceed.\n\n" + instructions_text,
                is_error=True,
                metadata={"preflight": "new_instructions", "path": str(path)},
            )
        client = context.client_file_system
        if client is not None:
            return await self._execute_client(resolved, client, context)
        return await run_blocking(self._execute_local, resolved, context)

    def _resolve_operations(
        self,
        operations: tuple[_PatchOperation, ...],
        context: ToolContext,
    ) -> tuple[tuple[_PatchOperation, Path, Path | None], ...]:
        resolved: list[tuple[_PatchOperation, Path, Path | None]] = []
        occupied: set[Path] = set()
        target_index = 0
        for operation in operations:
            _ensure_no_link_components(context, operation.path)
            if operation.kind == "add":
                access_operation = FilesystemAccessOperation.CREATE
                must_exist = False
            elif operation.kind == "delete":
                access_operation = FilesystemAccessOperation.DELETE
                must_exist = True
            elif operation.move_to is not None:
                access_operation = FilesystemAccessOperation.MOVE
                must_exist = True
            else:
                access_operation = FilesystemAccessOperation.UPDATE
                must_exist = True
            source = _resolve_path(
                context,
                operation.path,
                must_exist=must_exist and context.client_file_system is None,
                operation=access_operation,
                target_index=target_index,
            )
            target_index += 1
            destination: Path | None = None
            if operation.move_to is not None:
                _ensure_no_link_components(context, operation.move_to)
                destination = _resolve_path(
                    context,
                    operation.move_to,
                    must_exist=False,
                    operation=FilesystemAccessOperation.MOVE,
                    target_index=target_index,
                )
                target_index += 1
            targets = (source, destination) if destination is not None else (source,)
            for target in targets:
                if target is None or target in occupied:
                    raise ToolError("patch contains duplicate or overlapping file targets")
                occupied.add(target)
                if (
                    not _is_primary_workspace_path(context, target)
                    and context.sandbox_profile is not SandboxProfile.OFF
                ):
                    raise ToolError(
                        "sandboxed sessions permit only read access to additional workspace directories"
                    )
                if context.client_file_system is None and not target.parent.is_dir():
                    raise ToolError(f"patch target parent is not a directory: {target.parent}")
            if operation.kind == "add":
                if context.client_file_system is None and source.exists():
                    raise ToolError(f"cannot add existing file: {_display_path(context, source)}")
            elif context.client_file_system is None and not source.is_file():
                raise ToolError(f"patch source is not a file: {_display_path(context, source)}")
            if (
                destination is not None
                and context.client_file_system is None
                and destination.exists()
            ):
                raise ToolError(
                    f"move destination already exists: {_display_path(context, destination)}"
                )
            resolved.append((operation, source, destination))
        return tuple(resolved)

    @staticmethod
    def _instruction_preflight(
        operations: tuple[tuple[_PatchOperation, Path, Path | None], ...],
        context: ToolContext,
    ) -> tuple[Path, str] | None:
        if context.instruction_tracker is None:
            return None
        checked: set[Path] = set()
        for _operation, source, destination in operations:
            for path in (source, destination):
                if path is None or path in checked or not _is_primary_workspace_path(context, path):
                    continue
                checked.add(path)
                discovered = context.instruction_tracker.check_path_for_write(path)
                if discovered is not None:
                    return path, discovered.model_context_text()
        return None

    async def _execute_client(
        self,
        operations: tuple[tuple[_PatchOperation, Path, Path | None], ...],
        client: ClientFileSystem,
        context: ToolContext,
    ) -> ToolResult:
        if not (client.supports_read and client.supports_write):
            raise ToolError("ACP client does not support patch text-file reads and writes")
        if (
            len(operations) != 1
            or operations[0][0].kind != "update"
            or operations[0][2] is not None
        ):
            raise ToolError(
                "ACP delegated filesystem supports only one-file update patches; "
                "add/delete/move and multi-file transactions are unavailable"
            )
        operation, path, _destination = operations[0]
        try:
            original = await client.read_text_file(path)
        except Exception as error:
            raise ToolError("ACP client could not read the patch target") from error
        updated = _apply_patch_hunks(original, operation.hunks)
        try:
            await client.write_text_file(path, updated)
        except Exception as error:
            raise ToolError("ACP client could not write the patch target") from error
        return ToolResult(
            f"updated {_display_path(context, path)}",
            metadata={
                "changed_files": [_display_path(context, path)],
                "added_files": [],
                "deleted_files": [],
                "moved_files": [],
                "hunks_applied": len(operation.hunks),
                "client_delegated": True,
                "truncated": False,
            },
        )

    @staticmethod
    def _execute_local(
        operations: tuple[tuple[_PatchOperation, Path, Path | None], ...],
        context: ToolContext,
    ) -> ToolResult:
        originals: dict[Path, bytes | None] = {}
        modes: dict[Path, int] = {}
        prepared: dict[Path, tuple[bytes, int]] = {}
        changed: list[str] = []
        added: list[str] = []
        deleted: list[str] = []
        moved: list[dict[str, str]] = []
        hunks_applied = 0
        affected: set[Path] = set()
        for operation, source, destination in operations:
            affected.add(source)
            if destination is not None:
                affected.add(destination)
            if source.exists():
                originals[source] = source.read_bytes()
                modes[source] = source.stat().st_mode
            else:
                originals[source] = None
            if destination is not None:
                originals.setdefault(destination, None)
            if operation.kind == "add":
                added_content = _add_file_content(operation.add_lines)
                prepared[source] = (added_content.encode("utf-8"), 0o100644)
                added.append(_display_path(context, source))
                changed.append(_display_path(context, source))
                continue
            original_bytes = originals[source]
            if original_bytes is None:
                raise ToolError(f"patch source disappeared: {_display_path(context, source)}")
            try:
                original = original_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ToolError(
                    f"patch source is not UTF-8 text: {_display_path(context, source)}"
                ) from error
            if operation.kind == "delete":
                deleted.append(_display_path(context, source))
                changed.append(_display_path(context, source))
                continue
            updated = _apply_patch_hunks(original, operation.hunks)
            hunks_applied += len(operation.hunks)
            target = destination or source
            prepared[target] = (updated.encode("utf-8"), modes[source])
            if destination is None:
                changed.append(_display_path(context, source))
            else:
                moved.append(
                    {
                        "from": _display_path(context, source),
                        "to": _display_path(context, destination),
                    }
                )
                deleted.append(_display_path(context, source))
                changed.extend(
                    (_display_path(context, source), _display_path(context, destination))
                )

        temporary_paths: list[Path] = []

        def stage(path: Path, content: bytes, mode: int) -> Path:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".patch.tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.chmod(temporary_path, mode)
            temporary_paths.append(temporary_path)
            return temporary_path

        staged: dict[Path, Path] = {}
        try:
            for path, (content, mode) in prepared.items():
                staged[path] = stage(path, content, mode)
            for path, temporary in staged.items():
                os.replace(temporary, path)
            for operation, source, destination in operations:
                if operation.kind == "delete" or destination is not None:
                    source.unlink()
        except BaseException as error:
            for path in affected:
                original_bytes_for_restore = originals.get(path)
                try:
                    if original_bytes_for_restore is None:
                        if path.exists():
                            path.unlink()
                    else:
                        restore = stage(
                            path,
                            original_bytes_for_restore,
                            modes.get(path, 0o100644),
                        )
                        os.replace(restore, path)
                except OSError:
                    pass
            raise ToolError("patch transaction failed and was rolled back") from error
        finally:
            for temporary in temporary_paths:
                with suppress(OSError):
                    temporary.unlink()
        return ToolResult(
            f"applied patch to {len(changed)} file change(s)",
            metadata={
                "changed_files": list(dict.fromkeys(changed)),
                "added_files": added,
                "deleted_files": deleted,
                "moved_files": moved,
                "hunks_applied": hunks_applied,
                "truncated": False,
                "client_delegated": False,
            },
        )


class ExactWorkspaceMutationTool:
    """Execute one exact regular-file mutation inside the normal file boundary.

    This tool is intentionally not registered in the model tool collection.
    Result Adoption calls it only through ``ToolExecutor`` so canonical target
    resolution, permission policy, approval memory, instruction preflight, and
    sandbox/profile checks remain the same effective path as ordinary edits.
    """

    definition = ToolDefinition(
        name="apply_patch",
        description="Apply one internal exact workspace mutation.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "operation": {"type": "string"},
            },
            "required": ["path", "operation"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        request = arguments.get("_workspace_mutation_request")
        if not isinstance(request, WorkspaceMutationRequest):
            raise ToolError("internal workspace mutation request is missing")
        operation = {
            ResultAdoptionOperation.CREATE: FilesystemAccessOperation.CREATE,
            ResultAdoptionOperation.UPDATE: FilesystemAccessOperation.UPDATE,
            ResultAdoptionOperation.DELETE: FilesystemAccessOperation.DELETE,
        }[request.operation]
        return _prepare_local_targets(
            self.definition.name,
            context,
            (
                FilesystemTargetRequest(
                    request.path,
                    operation,
                    must_exist=operation is not FilesystemAccessOperation.CREATE,
                ),
            ),
        )

    def workspace_target_paths(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        path = arguments.get("path")
        return (path,) if isinstance(path, str) and path else ()

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        request = arguments.get("_workspace_mutation_request")
        if not isinstance(request, WorkspaceMutationRequest):
            raise ToolError("internal workspace mutation request is missing")
        if context.client_file_system is not None:
            raise ToolError("result adoption requires the local parent filesystem")
        if not context.sandbox_profile.workspace_writable:
            raise ToolError(
                f"sandbox profile {context.sandbox_profile.value!r} prohibits workspace edits"
            )
        plan = self.prepare_filesystem_targets(arguments, context)
        if plan is None:
            raise ToolError("internal workspace mutation has no canonical target plan")
        target = plan.target_at(0)
        path = target.canonical_path
        if context.instruction_tracker is not None:
            discovered = context.instruction_tracker.check_path_for_write(path)
            if discovered is not None:
                raise ToolError("project instructions require review before result adoption")
        _assert_exact_regular_image(path, request.expected)
        if request.operation is ResultAdoptionOperation.DELETE:
            if request.desired is not None:
                raise ToolError("delete mutation cannot carry a desired image")
            try:
                path.unlink()
            except OSError as error:
                raise ToolError("result adoption delete failed") from error
        else:
            desired = request.desired
            if desired is None or desired.content is None:
                raise ToolError("result adoption write requires regular-file content")
            if desired.kind.value != "regular":
                raise ToolError("result adoption supports regular files only")
            _write_exact_regular(path, request.expected, desired.content, desired.mode)
        return ToolResult(
            f"adopted {request.operation.value} {request.path}",
            metadata={
                "changed_files": [request.path],
                "operation": request.operation.value,
                "internal_result_adoption": True,
            },
        )


def _assert_exact_regular_image(path: Path, expected: object) -> None:
    """Compare one target immediately before execution without following links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if expected is None:
            return
        raise ToolError("workspace target changed before result adoption") from None
    except OSError as error:
        raise ToolError("workspace target could not be inspected safely") from error
    if expected is None:
        raise ToolError("workspace target changed before result adoption")
    if not isinstance(expected, WorkspaceFileEntry) or expected.kind.value != "regular":
        raise ToolError("result adoption expected image is unsupported")
    if not stat.S_ISREG(metadata.st_mode):
        raise ToolError("workspace target is link-like or not a regular file")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ToolError("workspace target could not be read safely") from error
    mode = 0o100755 if metadata.st_mode & 0o111 else 0o100644
    if content != expected.content or mode != expected.mode:
        raise ToolError("workspace target changed before result adoption")


def _write_exact_regular(
    path: Path,
    expected: WorkspaceFileEntry | None,
    content: bytes,
    mode: int,
) -> None:
    """Stage an exact regular-file image and recheck before replacement."""

    if mode not in {0o100644, 0o100755}:
        raise ToolError("result adoption file mode is unsupported")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".adoption.tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, mode)
        _assert_exact_regular_image(path, expected)
        os.replace(temporary_path, path)
    except OSError as error:
        raise ToolError("result adoption write failed") from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink()


class SearchReplaceTool:
    definition = ToolDefinition(
        name="search_replace",
        description=(
            "Replace a known exact text occurrence in one UTF-8 workspace file atomically. "
            "Use apply_patch for structural, multi-hunk, multi-file, add, delete, or move edits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    )
    side_effecting = True

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        requested = _require_string(arguments, "path")
        return _prepare_local_targets(
            "search_replace",
            context,
            (
                FilesystemTargetRequest(
                    requested, FilesystemAccessOperation.UPDATE, must_exist=True
                ),
            ),
        )

    def workspace_target_paths(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        path = arguments.get("path")
        return (path,) if isinstance(path, str) and path else ()

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not context.sandbox_profile.workspace_writable:
            raise ToolError(
                f"sandbox profile {context.sandbox_profile.value!r} prohibits workspace edits"
            )
        client_file_system = context.client_file_system
        requested_path = _require_string(arguments, "path")
        _ensure_no_link_components(context, requested_path)
        path = _resolve_path(
            context,
            requested_path,
            must_exist=client_file_system is None,
            operation=FilesystemAccessOperation.UPDATE,
        )
        old = _require_string(arguments, "old")
        new = arguments.get("new")
        replace_all = arguments.get("replace_all", False)
        if not isinstance(new, str):
            raise ToolError("new must be a string")
        if not isinstance(replace_all, bool):
            raise ToolError("replace_all must be a boolean")
        if client_file_system is None and not path.is_file():
            raise ToolError(f"not a file: {path}")
        if (
            _is_primary_workspace_path(context, path) is False
            and context.sandbox_profile is not SandboxProfile.OFF
        ):
            raise ToolError(
                "sandboxed sessions permit only read access to additional workspace directories"
            )
        if context.instruction_tracker is not None and _is_primary_workspace_path(context, path):
            new_instructions = context.instruction_tracker.check_path_for_write(path)
            if new_instructions is not None:
                instructions_text = new_instructions.model_context_text()
                rel = _display_path(context, path)
                return ToolResult(
                    "I discovered project instructions in the target directory "
                    f"that you haven't seen yet ({rel}). "
                    "Please review them before proceeding with the write. "
                    "Re-issue the command if you wish to proceed.\n\n" + instructions_text,
                    is_error=True,
                    metadata={"path": str(path), "preflight": "new_instructions"},
                )

        async def replace_client_text(file_system: ClientFileSystem) -> int:
            if not (file_system.supports_read and file_system.supports_write):
                raise ToolError("ACP client does not support text-file replacement")
            original = await file_system.read_text_file(path)
            count = original.count(old)
            if count == 0:
                raise ToolError("old text was not found")
            if count > 1 and not replace_all:
                raise ToolError(f"old text is ambiguous: found {count} occurrences")
            updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
            await file_system.write_text_file(path, updated)
            return count if replace_all else 1

        def replace_text() -> int:
            original = path.read_text(encoding="utf-8")
            count = original.count(old)
            if count == 0:
                raise ToolError("old text was not found")
            if count > 1 and not replace_all:
                raise ToolError(f"old text is ambiguous: found {count} occurrences")
            updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
            mode = path.stat().st_mode
            temporary_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary.write(updated)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_name = temporary.name
                os.chmod(temporary_name, mode)
                os.replace(temporary_name, path)
            finally:
                if temporary_name is not None and os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            return count if replace_all else 1

        if client_file_system is None:
            replaced = await run_blocking(replace_text)
        else:
            replaced = await replace_client_text(client_file_system)
        return ToolResult(
            f"replaced {replaced} occurrence(s) in {_display_path(context, path)}",
            metadata={
                "path": str(path),
                "replacements": replaced,
                "client_delegated": client_file_system is not None,
            },
        )


__all__ = [
    "ApplyPatchTool",
    "ExactWorkspaceMutationTool",
    "SearchReplaceTool",
]
