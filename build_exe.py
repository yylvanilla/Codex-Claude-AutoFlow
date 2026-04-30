#!/usr/bin/env python3
"""Build the GUI launcher into a Windows executable."""

from __future__ import annotations

import os
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_TOOLS = SCRIPT_DIR / "_build_tools"
TMP_DIR = SCRIPT_DIR / "_tmp"
DIST_DIR = SCRIPT_DIR / "dist"
BUILD_DIR = SCRIPT_DIR / "build"


def load_pyinstaller_run():
    if BUILD_TOOLS.exists():
        sys.path.insert(0, str(BUILD_TOOLS))

    try:
        from PyInstaller.__main__ import run  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "PyInstaller is not available. Install it globally with 'pip install pyinstaller' "
            "or place it under '_build_tools' before running this script."
        ) from exc

    return run


def main() -> int:
    run = load_pyinstaller_run()

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(TMP_DIR)
    os.environ["TMP"] = str(TMP_DIR)

    run(
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            "CodexClaudeWorkflow",
            "--distpath",
            str(DIST_DIR),
            "--workpath",
            str(BUILD_DIR),
            "--specpath",
            str(BUILD_DIR),
            str(SCRIPT_DIR / "workflow_launcher.py"),
        ]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
