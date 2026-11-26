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
import time
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

# ===== 两阶段学术拆书参数（Pass 1 原著解剖 / Pass 2 图谱建构） =====
# 单章正文不超过该字数时一次解剖；超过则按段落与页码边界分块 Map-Reduce，
# 每片约 PASS1_CHUNK_SIZE 字、相邻重叠 PASS1_CHUNK_OVERLAP 字，绝不静默丢弃后半章。
PASS1_DIRECT_LIMIT = 24000
PASS1_CHUNK_SIZE = 18000
PASS1_CHUNK_OVERLAP = 600

# DeepSeek / OpenAI 兼容接口的深度思考开关注入位置（接口不支持时自动降级）。
THINKING_EXTRA_BODY = {"thinking": {"type": "enabled"}}
THINKING_DISABLED_BODY = {"thinking": {"type": "disabled"}}

# 章节笔记固定的四节小标题，供标题层级矫正与总览取真实章名时排除。
NOTE_SECTIONS = ("核心问题意识", "论证结构与推进", "关键概念界定", "章节衔接与张力")


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


SOURCE_MANIFEST = "source.json"


def source_manifest_path(slug: str) -> Path:
    return WORKSPACE / sanitize_slug(slug) / SOURCE_MANIFEST


def read_source_manifest(slug: str) -> dict:
    """读取该书最近一次上传的原始文件清单（决定 resplit / 生成使用哪份文件）。"""
    path = source_manifest_path(slug)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_source_manifest(slug: str, book_path: Path,
                          inspection: Optional[dict] = None) -> dict:
    """记录当前生效的原始电子书，避免同 slug 下旧 PDF 抢走 EPUB 的目录解析。"""
    slug = sanitize_slug(slug)
    workspace = WORKSPACE / slug
    workspace.mkdir(parents=True, exist_ok=True)
    book_path = Path(book_path)
    try:
        rel = str(book_path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        rel = str(book_path)
    data = {
        "filename": book_path.name,
        "path": rel,
        "format": (inspection or {}).get("format") or split_book.detect_format(book_path),
        "uploaded_at": beijing_now().isoformat(timespec="seconds"),
        "inspection": inspection or {},
    }
    (workspace / SOURCE_MANIFEST).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


def find_raw_book(slug: str) -> Optional[Path]:
    """定位 slug 当前生效的原始电子书。

    优先使用上传清单 source.json；没有清单时取 raw_books/ 中最新的一份，
    不再固定优先 .pdf，避免上传 EPUB 后仍去解析旧 PDF。
    """
    slug = sanitize_slug(slug)
    data = read_source_manifest(slug)
    rel = str(data.get("path") or "").strip()
    if rel:
        candidate = Path(rel)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        if candidate.is_file():
            return candidate
    if not RAW_BOOKS.is_dir():
        return None
    candidates = [
        p for p in RAW_BOOKS.iterdir()
        if p.is_file() and p.stem == slug and p.suffix.lower() in split_book.SUPPORTED_EXTS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def inspect_book(book_path: Path) -> dict:
    """识别电子书格式 / 目录 / 文字层（供上传接口与前端体检卡使用）。"""
    return split_book.inspect_book(book_path)


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


_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _is_transient_llm_error(exc: Exception) -> bool:
    """判定网络超时、限流与 5xx 等可退避重试的临时错误。"""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUS:
        return True
    name = type(exc).__name__.lower()
    for kw in ("timeout", "connection", "internalserver", "ratelimit", "overloaded"):
        if kw in name:
            return True
    msg = str(exc).lower()
    for kw in ("timed out", "timeout", "connection reset", "connection aborted",
               "temporarily unavailable", "overloaded", "502", "503", "504", "429"):
        if kw in msg:
            return True
    return False
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
        self.last_mode = "plain"

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
             max_tokens: Optional[int] = None, thinking: Optional[bool] = None):
        """发起一次对话补全，返回 (文本, usage 字典)。

        候选参数按「response_format + thinking」→「仅 thinking」→「仅 response_format」
        →「两者都不带」逐级降级：JSON 已有 extract_json + parse_json_robust 兜底，
        因此优先保住深度思考；不兼容接口只会降级，不会中断流水线。
        thinking 显式传 False 时注入「disabled」关闭深度思考（供出题/清单等快速结构化生成）。
        网络超时与 5xx 临时错误最多重试 2 次（退避 2s / 5s）。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        base = {"model": model, "messages": messages}
        if temperature is not None:
            base["temperature"] = temperature
        if max_tokens:
            base["max_tokens"] = max_tokens

        want_thinking = (bool(self.enable_thinking) and model == self.model_pro) if thinking is None else bool(thinking)
        candidates = []
        if json_mode and want_thinking:
            candidates = [
                ("json+thinking", True, True),
                ("thinking", False, True),
                ("json", True, False),
                ("plain", False, False),
            ]
        elif json_mode:
            candidates = [("json", True, False), ("plain", False, False)]
        elif want_thinking:
            candidates = [("thinking", False, True), ("plain", False, False)]
        else:
            candidates = [("plain", False, False)]

        last_error = None
        for label, use_json, use_thinking in candidates:
            kwargs = dict(base)
            if use_json:
                kwargs["response_format"] = {"type": "json_object"}
            if use_thinking:
                kwargs["extra_body"] = dict(THINKING_EXTRA_BODY)
            elif thinking is False:
                kwargs["extra_body"] = dict(THINKING_DISABLED_BODY)
            for attempt in range(3):
                try:
                    resp = self.client.chat.completions.create(**kwargs)
                    self.last_mode = label
                    text = (resp.choices[0].message.content or "") if resp.choices else ""
                    usage = getattr(resp, "usage", None)
                    return text, {
                        "prompt": getattr(usage, "prompt_tokens", 0) or 0,
                        "completion": getattr(usage, "completion_tokens", 0) or 0,
                        "total": getattr(usage, "total_tokens", 0) or 0,
                    }
                except Exception as exc:
                    last_error = exc
                    if attempt < 2 and _is_transient_llm_error(exc):
                        time.sleep(2 if attempt == 0 else 5)
                        continue
                    break
        if last_error is not None:
            raise last_error
        raise RuntimeError("LLM 调用失败：未获得任何可用响应")

    def chat_stream(self, model: str, system: str, user: str,
                    temperature: Optional[float] = None, max_tokens: Optional[int] = None):
        """流式对话补全，逐段产出 content 增量（生成器，供内置 AI 问答使用）。

        复用同一 OpenAI 兼容客户端；流式模式不重试（失败由调用方捕获），
        避免 SSE 中断后重复输出。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs = {"model": model, "messages": messages, "stream": True}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if self.enable_thinking and model == self.model_pro:
            kwargs["extra_body"] = dict(THINKING_EXTRA_BODY)
        stream = self.client.chat.completions.create(**kwargs)
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # 推理模型的思考增量走 reasoning_content，正文走 content；内置 AI 只回传正文。
            content = getattr(delta, "content", None)
            if content:
                yield content
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
        self.merged_note_path = None
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


def _title_key(title) -> str:
    return split_book.normalize_title(title)


def _chapter_number_key(title) -> str:
    """提取「第X章 / Chapter N」编号，用于同一章换标题后的匹配。"""
    normalized = split_book.normalize_title(title)
    match = re.match(
        r"^第\s*([0-9一二三四五六七八九十百千零〇两]+)\s*[章节回卷篇部]", normalized
    )
    if match:
        return "zh:" + str(_cn_int(match.group(1)))
    match = re.match(r"^chapter\s*([0-9]+)", normalized, re.IGNORECASE)
    if match:
        return "en:" + str(int(match.group(1)))
    return ""


def _existing_chapter_index(slug: str) -> dict:
    """合并 books.json 与 workspace 清单中的已入藏章节，返回 {order: {order,title}}。"""
    by_order = {}
    for chapter in list_existing_chapters(slug):
        try:
            order = int(chapter.get("index"))
        except (TypeError, ValueError):
            continue
        by_order[order] = {"order": order, "title": str(chapter.get("title") or "")}
    for chapter in existing_chapters_for_slug(slug).get("chapters") or []:
        if not isinstance(chapter, dict) or not isinstance(chapter.get("order"), int):
            continue
        by_order.setdefault(
            int(chapter["order"]),
            {"order": int(chapter["order"]), "title": str(chapter.get("title") or "")},
        )
    return by_order


def plan_chapter_orders(existing: dict, titles: list) -> list:
    """按已入藏章节标题匹配新目录：重复章节原位替换，新章节顺延追加。

    匹配优先级：标题完全一致 -> 章号一致（仅当旧章号唯一）。返回与 titles
    等长的最终章号列表。这样无论上传多少次，同一章只会占用一个章号。
    """
    if not existing:
        return list(range(1, len(titles) + 1))

    by_title = {}
    by_number = {}
    for order, chapter in existing.items():
        title_key = _title_key(chapter.get("title"))
        if title_key:
            by_title.setdefault(title_key, []).append(order)
        number_key = _chapter_number_key(chapter.get("title"))
        if number_key:
            by_number.setdefault(number_key, []).append(order)

    used = set()
    next_new = max(existing) + 1
    orders = []
    for title in titles:
        candidates = [o for o in by_title.get(_title_key(title), []) if o not in used]
        if not candidates:
            number_key = _chapter_number_key(title)
            number_candidates = (
                [o for o in by_number.get(number_key, []) if o not in used]
                if number_key else []
            )
            if len(number_candidates) == 1:
                candidates = number_candidates
        if candidates:
            order = candidates[0]
        else:
            while next_new in used:
                next_new += 1
            order = next_new
            next_new += 1
        used.add(order)
        orders.append(order)
    return orders


def split_precheck(slug: str, book_path: Path, page_map: Optional[str] = None,
                   level: int = 1, offset: int = 0, skip_noncontent: bool = True) -> dict:
    """阶段1：切分电子书为逐章纯文本，并返回格式 / 目录体检结果。

    支持 PDF（含文字层或无文字层的扫描版）、EPUB 与 TXT：
      · 带目录且有文字层 -> 直接按目录拆章（status=ready）
      · 有文字层但无目录 -> 返回 needs_page_map，等待手动目录映射
      · 无文字层（扫描版）-> 抛 ScannedBookError，提示先 OCR
    返回 dict：status / chapters / inspection / skipped / unit / page_count / offset。

    重传同一本书时，按章节标题/章号匹配已入藏章节：重复章节使用原章号原位替换，
    新章节从最大章号之后顺延，避免出现「第一章 + 第五章 第一章」这类重复。
    """
    workspace = WORKSPACE / slug
    chapters_dir = workspace / "chapters"
    notes_dir = workspace / "chapter_notes"
    maps_dir = workspace / "chapter_maps"
    chapter_ir_dir = workspace / "chapter_ir"
    for d in (workspace, chapters_dir, notes_dir, maps_dir, chapter_ir_dir):
        d.mkdir(parents=True, exist_ok=True)
    glossary = workspace / "glossary.md"
    if not glossary.exists():
        glossary.write_text(split_book.GLOSSARY_HEADER, encoding="utf-8")

    existing_before = _existing_chapter_index(slug)
    existing_orders_before = set(existing_before)

    def _resolver(titles):
        return plan_chapter_orders(existing_before, titles)

    result = split_book.split_into_chapters(
        book_path,
        chapters_dir,
        level=level,
        offset=offset,
        skip_noncontent=skip_noncontent,
        map_path=page_map,
        order_resolver=_resolver,
    )
    status = result.get("status")
    unit = result.get("unit") or "页"
    total_units = int(result.get("total_units") or 0)
    inspection = result.get("inspection") or {}
    skipped = result.get("skipped") or []

    if status == "ready":
        chapters = result.get("chapters") or []
        for chapter in chapters:
            order = int(chapter.get("index") or 0)
            chapter["replaces"] = order if order in existing_orders_before else 0
            chapter["is_new"] = order not in existing_orders_before
        return {
            "status": "ready",
            "chapters": chapters,
            "avg_chars_per_page": result.get("avg_chars"),
            "inspection": inspection,
            "skipped": skipped,
            "unit": unit,
            "page_count": total_units,
            "offset": max(0, int(offset or 0)),
        }
    if status == "scanned":
        raise ScannedBookError(result.get("message") or "检测到无文字层的扫描版，请先完成 OCR 转换。")
    if status == "needs_manual_map":
        map_file = ""
        if unit == "页":
            map_file = str(split_book.generate_map_file(workspace, total_units, unit))
        return {
            "status": "needs_page_map",
            "page_count": total_units,
            "unit": unit,
            "inspection": inspection,
            "skipped": skipped,
            "map_file": map_file,
            "chapters": [],
            "message": result.get("message") or "该电子书没有可用目录，请填写目录映射。",
        }
    return {
        "status": "empty",
        "chapters": [],
        "inspection": inspection,
        "unit": unit,
        "page_count": total_units,
        "message": result.get("message") or "未能拆分出任何章节。",
    }


def list_existing_chapters(slug: str) -> list:
    """读取已切分章节，优先 chapters/manifest.json。

    新规范下分章文件只按章号命名（ch01.txt…），标题存放在 manifest.json；
    旧数据（NN_标题.txt）保留兼容读取。
    """
    chapters_dir = WORKSPACE / slug / "chapters"
    if not chapters_dir.is_dir():
        return []
    manifest = split_book.load_chapter_manifest(chapters_dir)
    if manifest:
        chapters = []
        for order in sorted(manifest):
            entry = manifest[order]
            filename = str(entry.get("filename") or split_book.chapter_filename(order))
            text_path = chapters_dir / filename
            if not text_path.exists():
                continue
            text = text_path.read_text(encoding="utf-8")
            chapters.append({
                "index": int(order),
                "filename": filename,
                "title": str(entry.get("title") or f"第{order}章"),
                "chars": len(text),
                "pages": int(entry.get("pages") or 0),
            })
        if chapters:
            return chapters

    chapters = []
    for f in sorted(chapters_dir.glob("*.txt"), key=natural_key):
        stem = f.stem
        canonical = re.match(r"^ch(\d+)$", stem, re.IGNORECASE)
        legacy = re.match(r"^(\d+)_(.*)$", stem)
        if canonical:
            index = int(canonical.group(1))
            title = f"第{index}章"
        elif legacy:
            index = int(legacy.group(1))
            title = legacy.group(2).replace("_", " ").strip()
        else:
            continue
        text = f.read_text(encoding="utf-8")
        chapters.append({
            "index": index or (len(chapters) + 1),
            "filename": f.name,
            "title": title or "未命名章节",
            "chars": len(text),
            "pages": 0,
        })
    chapters.sort(key=lambda chapter: chapter["index"])
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
    inspection = inspect_book(book)
    toc_desc = (
        f"目录 {inspection.get('toc_count')} 条"
        if inspection.get("has_toc") else "无可用目录"
    )
    job.log(
        f"识别原始文件：{book.name}（{inspection.get('format_label')}，{toc_desc}，"
        f"{inspection.get('total_units')} {inspection.get('unit')}）"
    )
    result = split_precheck(job.slug, book)
    if result["status"] == "needs_page_map":
        raise RuntimeError(
            "该电子书没有可用目录，请先通过 /api/books/resplit 提交目录映射"
            "（或改传带目录的 EPUB / TXT）。"
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


STYLE_REDLINES = (
    "\n\n【全站文风红线（Pass 1 与 Pass 2 共同遵守）】\n"
    "1. 使用简单平和的书面中文，直接主谓宾陈述句，忠于原稿，不寒暄、不抒情。\n"
    "2. 严禁使用「不是……而是……」「并非……而是……」句式。\n"
    "3. 严禁防御性表述（「需要指出的是」「不可否认」「在某种程度上」「众所周知」等）。\n"
    "4. 严禁比喻、口语寒暄与空洞套话（「影响深远」「具有重要意义」「有着深刻启示」等）。\n"
    "5. 术语首次出现给出原文括注，后文保持同一译名。\n"
)


def chapter_heading(ch: dict) -> str:
    """章节标题：标题已含「第X章」时直接复用，避免拼出「第2章 第二章」。"""
    title = str(ch.get("title") or "").strip()
    index = int(ch.get("index") or 0)
    if not title:
        return f"第{index}章"
    if re.match(r"^第\s*[0-9一二三四五六七八九十百千零〇两]+\s*章", title):
        return title
    return f"第{index}章 {title}"


def _chapter_context(ch: dict, job: JobState) -> dict:
    ctx = dict(ch)
    ctx["book_title"] = job.title
    ctx["book_author"] = job.author
    return ctx


def _page_range_of(text: str) -> str:
    pages = re.findall(r"\[\[p\.(\d+)\]\]", text or "")
    if not pages:
        return ""
    if pages[0] == pages[-1]:
        return f"p.{pages[0]}"
    return f"p.{pages[0]}-p.{pages[-1]}"


def chunk_chapter_text(text: str, size: int = PASS1_CHUNK_SIZE,
                       overlap: int = PASS1_CHUNK_OVERLAP) -> list:
    """按页码锚点与段落边界把长章切为约 size 字的片段，相邻保留 overlap 字重叠。"""
    text = str(text or "")
    if not text:
        return []
    if len(text) <= size:
        return [text]

    units = []
    buf = []
    for line in text.splitlines(keepends=True):
        if line.startswith("[[p."):
            if buf:
                units.append("".join(buf))
                buf = []
            units.append(line)
        else:
            buf.append(line)
    if buf:
        units.append("".join(buf))

    flat = []
    for unit in units:
        if len(unit) <= size:
            flat.append(unit)
        else:
            for i in range(0, len(unit), size):
                flat.append(unit[i:i + size])

    chunks = []
    current = ""
    for unit in flat:
        if current and len(current) + len(unit) > size:
            chunks.append(current)
            current = (current[-overlap:] if overlap > 0 else "") + unit
        else:
            current += unit
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]
GLOSSARY_HEADER = split_book.GLOSSARY_HEADER


def _canonical_term(term) -> str:
    """术语去重键：去掉括注与空白并转小写，「本原（arche）」与「本原」视为同一术语。"""
    s = str(term or "").strip()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


_CN_DIGITS = "零一二三四五六七八九"


def _cn_int(s: str) -> int:
    """把中文/阿拉伯章节序号转为整数（用于术语表章节出处去重与排序）。"""
    s = str(s).strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.endswith("十"):
        return (_CN_DIGITS.index(s[0]) if s[0] in _CN_DIGITS else 0) * 10
    if "十" in s:
        a, b = s.split("十", 1)
        return (_CN_DIGITS.index(a) if a in _CN_DIGITS else 1) * 10 + (_CN_DIGITS.index(b) if b in _CN_DIGITS else 0)
    v = 0
    for ch in s:
        if ch in _CN_DIGITS:
            v = v * 10 + _CN_DIGITS.index(ch)
    return v


def _parse_glossary_md(path: Path) -> list:
    """从既有 glossary.md 表格回填结构化术语，首次升级时不丢旧章术语。"""
    entries = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        term, chapters, definition = cells[0], cells[1], cells[2]
        if not term or term == "核心概念":
            continue
        chapter_list = []
        for part in re.split(r"[、,，;；\s]+", chapters):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^第\s*([0-9零一二三四五六七八九十百千〇两]+)\s*章$", part)
            chapter_list.append({
                "index": _cn_int(m.group(1)) if m else 0,
                "title": part,
                "definition": "",
            })
        key = _canonical_term(term)
        existing = next((e for e in entries if _canonical_term(e.get("term")) == key), None)
        if existing is not None:
            for c in chapter_list:
                dup = any(int(x.get("index") or 0) == int(c.get("index") or 0) and x.get("title") == c.get("title") for x in existing["chapters"])
                if not dup:
                    existing["chapters"].append(c)
            if definition and definition != existing.get("definition"):
                note = f"{chapters}：{definition}".strip("：")
                if note and note not in existing["evolution"]:
                    existing["evolution"].append(note)
            continue
        entries.append({
            "term": term,
            "original": "",
            "definition": definition,
            "scope": "",
            "related_existing_terms": [],
            "chapters": chapter_list,
            "evolution": [],
        })
    return entries


def load_glossary_entries(workspace: Path) -> list:
    """读取 glossary.json；缺失时从 glossary.md 表格回填。"""
    json_path = workspace / "glossary.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict)]
        except Exception:
            pass
    return _parse_glossary_md(workspace / "glossary.md")


def save_glossary_entries(workspace: Path, entries: list) -> Path:
    out = workspace / "glossary.json"
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _chapter_label(index: int, title: str) -> str:
    title = str(title or "").strip()
    if not title:
        return f"第{index}章" if index else ""
    if re.match(r"^第\s*[0-9一二三四五六七八九十百千零〇两]+\s*章", title):
        return title
    return f"第{index}章 {title}".strip() if index else title


def merge_glossary_entries(entries: list, new_items, chapter: dict) -> list:
    """按规范术语名去重合并：同章重跑覆盖更新该章条目，跨章出现合并章节与演进说明。"""
    index_by_key = {}
    for i, entry in enumerate(entries):
        index_by_key.setdefault(_canonical_term(entry.get("term")), i)
    idx = int(chapter.get("index") or 0)
    label = _chapter_label(idx, chapter.get("title", ""))
    for item in (new_items or []):
        if isinstance(item, str):
            term, _, definition = str(item).partition("|")
            item = {"term": term, "definition": definition}
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", "")).strip()
        if not term:
            continue
        key = _canonical_term(term)
        definition = str(item.get("definition", "")).strip()
        scope = str(item.get("scope", "")).strip()
        original = str(item.get("original", "")).strip()
        related = [str(r).strip() for r in (item.get("related_existing_terms") or []) if str(r).strip()]

        entry = entries[index_by_key[key]] if key in index_by_key else None
        if entry is None:
            entry = {
                "term": term,
                "original": original,
                "definition": definition,
                "scope": scope,
                "related_existing_terms": [],
                "chapters": [],
                "evolution": [],
            }
            index_by_key[key] = len(entries)
            entries.append(entry)
        else:
            if original and not str(entry.get("original") or "").strip():
                entry["original"] = original
            if scope and not str(entry.get("scope") or "").strip():
                entry["scope"] = scope
            if definition and not str(entry.get("definition") or "").strip():
                entry["definition"] = definition

        for r in related:
            if r not in entry["related_existing_terms"]:
                entry["related_existing_terms"].append(r)

        chapters = entry.setdefault("chapters", [])
        same = None
        if idx:
            same = next((c for c in chapters if int(c.get("index") or 0) == idx), None)
        if same is None:
            chapters.append({"index": idx, "title": label, "definition": definition})
        else:
            same["title"] = label
            if definition:
                same["definition"] = definition
        if len(chapters) > 1 and definition and definition != str(entry.get("definition") or "").strip():
            note = f"{label}：{definition}"
            evolution = entry.setdefault("evolution", [])
            if note not in evolution:
                evolution.append(note)
    return entries


def render_glossary_md(workspace: Path, entries: list) -> Path:
    """按结构化术语列表重写 glossary.md 表格（每个术语只保留一行）。"""
    lines = [GLOSSARY_HEADER.rstrip("\n"), ""]
    for entry in entries:
        term = str(entry.get("term", "")).strip()
        if not term:
            continue
        labels = []
        for c in entry.get("chapters") or []:
            label = _chapter_label(int(c.get("index") or 0), c.get("title", ""))
            if label and label not in labels:
                labels.append(label)
        definition = str(entry.get("definition", "")).strip()
        evolution = []
        for item in (entry.get("evolution") or []):
            item = str(item).strip()
            if item and item not in evolution:
                evolution.append(item)
        if evolution:
            extra = "；".join(evolution)
            definition = f"{definition}；{extra}" if definition else extra
        lines.append(f"| {term} | {'、'.join(labels)} | {definition} |")
    out = workspace / "glossary.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
def build_pass1_system() -> str:
    return load_style_system() + STYLE_REDLINES + (
        "\n\n【本阶段职责】忠实精读原著：只还原原文的论证关节，不画图，不补充原文未提及的外部学术史。\n"
        "每次只输出一个 JSON 对象，不要输出 JSON 之外的任何文字、注释或代码块。\n"
    )


def build_pass1_prompt(ch: dict, text: str, glossary_terms: list) -> str:
    terms = "、".join([t for t in (glossary_terms or []) if t][:80]) or "（暂无）"
    return (
        "请精读下面这一章，按六条铁律输出一个 JSON 对象（不要输出 JSON 之外的文字）。\n\n"
        f"书名：{ch.get('book_title', '')}\n"
        f"作者：{ch.get('book_author', '')}\n"
        f"章号：{ch['index']:02d}\n"
        f"章名：{ch['title']}\n\n"
        f"【全书已有术语名清单】\n{terms}\n\n"
        "【六条铁律】\n"
        "1. 还原推演关节：先写反对什么问题，再写用什么机制推导，最后写暴露何种内在两难。不要写成结论清单。\n"
        "2. 区分两类言说：被讨论思想家或核心对象的原论，与本书作者或原文引述的评述、批判，必须分开记录。\n"
        "3. 严禁脑补关系：verified_relations 只记录原文明确交代关系的双方，并在 evidence 摘录原文依据。\n"
        "   关系类型分两类：\n"
        "   · 跨学派/跨阵营关系：批评、继承、综合、对立、辩护、回应、平行对照；\n"
        "   · 同体系/同思想家内部逻辑关系：前提奠基、推论展开、方法支撑、极端推进、归结收束。\n"
        "   当原文围绕同一思想家或同一思潮展开多个命题、原则或方法时（如苏格拉底的「认识你自己 → 德性即知识 / 助产术与反诘」，"
        "或智者内部的「自然与约定 → 相对主义 → 怀疑论 → 危机」），必须提取它们在原文中的逻辑推导、方法支撑或因果收束关系并附原文依据，"
        "不得只抽跨学派冲突。原文仅平行阐述者只能标为「平行对照」，严禁标为「继承」或「转向」。\n"
        "4. 静默清洗明显 OCR 乱码与页眉页码；正文因扫描缺字导致论证残缺时标注【原文OCR残缺】；"
        "原文含 [[p.XX]] 页码标记时，在依据中保留页码线索。\n"
        "5. note 从 ### 标题起步，固定四节：### 核心问题意识、### 论证结构与推进、"
        "### 关键概念界定、### 章节衔接与张力。\n"
        "6. 属种边界保真：当原文明确交代了「上位总称与下位子派/独立成员的包含关系」，或「某几位思想家仅在分析上归为同一大类、历史上并无直接师承」时，"
        "必须如实记入 units 各对象的 parent_group（上位学派、总思潮、大流派或上位概念框架）与 sub_group（下位子学派、细分流派或独立身份），"
        "严禁在扁平化提取时抹掉这层属种区分；原文无上位或细分归属时分别留空字符串 \"\"。\n\n"
        "【输出 JSON 字段】\n"
        "{\n"
        '  "chapter_core_question": "2-3 句交代本章核心问题",\n'
        '  "note": "Markdown 精读正文，从 ### 核心问题意识 开始，固定四节",\n'
        '  "glossary": [{"term": "...", "original": "...", "definition": "...", "scope": "...", "related_existing_terms": []}],\n'
        '  "units": [{"name": "...", "parent_group": "...", "sub_group": "...", "problem_addressed": "...", "core_argument": "...", "key_distinctions": [], "internal_tensions": [], "author_comments": [], "primary_sources": []}],\n'
        '  "verified_relations": [{"from": "...", "to": "...", "relation_type": "批评|继承|综合|对立|辩护|回应|平行对照|前提奠基|推论展开|方法支撑|极端推进|归结收束", "evidence": "原文依据摘录"}],\n'
        '  "chapter_framework": "本章是否存在贯穿全局的分类轴或逻辑框架，没有则为空字符串"\n'
        "}\n\n"
        f"【本章原文】\n{text}\n"
    )


def build_pass1_chunk_prompt(ch: dict, chunk_text: str, page_range: str, part: int, total: int) -> str:
    return (
        f"下面是第 {ch['index']:02d} 章（{ch['title']}）的第 {part}/{total} 片原文"
        f"{('，页码范围 ' + page_range) if page_range else ''}。\n"
        "请只处理这一片，输出一个 JSON 对象，不要输出 JSON 之外的文字。\n\n"
        "【铁律】\n"
        "1. 只忠实还原本片原文的论证关节，不补充外部学术史，不画图。\n"
        "2. 区分原论与作者评述；verified_relations 只记录原文明确交代的关系并摘录依据，"
        "既包括跨学派/跨阵营的批评、继承、综合、对立、辩护、回应、平行对照，也包括同体系内部的"
        "前提奠基、推论展开、方法支撑、极端推进、归结收束等逻辑推进关系。\n"
        "3. 静默清洗 OCR 乱码与页眉页码；论证残缺处标注【原文OCR残缺】；保留 [[p.XX]] 页码线索。\n"
        "4. note_fragment 从 ### 标题起步，只写本片涉及的小节，不要重复章标题。\n"
        "5. 属种边界保真：如实记入 units 各对象的 parent_group（上位学派/总思潮/大流派/上位概念框架）与 sub_group（下位子学派/细分流派/独立身份），"
        "保留上位归属与下位细分，不得在扁平化提取时抹掉属种区分；原文无上位或细分归属时分别留空字符串 \"\"。\n\n"
        "【输出 JSON 字段】\n"
        "{\n"
        '  "page_range": "本片页码范围",\n'
        '  "chapter_core_question": "本片涉及的核心问题",\n'
        '  "note_fragment": "本片对应的 Markdown 小节",\n'
        '  "glossary": [{"term": "...", "original": "...", "definition": "...", "scope": "...", "related_existing_terms": []}],\n'
        '  "units": [{"name": "...", "parent_group": "...", "sub_group": "...", "problem_addressed": "...", "core_argument": "...", "key_distinctions": [], "internal_tensions": [], "author_comments": [], "primary_sources": []}],\n'
        '  "verified_relations": [{"from": "...", "to": "...", "relation_type": "批评|继承|综合|对立|辩护|回应|平行对照|前提奠基|推论展开|方法支撑|极端推进|归结收束", "evidence": "..."}],\n'
        '  "chapter_framework": "本片是否出现贯穿全局的分类轴"\n'
        "}\n\n"
        f"【本片原文】\n{chunk_text}\n"
    )


def build_pass1_merge_prompt(ch: dict, partials: list) -> str:
    return (
        f"下面是第 {ch['index']:02d} 章（{ch['title']}）分片精读得到的 {len(partials)} 份中间结果。\n"
        "请把它们合并成一份完整的本章解剖 JSON，只输出 JSON，不要输出其他文字。\n\n"
        "【合并规则】\n"
        "1. 只使用中间结果已有的事实，不得引入其中未出现的内容。\n"
        "2. units 按原文顺序合并去重，保留全部页码线索。\n"
        "3. glossary 同一术语只保留一条，合并界定与出处。\n"
        "4. verified_relations 去重，保留每条的原文依据；既保留跨学派/跨阵营关系，也保留同体系内部的"
        "前提奠基、推论展开、方法支撑、极端推进、归结收束等逻辑推进关系，不得只保留跨学派冲突。\n"
        "5. note 合并全部 note_fragment，从 ### 标题起步，固定四节："
        "### 核心问题意识、### 论证结构与推进、### 关键概念界定、### 章节衔接与张力；"
        "不得只保留前半章内容。\n"
        "6. 合并 units 时必须完整保留并统一每个单元的 parent_group（上位学派/总思潮/上位概念框架）与 sub_group（下位子学派/细分流派/独立身份），"
        "不得丢失任何单元的层级归属；同一上位归属在不同分片中的不同表述应统一为一致名称。\n\n"
        "【输出 JSON 字段】\n"
        "{\n"
        '  "chapter_core_question": "2-3 句交代本章核心问题",\n'
        '  "note": "Markdown 精读正文，固定四节",\n'
        '  "glossary": [...],\n'
        '  "units": [...],\n'
        '  "verified_relations": [...],\n'
        '  "chapter_framework": "..."\n'
        "}\n\n"
        "【分片中间结果】\n"
        + json.dumps(partials, ensure_ascii=False, indent=1)
        + "\n"
    )


def normalize_note_markdown(note: str) -> str:
    """把 Pass 1 的 note 规范为「### 四节」结构，去掉重复章标题与越级标题。"""
    text = str(note or "").strip()
    if not text:
        return "### 核心问题意识\n\n【原文OCR残缺】本章精读笔记缺失。\n"
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,2}\s*第\s*[0-9一二三四五六七八九十百千零〇两]+\s*章", stripped):
            continue
        if re.match(r"^#{1,2}\s+\S", stripped) and not stripped.startswith("###"):
            line = re.sub(r"^#{1,2}\s+", "### ", line)
        lines.append(line)
    body = "\n".join(lines).strip()
    for section in NOTE_SECTIONS:
        if not re.search(rf"^###\s*{section}", body, flags=re.MULTILINE):
            body += f"\n\n### {section}\n\n本章原文未明确交代。"
    return body.strip()


