#!/usr/bin/env python3
"""A small ADB-like Windows command execution client and agent.

The same script can be used locally or as a remote agent.  The wire protocol
is deliberately small: a four-byte big-endian frame length followed by a JSON
message.  Stream payloads are base64 encoded so arbitrary command output is
not corrupted by JSON or console encodings.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from winauto_modules.file_transfer import FILE_CHUNK_SIZE, iter_files, push_root, read_chunks, safe_destination
from winauto_modules.protocol import MAX_FRAME_SIZE, ProtocolError, recv_frame, send_frame
from winauto_modules.screenshot import Screenshot, capture_screenshot


VERSION = "0.3.0"
DEFAULT_PORT = 27889
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 256 * 1024 * 1024
READ_CHUNK_SIZE = 4096


def _strip_separator(command: Iterable[str]) -> List[str]:
    values = list(command)
    if values and values[0] == "--":
        return values[1:]
    return values


def _find_powershell() -> str:
    candidates = ["powershell.exe", "pwsh.exe"] if os.name == "nt" else ["pwsh", "powershell"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError("PowerShell was not found; install PowerShell or use --shell raw")


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def build_command(
    shell: str, command: Iterable[str], program: Optional[str] = None, interactive: bool = False
) -> List[str]:
    """Build an argument vector without passing through Python's shell=True."""
    values = _strip_separator(command)
    if program:
        return [program, *values]
    if not values:
        if interactive and shell == "cmd":
            return ["cmd.exe"] if os.name == "nt" else ["sh"]
        if interactive and shell == "powershell":
            return [_find_powershell(), "-NoLogo", "-NoProfile"]
        raise ValueError("a command is required")
    if shell == "raw":
        return values

    command_text = " ".join(values)
    if shell == "cmd":
        executable = "cmd.exe" if os.name == "nt" else "sh"
        # Accept familiar `cmd /c ...` and `cmd /k ...` forms when users
        # Migrate existing batch snippets to WinAuto.
        if os.name == "nt" and values[0].lower() in {"/c", "/k"}:
            return [executable, "/d", *values]
        return [executable, "/d", "/s", "/c", command_text] if os.name == "nt" else [executable, "-c", command_text]
    if shell == "powershell":
        executable = _find_powershell()
        args = [executable, "-NoLogo", "-NoProfile"]
        if not interactive:
            args.append("-NonInteractive")
        args.extend(["-Command", command_text])
        return args
    raise ValueError(f"unsupported shell: {shell}")


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def run_process(
    command: List[str],
    cwd: Optional[str],
    env_overrides: Optional[Dict[str, str]],
    timeout: Optional[float],
    on_output: Callable[[str, bytes], None],
) -> Tuple[int, bool]:
    env = os.environ.copy()
    if env_overrides:
        env.update({str(key): str(value) for key, value in env_overrides.items()})

    process = subprocess.Popen(
        command,
        cwd=cwd or None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_creation_flags(),
    )

    byte_counts = {"stdout": 0, "stderr": 0}
    truncated = {"stdout": False, "stderr": False}

    def pump(name: str, pipe: Any) -> None:
        try:
            while True:
                chunk = pipe.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                remaining = MAX_OUTPUT_BYTES - byte_counts[name]
                byte_counts[name] += len(chunk)
                if remaining > 0:
                    on_output(name, chunk[:remaining])
                if remaining < len(chunk) and not truncated[name]:
                    truncated[name] = True
                    on_output(name, b"\n[winauto: output truncated]\n")
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=pump, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=pump, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    timed_out = False
    while process.poll() is None:
        if timeout is not None and time.monotonic() - started >= timeout:
            timed_out = True
            _terminate_process_tree(process)
            break
        time.sleep(0.05)

    if timed_out:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            process.wait()
    else:
        process.wait()

    for thread in threads:
        thread.join(timeout=5)
    # 124 is the conventional timeout status and avoids trusting taskkill's
    # sometimes-successful return code for a process that was forcibly ended.
    code = 124 if timed_out else int(process.returncode if process.returncode is not None else 1)
    return code, timed_out


def _machine_info() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "pid": os.getpid(),
    }


