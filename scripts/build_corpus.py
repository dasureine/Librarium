# -*- coding: utf-8 -*-
"""Librarium 统一语料索引构建脚本（阶段 0 · 数据地基）。

把 workspace/<书名>/ 下的章节原文、术语表、精读笔记，统一编成可检索的
data/corpus_index.json：

  · 每章原文切分为「片段」（按句号边界聚合约 400 字），片段是引用与检索的最小单元；
  · 片段 ID 稳定为 {slug}:{章号}:{片段序号}，跨 PDF / EPUB 通用，不依赖页码；
  · file_page（文件页）仅当原文含 [[p.N]] 页码锚点（经 server 上传拆分）时才写入；
    printed_page = file_page - page_offset（page_offset 存于 book_meta.json，默认 0）；
  · 术语表（glossary.json）与精读笔记（chapter_notes/*.md）作为两类附加可检索文档入库。

零依赖、无网络调用，仅本地读取 workspace/ 与 books.json，绝不触碰 notes/*.md。

用法：
    python scripts/build_corpus.py [--book 书名缩写] [--min-segment 字]
"""

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace"
BOOKS_JSON = ROOT / "books.json"
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "corpus_index.json"

BEIJING = timezone(timedelta(hours=8))

# 片段目标字数：按句号边界聚合，单个片段约为该值上下。
MIN_SEGMENT = 400

# 页码锚点：[[p.N]]（server 上传拆分路径写入）。
PAGE_ANCHOR = re.compile(r"\[\[p\.(\d+)\]\]")

# 页脚噪声：形如「西方哲学简史| 2」。
FOOTER = re.compile(r"[|｜]\s*\d{1,4}\s*$")

# 纯章号行：形如「第一章」（作为页眉单独出现）。
CHAPTER_NUM_ONLY = re.compile(r"^第\s*[0-9一二三四五六七八九十百千零〇两]+\s*章$")


