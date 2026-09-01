from __future__ import annotations

import unittest
from unittest.mock import patch

from acp.exceptions import RequestError
from acp.schema import (
    AcpMcpServer,
    EnvVariable,
    HttpMcpServer,
    McpServerStdio,
    SseMcpServer,
)

import neuro_code.interfaces.acp.mcp_config as mcp_config
from neuro_code.application.acp.contracts import (
    AcpMcpHttpServerConfig,
    AcpMcpStdioServerConfig,
)


class AcpMcpConfigurationBoundaryTests(unittest.TestCase):
    @staticmethod
    def _stdio(
        *,
        name: str = "fixture",
        command: str = "fixture-command",
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
    ) -> McpServerStdio:
        return McpServerStdio(
            name=name,
            command=command,
            args=[] if args is None else args,
            env=[] if env is None else env,
        )

    @staticmethod
    def _http(
        *,
        name: str = "http",
        url: str = "https://mcp.example.test/mcp",
        headers: list[dict[str, str]] | None = None,
    ) -> HttpMcpServer:
        return HttpMcpServer.model_validate(
            {
                "name": name,
                "type": "http",
                "url": url,
                "headers": [] if headers is None else headers,
            }
        )

    @staticmethod
    def _reason(
        servers: list[object],
        *,
        protected: frozenset[str] = frozenset(),
    ) -> str:
        with unittest.TestCase().assertRaises(RequestError) as error:
            mcp_config._mcp_server_configurations(  # type: ignore[arg-type]
                servers,
                protected_environment_variables=protected,
            )
        return str(error.exception.data["reason"])

    def test_valid_stdio_preserves_empty_arguments_controls_and_output_contract(self) -> None:
        configurations = mcp_config._mcp_server_configurations(
            [
                self._stdio(
                    args=["", "--stdio"],
                    env=[EnvVariable(name="FIXTURE", value="line\nvalue")],
                )
            ],
            protected_environment_variables=frozenset(),
        )

        self.assertEqual(
            configurations,
            (
                AcpMcpStdioServerConfig(
                    name="fixture",
                    command="fixture-command",
                    args=("", "--stdio"),
                    env=(("FIXTURE", "line\nvalue"),),
                ),
            ),
        )

    def test_valid_http_and_sse_preserve_wire_values_and_transport(self) -> None:
        configurations = mcp_config._mcp_server_configurations(
            [
                HttpMcpServer(
                    name="http",
                    type="http",
                    url="https://mcp.example.test/v1?tenant=one",
                    headers=[],
                ),
                SseMcpServer(
                    name="sse",
                    type="sse",
                    url="http://127.0.0.1:8123/events",
                    headers=[],
                ),
            ],
            protected_environment_variables=frozenset(),
        )

        self.assertIsInstance(configurations[0], AcpMcpHttpServerConfig)
        self.assertEqual(configurations[0].url, "https://mcp.example.test/v1?tenant=one")
        self.assertEqual(configurations[0].transport, "http")
        self.assertEqual(configurations[1].transport, "sse")

    def test_stdio_security_and_transport_errors_keep_existing_reasons(self) -> None:
        cases = (
            ([self._stdio(name="same"), self._stdio(name="SAME")], "mcp_server_name_duplicate"),
            (
                [self._stdio(env=[EnvVariable(name="SECRET", value="x")])],
                "mcp_environment_protected",
            ),
            ([self._stdio(command="bad\ncommand")], "mcp_server_command_invalid"),
            (
                [self._stdio(command="x" * (mcp_config.MAX_MCP_COMMAND_BYTES + 1))],
                "mcp_server_command_invalid",
            ),
            (
                [self._stdio(env=[EnvVariable(name="BAD-NAME", value="x")])],
                "mcp_environment_name_invalid",
            ),
        )
        for servers, reason in cases:
            with self.subTest(reason=reason):
                protected = (
                    frozenset({"secret"}) if reason == "mcp_environment_protected" else frozenset()
                )
                self.assertEqual(self._reason(servers, protected=protected), reason)

    def test_stdio_argument_and_environment_bounds_keep_existing_reasons(self) -> None:
        cases = (
            (
                [self._stdio(args=["x"] * (mcp_config.MAX_MCP_ARGUMENTS + 1))],
                "too_many_mcp_server_arguments",
            ),
            (
                [self._stdio(args=["x" * (mcp_config.MAX_MCP_ARGUMENT_BYTES + 1)])],
                "mcp_server_argument_invalid",
            ),
            (
                [
                    self._stdio(
                        env=[
                            EnvVariable(
                                name="VALUE",
                                value="x" * (mcp_config.MAX_MCP_ENVIRONMENT_VALUE_BYTES + 1),
                            )
                        ]
                    )
                ],
                "mcp_environment_value_invalid",
            ),
        )
        for servers, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self._reason(servers), reason)

        with patch.object(mcp_config, "MAX_MCP_ENVIRONMENT_TOTAL_BYTES", 1):
            self.assertEqual(
                self._reason([self._stdio(env=[EnvVariable(name="VALUE", value="x")])]),
                "mcp_environment_too_large",
            )

    def test_http_security_and_unsupported_transport_errors_keep_existing_reasons(self) -> None:
        invalid_url = self._http(url="ftp://mcp.example.test")
        invalid_port = self._http(url="https://mcp.example.test:65536")
        reserved_header = self._http(headers=[{"name": "Host", "value": "override"}])

        self.assertEqual(self._reason([invalid_url]), "mcp_http_url_invalid")
        self.assertEqual(self._reason([invalid_port]), "mcp_http_url_invalid")
        self.assertEqual(self._reason([reserved_header]), "mcp_http_header_reserved")
        self.assertEqual(
            self._reason(
                [
                    AcpMcpServer(
                        name="acp",
                        id="server-id",
                        type="acp",
                    )
                ]
            ),
            "mcp_transport_unsupported",
        )

    def test_http_header_count_name_duplicate_value_and_total_bounds_keep_existing_reasons(
        self,
    ) -> None:
        too_many = self._http(
            headers=[
                {"name": f"X-Fixture-{index}", "value": "x"}
                for index in range(mcp_config.MAX_MCP_HTTP_HEADERS + 1)
            ]
        )
        invalid_name = self._http(headers=[{"name": "bad name", "value": "x"}])
        duplicate_names = self._http(
            headers=[
                {"name": "X-Fixture", "value": "one"},
                {"name": "x-fixture", "value": "two"},
            ]
        )
        oversized_value = self._http(
            headers=[
                {
                    "name": "X-Fixture",
                    "value": "x" * (mcp_config.MAX_MCP_HTTP_HEADER_VALUE_BYTES + 1),
                }
            ]
        )

        cases = (
            (too_many, "too_many_mcp_http_headers"),
            (invalid_name, "mcp_http_header_name_invalid"),
            (duplicate_names, "mcp_http_header_name_invalid"),
            (oversized_value, "mcp_http_header_value_invalid"),
        )
        for server, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self._reason([server]), reason)

        with patch.object(mcp_config, "MAX_MCP_HTTP_HEADER_TOTAL_BYTES", 1):
            self.assertEqual(
                self._reason([self._http(headers=[{"name": "X-Fixture", "value": "x"}])]),
                "mcp_http_headers_too_large",
            )

    def test_configuration_size_uses_canonical_serialized_bound(self) -> None:
        with patch.object(mcp_config, "MAX_MCP_CONFIGURATION_BYTES", 1):
            self.assertEqual(self._reason([self._stdio()]), "mcp_configuration_too_large")