def _connections_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) / "WinAuto" if local_app_data else Path.home() / ".winauto"
    return root / "connections.json"


def _load_connections() -> List[Dict[str, Any]]:
    try:
        data = json.loads(_connections_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("target"), str)]


def _remember_connection(target: str, agent: Dict[str, Any]) -> None:
    connections = [item for item in _load_connections() if item.get("target") != target]
    connections.append(
        {
            "target": target,
            "hostname": agent.get("hostname", ""),
            "version": agent.get("version", ""),
            "last_connected": int(time.time()),
        }
    )
    path = _connections_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(connections, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"winauto: connected, but could not save local connection record: {exc}", file=sys.stderr)


def _perform_handshake(sock: socket.socket) -> Dict[str, Any]:
    send_frame(sock, {"type": "hello", "client_version": VERSION})
    response = recv_frame(sock)
    if not response or response.get("type") != "hello_ok":
        message = response.get("message", "agent rejected connection") if response else "agent closed connection"
        raise ConnectionError(message)
    agent = response.get("agent", {})
    return agent if isinstance(agent, dict) else {}


def _ping_remote(target: str, timeout: float = 10.0) -> Dict[str, Any]:
    sock = _connect_target(target, timeout=timeout)
    try:
        agent = _perform_handshake(sock)
        send_frame(sock, {"operation": "ping"})
        response = recv_frame(sock)
        if not response or response.get("type") != "pong":
            message = response.get("message", "agent did not respond to ping") if response else "agent closed connection"
            raise ConnectionError(message)
        pong_agent = response.get("agent")
        return pong_agent if isinstance(pong_agent, dict) else agent
    finally:
        sock.close()


def _stream_pull(request: Dict[str, Any], sock: socket.socket, send_lock: threading.Lock) -> None:
    remote_text = str(request.get("remote_path", "")).strip()
    if not remote_text:
        raise ValueError("remote_path is required")
    root = Path(remote_text)
    if not root.exists():
        raise FileNotFoundError(remote_text)
    if not root.is_file() and not root.is_dir():
        raise ValueError("remote_path must point to a file or directory")

    kind = "file" if root.is_file() else "directory"
    root_name = root.name or "download"

    def emit(message: Dict[str, Any]) -> None:
        with send_lock:
            send_frame(sock, message)

    emit({"type": "pull_start", "kind": kind, "name": root_name})
    file_count = 0
    total_bytes = 0
    for path, relative_name in iter_files(root):
        size = path.stat().st_size
        emit({"type": "pull_file", "path": relative_name, "size": size})
        digest = hashlib.sha256()
        sent = 0
        for chunk in read_chunks(path, FILE_CHUNK_SIZE):
            digest.update(chunk)
            sent += len(chunk)
            emit({"type": "pull_chunk", "data_b64": base64.b64encode(chunk).decode("ascii")})
        emit(
            {
                "type": "pull_file_end",
                "path": relative_name,
                "size": sent,
                "sha256": digest.hexdigest(),
            }
        )
        file_count += 1
        total_bytes += sent
    emit({"type": "pull_done", "files": file_count, "bytes": total_bytes})


