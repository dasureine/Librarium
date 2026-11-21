# -*- coding: utf-8 -*-
"""Librarium 自动化制书流水线引擎。

四阶段：
  阶段1  切分与预检（复用 scripts/split_book.py 的 pymupdf 逻辑，支持 .pdf / .epub）
  阶段2  逐章笔记 + 逐章图谱 + 术语表（断点续跑、前缀缓存降本、可选 chapter_range 试跑）
  阶段3  全书总览与无损合并（复用 scripts/merge_notes.py 逻辑）
  阶段4  综合生成整体知识地图 -> 注入 templates/map-template.html -> 上架 books.json

硬性红线：本文件只读取模板、写新地图与 notes/<slug>.md、追加 books.json，
绝不修改 index.html、maps/ 下既有地图、templates/map-template.html、notes/ 既有文件。
"""

import json
import os
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import split_book

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace"
RAW_BOOKS = ROOT / "raw_books"
NOTES_DIR = ROOT / "notes"
MAPS_DIR = ROOT / "maps"
SKILL_PATH = ROOT / ".agents" / "skills" / "book-notes" / "SKILL.md"
TEMPLATE_PATH = ROOT / "templates" / "map-template.html"
BOOKS_JSON = ROOT / "books.json"

BEIJING = timezone(timedelta(hours=8))  # 北京时间 UTC+8，无夏令时

# 与母版渲染引擎一致
CANVAS_W = 980
CANVAS_H = 560
NODE_W = 268
NODE_H = 128

# ===== 确定性防重叠布局步长（硬性写死，严禁下调） =====
# 全站定稿标准的唯一布局参数：水平列步长 ≥ 440px、垂直行步长 ≥ 260px。
# 步长必须同时容纳母版卡片尺寸与外拱弧线控制点的安全留白，一旦下调就会
# 让相邻卡片或连线外拱控制点重叠，因此此处只许上调、严禁改小。
COL_GAP = 440
ROW_GAP = 260
MAX_COLS = 3


