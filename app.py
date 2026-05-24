"""Farzipedia: turn any YouTube video into a beautiful blog post."""

import base64
import json
import os
import re
import threading
import time
import traceback
import uuid
from collections import defaultdict, deque
from io import StringIO
from pathlib import Path
from typing import Optional, Tuple

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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

# Open CORS for /v1/* — the Chrome extension calls these from a
# chrome-extension:// origin and we don't expose user data through them.
# Real safety comes from the Anthropic spend cap + rate limiter, not CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# --- Anthropic proxy state ----------------------------------------------------
# In-memory token-bucket-ish: a deque of request times per IP. No persistence
# across redeploys, no cross-instance sharing — fine for a single Render
# instance behind a CDN. If we ever scale out, swap to Redis.
PROXY_RATE_LIMIT = int(os.environ.get("PROXY_RATE_LIMIT", "20"))   # requests
PROXY_RATE_WINDOW = int(os.environ.get("PROXY_RATE_WINDOW", "60"))  # seconds
_proxy_buckets: dict[str, deque] = defaultdict(deque)
_proxy_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """Best-effort client IP behind Render's load balancer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> Tuple[bool, int]:
    """Returns (allowed, retry_after_seconds)."""
    now = time.monotonic()
    with _proxy_lock:
        bucket = _proxy_buckets[ip]
        cutoff = now - PROXY_RATE_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= PROXY_RATE_LIMIT:
            retry = int(PROXY_RATE_WINDOW - (now - bucket[0])) + 1
            return False, max(retry, 1)
        bucket.append(now)
        return True, 0


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


# ============================================================================
#  Folio discovery — read everything in JOBS_ROOT/ that has a blog.json
# ============================================================================

def _safe_job_id(s: str) -> str:
    """Allow only the shape our generators emit: hex or job-<ts>-<rand>."""
    return s if re.fullmatch(r"[A-Za-z0-9_\-]{4,64}", s or "") else ""


def _read_folio(job_dir: Path) -> Optional[dict]:
    """Read a stored folio's summary for the homepage gallery."""
    blog_path = job_dir / "blog.json"
    if not blog_path.exists():
        return None
    try:
        blog = json.loads(blog_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    meta = blog.get("meta") or {}
    video_id = meta.get("videoId") or meta.get("video_id") or ""
    title = blog.get("title") or "Untitled folio"
    subtitle = blog.get("subtitle") or ""
    channel = meta.get("channel") or meta.get("uploader") or ""
    duration = meta.get("durationSeconds") or meta.get("duration_seconds") or 0
    yt_url = meta.get("url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")

    # Cover thumbnail: YouTube hqdefault if we know the video id, else first screenshot.
    cover = ""
    if video_id:
        cover = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    else:
        shots = _resolve_screenshots(job_dir.name, job_dir)
        if shots:
            cover = next(iter(shots.values()))

    # mtime for sort order
    try:
        mtime = blog_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    duration_str = ""
    if duration:
        mins = max(1, int(round(float(duration) / 60)))
        duration_str = f"{mins} min"

    return {
        "job_id": job_dir.name,
        "title": title,
        "subtitle": subtitle,
        "channel": channel,
        "duration_str": duration_str,
        "url": f"/blog/{job_dir.name}",
        "youtube_url": yt_url,
        "cover": cover,
        "video_id": video_id,
        "mtime": mtime,
    }


_ROMAN_PAIRS = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def _to_roman(n: int) -> str:
    if n <= 0:
        return ""
    out = []
    for value, sym in _ROMAN_PAIRS:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def _list_folios(limit: int = 60) -> list[dict]:
    out: list[dict] = []
    if not JOBS_ROOT.exists():
        return out
    for entry in JOBS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        f = _read_folio(entry)
        if f:
            out.append(f)
    out.sort(key=lambda x: x["mtime"], reverse=True)
    out = out[:limit]
    # Number them I, II, III… in the order they're displayed (newest first).
    for i, f in enumerate(out, start=1):
        f["roman"] = _to_roman(i)
    return out


# ============================================================================
#  Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    folios = _list_folios()
    return templates.TemplateResponse(
        request, "index.html", {"folios": folios},
    )


# ============================================================================
#  Anthropic proxy — extension calls this so users don't need their own key.
#  Body is forwarded verbatim to api.anthropic.com/v1/messages with the
#  server's ANTHROPIC_API_KEY attached.
# ============================================================================

@app.post("/v1/proxy/messages")
async def proxy_messages(request: Request):
    server_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not server_key:
        raise HTTPException(503, "Proxy is not configured (no ANTHROPIC_API_KEY on server).")

    ip = _client_ip(request)
    allowed, retry_after = _check_rate_limit(ip)
    if not allowed:
        return JSONResponse(
            {"error": {"type": "rate_limit_error",
                       "message": f"farzi.me proxy: too many requests, retry in {retry_after}s"}},
            status_code=429,
            headers={"retry-after": str(retry_after)},
        )

    try:
        body = await request.body()
    except Exception as e:
        raise HTTPException(400, f"Could not read request body: {e}")

    upstream_headers = {
        "x-api-key": server_key,
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        "content-type": "application/json",
    }
    # Echo a couple of optional beta headers the caller may set.
    for h in ("anthropic-beta",):
        v = request.headers.get(h)
        if v:
            upstream_headers[h] = v

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                content=body,
                headers=upstream_headers,
            )
    except httpx.TimeoutException:
        return JSONResponse(
            {"error": {"type": "timeout_error",
                       "message": "Upstream Anthropic API timed out via farzi.me proxy."}},
            status_code=504,
        )
    except httpx.HTTPError as e:
        return JSONResponse(
            {"error": {"type": "proxy_error", "message": f"Proxy network error: {e}"}},
            status_code=502,
        )

    # Pass status + body through unchanged. Strip hop-by-hop headers.
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