def _receive_push(request: Dict[str, Any], sock: socket.socket, send_lock: threading.Lock) -> None:
    kind = str(request.get("kind", ""))
    source_name = str(request.get("name", "")).strip()
    if kind not in {"file", "directory"}:
        raise ValueError("kind must be file or directory")
    if not source_name or source_name in {".", ".."}:
        raise ValueError("name is required")
    destination_root = push_root(str(request.get("remote_path", "")), kind, source_name)
    single_file = kind == "file"
    if kind == "directory":
        destination_root.mkdir(parents=True, exist_ok=True)

    def emit(message: Dict[str, Any]) -> None:
        with send_lock:
            send_frame(sock, message)

    emit({"type": "push_ready", "kind": kind, "destination": str(destination_root)})
    output_handle: Optional[Any] = None
    temp_path: Optional[Path] = None
    current_final: Optional[Path] = None
    current_hash: Optional[Any] = None
    current_size = 0
    expected_size = 0
    file_count = 0
    total_bytes = 0
    try:
        while True:
            message = recv_frame(sock)
            if message is None:
                raise ProtocolError("connection closed before push completed")
            message_type = message.get("type")
            if message_type == "push_file":
                if output_handle is not None:
                    raise ValueError("received a new file before the previous file ended")
                relative_name = str(message.get("path", ""))
                if single_file:
                    current_final = destination_root
                else:
                    current_final = safe_destination(destination_root, relative_name)
                expected_size = int(message.get("size", -1))
                if expected_size < 0:
                    raise ValueError("invalid file size")
                current_final.parent.mkdir(parents=True, exist_ok=True)
                temp_path = current_final.with_name(current_final.name + ".winauto.part")
                output_handle = temp_path.open("wb")
                current_hash = hashlib.sha256()
                current_size = 0
            elif message_type == "push_chunk":
                if output_handle is None or current_hash is None:
                    raise ValueError("received a file chunk without a file")
                try:
                    chunk = base64.b64decode(message.get("data_b64", ""), validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("invalid file chunk encoding") from exc
                output_handle.write(chunk)
                current_hash.update(chunk)
                current_size += len(chunk)
            elif message_type == "push_file_end":
                if output_handle is None or temp_path is None or current_final is None or current_hash is None:
                    raise ValueError("received a file end without a file")
                output_handle.close()
                output_handle = None
                sent_size = int(message.get("size", -1))
                sent_hash = str(message.get("sha256", ""))
                received_hash = current_hash.hexdigest()
                if current_size != expected_size or sent_size != current_size or received_hash != sent_hash:
                    raise IOError(
                        f"file verification failed for {current_final} "
                        f"(size {current_size}/{expected_size}, sha256 {received_hash}/{sent_hash})"
                    )
                os.replace(temp_path, current_final)
                temp_path = None
                file_count += 1
                total_bytes += current_size
                current_final = None
                current_hash = None
                current_size = 0
                expected_size = 0
            elif message_type == "push_done":
                if output_handle is not None:
                    raise ValueError("push completed while a file was still open")
                emit({"type": "push_done", "files": file_count, "bytes": total_bytes})
                return
            else:
                raise ValueError(f"unexpected push message: {message_type}")
    finally:
        if output_handle is not None:
            output_handle.close()
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _stream_screenshot(sock: socket.socket, send_lock: threading.Lock) -> None:
    screenshot = capture_screenshot()
    payload = screenshot.png
    if len(payload) > MAX_SCREENSHOT_BYTES:
        raise ValueError(f"screenshot is too large: {len(payload)} bytes")

    def emit(message: Dict[str, Any]) -> None:
        with send_lock:
            send_frame(sock, message)

    emit(
        {
            "type": "screenshot_start",
            "format": "png",
            "width": screenshot.width,
            "height": screenshot.height,
            "size": len(payload),
        }
    )
    digest = hashlib.sha256()
    for offset in range(0, len(payload), FILE_CHUNK_SIZE):
        chunk = payload[offset : offset + FILE_CHUNK_SIZE]
        digest.update(chunk)
        emit({"type": "screenshot_chunk", "data_b64": base64.b64encode(chunk).decode("ascii")})
    emit({"type": "screenshot_end", "size": len(payload), "sha256": digest.hexdigest()})


class WinautoRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            hello = recv_frame(self.request)
            if not hello or hello.get("type") != "hello":
                raise ProtocolError("hello is required")
            send_frame(self.request, {"type": "hello_ok", "agent": _machine_info()})

            request = recv_frame(self.request)
            if not request:
                return
            operation = request.get("operation")
            send_lock = threading.Lock()
            if operation == "ping":
                send_frame(self.request, {"type": "pong", "agent": _machine_info()})
                return
            if operation == "pull":
                try:
                    _stream_pull(request, self.request, send_lock)
                except (OSError, ValueError, RuntimeError) as exc:
                    with send_lock:
                        send_frame(self.request, {"type": "error", "code": "pull_failed", "message": str(exc)})
                return
            if operation == "push":
                try:
                    _receive_push(request, self.request, send_lock)
                except (OSError, ProtocolError, ValueError, RuntimeError) as exc:
                    with send_lock:
                        send_frame(self.request, {"type": "error", "code": "push_failed", "message": str(exc)})
                return
            if operation == "screenshot":
                try:
                    _stream_screenshot(self.request, send_lock)
                except (OSError, ProtocolError, ValueError, RuntimeError) as exc:
                    with send_lock:
                        send_frame(
                            self.request,
                            {"type": "error", "code": "screenshot_failed", "message": str(exc)},
                        )
                return
            if operation != "exec":
                send_frame(self.request, {"type": "error", "code": "unsupported", "message": "unsupported operation"})
                return

            shell = str(request.get("shell", "raw"))
            command = request.get("command", [])
            if not isinstance(command, list):
                raise ValueError("command must be an array")
            executable = build_command(shell, [str(item) for item in command], request.get("program"))
            timeout_value = request.get("timeout_ms")
            timeout = None if timeout_value in (None, 0) else max(0.001, float(timeout_value) / 1000)
            def emit(stream: str, data: bytes) -> None:
                if not data:
                    return
                message = {
                    "type": stream,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                }
                with send_lock:
                    send_frame(self.request, message)

            try:
                code, timed_out = run_process(
                    executable,
                    str(request["cwd"]) if request.get("cwd") else None,
                    request.get("env") if isinstance(request.get("env"), dict) else None,
                    timeout,
                    emit,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                send_frame(self.request, {"type": "error", "code": "exec_failed", "message": str(exc)})
                return
            with send_lock:
                send_frame(
                    self.request,
                    {"type": "exit", "code": code, "timed_out": timed_out},
                )
        except (OSError, ProtocolError, ValueError, json.JSONDecodeError) as exc:
            try:
                send_frame(self.request, {"type": "error", "code": "bad_request", "message": str(exc)})
            except OSError:
                pass


class WinautoServer(socketserver.ThreadingTCPServer):
    # Do not allow two Agent processes to share a port. On Windows, allowing
    # SO_REUSEADDR can route clients to an older process unexpectedly.
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: Tuple[str, int]):
        super().__init__(address, WinautoRequestHandler)


def _connect_target(target: str, timeout: float = 10.0) -> socket.socket:
    if target.startswith("[") and "]" in target:
        host, _, port_text = target[1:].partition("]:")
    else:
        host, separator, port_text = target.rpartition(":")
        if not separator:
            raise ValueError("target must be HOST:PORT")
    if not host or not port_text.isdigit():
        raise ValueError("target must be HOST:PORT")
    sock = socket.create_connection((host, int(port_text)), timeout=timeout)
    sock.settimeout(None)
    return sock


def _write_output(stream: str, data: bytes) -> None:
    output = sys.stderr.buffer if stream == "stderr" else sys.stdout.buffer
    output.write(data)
    output.flush()


def _screenshot_destination(output: Optional[str]) -> Path:
    default_name = time.strftime("screenshot-%Y%m%d-%H%M%S.png")
    if not output:
        return Path(default_name)
    destination = Path(output)
    if destination.is_dir() or output.endswith(("/", "\\")):
        return destination / default_name
    if not destination.suffix:
        return destination.with_suffix(".png")
    if destination.suffix.lower() != ".png":
        raise ValueError("screenshot output must use a .png extension")
    return destination


def _save_screenshot(destination: Path, screenshot: Screenshot) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.name + ".winauto.part")
    try:
        temp_path.write_bytes(screenshot.png)
        os.replace(temp_path, destination)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _screenshot_local(args: argparse.Namespace) -> int:
    try:
        screenshot = capture_screenshot()
        destination = _screenshot_destination(args.output)
        _save_screenshot(destination, screenshot)
        print(
            f"screenshot saved to {destination.resolve()} "
            f"({screenshot.width}x{screenshot.height}, {len(screenshot.png)} bytes)"
        )
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"winauto: screenshot failed: {exc}", file=sys.stderr)
        return 1


