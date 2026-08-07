"""Stable application error hierarchy.

Exceptions may contain operational context but must never contain credentials.
The CLI maps these errors to deterministic exit codes.

稳定的应用错误层次结构.
"""


class NeuroCodeError(Exception):
    """Base class for expected application failures.

    预期应用失败的基类."""


class ConfigurationError(NeuroCodeError):
    """Configuration is missing, invalid, or contradictory.

    配置缺失,无效或相互矛盾."""


class ProviderError(NeuroCodeError):
    """A model provider failed or returned an invalid stream.

    模型 Provider 失败,或返回了无效的流."""


class ToolError(NeuroCodeError):
    """A tool request is invalid or could not be completed.

    工具请求无效,或无法完成."""


class BackgroundTaskCapacityError(ToolError):
    """A managed task supervisor cannot accept another task right now.

    表示受管理任务监督器当前无法接受更多任务."""


class PermissionDenied(ToolError):
    """A tool call was rejected by the permission policy.

    工具调用被权限策略拒绝."""


class SandboxError(NeuroCodeError):
    """A requested operating-system sandbox could not be enforced.

    请求的操作系统沙箱无法强制启用."""


class TerminalError(NeuroCodeError):
    """An interactive terminal request or owned session failed.

    交互式终端请求或所属会话失败."""


class SessionError(NeuroCodeError):
    """Session persistence or reconstruction failed.

    会话持久化或重建失败."""


__all__ = [
    "BackgroundTaskCapacityError",
    "ConfigurationError",
    "NeuroCodeError",
    "PermissionDenied",
    "ProviderError",
    "SandboxError",
    "SessionError",
    "TerminalError",
    "ToolError",
]