def natural_key(path: Path):
    """按文件名开头的数字编号自然排序。"""
    name = path.name
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def load_book_index() -> dict:
    """从 books.json 建立 slug -> {title, author} 映射。"""
    mapping = {}
    if BOOKS_JSON.exists():
        try:
            data = json.loads(BOOKS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for b in data:
                    if isinstance(b, dict) and b.get("id"):
                        mapping[b["id"]] = {
                            "title": str(b.get("title") or b["id"]),
                            "author": str(b.get("author") or ""),
                        }
        except Exception:
            pass
    return mapping


def load_book_meta(workspace: Path) -> dict:
    """读取/初始化 workspace/<slug>/book_meta.json（含 page_offset）。"""
    p = workspace / "book_meta.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    meta = {"page_offset": 0}
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def normalize_text(text: str) -> str:
    """清洗章节原文：去页眉页脚噪声，合并硬换行，折叠空白为单空格。"""
    kept = []
    for line in str(text).splitlines():
        s = line.strip()
        if not s:
            continue
        if FOOTER.search(s):
            continue
        if CHAPTER_NUM_ONLY.match(s):
            continue
        kept.append(s)
    joined = "".join(kept)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined


def split_sentences(text: str) -> list:
    """按中文句末标点切分，保留标点。"""
    return [s for s in re.findall(r"[^。！？；!?;]*[。！？；!?;]|[^。！？；!?;]+$", text) if s.strip()]


def build_segments(text: str, min_segment: int) -> list:
    """把正文按句号边界聚合为约 min_segment 字的片段。"""
    segments = []
    buf = ""
    for s in split_sentences(text):
        buf += s
        if len(buf) >= min_segment:
            segments.append(buf)
            buf = ""
    if buf.strip():
        segments.append(buf)
    return segments


def split_by_pages(text: str) -> list:
    """按 [[p.N]] 锚点切分；无锚点时返回 [(None, text)]。"""
    if not PAGE_ANCHOR.search(text):
        return [(None, text)]
    blocks = []
    cur_page = None
    cur_buf = []
    for line in str(text).splitlines():
        m = PAGE_ANCHOR.match(line.strip())
        if m:
            if cur_page is not None:
                blocks.append((cur_page, "\n".join(cur_buf)))
            cur_page = int(m.group(1))
            cur_buf = []
        else:
            cur_buf.append(line)
    if cur_page is not None:
        blocks.append((cur_page, "\n".join(cur_buf)))
    return blocks


def parse_chapter_filename(name: str):
    """从 'ch01.txt' 或旧式 '01_第一章.txt' 解析 (章号, 章名)。"""
    stem = Path(name).stem
    m = re.match(r"^(\d+)_(.*)$", stem)
    if m:
        return int(m.group(1)), m.group(2).replace("_", " ").strip()
    m = re.match(r"^ch(\d+)$", stem, re.IGNORECASE)
    if m:
        return int(m.group(1)), ""
    return 0, stem


def chapter_titles(chapters_dir: Path) -> dict:
    """读取 chapters/manifest.json，返回 {章号: 标题}。"""
    try:
        import split_book

        manifest = split_book.load_chapter_manifest(chapters_dir)
    except Exception:
        return {}
    titles = {}
    for order, entry in manifest.items():
        title = str((entry or {}).get("title") or "").strip()
        if title:
            titles[int(order)] = title
    return titles


def chapter_segments(slug: str, chapter_index: int, text: str, page_offset: int,
                     min_segment: int) -> list:
    """把一章原文切为带元数据的片段列表。"""
    segments = []
    seq = 0
    for file_page, block in split_by_pages(text):
        for frag in build_segments(normalize_text(block), min_segment):
            seq += 1
            printed = (file_page - page_offset) if file_page is not None else None
            segments.append({
                "id": f"{slug}:{chapter_index}:{seq}",
                "seq": seq,
                "text": frag,
                "chars": len(frag),
                "file_page": file_page,
                "printed_page": printed,
            })
    return segments


def load_terms(workspace: Path) -> list:
    p = workspace / "glossary.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [t for t in data if isinstance(t, dict)]
        except Exception:
            pass
    return []


def load_notes(workspace: Path) -> list:
    notes_dir = workspace / "chapter_notes"
    notes = []
    if notes_dir.is_dir():
        titles = chapter_titles(workspace / "chapters")
        for f in sorted(notes_dir.glob("*.md"), key=natural_key):
            index, title = parse_chapter_filename(f.name)
            title = title or titles.get(index) or f"第{index}章"
            notes.append({
                "chapter_index": index,
                "title": title,
                "text": f.read_text(encoding="utf-8-sig").strip(),
            })
    return notes


def iter_book_workspaces():
    """扫描 workspace/ 下含 chapters/ 的书目目录。"""
    if not WORKSPACE.is_dir():
        return
    for d in sorted(WORKSPACE.iterdir(), key=lambda p: natural_key(p)):
        if d.is_dir() and (d / "chapters").is_dir():
            yield d


def build_corpus(only_slug: str = None, min_segment: int = MIN_SEGMENT) -> dict:
    book_meta = load_book_index()
    books = []
    for workspace in iter_book_workspaces():
        slug = workspace.name
        if only_slug and slug != only_slug:
            continue
        chapters_dir = workspace / "chapters"
        info = book_meta.get(slug, {})
        meta = load_book_meta(workspace)
        page_offset = int(meta.get("page_offset", 0) or 0)

        chapters = []
        anchor_count = 0
        titles = chapter_titles(chapters_dir)
        for f in sorted(chapters_dir.glob("*.txt"), key=natural_key):
            index, title = parse_chapter_filename(f.name)
            title = title or titles.get(index) or f"第{index}章"
            text = f.read_text(encoding="utf-8")
            has_anchor = bool(PAGE_ANCHOR.search(text))
            if has_anchor:
                anchor_count += 1
            segments = chapter_segments(slug, index, text, page_offset, min_segment)
            chapters.append({
                "index": index,
                "title": title or f"第{index}章",
                "segments": segments,
                "page_source": "anchor" if has_anchor else "none",
            })

        if not chapters:
            page_source = "none"
        elif anchor_count == len(chapters):
            page_source = "anchor"
        elif anchor_count == 0:
            page_source = "none"
        else:
            page_source = "mixed"

        books.append({
            "slug": slug,
            "title": info.get("title", slug),
            "author": info.get("author", ""),
            "page_offset": page_offset,
            "page_source": page_source,
            "chapters": chapters,
            "terms": load_terms(workspace),
            "notes": load_notes(workspace),
        })

    return {
        "version": 1,
        "built_at": datetime.now(BEIJING).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "books": books,
    }


def main():
    parser = argparse.ArgumentParser(description="构建 Librarium 统一语料索引。")
    parser.add_argument("--book", help="只构建指定书名缩写（对应 workspace/<缩写>/）")
    parser.add_argument("--min-segment", type=int, default=MIN_SEGMENT,
                        help=f"片段目标字数，默认 {MIN_SEGMENT}")
    args = parser.parse_args()

    corpus = build_corpus(only_slug=args.book, min_segment=args.min_segment)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8")

    total_books = len(corpus["books"])
    total_chapters = sum(len(b["chapters"]) for b in corpus["books"])
    total_segments = sum(
        len(c["segments"]) for b in corpus["books"] for c in b["chapters"]
    )
    total_terms = sum(len(b["terms"]) for b in corpus["books"])
    total_notes = sum(len(b["notes"]) for b in corpus["books"])

    print(f"[build_corpus] 已生成 {OUT_PATH}")
    print(f"[build_corpus] 书目 {total_books} 本 / 章节 {total_chapters} 章 / "
          f"片段 {total_segments} 个 / 术语 {total_terms} 条 / 笔记 {total_notes} 篇")
    for b in corpus["books"]:
        seg_count = sum(len(c["segments"]) for c in b["chapters"])
        print(f"  · {b['title']}（{b['slug']}）: {len(b['chapters'])} 章, "
              f"{seg_count} 片段, page_source={b['page_source']}")


if __name__ == "__main__":
    main()
