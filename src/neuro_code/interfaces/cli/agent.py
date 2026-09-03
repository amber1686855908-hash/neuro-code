"""Headless Agent command execution for the CLI.

CLI 无头 Agent 命令的执行边界.

The command owns CLI stream projection and resource cleanup for one headless
turn.  Runtime behavior and provider construction remain application and
bootstrap responsibilities.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import cast

from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions.service import ResumeSessionRequest
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.interfaces.cli.contracts import CliServices
from neuro_code.interfaces.cli.interaction import CliUserInteraction
from neuro_code.interfaces.cli.serialization import serialize_execution_outcome
from neuro_code.interfaces.cli.settings import _application_settings
from neuro_code.shared.errors import ConfigurationError


async def run_agent(args: argparse.Namespace, services: CliServices) -> int:
    """Run one bounded headless Agent turn and project its result."""
    if not args.prompt:
        raise ConfigurationError(
            "the agent subcommand requires -p/--single; run neuro without a subcommand "
            "for the interactive TUI"
        )
    application = await services.open_application(_application_settings(args))
    try:
        if args.resume is not None:
            await application.session_service.prepare_resume(ResumeSessionRequest(args.resume))
        binding = await application.create_binding(
            resume_id=args.resume,
            user_interaction=CliUserInteraction(interactive=args.output_format == "plain"),
        )

        async def stream_event(event: AgentEvent) -> None:
            if args.output_format == "plain" and event.kind is AgentEventKind.TEXT_DELTA:
                text = event.data.get("text")
                if isinstance(text, str):
                    print(text, end="", flush=True)
            elif args.output_format == "jsonl":
                if event.kind is not AgentEventKind.MODEL_REQUEST_SNAPSHOT:
                    print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)

        async def ultracode_delegate(
            request: RunTurnRequest,
            sink: EventSink | None,
        ) -> AgentRunResult:
            service = await application.create_ultracode_delegation_service(
                parent_binding=binding,
            )
            return cast(AgentRunResult, await service.run_turn(request, sink=sink))

        if getattr(binding.runner, "reasoning_effort", None) is ReasoningEffort.ULTRACODE:
            turn_service = application.session_service.bind_runner(
                binding.runner,
                ultracode_delegate=ultracode_delegate,
            )
        else:
            turn_service = application.session_service.bind_runner(binding.runner)
        result = await turn_service.run_turn(
            RunTurnRequest(
                args.prompt,
                expected_session_id=args.resume,
            ),
            sink=stream_event,
        )
        if args.output_format == "plain":
            print()
        elif args.output_format == "json":
            print(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "response": result.response,
                        "steps": result.steps,
                        "events": [
                            event.to_dict()
                            for event in result.events
                            if event.kind is not AgentEventKind.MODEL_REQUEST_SNAPSHOT
                        ],
                        "outcome": serialize_execution_outcome(result.outcome),
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    finally:
        await asyncio.shield(application.close())


__all__ = ["run_agent"]
