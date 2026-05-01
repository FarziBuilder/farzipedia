"""Orchestrate the full URL → blog pipeline.

Single source of truth for video interaction is `browser.py`, which
drives a remote Chromium (Bright Data Scraping Browser) — sidesteps
every yt-dlp anti-bot wall and gives us metadata, transcript, AND
frame screenshots in one round-trip per video.
"""

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from transcript import extract_video_id
from planner import plan_timestamps
from analyzer import analyze
from browser import capture
import trivia as trivia_mod


def estimate_eta_seconds(duration_seconds: float) -> int:
    """Rough end-to-end pipeline time estimate."""
    # Browser session: page-load + per-frame seek-and-snap
    browser_time = 15.0 + len([1]) * 0  # ~15s base
    # Claude vision call scales with frame count
    analysis = 30.0 + min(60.0, duration_seconds * 0.06)
    buffer = 10.0
    return int(browser_time + analysis + buffer)


def run(url: str, job_dir: Path,
        progress: Optional[Callable[[str, float], None]] = None,
        on_meta: Optional[Callable[[dict], None]] = None,
        on_trivia: Optional[Callable[[list], None]] = None) -> dict:
    """Execute the full pipeline. Returns the blog dict.

    `on_meta` is called as soon as we have title / duration / uploader.
    `on_trivia` is called once trivia is ready (runs in parallel with capture).
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

    # --- Pass 1: open the browser, grab title/duration/transcript first.
    # We need duration to plan capture timestamps, and transcript to feed
    # into the cue-based planner. Then Pass 2 takes the actual frames.
    #
    # In practice both passes happen in the same `capture()` call below,
    # but we plan timestamps based on a quick metadata-only fetch via the
    # browser. Simpler: do a single capture() call with periodic timestamps,
    # then re-plan if we want cue-aware sampling. For v1, do single pass.

    step("Opening remote browser", 0.10)
    # Plan first with a placeholder duration; refine after metadata.
    # Single-pass capture: ask for 8 evenly-spaced frames as a baseline,
    # then we'll do cue-based planning in a future iteration.

    # We need to know duration BEFORE calling capture() with timestamps,
    # so we either (a) call capture twice or (b) ask capture to give us
    # metadata in one trip and frames in another. Cheapest: pre-fetch
    # via a no-frames capture, then real capture with planned timestamps.
    initial = capture(video_id, planned_timestamps=[], screenshots_dir=shots_dir)
    meta = initial["meta"]
    snippets = initial["snippets"]
    title = meta.get("title", "")
    uploader = meta.get("uploader", "")
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

    # Trivia generation kicks off in parallel with the second capture
    def _trivia_worker():
        try:
            items = trivia_mod.generate(title=title, channel=uploader, description="")
            if on_trivia:
                on_trivia(items)
        except Exception:
            pass

    if on_trivia:
        threading.Thread(target=_trivia_worker, daemon=True).start()

    if not snippets:
        raise RuntimeError(
            "No transcript captions were found on the YouTube page. "
            "The video may have captions disabled."
        )

    step("Planning capture moments", 0.40)
    max_frames = min(60, max(20, int(duration / 60 * 3)))
    timestamps = plan_timestamps(snippets, duration, max_total=max_frames)

    step(f"Capturing {len(timestamps)} frames in browser", 0.50)
    cap = capture(video_id, planned_timestamps=timestamps, screenshots_dir=shots_dir)
    frames = cap["frames"]

    step("Asking Claude to write the post", 0.75)
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

    step("Done", 1.0)
    return blog
