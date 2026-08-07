"""Conversation operating modes and their provider-neutral guidance.

定义会话工作模式及与 Provider 无关的指引."""

from __future__ import annotations

from enum import StrEnum


class InteractionMode(StrEnum):
    """Application-owned operating modes for an interactive coding session.

    定义交互式编码会话的应用层工作模式."""

    NORMAL = "normal"
    ACCEPT_EDITS = "accept-edits"
    PLAN = "plan"
    AUTO = "auto"

    @property
    def glyph(self) -> str:
        return {
            InteractionMode.NORMAL: "◇",
            InteractionMode.ACCEPT_EDITS: "◆",
            InteractionMode.PLAN: "▣",
            InteractionMode.AUTO: "▶",
        }[self]

    @property
    def next(self) -> InteractionMode:
        modes = tuple(InteractionMode)
        return modes[(modes.index(self) + 1) % len(modes)]


def interaction_mode_guidance(mode: InteractionMode) -> str:
    """Return model guidance without granting permissions by prompt text.

    返回模型指引,但不通过提示文本授予权限."""

    guidance = {
        InteractionMode.NORMAL: (
            "Operating mode: Normal. Read-only exploration is automatic. Request tools "
            "normally; side effects still require the permission policy's approval."
        ),
        InteractionMode.ACCEPT_EDITS: (
            "Operating mode: Accept Edits. Workspace edits may be approved automatically, "
            "but shell commands, network access, and other effects remain permission-gated."
        ),
        InteractionMode.PLAN: (
            "Operating mode: Plan. Explore with read-only tools and produce a concrete plan. "
            "Use update_plan to save a concise structured plan before presenting it. Do not "
            "request edits, shell commands, network access, or other side effects."
        ),
        InteractionMode.AUTO: (
            "Operating mode: Auto. Continue autonomously through safe work, but obey every "
            "workspace, sandbox, explicit deny, and permission boundary."
        ),
    }
    return guidance[mode]


__all__ = ["InteractionMode", "interaction_mode_guidance"]