def run_pass1(job: JobState, llm: LLM, ch: dict, text: str, glossary_terms: list) -> dict:
    """Pass 1：原著解剖。短章直接解剖，长章分块 Map-Reduce 后合并，绝不截断。"""
    system = build_pass1_system()
    if len(text) <= PASS1_DIRECT_LIMIT:
        job.log(f"  Pass 1 直接解剖：{len(text)} 字（阈值 {PASS1_DIRECT_LIMIT} 字）")
        raw, usage = llm.chat(llm.model_pro, system,
                              build_pass1_prompt(ch, text, glossary_terms), json_mode=True)
        job.add_usage(usage)
        job.log(f"  Pass 1 调用模式：{llm.last_mode}")
        return parse_json_robust(raw, llm, job)

    chunks = chunk_chapter_text(text)
    job.log(
        f"  Pass 1 分块 Map-Reduce：{len(text)} 字 -> {len(chunks)} 片"
        f"（每片约 {PASS1_CHUNK_SIZE} 字，重叠 {PASS1_CHUNK_OVERLAP} 字）"
    )
    partials = []
    for i, chunk in enumerate(chunks, start=1):
        if job.cancel_event.is_set():
            raise JobCancelled()
        page_range = _page_range_of(chunk)
        job.log(f"  Pass 1 分块 {i}/{len(chunks)}：{page_range or '无页码锚点'}")
        raw, usage = llm.chat(
            llm.model_pro, system,
            build_pass1_chunk_prompt(ch, chunk, page_range, i, len(chunks)),
            json_mode=True,
        )
        job.add_usage(usage)
        data = parse_json_robust(raw, llm, job)
        if isinstance(data, dict):
            data.setdefault("page_range", page_range)
            partials.append(data)
    if not partials:
        raise RuntimeError(f"第 {ch['index']} 章分块精读未获得任何中间结果")
    job.log(f"  Pass 1 合并 {len(partials)} 片中间结果")
    raw, usage = llm.chat(llm.model_pro, system,
                          build_pass1_merge_prompt(ch, partials), json_mode=True)
    job.add_usage(usage)
    job.log(f"  Pass 1 合并调用模式：{llm.last_mode}")
    return parse_json_robust(raw, llm, job)
