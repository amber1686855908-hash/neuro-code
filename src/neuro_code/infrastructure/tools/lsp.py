"""Stable read-only model tool for semantic Language Server Protocol queries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from neuro_code.application.ports.lsp import (
    MAX_LSP_RESULT_ITEMS,
    LanguageServerService,
    LspError,
    LspFailureKind,
    LspFailurePhase,
    LspOperation,
    LspRequest,
)
from neuro_code.application.ports.tools import Tool, ToolContext
from neuro_code.application.ports.workspace import (
    FilesystemAccessOperation,
    FilesystemAccessPlan,
    FilesystemTargetProvider,
    FilesystemTargetRequest,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import ToolError


class LspTool(Tool, FilesystemTargetProvider):
    """Expose only read-only semantic operations; edits never enter the schema."""

    definition = ToolDefinition(
        name="lsp",
        description=(
            "Use the configured read-only Language Server Protocol server for "
            "definition, references, hover, document/workspace symbols, diagnostics, "
            "status, or a bounded restart. Use grep for plain-text search."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [operation.value for operation in LspOperation],
                },
                "path": {"type": "string"},
                "line": {"type": "integer", "minimum": 1},
                "column": {"type": "integer", "minimum": 1},
                "query": {"type": "string", "minLength": 1, "maxLength": 4096},
                "profile": {"type": "string", "minLength": 1, "maxLength": 128},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LSP_RESULT_ITEMS,
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    def __init__(self, service: LanguageServerService | None = None) -> None:
        self._service = service

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
        /,
    ) -> FilesystemAccessPlan | None:
        operation = self._operation(arguments)
        if operation not in {
            LspOperation.DEFINITION,
            LspOperation.REFERENCES,
            LspOperation.HOVER,
            LspOperation.DOCUMENT_SYMBOLS,
            LspOperation.DIAGNOSTICS,
        }:
            return None
        path = self._string(arguments, "path")
        if context.client_file_system is not None:
            return None
        from neuro_code.infrastructure.workspace.paths import resolve_filesystem_access_targets

        return resolve_filesystem_access_targets(
            "lsp",
            context.cwd,
            (
                FilesystemTargetRequest(
                    path,
                    FilesystemAccessOperation.READ,
                    must_exist=True,
                    reject_link_like=True,
                ),
            ),
            additional_workspace_roots=context.additional_workspace_roots,
        )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        try:
            operation = self._operation(arguments)
            if self._service is None:
                raise LspError(
                    "no LSP service is configured for this workspace",
                    kind=LspFailureKind.NOT_CONFIGURED,
                    phase=LspFailurePhase.CONFIGURATION,
                )
            path = self._path_from_context(arguments, context, operation)
            request = LspRequest(
                operation=operation,
                path=path,
                line=self._optional_positive_int(arguments, "line"),
                column=self._optional_positive_int(arguments, "column"),
                query=arguments.get("query") if isinstance(arguments.get("query"), str) else None,
                profile=arguments.get("profile")
                if isinstance(arguments.get("profile"), str)
                else None,
                max_results=self._max_results(arguments),
            )
            result = await self._service.execute(request)
            content = json.dumps(
                dict(result.payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(content.encode("utf-8")) > context.output_byte_limit:
                content = (
                    content.encode("utf-8")[: context.output_byte_limit].decode("utf-8", "ignore")
                    + "\n[output truncated]"
                )
                return ToolResult(content, metadata={"truncated": True})
            return ToolResult(content)
        except LspError as error:
            return self._error_result(error)
        except ValueError as error:
            return self._error_result(
                LspError(
                    str(error),
                    kind=LspFailureKind.DOCUMENT_ERROR,
                    phase=LspFailurePhase.REQUEST,
                )
            )

    @staticmethod
    def _error_result(error: LspError) -> ToolResult:
        payload = {
            "error": {
                "kind": error.kind.value,
                "phase": error.phase.value,
                "message": str(error),
                "retryable": error.retryable,
            }
        }
        return ToolResult(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            is_error=True,
            metadata={
                "error_kind": error.kind.value,
                "phase": error.phase.value,
                "retryable": error.retryable,
            },
        )

    @staticmethod
    def _operation(arguments: Mapping[str, Any]) -> LspOperation:
        value = arguments.get("operation")
        if not isinstance(value, str):
            raise ToolError("lsp operation must be a string")
        try:
            return LspOperation(value)
        except ValueError as error:
            raise ToolError(f"unsupported lsp operation: {value!r}") from error

    @staticmethod
    def _string(arguments: Mapping[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            raise ToolError(f"lsp {key} must be a non-empty string")
        return value

    @staticmethod
    def _optional_positive_int(arguments: Mapping[str, Any], key: str) -> int | None:
        value = arguments.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ToolError(f"lsp {key} must be a positive integer")
        return value

    @staticmethod
    def _max_results(arguments: Mapping[str, Any]) -> int:
        value = arguments.get("max_results", MAX_LSP_RESULT_ITEMS)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_LSP_RESULT_ITEMS
        ):
            raise ToolError(f"lsp max_results must be between 1 and {MAX_LSP_RESULT_ITEMS}")
        return value

    @staticmethod
    def _path_from_context(
        arguments: Mapping[str, Any],
        context: ToolContext,
        operation: LspOperation,
    ) -> Path | None:
        needs_path = operation in {
            LspOperation.DEFINITION,
            LspOperation.REFERENCES,
            LspOperation.HOVER,
            LspOperation.DOCUMENT_SYMBOLS,
            LspOperation.DIAGNOSTICS,
        }
        if not needs_path:
            return None
        requested = LspTool._string(arguments, "path")
        if context.client_file_system is not None:
            raise LspError(
                "LSP requires a local canonical filesystem authority",
                kind=LspFailureKind.DOCUMENT_ERROR,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            )
        plan = context.filesystem_access_plan
        if plan is None:
            raise LspError(
                "LSP input did not receive a canonical filesystem plan",
                kind=LspFailureKind.SECURITY_FILTERED,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            )
        target = plan.target_at(0)
        if (
            target.requested_path != requested
            or target.operation is not FilesystemAccessOperation.READ
        ):
            raise LspError(
                "LSP input does not match its canonical filesystem plan",
                kind=LspFailureKind.SECURITY_FILTERED,
                phase=LspFailurePhase.DOCUMENT_SYNC,
            )
        return target.canonical_path


__all__ = ["LspTool"]
