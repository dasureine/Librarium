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
def upload(
    files: list[UploadFile] = File(...),
    slug: str = Form(...),
    offset: int = Form(0),
    skip_noncontent: bool = Form(True),
):
    """上传并解析原始电子书。

    支持 .pdf / .epub / .txt；多选图片会按顺序合并为一份扫描版 PDF。
    上传后立刻做格式与目录体检：带目录且有文字层直接拆章，
    无目录返回 needs_page_map 让前端填写目录，扫描版返回 scanned 提示 OCR。
    """
    slug = pipeline.sanitize_slug(slug)
    if not files:
        raise HTTPException(status_code=400, detail="未收到任何文件")

    names = [f.filename or "" for f in files]
    exts = [Path(name).suffix.lower() for name in names]
    supported = pipeline.split_book.SUPPORTED_EXTS
    bad = [name for name, ext in zip(names, exts) if ext not in supported]
    if bad:
        raise HTTPException(
            status_code=400,
            detail="不支持的文件格式：" + "、".join(bad) + "（仅支持 PDF / EPUB / TXT / 图片）",
        )

    image_exts = pipeline.split_book.SUPPORTED_IMAGE_EXTS
    if len(files) > 1 and any(ext not in image_exts for ext in exts):
        raise HTTPException(
            status_code=400,
            detail="一次只能上传一份 PDF / EPUB / TXT；图片可以多选，按顺序合并为扫描版 PDF。",
        )

    pipeline.RAW_BOOKS.mkdir(parents=True, exist_ok=True)
    try:
        if all(ext in image_exts for ext in exts):
            tmp_dir = pipeline.RAW_BOOKS / f".{slug}_images"
            shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            image_paths = []
            try:
                for index, (upload_file, ext) in enumerate(zip(files, exts)):
                    image_path = tmp_dir / f"{index:04d}{ext}"
                    with image_path.open("wb") as fh:
                        shutil.copyfileobj(upload_file.file, fh)
                    image_paths.append(image_path)
                dest = pipeline.RAW_BOOKS / f"{slug}.pdf"
                pipeline.split_book.images_to_pdf(image_paths, dest)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            dest = pipeline.RAW_BOOKS / f"{slug}{exts[0]}"
            with dest.open("wb") as fh:
                shutil.copyfileobj(files[0].file, fh)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"保存文件失败：{exc}")

    inspection = pipeline.inspect_book(dest)
    pipeline.write_source_manifest(slug, dest, inspection)

    try:
        result = pipeline.split_precheck(
            slug, dest, offset=offset, skip_noncontent=skip_noncontent
        )
    except pipeline.ScannedBookError as e:
        return {
            "status": "scanned",
            "message": str(e),
            "chapters": [],
            "inspection": inspection,
            "saved_to": str(dest),
        }
    result["saved_to"] = str(dest)
    return result


@app.post("/api/books/resplit")
def resplit(
    slug: str = Form(...),
    page_map: str = Form(""),
    offset: int = Form(0),
    skip_noncontent: bool = Form(True),
):
    slug = pipeline.sanitize_slug(slug)
    book = pipeline.find_raw_book(slug)
    if not book:
        raise HTTPException(status_code=404, detail="未找到该 slug 对应的原始电子书，请先上传。")
    workspace = pipeline.WORKSPACE / slug
    workspace.mkdir(parents=True, exist_ok=True)
    map_file = None
    if page_map.strip():
        map_file = workspace / "pages_map.txt"
        map_file.write_text(page_map, encoding="utf-8")
    inspection = pipeline.inspect_book(book)
    if not pipeline.read_source_manifest(slug):
        pipeline.write_source_manifest(slug, book, inspection)
    try:
        result = pipeline.split_precheck(
            slug,
            book,
            page_map=str(map_file) if map_file else None,
            offset=offset,
            skip_noncontent=skip_noncontent,
        )
    except pipeline.ScannedBookError as e:
        return {
            "status": "scanned",
            "message": str(e),
            "chapters": [],
            "inspection": inspection,
        }
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


