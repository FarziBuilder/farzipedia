"""Fetch a YouTube transcript as timestamped snippets.

Supports an optional Webshare residential proxy (set
WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD) — required when running
from a data-centre IP, where YouTube blocks unauthenticated transcript
requests.
"""

import os
import re
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


def _normalize(fetched) -> List[dict]:
    out = []
    for s in fetched:
        if isinstance(s, dict):
            out.append({
                "start": float(s.get("start", 0.0)),
                "duration": float(s.get("duration", 0.0)),
                "text": s.get("text", ""),
            })
        else:
            out.append({
                "start": float(getattr(s, "start", 0.0)),
                "duration": float(getattr(s, "duration", 0.0)),
                "text": getattr(s, "text", ""),
            })
    return out


def _build_proxy_config():
    """Return a proxy_config object if env vars are set, else None.

    Mirrors pipeline._proxy_args() so the transcript fetch and the
    yt-dlp download go through the same proxy with consistent creds.
    """
    user = os.environ.get("WEBSHARE_PROXY_USERNAME", "").strip()
    pwd = os.environ.get("WEBSHARE_PROXY_PASSWORD", "").strip()
    host = os.environ.get("WEBSHARE_PROXY_HOST", "p.webshare.io").strip()
    port = os.environ.get("WEBSHARE_PROXY_PORT", "80").strip()

    if user and pwd:
        # Use the username verbatim — different Webshare account types accept
        # different formats. Append `-rotate` in the env var yourself if needed.
        proxy_url = f"http://{user}:{pwd}@{host}:{port}"
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
            return GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
        except ImportError:
            pass

    # Generic proxy fallback (HTTP_PROXY / HTTPS_PROXY)
    http_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if http_url:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
            return GenericProxyConfig(http_url=http_url, https_url=http_url)
        except ImportError:
            pass
    return None


def fetch_snippets(video_id: str, languages: Optional[List[str]] = None) -> List[dict]:
    """Fetch with retry — YouTube drops occasional SSL handshakes through
    residential proxies when an exit IP is on its blocklist. With
    -rotate suffix on the Webshare username, each retry lands on a
    different IP, so a few retries usually succeed.
    """
    import time
    from youtube_transcript_api import YouTubeTranscriptApi

    proxy_config = _build_proxy_config()
    last_error: Optional[Exception] = None

    # Try to import the specific exception classes — let us check by type
    # rather than relying on error-message text matching.
    try:
        from youtube_transcript_api._errors import RequestBlocked, IpBlocked
        BLOCK_ERRORS: tuple = (RequestBlocked, IpBlocked)
    except ImportError:
        BLOCK_ERRORS = ()

    for attempt in range(6):
        try:
            if hasattr(YouTubeTranscriptApi, "fetch") or hasattr(YouTubeTranscriptApi(), "fetch"):
                api = YouTubeTranscriptApi(proxy_config=proxy_config) if proxy_config else YouTubeTranscriptApi()
                kwargs = {"languages": languages} if languages else {}
                return _normalize(api.fetch(video_id, **kwargs))
            kwargs = {"languages": languages} if languages else {}
            return _normalize(YouTubeTranscriptApi.get_transcript(video_id, **kwargs))
        except Exception as e:
            msg = str(e).lower()
            # YouTube IP-block errors: retry — rotation will give a fresh IP.
            blocked = isinstance(e, BLOCK_ERRORS) or "is blocking your requests" in msg
            # SSL / transport errors: also retry.
            transport = any(s in msg for s in (
                "ssl", "eof", "connection reset", "max retries", "remote disconnected",
            ))
            last_error = e
            if not (blocked or transport) or attempt == 5:
                raise
            # Longer waits when blocked so the proxy has time to rotate IP
            # (1s, 2s, 3s, 5s, 8s ≈ Fibonacci, total ~19s worst case)
            wait = (1, 2, 3, 5, 8)[attempt]
            time.sleep(wait)

    if last_error is not None:
        raise last_error
    raise RuntimeError("transcript fetch failed without an error")
