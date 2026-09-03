"""Explicit CLI commands for bounded subagent operations.

CLI 有界子代理操作命令.

This module owns only the inbound command sequencing and safe output
projection.  Parent capability, relationship, and child-runtime semantics
remain application-owned.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipAction,
    SubagentRelationshipActionRequest,
)
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.workflows.subagent import RunSubagentRequest
from neuro_code.interfaces.cli.contracts import CliServices
from neuro_code.interfaces.cli.serialization import (
    serialize_subagent_relationship_action,
    serialize_subagent_result,
)
from neuro_code.interfaces.cli.settings import _application_settings
from neuro_code.shared.errors import ConfigurationError


async def run_subagent(args: argparse.Namespace, services: CliServices) -> int:
    """Run one explicit read-only child and print its safe projection.

    运行一次明确的只读子代理并输出安全投影.
    """

    application = await services.open_application(_application_settings(args))
    parent_binding = None
    try:
        parent_config = await application.config_for_session_resume(args.parent_session)
        parent_binding = await application.create_binding(
            config=parent_config,
            resume_id=args.parent_session,
        )
        parent_capabilities = parent_binding.capabilities
        if parent_capabilities is None:
            raise ConfigurationError("parent binding capability metadata is missing")
        try:
            request = RunSubagentRequest(
                args.parent_session,
                args.prompt,
                max_steps=args.max_steps,
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from None
        service = application.create_read_only_subagent_application_service()
        projection = await service.run_subagent(
            request,
            parent_capabilities=parent_capabilities,
        )
        if args.json:
            print(json.dumps(serialize_subagent_result(projection), ensure_ascii=False))
        else:
            print(projection.response)
        return 0
    finally:
        if parent_binding is not None and parent_binding.background_tasks is not None:
            await asyncio.shield(parent_binding.background_tasks.shutdown())
        await asyncio.shield(application.close())


async def run_subagent_lifecycle(args: argparse.Namespace, services: CliServices) -> int:
    """Run one explicit lifecycle action for a linked child session.

    对关联子会话执行一次明确的生命周期动作.

    The parent is checked through the composition workspace/resume boundary;
    the lifecycle service then owns relationship and terminal-task validation.
    No model turn is started by this command.
    先通过组合根工作区/恢复边界校验父会话,再由生命周期服务负责关系和终态任务校验.
    本命令不会启动模型回合.
    """

    application = await services.open_application(
        ApplicationSettings(
            cwd=args.cwd,
            resume_id=args.parent_session,
        )
    )
    try:
        await application.config_for_session_resume(args.parent_session)
        action = SubagentRelationshipAction(args.action)
        result = await application.create_subagent_relationship_lifecycle_service().execute(
            SubagentRelationshipActionRequest(
                parent_session_id=args.parent_session,
                parent_task_id=args.task_id,
                action=action,
            )
        )
        payload = serialize_subagent_relationship_action(result)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        elif action is SubagentRelationshipAction.RESUME:
            print(f"Child session {result.child_session_id} is ready to resume.")
        elif action is SubagentRelationshipAction.FORK:
            assert result.forked_session_id is not None
            print(
                f"Forked child session {result.forked_session_id}; it was not opened automatically."
            )
        else:
            print(f"Deleted child session {result.child_session_id}.")
        return 0
    finally:
        await asyncio.shield(application.close())


__all__ = ["run_subagent", "run_subagent_lifecycle"]
