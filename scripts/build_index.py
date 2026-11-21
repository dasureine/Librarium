# -*- coding: utf-8 -*-
"""Librarium 学科分类索引。

仅按 books.json 中标准化的「一级学科/二级学科」标签，生成干净的两级树
data/taxonomy.json：一级学科为父分组，二级学科嵌套其下；每个二级学科直接列出
对应书目与具体分章条目（整书未完结时列分章），不再抓取零散节点概念。

用法：
    python scripts/build_index.py

本脚本零依赖、无网络调用，仅本地读取 books.json，绝不触碰 notes/*.md。
"""

import json
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOKS_JSON = ROOT / "books.json"
DATA_DIR = ROOT / "data"
TAXONOMY_PATH = DATA_DIR / "taxonomy.json"

def split_tag(tag: str) -> tuple:
    """把「一级学科/二级学科」拆成两级；无斜杠时二级兜底为「综合」。"""
    tag = str(tag or "").strip()
    if "/" in tag:
        primary, _, secondary = tag.partition("/")
        primary, secondary = primary.strip(), secondary.strip()
        if primary and secondary:
            return primary, secondary
    return (tag or "其他"), "综合"

CN_NUM = "零一二三四五六七八九"


def cn_num(n: int) -> str:
    if n <= 0:
        return str(n)
    if n <= 10:
        return CN_NUM[n]
    if n < 20:
        return "十" + (CN_NUM[n % 10] if n % 10 else "")
    tens, ones = divmod(n, 10)
    return CN_NUM[tens] + "十" + (CN_NUM[ones] if ones else "")


# 已移除旧版节点概念抓取：仅按 books.json 的「一级学科/二级学科」生成两级学科树。


def chapter_scope(chapter: dict) -> str:
    title = str(chapter.get("title") or "").strip()
    if title:
        first = title.split(" ", 1)[0]
        if first:
            return first
    order = chapter.get("order")
    if isinstance(order, int) and order > 0:
        return f"第{cn_num(order)}章"
    return "分章"


def add_reference(taxonomy: OrderedDict, tag: str, ref: dict) -> None:
    """把一条书目 / 分章引用挂到对应「一级学科/二级学科」的 references 下。"""
    primary, secondary = split_tag(tag)
    if not primary or not secondary:
        return
    taxonomy.setdefault(primary, OrderedDict()).setdefault(
        secondary, OrderedDict({"references": []})
    )["references"].append(ref)


def rebuild_index() -> Path:
    """扫描 books.json，重建两级学科树 data/taxonomy.json。"""
    if not BOOKS_JSON.exists():
        raise RuntimeError(f"未找到 {BOOKS_JSON}")

    books = json.loads(BOOKS_JSON.read_text(encoding="utf-8"))
    if not isinstance(books, list):
        books = []

    taxonomy = OrderedDict()
    for book in books:
        if not isinstance(book, dict):
            continue
        book_title = str(book.get("title") or "").strip()
        tags = book.get("tags") or []
        whole_rel = str(book.get("url") or book.get("mapPath") or "").lstrip("./").strip()
        chapters = book.get("chapters") or []

        for tag in tags:
            if not str(tag or "").strip():
                continue
            if whole_rel:
                add_reference(taxonomy, tag, {
                    "bookTitle": book_title,
                    "scope": "整书总图",
                    "title": book_title,
                    "targetUrl": whole_rel,
                    "summary": str(book.get("summary") or "").strip(),
                })
            else:
                for chapter in chapters:
                    if not isinstance(chapter, dict):
                        continue
                    ch_rel = str(chapter.get("mapPath") or "").lstrip("./").strip()
                    if not ch_rel:
                        continue
                    add_reference(taxonomy, tag, {
                        "bookTitle": book_title,
                        "scope": chapter_scope(chapter),
                        "title": str(chapter.get("title") or "").strip() or chapter_scope(chapter),
                        "targetUrl": ch_rel,
                        "summary": str(chapter.get("summary") or "").strip(),
                    })

    # 稳定输出：一级学科与二级学科均按字典序排列。
    ordered = OrderedDict()
    for primary in sorted(taxonomy.keys()):
        ordered[primary] = OrderedDict(sorted(taxonomy[primary].items()))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TAXONOMY_PATH.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return TAXONOMY_PATH


if __name__ == "__main__":
    out = rebuild_index()
    data = json.loads(out.read_text(encoding="utf-8"))
    total_secondary = sum(len(secs) for secs in data.values())
    total_refs = sum(
        len(refs["references"]) for secs in data.values() for refs in secs.values()
    )
    print(f"[build_index] 已生成 {out}")
    print(f"[build_index] 一级学科 {len(data)} 个 / 二级学科 {total_secondary} 个 / 引用 {total_refs} 条")

