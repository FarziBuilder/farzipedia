"""Generate timestamped transcripts via OpenAI Whisper.

Replaces the previous youtube-transcript-api flow because YouTube
aggressively blocks transcript requests from data-centre IPs even
through residential rotating proxies. Whisper transcribes the audio
we've already downloaded, sidestepping the entire YouTube transcript
endpoint.

Public API:
    extract_video_id(url) -> str | None
    transcribe_audio(audio_path) -> List[{start, duration, text}]
"""

import os
import re
from pathlib import Path
from typing import List, Optional

YOUTUBE_ID_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?(?:[^&]*&)*v=)([\w-]{11})"),
    re.compile(r"(?:youtu\.be/)([\w-]{11})"),
    re.compile(r"(?:youtube\.com/shorts/)([\w-]{11})"),
    re.compile(r"(?:youtube\.com/embed/)([\w-]{11})"),
    re.compile(r"(?:youtube\.com/v/)([\w-]{11})"),
]


def extract_video_id(url: str) -> Optional[str]:
    for pattern in YOUTUBE_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    bare = url.strip()
    if re.fullmatch(r"[\w-]{11}", bare):
        return bare
    return None


def transcribe_audio(audio_path: Path, language: Optional[str] = None) -> List[dict]:
    """Send an audio file to OpenAI Whisper and return timestamped segments
    in the same shape we used before: [{start, duration, text}, ...].

    Whisper API limits: 25 MB per file. The pipeline encodes audio at
    32 kbps mono MP3 to keep most videos under that.
    """
    from openai import OpenAI

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Whisper-based transcription requires "
            "an OpenAI API key — get one at https://platform.openai.com/api-keys "
            "and set it as an env var on Render."
        )

    client = OpenAI()
    with open(audio_path, "rb") as f:
        kwargs = {
            "model": "whisper-1",
            "file": f,
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment"],
        }
        if language:
            kwargs["language"] = language
        result = client.audio.transcriptions.create(**kwargs)

    # `result.segments` is a list of objects with .start, .end, .text
    segments = getattr(result, "segments", None) or []
    out: List[dict] = []
    for seg in segments:
        start = float(getattr(seg, "start", 0.0))
        end = float(getattr(seg, "end", start + 1.0))
        text = (getattr(seg, "text", "") or "").strip()
        if text:
            out.append({
                "start": start,
                "duration": max(0.5, end - start),
                "text": text,
            })
    return out
