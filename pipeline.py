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
    # Single browser session for everything: capture() opens the page,
    # extracts metadata + transcript, then captures the planned frames
    # in the SAME session. We use a metadata-aware planner_factory so
    # capture() can call back into the planner once duration is known.
    def planner_factory(meta_dict, snippets_list):
        """Called by capture() once metadata + transcript are extracted.
        Returns the list of timestamps to seek to."""
        nonlocal title, uploader, duration, thumbnail, snippets
        title = meta_dict.get("title", "")
        uploader = meta_dict.get("uploader", "")
        duration = float(meta_dict.get("duration") or 0.0)
        thumbnail = meta_dict.get("thumbnail", "")
        snippets = snippets_list

        if on_meta:
            on_meta({
                "title": title,
                "uploader": uploader,
                "duration": duration,
                "thumbnail": thumbnail,
                "video_id": video_id,
                "eta_seconds": estimate_eta_seconds(duration) if duration else None,
            })

        # Trivia in a daemon thread, runs in parallel with frame capture
        if on_trivia:
            def _trivia_worker():
                try:
                    items = trivia_mod.generate(title=title, channel=uploader, description="")
                    if on_trivia:
                        on_trivia(items)
                except Exception:
                    pass
            threading.Thread(target=_trivia_worker, daemon=True).start()

        if not snippets_list:
            # Plan periodic-only without transcript-driven cues
            duration_for_plan = duration or 600.0
            n = min(40, max(8, int(duration_for_plan / 60 * 3)))
            return [duration_for_plan * (i + 0.5) / n for i in range(n)]

        max_frames = min(60, max(20, int(duration / 60 * 3)))
        return plan_timestamps(snippets_list, duration, max_total=max_frames)

    # Initialise outer-scope vars that planner_factory will populate
    title = ""
    uploader = ""
    duration = 0.0
    thumbnail = ""
    snippets = []

    step("Capturing video", 0.30)
    cap = capture(
        video_id,
        planned_timestamps=[],  # signal: use planner_factory
        screenshots_dir=shots_dir,
        planner_factory=planner_factory,
    )
    frames = cap["frames"]

    # Persist the capture log to disk for post-mortem debugging.
    try:
        log_lines = []
        for entry in cap.get("logs") or []:
            t_s = entry.get("t", 0)
            msg = entry.get("msg", "")
            extras = {k: v for k, v in entry.items() if k not in ("t", "msg")}
            extras_str = ""
            if extras:
                extras_str = "  " + " | ".join(f"{k}={v}" for k, v in extras.items())
            log_lines.append(f"[{t_s:6.2f}s] {msg}{extras_str}")
        (job_dir / "capture.log").write_text("\n".join(log_lines), encoding="utf-8")
    except Exception:
        pass

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
