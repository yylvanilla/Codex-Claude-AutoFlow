#!/usr/bin/env python3
"""Fake Claude CLI for smoke-testing the orchestration workflow."""

from __future__ import annotations

import argparse
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", action="store_true")
    parser.add_argument("--permission-mode")
    parser.add_argument("--add-dir")
    parser.add_argument("--mode", choices=["implement", "revise"], required=True)
    parser.add_argument("rest", nargs="*")
    return parser.parse_args()


def main() -> int:
    stdin_text = sys.stdin.read()
    args = parse_args()

    if args.mode == "implement":
        print(
            """===SUMMARY===
已根据计划生成首轮补丁。
===END_SUMMARY===
===PATCH===
diff --git a/curatain/main.c b/curatain/main.c
--- a/curatain/main.c
+++ b/curatain/main.c
@@ -1,1 +1,1 @@
-old
+new
===END_PATCH===
"""
        )
        return 0

    marker = "已输出修正版补丁。"
    if "允许进入修正阶段" in stdin_text:
        marker = "已根据审查意见输出修正版补丁。"
    print(
        f"""===SUMMARY===
{marker}
===END_SUMMARY===
===PATCH===
diff --git a/curatain/main.c b/curatain/main.c
--- a/curatain/main.c
+++ b/curatain/main.c
@@ -1,1 +1,1 @@
-new
+newer
===END_PATCH===
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
