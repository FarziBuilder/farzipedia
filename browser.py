"""Drive a remote Chromium (Bright Data Scraping Browser / Browserless)
to interact with YouTube as a real browser — sidesteps every yt-dlp
anti-bot wall.

Public API:
    capture(video_id, planned_timestamps, screenshots_dir) -> dict
        returns {meta, snippets, frames, landing, logs}

Env var:
    BD_BROWSER_URL   wss://USER:PASS@host:port
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import time
from pathlib import Path
from typing import List, Optional


def _load_youtube_cookies() -> List[dict]:
    """Load YouTube cookies from /etc/secrets/cookies.txt (Netscape format)."""
    path = os.environ.get("YT_COOKIES_FILE", "/etc/secrets/cookies.txt")
    if not os.path.isfile(path):
        return []
    cookies: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, cpath, secure, expiry, name, value = parts[:7]
                try:
                    expires = int(expiry)
                except ValueError:
                    expires = -1
                cookies.append({
                    "name": name, "value": value, "domain": domain,
                    "path": cpath, "secure": secure.upper() == "TRUE",
                    "httpOnly": False,
                    **({"expires": expires} if expires > 0 else {}),
                    "sameSite": "Lax",
                })
    except OSError:
        return []
    return cookies


def _parse_xml_captions(xml: str) -> List[dict]:
    out: List[dict] = []
    for m in re.finditer(
        r'<text\s+start="([\d.]+)"(?:\s+dur="([\d.]+)")?[^>]*>(.*?)</text>',
        xml, flags=re.DOTALL,
    ):
        start = float(m.group(1))
        dur = float(m.group(2)) if m.group(2) else 1.5
        body = m.group(3)
        body = re.sub(r"<[^>]+>", "", body)
        body = html.unescape(body)
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            out.append({"start": start, "duration": max(0.5, dur), "text": body})
    return out


def _hide_overlay_js() -> str:
    return """
    const clickFirst = (selectors) => {
      for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn) { try { btn.click(); return true; } catch (e) {} }
      }
      return false;
    };
    clickFirst([
      'tp-yt-paper-button[aria-label*="Accept"]',
      'tp-yt-paper-button[aria-label*="Reject"]',
      'button[aria-label*="Accept all"]',
      'button[aria-label*="Reject all"]',
      'button[aria-label*="Reject the use"]',
      'button[aria-label*="Accept the use"]',
      'form[action*="consent"] button[type="submit"]',
    ]);
    for (const b of document.querySelectorAll('button, [role="button"]')) {
      const t = (b.innerText || b.textContent || '').trim();
      if (/^(Reject all|Accept all|I agree)$/i.test(t)) {
        try { b.click(); break; } catch (e) {}
      }
    }
    const hideSel = [
      '.ytp-chrome-bottom', '.ytp-chrome-top', '.ytp-gradient-bottom',
      '.ytp-gradient-top', '.ytp-cc-window-container', '.ytp-pause-overlay',
      '.ytp-watermark', '.ytp-popup', '.ytp-spinner', '.ytp-watch-later-icon',
      'ytd-popup-container', 'tp-yt-paper-dialog',
      'ytd-consent-bump-v2-lightbox', 'ytd-consent-bump-lightbox',
    ];
    for (const s of hideSel) {
      document.querySelectorAll(s).forEach(e => e.style.display = 'none');
    }
    const v = document.querySelector('video');
    if (v) {
      v.style.visibility = 'visible';
      v.style.opacity = '1';
      v.scrollIntoView({block: 'center', inline: 'center'});
    }
    """


_VIDEO_STATE_JS = """
() => {
    const v = document.querySelector('video');
    if (!v) return {present: false};
    const player = document.getElementById('movie_player');
    const buffered = [];
    for (let i = 0; i < v.buffered.length; i++) {
        buffered.push([v.buffered.start(i), v.buffered.end(i)]);
    }
    return {
        present: true,
        currentTime: v.currentTime,
        duration: v.duration,
        paused: v.paused,
        muted: v.muted,
        ended: v.ended,
        readyState: v.readyState,
        networkState: v.networkState,
        videoWidth: v.videoWidth,
        videoHeight: v.videoHeight,
        seeking: v.seeking,
        bufferedRanges: buffered.length,
        firstBuffered: buffered[0] || null,
        error: v.error ? {code: v.error.code, message: v.error.message} : null,
        playerAdShowing: !!(player && player.classList.contains('ad-showing')),
        playerError: !!document.querySelector('.ytp-error, ytd-player-error-message-renderer'),
    };
}
"""


def capture(video_id: str,
            planned_timestamps: List[float],
            screenshots_dir: Path,
            languages: Optional[List[str]] = None,
            debug_landing: bool = False) -> dict:
    """Capture metadata + transcript + frames from a YouTube video.

    Returns:
        {
          "meta":     {title, duration, uploader, thumbnail},
          "snippets": [{start, duration, text}, ...],
          "frames":   [{timestamp, actual_t, path, sha, size, method}, ...],
          "landing":  {url, title, screenshot},
          "logs":     [{t: <s_since_start>, msg: <str>}, ...]
        }
    """
    from playwright.sync_api import sync_playwright

    cdp_url = os.environ.get("BD_BROWSER_URL", "").strip()
    if not cdp_url:
        raise RuntimeError(
            "BD_BROWSER_URL is not set. Create a browser-session zone in your "
            "provider (Browserless / Bright Data Scraping Browser / Browserbase) "
            "and copy the WebSocket URL into Render's env vars."
        )

    screenshots_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    logs: List[dict] = []
    def log(msg: str, **fields):
        logs.append({
            "t": round(time.time() - started, 2),
            "msg": msg,
            **fields,
        })

    log("Opening Browserless connection")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        log("Connected to remote Chromium",
            contexts=len(browser.contexts), version=browser.version)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            log("Got browser context", existing=bool(browser.contexts))

            # Preset Google consent cookies
            try:
                context.add_cookies([
                    {"name": "CONSENT", "value": "YES+cb",
                     "domain": ".youtube.com", "path": "/"},
                    {"name": "CONSENT", "value": "YES+cb",
                     "domain": ".google.com", "path": "/"},
                    {"name": "SOCS", "value": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg",
                     "domain": ".youtube.com", "path": "/"},
                    {"name": "SOCS", "value": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg",
                     "domain": ".google.com", "path": "/"},
                ])
                log("Consent cookies preset")
            except Exception as e:
                log(f"Consent cookie preset FAILED: {e}")

            # Preload YouTube cookies from secret file
            youtube_cookies = _load_youtube_cookies()
            if youtube_cookies:
                try:
                    context.add_cookies(youtube_cookies)
                    log(f"Loaded {len(youtube_cookies)} YouTube cookies from secret file")
                except Exception as e:
                    log(f"YouTube cookie load FAILED: {e}")
            else:
                log("No YouTube cookies file found at /etc/secrets/cookies.txt")

            page = context.pages[0] if context.pages else context.new_page()
            log("Got page", existing=bool(context.pages))

            log(f"Navigating to https://www.youtube.com/watch?v={video_id}")
            page.goto(f"https://www.youtube.com/watch?v={video_id}",
                      wait_until="domcontentloaded", timeout=60_000)
            log(f"Navigation done, page.url={page.url!r}")

            landing_info = {"url": page.url, "title": "", "screenshot": None}
            if debug_landing:
                try:
                    landing_path = screenshots_dir / "_landing.jpg"
                    # Viewport-only (not full_page) — much faster on a long
                    # YouTube watch page (was costing 5s on free tier).
                    page.screenshot(path=str(landing_path), type="jpeg",
                                    quality=70, full_page=False)
                    landing_info["title"] = page.title()
                    landing_info["screenshot"] = landing_path
                    log(f"Landing screenshot saved (viewport), title={landing_info['title']!r}")
                except Exception as e:
                    log(f"Landing screenshot FAILED: {e}")

            # Skip the click-each-consent-selector loop entirely. With our
            # preset CONSENT + SOCS cookies + the Render Secret File cookies,
            # the consent dialog should already be dismissed. Saves ~10s
            # of wasted 2-second timeouts per missing selector.

            # Wait for player JSON
            try:
                page.wait_for_function(
                    "() => window.ytInitialPlayerResponse && document.querySelector('video')",
                    timeout=15_000,
                )
                log("ytInitialPlayerResponse + video element are present")
            except Exception as wait_err:
                log(f"PLAYER NEVER LOADED: {wait_err}")
                raise RuntimeError(
                    f"Watch page didn't render. URL: {page.url}, "
                    f"title: {page.title()!r}"
                ) from wait_err

            # Get metadata
            details = page.evaluate(
                """() => {
                    const d = window.ytInitialPlayerResponse?.videoDetails || {};
                    const thumbs = (d.thumbnail?.thumbnails || []).slice(-1)[0]?.url;
                    return {
                      title: d.title || '',
                      duration: parseFloat(d.lengthSeconds || '0'),
                      uploader: d.author || '',
                      thumbnail: thumbs || '',
                    };
                }"""
            )
            log(f"Metadata: title={details.get('title')!r}, "
                f"duration={details.get('duration')}, uploader={details.get('uploader')!r}")

            # Get captions URL
            caption_url = page.evaluate(
                """(prefs) => {
                    const tracks = window.ytInitialPlayerResponse
                        ?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
                    if (!tracks.length) return null;
                    if (prefs && prefs.length) {
                        for (const lang of prefs) {
                            const t = tracks.find(t => t.languageCode?.toLowerCase().startsWith(lang.toLowerCase()));
                            if (t) return t.baseUrl;
                        }
                    }
                    const real = tracks.find(t => t.kind !== 'asr');
                    return (real || tracks[0]).baseUrl;
                }""",
                languages or ["en"],
            )
            log(f"Caption URL found: {bool(caption_url)}")

            snippets: List[dict] = []
            if caption_url:
                try:
                    resp = context.request.get(caption_url, timeout=30_000)
                    if resp.ok:
                        snippets = _parse_xml_captions(resp.text())
                        log(f"Parsed {len(snippets)} caption snippets")
                    else:
                        log(f"Caption fetch returned status {resp.status}")
                except Exception as e:
                    log(f"Caption fetch FAILED: {e}")

            # Wait for video to be ready
            try:
                page.wait_for_function(
                    "() => { const v = document.querySelector('video'); return v && v.readyState >= 2; }",
                    timeout=10_000,
                )
                log("Video readyState >= 2 (HAVE_CURRENT_DATA)")
            except Exception:
                log("Video readyState wait TIMED OUT")

            page.evaluate(_hide_overlay_js())

            state_before_play = page.evaluate(_VIDEO_STATE_JS)
            log("State before play()", **(state_before_play or {}))

            # Mute + play
            try:
                page.evaluate(
                    """() => {
                        const v = document.querySelector('video');
                        if (v) { v.muted = true; v.volume = 0; }
                        const player = document.getElementById('movie_player');
                        if (player) {
                            try { player.mute && player.mute(); } catch (e) {}
                            try { player.setVolume && player.setVolume(0); } catch (e) {}
                            try { player.playVideo && player.playVideo(); } catch (e) {}
                        }
                        try { v && v.play().catch(() => {}); } catch (e) {}
                    }"""
                )
                page.wait_for_function(
                    """() => {
                        const v = document.querySelector('video');
                        return v && !v.paused && v.currentTime > 0.5 && v.readyState >= 3;
                    }""",
                    timeout=8_000,
                )
                log("Video is actually playing (currentTime > 0.5)")
            except Exception as e:
                log(f"Play wait TIMED OUT: {e}")

            state_after_play = page.evaluate(_VIDEO_STATE_JS)
            log("State after play()", **(state_after_play or {}))

            duration = details.get("duration") or 0.0
            frames: List[dict] = []
            session_killed = False
            for i, t in enumerate(planned_timestamps):
                if session_killed:
                    log(f"Skipping frame {i+1}/{len(planned_timestamps)} — session already killed")
                    continue
                t = max(0.5, min(t, max(duration - 1, 1.0)))
                log(f"--- Frame {i+1}/{len(planned_timestamps)} target t={t} ---")

                # Recover from error state
                try:
                    in_error = page.evaluate(
                        """() => !!document.querySelector('.ytp-error, ytd-player-error-message-renderer')"""
                    )
                    if in_error:
                        log("Player IS in error state — re-navigating to recover")
                        page.goto(f"https://www.youtube.com/watch?v={video_id}",
                                  wait_until="domcontentloaded", timeout=15_000)
                        page.evaluate(_hide_overlay_js())
                        page.evaluate(
                            """() => {
                                const v = document.querySelector('video');
                                if (v) { v.muted = true; v.volume = 0; v.play().catch(()=>{}); }
                                const p = document.getElementById('movie_player');
                                if (p && p.playVideo) p.playVideo();
                            }"""
                        )
                        page.wait_for_function(
                            "() => { const v = document.querySelector('video'); return v && v.readyState >= 3; }",
                            timeout=10_000,
                        )
                        log("Recovered from error state")
                except Exception as e:
                    log(f"Error-state check FAILED: {e}")

                # Seek without pausing — let video play through the seek
                page.evaluate(
                    f"""() => {{
                        const player = document.getElementById('movie_player');
                        if (player && player.seekTo) {{
                            player.seekTo({t}, true);
                            try {{ player.playVideo && player.playVideo(); }} catch (e) {{}}
                        }} else {{
                            const v = document.querySelector('video');
                            if (v) {{
                                v.currentTime = {t};
                                v.play().catch(() => {{}});
                            }}
                        }}
                    }}"""
                )
                log(f"seekTo({t}) called")
                # Wait for the seek to FULLY complete:
                #   1. seeking === false (player has finished the seek operation)
                #   2. readyState >= 3 (HAVE_FUTURE_DATA — frame data is decoded)
                #   3. buffered range CONTAINS our target time (data fetched)
                # YouTube's MSE streaming can take 10-20s to fetch a segment far
                # ahead of the previous position, so we use a 20s timeout.
                try:
                    page.wait_for_function(
                        f"""() => {{
                            const v = document.querySelector('video');
                            if (!v) return false;
                            if (v.seeking) return false;
                            if (v.readyState < 3) return false;
                            if (Math.abs(v.currentTime - {t}) > 1.5) return false;
                            for (let i = 0; i < v.buffered.length; i++) {{
                                const s = v.buffered.start(i), e = v.buffered.end(i);
                                if (s <= v.currentTime + 0.1 && e >= v.currentTime + 0.5) {{
                                    return true;
                                }}
                            }}
                            return false;
                        }}""",
                        timeout=6_000,
                    )
                    log(f"Seek COMPLETE: not-seeking + readyState>=3 + buffer covers {t}")
                except Exception as e:
                    # Catch ALL exceptions including session-closed — log
                    # whatever state we can get, and skip the frame instead
                    # of crashing the whole capture.
                    try:
                        state = page.evaluate(_VIDEO_STATE_JS) or {}
                    except Exception:
                        state = {"err": "page closed"}
                    log(f"Seek wait TIMED OUT after 6s — state: "
                        f"seeking={state.get('seeking')} readyState={state.get('readyState')} "
                        f"currentTime={state.get('currentTime')} bufferedRanges={state.get('bufferedRanges')} "
                        f"firstBuffered={state.get('firstBuffered')}")
                    # If page is closed, abort the loop cleanly.
                    if state.get("err") == "page closed":
                        log("Page closed — aborting capture loop")
                        break

                # Brief settle for the GPU to paint the newly-decoded frame.
                page.wait_for_timeout(250)
                page.evaluate(_hide_overlay_js())

                state_at_capture = page.evaluate(_VIDEO_STATE_JS)
                log("State at capture moment", **(state_at_capture or {}))

                actual_t = state_at_capture.get("currentTime") if state_at_capture else None
                path = screenshots_dir / f"t{int(round(t))}.jpg"

                # Try canvas.drawImage first
                method = "?"
                try:
                    data_url = page.evaluate(
                        """() => {
                            const v = document.querySelector('video');
                            if (!v || v.videoWidth === 0 || v.videoHeight === 0) return null;
                            try {
                                const canvas = document.createElement('canvas');
                                canvas.width = v.videoWidth;
                                canvas.height = v.videoHeight;
                                const ctx = canvas.getContext('2d');
                                ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
                                return canvas.toDataURL('image/jpeg', 0.85);
                            } catch (e) {
                                return 'ERROR:' + e.name + ':' + e.message;
                            }
                        }"""
                    )
                    if isinstance(data_url, str) and data_url.startswith("data:image/"):
                        img_bytes = base64.b64decode(data_url.split(",", 1)[1])
                        path.write_bytes(img_bytes)
                        method = "canvas"
                        log(f"Canvas capture OK ({len(img_bytes)} bytes)")
                    else:
                        log(f"Canvas capture FAILED, raw return: {str(data_url)[:120]}")
                        # Fallback: page.screenshot
                        bbox = page.locator("video").first.bounding_box(timeout=5_000)
                        log(f"BBox: {bbox}")
                        if bbox and bbox.get("width", 0) > 0:
                            page.screenshot(
                                path=str(path),
                                clip={"x": bbox["x"], "y": bbox["y"],
                                      "width": bbox["width"], "height": bbox["height"]},
                                type="jpeg", quality=80,
                            )
                            method = "page-screenshot-clip"
                        else:
                            page.screenshot(path=str(path), type="jpeg", quality=80)
                            method = "page-screenshot-full"
                        log(f"Fallback screenshot via {method}")
                except Exception as e:
                    log(f"Frame capture EXCEPTION: {type(e).__name__}: {e}")
                    continue

                # Compute hash + size for diagnostic
                try:
                    data = path.read_bytes()
                    sha = hashlib.sha256(data).hexdigest()[:16]
                    size = len(data)
                    log(f"Saved {path.name}: sha={sha} size={size}B method={method}")
                except Exception as e:
                    sha = "?"
                    size = 0
                    log(f"Hashing FAILED: {e}")

                frames.append({
                    "timestamp": float(t),
                    "actual_t": float(actual_t) if actual_t is not None else None,
                    "path": path,
                    "sha": sha,
                    "size": size,
                    "method": method,
                })

            log(f"DONE. Captured {len(frames)} frames total.")
            return {
                "meta": details,
                "snippets": snippets,
                "frames": frames,
                "landing": landing_info,
                "logs": logs,
            }
        finally:
            try:
                browser.close()
            except Exception:
                pass
