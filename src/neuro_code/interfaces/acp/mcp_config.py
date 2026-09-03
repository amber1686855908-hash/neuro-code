"""Bounded ACP MCP configuration conversion.

ACP MCP 配置的有界转换.

This module owns only the stateless conversion of ACP MCP server declarations
into application MCP configuration contracts. It validates input, preserves
the ACP transport discriminator, and performs no filesystem, subprocess,
network, environment, database, provider, or MCP runtime work.
本模块只负责将 ACP MCP server declaration 无状态地转换为应用 MCP 配置契约.
它校验输入、保留 ACP transport discriminator,不执行文件系统、子进程、网络、环境、
数据库、provider 或 MCP runtime 操作.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from acp.schema import AcpMcpServer, HttpMcpServer, McpServerStdio, SseMcpServer

from neuro_code.application.acp.contracts import (
    MAX_MCP_SERVERS,
    AcpMcpHttpServerConfig,
    AcpMcpServerConfig,
    AcpMcpStdioServerConfig,
)
from neuro_code.interfaces.acp.errors import invalid_params as _invalid_params
from neuro_code.interfaces.acp.serialization import serialized_size_bytes

MAX_MCP_SERVER_NAME_BYTES = 128
MAX_MCP_COMMAND_BYTES = 4 * 1024
MAX_MCP_ARGUMENTS = 64
MAX_MCP_ARGUMENT_BYTES = 4 * 1024
MAX_MCP_ARGUMENT_TOTAL_BYTES = 32 * 1024
MAX_MCP_ENVIRONMENT_VARIABLES = 64
MAX_MCP_ENVIRONMENT_NAME_BYTES = 256
MAX_MCP_ENVIRONMENT_VALUE_BYTES = 16 * 1024
MAX_MCP_ENVIRONMENT_TOTAL_BYTES = 64 * 1024
MAX_MCP_URL_BYTES = 8 * 1024
MAX_MCP_HTTP_HEADERS = 64
MAX_MCP_HTTP_HEADER_NAME_BYTES = 256
MAX_MCP_HTTP_HEADER_VALUE_BYTES = 16 * 1024
MAX_MCP_HTTP_HEADER_TOTAL_BYTES = 64 * 1024
MAX_MCP_CONFIGURATION_BYTES = 256 * 1024

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED_MCP_HTTP_HEADERS = frozenset(
    {
        "accept",
        "connection",
        "content-length",
        "content-type",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

McpServer = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio


def _mcp_string(
    value: object,
    *,
    limit: int,
    reason: str,
    allow_empty: bool = False,
    allow_controls: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise _invalid_params(reason)
    if (
        "\x00" in value
        or (
            not allow_controls
            and any(ord(character) < 32 or ord(character) == 127 for character in value)
        )
        or len(value.encode("utf-8")) > limit
    ):
        raise _invalid_params(reason)
    return value


def _mcp_server_configurations(
    servers: list[McpServer] | None,
    *,
    protected_environment_variables: frozenset[str],
) -> tuple[AcpMcpServerConfig, ...]:
    if not servers:
        return ()
    if len(servers) > MAX_MCP_SERVERS:
        raise _invalid_params("too_many_mcp_servers")

    protected = {name.casefold() for name in protected_environment_variables}
    configurations: list[AcpMcpServerConfig] = []
    server_names: set[str] = set()
    serialized: list[dict[str, object]] = []
    for server in servers:
        if not isinstance(server, HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio):
            raise _invalid_params("mcp_transport_unsupported")
        name = _mcp_string(
            server.name,
            limit=MAX_MCP_SERVER_NAME_BYTES,
            reason="mcp_server_name_invalid",
        )
        folded_name = name.casefold()
        if folded_name in server_names:
            raise _invalid_params("mcp_server_name_duplicate")
        server_names.add(folded_name)
        if isinstance(server, HttpMcpServer | SseMcpServer):
            url = _mcp_http_url(server.url)
            headers = _mcp_http_headers(server.headers)
            serialized.append(
                {
                    "name": name,
                    "transport": server.type,
                    "url": url,
                    "headers": dict(headers),
                }
            )
            configurations.append(
                AcpMcpHttpServerConfig(
                    name=name,
                    url=url,
                    headers=tuple(headers),
                    transport=server.type,
                )
            )
            continue
        if not isinstance(server, McpServerStdio):
            raise _invalid_params("mcp_transport_unsupported")
        command = _mcp_string(
            server.command,
            limit=MAX_MCP_COMMAND_BYTES,
            reason="mcp_server_command_invalid",
        )
        if len(server.args) > MAX_MCP_ARGUMENTS:
            raise _invalid_params("too_many_mcp_server_arguments")
        arguments: list[str] = []
        argument_bytes = 0
        for argument in server.args:
            rendered = _mcp_string(
                argument,
                limit=MAX_MCP_ARGUMENT_BYTES,
                reason="mcp_server_argument_invalid",
                allow_empty=True,
            )
            argument_bytes += len(rendered.encode("utf-8"))
            if argument_bytes > MAX_MCP_ARGUMENT_TOTAL_BYTES:
                raise _invalid_params("mcp_server_arguments_too_large")
            arguments.append(rendered)

        if len(server.env) > MAX_MCP_ENVIRONMENT_VARIABLES:
            raise _invalid_params("too_many_mcp_environment_variables")
        environment: list[tuple[str, str]] = []
        environment_names: set[str] = set()
        environment_bytes = 0
        for variable in server.env:
            variable_name = _mcp_string(
                variable.name,
                limit=MAX_MCP_ENVIRONMENT_NAME_BYTES,
                reason="mcp_environment_name_invalid",
            )
            folded_variable_name = variable_name.casefold()
            if (
                not _ENVIRONMENT_NAME.fullmatch(variable_name)
                or folded_variable_name in environment_names
            ):
                raise _invalid_params("mcp_environment_name_invalid")
            if folded_variable_name in protected:
                raise _invalid_params("mcp_environment_protected")
            environment_names.add(folded_variable_name)
            variable_value = _mcp_string(
                variable.value,
                limit=MAX_MCP_ENVIRONMENT_VALUE_BYTES,
                reason="mcp_environment_value_invalid",
                allow_empty=True,
                allow_controls=True,
            )
            environment_bytes += len(variable_name.encode("utf-8")) + len(
                variable_value.encode("utf-8")
            )
            if environment_bytes > MAX_MCP_ENVIRONMENT_TOTAL_BYTES:
                raise _invalid_params("mcp_environment_too_large")
            environment.append((variable_name, variable_value))
        serialized.append(
            {
                "name": name,
                "command": command,
                "args": arguments,
                "env": dict(environment),
            }
        )
        configurations.append(
            AcpMcpStdioServerConfig(
                name=name,
                command=command,
                args=tuple(arguments),
                env=tuple(environment),
            )
        )
    if serialized_size_bytes(serialized) > MAX_MCP_CONFIGURATION_BYTES:
        raise _invalid_params("mcp_configuration_too_large")
    return tuple(configurations)


def _mcp_http_url(value: object) -> str:
    url = _mcp_string(
        value,
        limit=MAX_MCP_URL_BYTES,
        reason="mcp_http_url_invalid",
    )
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise _invalid_params("mcp_http_url_invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 0 < port <= 65_535)
    ):
        raise _invalid_params("mcp_http_url_invalid")
    return url


def _mcp_http_headers(headers: Sequence[Any]) -> list[tuple[str, str]]:
    if len(headers) > MAX_MCP_HTTP_HEADERS:
        raise _invalid_params("too_many_mcp_http_headers")
    values: list[tuple[str, str]] = []
    names: set[str] = set()
    total_bytes = 0
    for header in headers:
        name = _mcp_string(
            header.name,
            limit=MAX_MCP_HTTP_HEADER_NAME_BYTES,
            reason="mcp_http_header_name_invalid",
        )
        folded_name = name.casefold()
        if not _HTTP_HEADER_NAME.fullmatch(name) or folded_name in names:
            raise _invalid_params("mcp_http_header_name_invalid")
        if folded_name in _RESERVED_MCP_HTTP_HEADERS:
            raise _invalid_params("mcp_http_header_reserved")
        value = _mcp_string(
            header.value,
            limit=MAX_MCP_HTTP_HEADER_VALUE_BYTES,
            reason="mcp_http_header_value_invalid",
            allow_empty=True,
        )
        total_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total_bytes > MAX_MCP_HTTP_HEADER_TOTAL_BYTES:
            raise _invalid_params("mcp_http_headers_too_large")
        names.add(folded_name)
        values.append((name, value))
    return values