def load_env() -> None:
    """读取根目录 .env（优先 dotenv，缺失时手工解析，保证零依赖兜底）。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except Exception:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def pricing_status(now: Optional[datetime] = None) -> dict:
    """北京时间峰谷计价检测。

    工作日 9:00-12:00、14:00-18:00 为高峰；其余时间与周末为闲时（半价）。
    """
    now = now or beijing_now()
    t = now.time()
    weekday = now.weekday()  # 0=周一 ... 6=周日
    if weekday >= 5:
        return {
            "peak": False,
            "tier": "offpeak",
            "label": "闲时半价（周末）",
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    if (dtime(9, 0) <= t < dtime(12, 0)) or (dtime(14, 0) <= t < dtime(18, 0)):
        return {
            "peak": True,
            "tier": "peak",
            "label": "高峰时段（工作日 9:00-12:00 / 14:00-18:00）",
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return {
        "peak": False,
        "tier": "offpeak",
        "label": "闲时半价（工作日非高峰）",
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


class ScannedBookError(Exception):
    """纯图片扫描版（平均每页不足 30 字）中断异常。"""


class JobCancelled(Exception):
    """任务被主动取消。"""


def sanitize_slug(slug: str) -> str:
    """把英文短名收敛为安全文件名，防路径穿越。"""
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", str(slug).strip().lower())
    slug = slug.strip("-")
    return slug or "book"


def find_raw_book(slug: str) -> Optional[Path]:
    """在 raw_books/ 中定位 slug 对应的电子书。"""
    if not RAW_BOOKS.is_dir():
        return None
    for ext in (".pdf", ".epub"):
        p = RAW_BOOKS / f"{slug}{ext}"
        if p.exists():
            return p
    for p in RAW_BOOKS.iterdir():
        if p.is_file() and p.stem == slug and p.suffix.lower() in (".pdf", ".epub"):
            return p
    return None


def natural_key(path: Path):
    name = path.name
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 代码块外壳。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def extract_json(text: str) -> str:
    """从模型输出中稳健提取第一个 JSON 对象/数组。"""
    text = strip_code_fence(text)
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        j = text.rfind(closer)
        if i != -1 and j > i:
            return text[i : j + 1]
    return text


class LLM:
    """官方 openai SDK 封装，兼容 OpenAI 接口（DeepSeek 等）。"""

    def __init__(self):
        self.base_url = os.getenv("API_BASE_URL", "https://api.deepseek.com")
        self.api_key = os.getenv("API_KEY", "")
        self.model_pro = os.getenv("MODEL_PRO", "deepseek-v4-pro")
        self.model_flash = os.getenv("MODEL_FLASH", "deepseek-flash")
        self.enable_thinking = os.getenv("ENABLE_THINKING", "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
        self._client = None

    @property
    def configured(self) -> bool:
        key = (self.api_key or "").strip()
        return bool(key) and key not in ("", "your_api_key_here", "your_api_key")

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=1200)
        return self._client

    def chat(self, model: str, system: str, user: str,
             json_mode: bool = False, temperature: Optional[float] = None,
             max_tokens: Optional[int] = None):
        """发起一次对话补全，返回 (文本, usage 字典)。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs = {"model": model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception:
                # 部分兼容接口不支持 response_format，降级重试一次
                kwargs.pop("response_format", None)
                resp = self.client.chat.completions.create(**kwargs)
        else:
            resp = self.client.chat.completions.create(**kwargs)

        text = (resp.choices[0].message.content or "") if resp.choices else ""
        usage = getattr(resp, "usage", None)
        u = {
            "prompt": getattr(usage, "prompt_tokens", 0) or 0,
            "completion": getattr(usage, "completion_tokens", 0) or 0,
            "total": getattr(usage, "total_tokens", 0) or 0,
        }
        return text, u


class JobState:
    """一次制书任务的实时状态（线程安全）。"""

    def __init__(self, slug: str, title: str, author: str, chapter_range: Optional[str] = None):
        self.id = uuid.uuid4().hex[:12]
        self.slug = slug
        self.title = title
        self.author = author
        self.chapter_range = chapter_range
        self.status = "queued"  # queued | running | done | error | cancelled
        self.stage = "pending"
        self.done_chapters = 0
        self.total_chapters = 0
        self.tokens = {"prompt": 0, "completion": 0, "total": 0}
        self.logs = []
        self.error = None
        self.result = None
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()

    def log(self, msg: str) -> str:
        line = f"[{datetime.now(BEIJING).strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            self.logs.append(line)
            self.logs = self.logs[-200:]
        return line

    def add_usage(self, u: dict) -> None:
        with self._lock:
            self.tokens["prompt"] += int(u.get("prompt", 0) or 0)
            self.tokens["completion"] += int(u.get("completion", 0) or 0)
            self.tokens["total"] += int(u.get("total", 0) or 0)

    def set(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "job_id": self.id,
                "slug": self.slug,
                "title": self.title,
                "author": self.author,
                "status": self.status,
                "stage": self.stage,
                "done_chapters": self.done_chapters,
                "total_chapters": self.total_chapters,
                "tokens": dict(self.tokens),
                "logs": list(self.logs),
                "error": self.error,
                "result": self.result,
                "pricing": pricing_status(),
            }


_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def start_job(title: str, author: str, slug: str, chapter_range: Optional[str] = None) -> JobState:
    """创建任务并在后台线程启动制书流水线。"""
    slug = sanitize_slug(slug)
    job = JobState(slug=slug, title=title.strip(), author=author.strip(), chapter_range=chapter_range)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    thread = threading.Thread(target=run_pipeline, args=(job,), daemon=True, name=f"book-{slug}")
    thread.start()
    return job


def get_job(job_id: str) -> Optional[JobState]:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> list:
    with _JOBS_LOCK:
        return [job.snapshot() for job in _JOBS.values()]


def _sample_page_avg(doc, page_count: int, sample_size: int = 18) -> float:
    """对 PDF 前中后均匀抽样约 sample_size 页，返回平均每页字符数。

    用于 30MB+ 大文件在逐页抽字前快速判定扫描版，避免长时间卡顿。
    """
    if page_count <= 0:
        return 0.0
    if page_count <= sample_size:
        pages = list(range(page_count))
    else:
        step = (page_count - 1) / (sample_size - 1)
        pages = sorted({round(step * i) for i in range(sample_size)})
        if pages[0] != 0:
            pages.insert(0, 0)
        if pages[-1] != page_count - 1:
            pages.append(page_count - 1)
    chars = sum(len((doc.load_page(p).get_text() or "").strip()) for p in pages)
    return chars / len(pages)


def split_precheck(slug: str, book_path: Path, page_map: Optional[str] = None, level: int = 1, offset: int = 0) -> dict:
    """阶段1：切分电子书为逐章纯文本，并做扫描版/无目录预检。

    返回 dict：status 为 ready / needs_page_map / empty，chapters 为章节列表。
    纯图片扫描版（平均每页 < 30 字）抛 ScannedBookError。
    """
    workspace = WORKSPACE / slug
    chapters_dir = workspace / "chapters"
    notes_dir = workspace / "chapter_notes"
    maps_dir = workspace / "chapter_maps"
    for d in (workspace, chapters_dir, notes_dir, maps_dir):
        d.mkdir(parents=True, exist_ok=True)
    glossary = workspace / "glossary.md"
    if not glossary.exists():
        glossary.write_text(split_book.GLOSSARY_HEADER, encoding="utf-8")

    fitz = split_book.load_fitz()
    doc = fitz.open(str(book_path))
    try:
        page_count = doc.page_count
        toc = doc.get_toc()
        spans = []
        if toc:
            toc_entries = split_book.filter_toc(toc, level)
            spans = split_book.toc_to_spans(toc_entries, page_count)

        if not spans:
            if page_map:
                map_path = Path(page_map)
                if not map_path.exists():
                    raise FileNotFoundError(f"找不到页码映射文件 {map_path}")
                spans = split_book.map_to_spans(split_book.parse_map(map_path), page_count)
            else:
                map_file = split_book.generate_map_file(workspace, page_count)
                return {
                    "status": "needs_page_map",
                    "page_count": page_count,
                    "map_file": str(map_file),
                    "chapters": [],
                    "message": "该电子书无内嵌目录，请填写页码映射后调用 /api/books/resplit。",
                }

        # 大文件先抽样预检扫描版（前中后约 18 页），避免逐页抽字卡顿
        try:
            file_size = book_path.stat().st_size
        except OSError:
            file_size = 0
        if file_size >= 30 * 1024 * 1024:
            sample_avg = _sample_page_avg(doc, page_count)
            if sample_avg < 30:
                raise ScannedBookError(
                    f"检测到疑似纯图片扫描版（抽样平均每页 {sample_avg:.1f} 字，低于 30 字），请先完成 OCR 转换。"
                )

        chapters = []
        total_chars = 0
        total_pages = 0
        for idx, (title, start, end) in enumerate(spans, start=1):
            start = max(0, start)
            end = min(page_count, max(end, start + 1))
            if start >= page_count:
                continue
            parts = [doc.load_page(pno).get_text() for pno in range(start, end)]
            text = "\n".join(parts).strip()
            total_pages += end - start
            total_chars += len(text)
            global_idx = idx + max(0, int(offset or 0))
            filename = f"{global_idx:02d}_{split_book.to_filename(title) or '未命名章节'}.txt"
            (chapters_dir / filename).write_text(text, encoding="utf-8")
            chapters.append({
                "index": global_idx,
                "filename": filename,
                "title": title or "未命名章节",
                "chars": len(text),
                "pages": end - start,
            })

        if not chapters:
            return {"status": "empty", "chapters": [], "message": "未能拆分出任何章节，请检查目录层级或页码映射。"}

        avg = total_chars / total_pages if total_pages else 0
        if avg < 30:
            raise ScannedBookError(
                f"检测到疑似纯图片扫描版（平均每页 {avg:.1f} 字，低于 30 字），请先完成 OCR 转换。"
            )
        return {"status": "ready", "chapters": chapters, "avg_chars_per_page": round(avg, 1)}
    finally:
        doc.close()


def list_existing_chapters(slug: str) -> list:
    """读取 workspace/<slug>/chapters/ 已切分章节，供断点复用（避免重复切分）。"""
    chapters_dir = WORKSPACE / slug / "chapters"
    if not chapters_dir.is_dir():
        return []
    chapters = []
    for f in sorted(chapters_dir.glob("*.txt"), key=natural_key):
        stem = f.stem
        m = re.match(r"^(\d+)_(.*)$", stem)
        index = int(m.group(1)) if m else 0
        title = (m.group(2) if m else stem).replace("_", " ").strip()
        text = f.read_text(encoding="utf-8")
        chapters.append({
            "index": index or (len(chapters) + 1),
            "filename": f.name,
            "title": title or "未命名章节",
            "chars": len(text),
            "pages": 0,
        })
    return chapters


def stage1_split(job: JobState) -> list:
    """阶段1：定位电子书并切分（已切分则复用），返回章节列表。"""
    job.set(stage="split")
    job.log("阶段1：切分与预检")
    existing = list_existing_chapters(job.slug)
    if existing:
        job.log(f"复用已切分章节：{len(existing)} 章")
        return existing
    book = find_raw_book(job.slug)
    if not book:
        raise RuntimeError(f"未在 raw_books/ 找到 {job.slug} 的电子书，请先调用 /api/books/upload 上传。")
    result = split_precheck(job.slug, book)
    if result["status"] == "needs_page_map":
        raise RuntimeError(
            f"该电子书无内嵌目录，请先通过 /api/books/resplit 提交页码映射。示例表：{result.get('map_file')}"
        )
    if result["status"] == "empty":
        raise RuntimeError(result.get("message", "未能拆分出章节"))
    chapters = result["chapters"]
    job.log(f"切分完成：{len(chapters)} 章（平均每页 {result.get('avg_chars_per_page')} 字）")
    return chapters


def parse_range(chapter_range: str) -> set:
    """解析 '1-3' / '1,3,5' / '1-2,4' 为 1 基章节序号集合。"""
    idxs = set()
    for part in str(chapter_range).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                idxs.update(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                idxs.add(int(part))
            except ValueError:
                continue
    return idxs


def filter_chapters(chapters: list, chapter_range: Optional[str]) -> list:
    if not chapter_range:
        return chapters
    idxs = parse_range(chapter_range)
    return [c for c in chapters if c["index"] in idxs]


def load_style_system() -> str:
    """读取 book-notes 技能文风规范，作为跨章固定不变的 system（命中前缀缓存）。"""
    skill = ""
    if SKILL_PATH.exists():
        skill = SKILL_PATH.read_text(encoding="utf-8")
    return (
        "你是 Librarium 的学术精读助手，负责把大部头电子书拆解为平实精准的学术笔记与知识图谱。\n"
        "必须逐条遵守以下文风与流程规范，写作时使用简单平和的书面中文，直接主谓宾陈述句：\n\n"
        + skill
    )


def build_chapter_prompt(ch: dict, text: str) -> str:
    max_chars = 24000
    excerpt = text if len(text) <= max_chars else text[:max_chars] + "\n……（章节过长，已截断）"
    return (
        "请精读下面这一章，并严格输出一个 JSON 对象（不要输出任何 JSON 之外的文字）。\n\n"
        f"章节编号：{ch['index']:02d}\n章节标题：{ch['title']}\n\n"
        f"【原文】\n{excerpt}\n\n"
        "JSON 字段如下：\n"
        '{\n'
        '  "note": "...",\n'
        '  "glossary": ["概念 | 一句话界定与要点", ...3到5条],\n'
        '  "map": {"nodes": [...], "edges": [...]}\n'
        '}\n\n'
        "1. note：本章学术精读笔记（Markdown），必须固定包含四部分：核心问题意识、论证结构与推进、关键概念界定、章节衔接与张力。\n"
        "2. glossary：本章 3-5 条核心概念，每条格式为「概念 | 一句话界定与要点」。\n"
        "3. map.nodes：本章概念/论点节点数组，每项含 id、category、title、brief、summary、subPoints（label+text 数组）、perspectivesNote（label+text 数组，可空数组）。\n"
        "4. map.edges：本章节点间因果/张力连线数组，每项含 id、source（节点id）、target（节点id）、label、detail。\n"
        f"节点 id 用 c{ch['index']:02d}n1、c{ch['index']:02d}n2… 递增；连线 id 用 c{ch['index']:02d}e1、c{ch['index']:02d}e2… 递增。\n"
    )


def append_glossary(glossary_path: Path, concepts, chapter: dict) -> None:
    if not concepts:
        return
    lines = []
    for item in concepts:
        item = str(item).strip()
        if not item:
            continue
        if "|" in item:
            name, rest = item.split("|", 1)
            lines.append(f"| {name.strip()} | {chapter['title']} | {rest.strip()} |")
        else:
            lines.append(f"| {item} | {chapter['title']} | |")
    with glossary_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def parse_json_robust(raw: str, llm: LLM, job: JobState) -> dict:
    """解析模型 JSON，失败则用 MODEL_FLASH 自动修复。"""
    try:
        return json.loads(extract_json(raw))
    except Exception as e:
        job.log(f"JSON 解析失败，调用 MODEL_FLASH 修复：{e}")
        fixed, usage = llm.chat(
            llm.model_flash,
            "你是 JSON 修复器。请把用户给出的内容修复为合法 JSON，只输出修复后的纯 JSON，不要输出解释。",
            raw,
            json_mode=True,
        )
        job.add_usage(usage)
        return json.loads(extract_json(fixed))


def stage2_chapters(job: JobState, llm: LLM, chapters: list) -> None:
    """阶段2：逐章生成笔记 + 图谱 + 术语表（断点续跑，绝不重复扣费）。"""
    job.set(stage="chapters")
    workspace = WORKSPACE / job.slug
    chapters_dir = workspace / "chapters"
    notes_dir = workspace / "chapter_notes"
    maps_dir = workspace / "chapter_maps"
    glossary_path = workspace / "glossary.md"
    for d in (notes_dir, maps_dir):
        d.mkdir(parents=True, exist_ok=True)

    chapter_system = load_style_system() + (
        "\n\n输出要求：每次只输出一个 JSON 对象（含 note、glossary、map 三个字段），"
        "不要输出任何 JSON 之外的文字、注释或代码块。"
    )

    total = len(chapters)
    job.set(total_chapters=total)
    for done, ch in enumerate(chapters, start=0):
        if job.cancel_event.is_set():
            raise JobCancelled()
        job.set(done_chapters=done)
        job.log(f"处理第 {ch['index']}/{total} 章：{ch['title']}")
        base = ch["filename"][: -len(".txt")]
        note_path = notes_dir / f"{base}.md"
        map_path = maps_dir / f"{base}.json"
        if note_path.exists() and map_path.exists():
            job.log("  已存在，跳过（断点续跑，不重复扣费）")
            job.set(done_chapters=done + 1)
            continue

        text = (chapters_dir / ch["filename"]).read_text(encoding="utf-8")
        raw, usage = llm.chat(llm.model_pro, chapter_system, build_chapter_prompt(ch, text), json_mode=True)
        job.add_usage(usage)
        data = parse_json_robust(raw, llm, job)

        note = str(data.get("note", "") or "").strip()
        note_path.write_text(note + "\n", encoding="utf-8")

        append_glossary(glossary_path, data.get("glossary", []), ch)

        cmap = data.get("map") if isinstance(data.get("map"), dict) else {}
        cmap.setdefault("chapter_id", f"{ch['index']:02d}")
        cmap.setdefault("chapter_title", ch["title"])
        map_path.write_text(json.dumps(cmap, ensure_ascii=False, indent=2), encoding="utf-8")

        # 顺手用母版把本章 JSON 直接固化为独立分章地图，供整书总图下钻。
        try:
            chapter_html = inject_chapter_map(job.slug, ch["index"], cmap, job.title,
                                              has_parent=(not job.chapter_range) or (MAPS_DIR / f"{job.slug}.html").exists())
            job.log(f"  分章地图已生成：{chapter_html}")
        except Exception as e:
            job.log(f"  分章地图生成失败：{e}")

        job.set(done_chapters=done + 1)
        job.log(f"  完成第 {ch['index']} 章（笔记 + 图谱 + 术语表 + 分章地图）")


def merge_notes_into(slug: str, title: str) -> tuple:
    """复用 merge_notes.py 逻辑：overview + 逐章笔记 -> notes/<slug>.md。"""
    workspace = WORKSPACE / slug
    notes_dir = workspace / "chapter_notes"
    overview = workspace / "overview.md"
    parts = []
    if title:
        parts.append(title.strip())
    if overview.exists():
        parts.append(overview.read_text(encoding="utf-8-sig").rstrip("\n"))
    chapter_files = sorted(notes_dir.glob("*.md"), key=natural_key) if notes_dir.is_dir() else []
    for f in chapter_files:
        parts.append(f.read_text(encoding="utf-8-sig").rstrip("\n"))
    content = "\n\n".join(p for p in parts if p) + "\n"
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    out = NOTES_DIR / f"{slug}.md"
    out.write_text(content, encoding="utf-8")
    return out, len(chapter_files), len(content)


def stage3_overview(job: JobState, llm: LLM) -> Path:
    """阶段3：生成全书总览 overview.md，并无损合并输出 notes/<slug>.md。"""
    job.set(stage="overview")
    job.log("阶段3：全书总览与无损合并")
    workspace = WORKSPACE / job.slug
    glossary_path = workspace / "glossary.md"
    notes_dir = workspace / "chapter_notes"
    glossary = glossary_path.read_text(encoding="utf-8") if glossary_path.exists() else ""
    titles = []
    if notes_dir.is_dir():
        for f in sorted(notes_dir.glob("*.md"), key=natural_key):
            head = f.read_text(encoding="utf-8-sig").split("\n", 1)[0].strip()
            titles.append(head)

    system = load_style_system()
    user = (
        "请基于下面的全书术语表与各章标题，撰写一份全书总览（直接输出 Markdown，不要任何 JSON 或解释）。\n"
        "总览需包含：全书核心问题、逻辑框架、章节脉络。\n\n"
        f"【术语表】\n{glossary[:6000]}\n\n"
        f"【各章标题】\n" + "\n".join(titles[:200])
    )
    raw, usage = llm.chat(llm.model_pro, system, user)
    job.add_usage(usage)
    overview_path = workspace / "overview.md"
    overview_path.write_text(raw.strip() + "\n", encoding="utf-8")

    out, n, size = merge_notes_into(job.slug, job.title)
    job.log(f"总览完成，合并 {n} 章笔记 -> {out}（{size} 字）")
    return out


def build_map_prompt(job: JobState, overview: str, glossary: str, chapter_maps: list) -> str:
    cmap_parts = [json.dumps(cm, ensure_ascii=False)[:8000] for cm in chapter_maps]
    return (
        "你是知识图谱工程师。请把以下资料综合成一份信息密度与 templates/map-template.html 所载范例相当的整书知识地图 JSON。\n\n"
        f"【全书总览】\n{overview[:6000]}\n\n"
        f"【术语表】\n{glossary[:6000]}\n\n"
        f"【各章结构化图谱】\n{json.dumps(cmap_parts, ensure_ascii=False)}\n\n"
        "严格输出一个 JSON 对象（不要输出任何 JSON 之外的文字、注释或 Markdown 代码块），结构如下：\n"
        "{\n"
        '  "meta": {"title": "...", "subtitle": "...", "overview": "..."},\n'
        '  "perspectives": [\n'
        '    {"id": "all", "label": "全景互锁", "legendLabel": "...", "lineClass": "both"},\n'
        '    {"id": "...", "label": "...", "legendLabel": "...", "lineClass": "primary"},\n'
        '    {"id": "...", "label": "...", "legendLabel": "...", "lineClass": "secondary"}\n'
        '  ],\n'
        '  "readingGuide": {"coreQuestion": "...", "startNodeId": "n1", "recommendedPath": [{"id": "n1", "label": "..."}]},\n'
        '  "nodes": [\n'
        '    {"id": "n1", "category": "...", "title": "...", "brief": "...", "coreIdea": "...", "summary": "...",\n'
        '     "subPoints": [{"label": "...", "text": "..."}],\n'
        '     "perspectivesNote": [{"label": "...", "text": "..."}]}\n'
        '  ],\n'
        '  "edges": [\n'
        '    {"id": "e1", "source": "n1", "target": "n2", "perspective": "both", "label": "...", "curve": 0, "labelT": 0.5, "detail": "..."}\n'
        '  ],\n'
        '  "steps": [\n'
        '    {"title": "步骤 1 / N：...", "description": "...", "activeNodes": ["n1"], "activeEdges": ["e1"]}\n'
        '  ]\n'
        "}\n\n"
        "硬性要求：\n"
        "1. 只输出纯 JSON，键名与上述完全一致，值用中文。\n"
        "2. 节点数量控制在 6-10 个，覆盖全书核心论点群；每节点 title 严格 ≤12 字、brief 严格 ≤16 字（brief 只放关键词锚点，严禁写长句）、coreIdea 为 20-40 字的一句话核心结论、summary 60-120 字，所有详细解释全部放进 subPoints/perspectivesNote，保证右侧阅读栏解析详实。\n"
        "3. 严禁输出任何 x/y 坐标：节点坐标由后端 Python 按层级网格/蛇形递进算法自动计算，杜绝重叠。\n"
        '4. edges 的 perspective 取值必须是 "both" 或 perspectives 中某个 id；curve 为 -200~200 整数（0 为直线），labelT 为 0~1；label 短，detail 为 40-90 字深度解析。\n'
        "5. steps 给 4-6 步，按逻辑顺序覆盖因果链，activeNodes/activeEdges 引用上面 id。\n"
        "6. readingGuide.coreQuestion 用 2-3 句直白的话交代本部分核心问题；startNodeId 指向逻辑起点节点；recommendedPath 给 3-5 个按顺序编号的关键节点（含 id 与简短 label）。\n"
        '7. 除 "all" 外再给出 2 个分析视角（简短英文/拼音 id），分别对应 lineClass 的 primary 与 secondary。\n'
    )


def _as_int(value, fallback=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def _as_float(value, fallback=0.5):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def truncate_cjk(value, limit: int) -> str:
    """按字符数截断中文文本（严格限长）。"""
    s = str(value or "").strip()
    return s if len(s) <= limit else s[:limit]


def first_sentence(value: str) -> str:
    """截取第一句作为一句话核心结论。"""
    s = str(value or "").strip()
    for sep in ("。", "！", "？", "\n"):
        idx = s.find(sep)
        if idx >= 0:
            return s[: idx + 1].strip()
    return s


def build_deterministic_layout(nodes: list, col_gap: int = COL_GAP, row_gap: int = ROW_GAP, max_cols: int = MAX_COLS) -> list:
    """层级网格/蛇形递进布局：按节点数组顺序蛇形铺开，输出无重叠的中心坐标 (x, y)。

    列距 = col_gap、行距 = row_gap，保证卡片之间水平、垂直均留出安全空隙。
    """
    n = len(nodes)
    if n == 0:
        return nodes
    cols = max(1, min(max_cols, n))
    for i, node in enumerate(nodes):
        row = i // cols
        col = i % cols
        if row % 2 == 1:
            col = cols - 1 - col  # 蛇形：奇数行反向递进
        node["x"] = int(col * col_gap)
        node["y"] = int(row * row_gap)
    return nodes


def normalize_map_data(data: dict, job: JobState) -> dict:
    """把模型输出的 MAP_DATA 规范化为母版渲染引擎所需结构。

    这是「LLM 只输出结构化 JSON → Python 确定性防重叠布局 → 注入模板」流程中的
    唯一确定性后处理关卡：模型绝不直接写 HTML/CSS，任何字段都必须在此被校验、
    截断与兜底后，才能由 inject_map() 注入 templates/map-template.html。
    """
    data = data if isinstance(data, dict) else {}
    meta = data.get("meta") or {}
    title = str(meta.get("title", job.title or "")).strip() or job.title
    subtitle = str(meta.get("subtitle", "")).strip()
    overview = str(meta.get("overview", "")).strip()

    perspectives = []
    for p in (data.get("perspectives") or []):
        if not isinstance(p, dict):
            continue
        perspectives.append({
            "id": str(p.get("id", "")).strip(),
            "label": str(p.get("label", "")).strip(),
            "legendLabel": str(p.get("legendLabel", "")).strip(),
            "lineClass": p.get("lineClass") if p.get("lineClass") in ("both", "primary", "secondary") else "both",
        })
    if not any(p["id"] == "all" for p in perspectives):
        perspectives.insert(0, {"id": "all", "label": "全景互锁", "legendLabel": "全书共用结构", "lineClass": "both"})

    nodes = []
    for i, n in enumerate(data.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        # 卡片标题 ≤12 字，超长自动截断（用独立变量名，避免遮蔽外层 meta.title）。
        node_title = truncate_cjk(n.get("title"), 12)
        # 卡片副标题（brief）≤16 字：超长时把完整原文前置保留到右侧正文，
        # 卡片只保留 16 字以内精简锚点，彻底杜绝长句塞进卡片撑爆布局。
        raw_brief = str(n.get("brief", "")).strip()
        brief = truncate_cjk(raw_brief, 16)
        summary = str(n.get("summary", "")).strip()
        if len(raw_brief) > 16 and raw_brief not in summary:
            summary = f"{raw_brief}。{summary}".strip()
        core_idea = str(n.get("coreIdea", "")).strip() or first_sentence(summary)
        node_obj = {
            "id": str(n.get("id") or f"n{i + 1}"),
            "x": 0,
            "y": 0,
            "category": str(n.get("category", "")).strip(),
            "title": node_title,
            "brief": brief,
            "coreIdea": truncate_cjk(core_idea, 60),
            "summary": summary,
            "subPoints": [
                {"label": str(sp.get("label", "")).strip(), "text": str(sp.get("text", "")).strip()}
                for sp in (n.get("subPoints") or []) if isinstance(sp, dict)
            ],
            "perspectivesNote": [
                {"label": str(pn.get("label", "")).strip(), "text": str(pn.get("text", "")).strip()}
                for pn in (n.get("perspectivesNote") or []) if isinstance(pn, dict)
            ],
        }
        if n.get("subMapPath"):
            node_obj["subMapPath"] = str(n["subMapPath"]).strip()
        nodes.append(node_obj)
    nodes = build_deterministic_layout(nodes)

    perspective_ids = {p["id"] for p in perspectives}
    edges = []
    for i, e in enumerate(data.get("edges") or []):
        if not isinstance(e, dict):
            continue
        persp = str(e.get("perspective", "both")).strip()
        if persp not in perspective_ids:
            persp = "both"
        edges.append({
            "id": str(e.get("id") or f"e{i + 1}"),
            "source": str(e.get("source", "")).strip(),
            "target": str(e.get("target", "")).strip(),
            "perspective": persp,
            "label": str(e.get("label", "")).strip(),
            "curve": _as_int(e.get("curve"), 0),
            "labelT": _as_float(e.get("labelT"), 0.5),
            "detail": str(e.get("detail", "")).strip(),
        })

    steps = []
    for s in (data.get("steps") or []):
        if not isinstance(s, dict):
            continue
        steps.append({
            "title": str(s.get("title", "")).strip(),
            "description": str(s.get("description", "")).strip(),
            "activeNodes": [str(x) for x in (s.get("activeNodes") or [])],
            "activeEdges": [str(x) for x in (s.get("activeEdges") or [])],
        })

    guide = data.get("readingGuide") or {}
    start_node_id = str(guide.get("startNodeId", "")).strip()
    recommended_path = []
    for rp in (guide.get("recommendedPath") or []):
        if isinstance(rp, dict):
            recommended_path.append({"id": str(rp.get("id", "")).strip(), "label": str(rp.get("label", "")).strip()})
        elif isinstance(rp, str):
            recommended_path.append({"id": rp.strip(), "label": ""})
    # 硬性兜底：每本书都必须有新手导览，绝不允许空 startNodeId / 空推荐路线。
    node_ids = {n["id"] for n in nodes}
    if not start_node_id or start_node_id not in node_ids:
        start_node_id = nodes[0]["id"] if nodes else ""
    recommended_path = [rp for rp in recommended_path if rp["id"] in node_ids]
    if not recommended_path and nodes:
        recommended_path = [{"id": n["id"], "label": n["title"]} for n in nodes[:5]]
    reading_guide = {
        "coreQuestion": str(guide.get("coreQuestion", "")).strip(),
        "startNodeId": start_node_id,
        "recommendedPath": recommended_path,
    }

    result = {"meta": {"title": title, "subtitle": subtitle, "overview": overview},
              "perspectives": perspectives, "nodes": nodes, "edges": edges, "steps": steps,
              "readingGuide": reading_guide}
    # 整书总图下钻分章 / 分章返回整书的双向跳转字段（可选，原样透传）
    if isinstance(data.get("subMaps"), list):
        result["subMaps"] = data["subMaps"]
    if isinstance(data.get("parentMap"), dict):
        result["parentMap"] = data["parentMap"]
    return result


def generate_metadata(job: JobState, llm: LLM, map_data: dict) -> dict:
    """用 MODEL_FLASH 生成 books.json 精炼元数据条目。"""
    system = (
        "你是书籍元数据编辑。只输出一个合法 JSON 对象，不要输出解释或代码块。"
    )
    user = (
        "请基于下面的知识地图元信息，生成 books.json 所需的精炼条目。\n"
        "字段：id、title、subtitle、author、date、tags、summary、mapPath。\n"
        f"其中 id 固定为 \"{job.slug}\"，mapPath 固定为 \"./maps/{job.slug}.html\"，"
        "date 用 YYYY-MM 格式（当前年月），tags 仅给 1-3 个标准「一级学科/二级学科」标签"
        "（如 哲学/分析哲学、社会学/宗教社会学、哲学/西方哲学），严禁输出具体理论概念"
        "（如 本原、逻各斯中心主义、国家机器 等），summary 给 80-150 字概括。\n\n"
        f"【地图元信息】\n"
        f"title: {job.title}\n"
        f"subtitle: {map_data['meta']['subtitle']}\n"
        f"author: {job.author}\n"
        f"overview: {map_data['meta']['overview'][:500]}\n"
    )
    raw, usage = llm.chat(llm.model_flash, system, user, json_mode=True)
    job.add_usage(usage)
    try:
        entry = json.loads(extract_json(raw))
    except Exception:
        entry = {}
    entry["id"] = job.slug
    entry["title"] = job.title
    entry.setdefault("author", job.author)
    entry.setdefault("subtitle", "")
    entry.setdefault("tags", [])
    entry.setdefault("summary", "")
    entry.setdefault("date", beijing_now().strftime("%Y-%m"))
    entry["mapPath"] = f"./maps/{job.slug}.html"
    return entry


def load_books() -> list:
    """读取 books.json 为列表（缺失或损坏时返回空列表）。"""
    if not BOOKS_JSON.exists():
        return []
    try:
        data = json.loads(BOOKS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def existing_chapters_for_slug(slug: str) -> dict:
    """返回某 slug 已入藏的分章信息，供续传防重与防撞号。

    同时检查 books.json 的 chapters 列表与 maps/<slug>/ 已有文件。
    """
    book = next((b for b in load_books() if isinstance(b, dict) and b.get("id") == slug), None)
    chapters = [c for c in (book.get("chapters") or []) if isinstance(c, dict)] if book else []
    max_order = 0
    for c in chapters:
        order = c.get("order")
        if isinstance(order, int) and order > max_order:
            max_order = order
    map_files = []
    sub = MAPS_DIR / slug
    if sub.is_dir():
        map_files = [p.name for p in sorted(sub.glob("*.html"), key=natural_key)]
    return {
        "slug": slug,
        "title": (book.get("title") or "") if book else "",
        "chapters": chapters,
        "max_order": max_order,
        "map_files": map_files,
    }


def _chapter_key(chapter: dict):
    """归一化章节合并键：优先 order，其次 mapPath，最后 id。"""
    order = chapter.get("order")
    if isinstance(order, int):
        return ("order", order)
    map_path = str(chapter.get("mapPath") or "").strip()
    if map_path:
        return ("mapPath", map_path)
    return ("id", str(chapter.get("id") or "").strip())


def _merge_chapter(old_ch: dict, new_ch: dict) -> dict:
    """合并同一章：以新值为准，但新章未携带标签时保留旧章标签。"""
    merged = dict(old_ch)
    for key, val in new_ch.items():
        if key == "tags" and not val:
            continue
        merged[key] = val
    return merged


def merge_chapters(old_chapters: list, new_chapters: list) -> list:
    """无损合并章节：旧章全部保留；新章仅当章号/路径完全一致时更新对应旧章。"""
    merged = []
    index_by_key = {}
    for ch in old_chapters:
        if not isinstance(ch, dict):
            continue
        key = _chapter_key(ch)
        if key in index_by_key:
            continue
        index_by_key[key] = len(merged)
        merged.append(dict(ch))
    for ch in new_chapters:
        if not isinstance(ch, dict):
            continue
        key = _chapter_key(ch)
        if key in index_by_key:
            merged[index_by_key[key]] = _merge_chapter(merged[index_by_key[key]], ch)
        else:
            index_by_key[key] = len(merged)
            merged.append(dict(ch))

    def _sort_key(ch):
        order = ch.get("order")
        return (0, order) if isinstance(order, int) else (1, 0)

    merged.sort(key=_sort_key)
    return merged


def merge_book_entry(old: dict, new: dict) -> dict:
    """合并到已有书目：完整保留旧元信息，仅无损合并 chapters。"""
    merged = dict(old)
    # 元信息旧值优先，缺项用新值补齐（完整保留原有元信息）
    for key in ("id", "title", "subtitle", "author", "date", "tags", "summary"):
        if not merged.get(key) and new.get(key):
            merged[key] = new[key]
    # 全书总图链接：优先保留已有链接，缺省时用新生成链接补齐
    if not str(merged.get("mapPath") or "").strip():
        new_map = str(new.get("mapPath") or "").strip()
        if new_map:
            merged["mapPath"] = new_map
            merged["url"] = new_map
    if not str(merged.get("url") or "").strip():
        new_url = str(new.get("url") or "").strip()
        if new_url:
            merged["url"] = new_url
    # overallStatus：新条目为 complete 且带总图时升级，否则保留旧状态
    if new.get("overallStatus") == "complete" and str(merged.get("mapPath") or "").strip():
        merged["overallStatus"] = "complete"
    elif not merged.get("overallStatus"):
        merged["overallStatus"] = new.get("overallStatus") or "partial"
    merged["chapters"] = merge_chapters(old.get("chapters") or [], new.get("chapters") or [])
    return merged


def update_books_json(entry: dict) -> Path:
    """按 slug/id 无损合并写入 books.json。

    - 新书：直接追加；
    - 已有书：完整保留原元信息与旧 chapters，仅把本次新章节合并进去；
    - 合并后 chapters 按章号升序稳定排序；
    - 写回后自动同步 data/taxonomy.json。
    """
    entry = dict(entry)
    map_path = str(entry.pop("mapPath", "") or "").strip()
    entry["mapPath"] = map_path
    if map_path:
        entry["url"] = map_path  # 首页 Atlas 读取的是 book.url
    else:
        entry.pop("url", None)  # 整书未完篇：不生成 url，保持 mapPath 为空

    data = load_books()
    target_id = entry.get("id")
    merged_any = False
    for i, b in enumerate(data):
        if isinstance(b, dict) and b.get("id") == target_id:
            data[i] = merge_book_entry(b, entry)
            merged_any = True
            break
    if not merged_any:
        data.append(entry)

    BOOKS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 合并写回后自动同步学科分类索引，确保 Atlases 与 Taxonomy 立刻展示最新章节。
    try:
        import build_index

        build_index.rebuild_index()
    except Exception:
        # 索引同步失败不应阻断制书流程，仅静默降级。
        pass
    return BOOKS_JSON


def inject_map_to(slug: str, map_data: dict, out_dir: Path = None) -> Path:
    """把标题与 MAP_DATA 精准注入母版，生成 out_dir/<slug>.html。

    仅替换 <title> 与 const MAP_DATA = { ... };，其余 HTML/CSS/JS 逐字节不动。
    out_dir 默认 MAPS_DIR，分章地图可传入 MAPS_DIR/<book_slug> 子目录。
    """
    out_dir = out_dir or MAPS_DIR
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    full_title = map_data["meta"]["title"]
    short_title = re.split(r"[：:]", full_title, 1)[0].strip() or full_title
    template = re.sub(
        r"<title>.*?</title>",
        f"<title>{short_title} — Librarium</title>",
        template,
        count=1,
    )

    start = template.index("const MAP_DATA = {")
    render_comment_idx = template.index("渲染与交互引擎")
    old_brace = template.rfind("};", start, render_comment_idx)
    end = old_brace + len("};")
    new_block = "const MAP_DATA = " + json.dumps(map_data, ensure_ascii=False, indent=2) + ";"
    result = template[:start] + new_block + template[end:]

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{slug}.html"
    out.write_text(result, encoding="utf-8")
    return out


def inject_map(slug: str, map_data: dict) -> Path:
    """把标题与 MAP_DATA 注入母版，生成 maps/<slug>.html（整书总图）。"""
    return inject_map_to(slug, map_data, MAPS_DIR)


def build_chapter_map_data(chapter_map: dict, book_title: str, parent_path: str,
                           order: int, chapter_title: str = None,
                           subtitle: str = "", overview: str = "") -> dict:
    """把单章 chapter_maps/xx.json（nodes/edges）确定性包装为完整 MAP_DATA。

    复用 build_deterministic_layout 计算无重叠坐标；自动注入 parentMap 指向整书总图，
    使分章地图左上角出现「← 返回整书总图」按钮。
    """
    cmap = chapter_map if isinstance(chapter_map, dict) else {}
    raw_chapter_title = str(cmap.get("chapter_title") or "").strip()
    chapter_title = (chapter_title or raw_chapter_title or f"第{order}章").strip()

    nodes = []
    for i, n in enumerate(cmap.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        raw_brief = str(n.get("brief", "")).strip()
        brief = truncate_cjk(raw_brief, 16)
        summary = str(n.get("summary", "")).strip()
        if len(raw_brief) > 16 and raw_brief not in summary:
            summary = f"{raw_brief}。{summary}".strip()
        core_idea = str(n.get("coreIdea", "")).strip() or first_sentence(summary)
        nodes.append({
            "id": str(n.get("id") or f"n{i + 1}"),
            "x": 0,
            "y": 0,
            "category": str(n.get("category", "")).strip(),
            "title": truncate_cjk(n.get("title"), 12),
            "brief": brief,
            "coreIdea": truncate_cjk(core_idea, 60),
            "summary": summary,
            "subPoints": [
                {"label": str(sp.get("label", "")).strip(), "text": str(sp.get("text", "")).strip()}
                for sp in (n.get("subPoints") or []) if isinstance(sp, dict)
            ],
            "perspectivesNote": [
                {"label": str(pn.get("label", "")).strip(), "text": str(pn.get("text", "")).strip()}
                for pn in (n.get("perspectivesNote") or []) if isinstance(pn, dict)
            ],
        })
    nodes = build_deterministic_layout(nodes)

    edges = []
    for i, e in enumerate(cmap.get("edges") or []):
        if not isinstance(e, dict):
            continue
        edges.append({
            "id": str(e.get("id") or f"e{i + 1}"),
            "source": str(e.get("source", "")).strip(),
            "target": str(e.get("target", "")).strip(),
            "perspective": "both",
            "label": str(e.get("label", "")).strip(),
            "curve": _as_int(e.get("curve"), 0),
            "labelT": _as_float(e.get("labelT"), 0.5),
            "detail": str(e.get("detail", "")).strip(),
        })

    start_node_id = nodes[0]["id"] if nodes else ""
    recommended_path = [{"id": n["id"], "label": n["title"]} for n in nodes[:5]]
    overview = overview or (first_sentence(nodes[0]["summary"]) if nodes else "")
    title = f"{book_title} · {chapter_title}" if book_title else chapter_title

    return {
        "meta": {
            "title": title,
            "subtitle": subtitle,
            "overview": overview,
        },
        "perspectives": [
            {"id": "all", "label": "全景互锁", "legendLabel": "本章结构", "lineClass": "both"},
        ],
        "nodes": nodes,
        "edges": edges,
        "steps": [],
        "readingGuide": {
            "coreQuestion": overview,
            "startNodeId": start_node_id,
            "recommendedPath": recommended_path,
        },
        "parentMap": {"title": book_title, "path": parent_path},
        "subMaps": [],
    }


def inject_chapter_map(slug: str, order: int, chapter_map: dict, book_title: str,
                       chapter_title: str = None, subtitle: str = "",
                       overview: str = "", has_parent: bool = True) -> Path:
    """阶段2：每完成一章即用母版生成 maps/<slug>/chXX.html 分章地图。"""
    parent_path = f"../{slug}.html" if has_parent else ""
    map_data = build_chapter_map_data(
        chapter_map, book_title, parent_path, order,
        chapter_title=chapter_title, subtitle=subtitle, overview=overview,
    )
    return inject_map_to(f"ch{order:02d}", map_data, MAPS_DIR / slug)


def list_chapter_maps(slug: str) -> list:
    """读取 workspace/<slug>/chapter_maps/*.json，按编号排序返回 {order, chapter_map}。"""
    maps_dir = WORKSPACE / slug / "chapter_maps"
    result = []
    if not maps_dir.is_dir():
        return result
    for f in sorted(maps_dir.glob("*.json"), key=natural_key):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        m = re.match(r"^(\d+)_", f.stem)
        order = int(m.group(1)) if m else 0
        result.append({"order": order or (len(result) + 1), "chapter_map": data})
    return result


def build_submaps_and_chapters(slug: str, book_title: str) -> tuple:
    """由已生成的分章图谱构造整书总图的 subMaps 与 books.json 的 chapters。"""
    submaps = []
    chapters = []
    for item in list_chapter_maps(slug):
        cmap = item["chapter_map"]
        order = item["order"]
        title = str(cmap.get("chapter_title") or f"第{order}章").strip()
        summary = ""
        nodes = cmap.get("nodes") or []
        if nodes and isinstance(nodes[0], dict):
            summary = first_sentence(str(nodes[0].get("summary", "")).strip())
        submaps.append({"title": title, "path": f"{slug}/ch{order:02d}.html"})
        chapters.append({
            "id": f"{slug}-ch{order:02d}",
            "order": order,
            "title": title,
            "summary": summary,
            "tags": [],
            "mapPath": f"./maps/{slug}/ch{order:02d}.html",
        })
    return submaps, chapters


def stage2_partial_finish(job: JobState) -> Path:
    """试跑/部分章节模式：仅上架已生成的分章地图，整书保持 partial 且 mapPath 为空。"""
    _, chapters = build_submaps_and_chapters(job.slug, job.title)
    entry = {
        "id": job.slug,
        "title": job.title,
        "author": job.author,
        "subtitle": "",
        "date": beijing_now().strftime("%Y-%m"),
        "tags": [],
        "summary": "",
        "overallStatus": "partial",
        "mapPath": "",
        "chapters": chapters,
    }
    out = update_books_json(entry)
    job.log("已上架 books.json（无损合并）")
    return out


def stage4_map(job: JobState, llm: LLM) -> tuple:
    """阶段4a：综合 overview + glossary + 全部 chapter_maps 生成完整 MAP_DATA。"""
    job.set(stage="map")
    job.log("阶段4：综合生成整体知识地图")
    workspace = WORKSPACE / job.slug
    overview = (workspace / "overview.md").read_text(encoding="utf-8") if (workspace / "overview.md").exists() else ""
    glossary = (workspace / "glossary.md").read_text(encoding="utf-8") if (workspace / "glossary.md").exists() else ""
    maps_dir = workspace / "chapter_maps"
    chapter_maps = []
    if maps_dir.is_dir():
        for f in sorted(maps_dir.glob("*.json"), key=natural_key):
            try:
                chapter_maps.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue

    system = "你是知识图谱工程师。只输出一个合法 JSON 对象，绝不输出 HTML、Markdown 代码块或解释文字。"
    raw, usage = llm.chat(llm.model_pro, system, build_map_prompt(job, overview, glossary, chapter_maps), json_mode=True)
    job.add_usage(usage)
    data = parse_json_robust(raw, llm, job)
    map_data = normalize_map_data(data, job)
    job.log(f"MAP_DATA 生成完成：{len(map_data['nodes'])} 节点 / {len(map_data['edges'])} 连线 / {len(map_data['steps'])} 步")

    meta_entry = generate_metadata(job, llm, map_data)
    job.log("元数据条目生成完成（MODEL_FLASH）")
    return map_data, meta_entry


def stage4_inject(job: JobState, map_data: dict, meta_entry: dict) -> tuple:
    """阶段4b：注入模板生成 maps/<slug>.html，并上架 books.json。"""
    job.set(stage="inject")
    submaps, chapters = build_submaps_and_chapters(job.slug, job.title)
    map_data["subMaps"] = submaps
    meta_entry["overallStatus"] = "complete"
    meta_entry["chapters"] = chapters
    out = inject_map(job.slug, map_data)
    job.log(f"地图已生成：{out}")
    update_books_json(meta_entry)
    job.log("已上架 books.json（无损合并）")
    return str(out), str(NOTES_DIR / f"{job.slug}.md")


def run_pipeline(job: JobState) -> None:
    """制书流水线主入口（在后台线程中运行）。"""
    llm = LLM()
    if not llm.configured:
        job.set(status="error", error="未配置有效 API_KEY，请检查根目录 .env")
        job.log("错误：未配置有效 API_KEY")
        return
    try:
        job.set(status="running")
        job.log(f"开始制书：{job.title}（{job.slug}）")
        job.log(f"当前计费：{pricing_status()['label']}")

        chapters = stage1_split(job)
        if job.chapter_range:
            chapters = filter_chapters(chapters, job.chapter_range)
            job.log(f"试跑模式：仅处理 {len(chapters)} 章（范围 {job.chapter_range}）")
        job.set(total_chapters=len(chapters))

        stage2_chapters(job, llm, chapters)
        if job.cancel_event.is_set():
            raise JobCancelled()

        if job.chapter_range:
            out = stage2_partial_finish(job)
            job.set(status="done")
            job.result = {
                "chapters": len(chapters),
                "books_json": str(out),
                "map_url": "",
            }
            job.log(f"试跑完成：仅保存 {len(chapters)} 章分章地图，整书总图待全书完成后生成。")
            return

        stage3_overview(job, llm)
        if job.cancel_event.is_set():
            raise JobCancelled()

        map_data, meta_entry = stage4_map(job, llm)
        if job.cancel_event.is_set():
            raise JobCancelled()

        map_path, note_path = stage4_inject(job, map_data, meta_entry)

        job.set(status="done")
        job.result = {
            "map_path": map_path,
            "note_path": note_path,
            "map_url": f"./maps/{job.slug}.html",
        }
        job.log("制书完成。")
    except JobCancelled:
        job.set(status="cancelled")
        job.log("任务已取消，已完成的章节保留在硬盘，可断点续跑。")
    except ScannedBookError as e:
        job.set(status="error", error=str(e))
        job.log(f"错误：{e}")
    except Exception as e:
        job.set(status="error", error=str(e))
        job.log(f"异常：{e}")
        job.log(traceback.format_exc())


load_env()


if __name__ == "__main__":
    job = start_job(
        title=os.getenv("BOOK_TITLE", "测试书"),
        author=os.getenv("BOOK_AUTHOR", "测试作者"),
        slug=os.getenv("BOOK_SLUG", "test-book"),
        chapter_range=os.getenv("CHAPTER_RANGE") or None,
    )
    print(f"job_id: {job.id}")
    while job.status in ("queued", "running"):
        import time as _time

        _time.sleep(2)
        print(job.snapshot()["stage"], job.snapshot()["done_chapters"], "/", job.snapshot()["total_chapters"])
    print(job.snapshot()["status"], job.snapshot()["error"] or job.snapshot()["result"])







