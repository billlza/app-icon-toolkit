#!/usr/bin/env python3
"""Exercise the packaged MCP command through real stdio and filesystem I/O."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import tempfile
import threading
import zlib


PROTOCOL_VERSION = "2025-11-25"
EXPECTED_ARTIFACTS = 44
STDERR_CAPTURE_LIMIT = 64 * 1024
DIAGNOSTIC_OVERLAP = 64
DISALLOWED_DIAGNOSTIC = re.compile(
    r"\b(?:WARN(?:ING)?|ERROR)(?=[:\s])|\b[A-Za-z]+Warning:|\[DEP[0-9]+\]",
    flags=re.IGNORECASE,
)


class McpProcessFailure(RuntimeError):
    def __init__(self, issues: list[tuple[str, Exception]]) -> None:
        self.issues = tuple(issues)
        details = "; ".join(
            f"{stage}: {type(error).__name__}: {str(error)!r}"
            for stage, error in self.issues
        )
        super().__init__(f"MCP process validation failed: {details}")


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} must be a non-empty string")
    return value


def _require_inside_plugin(path: Path, plugin_root: Path, field: str) -> Path:
    try:
        path.relative_to(plugin_root)
    except ValueError as error:
        raise RuntimeError(f"{field} escapes the packaged plugin root: {path}") from error
    return path


def resolve_packaged_server_process(
    plugin_root: Path, server: dict[str, object]
) -> tuple[list[str], Path]:
    """Resolve the bundled command exactly as a plugin-root-relative process."""

    plugin_root = plugin_root.resolve(strict=True)
    cwd_value = _require_string(server.get("cwd", "."), "MCP server cwd")
    configured_cwd = Path(cwd_value)
    if configured_cwd.is_absolute():
        raise RuntimeError("packaged MCP server cwd must be relative to the plugin root")
    working_directory = _require_inside_plugin(
        (plugin_root / configured_cwd).resolve(strict=True),
        plugin_root,
        "MCP server cwd",
    )
    if not working_directory.is_dir():
        raise RuntimeError(f"MCP server cwd is not a directory: {working_directory}")

    command_value = _require_string(server.get("command"), "MCP server command")
    configured_command = Path(command_value)
    if configured_command.is_absolute():
        raise RuntimeError("packaged MCP server command must be relative to its cwd")
    unresolved_command = _require_inside_plugin(
        (working_directory / configured_command).resolve(strict=False),
        plugin_root,
        "MCP server command",
    )

    # Codex resolves this extensionless path against the configured cwd and uses
    # PATHEXT on Windows. Materialize the package's known native suffix before
    # Popen so no parent-process cwd or PATH entry can shadow the bundled binary.
    executable_candidate = unresolved_command
    if os.name == "nt" and not executable_candidate.suffix:
        executable_candidate = executable_candidate.with_name(
            f"{executable_candidate.name}.exe"
        )
    try:
        executable = _require_inside_plugin(
            executable_candidate.resolve(strict=True),
            plugin_root,
            "resolved MCP server command",
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            f"packaged MCP command does not exist: {executable_candidate}"
        ) from error
    if not executable.is_file():
        raise RuntimeError(f"packaged MCP command is not a file: {executable}")
    if os.name != "nt" and not os.access(executable, os.X_OK):
        raise RuntimeError(f"packaged MCP command is not executable: {executable}")

    args_value = server.get("args", [])
    if not isinstance(args_value, list) or not all(
        isinstance(argument, str) for argument in args_value
    ):
        raise RuntimeError("MCP server args must be an array of strings")
    return [str(executable), *args_value], working_directory


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
        if not isinstance(config, dict):
            raise RuntimeError(".mcp.json must contain an object")
        servers = config.get("mcpServers")
        if not isinstance(servers, dict):
            raise RuntimeError(".mcp.json must contain an mcpServers object")
        server = servers.get("app-icon-toolkit")
        if not isinstance(server, dict):
            raise RuntimeError(".mcp.json omitted the app-icon-toolkit server")
        command, working_directory = resolve_packaged_server_process(plugin_root, server)
        try:
            self._process = subprocess.Popen(
                command,
                cwd=working_directory,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            raise RuntimeError(
                f"failed to start packaged MCP command {command[0]}: {error}"
            ) from error
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("MCP process did not expose stdio")
        if self._process.stderr is None:
            raise RuntimeError("MCP process did not expose stderr")
        self._start_readers()

    def _start_readers(self) -> None:
        self._messages: queue.Queue[dict[str, object] | BaseException | None] = queue.Queue()
        self._seen: list[dict[str, object]] = []
        self._stderr_capture = ""
        self._stderr_truncated = False
        self._stderr_overlap = ""
        self._stderr_has_ansi = False
        self._stderr_has_disallowed_diagnostic = False
        self._stderr_error: Exception | None = None
        self._closed = False
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def __enter__(self) -> McpProcess:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> bool:
        try:
            self.close()
        except Exception as shutdown_error:
            if isinstance(exception, Exception):
                raise ExceptionGroup(
                    "MCP protocol exchange and shutdown both failed",
                    [exception, shutdown_error],
                ) from None
            raise
        return False

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

    def _read_stderr(self) -> None:
        if self._process.stderr is None:
            self._stderr_error = RuntimeError("MCP stderr is unavailable")
            return
        try:
            while chunk := self._process.stderr.read(8192):
                diagnostic_probe = self._stderr_overlap + chunk
                self._stderr_has_ansi |= "\x1b" in diagnostic_probe
                self._stderr_has_disallowed_diagnostic |= bool(
                    DISALLOWED_DIAGNOSTIC.search(diagnostic_probe)
                )
                self._stderr_overlap = diagnostic_probe[-DIAGNOSTIC_OVERLAP:]
                remaining = STDERR_CAPTURE_LIMIT - len(self._stderr_capture)
                if remaining > 0:
                    self._stderr_capture += chunk[:remaining]
                if len(chunk) > remaining:
                    self._stderr_truncated = True
        except Exception as error:
            self._stderr_error = error

    def _captured_stderr(self) -> str:
        if self._stderr_truncated:
            return f"{self._stderr_capture}\n...[stderr truncated]"
        return self._stderr_capture

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
        if self._closed:
            return
        self._closed = True

        stdin = self._process.stdin
        stdout = self._process.stdout
        stderr_stream = self._process.stderr
        issues: list[tuple[str, Exception]] = []
        return_code: int | None = None

        if stdin is None:
            issues.append(("validate stdin", RuntimeError("stdin is unavailable")))
        else:
            try:
                stdin.close()
            except Exception as error:
                issues.append(("close stdin", error))

        try:
            return_code = self._process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            issues.append(("wait for graceful shutdown", error))
        except Exception as error:
            issues.append(("wait for graceful shutdown", error))

        if return_code is None:
            process_is_running = True
            try:
                observed_return_code = self._process.poll()
                process_is_running = observed_return_code is None
                if observed_return_code is not None:
                    return_code = observed_return_code
            except Exception as error:
                issues.append(("inspect process after failed wait", error))
            if process_is_running:
                try:
                    self._process.kill()
                except Exception as error:
                    issues.append(("kill process after failed wait", error))
            try:
                return_code = self._process.wait(timeout=10)
            except Exception as error:
                issues.append(("reap process after failed wait", error))

        for name, reader in (
            ("stdout", self._reader),
            ("stderr", self._stderr_reader),
        ):
            try:
                reader.join(timeout=10)
            except Exception as error:
                issues.append((f"join {name} reader", error))
            try:
                if reader.is_alive():
                    issues.append(
                        (f"join {name} reader", RuntimeError("reader did not stop"))
                    )
            except Exception as error:
                issues.append((f"inspect {name} reader", error))

        for name, stream in (
            ("stdin", stdin),
            ("stdout", stdout),
            ("stderr", stderr_stream),
        ):
            if stream is None:
                issues.append(
                    (f"close {name}", RuntimeError(f"{name} stream is unavailable"))
                )
                continue
            try:
                stream.close()
            except Exception as error:
                issues.append((f"close {name}", error))

        if self._stderr_error is not None:
            issues.append(("read stderr", self._stderr_error))
        stderr = self._captured_stderr()
        if return_code not in (None, 0):
            issues.append(
                (
                    "process exit",
                    RuntimeError(f"exit code {return_code}; stderr={stderr!r}"),
                )
            )
        if self._stderr_has_ansi:
            issues.append(
                ("validate stderr", RuntimeError("stderr contained ANSI escapes"))
            )
        if self._stderr_has_disallowed_diagnostic:
            issues.append(
                (
                    "validate stderr",
                    RuntimeError(f"stderr contained an error or warning: {stderr!r}"),
                )
            )
        if not self._seen:
            issues.append(
                ("validate protocol", RuntimeError("no protocol messages were produced"))
            )
        if issues:
            raise McpProcessFailure(issues)


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
        with mcp:
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_root", type=Path)
    arguments = parser.parse_args()
    plugin_root = arguments.plugin_root.resolve(strict=True)
    run_smoke(plugin_root)


if __name__ == "__main__":
    main()
