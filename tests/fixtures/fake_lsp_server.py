"""Small real stdio LSP conformance child used by the Neuro Code tests.

It intentionally runs as a separate process so framing, lifecycle, server
requests, and stderr backpressure are exercised through the production process
port instead of a Python mock.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def send(message: dict[str, object]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(
        b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload
    )
    sys.stdout.buffer.flush()


def read_message() -> dict[str, object] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.casefold()] = value.strip()
    length = int(headers["content-length"])
    body = sys.stdin.buffer.read(length)
    if len(body) != length:
        return None
    return json.loads(body.decode("utf-8"))


def location(uri: str) -> dict[str, object]:
    return {
        "uri": uri,
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 4},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal")
    args = parser.parse_args()
    mode = args.mode
    current_uri = ""
    initialize_id: int | str | None = None
    while True:
        message = read_message()
        if message is None:
            return 0
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            initialize_id = request_id if isinstance(request_id, (int, str)) else 1
            params = message.get("params")
            if isinstance(params, dict):
                value = params.get("rootUri")
                if isinstance(value, str):
                    current_uri = value
            if mode == "stderr-spam":
                sys.stderr.write("stderr-noise " * 20_000)
                sys.stderr.flush()
            if mode in {"server-request", "server-request-all"}:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 700,
                        "method": "workspace/configuration",
                        "params": {"items": [{"section": "neuro"}]},
                    }
                )
                if mode == "server-request-all":
                    for request_method in (
                        "client/registerCapability",
                        "client/unregisterCapability",
                        "window/showMessageRequest",
                        "workspace/workspaceFolders",
                        "unknown/clientRequest",
                    ):
                        send(
                            {
                                "jsonrpc": "2.0",
                                "id": 710 + len(request_method),
                                "method": request_method,
                                "params": {},
                            }
                        )
            if mode == "apply-edit":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 701,
                        "method": "workspace/applyEdit",
                        "params": {
                            "edit": {
                                "changes": {
                                    current_uri: [
                                        {
                                            "range": {
                                                "start": {"line": 0, "character": 0},
                                                "end": {"line": 0, "character": 0},
                                            },
                                            "newText": "MUST NOT APPLY",
                                        }
                                    ]
                                }
                            }
                        },
                    }
                )
            capabilities: dict[str, object] = {
                "positionEncoding": "utf-16",
                "definitionProvider": True,
                "referencesProvider": True,
                "hoverProvider": True,
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
                "diagnosticProvider": False if mode == "no-publish" else {},
            }
            if mode == "minimal":
                capabilities = {"positionEncoding": "utf-16"}
            send(
                {
                    "jsonrpc": "2.0",
                    "id": initialize_id,
                    "result": {
                        "capabilities": capabilities,
                    },
                }
            )
            continue
        if method == "initialized":
            continue
        if method == "textDocument/didOpen" or method == "textDocument/didChange":
            params = message.get("params")
            if isinstance(params, dict):
                document = params.get("textDocument")
                if isinstance(document, dict) and isinstance(document.get("uri"), str):
                    current_uri = document["uri"]
            if mode != "no-publish":
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/publishDiagnostics",
                        "params": {
                            "uri": current_uri,
                            "diagnostics": [
                                {
                                    "range": {
                                        "start": {"line": 0, "character": 0},
                                        "end": {"line": 0, "character": 4},
                                    },
                                    "severity": 1,
                                    "message": "fake diagnostic",
                                    "source": "fake-lsp",
                                }
                            ],
                        },
                    }
                )
            continue
        if method == "$/cancelRequest":
            continue
        if method == "shutdown":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            continue
        if method == "exit":
            return 0
        if request_id is None:
            continue
        if mode == "crash":
            os._exit(7)
        if mode == "timeout":
            time.sleep(5)
            continue
        if mode == "malformed-json":
            sys.stdout.buffer.write(b"Content-Length: 5\r\n\r\nnope!")
            sys.stdout.buffer.flush()
            return 0
        if mode == "malformed-header":
            sys.stdout.buffer.write(b"Content-Length nope\r\n\r\n{}")
            sys.stdout.buffer.flush()
            return 0
        if mode == "oversized":
            sys.stdout.buffer.write(b"Content-Length: 1048577\r\n\r\n")
            sys.stdout.buffer.flush()
            return 0
        if mode == "duplicate-late":
            send({"jsonrpc": "2.0", "id": request_id, "result": []})
            send({"jsonrpc": "2.0", "id": request_id, "result": [{"late": True}]})
            continue
        params = message.get("params")
        if isinstance(params, dict):
            document = params.get("textDocument")
            if isinstance(document, dict) and isinstance(document.get("uri"), str):
                current_uri = document["uri"]
        if method == "textDocument/definition":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": [
                        location(current_uri),
                        location("file:///outside-neuro-code.txt"),
                    ],
                }
            )
        elif method == "textDocument/references":
            send({"jsonrpc": "2.0", "id": request_id, "result": [location(current_uri)]})
        elif method == "textDocument/hover":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "contents": {
                            "kind": "markdown",
                            "value": "<script>bad</script>fake hover",
                        },
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 4},
                        },
                    },
                }
            )
        elif method == "textDocument/documentSymbol":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": [
                        {
                            "name": "fakeSymbol",
                            "kind": 12,
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 4},
                            },
                        }
                    ],
                }
            )
        elif method == "workspace/symbol":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": [
                        {
                            "name": "fakeSymbol",
                            "kind": 12,
                            "location": location(current_uri),
                        }
                    ],
                }
            )
        elif method == "textDocument/diagnostic":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "kind": "full",
                        "items": [
                            {
                                "range": {
                                    "start": {"line": 0, "character": 0},
                                    "end": {"line": 0, "character": 4},
                                },
                                "severity": 2,
                                "message": "fake pull diagnostic",
                            }
                        ],
                    },
                }
            )
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