def _screenshot_remote(args: argparse.Namespace) -> int:
    sock = _connect_target(args.target)
    destination: Optional[Path] = None
    temp_path: Optional[Path] = None
    output_handle: Optional[Any] = None
    digest = hashlib.sha256()
    received_size = 0
    expected_size = -1
    width = 0
    height = 0
    try:
        _perform_handshake(sock)
        send_frame(sock, {"operation": "screenshot"})
        while True:
            response = recv_frame(sock)
            if response is None:
                raise ConnectionError("agent closed the connection before screenshot completed")
            kind = response.get("type")
            if kind == "screenshot_start":
                if output_handle is not None:
                    raise ValueError("received more than one screenshot_start message")
                if response.get("format") != "png":
                    raise ValueError(f"unsupported screenshot format: {response.get('format')}")
                width = int(response.get("width", 0))
                height = int(response.get("height", 0))
                expected_size = int(response.get("size", -1))
                if width <= 0 or height <= 0:
                    raise ValueError("invalid screenshot dimensions")
                if expected_size <= 0 or expected_size > MAX_SCREENSHOT_BYTES:
                    raise ValueError(f"invalid screenshot size: {expected_size}")
                destination = _screenshot_destination(args.output).resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                temp_path = destination.with_name(destination.name + ".winauto.part")
                output_handle = temp_path.open("wb")
            elif kind == "screenshot_chunk":
                if output_handle is None:
                    raise ValueError("received screenshot data before screenshot_start")
                try:
                    chunk = base64.b64decode(response.get("data_b64", ""), validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("invalid screenshot chunk encoding") from exc
                if received_size + len(chunk) > expected_size:
                    raise ValueError("screenshot data exceeds its advertised size")
                output_handle.write(chunk)
                digest.update(chunk)
                received_size += len(chunk)
            elif kind == "screenshot_end":
                if output_handle is None or temp_path is None or destination is None:
                    raise ValueError("received screenshot_end before screenshot_start")
                output_handle.close()
                output_handle = None
                sent_size = int(response.get("size", -1))
                sent_hash = str(response.get("sha256", ""))
                if received_size != expected_size or sent_size != received_size or digest.hexdigest() != sent_hash:
                    raise IOError("screenshot verification failed")
                os.replace(temp_path, destination)
                temp_path = None
                print(f"screenshot saved to {destination} ({width}x{height}, {received_size} bytes)")
                return 0
            elif kind == "error":
                print(f"{response.get('code', 'error')}: {response.get('message', '')}", file=sys.stderr)
                return 1
            else:
                raise ValueError(f"unexpected screenshot message: {kind}")
    except (OSError, ProtocolError, ValueError, ConnectionError) as exc:
        print(f"winauto: screenshot failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if output_handle is not None:
            output_handle.close()
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        sock.close()


def _execute_remote(args: argparse.Namespace, command: List[str]) -> int:
    sock = _connect_target(args.target)
    try:
        try:
            _perform_handshake(sock)
        except ConnectionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        send_frame(
            sock,
            {
                "operation": "exec",
                "shell": args.shell,
                "command": command,
                "program": args.program,
                "cwd": args.cwd,
                "env": {},
                "timeout_ms": args.timeout,
            },
        )
        while True:
            response = recv_frame(sock)
            if response is None:
                print("agent closed the connection before exit", file=sys.stderr)
                return 1
            kind = response.get("type")
            if kind in {"stdout", "stderr"}:
                try:
                    _write_output(kind, base64.b64decode(response.get("data_b64", "")))
                except (ValueError, TypeError) as exc:
                    print(f"invalid output frame: {exc}", file=sys.stderr)
                    return 1
            elif kind == "exit":
                if response.get("timed_out"):
                    print("winauto: command timed out", file=sys.stderr)
                return int(response.get("code", 1))
            elif kind == "error":
                print(f"{response.get('code', 'error')}: {response.get('message', '')}", file=sys.stderr)
                return 1
    finally:
        sock.close()


def _pull_remote(args: argparse.Namespace) -> int:
    sock = _connect_target(args.target)
    temp_path: Optional[Path] = None
    output_handle: Optional[Any] = None
    current_hash: Optional[Any] = None
    current_size = 0
    expected_size = 0
    current_final: Optional[Path] = None
    destination_root: Optional[Path] = None
    single_file_destination: Optional[Path] = None
    total_files = 0
    total_bytes = 0
    try:
        try:
            _perform_handshake(sock)
        except ConnectionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        send_frame(sock, {"operation": "pull", "remote_path": args.remote})
        while True:
            response = recv_frame(sock)
            if response is None:
                print("agent closed the connection before pull completed", file=sys.stderr)
                return 1
            kind = response.get("type")
            if kind == "pull_start":
                transfer_kind = response.get("kind")
                root_name = str(response.get("name") or "download")
                local = Path(args.local) if args.local else None
                if transfer_kind == "file":
                    if local is None or local.is_dir():
                        single_file_destination = (local or Path(".")) / root_name
                    else:
                        single_file_destination = local
                    destination_root = single_file_destination.parent.resolve()
                elif transfer_kind == "directory":
                    destination_root = (local or Path(root_name)).resolve()
                    destination_root.mkdir(parents=True, exist_ok=True)
                else:
                    raise ValueError(f"unsupported pull type: {transfer_kind}")
            elif kind == "pull_file":
                if output_handle is not None:
                    raise ValueError("received a new file before the previous file ended")
                if destination_root is None:
                    raise ValueError("pull_file received before pull_start")
                relative_name = str(response.get("path", ""))
                if single_file_destination is not None:
                    current_final = single_file_destination.resolve()
                else:
                    current_final = safe_destination(destination_root, relative_name)
                expected_size = int(response.get("size", -1))
                if expected_size < 0:
                    raise ValueError("invalid remote file size")
                current_final.parent.mkdir(parents=True, exist_ok=True)
                temp_path = current_final.with_name(current_final.name + ".winauto.part")
                output_handle = temp_path.open("wb")
                current_hash = hashlib.sha256()
                current_size = 0
            elif kind == "pull_chunk":
                if output_handle is None or current_hash is None:
                    raise ValueError("received a file chunk without a file")
                try:
                    chunk = base64.b64decode(response.get("data_b64", ""), validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("invalid file chunk encoding") from exc
                output_handle.write(chunk)
                current_hash.update(chunk)
                current_size += len(chunk)
            elif kind == "pull_file_end":
                if output_handle is None or temp_path is None or current_final is None or current_hash is None:
                    raise ValueError("received a file end without a file")
                output_handle.close()
                output_handle = None
                received_hash = current_hash.hexdigest()
                expected_hash = str(response.get("sha256", ""))
                if current_size != expected_size or received_hash != expected_hash:
                    raise IOError(
                        f"file verification failed for {current_final} "
                        f"(size {current_size}/{expected_size}, sha256 {received_hash}/{expected_hash})"
                    )
                os.replace(temp_path, current_final)
                temp_path = None
                total_files += 1
                total_bytes += current_size
                current_final = None
                current_hash = None
                current_size = 0
                expected_size = 0
                single_file_destination = None
            elif kind == "pull_done":
                if output_handle is not None:
                    raise ValueError("pull completed while a file was still open")
                print(f"pulled {total_files} file(s), {total_bytes} byte(s)")
                return 0
            elif kind == "error":
                print(f"{response.get('code', 'error')}: {response.get('message', '')}", file=sys.stderr)
                return 1
            else:
                raise ValueError(f"unexpected pull message: {kind}")
    except (OSError, ProtocolError, ValueError, ConnectionError) as exc:
        print(f"winauto: pull failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if output_handle is not None:
            output_handle.close()
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        sock.close()


def _push_remote(args: argparse.Namespace) -> int:
    source = Path(args.local)
    if not source.exists():
        print(f"winauto: local path does not exist: {source}", file=sys.stderr)
        return 1
    if not source.is_file() and not source.is_dir():
        print(f"winauto: local path must be a file or directory: {source}", file=sys.stderr)
        return 1
    kind = "file" if source.is_file() else "directory"
    source_name = source.name or source.resolve().name
    sock = _connect_target(args.target)
    total_files = 0
    total_bytes = 0
    try:
        try:
            _perform_handshake(sock)
        except ConnectionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        send_frame(
            sock,
            {
                "operation": "push",
                "remote_path": args.remote or "",
                "kind": kind,
                "name": source_name,
            },
        )
        ready = recv_frame(sock)
        if not ready or ready.get("type") != "push_ready":
            message = ready.get("message", "agent rejected push") if ready else "agent closed connection"
            print(message, file=sys.stderr)
            return 1
        for path, relative_name in iter_files(source):
            digest = hashlib.sha256()
            file_size = 0
            try:
                advertised_size = path.stat().st_size
                send_frame(sock, {"type": "push_file", "path": relative_name, "size": advertised_size})
                for chunk in read_chunks(path, FILE_CHUNK_SIZE):
                    digest.update(chunk)
                    file_size += len(chunk)
                    send_frame(sock, {"type": "push_chunk", "data_b64": base64.b64encode(chunk).decode("ascii")})
            except OSError as exc:
                print(f"winauto: unable to read {path}: {exc}", file=sys.stderr)
                return 1
            send_frame(
                sock,
                {
                    "type": "push_file_end",
                    "path": relative_name,
                    "size": file_size,
                    "sha256": digest.hexdigest(),
                },
            )
            total_files += 1
            total_bytes += file_size
        send_frame(sock, {"type": "push_done"})
        response = recv_frame(sock)
        if not response:
            print("agent closed the connection before push completed", file=sys.stderr)
            return 1
        if response.get("type") == "error":
            print(f"{response.get('code', 'error')}: {response.get('message', '')}", file=sys.stderr)
            return 1
        if response.get("type") != "push_done":
            print(f"unexpected push response: {response.get('type')}", file=sys.stderr)
            return 1
        print(f"pushed {total_files} file(s), {total_bytes} byte(s)")
        return 0
    except (OSError, ProtocolError, ValueError, ConnectionError) as exc:
        print(f"winauto: push failed: {exc}", file=sys.stderr)
        return 1
    finally:
        sock.close()


def _execute_local(args: argparse.Namespace, command: List[str]) -> int:
    executable = build_command(args.shell, command, args.program)
    try:
        code, timed_out = run_process(executable, args.cwd, None, args.timeout / 1000 if args.timeout else None, _write_output)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"winauto: {exc}", file=sys.stderr)
        return 1
    if timed_out:
        print("winauto: command timed out", file=sys.stderr)
    return code


def _run_interactive(args: argparse.Namespace, command: List[str]) -> int:
    if args.target:
        print("winauto: remote interactive shell is not included in this MVP; use exec", file=sys.stderr)
        return 2
    try:
        executable = build_command(args.shell, command, args.program, interactive=True)
        return subprocess.call(executable, cwd=args.cwd or None)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"winauto: {exc}", file=sys.stderr)
        return 1


def _run_agent(args: argparse.Namespace) -> int:
    try:
        server = WinautoServer((args.host, args.port))
    except OSError as exc:
        print(
            f"winauto: unable to listen on {args.host}:{args.port}; "
            f"the port may already be used by another Agent: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"winauto agent {VERSION} listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping agent", flush=True)
    finally:
        server.shutdown()
        server.server_close()
    return 0


def _run_connect(args: argparse.Namespace) -> int:
    timeout = args.timeout / 1000 if args.timeout else 10.0
    try:
        agent = _ping_remote(args.target, timeout=timeout)
    except (OSError, ProtocolError, ValueError, ConnectionError) as exc:
        print(f"winauto: unable to connect to {args.target}: {exc}", file=sys.stderr)
        return 1
    _remember_connection(args.target, agent)
    hostname = agent.get("hostname", "unknown-host")
    print(f"connected to {args.target} ({hostname})")
    return 0


def _run_devices() -> int:
    connections = _load_connections()
    if not connections:
        print("No connected targets")
        return 0
    print("TARGET\tSTATE\tHOSTNAME")
    for item in connections:
        target = str(item["target"])
        try:
            agent = _ping_remote(target, timeout=2.0)
            print(f"{target}\tonline\t{agent.get('hostname', '')}")
        except (OSError, ProtocolError, ValueError, ConnectionError):
            print(f"{target}\toffline\t{item.get('hostname', '')}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ADB-like Windows command execution tool")
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("-s", dest="global_target", help="target agent in HOST:PORT form")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    agent = subparsers.add_parser("agent", help="run a command execution agent")
    agent.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    agent.add_argument("--port", type=int, default=DEFAULT_PORT)

    exec_parser = subparsers.add_parser("exec", help="execute one command locally or on an agent")
    exec_parser.set_defaults(target=None)
    exec_parser.add_argument("--shell", choices=["cmd", "powershell", "raw"], default="cmd")
    exec_parser.add_argument("--program", help="explicit executable path; overrides --shell wrapper")
    exec_parser.add_argument("--cwd")
    exec_parser.add_argument(
        "--timeout", type=_non_negative_int, default=0, help="timeout in milliseconds; 0 means no timeout"
    )
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)

    cmd_parser = subparsers.add_parser( "cmd", help="execute a Windows CMD command (shortcut for exec --shell cmd)")
    cmd_parser.set_defaults(target=None)
    cmd_parser.add_argument("--cwd")
    cmd_parser.add_argument("--timeout", type=_non_negative_int, default=0, help="timeout in milliseconds; 0 means no timeout")
    cmd_parser.set_defaults(shell="cmd", program=None)
    cmd_parser.add_argument("command", nargs=argparse.REMAINDER)

    pull_parser = subparsers.add_parser("pull", help="pull a file or directory from an agent")
    pull_parser.set_defaults(target=None)
    pull_parser.add_argument("remote", help="remote file or directory path")
    pull_parser.add_argument("local", nargs="?", help="local file or directory path (default: current directory)")

    push_parser = subparsers.add_parser("push", help="push a file or directory to an agent")
    push_parser.set_defaults(target=None)
    push_parser.add_argument("local", help="local file or directory path")
    push_parser.add_argument("remote", nargs="?", help="remote file or directory path (default: current directory)")

    screenshot_parser = subparsers.add_parser(
        "screenshot", help="capture the local or remote Windows desktop as PNG"
    )
    screenshot_parser.set_defaults(target=None)
    screenshot_parser.add_argument(
        "output",
        nargs="?",
        help="local PNG file or directory (default: timestamped file in current directory)",
    )

    connect_parser = subparsers.add_parser("connect", help="check and remember an agent connection")
    connect_parser.add_argument("target", help="agent in HOST:PORT form")
    connect_parser.add_argument("--timeout", type=_non_negative_int, default=0, help="connection timeout in milliseconds; 0 means 10 seconds")

    subparsers.add_parser("devices", help="list remembered agents and their current state")

    shell_parser = subparsers.add_parser("shell", help="open an interactive local shell")
    shell_parser.set_defaults(target=None)
    shell_parser.add_argument("--shell", choices=["cmd", "powershell", "raw"], default="cmd")
    shell_parser.add_argument("--program")
    shell_parser.add_argument("--cwd")
    shell_parser.add_argument("command", nargs=argparse.REMAINDER)

    info = subparsers.add_parser("info", help="print local machine information")
    info.set_defaults(show_info=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "show_info", False):
        print(json.dumps(_machine_info(), ensure_ascii=False, indent=2))
        return 0
    if args.global_target:
        if args.subcommand not in {"exec", "cmd", "shell", "pull", "push", "screenshot"}:
            parser.error("-s is only valid with exec, cmd, shell, pull, push, or screenshot")
        args.target = args.global_target
    if args.subcommand == "agent":
        return _run_agent(args)
    if args.subcommand == "connect":
        return _run_connect(args)
    if args.subcommand == "devices":
        return _run_devices()
    if args.subcommand == "pull":
        if not args.target:
            parser.error("pull requires -s HOST:PORT")
        return _pull_remote(args)
    if args.subcommand == "push":
        if not args.target:
            parser.error("push requires -s HOST:PORT")
        return _push_remote(args)
    if args.subcommand == "screenshot":
        if args.target:
            try:
                return _screenshot_remote(args)
            except (OSError, ValueError) as exc:
                print(f"winauto: screenshot failed: {exc}", file=sys.stderr)
                return 1
        return _screenshot_local(args)
    command = _strip_separator(args.command)
    if args.subcommand == "shell":
        if not command and not args.program:
            command = []
        elif not command and args.program:
            command = []
        return _run_interactive(args, command)
    if not command and not args.program:
        parser.error("exec requires a command after --")
    if args.target:
        try:
            return _execute_remote(args, command)
        except (OSError, ValueError) as exc:
            print(f"winauto: {exc}", file=sys.stderr)
            return 1
    return _execute_local(args, command)


if __name__ == "__main__":
    raise SystemExit(main())