@app.get("/api/books/completion/{slug}")
def completion(slug: str):
    """返回某本书的分章完成度（供「生成整书总图」按钮判断）。"""
    slug = pipeline.sanitize_slug(slug)
    return pipeline.chapters_completion(slug)


@app.post("/api/books/finalize")
def finalize(
    title: str = Form(...),
    author: str = Form(...),
    slug: str = Form(...),
):
    """整书分章全部完成后，一键生成全书总览与整书总图。"""
    slug = pipeline.sanitize_slug(slug)
    progress = pipeline.chapters_completion(slug)
    if progress["total"] <= 0:
        raise HTTPException(status_code=400, detail="该书还没有切分章节，请先上传。")
    # 同一本书已有任务在跑时直接复用，避免重复点击生成两份总图。
    for running in pipeline.list_jobs():
        if running.get("slug") == slug and running.get("status") in ("queued", "running"):
            return {
                "job_id": running["job_id"],
                "status": running["status"],
                "slug": slug,
                "reused": True,
            }
    # 个别分章图谱缺失/残缺不阻止生成：整书任务会按原文指纹补齐后再出总图。
    job = pipeline.start_job(title=title, author=author, slug=slug, chapter_range=None)
    return {
        "job_id": job.id,
        "status": job.status,
        "slug": job.slug,
        "total_chapters": progress["total"],
        "missing_chapters": progress["missing"],
    }


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


# ---------- 本机书库：查看 / 打开已拆书的原文本 ----------

def _open_path(path: Path) -> None:
    """在本机文件管理器（Windows 资源管理器）中打开指定目录。"""
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [opener, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@app.get("/api/library/overview")
def library_overview():
    """列出 workspace/ 下所有已拆书及其原文本、笔记、地图数量。"""
    titles = {}
    for book in pipeline.load_books():
        if isinstance(book, dict) and book.get("id"):
            titles[book["id"]] = book.get("title") or ""
    books = []
    workspace = pipeline.WORKSPACE
    if workspace.is_dir():
        for entry in sorted(workspace.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            chapters_dir = entry / "chapters"
            chapters = sorted(chapters_dir.glob("*.txt")) if chapters_dir.is_dir() else []
            notes_dir = entry / "chapter_notes"
            notes = sorted(notes_dir.glob("*.md")) if notes_dir.is_dir() else []
            maps_dir = pipeline.MAPS_DIR / entry.name
            maps = sorted(maps_dir.glob("ch*.html")) if maps_dir.is_dir() else []
            manifest = pipeline.read_source_manifest(entry.name)
            books.append({
                "slug": entry.name,
                "title": titles.get(entry.name) or entry.name,
                "source": manifest.get("filename") or "",
                "format": manifest.get("format") or "",
                "chapters": len(chapters),
                "notes": len(notes),
                "maps": len(maps),
                "path": str(chapters_dir),
            })
    return {
        "workspace": str(workspace),
        "books": books,
        "total_books": len(books),
        "total_chapters": sum(item["chapters"] for item in books),
    }


@app.post("/api/library/open")
def library_open(request: Request, payload: Optional[dict] = Body(None)):
    """在资源管理器中打开书库目录：默认全部原文本，也可定位某本书的 chapters。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问")
    payload = payload or {}
    target = str(payload.get("target") or "workspace").strip().lower()
    slug = pipeline.sanitize_slug(payload.get("slug") or "")
    if target == "raw":
        path = pipeline.RAW_BOOKS
    elif target in ("chapters", "book"):
        if not str(payload.get("slug") or "").strip():
            raise HTTPException(status_code=400, detail="缺少 slug")
        path = pipeline.WORKSPACE / slug
        if target == "chapters":
            path = path / "chapters"
    else:
        path = pipeline.WORKSPACE
    path.mkdir(parents=True, exist_ok=True)
    try:
        _open_path(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开文件夹失败：{exc}")
    return {"status": "ok", "path": str(path)}


# 静态站点服务（挂载项目根目录，须在 API 路由之后注册）
app.mount("/", StaticFiles(directory=str(ROOT), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
