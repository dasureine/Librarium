"""无损合并脚本：把逐章精读笔记合并为整书笔记。

用法：
    python scripts/merge_notes.py <书名缩写> [--title "书名"]
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def natural_key(path):
    """按文件名开头的数字编号自然排序。"""
    name = path.name
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def main():
    parser = argparse.ArgumentParser(
        description="按编号顺序无损合并 workspace/<书名缩写>/chapter_notes/ 下的笔记到 notes/<书名缩写>.md"
    )
    parser.add_argument("name", help="书名缩写（对应 workspace/<书名缩写>/）")
    parser.add_argument("--title", help="可选：全书标题，写入合并文件开头")
    args = parser.parse_args()

    workspace = ROOT / "workspace" / args.name
    notes_dir = workspace / "chapter_notes"
    overview = workspace / "overview.md"

    if not notes_dir.is_dir():
        print(f"错误：找不到目录 {notes_dir}")
        sys.exit(1)

    chapter_files = sorted(notes_dir.glob("*.md"), key=natural_key)
    if not chapter_files and not overview.exists():
        print(f"错误：{notes_dir} 下没有 .md 笔记，且不存在 {overview}")
        sys.exit(1)

    parts = []
    if args.title:
        parts.append(args.title.strip())
    if overview.exists():
        parts.append(overview.read_text(encoding="utf-8-sig").rstrip("\n"))
    for f in chapter_files:
        parts.append(f.read_text(encoding="utf-8-sig").rstrip("\n"))

    content = "\n\n".join(p for p in parts if p) + "\n"

    out_dir = ROOT / "notes"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.name}.md"
    out_path.write_text(content, encoding="utf-8")

    overview_flag = " + overview.md" if overview.exists() else ""
    print(f"已合并 {len(chapter_files)} 章笔记{overview_flag}")
    print(f"输出文件：{out_path}")
    print(f"总字数：{len(content)}")


if __name__ == "__main__":
    main()
