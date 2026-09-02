#!/usr/bin/env python3
"""Build the standalone winauto executable with PyInstaller."""

from __future__ import annotations

import subprocess
import sys
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def run(*args: str) -> None:
    subprocess.run(list(args), cwd=ROOT, check=True)


def main() -> int:
    # Remove only this project's generated directories/files so an old EXE
    # cannot be mistaken for the newly built artifact.
    build_dir = ROOT / "build"
    dist_exe = ROOT / "dist" / "winauto.exe"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    if dist_exe.exists():
        dist_exe.unlink()

    run(sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"))
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        "winauto",
        str(ROOT / "winauto.py"),
    )
    if not dist_exe.exists():
        raise RuntimeError(f"PyInstaller did not produce {dist_exe}")
    help_result = subprocess.run(
        [str(dist_exe), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    if "--token" in help_text:
        raise RuntimeError("built EXE still exposes removed authentication options")
    print(f"Built {dist_exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
