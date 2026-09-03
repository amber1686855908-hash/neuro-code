"""Import and export command handlers for persisted sessions.

持久化 session 的导入与导出命令处理器.

The application session services own persistence semantics; this adapter owns
only file/stdout selection and the established CLI projections.
"""

from __future__ import annotations

import argparse
import json

from neuro_code.application.sessions.lifecycle import (
    ImportSessionRequest,
    SessionLifecycleService,
)
from neuro_code.application.sessions.service import (
    ExportSessionRequest,
    SessionApplicationService,
)
from neuro_code.interfaces.cli.contracts import CliServices
from neuro_code.interfaces.cli.serialization import render_session_markdown


async def export_session(args: argparse.Namespace, services: CliServices) -> int:
    config = services.load_config(args.cwd)
    store = await services.create_session_store(config)
    exported = await SessionApplicationService(store).export_session(
        ExportSessionRequest(args.session_id, include_events=args.format == "json")
    )
    summary = exported.snapshot.summary
    items = exported.snapshot.items
    messages = exported.snapshot.messages
    if args.format == "json":
        content = (
            json.dumps(
                {
                    "schema_version": 4,
                    "session": summary.to_dict(),
                    "messages": [message.to_dict() for message in messages],
                    "conversation_items": [item.to_dict() for item in items],
                    "events": [dict(event) for event in exported.events],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        content = render_session_markdown(items)
    if args.output is None:
        print(content, end="")
    else:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(output)
    return 0


async def import_session(args: argparse.Namespace, services: CliServices) -> int:
    config = services.load_config(args.cwd)
    imported = await services.load_rust_session(args.source)
    store = await services.create_session_store(config)
    await SessionLifecycleService(store).import_session(ImportSessionRequest(imported.snapshot))
    if args.json:
        print(json.dumps(imported.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"Imported upstream Rust session {imported.snapshot.summary.id}: "
            f"{imported.imported_messages}/{imported.total_records} messages, "
            f"{imported.preserved_context_records} context records preserved "
            f"({imported.recovered_context_records} recovered, "
            f"{imported.deduplicated_context_records} duplicates skipped), "
            f"{imported.invalid_records} invalid and "
            f"{imported.unsupported_records} unsupported records skipped; "
            f"{imported.invalid_embedded_records} invalid and "
            f"{imported.unsupported_embedded_records} unsupported embedded items ignored."
        )
    return 0


__all__ = ["export_session", "import_session"]
