#!/usr/bin/env python3
"""Exercise the packaged MCP command through real stdio and filesystem I/O."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading
import zlib


PROTOCOL_VERSION = "2025-11-25"
EXPECTED_ARTIFACTS = 44


def png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


def write_opaque_png(path: Path, edge: int = 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = bytes((37, 109, 219, 255)) * edge
    pixels = b"".join(b"\x00" + row for _ in range(edge))
    image = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", edge, edge, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(pixels, level=6))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(image)


class McpProcess:
    def __init__(self, plugin_root: Path) -> None:
        config = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["app-icon-toolkit"]
        command = [server["command"], *server.get("args", [])]
        self._process = subprocess.Popen(
            command,
            cwd=plugin_root / server.get("cwd", "."),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("MCP process did not expose stdio")
        self._messages: queue.Queue[dict[str, object] | BaseException | None] = queue.Queue()
        self._seen: list[dict[str, object]] = []
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        if self._process.stdout is None:
            self._messages.put(RuntimeError("MCP stdout is unavailable"))
            self._messages.put(None)
            return
        try:
            for raw_line in self._process.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise RuntimeError(f"stdout contained a non-JSON-RPC message: {line}")
                self._seen.append(message)
                self._messages.put(message)
        except BaseException as error:
            self._messages.put(error)
        finally:
            self._messages.put(None)

    def send(self, message: dict[str, object]) -> None:
        if self._process.stdin is None:
            raise RuntimeError("MCP stdin is closed")
        self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._process.stdin.flush()

    def response(self, request_id: int, timeout_seconds: float = 120.0) -> dict[str, object]:
        while True:
            item = self._messages.get(timeout=timeout_seconds)
            if item is None:
                raise RuntimeError(f"MCP stdout closed before response {request_id}")
            if isinstance(item, BaseException):
                raise item
            if item.get("id") == request_id:
                return item

    def close(self) -> None:
        if self._process.stdin is None:
            raise RuntimeError("MCP stdin is unavailable during shutdown")
        self._process.stdin.close()
        return_code = self._process.wait(timeout=10)
        self._reader.join(timeout=10)
        if self._reader.is_alive():
            raise RuntimeError("MCP stdout reader did not stop")
        stderr = ""
        if self._process.stderr is not None:
            stderr = self._process.stderr.read()
        if return_code != 0:
            raise RuntimeError(f"MCP process exited with {return_code}: {stderr}")
        if "\x1b" in stderr:
            raise RuntimeError("MCP stderr contained ANSI escape sequences")
        if not self._seen:
            raise RuntimeError("MCP process produced no protocol messages")


def structured_content(response: dict[str, object]) -> dict[str, object]:
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"tool response omitted result: {response}")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise RuntimeError(f"tool response omitted structuredContent: {response}")
    return structured


def call_arguments(workspace: Path) -> dict[str, object]:
    return {
        "workspace_root": str(workspace.resolve()),
        "output_directory": "generated",
        "sources": {
            "flattened": "sources/flattened.png",
            "adaptive": {
                "foreground": "sources/foreground.png",
                "background": "sources/background.png",
                "monochrome": "sources/monochrome.png",
            },
        },
        "targets": [
            {"profile": "mac_os_app_icon_set", "icon_set_name": "Assets"},
            {"profile": "android_adaptive", "resource_name": "ic_launcher"},
            {"profile": "windows_ico", "file_stem": "app-icon"},
            {
                "profile": "linux_xdg",
                "application_id": "com.example.IconProbe",
                "display_name": "Icon Probe",
                "executable": "icon-probe",
            },
        ],
    }


def run_smoke(plugin_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="app-icon-toolkit-smoke-") as temporary:
        workspace = Path(temporary)
        for name in ("flattened", "foreground", "background", "monochrome"):
            write_opaque_png(workspace / "sources" / f"{name}.png")

        mcp = McpProcess(plugin_root)
        try:
            mcp.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "release-smoke", "version": "1.0.0"},
                    },
                }
            )
            initialized = mcp.response(1)
            result = initialized.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("initialize omitted result")
            if result.get("protocolVersion") != PROTOCOL_VERSION:
                raise RuntimeError(f"unexpected protocol version: {result}")

            mcp.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
            arguments = call_arguments(workspace)
            mcp.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "plan_icon_set", "arguments": arguments},
                }
            )
            plan = structured_content(mcp.response(2))
            profiles = plan.get("profiles")
            if not isinstance(profiles, list) or len(profiles) != 4:
                raise RuntimeError(f"plan returned an unexpected profile set: {plan}")

            mcp.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "generate_icon_set", "arguments": arguments},
                }
            )
            generated = structured_content(mcp.response(3))
            artifacts = generated.get("artifacts")
            if not isinstance(artifacts, list) or len(artifacts) != EXPECTED_ARTIFACTS:
                raise RuntimeError(f"generate returned an unexpected receipt: {generated}")
            actual_files = [path for path in (workspace / "generated").rglob("*") if path.is_file()]
            if len(actual_files) != EXPECTED_ARTIFACTS:
                raise RuntimeError(f"generated {len(actual_files)} files, expected {EXPECTED_ARTIFACTS}")

            mcp.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "generate_icon_set", "arguments": arguments},
                }
            )
            duplicate = mcp.response(4)
            duplicate_result = duplicate.get("result")
            if not isinstance(duplicate_result, dict) or duplicate_result.get("isError") is not True:
                raise RuntimeError(f"duplicate generation did not fail: {duplicate}")
            failure = structured_content(duplicate)
            if failure.get("code") != "OUTPUT_EXISTS":
                raise RuntimeError(f"duplicate generation returned the wrong error: {failure}")
        finally:
            mcp.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_root", type=Path)
    arguments = parser.parse_args()
    plugin_root = arguments.plugin_root.resolve(strict=True)
    run_smoke(plugin_root)


if __name__ == "__main__":
    main()
