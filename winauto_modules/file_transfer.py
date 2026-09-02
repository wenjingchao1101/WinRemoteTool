"""Streaming file-transfer helpers for WinAuto pull and push operations."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterator, Tuple


FILE_CHUNK_SIZE = 64 * 1024


def iter_files(root: Path) -> Iterator[Tuple[Path, str]]:
    """Yield files and POSIX-style relative names in deterministic order."""
    if root.is_file():
        yield root, root.name
        return
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path, path.relative_to(root).as_posix()


def read_chunks(path: Path, chunk_size: int = FILE_CHUNK_SIZE) -> Iterator[bytes]:
    with path.open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                return
            yield chunk


def safe_destination(base: Path, relative_name: str) -> Path:
    """Resolve a server-provided relative path without allowing traversal."""
    normalized = relative_name.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"invalid relative file name: {relative_name!r}")
    base_resolved = base.resolve()
    candidate = (base_resolved / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(base_resolved)
    except (ValueError, OSError) as exc:
        raise ValueError(f"file path escapes destination: {relative_name!r}") from exc
    return candidate


def push_root(remote_path: str, kind: str, source_name: str) -> Path:
    """Resolve the destination root using familiar file-copy semantics."""
    remote = Path(remote_path) if remote_path else Path(".")
    if kind == "file":
        if not remote_path or remote.is_dir() or remote_path.endswith(("/", "\\")):
            return remote / source_name
        return remote
    if kind == "directory":
        return remote / source_name if not remote_path else remote
    raise ValueError(f"unsupported push type: {kind}")