def build_pass2_system() -> str:
    return load_style_system() + STYLE_REDLINES + (
        "\n\n【本阶段职责】把 Pass 1 的原著解剖抽象为本章知识图谱，并补充原书未展开的学术延伸。\n"
        "唯一原著事实源是 Pass 1 的 chapter_ir JSON，严禁超出它虚构原著内容。\n"
        "每次只输出一个 JSON 对象，不要输出 JSON 之外的任何文字、注释或代码块。\n"
    )


def build_pass2_prompt(ch: dict, ir: dict, book_anchors: list,
                       other_books: list, glossary_entries: list) -> str:
    anchors = json.dumps(book_anchors[-6:], ensure_ascii=False, indent=1) if book_anchors else "（本章是本次处理的第一章，暂无前章锚点）"
    books = json.dumps(other_books[:8], ensure_ascii=False, indent=1) if other_books else "（书架暂无其他书目）"
    terms = "、".join([str(e.get("term", "")) for e in glossary_entries if e.get("term")][:80]) or "（暂无）"
    return (
        "请基于下面的 Pass 1 原著解剖，建构本章知识图谱并输出一个 JSON 对象。\n\n"
        f"章号：{ch['index']:02d}\n章名：{ch['title']}\n\n"
        f"【Pass 1 原著解剖 JSON（唯一原著事实源）】\n{json.dumps(ir, ensure_ascii=False, indent=1)}\n\n"
        f"【本书已完成章节的精简锚点】\n{anchors}\n\n"
        f"【书架其他书目（用于跨书锚点）】\n{books}\n\n"
        f"【全书术语名】\n{terms}\n\n"
        "【九条硬规范】\n"
        "1. 禁止按教材小节 1:1 排流水账：节点控制在 7-10 个；递进紧密的早期探索合并为高密度演进节点，"
        "具有独立理论转折意义者拆分为独立节点。\n"
        "2. 严格遵守表层字数红线：title 严格 ≤12 字；brief 严格 ≤10 个汉字（确保单行显示，如「知善必行、无人作恶」「承认无知与反诘」，防止「01 · 起点」卡片折行切字），"
        "禁止写完整长句，禁止与 summary 重复；coreIdea 20-40 字（一句话论证判断）；"
        "summary 60-120 字（交代困境 → 推演机制 → 理论落点）。\n"
        "3. subPoints 每节点 3-4 条，只使用四个语义槽位：核心论证、关键区分、内在两难、原典锚点；"
        "只要 Pass 1 提取出内在矛盾，必须输出「内在两难」。\n"
        "4. perspectivesNote 每节点 2-3 条，严禁留空数组，只用三层来源标签：\n"
        "   【原书评述】严格取自 Pass 1 的 author_comments，保留评价主体；\n"
        "   【学术延伸】补充原书未展开但具解释力的学术史脉络、后世哲学家或学者回应、理论争论，"
        "40-80 字，必须具名学者或具体概念，绝不混充原著；\n"
        "   【跨章锚点】或【跨书锚点】与前章或书架其他书目呼应，写明概念演进或张力。\n"
        "5. 防伪边与双视角铁律：每条 edge 必须严格对应 Pass 1 的 verified_relations；"
        "无直接师承关系者严禁使用「转向」「继承」「发展」，改用「立场对立」「平行对比」或指向真实共同影响对象；"
        "除 all 外必须提炼 2 个贯穿本章的分析视角，分别对应 lineClass 的 primary 与 secondary，"
        "并把大多数 edge 的 perspective 精准归类为对应视角 id；\"both\" 仅可用于真正横跨两个分析视角的交叉桥接边，"
        "完全发生在同一学派或同一思想家内部的逻辑推导边（如同一哲学家的不同核心命题之间）一律归入该方专属视角，严禁标为 \"both\"，"
        "且 \"both\" 不超过总边数的 25%，以保证两个视角切换有明显区分度；任意两个节点之间在同一方向（相同 source 与 target）"
        "最多只允许建立 1 条边，存在复合关系（如同时包含批评与继承）时合并为 1 条边并在 label（如「批判与继承」）与 detail 中一并说明；"
        "严禁输出 x/y 坐标，curve 一律填 0。\n"
        "6. 必须输出 readingGuide（coreQuestion 为本章核心问题，recommendedPath 给 3-5 个节点）"
        "与 4-5 步逻辑推演 steps。\n"
        "7. 当 Pass 1 的 chapter_framework 非空时，在 7-10 个节点上限内优先保留一个「框架收束」节点，"
        "把该分类轴或逻辑框架落为一个贯穿全章的收束性判断。\n"
        "8. 零孤立节点硬规则：图谱严禁出现没有任何连线的孤立节点；每个节点至少应有 1 条有原文依据的连线，"
        "既包括跨学派交锋，也包括同一思想体系内部的逻辑推进与方法支撑（如苏格拉底「认识你自己 → 德性即知识 → 助产术与反诘」、"
        "智者「自然与约定 → 相对主义 → 怀疑论 → 危机」）；仍严禁杜撰无依据的跨学派历时师承。\n"
        "9. 属种命名不越界与集合外延自检：\n"
        "   a. 只有当单个节点完整囊括了该 parent_group 在本章的全部核心成员或子派时，才允许使用 parent_group 的上位总称作为该节点 title；\n"
        "   b. 一旦同一个 parent_group（同一上位学派、思潮或总概念）被拆分到 2 个及以上并列节点中，严禁让其中任何一个局部节点在 title 里独占上位总称，"
        "每个并列节点必须改用各自的 sub_group、具体思想家名或精确子论题命名，并在 summary 或 perspectivesNote 中交代它们共同隶属的 parent_group；\n"
        "   c. 输出 JSON 前必须横向复查所有并列节点的 title，确保任意两个并列节点之间不存在「一个占用了上位总名、另一个实为其下位成员」的属种错位。\n\n"
        "【节点 category 要求】category 为 4-8 字的「理论结构定位」，刻画该节点在论证结构中承担的角色，"
        "例如「感性始基」「逻辑本体」「微观折中」「问题框架」；"
        "严禁使用「学说」「概念」「论点」「理论」这类泛称。\n\n"
        "【输出 JSON 结构】\n"
        "{\n"
        f'  "chapter_id": "{ch["index"]:02d}",\n'
        f'  "chapter_title": "{ch["title"]}",\n'
        '  "chapter_core_question": "...",\n'
        '  "meta": {"title": "...", "subtitle": "...", "overview": "..."},\n'
        '  "perspectives": [\n'
        '    {"id": "all", "label": "全景互锁", "legendLabel": "本章结构", "lineClass": "both"},\n'
        '    {"id": "...", "label": "...", "legendLabel": "...", "lineClass": "primary"},\n'
        '    {"id": "...", "label": "...", "legendLabel": "...", "lineClass": "secondary"}\n'
        "  ],\n"
        '  "readingGuide": {"coreQuestion": "...", "startNodeId": "n1", "recommendedPath": [{"id": "n1", "label": "..."}]},\n'
        '  "nodes": [{"id": "n1", "category": "...", "title": "...", "brief": "...", "coreIdea": "...", "summary": "...",\n'
        '             "subPoints": [{"label": "核心论证", "text": "..."}],\n'
        '             "perspectivesNote": [{"label": "【原书评述】", "text": "..."}]}],\n'
        '  "edges": [{"id": "e1", "source": "n1", "target": "n2", "perspective": "both", "label": "...", "curve": 0, "labelT": 0.5, "detail": "..."}],\n'
        '  "steps": [{"title": "步骤 1 / N：...", "description": "...", "activeNodes": ["n1"], "activeEdges": ["e1"]}]\n'
        "}\n"
    )


