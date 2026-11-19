#!/usr/bin/env python3
"""从 CHANGELOG.md 里抽出某个版本的段落，作为 GitHub Release 的正文。

用法：
    scripts/release_notes.py v0.1.1

为什么不用 `git tag -l --format`：Release 正文的价值在于"改了什么 + 跑了什么 + 没试什么"，
这三样只有 CHANGELOG 里有；tag message 通常是空的或随手写的。
找不到对应版本段落时，退回 [Unreleased] 并明确标注 —— 宁可写出"这是未整理版本"，
也不要在 Release 页面上假装有内容。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"


def section(text: str, heading: str) -> str | None:
    """抓取 `## [heading]` 到下一个 `## ` 之间的内容。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^##\s*\[?{re.escape(heading)}\]?", line.strip()):
            start = i + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip() or None


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: release_notes.py v0.1.1", file=sys.stderr)
        return 2
    tag = sys.argv[1].lstrip("vV")
    text = CHANGELOG.read_text(encoding="utf-8")

    body = section(text, tag)
    if body:
        print(body)
        return 0

    unreleased = section(text, "Unreleased")
    print(f"> ⚠️ CHANGELOG 里没有 `{tag}` 的独立段落，下面取自 `[Unreleased]`。")
    print("> 发版前请把该段落的标题改成 `## [" + tag + "] - YYYY-MM-DD`。")
    print()
    print(unreleased or "（CHANGELOG 无内容）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