# ============================================================================
#  Folio ingestion — extension uploads its generated blog + frames here.
#  We write blog.json + screenshots/tNNN.jpg so the existing /blog/{id}
#  route serves the post and the homepage picks it up automatically.
# ============================================================================

_FRAME_TS_RE = re.compile(r"^t\d{1,5}$")


def _persist_folio(payload: dict) -> str:
    """Validate + write to disk. Returns the new job_id."""
    blog = payload.get("blog")
    if not isinstance(blog, dict) or not blog.get("title"):
        raise HTTPException(400, "Missing or invalid 'blog' object.")

    frames = payload.get("frames") or []
    if not isinstance(frames, list):
        raise HTTPException(400, "'frames' must be a list.")

    # Allow client to suggest a job id but sanitize it.
    raw_id = payload.get("job_id") or ""
    job_id = _safe_job_id(raw_id) or f"ext-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    job_dir = JOBS_ROOT / job_id
    shots_dir = job_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    # Decode frames. Accept {timestamp, dataB64, mediaType?} or {timestamp, dataUrl}.
    written = 0
    for f in frames[:200]:  # hard cap
        try:
            ts = float(f.get("timestamp"))
        except (TypeError, ValueError):
            continue
        b64 = f.get("dataB64") or ""
        if not b64 and isinstance(f.get("dataUrl"), str):
            m = re.match(r"^data:[^;]+;base64,(.*)$", f["dataUrl"])
            if m:
                b64 = m.group(1)
        if not b64:
            continue
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception:
            continue
        media = (f.get("mediaType") or "image/jpeg").lower()
        ext = ".jpg" if "jpeg" in media or "jpg" in media else (".png" if "png" in media else ".jpg")
        stem = f"t{int(round(ts)):03d}"
        out_path = shots_dir / f"{stem}{ext}"
        try:
            out_path.write_bytes(data)
            written += 1
        except OSError:
            continue

    # Persist blog.json (overwrite if same job_id submitted twice).
    (job_dir / "blog.json").write_text(
        json.dumps(blog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return job_id


@app.post("/v1/folios")
async def upload_folio(request: Request):
    ip = _client_ip(request)
    allowed, retry_after = _check_rate_limit(ip)
    if not allowed:
        return JSONResponse(
            {"error": f"too many requests, retry in {retry_after}s"},
            status_code=429,
            headers={"retry-after": str(retry_after)},
        )
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON.")
    job_id = _persist_folio(payload)
    return JSONResponse({
        "ok": True,
        "job_id": job_id,
        "url": f"/blog/{job_id}",
    })


@app.get("/v1/folios")
def list_folios(limit: int = 60):
    return JSONResponse({"folios": _list_folios(limit=max(1, min(limit, 200)))})


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
