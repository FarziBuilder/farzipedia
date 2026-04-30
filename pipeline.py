"""Orchestrate the full URL → blog pipeline."""

import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from transcript import extract_video_id, fetch_snippets
from planner import plan_timestamps
from analyzer import analyze
import trivia as trivia_mod


def _canonical_youtube_url(video_id: str) -> str:
    """Strip playlist/index/si params; yt-dlp behaves better on a clean URL."""
    return f"https://www.youtube.com/watch?v={video_id}"


def _cookies_args() -> list:
    """Pass --cookies if a cookie file is available.

    YouTube's 2024+ anti-bot check ("Sign in to confirm you're not a
    bot") requires an authenticated session for most video metadata.
    On Render, mount a YouTube cookies.txt as a Secret File at
    /etc/secrets/cookies.txt (or override the path with YT_COOKIES_FILE).

    Render mounts secret files read-only, but yt-dlp tries to write
    refreshed cookies back. Copy the file to /tmp on every call so
    yt-dlp can update its mutable copy without crashing.
    """
    src = os.environ.get("YT_COOKIES_FILE", "/etc/secrets/cookies.txt")
    if not os.path.isfile(src):
        return []
    dest = "/tmp/yt-cookies.txt"
    try:
        shutil.copyfile(src, dest)
    except OSError:
        return []
    return ["--cookies", dest]


def _proxy_args() -> list:
    """yt-dlp proxy flags. Two modes:

    1. Webshare rotating endpoint (default):
       Set WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD only.
       Username gets `-rotate` appended automatically.

    2. Webshare static datacenter proxy (free plan default):
       Set WEBSHARE_PROXY_HOST and WEBSHARE_PROXY_PORT to a specific
       IP:port from your Proxy List. Username is used as-is.

    3. Any other HTTP/HTTPS proxy via HTTP_PROXY / HTTPS_PROXY.
    """
    user = os.environ.get("WEBSHARE_PROXY_USERNAME", "").strip()
    pwd = os.environ.get("WEBSHARE_PROXY_PASSWORD", "").strip()
    host = os.environ.get("WEBSHARE_PROXY_HOST", "p.webshare.io").strip()
    port = os.environ.get("WEBSHARE_PROXY_PORT", "80").strip()

    if user and pwd:
        # Use the username verbatim — different Webshare account types accept
        # different username formats (bare, `-rotate`, `-country-XX-rotate`, etc.).
        # If you want rotation, append `-rotate` yourself in the env var.
        return ["--proxy", f"http://{user}:{pwd}@{host}:{port}"]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        return ["--proxy", proxy]
    return []


def _yt_dlp_metadata(url: str) -> dict:
    """Fast metadata-only fetch (no download). Returns {} on failure."""
    args = [
        sys.executable, "-m", "yt_dlp",
        "--extractor-args", "youtube:player_client=tv,web_safari,android,web,ios,mweb",
        "--no-playlist", "--skip-download", "--print-json", "--no-progress",
        "--socket-timeout", "20",
        *_proxy_args(),
        *_cookies_args(),
        url,
    ]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {proc.stderr.strip()[-500:]}")
    for line in proc.stdout.strip().splitlines()[::-1]:
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def _yt_dlp_download(url: str, out_path: Path) -> dict:
    """Download a single-file mp4 ≤480p. Format 18 (360p mp4) is the
    universal fallback YouTube always serves."""
    args = [
        sys.executable, "-m", "yt_dlp",
        "--extractor-args", "youtube:player_client=tv,web_safari,android,web,ios,mweb",
        "-f", "b[height<=480][ext=mp4]/18/best[height<=480]/best",
        "--no-playlist", "--print-json", "--no-progress",
        "--socket-timeout", "30",
        "--retries", "5",
        "--fragment-retries", "5",
        *_proxy_args(),
        *_cookies_args(),
        "-o", str(out_path),
        url,
    ]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=480)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {proc.stderr.strip()[-500:]}")
    for line in proc.stdout.strip().splitlines()[::-1]:
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def _ffprobe_duration(video_path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(video_path)],
        capture_output=True, text=True, check=True,
    )
    return float(proc.stdout.strip())