def ensure_perspectives_notes(data: dict, ir: dict, book_anchors: list,
                              scope_word: str = "本章") -> tuple:
    """补填缺失的 perspectivesNote：只用 Pass 1 原书评述与前章锚点，绝不虚构学者。"""
    nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
    units = [u for u in (ir.get("units") or []) if isinstance(u, dict)]
    core_question = str(ir.get("chapter_core_question", "")).strip()
    cross_text = ""
    if book_anchors:
        prev = book_anchors[-1]
        prev_title = str(prev.get("chapter_title", "")).strip()
        prev_nodes = "、".join([t for t in (prev.get("nodes") or []) if t][:3])
        if prev_title or prev_nodes:
            cross_text = f"承接{prev_title}的{prev_nodes}，本章把它们推进到新的问题层面。"
    filled = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        notes = node.get("perspectivesNote")
        if isinstance(notes, list) and any(
            isinstance(x, dict) and str(x.get("text", "")).strip() for x in notes
        ):
            continue
        title = str(node.get("title", "")).strip()
        notes = []
        comment = ""
        for unit in units:
            comments = unit.get("author_comments") or []
            if isinstance(comments, str):
                comments = [comments]
            comments = [str(c).strip() for c in comments if str(c).strip()]
            if not comments:
                continue
            if title and title in str(unit.get("name", "")):
                comment = comments[0]
                break
            if not comment:
                comment = comments[0]
        if comment:
            notes.append({"label": "【原书评述】", "text": comment})
        else:
            source = ""
            for unit in units:
                sources = unit.get("primary_sources") or []
                if isinstance(sources, str):
                    sources = [sources]
                sources = [str(s).strip() for s in sources if str(s).strip()]
                if sources:
                    source = sources[0]
                    break
            text = f"原文出处：{source}" if source else f"{scope_word}原文对该节点只作平行阐述，未给出明确评价。"
            notes.append({"label": "【原书评述】", "text": text})
        anchor = cross_text or (
            f"与{scope_word}核心问题「{core_question[:40]}」互为支点。" if core_question
            else f"与{scope_word}核心问题互为支点，等待后续章节补全。"
        )
        notes.append({"label": "【跨章锚点】", "text": anchor})
        node["perspectivesNote"] = notes
        filled += 1
    return data, filled


