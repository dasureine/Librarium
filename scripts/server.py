# -*- coding: utf-8 -*-
"""Librarium 本地制书后端（FastAPI）。

启动方式（二选一）：
    uvicorn scripts.server:app --host 127.0.0.1 --port 8000
    python scripts/server.py

启动后访问 http://localhost:8000 展示主页与地图页。
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import assistant
import pipeline
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="Librarium 制书后端", version="1.0.0")


@app.get("/api/health")
def health():
    llm = pipeline.LLM()
    return {
        "online": True,
        "backend": "Librarium 制书后端",
        "env_configured": llm.configured,
        "model_pro": llm.model_pro,
        "model_flash": llm.model_flash,
        "pricing": pipeline.pricing_status(),
        "time": pipeline.beijing_now().isoformat(),
    }


@app.post("/api/books/upload")
def upload(file: UploadFile = File(...), slug: str = Form(...), offset: int = Form(0)):
    slug = pipeline.sanitize_slug(slug)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".pdf", ".epub"):
        raise HTTPException(status_code=400, detail="仅支持 .pdf 或 .epub 文件")
    pipeline.RAW_BOOKS.mkdir(parents=True, exist_ok=True)
    dest = pipeline.RAW_BOOKS / f"{slug}{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        result = pipeline.split_precheck(slug, dest, offset=offset)
    except pipeline.ScannedBookError as e:
        return {"status": "scanned", "message": str(e), "chapters": []}
    result["saved_to"] = str(dest)
    return result


@app.post("/api/books/resplit")
def resplit(slug: str = Form(...), page_map: str = Form(""), offset: int = Form(0)):
    slug = pipeline.sanitize_slug(slug)
    book = pipeline.find_raw_book(slug)
    if not book:
        raise HTTPException(status_code=404, detail="未找到该 slug 对应的原始电子书，请先上传。")
    workspace = pipeline.WORKSPACE / slug
    workspace.mkdir(parents=True, exist_ok=True)
    map_file = None
    if page_map:
        map_file = workspace / "pages_map.txt"
        map_file.write_text(page_map, encoding="utf-8")
    try:
        result = pipeline.split_precheck(slug, book, page_map=str(map_file) if map_file else None, offset=offset)
    except pipeline.ScannedBookError as e:
        return {"status": "scanned", "message": str(e), "chapters": []}
    return result


@app.get("/api/books/existing/{slug}")
def existing(slug: str):
    slug = pipeline.sanitize_slug(slug)
    return pipeline.existing_chapters_for_slug(slug)


@app.post("/api/books/start")
def start(
    title: str = Form(...),
    author: str = Form(...),
    slug: str = Form(...),
    chapter_range: Optional[str] = Form(None),
):
    slug = pipeline.sanitize_slug(slug)
    job = pipeline.start_job(title=title, author=author, slug=slug, chapter_range=chapter_range)
    return {"job_id": job.id, "status": job.status, "slug": job.slug}


@app.get("/api/books/status/{job_id}")
def status(job_id: str):
    job = pipeline.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.snapshot()


@app.post("/api/books/cancel/{job_id}")
def cancel(job_id: str):
    job = pipeline.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    job.cancel_event.set()
    job.log("已请求取消（当前章节完成后中止，已写章节保留可断点续跑）")
    return {"job_id": job_id, "cancelled": True, "status": job.status}


@app.get("/api/books")
def list_books():
    return pipeline.list_jobs()


# ---------- 内置 AI 学术工作台（阶段 1） ----------

@app.get("/api/corpus/overview")
def corpus_overview():
    return assistant.overview()


@app.post("/api/chat")
def chat(payload: dict = Body(...)):
    query = str(payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")
    scope = payload.get("scope") or []
    mode = str(payload.get("mode") or "qa")
    external = str(payload.get("external") or "closed")
    web = bool(payload.get("web"))
    top_k = int(payload.get("top_k") or 10)

    system, user, sources = assistant.assemble(
        query=query, scope=scope, mode=mode, external=external, web=web, top_k=top_k
    )

    llm = pipeline.LLM()
    if not llm.configured:
        raise HTTPException(status_code=500, detail="未配置 API_KEY（.env），无法调用内置 AI")

    def sse(obj):
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def gen():
        yield sse({"type": "meta", "sources": sources, "mode": mode,
                   "external": external, "web": web, "source_count": len(sources)})
        try:
            for delta in llm.chat_stream(llm.model_pro, system, user):
                yield sse({"type": "delta", "content": delta})
        except Exception as exc:  # noqa: BLE001
            yield sse({"type": "error", "message": f"内置 AI 调用失败：{exc}"})
        else:
            yield sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/corpus/segment/{segment_id}")
def corpus_segment(segment_id: str):
    seg = assistant.get_segment(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="片段不存在")
    return seg


@app.post("/api/quiz/ledger")
def quiz_ledger(payload: dict = Body(...)):
    scope = payload.get("scope") or []
    top_k = int(payload.get("top_k") or 24)
    corpus = assistant.load_corpus()
    book_tags = assistant.load_book_tags()
    docs, terms = assistant.resolve_scope(corpus, scope, book_tags)
    selected = assistant.sample_spread(docs, top_k)
    sources, parts = assistant._sources_and_parts(selected)

    system = assistant.build_ledger_system()
    user = "请根据以下资料生成知识点清单。\n\n【可溯源资料】\n" + "\n\n".join(parts)
    gloss = assistant.build_glossary(terms)
    if gloss:
        user += "\n\n" + gloss

    llm = pipeline.LLM()
    if not llm.configured:
        raise HTTPException(status_code=500, detail="未配置 API_KEY（.env），无法调用内置 AI")
    raw, _u = llm.chat(llm.model_pro, system, user, json_mode=True, max_tokens=8000, thinking=False)
    data = json.loads(pipeline.extract_json(raw))
    points = data.get("points") if isinstance(data, dict) else None
    if not isinstance(points, list):
        raise HTTPException(status_code=500, detail="知识点清单解析失败，请重试")

    marker_to_seg = {s["marker"]: s["id"] for s in sources}
    state = assistant.get_quiz_state(scope)
    state["points"] = []
    for i, p in enumerate(points, start=1):
        m = str(p.get("source") or "").strip()
        state["points"].append({
            "id": f"k{i}",
            "name": str(p.get("name") or "").strip(),
            "source_marker": m,
            "segment_id": marker_to_seg.get(m),
        })
    state["covered"] = []
    state["questions"] = []
    assistant.set_quiz_state(scope, state)
    return {
        "scope_key": state["scope_key"],
        "points": state["points"],
        "sources": sources,
        "progress": {"covered": 0, "total": len(state["points"])},
    }


@app.post("/api/quiz/generate")
def quiz_generate(payload: dict = Body(...)):
    scope = payload.get("scope") or []
    count = int(payload.get("count") or 5)
    qtype = str(payload.get("qtype") or "简答/选择混合")
    state = assistant.get_quiz_state(scope)
    points = state.get("points") or []
    covered = set(state.get("covered") or [])
    uncovered = [p for p in points if p.get("id") not in covered]
    if not points:
        raise HTTPException(status_code=400, detail="请先生成知识点清单")
    if not uncovered:
        return {"done": True, "questions": [], "sources": [],
                "progress": {"covered": len(covered), "total": len(points)}}

    seen, docs = set(), []
    for p in uncovered:
        sid = p.get("segment_id")
        if sid and sid not in seen:
            seg = assistant.get_segment(sid)
            if seg:
                seen.add(sid)
                docs.append(seg)
    if not docs:
        raise HTTPException(status_code=500, detail="未覆盖知识点的原文片段缺失")

    sources, parts = assistant._sources_and_parts(docs)
    system = assistant.build_generate_system(uncovered, count, qtype)
    user = "请根据以下资料出题。\n\n【可溯源资料】\n" + "\n\n".join(parts)

    llm = pipeline.LLM()
    raw, _u = llm.chat(llm.model_pro, system, user, json_mode=True, max_tokens=8000, thinking=False)
    data = json.loads(pipeline.extract_json(raw))
    qs = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(qs, list):
        raise HTTPException(status_code=500, detail="出题解析失败，请重试")

    marker_to_seg = {s["marker"]: s["id"] for s in sources}
    seg_to_point = {p["segment_id"]: p["id"] for p in uncovered if p.get("segment_id")}
    base = len(state.get("questions") or [])
    new_qs = []
    for j, q in enumerate(qs, start=1):
        m = str(q.get("source") or "").strip()
        sid = marker_to_seg.get(m)
        new_qs.append({
            "id": f"q{base + j}",
            "question": str(q.get("question") or ""),
            "answer": str(q.get("answer") or ""),
            "analysis": str(q.get("analysis") or ""),
            "source": m,
            "point_id": seg_to_point.get(sid) if sid else None,
            "mastered": False,
            "favorited": False,
        })
    state["questions"] = (state.get("questions") or []) + new_qs
    assistant.set_quiz_state(scope, state)
    return {"done": False, "questions": new_qs, "sources": sources,
            "progress": {"covered": len(covered), "total": len(points)}}


@app.post("/api/quiz/mark")
def quiz_mark(payload: dict = Body(...)):
    scope = payload.get("scope") or []
    question_id = str(payload.get("question_id") or "")
    mastered = bool(payload.get("mastered"))
    state = assistant.get_quiz_state(scope)
    q = next((x for x in state.get("questions") or [] if x.get("id") == question_id), None)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    q["mastered"] = mastered
    covered = list(state.get("covered") or [])
    if mastered:
        if q.get("point_id") and q["point_id"] not in covered:
            covered.append(q["point_id"])
    else:
        if q.get("point_id") and q["point_id"] in covered:
            covered.remove(q["point_id"])
    state["covered"] = covered
    assistant.set_quiz_state(scope, state)
    return {"progress": {"covered": len(covered), "total": len(state.get("points") or [])},
            "mastered": mastered}


@app.post("/api/quiz/favorite")
def quiz_favorite(payload: dict = Body(...)):
    scope = payload.get("scope") or []
    question_id = str(payload.get("question_id") or "")
    state = assistant.get_quiz_state(scope)
    q = next((x for x in state.get("questions") or [] if x.get("id") == question_id), None)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    q["favorited"] = not bool(q.get("favorited"))
    assistant.set_quiz_state(scope, state)
    return {"favorited": q["favorited"]}


@app.post("/api/quiz/state")
def quiz_state(payload: dict = Body(...)):
    scope = payload.get("scope") or []
    state = assistant.get_quiz_state(scope)
    return {
        "points": state.get("points") or [],
        "questions": state.get("questions") or [],
        "covered": state.get("covered") or [],
        "progress": {"covered": len(state.get("covered") or []), "total": len(state.get("points") or [])},
    }


# ---------- 本机专用：一键同步 GitHub 与休眠服务 ----------

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _is_local(request: Request) -> bool:
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _run_git(cmd, cwd, timeout=None):
    kwargs = dict(cwd=str(cwd), encoding="utf-8", errors="replace",
                  capture_output=True, timeout=timeout)
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def _git_status_files():
    proc = _run_git(["git", "-c", "core.quotepath=false", "status", "--porcelain"], ROOT)
    files = []
    for line in proc.stdout.splitlines():
        if len(line) < 3:
            continue
        entry = line[2:].strip()
        if entry:
            files.append(entry.split(" -> ")[-1])
    return files


def _is_sensitive(path):
    p = path.replace("\\", "/").lstrip("./").lower()
    if p == ".env" or p.endswith("/.env"):
        return True
    return p == "raw_books" or p.startswith("raw_books/") or p == "workspace" or p.startswith("workspace/")


def _explain_push_error(err):
    e = (err or "").lower()
    tail = (err or "").strip()[:180]
    if any(k in e for k in ("could not resolve", "unable to access", "timed out", "connection", "network", "early eof", "couldn't connect", "operation timed out")):
        return "网络超时或无法访问远程仓库，请检查网络 / 代理后重试。" + ("（" + tail + "）" if tail else "")
    if any(k in e for k in ("non-fast-forward", "rejected", "fetch first", "conflict", "would be overwritten")):
        return "远程仓库有新的提交，推送被拒绝：请先拉取远程更新（git pull）后再同步。" + ("（" + tail + "）" if tail else "")
    if any(k in e for k in ("authentication", "permission", "credentials", "403", "401", "not authorized")):
        return "远程仓库鉴权失败，请检查 Git 凭据 / token 后重试。" + ("（" + tail + "）" if tail else "")
    return "git push 失败：" + tail


@app.get("/api/git/status")
def git_status(request: Request):
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问")
    files = _git_status_files()
    return {"has_changes": bool(files), "count": len(files), "files": files}


@app.post("/api/git/sync")
def git_sync(request: Request):
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问")

    # 1) 检查 .gitignore（用于判断敏感路径是否被忽略）
    gi_text = ""
    try:
        gi_text = (ROOT / ".gitignore").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        gi_text = ""

    # 2) 扫描待提交变更（git status 已遵循 .gitignore），硬拦截敏感文件 / 目录
    sensitive_hits = []
    for f in _git_status_files():
        if not _is_sensitive(f):
            continue
        base = f.replace("\\", "/").split("/")[0].lower()
        if base not in gi_text and f.lower() not in gi_text:
            sensitive_hits.append(f + "（未在 .gitignore 中忽略）")
        else:
            sensitive_hits.append(f)
    if sensitive_hits:
        raise HTTPException(
            status_code=400,
            detail="检测到敏感文件将被提交（.env / raw_books/ / workspace/），已中止同步："
            + "、".join(sensitive_hits[:6]),
        )

    proc = _run_git(["git", "add", "-A"], ROOT)
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="git add 失败：" + (proc.stderr or proc.stdout).strip()[:200])

    if _run_git(["git", "diff", "--cached", "--quiet"], ROOT).returncode == 0:
        return {"status": "clean", "message": "线上与本地已是最新，无需同步"}

    msg = "sync: update Librarium (" + datetime.now().strftime("%Y-%m-%d %H:%M") + ")"
    try:
        proc = _run_git(["git", "commit", "-m", msg], ROOT, timeout=45)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=400, detail="提交超时（45 秒），请稍后重试或检查网络 / 代理。")
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="git commit 失败：" + (proc.stderr or proc.stdout).strip()[:300])

    try:
        proc = _run_git(["git", "push"], ROOT, timeout=45)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=400, detail="推送超时（45 秒），请检查网络 / 代理后重试。")
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=_explain_push_error(proc.stderr or proc.stdout))

    return {"status": "ok", "message": "已同步至 GitHub，线上网页约 1–2 分钟后更新"}


@app.post("/api/shutdown")
def shutdown(request: Request):
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问")

    def _stop():
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return {"status": "ok", "message": "本地服务已休眠，下次双击桌面图标即可唤醒"}


# 静态站点服务（挂载项目根目录，须在 API 路由之后注册）
app.mount("/", StaticFiles(directory=str(ROOT), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
