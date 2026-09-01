from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def _response(request_id: object, result: dict[str, Any]) -> None:
    payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _tool(name: str, description: str, properties: dict[str, object]) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        },
    }


mode = sys.argv[1] if len(sys.argv) > 1 else "normal"

for raw_line in sys.stdin:
    request = json.loads(raw_line)
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        continue
    if method == "initialize" and mode != "normal":
        if mode == "hang":
            time.sleep(60)
        elif mode == "missing-newline":
            sys.stdout.write('{"jsonrpc":"2.0","id":0,"result":{}}')
            sys.stdout.flush()
        elif mode == "oversized":
            sys.stdout.write("{" + ("x" * (1024 * 1024 + 1)))
            sys.stdout.flush()
        else:
            sys.stdout.write("not-json\n")
            sys.stdout.flush()
        raise SystemExit(0)
    if method == "initialize":
        _response(
            request_id,
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "neuro-code-test-mcp", "version": "1"},
            },
        )
    elif method == "tools/list":
        _response(
            request_id,
            {
                "tools": [
                    _tool(
                        "echo",
                        "Return text from the test MCP server.",
                        {"text": {"type": "string"}},
                    ),
                    _tool(
                        "configured_secret",
                        "Return the configured fixture value.",
                        {},
                    ),
                    _tool("wait_forever", "Wait until the client cancels.", {}),
                    _tool("rich_result", "Return every supported result shape.", {}),
                    _tool("empty_error", "Return an empty error result.", {}),
                    _tool("disconnect", "Exit before returning a result.", {}),
                ]
            },
        )
    elif method == "resources/list":
        _response(
            request_id,
            {
                "resources": [
                    {
                        "uri": "fixture://resource",
                        "name": "fixture-resource",
                        "description": "Fixture resource",
                        "mimeType": "text/plain",
                    }
                ]
            },
        )
    elif method == "resources/templates/list":
        _response(
            request_id,
            {
                "resourceTemplates": [
                    {
                        "uriTemplate": "fixture://resource/{name}",
                        "name": "fixture-template",
                        "mimeType": "text/plain",
                    }
                ]
            },
        )
    elif method == "prompts/list":
        _response(
            request_id,
            {
                "prompts": [
                    {
                        "name": "fixture-prompt",
                        "description": "Fixture prompt",
                        "arguments": [{"name": "topic", "required": True}],
                    }
                ]
            },
        )
    elif method == "resources/read":
        _response(
            request_id,
            {
                "contents": [
                    {
                        "uri": "fixture://resource",
                        "mimeType": "text/plain",
                        "text": "fixture resource text",
                    },
                    {
                        "uri": "fixture://blob",
                        "mimeType": "application/octet-stream",
                        "blob": "AQI=",
                    },
                ]
            },
        )
    elif method == "prompts/get":
        _response(
            request_id,
            {
                "description": "Fixture prompt",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": "fixture prompt text"},
                    }
                ],
            },
        )
    elif method == "tools/call":
        params = request["params"]
        name = params["name"]
        if name == "wait_forever":
            time.sleep(60)
            text = "unreachable"
        elif name == "disconnect":
            raise SystemExit(0)
        elif name == "rich_result":
            _response(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": "visible\u0001 text"},
                        {
                            "type": "resource_link",
                            "uri": "https://example.invalid/reference",
                            "name": "reference",
                            "title": "fixture-secret-value",
                            "description": "safe",
                            "mimeType": "text/plain",
                            "size": 12,
                            "annotations": {
                                "audience": ["assistant"],
                                "priority": 0.5,
                            },
                            "_meta": {"hidden": "must-not-appear"},
                        },
                        {
                            "type": "image",
                            "data": "ignored",
                            "mimeType": "image/png",
                        },
                        {
                            "type": "audio",
                            "data": "ignored",
                            "mimeType": "audio/wav",
                        },
                        {
                            "type": "resource",
                            "resource": {
                                "uri": "file:///ignored",
                                "text": "embedded-secret",
                            },
                        },
                    ],
                    "structuredContent": {
                        "value": "fixture-secret-value",
                        "_meta": {"hidden": True},
                    },
                    "_meta": {"hidden": "must-not-appear"},
                    "isError": False,
                },
            )
            continue
        elif name == "empty_error":
            _response(request_id, {"content": [], "isError": True})
            continue
        elif name == "configured_secret":
            text = os.environ.get("MCP_FIXTURE_SECRET", "missing")
        else:
            text = params.get("arguments", {}).get("text", "")
        _response(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            },
        )