def _pass2_ready(map_path: Path) -> bool:
    """Pass 2 断点判据：地图已含非空 steps 或 2 个以上 perspectives。"""
    if not map_path.exists():
        return False
    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    steps = data.get("steps")
    if isinstance(steps, list) and len(steps) > 0:
        return True
    perspectives = data.get("perspectives")
    if isinstance(perspectives, list):
        return len([p for p in perspectives if isinstance(p, dict)]) >= 2
    return False


def _ir_usable(ir) -> bool:
    """chapter_ir 是否包含可用的原著解剖内容（防止空壳 IR 让 Pass 2 空转）。"""
    if not isinstance(ir, dict):
        return False
    if str(ir.get("note") or "").strip():
        return True
    units = ir.get("units")
    return isinstance(units, list) and bool(units)


def _read_note_as_ir(note_path: Path, ch: dict) -> dict:
    body = note_path.read_text(encoding="utf-8-sig").strip() if note_path.exists() else ""
    return {
        "chapter_core_question": "",
        "note": body or f"### 核心问题意识\n\n【原文OCR残缺】第{ch['index']}章笔记缺失。",
        "glossary": [],
        "units": [],
        "verified_relations": [],
        "chapter_framework": "",
    }


def _chapter_anchor(ch: dict, cmap: dict, glossary_entries: list) -> dict:
    """本章精简锚点：只保留章号、节点标题与术语，供下一章 Pass 2 做跨章呼应。"""
    titles = []
    for n in (cmap.get("nodes") or []):
        if isinstance(n, dict):
            t = str(n.get("title", "")).strip()
            if t and t not in titles:
                titles.append(t)
    idx = int(ch.get("index") or 0)
    terms = []
    for entry in glossary_entries:
        for c in (entry.get("chapters") or []):
            if int(c.get("index") or 0) == idx:
                term = str(entry.get("term", "")).strip()
                if term and term not in terms:
                    terms.append(term)
                break
    return {
        "chapter_id": f"{idx:02d}",
        "chapter_title": str(ch.get("title", "")).strip(),
        "nodes": titles[:10],
        "terms": terms[:10],
    }


