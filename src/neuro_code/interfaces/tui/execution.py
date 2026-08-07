"""Bounded execution metadata projections for the TUI.

TUI 使用的有界执行 metadata 投影.

This module converts untrusted event data into an existing domain status. It
does not choose a recovery action, access persistence, or render widgets.

本模块把不可信事件数据转换为已有领域状态,不选择恢复动作、不访问持久化、也不渲染组件.
"""

from __future__ import annotations

from collections.abc import Mapping

from neuro_code.domain.execution import AgentExecutionStatus

__all__ = ["recoverable_terminal_status"]

_RECOVERABLE_TERMINAL_STATUSES = frozenset(
    {AgentExecutionStatus.STUCK, AgentExecutionStatus.BUDGET_LIMITED}
)


def recoverable_terminal_status(
    data: Mapping[str, object],
) -> AgentExecutionStatus | None:
    """Return a supported recoverable terminal status from event metadata.

    从事件 metadata 中返回受支持的可恢复终态,未知或不安全值返回 None.
    """

    if data.get("recoverable") is not True:
        return None
    raw_status = data.get("execution_status")
    if not isinstance(raw_status, str):
        return None
    try:
        status = AgentExecutionStatus(raw_status)
    except ValueError:
        return None
    return status if status in _RECOVERABLE_TERMINAL_STATUSES else None
