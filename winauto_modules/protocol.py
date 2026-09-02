"""Framed JSON transport shared by the WinAuto client and Agent."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any, Dict, List, Optional


MAX_FRAME_SIZE = 16 * 1024 * 1024


class ProtocolError(Exception):
    """Raised when a peer sends an invalid wire message."""


def _read_exact(sock: socket.socket, size: int) -> Optional[bytes]:
    chunks: List[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            if not chunks:
                return None
            raise ProtocolError("connection closed in the middle of a frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, message: Dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > MAX_FRAME_SIZE:
        raise ProtocolError("message is too large")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def recv_frame(sock: socket.socket) -> Optional[Dict[str, Any]]:
    header = _read_exact(sock, 4)
    if header is None:
        return None
    (size,) = struct.unpack("!I", header)
    if size <= 0 or size > MAX_FRAME_SIZE:
        raise ProtocolError(f"invalid frame size: {size}")
    payload = _read_exact(sock, size)
    if payload is None:
        raise ProtocolError("connection closed before frame payload")
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON frame") from exc
    if not isinstance(message, dict):
        raise ProtocolError("frame must contain a JSON object")
    return message
