"""通用拆章脚本：把大部头电子书按目录拆分为逐章纯文本。

用法：
    python scripts/split_book.py <电子书路径(.pdf或.epub)> [--name 英文书名缩写] [--level 目录层级] [--map 页码映射文件路径]

依赖：pymupdf（fitz），安装方式：pip install pymupdf
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GLOSSARY_HEADER = (
    "# 全书术语与概念表\n\n"
    "| 核心概念 | 章节出处 | 界定与要点 |\n"
    "| --- | --- | --- |\n"
)


def load_fitz():
    """优先使用新版 pymupdf，兼容旧版 fitz 别名。"""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz


def sanitize(value):
    """清理文件名中的非法字符。"""
    value = re.sub(r'[\\/:*?"<>|\r\n\t]', "", value)
    return value.strip(" .")


def to_filename(value):
    """把章节名转为安全文件名片段（空格转下划线）。"""
    value = sanitize(value)
    value = re.sub(r"\s+", "_", value)
    return value


def build_workspace(name):
    """在 workspace/<name>/ 下创建 chapters/、chapter_notes/ 及空 glossary.md。"""
    workspace = ROOT / "workspace" / name
    chapters_dir = workspace / "chapters"
    notes_dir = workspace / "chapter_notes"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    glossary = workspace / "glossary.md"
    if not glossary.exists():
        glossary.write_text(GLOSSARY_HEADER, encoding="utf-8")
    return workspace, chapters_dir


def filter_toc(toc, level):
    """按指定层级筛选目录条目；若无该层级，则回退到最浅层级。"""
    candidates = [e for e in toc if len(e) >= 3 and e[1] and str(e[1]).strip()]
    entries = [e for e in candidates if e[0] == level]
    if entries:
        return entries
    levels = sorted({e[0] for e in candidates})
    if not levels:
        return []
    min_level = levels[0]
    print(f"提示：目录中没有层级 {level} 的条目，已回退到层级 {min_level}。")
    return [e for e in candidates if e[0] == min_level]


def toc_to_spans(toc_entries, page_count):
    """把 [层级, 标题, 起始页] 转为 (标题, 起始页0基, 结束页0基开区间)。"""
    spans = []
    for i, entry in enumerate(toc_entries):
        title = entry[1]
        start = int(entry[2]) - 1
        if i + 1 < len(toc_entries):
            end = int(toc_entries[i + 1][2]) - 1
        else:
            end = page_count
        spans.append((title, start, end))
    return spans


def parse_map(path):
    """解析页码映射表，返回原始条目 [(标题, 起始页1基, 结束页1基或None)]。"""
    raw = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                page_part, title = line.split("|", 1)
                title = title.strip()
            else:
                page_part, title = line, ""
            page_part = page_part.strip()
            if "-" in page_part:
                a, b = page_part.split("-", 1)
                start = int(a.strip())
                end = int(b.strip())
            else:
                start = int(page_part)
                end = None
            raw.append({"title": title, "start": start, "end": end})

    entries = []
    for i, item in enumerate(raw):
        start = item["start"]
        end = item["end"]
        if end is None:
            end = raw[i + 1]["start"] if i + 1 < len(raw) else None
        entries.append((item["title"], start, end))
    return entries


def map_to_spans(entries, page_count):
    """把 map 条目转为 (标题, 起始页0基, 结束页0基开区间)。"""
    spans = []
    for title, start1, end1 in entries:
        start = max(0, start1 - 1)
        end = page_count if end1 is None else end1
        end = min(page_count, max(end, start + 1))
        spans.append((title, start, end))
    return spans


def generate_map_file(workspace, page_count):
    """生成示例页码映射表（用于无内嵌目录的电子书）。"""
    lines = [
        "# 示例页码映射表（请按实际章节填写）",
        "# 格式：起始页-结束页 | 章节名（页码从 1 起算，结束页含本页）",
        "# 填写完成后，用 --map 指向本文件重新运行拆分脚本。",
    ]
    if page_count <= 3:
        lines.append(f"1-{page_count} | 第一章")
    else:
        third = page_count // 3
        lines.append(f"1-{third} | 第一章")
        lines.append(f"{third + 1}-{2 * third} | 第二章")
        lines.append(f"{2 * third + 1}-{page_count} | 第三章")
    map_file = workspace / "pages_map.txt"
    map_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return map_file


def main():
    parser = argparse.ArgumentParser(
        description="把大部头电子书（.pdf / .epub）按目录拆分为逐章纯文本。"
    )
    parser.add_argument("book", help="电子书路径（.pdf 或 .epub）")
    parser.add_argument("--name", help="英文书名缩写（默认取文件名主干）")
    parser.add_argument("--level", type=int, default=1, help="目录层级，默认 1")
    parser.add_argument("--map", help="页码映射文件路径（用于无内嵌目录的电子书）")
    args = parser.parse_args()

    book_path = Path(args.book)
    if not book_path.exists():
        print(f"错误：找不到电子书文件 {book_path}")
        sys.exit(1)
    suffix = book_path.suffix.lower()
    if suffix not in (".pdf", ".epub"):
        print(f"错误：仅支持 .pdf 或 .epub，收到扩展名 {suffix}")
        sys.exit(1)

    name = to_filename(args.name) if args.name else to_filename(book_path.stem)
    if not name:
        print("错误：无法确定书名缩写，请用 --name 指定。")
        sys.exit(1)

    workspace, chapters_dir = build_workspace(name)

    fitz = load_fitz()
    doc = fitz.open(str(book_path))
    try:
        page_count = doc.page_count
        toc = doc.get_toc()

        spans = []
        if toc:
            toc_entries = filter_toc(toc, args.level)
            spans = toc_to_spans(toc_entries, page_count)

        if not spans:
            if args.map:
                map_path = Path(args.map)
                if not map_path.exists():
                    print(f"错误：找不到页码映射文件 {map_path}")
                    sys.exit(1)
                spans = map_to_spans(parse_map(map_path), page_count)
                print(f"未检测到内嵌目录，已按 --map 指定的页码映射拆分为 {len(spans)} 章。")
            else:
                map_file = generate_map_file(workspace, page_count)
                print("提示：该电子书没有内嵌目录（书签），且未提供 --map 参数。")
                print(f"已生成示例页码表：{map_file}")
                print("请按实际章节填写后，执行：")
                print(f'  python scripts/split_book.py "{book_path}" --name {name} --map "{map_file}"')
                return

        total_chars = 0
        total_pages = 0
        written = 0
        for idx, (title, start, end) in enumerate(spans, start=1):
            start = max(0, start)
            end = min(page_count, max(end, start + 1))
            if start >= page_count:
                continue
            parts = []
            for pno in range(start, end):
                parts.append(doc.load_page(pno).get_text())
            text = "\n".join(parts).strip()
            total_pages += end - start
            total_chars += len(text)
            filename = f"{idx:02d}_{to_filename(title) or '未命名章节'}.txt"
            (chapters_dir / filename).write_text(text, encoding="utf-8")
            written += 1

        if written == 0:
            print("警告：未能拆分出任何章节，请检查目录层级（--level）或页码映射（--map）。")
        else:
            print(f"已拆分 {written} 章，输出目录：{chapters_dir}")
            avg = total_chars / total_pages if total_pages else 0
            if avg < 30:
                print("警告：检测到疑似纯图片扫描版 PDF，需先 OCR 转换后再拆分。")
    finally:
        doc.close()


if __name__ == "__main__":
    main()
