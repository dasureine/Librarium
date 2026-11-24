# -*- coding: utf-8 -*-
"""Librarium 内置 AI 核心（阶段 1）。

职责（纯本地装配，不直接发网络请求；实际 LLM 流式调用在 server.py 完成）：

  · 加载 data/corpus_index.json 统一语料索引；
  · 解析用户圈定的「范围」（书 / 标签 / 章节 多选）；
  · 小范围整段注入、大范围关键词召回 top-k；
  · 按「模式 × 外源三档 × 联网开关」装配 system + user；
  · 每个注入片段带可溯源标记 [S1][S2]...，附稳定片段 ID 与页码，供前端回链原文。

零依赖，仅本地读取 data/ 与 books.json，绝不触碰 notes/*.md。
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = ROOT / "data" / "corpus_index.json"
BOOKS_JSON = ROOT / "books.json"

# 大范围时单次注入的原文预算（字符数），超过则走关键词召回。
MAX_CONTEXT_CHARS = 14000
DEFAULT_TOP_K = 10

MODES = ("qa", "inspire", "exam", "verbatim")
EXTERNALS = ("closed", "semi", "open")


def _pick(value, allowed, default):
    return value if value in allowed else default


def load_corpus() -> dict:
    """读取统一语料索引（每次现读，避免服务运行期索引过期）。"""
    if not CORPUS_PATH.exists():
        return {"version": 1, "books": []}
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def load_book_tags() -> dict:
    """从 books.json 建立 tag -> [slug] 映射，供「按标签」范围解析。"""
    tags = {}
    if BOOKS_JSON.exists():
        try:
            data = json.loads(BOOKS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for b in data:
                    if not isinstance(b, dict):
                        continue
                    slug = b.get("id")
                    if not slug:
                        continue
                    for tag in b.get("tags") or []:
                        tags.setdefault(str(tag), [])
                        if slug not in tags[str(tag)]:
                            tags[str(tag)].append(slug)
        except Exception:
            pass
    return tags


def extract_tokens(text: str) -> list:
    """轻量中文关键词提取：拉丁词/数字 + 相邻二字组（CJK bigram）。"""
    tokens = []
    for m in re.findall(r"[A-Za-z0-9]+", str(text).lower()):
        tokens.append(m)
    for run in re.findall(r"[\u4e00-\u9fff]+", str(text)):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def score(query: str, text: str) -> int:
    """关键词命中数打分（无 jieba 依赖的近似召回）。"""
    tokens = extract_tokens(query)
    if not tokens:
        return 0
    low = str(text).lower()
    return sum(1 for t in tokens if t in low)


def resolve_scope(corpus: dict, scope: list, book_tags: dict):
    """把 scope 选择器解析为富化片段列表 docs 与范围内术语 terms。

    scope 元素：{"type": "book"|"tag"|"chapter", "value": ...}
      · book    value = slug
      · tag     value = "一级/二级" 标签
      · chapter value = "slug:章号"
    空 scope 视为选择全部书目。
    """
    targets = {}  # slug -> None（整书）或 {章号,...}
    for sel in scope or []:
        if not isinstance(sel, dict):
            continue
        t = sel.get("type")
        v = str(sel.get("value") or "").strip()
        if not v:
            continue
        if t == "book":
            targets[v] = None
        elif t == "tag":
            for slug in book_tags.get(v, []):
                targets[slug] = None
        elif t == "chapter":
            slug, _, idxs = v.partition(":")
            idx = int(idxs) if idxs.strip().isdigit() else None
            if not slug or idx is None:
                continue
            if slug not in targets:
                targets[slug] = {idx}
            else:
                cur = targets[slug]
                if cur is None:
                    continue  # 整书已选，章节选择冗余
                cur.add(idx)

    if not targets:
        targets = {b.get("slug"): None for b in corpus.get("books", []) if b.get("slug")}

    docs = []
    terms = []
    for book in corpus.get("books", []):
        slug = book.get("slug")
        if slug not in targets:
            continue
        sel_chapters = targets[slug]
        for ch in book.get("chapters", []):
            if sel_chapters is not None and ch.get("index") not in sel_chapters:
                continue
            for seg in ch.get("segments", []):
                docs.append({
                    "id": seg.get("id"),
                    "text": seg.get("text") or "",
                    "seq": seg.get("seq"),
                    "file_page": seg.get("file_page"),
                    "printed_page": seg.get("printed_page"),
                    "book_slug": slug,
                    "book_title": book.get("title", slug),
                    "chapter_index": ch.get("index"),
                    "chapter_title": ch.get("title", ""),
                })
        terms.extend(book.get("terms") or [])
    return docs, terms


def citation_label(doc: dict) -> str:
    """片段的人类可读引用标签：书名 · 章 · 片段N · p.页。"""
    ch_title = str(doc.get("chapter_title") or "").strip()
    if not ch_title:
        ch_title = f"第{doc.get('chapter_index')}章"
    parts = [str(doc.get("book_title") or doc.get("book_slug")), ch_title,
             f"片段{doc.get('seq')}"]
    page = doc.get("printed_page") or doc.get("file_page")
    if page is not None:
        parts.append(f"p.{page}")
    return " · ".join(parts)


def retrieve(query: str, docs: list, top_k: int) -> list:
    """关键词召回 top-k，按命中数、书/章/片段顺序稳定排序。"""
    scored = sorted(
        ((score(query, d["text"]), d) for d in docs),
        key=lambda x: (-x[0], x[1]["book_slug"], x[1]["chapter_index"], x[1]["seq"]),
    )
    return [d for _, d in scored[:max(1, top_k)]]


def build_system(mode: str, external: str, web: bool) -> str:
    mode = _pick(mode, MODES, "qa")
    external = _pick(external, EXTERNALS, "closed")

    base = (
        "你是 Librarium 的内置学术 AI，服务一位正在精读学术著作的用户。\n"
        "你的全部回答必须严谨、可溯源、忠于资料；语言使用简单平和的书面中文，"
        "直接主谓宾陈述句，不寒暄、不抒情、不空话，去掉「不是……而是……」句式。\n\n"
        "【引用铁律】涉及资料事实的每个论断，必须用 [S1][S2] 等标记标出依据；"
        "每个 [Sn] 对应下方「可溯源资料」里唯一片段。引用时同时写出「第X章 · 片段N」"
        "（有页码则附 p.XX）。严禁编造资料中不存在的页码或内容。\n"
    )

    mode_block = {
        "qa": "【模式：问答】只就用户圈定的资料回答问题：先直接作答，再用资料佐证；"
              "资料不足以回答时，明确说明资料范围内没有依据。",
        "inspire": "【模式：学术启发】除回答问题外，主动指出资料内部的论证张力、"
                   "可深挖的问题、跨章/跨书可关联的线索，并给出下一步研究建议；"
                   "启发必须扎根于资料，不得空谈。",
        "exam": "【模式：闭卷考官】你是严格闭卷考官：每次只出一题，不给任何提示；"
                "仅当用户回答准确覆盖原文要素、概念界定清晰且逻辑无误才判【通过】"
                "并附极简原文依据；回答遗漏要点、表述含糊或掺入书外理解一律判【未通过】，"
                "严禁直接给答案，只指出漏了哪部分/哪里与原文冲突，要求重答；"
                "用户输入「看答案/跳过」才展示原文依据并记入错题本。",
        "verbatim": "【模式：逐字精读考官】收到资料切片后，先按顺序逐段输出"
                    "「无遗漏细节台账」（穷举定义、限定词、因果链、例证），"
                    "暂停等待用户核对说「开始」后再逐条单题考核，判分同闭卷考官；"
                    "全部通过后追加结构回扣题，并输出本节错题清单。",
    }[mode]

    external_block = {
        "closed": "【知识边界：闭卷】你的所有回答 100% 只依据下方「可溯源资料」。"
                  "严禁引入任何资料之外的知识、常识补充、其他学者观点或主观延伸；"
                  "资料未提及的一律回答「资料未提及」。",
        "semi": "【知识边界：半开卷】以「可溯源资料」为主；确需背景解释时可调用你的通识，"
                "但必须明确标注【书外】字样，与资料内内容区分。",
        "open": "【知识边界：全开卷】可以结合资料、你的通识与外部信息；"
                "所有非资料来源的论断必须标注【书外】或【外部】来源。",
    }[external]

    web_block = ""
    if web and external != "closed":
        web_block = ("\n【联网】联网搜索已开启：可引用外部网络信息，"
                     "但必须标注【外部】并尽量给出出处。")

    return base + mode_block + "\n" + external_block + web_block


def build_glossary(terms: list, cap: int = 40) -> str:
    items = []
    for t in terms[:cap]:
        term = str(t.get("term") or "").strip()
        definition = str(t.get("definition") or "").strip()
        if term and definition:
            items.append(f"- {term}：{definition}")
    return ("【术语表】\n" + "\n".join(items)) if items else ""


def assemble(query: str, scope: list, mode: str, external: str, web: bool,
             top_k: int = DEFAULT_TOP_K) -> tuple:
    """装配一次内置 AI 请求，返回 (system, user, sources)。"""
    corpus = load_corpus()
    book_tags = load_book_tags()
    docs, terms = resolve_scope(corpus, scope, book_tags)

    total = sum(len(d["text"]) for d in docs)
    if total <= MAX_CONTEXT_CHARS:
        selected = docs
    else:
        selected = retrieve(query, docs, top_k)

    sources = []
    parts = []
    for i, d in enumerate(selected, start=1):
        label = citation_label(d)
        sources.append({
            "marker": f"S{i}",
            "id": d["id"],
            "label": label,
            "page": d.get("printed_page") or d.get("file_page"),
        })
        parts.append(f"【S{i}｜{label}】\n{d['text']}")

    system = build_system(mode, external, web)
    user = f"【用户问题】\n{query}\n"
    if parts:
        user += "\n【可溯源资料】\n" + "\n\n".join(parts)
    else:
        user += "\n（当前圈定的资料范围内没有可溯源内容。）"
    gloss = build_glossary(terms)
    if gloss:
        user += "\n\n" + gloss

    return system, user, sources


def overview() -> dict:
    """返回前端选择器所需的书目/章节/标签概览。"""
    corpus = load_corpus()
    book_tags = load_book_tags()
    books = []
    for b in corpus.get("books", []):
        chapters = []
        for c in b.get("chapters", []):
            has_page = any(s.get("file_page") is not None for s in c.get("segments", []))
            chapters.append({
                "index": c.get("index"),
                "title": c.get("title", ""),
                "segment_count": len(c.get("segments", [])),
                "has_page": has_page,
            })
        books.append({
            "slug": b.get("slug"),
            "title": b.get("title", ""),
            "author": b.get("author", ""),
            "page_source": b.get("page_source"),
            "chapter_count": len(b.get("chapters", [])),
            "segment_count": sum(len(c.get("segments", [])) for c in b.get("chapters", [])),
            "chapters": chapters,
        })
    tags = [{"tag": k, "books": v} for k, v in sorted(book_tags.items())]
    return {"books": books, "tags": tags}


def get_segment(segment_id: str):
    """按片段 ID 返回单个片段的原文与元数据（供前端点击引用回显原文）。"""
    corpus = load_corpus()
    for book in corpus.get("books", []):
        for ch in book.get("chapters", []):
            for seg in ch.get("segments", []):
                if seg.get("id") == segment_id:
                    return {
                        "id": seg.get("id"),
                        "text": seg.get("text") or "",
                        "book_title": book.get("title", ""),
                        "chapter_index": ch.get("index"),
                        "chapter_title": ch.get("title", ""),
                        "seq": seg.get("seq"),
                        "file_page": seg.get("file_page"),
                        "printed_page": seg.get("printed_page"),
                    }
    return None


def sample_spread(docs: list, k: int) -> list:
    """在 docs 中均匀抽样 k 个片段（跨章节分布，避免只取开头）。"""
    if len(docs) <= k:
        return docs
    step = len(docs) / k
    return [docs[int(i * step)] for i in range(k)]


def build_quiz_system(count: int, qtype: str) -> str:
    return (
        "你是 Librarium 的出题助手，基于用户圈定的资料生成自测题。\n"
        f"生成 {count} 道题（题型：{qtype}）。\n"
        "只输出一个 JSON 对象，不要输出 JSON 之外的任何文字、注释或代码块。\n"
        '{"questions":[{"question":"题目","answer":"标准答案","analysis":"解析","source":"S1"}]}\n'
        "要求：\n"
        "1. question 精准针对资料的精确表述、概念界定与论证逻辑，禁止笼统水题；\n"
        "2. answer 完整准确，覆盖该题全部要点，忠实于原文；\n"
        "3. analysis 说明本题考点与原文依据；\n"
        "4. source 填对应片段的标记（只写 S1、S2 等，去掉方括号），必须来自下方资料；\n"
        "5. 选择题的每个选项（A/B/C/D）独立成行，不得挤在同一行；\n"
        "6. 严禁引入资料之外的内容。"
    )


def assemble_quiz(scope: list, count: int, qtype: str, top_k: int = 12) -> tuple:
    """装配一次出题请求，返回 (system, user, sources)。"""
    corpus = load_corpus()
    book_tags = load_book_tags()
    docs, terms = resolve_scope(corpus, scope, book_tags)
    selected = sample_spread(docs, top_k)

    sources = []
    parts = []
    for i, d in enumerate(selected, start=1):
        label = citation_label(d)
        sources.append({
            "marker": f"S{i}",
            "id": d["id"],
            "label": label,
            "page": d.get("printed_page") or d.get("file_page"),
        })
        parts.append(f"【S{i}｜{label}】\n{d['text']}")

    system = build_quiz_system(count, qtype)
    user = f"请根据以下资料生成 {count} 道自测题。\n"
    if parts:
        user += "\n【可溯源资料】\n" + "\n\n".join(parts)
    else:
        user += "\n（当前圈定的资料范围内没有可溯源内容。）"
    gloss = build_glossary(terms)
    if gloss:
        user += "\n\n" + gloss
    return system, user, sources


# ---------- 出题自测：知识点覆盖进度与状态持久化 ----------

QUIZ_STATE_PATH = ROOT / "data" / "quiz_state.json"


def scope_key(scope: list) -> str:
    """把 scope 序列化为稳定字符串，作为进度状态的键。"""
    norm = sorted(
        (str(s.get("type") or ""), str(s.get("value") or ""))
        for s in (scope or []) if isinstance(s, dict)
    )
    return json.dumps(norm, ensure_ascii=False, sort_keys=True)


def _load_quiz_states() -> dict:
    if QUIZ_STATE_PATH.exists():
        try:
            data = json.loads(QUIZ_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_quiz_states(states: dict) -> None:
    QUIZ_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUIZ_STATE_PATH.write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")


def get_quiz_state(scope: list) -> dict:
    key = scope_key(scope)
    return _load_quiz_states().get(key, {
        "scope_key": key,
        "points": [],
        "questions": [],
        "covered": [],
    })


def set_quiz_state(scope: list, state: dict) -> None:
    states = _load_quiz_states()
    states[scope_key(scope)] = state
    _save_quiz_states(states)


def _sources_and_parts(docs: list) -> tuple:
    """把片段列表装配为 (sources, 引用文本块)。"""
    sources = []
    parts = []
    for i, d in enumerate(docs, start=1):
        label = citation_label(d)
        sources.append({
            "marker": f"S{i}",
            "id": d["id"],
            "label": label,
            "page": d.get("printed_page") or d.get("file_page"),
        })
        parts.append(f"【S{i}｜{label}】\n{d['text']}")
    return sources, parts


def build_ledger_system() -> str:
    return (
        "你是 Librarium 的知识点梳理助手，把圈定资料拆成一条条「知识点清单」。\n"
        "只输出一个 JSON 对象，不要输出 JSON 之外的任何文字：\n"
        '{"points":[{"name":"知识点名称","source":"S1"}]}\n'
        "要求：\n"
        "1. 每条知识点是一个可独立出题的考点：概念界定、命题、论证关节或关键例证；\n"
        "2. name 精炼（8-20 字），不重复；\n"
        "3. source 填该知识点所在片段的标记（只写 S1、S2 等，去掉方括号）；\n"
        "4. 覆盖本范围全部实质考点，不遗漏；\n"
        "5. 控制总量在 15-25 条左右，把相近的小考点合并成一条完整考点，不要拆得过细；\n"
        "6. 严禁引入资料之外的内容。"
    )


def build_generate_system(uncovered_points: list, count: int, qtype: str) -> str:
    points_desc = "、".join(f"{p['name']}（{p.get('source_marker')}）" for p in uncovered_points)
    return (
        "你是 Librarium 的出题助手，针对下面这些「未覆盖知识点」出题。\n"
        f"未覆盖知识点：{points_desc}\n"
        f"生成 {count} 道题（题型：{qtype}），优先覆盖这些未覆盖知识点。\n"
        "只输出一个 JSON 对象：\n"
        '{"questions":[{"question":"题目","answer":"标准答案","analysis":"解析","source":"S1"}]}\n'
        "要求：\n"
        "1. question 精准，选择题每个选项独立成行；\n"
        "2. answer 完整准确；\n"
        "3. analysis 说明考点；\n"
        "4. source 填本题考察知识点对应的片段标记（只写 S1 等）；\n"
        "5. 严禁引入资料之外的内容。"
    )
