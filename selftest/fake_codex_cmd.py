#!/usr/bin/env python3
"""Fake Codex CLI for smoke-testing the orchestration workflow."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["exec"])
    parser.add_argument("--cd")
    parser.add_argument("--output-last-message", dest="output_last_message")
    parser.add_argument("--mode", choices=["plan", "review"], required=True)
    parser.add_argument("rest", nargs="*")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _stdin = sys.stdin.read()

    if args.mode == "plan":
        content = """# 计划

## 目标
- 增加一个演示改动

## 改动点
- 修改 `main.c`

## 影响文件
- `main.c`

## 测试建议
- 进行编译检查

## 验收标准
- 计划、补丁、审查和修正产物都成功落盘
"""
    else:
        content = """# 审查报告

1. 是否满足需求：基本满足
2. 潜在 bug / 风险：未发现严重风险
3. 风格或架构问题：输出结构符合预期
4. 必改项与可选优化项：建议进入修正阶段并重新整理补丁说明
5. 是否允许进入修正阶段：允许
"""

    if args.output_last_message:
        Path(args.output_last_message).write_text(content, encoding="utf-8")
    else:
        print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