def run_pass2(job: JobState, llm: LLM, ch: dict, ir: dict,
              book_anchors: list, glossary_entries: list) -> dict:
    """Pass 2：图谱建构与学术延伸，输出本章 MAP_DATA 原始结构。"""
    other_books = []
    for book in load_books():
        if not isinstance(book, dict):
            continue
        if str(book.get("id") or "") == job.slug:
            continue
        other_books.append({
            "title": str(book.get("title", "")).strip(),
            "tags": book.get("tags") or [],
            "summary": str(book.get("summary", "")).strip()[:120],
        })
    prompt = build_pass2_prompt(ch, ir, book_anchors, other_books, glossary_entries)
    raw, usage = llm.chat(llm.model_pro, build_pass2_system(), prompt, json_mode=True)
    job.add_usage(usage)
    job.log(f"  Pass 2 调用模式：{llm.last_mode}")
    data = parse_json_robust(raw, llm, job)
    if not isinstance(data, dict):
        data = {}
    data, filled = ensure_perspectives_notes(data, ir, book_anchors)
    if filled:
        job.log(f"  Pass 2 兜底补填 perspectivesNote：{filled} 个节点")
    return data
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


def warn_taxonomy_naming(ir: dict, map_data: dict, job: JobState) -> None:
    """只读告警：检测 Pass 1 的属种层级在图谱节点命名中是否越界。

    统计 Pass 1 units 中同一 parent_group 下被拆成多个不同 sub_group/单元的集合；
    若某 parent_group 的成员在图谱中被拆成多个节点，而其中某个节点的 title 直接
    包含了该 parent_group 名称（疑似独占上位总称），仅记录日志警告，不抛异常、
    不强行改写节点标题。
    """
    units = [u for u in (ir.get("units") or []) if isinstance(u, dict)]
    nodes = [n for n in (map_data.get("nodes") or []) if isinstance(n, dict)]
    if not units or not nodes:
        return

    pg_to_children = {}
    for u in units:
        pg = str(u.get("parent_group") or "").strip()
        if not pg:
            continue
        child = str(u.get("sub_group") or u.get("name") or "").strip()
        pg_to_children.setdefault(pg, set())
        if child:
            pg_to_children[pg].add(child)

    for pg, children in pg_to_children.items():
        if len(children) < 2:
            continue
        title_hits = [n["id"] for n in nodes if pg and pg in str(n.get("title") or "")]
        if not title_hits:
            continue
        sibling_hits = []
        for n in nodes:
            if n.get("id") in title_hits:
                continue
            title = str(n.get("title") or "").strip()
            if any(c and c in title for c in children):
                sibling_hits.append(n["id"])
        if sibling_hits:
            job.log(
                f"[校验警告] 疑似属种命名越界：上位总称「{pg}」对应多个下位成员，"
                f"节点 {'、'.join(title_hits)} 疑似独占上位名，并列成员节点 {'、'.join(sibling_hits)} 未归并于其中。"
            )


