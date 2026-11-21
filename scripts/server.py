# -*- coding: utf-8 -*-
"""Librarium 本地制书后端（FastAPI）。

启动方式（二选一）：
    uvicorn scripts.server:app --host 127.0.0.1 --port 8000
    python scripts/server.py

启动后访问 http://localhost:8000 展示主页与地图页。
"""

import shutil
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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
def upload(file: UploadFile = File(...), slug: str = Form(...)):
    slug = pipeline.sanitize_slug(slug)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".pdf", ".epub"):
        raise HTTPException(status_code=400, detail="仅支持 .pdf 或 .epub 文件")
    pipeline.RAW_BOOKS.mkdir(parents=True, exist_ok=True)
    dest = pipeline.RAW_BOOKS / f"{slug}{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        result = pipeline.split_precheck(slug, dest)
    except pipeline.ScannedBookError as e:
        return {"status": "scanned", "message": str(e), "chapters": []}
    result["saved_to"] = str(dest)
    return result


@app.post("/api/books/resplit")
def resplit(slug: str = Form(...), page_map: str = Form(...)):
    slug = pipeline.sanitize_slug(slug)
    book = pipeline.find_raw_book(slug)
    if not book:
        raise HTTPException(status_code=404, detail="未找到该 slug 对应的原始电子书，请先上传。")
    workspace = pipeline.WORKSPACE / slug
    workspace.mkdir(parents=True, exist_ok=True)
    map_file = workspace / "pages_map.txt"
    map_file.write_text(page_map, encoding="utf-8")
    try:
        result = pipeline.split_precheck(slug, book, page_map=str(map_file))
    except pipeline.ScannedBookError as e:
        return {"status": "scanned", "message": str(e), "chapters": []}
    return result


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


# 静态站点服务（挂载项目根目录，须在 API 路由之后注册）
app.mount("/", StaticFiles(directory=str(ROOT), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
