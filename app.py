"""Farzipedia: turn any YouTube video into a beautiful blog post."""

import json
import re
import threading
import time
import traceback
import uuid
from io import StringIO
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pipeline import run as run_pipeline

APP_ROOT = Path(__file__).parent.resolve()
JOBS_ROOT = APP_ROOT / "jobs"
JOBS_ROOT.mkdir(exist_ok=True)

app = FastAPI(title="Farzipedia")
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")
app.mount("/jobs", StaticFiles(directory=str(JOBS_ROOT)), name="jobs")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _set(job_id: str, **fields):
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


def _worker(job_id: str, url: str):
    job_dir = JOBS_ROOT / job_id

    def progress(msg: str, frac: float):
        _set(job_id, status="running", message=msg, progress=frac)

    def on_meta(meta: dict):
        _set(job_id, video_meta=meta, started_at=time.time())

    def on_trivia(items: list):
        _set(job_id, trivia=items)

    try:
        blog = run_pipeline(url, job_dir,
                            progress=progress,
                            on_meta=on_meta,
                            on_trivia=on_trivia)
        _set(job_id, status="done", message="Done", progress=1.0, blog=blog)
    except Exception as e:
        _set(job_id, status="error", message=str(e),
             traceback=traceback.format_exc(), progress=0.0)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/process")
def process(url: str = Form(...)):
    url = url.strip()
    if not url:
        raise HTTPException(400, "Please paste a YouTube URL.")

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "url": url,
            "status": "queued",
            "message": "Queued",
            "progress": 0.0,
            "started_at": time.time(),
            "video_meta": None,
            "trivia": [],
        }
    threading.Thread(target=_worker, args=(job_id, url), daemon=True).start()
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job(request: Request, job_id: str):
    with JOBS_LOCK:
        info = JOBS.get(job_id)
    if not info:
        raise HTTPException(404, "Unknown job.")
    if info["status"] == "done":
        return RedirectResponse(f"/blog/{job_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "job.html",
        {"job_id": job_id, "info": info},
    )


@app.get("/job/{job_id}/status")
def job_status(job_id: str):
    with JOBS_LOCK:
        info = JOBS.get(job_id)
    if not info:
        raise HTTPException(404, "Unknown job.")
    started = info.get("started_at") or time.time()
    return JSONResponse({
        "status": info["status"],
        "message": info["message"],
        "progress": info["progress"],
        "video_meta": info.get("video_meta"),
        "trivia": info.get("trivia") or [],
        "elapsed_seconds": int(time.time() - started),
    })


def _format_seconds(s: float) -> str:
    s = int(round(s))
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


def _resolve_screenshots(job_id: str, job_dir: Path) -> dict:
    """Return {timestamp_seconds: served_url} for screenshots on disk."""
    shots_dir = job_dir / "screenshots"
    out = {}
    if shots_dir.exists():
        for p in shots_dir.iterdir():
            stem = p.stem  # tNNN
            if stem.startswith("t") and stem[1:].isdigit():
                out[int(stem[1:])] = f"/jobs/{job_id}/screenshots/{p.name}"
    return out


def _make_resolver(available: dict):
    def resolve(ts) -> str:
        if ts is None or available == {}:
            return ""
        ts_int = int(round(float(ts)))
        if ts_int in available:
            return available[ts_int]
        nearest = min(available.keys(), key=lambda k: abs(k - ts_int))
        return available[nearest] if abs(nearest - ts_int) <= 4 else ""
    return resolve


def _load_blog(job_id: str) -> dict:
    job_dir = JOBS_ROOT / job_id
    blog_path = job_dir / "blog.json"
    if blog_path.exists():
        return json.loads(blog_path.read_text(encoding="utf-8"))
    with JOBS_LOCK:
        info = JOBS.get(job_id) or {}
    if "blog" in info:
        return info["blog"]
    raise HTTPException(404, "Blog not ready yet.")