def stage2_chapters(job: JobState, llm: LLM, chapters: list) -> None:
    """阶段2：逐章两阶段学术拆书（Pass 1 原著解剖 → Pass 2 图谱建构），断点续跑。"""
    job.set(stage="chapters")
    workspace = WORKSPACE / job.slug
    chapters_dir = workspace / "chapters"
    notes_dir = workspace / "chapter_notes"
    maps_dir = workspace / "chapter_maps"
    ir_dir = workspace / "chapter_ir"
    for d in (notes_dir, maps_dir, ir_dir):
        d.mkdir(parents=True, exist_ok=True)
    glossary_path = workspace / "glossary.md"
    if not glossary_path.exists():
        glossary_path.write_text(split_book.GLOSSARY_HEADER, encoding="utf-8")
    glossary_entries = load_glossary_entries(workspace)

    book_anchors = []
    total = len(chapters)
    job.set(total_chapters=total)
    for done, ch in enumerate(chapters, start=0):
        if job.cancel_event.is_set():
            raise JobCancelled()
        job.set(done_chapters=done)
        job.log(f"处理第 {ch['index']}/{total} 章：{ch['title']}")
        base = f"ch{int(ch['index']):02d}"
        ir_path = ir_dir / f"{base}.json"
        note_path = notes_dir / f"{base}.md"
        map_path = maps_dir / f"{base}.json"

        text_path = chapters_dir / str(ch.get("filename") or f"{base}.txt")
        if not text_path.exists():
            text_path = chapters_dir / f"{base}.txt"
        text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
        text_hash = split_book.chapter_hash(ch.get("title"), text)
        ctx = _chapter_context(ch, job)

        # ---- Pass 1：原著解剖（原文指纹一致且 ir/note 都存在才跳过）----
        pass1_reused = False
        ir = {}
        if ir_path.exists() and note_path.exists():
            try:
                loaded = json.loads(ir_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = {}
            if not isinstance(loaded, dict):
                loaded = {}
            stored_hash = str(loaded.get("source_hash") or "")
            if not _ir_usable(loaded):
                job.log("  chapter_ir 为空或残缺：Pass 1 重新生成")
            elif stored_hash and stored_hash != text_hash:
                job.log("  检测到本章原文已更新：Pass 1 重新生成")
            else:
                ir = loaded
                pass1_reused = True
                job.log("  Pass 1 已存在，读取 chapter_ir（断点续跑）")

        if not pass1_reused and not text.strip():
            job.log("  警告：本章切分文本为空，写入残缺占位（不调用模型）")
            ir = {
                "chapter_core_question": "",
                "note": f"### 核心问题意识\n\n【原文OCR残缺】第{ch['index']}章切分文本为空。",
                "glossary": [],
                "units": [],
                "verified_relations": [],
                "chapter_framework": "",
            }
            note_path.write_text(f"## {chapter_heading(ch)}\n\n{ir['note']}\n", encoding="utf-8")
        elif not pass1_reused:
            ir = run_pass1(job, llm, ctx, text,
                           [e.get("term", "") for e in glossary_entries if e.get("term")])
            if not isinstance(ir, dict):
                ir = {}
            note_body = normalize_note_markdown(ir.get("note", ""))
            note_path.write_text(f"## {chapter_heading(ch)}\n\n{note_body}\n", encoding="utf-8")
            job.log("  Pass 1 完成：chapter_ir + chapter_notes 已写入")

        ir["source_hash"] = text_hash
        ir_path.write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")

        # 术语表去重合并（幂等：同章重跑只覆盖，不重复追加）
        glossary_entries = merge_glossary_entries(glossary_entries, ir.get("glossary") or [], ch)
        save_glossary_entries(workspace, glossary_entries)
        render_glossary_md(workspace, glossary_entries)

        # ---- Pass 2：图谱建构与学术延伸 ----
        pass2_skipped = False
        cmap = None
        if pass1_reused and _pass2_ready(map_path):
            try:
                loaded = json.loads(map_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                stored_hash = str(loaded.get("source_hash") or "")
                if not stored_hash or stored_hash == text_hash:
                    cmap = loaded
                    pass2_skipped = True
        if cmap is None:
            cmap = run_pass2(job, llm, ctx, ir, book_anchors, glossary_entries)
        else:
            job.log("  Pass 2 已存在（含 steps 或双视角），跳过模型调用")

        cmap.setdefault("chapter_id", f"{ch['index']:02d}")
        cmap.setdefault("chapter_title", ch["title"])
        cmap["source_hash"] = text_hash
        map_path.write_text(json.dumps(cmap, ensure_ascii=False, indent=2), encoding="utf-8")
        if not pass2_skipped:
            job.log(f"  Pass 2 完成：chapter_maps 已写入（{len(cmap.get('nodes') or [])} 节点）")

        # 轻量只读告警：属种层级在图谱节点命名中是否越界（不篡改数据、不中断任务）。
        warn_taxonomy_naming(ir, cmap, job)

        chapter_html = MAPS_DIR / job.slug / f"ch{ch['index']:02d}.html"
        if (not pass2_skipped) or (not chapter_html.exists()):
            try:
                has_parent = (not job.chapter_range) or (MAPS_DIR / f"{job.slug}.html").exists()
                out = inject_chapter_map(job.slug, ch["index"], cmap, job.title, has_parent=has_parent)
                job.log(f"  分章地图已生成：{out}")
            except Exception as e:
                job.log(f"  分章地图生成失败：{e}")
        else:
            job.log("  分章地图已存在，跳过生成")

        book_anchors.append(_chapter_anchor(ch, cmap, glossary_entries))
        job.set(done_chapters=done + 1)
        job.log(f"  完成第 {ch['index']} 章（Pass 1 + Pass 2）")
def merge_notes_into(slug: str, title: str, job: Optional[JobState] = None) -> tuple:
    """复用 merge_notes.py 逻辑：overview + 逐章笔记 -> notes/<slug>.md。

    notes/*.md 属用户只读原稿，若已存在严禁覆盖，改写到 workspace/<slug>/<slug>_merged.md。
    """
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

    notes_out = NOTES_DIR / f"{slug}.md"
    if notes_out.exists():
        out = WORKSPACE / slug / f"{slug}_merged.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        if job is not None:
            job.log(f"检测到 notes/{slug}.md 已存在（只读原稿），已改写到 {out}，请手动比对合并。")
    else:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        out = notes_out
    out.write_text(content, encoding="utf-8")
    return out, len(chapter_files), len(content)
def note_heading(path: Path) -> str:
    """从章节笔记首行提取真实章节标题，避开四节固定小标题。"""
    try:
        head_lines = path.read_text(encoding="utf-8-sig").splitlines()[:10]
    except Exception:
        return ""
    for line in head_lines:
        stripped = line.strip()
        m = re.match(r"^#{1,3}\s*(第\s*[0-9一二三四五六七八九十百千零〇两]+\s*章.*)$", stripped)
        if m:
            return m.group(1).strip()
    for line in head_lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            content = stripped[3:].strip()
            if content and content not in NOTE_SECTIONS:
                return content
    m = re.match(r"^\d+_(.+)$", path.stem)
    if m:
        return m.group(1).replace("_", " ").strip()
    m = re.match(r"^ch(\d+)$", path.stem, re.IGNORECASE)
    if m:
        return f"第{int(m.group(1))}章"
    return path.stem


def stage3_overview(job: JobState, llm: LLM) -> Path:
    """阶段3：生成全书总览 overview.md，并无损合并输出章节笔记。"""
    job.set(stage="overview")
    job.log("阶段3：全书总览与无损合并")
    workspace = WORKSPACE / job.slug
    glossary_path = workspace / "glossary.md"
    notes_dir = workspace / "chapter_notes"
    glossary = glossary_path.read_text(encoding="utf-8") if glossary_path.exists() else ""
    titles = []
    if notes_dir.is_dir():
        for f in sorted(notes_dir.glob("*.md"), key=natural_key):
            titles.append(note_heading(f))

    system = load_style_system()
    user = (
        "请基于下面的全书术语表与各章标题，撰写一份全书总览（直接输出 Markdown，不要任何 JSON 或解释）。\n"
        "总览需包含：全书核心问题、逻辑框架、章节脉络。\n\n"
        f"【术语表】\n{glossary[:6000]}\n\n"
        f"【各章标题】\n" + "\n".join(titles[:200])
    )
    raw, usage = llm.chat(llm.model_pro, system, user)
    job.add_usage(usage)
    job.log(f"总览调用模式：{llm.last_mode}")
    overview_path = workspace / "overview.md"
    overview_path.write_text(raw.strip() + "\n", encoding="utf-8")

    out, n, size = merge_notes_into(job.slug, job.title, job)
    job.set(merged_note_path=str(out))
    job.log(f"总览完成，合并 {n} 章笔记 -> {out}（{size} 字）")
    return out
def build_chapter_summaries(chapter_maps: list) -> list:
    """把各章图谱压成干净摘要对象：只保留章号、核心问题、节点 id/标题/核心判断与核心边。"""
    summaries = []
    for cmap in chapter_maps or []:
        if not isinstance(cmap, dict):
            continue
        nodes = []
        for n in (cmap.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            nodes.append({
                "id": str(n.get("id") or "").strip(),
                "title": str(n.get("title") or "").strip(),
                "coreIdea": str(n.get("coreIdea") or "").strip(),
            })
        node_ids = {n["id"] for n in nodes if n["id"]}
        edges = []
        for e in (cmap.get("edges") or []):
            if not isinstance(e, dict):
                continue
            source = str(e.get("source") or "").strip()
            target = str(e.get("target") or "").strip()
            if source not in node_ids or target not in node_ids:
                continue
            edges.append({
                "source": source,
                "target": target,
                "label": str(e.get("label") or "").strip(),
            })
        guide = cmap.get("readingGuide") if isinstance(cmap.get("readingGuide"), dict) else {}
        summaries.append({
            "chapter_id": str(cmap.get("chapter_id") or "").strip(),
            "chapter_title": str(cmap.get("chapter_title") or "").strip(),
            "coreQuestion": str(guide.get("coreQuestion") or cmap.get("chapter_core_question") or "").strip(),
            "nodes": nodes,
            "edges": edges,
        })
    return summaries


def build_map_system() -> str:
    return load_style_system() + STYLE_REDLINES + (
        "\n\n【本阶段职责】把各章已校验的结构化摘要综合为整书知识图谱，并补充原书未展开的学术延伸；"
        "唯一事实源是各章图谱摘要与全书术语表，严禁虚构原著内容。\n"
        "每次只输出一个 JSON 对象，不要输出 JSON 之外的任何文字、注释或代码块。\n"
    )


def build_map_prompt(job: JobState, overview: str, glossary: str, chapter_maps: list) -> str:
    summaries = build_chapter_summaries(chapter_maps)
    other_books = []
    for book in load_books():
        if not isinstance(book, dict):
            continue
        if str(book.get("id") or "") == job.slug:
            continue
        other_books.append({
            "title": str(book.get("title", "")).strip(),
            "tags": book.get("tags") or [],
            "summary": str(book.get("summary", "")).strip()[:120],
        })
    books_text = json.dumps(other_books[:8], ensure_ascii=False, indent=1) if other_books else "（书架暂无其他书目）"
    return (
        "你是知识图谱工程师。请把以下资料综合成一份信息密度与 templates/map-template.html 所载范例相当的整书知识地图 JSON。\n\n"
        f"【全书总览】\n{overview[:6000]}\n\n"
        f"【术语表】\n{glossary[:6000]}\n\n"
        "【各章结构化摘要】\n"
        f"{json.dumps(summaries, ensure_ascii=False, indent=2)}\n\n"
        f"【书架其他书目（用于跨书锚点）】\n{books_text}\n\n"
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
        '     "subPoints": [{"label": "核心论证", "text": "..."}],\n'
        '     "perspectivesNote": [{"label": "【原书评述】", "text": "..."}]}\n'
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
        "2. 节点数量控制在 6-10 个，覆盖全书核心论点群；每节点 title 严格 ≤12 字、brief 严格 ≤10 个汉字（只放关键词锚点，如「知善必行、无人作恶」「承认无知与反诘」，确保单行显示、防止卡片折行切字）、coreIdea 为 20-40 字的一句话核心结论、summary 60-120 字，所有详细解释全部放进 subPoints/perspectivesNote，保证右侧阅读栏解析详实。\n"
        "3. 节点 category 为 4-8 字的「理论结构定位」，刻画该节点在论证结构中承担的角色，例如「感性始基」「逻辑本体」「微观折中」「问题框架」，严禁使用「学说」「概念」「论点」「理论」这类泛称。\n"
        "4. subPoints 每节点 3-4 条，只使用四个语义槽位：核心论证、关键区分、内在两难、原典锚点。\n"
        "5. perspectivesNote 每节点 2-3 条，严禁留空数组，只用三层来源标签：【原书评述】取自各章作者评述、【学术延伸】具名学者或具体概念的学术史脉络（40-80 字）、【跨章锚点】或【跨书锚点】与前章或书架其他书目呼应。\n"
        "6. 严禁输出任何 x/y 坐标：节点坐标由后端 Python 按层级网格/蛇形递进算法自动计算，杜绝重叠。\n"
        '7. edges 的 perspective 取值必须是 "both" 或 perspectives 中某个 id；大多数连线精准归入两个分析视角之一；"both" 仅可用于真正横跨两个分析视角的交叉桥接边，完全发生在同一学派或同一思想家内部的逻辑推导边（如同一哲学家的不同核心命题之间）一律归入该方专属视角，严禁标为 "both"，且 "both" 不超过总边数的 25%；任意两个节点之间在同一方向（相同 source 与 target）最多只允许建立 1 条边，存在复合关系（如同时包含批评与继承）时合并为 1 条边并在 label（如「批判与继承」）与 detail 中一并说明；curve 一律填 0，labelT 为 0~1；label 短，detail 为 40-90 字深度解析。\n'
        "8. steps 给 4-6 步，按逻辑顺序覆盖因果链，activeNodes/activeEdges 引用上面 id。\n"
        "9. readingGuide.coreQuestion 用 2-3 句直白的话交代本部分核心问题；startNodeId 指向逻辑起点节点；recommendedPath 给 3-5 个按顺序编号的关键节点（含 id 与简短 label）。\n"
        '10. 除 "all" 外再给出 2 个分析视角（简短英文/拼音 id），分别对应 lineClass 的 primary 与 secondary。\n'
        "11. 零孤立节点硬规则：图谱严禁出现没有任何连线的孤立节点；每个节点至少应有 1 条有原文依据的连线，既包括跨学派交锋，也包括同一思想体系内部的逻辑推进与方法支撑；仍严禁杜撰无依据的跨学派历时师承。\n"
        "12. 属种命名不越界：综合全书节点时，若同一理论传统、流派或大概念被拆分为多个并列节点，严禁任一局部节点独占上位总称，所有并列节点必须在同一抽象层级上准确命名。\n"
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


def normalize_map_data(data: dict, job: Optional[JobState] = None,
                       fallback_title: str = "") -> dict:
    """把模型输出的 MAP_DATA 规范化为母版渲染引擎所需结构。

    这是「LLM 只输出结构化 JSON → Python 确定性防重叠布局 → 注入模板」流程中的
    唯一确定性后处理关卡：模型绝不直接写 HTML/CSS，任何字段都必须在此被校验、
    截断、去悬空边、去无效步骤引用与兜底后，才能由 inject_map() 注入母版。
    """
    data = data if isinstance(data, dict) else {}
    meta = data.get("meta") or {}
    default_title = (job.title if job is not None else "") or fallback_title
    title = str(meta.get("title", default_title or "")).strip() or default_title
    subtitle = str(meta.get("subtitle", "")).strip()
    overview = str(meta.get("overview", "")).strip()

    perspectives = []
    seen_persp = set()
    for p in (data.get("perspectives") or []):
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id", "")).strip()
        if not pid or pid in seen_persp:
            continue
        seen_persp.add(pid)
        perspectives.append({
            "id": pid,
            "label": str(p.get("label", "")).strip(),
            "legendLabel": str(p.get("legendLabel", "")).strip(),
            "lineClass": p.get("lineClass") if p.get("lineClass") in ("both", "primary", "secondary") else "both",
        })
    if not any(p["id"] == "all" for p in perspectives):
        perspectives.insert(0, {"id": "all", "label": "全景互锁", "legendLabel": "全书共用结构", "lineClass": "both"})

    nodes = []
    seen_node = set()
    for i, n in enumerate(data.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        node_title = truncate_cjk(n.get("title"), 12)
        raw_brief = str(n.get("brief", "")).strip()
        brief = truncate_cjk(raw_brief, 16)
        summary = str(n.get("summary", "")).strip()
        if len(raw_brief) > 16 and raw_brief not in summary:
            summary = f"{raw_brief}。{summary}".strip()
        core_idea = str(n.get("coreIdea", "")).strip() or first_sentence(summary)
        node_id = str(n.get("id") or f"n{i + 1}").strip() or f"n{i + 1}"
        if node_id in seen_node:
            node_id = f"{node_id}-{i + 1}"
        seen_node.add(node_id)
        node_obj = {
            "id": node_id,
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
    perspective_aliases = {}
    for p in perspectives:
        pid = p["id"]
        perspective_aliases.setdefault(pid, pid)
        perspective_aliases.setdefault(pid.lower(), pid)
        lc = str(p.get("lineClass", "")).strip().lower()
        if lc in ("primary", "secondary"):
            perspective_aliases.setdefault(lc, pid)
            for key in (str(p.get("label", "")).strip(), str(p.get("legendLabel", "")).strip()):
                if key:
                    perspective_aliases.setdefault(key, pid)
                    perspective_aliases.setdefault(key.lower(), pid)
    node_ids = {n["id"] for n in nodes}
    edges = []
    seen_edge = set()
    for i, e in enumerate(data.get("edges") or []):
        if not isinstance(e, dict):
            continue
        source = str(e.get("source", "")).strip()
        target = str(e.get("target", "")).strip()
        # 过滤悬空边与自环边
        if source not in node_ids or target not in node_ids or source == target:
            continue
        edge_id = str(e.get("id") or f"e{i + 1}").strip() or f"e{i + 1}"
        if edge_id in seen_edge:
            edge_id = f"{edge_id}-{i + 1}"
        seen_edge.add(edge_id)
        persp = str(e.get("perspective", "both")).strip()
        if persp not in perspective_ids:
            persp = perspective_aliases.get(persp, perspective_aliases.get(persp.lower(), "both"))
        edges.append({
            "id": edge_id,
            "source": source,
            "target": target,
            "perspective": persp,
            "label": str(e.get("label", "")).strip(),
            "curve": _as_int(e.get("curve"), 0),
            "labelT": _as_float(e.get("labelT"), 0.5),
            "detail": str(e.get("detail", "")).strip(),
        })

    valid_edge_ids = {e["id"] for e in edges}
    steps = []
    for s in (data.get("steps") or []):
        if not isinstance(s, dict):
            continue
        active_nodes = []
        for x in (s.get("activeNodes") or []):
            x = str(x).strip()
            if x in node_ids and x not in active_nodes:
                active_nodes.append(x)
        active_edges = []
        for x in (s.get("activeEdges") or []):
            x = str(x).strip()
            if x in valid_edge_ids and x not in active_edges:
                active_edges.append(x)
        if not active_nodes and not active_edges:
            continue
        steps.append({
            "title": str(s.get("title", "")).strip(),
            "description": str(s.get("description", "")).strip(),
            "activeNodes": active_nodes,
            "activeEdges": active_edges,
        })

    guide = data.get("readingGuide") or {}
    start_node_id = str(guide.get("startNodeId", "")).strip()
    if start_node_id not in node_ids:
        start_node_id = nodes[0]["id"] if nodes else ""
    recommended_path = []
    for rp in (guide.get("recommendedPath") or []):
        if isinstance(rp, dict):
            rid = str(rp.get("id", "")).strip()
            label = str(rp.get("label", "")).strip()
        elif isinstance(rp, str):
            rid, label = rp.strip(), ""
        else:
            continue
        if rid in node_ids and all(x["id"] != rid for x in recommended_path):
            recommended_path.append({"id": rid, "label": label})
    if len(recommended_path) > 5:
        recommended_path = recommended_path[:5]
    if len(recommended_path) < 3 and nodes:
        for n in nodes:
            if len(recommended_path) >= 5:
                break
            if all(x["id"] != n["id"] for x in recommended_path):
                recommended_path.append({"id": n["id"], "label": n["title"]})
    core_question = str(guide.get("coreQuestion", "")).strip()
    reading_guide = {
        "coreQuestion": core_question,
        "startNodeId": start_node_id,
        "recommendedPath": recommended_path,
    }

    result = {"meta": {"title": title, "subtitle": subtitle, "overview": overview},
              "perspectives": perspectives, "nodes": nodes, "edges": edges, "steps": steps,
              "readingGuide": reading_guide}
    if isinstance(data.get("subMaps"), list):
        result["subMaps"] = data["subMaps"]
    if isinstance(data.get("parentMap"), dict):
        result["parentMap"] = data["parentMap"]

    # 零孤立节点校验：过滤悬空边后，度数（入度+出度）为 0 的节点必须显式告警，不静默吞掉。
    degree = {n["id"]: 0 for n in nodes}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1
    isolated = [nid for nid, d in degree.items() if d == 0]
    if isolated:
        warning = f"[校验警告] 存在孤立节点：{'、'.join(isolated)}"
        if job is not None:
            job.log(warning)
        else:
            print(warning, file=sys.stderr)
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


def _skip_books_json() -> bool:
    """SKIP_BOOKS_JSON=1 时禁止写入 books.json（试验田专用，不影响返回值结构）。"""
    return os.getenv("SKIP_BOOKS_JSON", "").strip().lower() in ("1", "true", "yes", "on")


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

    if _skip_books_json():
        return BOOKS_JSON

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
    """把单章 chapter_maps/xx.json 完整包装为 MAP_DATA。

    复用 normalize_map_data() 完整保留 Pass 2 产出的双视角、步骤与导览，
    再补充 parentMap 指向整书总图与 subMaps: []。
    """
    cmap = chapter_map if isinstance(chapter_map, dict) else {}
    raw_chapter_title = str(cmap.get("chapter_title") or "").strip()
    chapter_title = (chapter_title or raw_chapter_title or f"第{order}章").strip()
    cmap_meta = cmap.get("meta") if isinstance(cmap.get("meta"), dict) else {}
    chapter_core_question = str(
        cmap.get("chapter_core_question") or cmap.get("coreQuestion") or ""
    ).strip()

    data = {
        "meta": {
            "title": f"{book_title} · {chapter_title}" if book_title else chapter_title,
            "subtitle": subtitle or str(cmap_meta.get("subtitle", "")).strip(),
            "overview": overview or str(cmap_meta.get("overview", "")).strip() or chapter_core_question,
        },
        "perspectives": cmap.get("perspectives"),
        "nodes": cmap.get("nodes"),
        "edges": cmap.get("edges"),
        "steps": cmap.get("steps"),
        "readingGuide": cmap.get("readingGuide") or {"coreQuestion": chapter_core_question},
    }
    normalized = normalize_map_data(data, fallback_title=chapter_title)
    if not normalized["readingGuide"]["coreQuestion"]:
        normalized["readingGuide"]["coreQuestion"] = chapter_core_question
    normalized["parentMap"] = {"title": book_title, "path": parent_path}
    normalized["subMaps"] = []
    return normalized
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
        canonical = re.match(r"^ch(\d+)$", f.stem, re.IGNORECASE)
        legacy = re.match(r"^(\d+)_", f.stem)
        match = canonical or legacy
        order = int(match.group(1)) if match else 0
        result.append({"order": order or (len(result) + 1), "chapter_map": data})
    return result


def chapter_map_ready(slug: str, order: int) -> bool:
    """判断某一章的分章图谱是否已生成且结构完整。"""
    return _pass2_ready(WORKSPACE / slug / "chapter_maps" / f"ch{int(order):02d}.json")


def chapters_completion(slug: str) -> dict:
    """统计全书章节完成度，供「生成整书总图」按钮前置校验。"""
    chapters = list_existing_chapters(slug)
    missing = [
        int(chapter["index"]) for chapter in chapters
        if not chapter_map_ready(slug, int(chapter["index"]))
    ]
    return {
        "total": len(chapters),
        "ready": len(chapters) - len(missing),
        "missing": missing[:20],
        "complete": bool(chapters) and not missing,
    }


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
    if _skip_books_json():
        job.log("SKIP_BOOKS_JSON=1：已跳过 books.json 写入")
    else:
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

    raw, usage = llm.chat(llm.model_pro, build_map_system(), build_map_prompt(job, overview, glossary, chapter_maps), json_mode=True)
    job.add_usage(usage)
    job.log(f"  MAP_DATA 调用模式：{llm.last_mode}")
    data = parse_json_robust(raw, llm, job)
    data, filled = ensure_perspectives_notes(data, {}, [], scope_word="全书")
    if filled:
        job.log(f"  MAP_DATA 兜底补填 perspectivesNote：{filled} 个节点")
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
    if _skip_books_json():
        job.log("SKIP_BOOKS_JSON=1：已跳过 books.json 写入")
    else:
        job.log("已上架 books.json（无损合并）")
    note_path = getattr(job, "merged_note_path", None) or str(NOTES_DIR / f"{job.slug}.md")
    return str(out), str(note_path)
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







