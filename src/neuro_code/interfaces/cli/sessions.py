"""Canonical execution boundary for the parsed CLI sessions command.

CLI sessions 命令已解析输入的规范执行边界.

This module owns request validation, application-service selection, command
execution, and presentation for the existing sessions command. Parser grammar
and top-level dispatch live in ``neuro_code.interfaces.cli.parser`` and
``neuro_code.interfaces.cli.dispatch``.

本模块拥有既有 sessions 命令的请求校验、应用服务选择、命令执行和展示.
Parser grammar 与顶层 dispatch 位于 ``neuro_code.interfaces.cli.parser`` 和
``neuro_code.interfaces.cli.dispatch``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionCommandResult,
)
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES
from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.sessions.catalog import (
    ListSessionsRequest,
    SearchSessionsRequest,
    SessionCatalogApplicationService,
)
from neuro_code.application.sessions.lifecycle import (
    RenameSessionRequest,
    SessionLifecycleService,
)
from neuro_code.application.sessions.recovery import TurnRecoveryService
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.tools.service import (
    ListSessionToolOutputArtifactsRequest,
    ReadSessionToolOutputArtifactRequest,
    SessionToolOutputArtifactApplicationService,
)
from neuro_code.interfaces.cli.serialization import (
    serialize_execution_record,
    serialize_session_search_page,
    serialize_tool_output_artifact,
    serialize_tool_output_artifact_read,
)
from neuro_code.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from neuro_code.application.ports.configuration import AppConfig


class SessionCliRunner(Protocol):
    """Minimum runner surface required by compact and recovery retry."""

    async def compact_now(self) -> ContextCompactionCommandResult: ...

    async def retry_recovery(self, turn_id: str) -> AgentRunResult: ...


class SessionCliBinding(Protocol):
    """Minimum binding surface required by the sessions command."""

    @property
    def runner(self) -> SessionCliRunner: ...


class SessionCliApplication(Protocol):
    """Minimum opened-application surface required by sessions commands."""

    async def config_for_session_resume(self, session_id: str) -> object: ...

    async def create_binding(
        self,
        *,
        resume_id: str | None = None,
    ) -> SessionCliBinding: ...

    async def close(self) -> None: ...


class SessionCliServices(Protocol):
    """Narrow bootstrap-provided service contract for session commands."""

    def load_config(self, cwd: Path | None) -> AppConfig: ...

    async def create_session_store(self, config: AppConfig) -> SessionStore: ...

    def create_tool_output_artifact_service(
        self,
        config: AppConfig,
        store: SessionStore,
    ) -> SessionToolOutputArtifactApplicationService: ...

    async def open_application(self, settings: ApplicationSettings) -> SessionCliApplication: ...


async def run_sessions_command(args: argparse.Namespace, services: SessionCliServices) -> int:
    """Execute the already-parsed sessions command without owning parser state."""

    config = services.load_config(args.cwd)
    store = await services.create_session_store(config)
    session_lifecycle = SessionLifecycleService(store)
    session_catalog = SessionCatalogApplicationService(store)
    if args.session_action == "recover":
        if args.query is None or not args.query.strip():
            raise ConfigurationError("sessions recover requires a session ID")
        if args.limit != 50 or args.offset != 0 or args.include_content or args.prune:
            raise ConfigurationError(
                "sessions recover accepts only a session ID and recovery options"
            )
        recovery = TurnRecoveryService(store)
        if args.action == "inspect":
            if args.title is not None:
                raise ConfigurationError("sessions recover inspect does not accept a turn ID")
            recovery_inspections = await recovery.inspect(args.query)
            recovery_payload = [inspection.to_dict() for inspection in recovery_inspections]
            if args.json:
                print(json.dumps(recovery_payload, ensure_ascii=False))
            elif not recovery_payload:
                print("No turn recovery attempts found.")
            else:
                for recovery_row in recovery_payload:
                    print(
                        f"{recovery_row['turn_id']}\t{recovery_row['status']}\t"
                        f"{recovery_row['last_stage']}\t"
                        "input_reconstructable="
                        f"{str(recovery_row['input_reconstructable']).lower()}\t"
                        "retry_available="
                        f"{str(recovery_row['retry_available']).lower()}\t"
                        "abandon_available="
                        f"{str(recovery_row['abandon_available']).lower()}\t"
                        f"reason={recovery_row['reason']}"
                    )
            return 0
        if args.title is None or not args.title.strip():
            raise ConfigurationError("sessions recover action requires a turn ID")
        if args.action == "abandon":
            abandoned = await recovery.abandon(args.query, args.title, reason=args.reason)
            abandoned_payload = abandoned.to_dict()
            if args.json:
                print(json.dumps(abandoned_payload, ensure_ascii=False))
            else:
                print(f"Turn {args.title} is now {abandoned_payload['status']}.")
            return 0
        if args.action == "retry":
            application = await services.open_application(
                ApplicationSettings(cwd=args.cwd, resume_id=args.query)
            )
            try:
                await application.config_for_session_resume(args.query)
                binding = await application.create_binding(resume_id=args.query)
                retry_result = await binding.runner.retry_recovery(args.title)
                payload = {
                    "status": "retried",
                    "session_id": retry_result.session_id,
                    "steps": retry_result.steps,
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False))
                else:
                    print(f"Turn {args.title} was explicitly retried.")
                return 0
            finally:
                await asyncio.shield(application.close())
        raise ConfigurationError(f"unsupported recovery action: {args.action}")
    if args.session_action == "compact":
        if args.query is None or not args.query.strip():
            raise ConfigurationError("sessions compact requires a session ID")
        if args.title is not None or args.prune or args.include_content or args.offset != 0:
            raise ConfigurationError("sessions compact accepts only a session ID")
        application = await services.open_application(
            ApplicationSettings(cwd=args.cwd, resume_id=args.query)
        )
        try:
            await application.config_for_session_resume(args.query)
            binding = await application.create_binding(resume_id=args.query)
            compaction_result = await binding.runner.compact_now()
            payload = {
                "status": compaction_result.status.value,
                "triggered": compaction_result.triggered,
                "compaction_id": compaction_result.compaction_id,
                "source_item_count": compaction_result.source_item_count,
                "candidate_item_count": compaction_result.candidate_item_count,
                "summary_tokens": compaction_result.summary_tokens,
                "summary_truncated": compaction_result.summary_truncated,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(
                    f"Context compaction: {compaction_result.status.value}"
                    + (
                        f" ({compaction_result.compaction_id})"
                        if compaction_result.compaction_id is not None
                        else ""
                    )
                )
            return 0
        finally:
            await asyncio.shield(application.close())
    if args.session_action == "artifacts":
        artifact_service = services.create_tool_output_artifact_service(config, store)
        if args.prune:
            if args.query is not None or args.title is not None:
                raise ConfigurationError("--prune cannot be combined with a session or artifact ID")
            if (
                args.limit != 50
                or args.offset != 0
                or args.include_content
                or args.max_bytes != MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES
            ):
                raise ConfigurationError(
                    "--limit, --offset, --include-content and --max-bytes are not valid with --prune"
                )
            prune_result = await artifact_service.prune_unreferenced()
            payload = {
                "deleted": prune_result.deleted_count,
                "preserved": prune_result.preserved_count,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(
                    f"Pruned {prune_result.deleted_count} artifact(s); "
                    f"preserved {prune_result.preserved_count}."
                )
            return 0
        if args.query is None or not args.query.strip():
            raise ConfigurationError("sessions artifacts requires a session ID")
        if args.offset != 0 or args.include_content:
            raise ConfigurationError(
                "--offset and --include-content are not valid for sessions artifacts"
            )
        if args.title is None:
            references = await artifact_service.list(
                ListSessionToolOutputArtifactsRequest(args.query, limit=args.limit)
            )
            if args.max_bytes != MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES:
                raise ConfigurationError(
                    "--max-bytes requires an artifact ID for sessions artifacts"
                )
            if args.json:
                print(
                    json.dumps(
                        [serialize_tool_output_artifact(reference) for reference in references],
                        ensure_ascii=False,
                    )
                )
            elif not references:
                print("No tool output artifacts found.")
            else:
                for reference in references:
                    artifact = reference.artifact
                    print(
                        f"{artifact.artifact_id}\t{artifact.byte_count} bytes\t"
                        f"truncated={str(artifact.truncated).lower()}\t"
                        f"event={reference.event_sequence}"
                    )
            return 0

        if args.limit != 50:
            raise ConfigurationError("--limit is not valid when an artifact ID is provided")
        artifact_result = await artifact_service.read(
            ReadSessionToolOutputArtifactRequest(
                args.query,
                args.title,
                max_bytes=args.max_bytes,
            )
        )
        if args.json:
            print(
                json.dumps(
                    serialize_tool_output_artifact_read(
                        artifact_result.artifact.artifact_id,
                        artifact_result.content,
                        artifact_result.read_truncated,
                    ),
                    ensure_ascii=False,
                )
            )
        else:
            print(
                artifact_result.content,
                end="" if artifact_result.content.endswith("\n") else "\n",
            )
            if artifact_result.read_truncated:
                print("[output truncated at the requested read limit]")
        return 0
    if args.prune:
        raise ConfigurationError("--prune is only valid for sessions artifacts")
    if args.session_action == "search":
        if args.title is not None:
            raise ConfigurationError("sessions search accepts exactly one query")
        if args.query is None or not args.query.strip():
            raise ConfigurationError("sessions search requires a non-empty query")
        page = await session_catalog.search_sessions(
            SearchSessionsRequest(
                args.query,
                limit=args.limit,
                offset=args.offset,
                include_content=args.include_content,
            )
        )
        if args.json:
            print(
                json.dumps(
                    await serialize_session_search_page(page),
                    ensure_ascii=False,
                )
            )
        elif not page.results:
            print("No matching sessions found.")
        else:
            for inspection in page.results:
                hit = inspection.hit
                session = hit.summary
                title = session.title or "New session"
                fields = ",".join(hit.matched_fields)
                print(
                    f"{session.id}\t{session.updated_at.isoformat()}\t"
                    f"{session.provider}/{session.model}\t{title}\tmatch={fields}"
                )
                if hit.snippet is not None:
                    print(f"  {hit.snippet}")
        return 0
    if args.session_action == "rename":
        if args.query is None or not args.query.strip():
            raise ConfigurationError("sessions rename requires a session ID")
        if args.title is None or not args.title.strip():
            raise ConfigurationError("sessions rename requires a non-empty title")
        if args.limit != 50 or args.offset != 0 or args.include_content:
            raise ConfigurationError(
                "--limit, --offset and --include-content are not valid for sessions rename"
            )
        summary = await session_lifecycle.rename_session(
            RenameSessionRequest(args.query, args.title)
        )
        if args.json:
            print(json.dumps(summary.to_dict(), ensure_ascii=False))
        else:
            print(f"Renamed session {summary.id} to {summary.title!r}.")
        return 0
    if args.query is not None:
        raise ConfigurationError("sessions list does not accept a query")
    if args.title is not None:
        raise ConfigurationError("sessions list does not accept a title")
    if args.offset != 0 or args.include_content:
        raise ConfigurationError(
            "--offset and --include-content are only valid for sessions search"
        )
    inspections = await session_catalog.list_sessions(ListSessionsRequest(args.limit))
    if args.json:
        rows: list[dict[str, object]] = []
        for session_inspection in inspections:
            row: dict[str, object] = dict(session_inspection.summary.to_dict())
            row["last_execution"] = serialize_execution_record(session_inspection.execution_record)
            rows.append(row)
        print(json.dumps(rows, ensure_ascii=False))
    elif not inspections:
        print("No sessions found.")
    else:
        for session_inspection in inspections:
            session = session_inspection.summary
            print(
                f"{session.id}\t{session.updated_at.isoformat()}\t"
                f"{session.provider}/{session.model}\t"
                f"sandbox={session.sandbox_profile.value if session.sandbox_profile else 'legacy'}"
                f"\t{session.title or 'New session'}\t{session.cwd}"
            )
    return 0


__all__ = [
    "SessionCliApplication",
    "SessionCliBinding",
    "SessionCliRunner",
    "SessionCliServices",
    "run_sessions_command",
]
