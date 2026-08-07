"""Compatibility facade for the canonical stdio MCP infrastructure.

提供标准输入输出 MCP 基础设施的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.mcp.stdio import (
    MAX_MCP_SERVERS,
    McpStdioError,
    McpStdioServerConfig,
    McpStdioTool,
    McpStdioToolCollection,
)

__all__ = [
    "MAX_MCP_SERVERS",
    "McpStdioError",
    "McpStdioServerConfig",
    "McpStdioTool",
    "McpStdioToolCollection",
]
