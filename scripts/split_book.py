"""通用拆章脚本：把大部头电子书按目录拆分为逐章纯文本。

用法：
    python scripts/split_book.py <电子书路径(.pdf或.epub)> [--name 英文书名缩写] [--level 目录层级] [--map 页码映射文件路径]

依赖：pymupdf（fitz），安装方式：pip install pymupdf
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GLOSSARY_HEADER = (
    "# 全书术语与概念表\n\n"
    "| 核心概念 | 章节出处 | 界定与要点 |\n"
    "| --- | --- | --- |\n"
)

# ===== 格式与目录识别常量 =====
SUPPORTED_DOC_EXTS = (".pdf", ".epub", ".txt")
SUPPORTED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
SUPPORTED_EXTS = SUPPORTED_DOC_EXTS + SUPPORTED_IMAGE_EXTS

# 平均每页字符数低于该值即视为纯图片扫描版（无文字层）。
MIN_CHARS_PER_PAGE = 30
# TXT 章标题之间的最小行距：低于该值说明是密集的目录页条目，而非真实章标题。
TXT_HEADING_MIN_GAP = 4

FORMAT_LABELS = {
    "pdf": "PDF 文档",
    "epub": "EPUB 电子书",
    "txt": "TXT 纯文本",
    "image": "图片",
}

# 非正文条目：版权页、目录页、前言序言、参考文献、索引等。
NONCONTENT_TITLE_RE = re.compile(
    r"^(?:"
    r"版权(?:信息|页|声明|所有)?|封面|扉页|书名页|目录|出版说明|出版信息|"
    r"内容简介|内容提要|印刷信息|"
    r"(?:修订版|新版|增订版|再版|第[一二三四五六七八九十\d]+版)?前言|"
    r"(?:自|译|编|代|总|原)?序(?:言|一|二|三)?|"
    r"后记|跋|致谢|"
    r"(?:主要)?参考文献|引用文献|索引|"
    r"copyright|contents|table\s*of\s*contents|title\s*page|preface|foreword|"
    r"acknowledg(?:e)?ments?|index|bibliography|references"
    r")$",
    re.IGNORECASE,
)

# TXT 中可识别的章标题：第一章 / 第1章 / Chapter 1 / 卷一 等。
TXT_HEADING_RE = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百千零〇两]+\s*[章节回卷篇部]"
    r"|Chapter\s+[0-9IVXLCivxlc]+"
    r"|CHAPTER\s+[0-9IVXLCivxlc]+"
    r"|卷\s*[0-9一二三四五六七八九十]+)"
    r"[\s:：、.\-—·]*\S{0,40}$"
)


def detect_format(path):
    """按扩展名返回 pdf / epub / txt / image。"""
    ext = Path(path).suffix.lower()
    if ext in SUPPORTED_IMAGE_EXTS:
        return "image"
    return ext.lstrip(".")


def detect_format_label(fmt):
    return FORMAT_LABELS.get(fmt, str(fmt or "").upper())


def normalize_title(title):
    """去掉标题中的空白与首尾标点，供非正文条目判别。"""
    text = re.sub(r"[\s\u3000]+", "", str(title or ""))
    return text.strip("：:·—–-.、，,（）()《》[]【】\"'“”‘’")


def is_noncontent_title(title):
    """判断目录条目是否属于版权页、目录页、前言、参考文献等非正文。"""
    return bool(NONCONTENT_TITLE_RE.match(normalize_title(title)))


def read_text_file(path):
    """解码 TXT：优先 BOM，其次 UTF-8，最后 GB18030 兜底。"""
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def detect_txt_headings(text):
    """返回 TXT 中的章标题 [(行号, 标题)]。

    先剔除连续多行紧挨着的「目录页」条目（真实章标题之间通常隔着正文），
    再要求相邻章标题至少间隔 TXT_HEADING_MIN_GAP 行，避免把目录当成章节。
    """
    lines = str(text or "").splitlines()
    raw = []
    for i, line in enumerate(lines):
        title = line.strip()
        if not title or len(title) > 46:
            continue
        if TXT_HEADING_RE.match(title):
            raw.append((i, title))
    if not raw:
        return []

    # 连续（行距 <= 2）出现的 2 条及以上标题视为目录页条目，整段丢弃。
    dropped = set()
    i = 0
    while i < len(raw):
        j = i
        while j + 1 < len(raw) and raw[j + 1][0] - raw[j][0] <= 2:
            j += 1
        if j > i:
            dropped.update(range(i, j + 1))
        i = j + 1

    heads = []
    kept = [item for idx, item in enumerate(raw) if idx not in dropped]
    for i, (line_no, title) in enumerate(kept):
        if i + 1 < len(kept):
            if kept[i + 1][0] - line_no < TXT_HEADING_MIN_GAP:
                continue
        elif not any(rest.strip() for rest in lines[line_no + 1:]):
            # 末尾标题之后没有任何正文，视为页面页脚/目录残行。
            continue
        heads.append((line_no, title))
    return heads


def txt_spans(text, skip_noncontent=True):
    """把带章标题的 TXT 转为 (标题, 起始行0基, 结束行0基) 列表。"""
    lines = str(text or "").splitlines()
    heads = detect_txt_headings(text)
    if not heads:
        return [], []
    spans = []
    if heads[0][0] > 0:
        prelude = "\n".join(lines[: heads[0][0]]).strip()
        if len(prelude) >= 200:
            spans.append(("卷首", 0, heads[0][0]))
    for i, (line_no, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(lines)
        spans.append((title, line_no, end))
    return filter_noncontent_spans(spans, skip_noncontent)


def filter_noncontent_spans(spans, skip_noncontent=True):
    """剔除非正文条目；若剔除后为空则恢复全部，避免整书丢失。"""
    if not skip_noncontent:
        return list(spans), []
    kept, skipped = [], []
    for title, start, end in spans:
        if is_noncontent_title(title):
            skipped.append(str(title).strip())
        else:
            kept.append((title, start, end))
    if not kept:
        return list(spans), []
    return kept, skipped


def sample_avg_chars(doc, page_count, sample_size=12):
    """对前中后均匀抽样页面，返回平均每页字符数（用于快速判定扫描版）。"""
    if page_count <= 0:
        return 0.0
    if page_count <= sample_size:
        pages = list(range(page_count))
    else:
        step = (page_count - 1) / (sample_size - 1)
        pages = sorted({round(step * i) for i in range(sample_size)})
    chars = 0
    for pno in pages:
        try:
            chars += len((doc.load_page(pno).get_text() or "").strip())
        except Exception:
            continue
    return chars / max(1, len(pages))


def inspect_book(book_path):
    """识别电子书格式、目录与文字层，决定后续走自动拆书还是手动目录。

    返回字段：
        format / format_label / page_count / total_units / unit
        toc_total / toc_count / toc_titles / toc_source
        text_layer / avg_chars_per_page / scanned / route
    route 取值：
        auto_split  带目录且有文字层，可直接拆书；
        manual_toc  有文字层但无目录，需要手动填写目录映射；
        need_ocr    扫描版/图片，无文字层，需要先 OCR。
    """
    path = Path(book_path)
    fmt = detect_format(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    info = {
        "filename": path.name,
        "format": fmt,
        "format_label": detect_format_label(fmt),
        "file_size": size,
        "page_count": 0,
        "total_units": 0,
        "unit": "页",
        "toc_total": 0,
        "toc_count": 0,
        "toc_titles": [],
        "toc_source": "none",
        "has_toc": False,
        "text_layer": False,
        "avg_chars_per_page": 0.0,
        "scanned": False,
        "route": "manual_toc",
    }

    if fmt == "txt":
        text = read_text_file(path)
        lines = text.splitlines()
        heads = detect_txt_headings(text)
        titles = [title for _line, title in heads]
        usable, _skipped = filter_noncontent_spans(
            [(t, 0, 0) for t in titles], True
        )
        info.update(
            page_count=0,
            total_units=len(lines),
            unit="行",
            toc_total=len(titles),
            toc_count=len(usable),
            toc_titles=[t for t, _s, _e in usable][:12],
            toc_source="headings" if usable else "none",
            has_toc=bool(usable),
            text_layer=bool(text.strip()),
            avg_chars_per_page=round(len(text) / max(1, len(lines)), 1),
            scanned=not bool(text.strip()),
        )
        if not info["text_layer"]:
            info["route"] = "need_ocr"
        elif info["has_toc"]:
            info["route"] = "auto_split"
        else:
            info["route"] = "manual_toc"
        return info

    if fmt == "image":
        info.update(
            format_label="图片（需 OCR）",
            page_count=1,
            total_units=1,
            scanned=True,
            route="need_ocr",
        )
        return info

    fitz = load_fitz()
    doc = fitz.open(str(path))
    try:
        page_count = doc.page_count
        toc = doc.get_toc() or []
        entries = filter_toc(toc, 1) if toc else []
        titles = [str(e[1]).strip() for e in entries if str(e[1]).strip()]
        usable, _skipped = filter_noncontent_spans([(t, 0, 0) for t in titles], True)
        avg = sample_avg_chars(doc, page_count)
        has_text = avg >= MIN_CHARS_PER_PAGE
        info.update(
            page_count=page_count,
            total_units=page_count,
            toc_total=len(titles),
            toc_count=len(usable),
            toc_titles=[t for t, _s, _e in usable][:12],
            toc_source="embedded" if usable else "none",
            has_toc=bool(usable),
            text_layer=has_text,
            avg_chars_per_page=round(avg, 1),
            scanned=not has_text,
        )
        if not has_text:
            info["route"] = "need_ocr"
        elif usable:
            info["route"] = "auto_split"
        else:
            info["route"] = "manual_toc"
    finally:
        doc.close()
    return info


def archive_chapters(chapters_dir, keep=3):
    """把上一次切分的 txt 归档到 _chapters_prev/，保证每次切分目录干净。"""
    chapters_dir = Path(chapters_dir)
    if not chapters_dir.is_dir():
        return None
    files = [p for p in chapters_dir.glob("*.txt") if p.is_file()]
    if not files:
        return None
    root = chapters_dir.parent / "_chapters_prev"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = root / stamp
    n = 1
    while target.exists():
        target = root / f"{stamp}-{n}"
        n += 1
    target.mkdir(parents=True)
    for f in files:
        shutil.move(str(f), str(target / f.name))
    archives = sorted(
        [d for d in root.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
    )
    for old in archives[:-keep] if keep > 0 else archives:
        shutil.rmtree(old, ignore_errors=True)
    return target


def chapter_filename(order):
    """统一分章文件名：只按章号命名，标题不再进入文件名。"""
    return f"ch{int(order):02d}.txt"


def chapter_hash(title, text):
    """章节身份指纹：标题 + 正文，用于换源后判断笔记与图谱是否过期。"""
    payload = f"{str(title or '').strip()}\n{text or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


CHAPTER_MANIFEST = "manifest.json"


def load_chapter_manifest(chapters_dir):
    """读取 chapters/manifest.json，返回 {order: entry}。"""
    path = Path(chapters_dir) / CHAPTER_MANIFEST
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = data.get("chapters") if isinstance(data, dict) else data
    result = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            order = int(entry.get("order"))
        except (TypeError, ValueError):
            continue
        result[order] = entry
    return result


def save_chapter_manifest(chapters_dir, entries, source=""):
    """写回 chapters/manifest.json（按章号排序，标题与指纹都在这里）。"""
    chapters_dir = Path(chapters_dir)
    chapters_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source": source or "",
        "chapters": [entries[order] for order in sorted(entries)],
    }
    path = chapters_dir / CHAPTER_MANIFEST
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _resolve_orders(spans, order_resolver, offset):
    """确定每章最终章号：优先用调用方的重复章节匹配结果，否则按目录顺序编号。"""
    titles = [str(title).strip() or "未命名章节" for title, _s, _e in spans]
    if order_resolver is not None:
        orders = [max(1, int(order)) for order in order_resolver(titles)]
        if len(orders) != len(titles):
            raise ValueError("order_resolver 返回的章号数量与目录条目不一致")
    else:
        orders = [index + offset for index in range(1, len(titles) + 1)]
    if len(set(orders)) != len(orders):
        raise ValueError("order_resolver 返回了重复章号")
    return titles, orders


def _write_chapter(out_dir, order, title, text, chars, pages, manifest):
    """按章号写入分章文件并更新 manifest 条目。"""
    filename = chapter_filename(order)
    (out_dir / filename).write_text(text, encoding="utf-8")
    manifest[int(order)] = {
        "order": int(order),
        "title": title,
        "filename": filename,
        "chars": int(chars),
        "pages": int(pages),
        "hash": chapter_hash(title, text),
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return {
        "index": int(order),
        "filename": filename,
        "title": title,
        "chars": int(chars),
        "pages": int(pages),
    }


def split_into_chapters(book_path, out_dir, level=1, offset=0,
                        skip_noncontent=True, map_path=None, order_resolver=None):
    """通用拆章入口：把 PDF / EPUB / TXT 按目录拆成逐章 txt。

    分章文件统一按章号命名：chapters/ch01.txt、ch02.txt……章节标题与内容指纹
    写入 chapters/manifest.json。换源重传时同名文件原位覆盖，标题变化也不会
    产生重复章节文件。

    order_resolver(titles) -> [order, ...]：由调用方按已入藏章节计算最终章号，
    实现「重复章节原位替换、新章节顺延追加」；不传时按目录顺序 + offset 编号。

    返回 dict：
        status     ready / needs_manual_map / scanned / empty
        chapters   [{index, filename, title, chars, pages}]
        unit / total_units / skipped / inspection / avg_chars
    """
    path = Path(book_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = inspect_book(path)
    fmt = info["format"]
    offset = max(0, int(offset or 0))
    skipped = []
    spans = []
    unit = info["unit"]
    total_units = info["total_units"]

    if fmt == "txt":
        text = read_text_file(path)
        lines = text.splitlines()
        total_units = len(lines)
        unit = "行"
        spans, skipped = txt_spans(text, skip_noncontent=skip_noncontent)
        if not spans and map_path:
            spans = map_to_spans(parse_map(map_path), total_units)
        if not spans:
            return {
                "status": "needs_manual_map",
                "chapters": [],
                "unit": unit,
                "total_units": total_units,
                "skipped": skipped,
                "inspection": info,
                "message": "该 TXT 未识别到章标题，请按「起始行-结束行 | 章节名」填写目录映射。",
            }
        if not text.strip():
            return {
                "status": "scanned",
                "chapters": [],
                "inspection": info,
                "message": "该 TXT 内容为空，无法拆章。",
            }
        titles, orders = _resolve_orders(spans, order_resolver, offset)
        manifest = load_chapter_manifest(out_dir)
        chapters = []
        total_chars = 0
        for (_raw_title, start, end), title, order in zip(spans, titles, orders):
            start = max(0, start)
            end = min(total_units, max(end, start + 1))
            if start >= total_units:
                continue
            part = "\n".join(lines[start:end]).strip()
            total_chars += len(part)
            chapters.append(
                _write_chapter(out_dir, order, title, part, len(part), end - start, manifest)
            )
        save_chapter_manifest(out_dir, manifest, source=str(path))
        chapters.sort(key=lambda item: item["index"])
        if not chapters:
            return {"status": "empty", "chapters": [], "inspection": info,
                    "message": "未能拆分出任何章节，请检查目录映射。"}
        return {
            "status": "ready",
            "chapters": chapters,
            "unit": unit,
            "total_units": total_units,
            "skipped": skipped,
            "inspection": info,
            "avg_chars": None,
        }

    fitz = load_fitz()
    doc = fitz.open(str(path))
    try:
        total_units = doc.page_count
        unit = "页"
        toc = doc.get_toc() or []
        if toc:
            entries = filter_toc(toc, level)
            all_spans = toc_to_spans(entries, total_units)
            kept, skipped = filter_noncontent_spans(all_spans, skip_noncontent)
            spans = [(str(t).strip(), s, e) for t, s, e in kept]
        if not spans and map_path:
            spans = map_to_spans(parse_map(map_path), total_units)
        if not spans:
            return {
                "status": "needs_manual_map",
                "chapters": [],
                "unit": unit,
                "total_units": total_units,
                "skipped": skipped,
                "inspection": info,
                "message": "该电子书没有可用目录，请按「起始页-结束页 | 章节名」填写目录映射。",
            }

        avg_sample = sample_avg_chars(doc, total_units)
        if avg_sample < MIN_CHARS_PER_PAGE:
            return {
                "status": "scanned",
                "chapters": [],
                "inspection": info,
                "message": (
                    f"检测到疑似纯图片扫描版（抽样平均每页 {avg_sample:.1f} 字，低于 "
                    f"{MIN_CHARS_PER_PAGE} 字），请先完成 OCR 转换为文字层 PDF 或 TXT。"
                ),
            }

        titles, orders = _resolve_orders(spans, order_resolver, offset)
        manifest = load_chapter_manifest(out_dir)
        chapters = []
        total_chars = 0
        total_pages = 0
        for (_raw_title, start, end), title, order in zip(spans, titles, orders):
            start = max(0, start)
            end = min(total_units, max(end, start + 1))
            if start >= total_units:
                continue
            # 逐页写入轻量页码锚点 [[p.N]]，供 Pass 1 精读做页码溯源。
            parts = []
            raw_chars = 0
            for pno in range(start, end):
                page_text = doc.load_page(pno).get_text() or ""
                raw_chars += len(page_text)
                parts.append(f"[[p.{pno + 1}]]\n{page_text}")
            text = "\n".join(parts).strip()
            total_pages += end - start
            total_chars += raw_chars
            chapters.append(
                _write_chapter(out_dir, order, title, text, raw_chars, end - start, manifest)
            )
        save_chapter_manifest(out_dir, manifest, source=str(path))
        chapters.sort(key=lambda item: item["index"])

        if not chapters:
            return {"status": "empty", "chapters": [], "inspection": info,
                    "message": "未能拆分出任何章节，请检查目录层级或目录映射。"}
        avg = total_chars / total_pages if total_pages else 0
        return {
            "status": "ready",
            "chapters": chapters,
            "unit": unit,
            "total_units": total_units,
            "skipped": skipped,
            "inspection": info,
            "avg_chars": round(avg, 1),
        }
    finally:
        doc.close()


def images_to_pdf(image_paths, dest_path):
    """把多张扫描图片按顺序合并为一份 PDF（每张图一页），供手动目录切分。"""
    fitz = load_fitz()
    out = fitz.open()
    try:
        for img_path in image_paths:
            src = fitz.open(str(img_path))
            try:
                pdf_bytes = src.convert_to_pdf()
            finally:
                src.close()
            pdf_doc = fitz.open("pdf", pdf_bytes)
            try:
                out.insert_pdf(pdf_doc)
            finally:
                pdf_doc.close()
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(dest_path))
    finally:
        out.close()
    return Path(dest_path)


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


def generate_map_file(workspace, total_units, unit="页"):
    """写一份目录映射模板（全部为注释，避免未编辑就被当成真实目录）。"""
    lines = [
        "# 目录映射模板（请按实际章节填写后删除行首的 #）",
        f"# 格式：起始{unit}-结束{unit} | 章节名（{unit}码从 1 起算，结束{unit}含本{unit}）",
        f"# 本书共 {total_units} {unit}。",
        "#",
        f"# 1-40 | 第一章",
        f"# 41-80 | 第二章",
    ]
    map_file = Path(workspace) / "pages_map.txt"
    map_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return map_file


def main():
    parser = argparse.ArgumentParser(
        description="把大部头电子书（.pdf / .epub / .txt）按目录拆分为逐章纯文本。"
    )
    parser.add_argument("book", help="电子书路径（.pdf / .epub / .txt）")
    parser.add_argument("--name", help="英文书名缩写（默认取文件名主干）")
    parser.add_argument("--level", type=int, default=1, help="目录层级，默认 1")
    parser.add_argument("--map", help="目录映射文件路径（用于无内嵌目录的电子书）")
    parser.add_argument("--keep-noncontent", action="store_true",
                        help="保留版权页 / 目录页 / 前言 / 参考文献等非正文条目")
    args = parser.parse_args()

    book_path = Path(args.book)
    if not book_path.exists():
        print(f"错误：找不到电子书文件 {book_path}")
        sys.exit(1)
    suffix = book_path.suffix.lower()
    if suffix not in SUPPORTED_DOC_EXTS:
        print(f"错误：仅支持 {' / '.join(SUPPORTED_DOC_EXTS)}，收到扩展名 {suffix}")
        sys.exit(1)

    name = to_filename(args.name) if args.name else to_filename(book_path.stem)
    if not name:
        print("错误：无法确定书名缩写，请用 --name 指定。")
        sys.exit(1)

    workspace, chapters_dir = build_workspace(name)
    info = inspect_book(book_path)
    print(
        f"识别结果：{info['format_label']} · "
        f"{'内嵌目录 ' + str(info['toc_count']) + ' 条' if info['has_toc'] else '无可用目录'} · "
        f"{'文字层正常' if info['text_layer'] else '无文字层'}"
    )

    map_path = None
    if args.map:
        map_path = Path(args.map)
        if not map_path.exists():
            print(f"错误：找不到目录映射文件 {map_path}")
            sys.exit(1)

    result = split_into_chapters(
        book_path,
        chapters_dir,
        level=args.level,
        skip_noncontent=not args.keep_noncontent,
        map_path=map_path,
    )
    status = result["status"]
    if status == "needs_manual_map":
        map_file = generate_map_file(workspace, result["total_units"], result["unit"])
        print(f"提示：{result['message']}")
        print(f"已生成目录映射模板：{map_file}")
        print("填写后执行：")
        print(f'  python scripts/split_book.py "{book_path}" --name {name} --map "{map_file}"')
        return
    if status == "scanned":
        print("警告：" + result["message"])
        sys.exit(1)
    if status == "empty":
        print("警告：" + result.get("message", "未能拆分出任何章节。"))
        sys.exit(1)

    print(f"已拆分 {len(result['chapters'])} 章，输出目录：{chapters_dir}")
    if result["skipped"]:
        print("已跳过的非正文条目：" + "、".join(result["skipped"]))
    if result.get("avg_chars") is not None:
        print(f"平均每页 {result['avg_chars']} 字")


if __name__ == "__main__":
    main()
