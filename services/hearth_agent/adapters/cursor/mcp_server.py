"""Stdio MCP server for one chat turn's caller-run tools; ``cursor-agent`` starts it from ``.cursor/mcp.json``.

    python mcp_server.py <spec.json>

The spec (written by ``mcp_tools.ClientToolRelay`` into the run's private folder) lists the tools and the
loopback port and token of the relay inside Hearth-Core. ``tools/list`` serves the tools; each ``tools/call``
is forwarded to the relay, which shows it to the web app and waits for the result.

Speaks the MCP stdio transport directly — newline-delimited JSON-RPC 2.0: ``initialize``, ``ping``,
``tools/list``, ``tools/call`` — using the standard library only, so it neither depends on an MCP SDK
version nor imports Hearth-Core. Only protocol messages are written to stdout.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from typing import Any, BinaryIO

DEFAULT_PROTOCOL = "2025-06-18"


class Server:
    def __init__(self, spec: dict[str, Any], out: BinaryIO):
        self.spec = spec
        self.out = out
        self.lock = threading.Lock()
        self.calls: list[threading.Thread] = []

    def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        with self.lock:
            self.out.write(data)
            self.out.flush()

    def reply(self, msg_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def fail(self, msg_id: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    def relay(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        request = {"token": self.spec["token"], "name": name, "arguments": arguments}
        try:
            with socket.create_connection((self.spec["host"], self.spec["port"]), timeout=10) as conn:
                conn.settimeout(float(self.spec.get("timeout") or 150))
                conn.sendall(json.dumps(request).encode() + b"\n")
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            answer = json.loads(data or b"{}")
            return str(answer.get("output", "")), bool(answer.get("is_error"))
        except (OSError, ValueError) as exc:
            return f"Hearth-Core did not answer the tool call: {exc}", True

    def call_tool(self, msg_id: Any, params: dict[str, Any]) -> None:
        arguments = params.get("arguments")
        output, is_error = self.relay(str(params.get("name", "")), arguments if isinstance(arguments, dict) else {})
        self.reply(msg_id, {"content": [{"type": "text", "text": output}], "isError": is_error})

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if msg_id is None:  # notifications (initialized, cancelled, ...) need no answer
            return
        if method == "initialize":
            self.reply(msg_id, {
                "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": f"hearth-{self.spec.get('name', 'client')}", "version": "1"},
            })
        elif method == "ping":
            self.reply(msg_id, {})
        elif method == "tools/list":
            self.reply(msg_id, {"tools": self.spec.get("tools") or []})
        elif method == "tools/call":
            # A web-app tool can take minutes; answer pings and other calls meanwhile.
            thread = threading.Thread(target=self.call_tool, args=(msg_id, params), daemon=True)
            self.calls.append(thread)
            thread.start()
        else:
            self.fail(msg_id, -32601, f"method not found: {method}")


def main(argv: list[str], stdin: BinaryIO, stdout: BinaryIO) -> int:
    if len(argv) != 2:
        print("usage: mcp_server.py <spec.json>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    server = Server(spec, stdout)
    for raw in stdin:
        try:
            message = json.loads(raw)
        except ValueError:
            server.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(message, dict):
            server.handle(message)
    for thread in server.calls:  # stdin closed: answer the calls still running, then exit
        thread.join()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin.buffer, sys.stdout.buffer))
