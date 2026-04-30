#!/usr/bin/env python3
"""Remove local build leftovers while keeping the built EXE."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parent


def is_within_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT.resolve())
        return True
    except Exception:
        return False


def remove_path(path: Path) -> tuple[str, str]:
    if not is_within_root(path):
        return ("blocked", str(path))
    if not path.exists():
        return ("missing", str(path))
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return ("removed", str(path))
    except PermissionError:
        return ("locked", str(path))
    except OSError as exc:
        return (f"error: {exc}", str(path))


def main() -> int:
    targets = [
        ROOT / ".venv_build",
        ROOT / "_build_tools",
        ROOT / "_tmp",
        ROOT / "build",
        ROOT / "dist" / "launcher_settings.json",
    ]

    targets.extend(path for path in ROOT.rglob("__pycache__"))
    targets.extend(path for path in ROOT.rglob("*.pyc"))
    targets.extend(path for path in ROOT.glob("*.spec"))

    seen: set[Path] = set()
    had_problem = False

    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        status, target_text = remove_path(target)
        print(f"{status:>8}  {target_text}")
        if status not in {"removed", "missing"}:
            had_problem = True

    print("\nCleanup finished.")
    if had_problem:
        print("Some items could not be removed. Close related processes and run again if needed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