@app.get("/blog/{job_id}", response_class=HTMLResponse)
def blog(request: Request, job_id: str):
    blog_data = _load_blog(job_id)
    job_dir = JOBS_ROOT / job_id
    available = _resolve_screenshots(job_id, job_dir)
    return templates.TemplateResponse(
        request,
        "blog.html",
        {
            "blog": blog_data,
            "resolve": _make_resolver(available),
            "format_ts": _format_seconds,
            "job_id": job_id,
        },
    )


def _slugify(s: str, default: str = "post") -> str:
    s = re.sub(r"[^A-Za-z0-9\-_ ]+", "", s or "").strip().replace(" ", "-")
    return (s or default).lower()[:80]


def _blog_to_markdown(blog: dict, job_id: str, request_base: str = "") -> str:
    """Render the structured blog dict to Markdown.

    Image references use absolute URLs to the served screenshots so the
    .md file stays self-contained when shared (recipient must hit the
    running server). For a fully portable export, embed images as base64
    in a future iteration.
    """
    out = StringIO()
    title = blog.get("title", "Untitled post")
    subtitle = blog.get("subtitle", "")
    meta = blog.get("meta") or {}

    out.write(f"# {title}\n\n")
    if subtitle:
        out.write(f"> {subtitle}\n\n")
    if meta.get("url"):
        ch = meta.get("uploader", "")
        ch_str = f"{ch} — " if ch else ""
        dur = meta.get("duration_seconds")
        dur_str = f" · {_format_seconds(dur)}" if dur else ""
        out.write(f"{ch_str}[Watch on YouTube]({meta['url']}){dur_str}\n\n")
    out.write("---\n\n")

    # Resolve images relative to job_id; recipients need the server up.
    available = _resolve_screenshots(job_id, JOBS_ROOT / job_id)
    resolver = _make_resolver(available)

    for section in blog.get("sections", []):
        heading = section.get("heading")
        if heading:
            out.write(f"## {heading}\n\n")
        for block in section.get("blocks", []):
            t = block.get("type")
            if t == "paragraph":
                out.write(block.get("text", "").strip() + "\n\n")
            elif t == "image":
                ts = block.get("timestamp")
                cap = block.get("caption", "")
                src = resolver(ts)
                if src:
                    full = request_base + src if request_base and src.startswith("/") else src
                    out.write(f"![{cap}]({full})\n")
                    if cap:
                        out.write(f"*[{_format_seconds(ts)}] {cap}*\n\n")
                    else:
                        out.write("\n")
            elif t == "callout":
                kind = block.get("kind", "key").upper()
                out.write(f"> **{kind}** — {block.get('text', '').strip()}\n\n")
            elif t == "code":
                lang = block.get("language", "")
                out.write(f"```{lang}\n{block.get('text', '')}\n```\n\n")
            elif t == "quote":
                ts = block.get("timestamp")
                ts_str = f" — at {_format_seconds(ts)}" if ts is not None else ""
                out.write(f"> \"{block.get('text', '').strip()}\"{ts_str}\n\n")

    if blog.get("key_takeaways"):
        out.write("## Key takeaways\n\n")
        for kt in blog["key_takeaways"]:
            out.write(f"- {kt}\n")
        out.write("\n")
    out.write(
        f"\n---\n*Generated by Farzipedia from "
        f"[{meta.get('url', 'a YouTube video')}]({meta.get('url', '#')}).*\n"
    )
    return out.getvalue()


@app.get("/blog/{job_id}/download.md")
def download_md(request: Request, job_id: str):
    blog = _load_blog(job_id)
    base = f"{request.url.scheme}://{request.url.netloc}"
    md = _blog_to_markdown(blog, job_id, request_base=base)
    fname = _slugify(blog.get("title", ""), "farzipedia") + ".md"
    return PlainTextResponse(
        md,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Type": "text/markdown; charset=utf-8",
        },
    )


if __name__ == "__main__":
    import os
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=False)