def _extract_frame(video_path: Path, t: float, out_path: Path):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t}", "-i", str(video_path),
         "-frames:v", "1", "-q:v", "4", "-y", str(out_path)],
        check=True,
    )


def estimate_eta_seconds(duration_seconds: float) -> int:
    """Rough estimate of total pipeline time, in seconds.

    Empirically: download ~25-50 KB/s sustained worst-case for a 360p mp4
    (~7 MB/min of video). Frame extraction is fast. Claude vision call
    scales with frame count, ~30-90s.
    """
    download = max(15.0, duration_seconds * 0.4)  # worst-ish case
    frames = 8.0  # parallelized ffmpeg
    analysis = 30.0 + min(60.0, duration_seconds * 0.06)  # frames-driven
    buffer = 10.0
    return int(download + frames + analysis + buffer)


def run(url: str, job_dir: Path,
        progress: Optional[Callable[[str, float], None]] = None,
        on_meta: Optional[Callable[[dict], None]] = None,
        on_trivia: Optional[Callable[[list], None]] = None) -> dict:
    """Execute the full pipeline. Returns the blog dict.

    `on_meta` is called as soon as we have title / duration / uploader.
    `on_trivia` is called once trivia is ready (runs in parallel with download).
    """
    def step(msg: str, frac: float):
        if progress:
            progress(msg, frac)

    job_dir.mkdir(parents=True, exist_ok=True)
    shots_dir = job_dir / "screenshots"
    shots_dir.mkdir(exist_ok=True)

    step("Resolving video", 0.02)
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError("Could not extract a YouTube video id from that URL.")
    canonical = _canonical_youtube_url(video_id)

    step("Fetching transcript", 0.06)
    snippets = fetch_snippets(video_id)
    if not snippets:
        raise RuntimeError("No transcript snippets returned (captions may be disabled).")

    step("Reading video info", 0.10)
    meta = _yt_dlp_metadata(canonical)
    title = meta.get("title", "")
    uploader = meta.get("uploader") or meta.get("channel", "")
    description = meta.get("description") or ""
    duration = float(meta.get("duration") or 0.0)
    thumbnail = meta.get("thumbnail", "")

    if on_meta:
        on_meta({
            "title": title,
            "uploader": uploader,
            "duration": duration,
            "thumbnail": thumbnail,
            "video_id": video_id,
            "eta_seconds": estimate_eta_seconds(duration) if duration else None,
        })

    # Kick off trivia in a background thread — it runs in parallel with the
    # video download so it's ready by the time the loading screen needs it.
    def _trivia_worker():
        try:
            items = trivia_mod.generate(title=title, channel=uploader, description=description)
            if on_trivia:
                on_trivia(items)
        except Exception:
            pass

    if on_trivia:
        threading.Thread(target=_trivia_worker, daemon=True).start()

    step("Downloading video", 0.15)
    video_path = job_dir / "video.mp4"
    dl_meta = _yt_dlp_download(canonical, video_path)
    # Prefer metadata from the actual download (sometimes more accurate).
    title = dl_meta.get("title", title)
    uploader = dl_meta.get("uploader") or dl_meta.get("channel") or uploader
    duration = float(dl_meta.get("duration") or duration or _ffprobe_duration(video_path))

    step("Planning capture moments", 0.45)
    max_frames = min(60, max(20, int(duration / 60 * 3)))
    timestamps = plan_timestamps(snippets, duration, max_total=max_frames)

    step(f"Extracting {len(timestamps)} frames", 0.50)
    frames = []

    def _extract_one(t):
        out = shots_dir / f"t{int(round(t))}.jpg"
        _extract_frame(video_path, t, out)
        return {"timestamp": t, "path": out}

    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in ex.map(_extract_one, timestamps):
            frames.append(f)

    step("Asking Claude to write the post", 0.65)
    blog = analyze(snippets, frames, video_title=title)

    blog["meta"] = {
        "video_id": video_id,
        "url": url,
        "title": title,
        "uploader": uploader,
        "duration_seconds": duration,
        "thumbnail": thumbnail,
        "n_frames": len(frames),
    }

    step("Saving result", 0.95)
    (job_dir / "blog.json").write_text(json.dumps(blog, indent=2), encoding="utf-8")

    try:
        video_path.unlink()
    except OSError:
        pass

    step("Done", 1.0)
    return blog
