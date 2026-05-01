"""Drive a remote Chromium (Bright Data Scraping Browser) to interact
with YouTube as a real browser — sidesteps every yt-dlp anti-bot wall.

We open the watch page, scrape title/duration/transcript from the
in-page `ytInitialPlayerResponse` blob, then seek-and-screenshot the
<video> element at each requested timestamp.

Public API:
    capture(video_id, planned_timestamps, screenshots_dir) -> dict
        returns {meta: {...}, snippets: [...], frames: [...]}

Env var:
    BD_BROWSER_URL   wss://USER:PASS@brd.superproxy.io:9222
"""

from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path
from typing import List, Optional


def _load_youtube_cookies() -> List[dict]:
    """Load YouTube cookies from /etc/secrets/cookies.txt (Netscape format).

    Returns Playwright-compatible cookie dicts. If no file is present,
    returns []. Lets the browser session pretend to be a logged-in user,
    which usually bypasses YouTube's "Sign in to confirm you're not a bot"
    challenge.
    """
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
                # Netscape format: domain, flag, path, secure, expiry, name, value
                if len(parts) < 7:
                    continue
                domain, _flag, cpath, secure, expiry, name, value = parts[:7]
                try:
                    expires = int(expiry)
                except ValueError:
                    expires = -1
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": cpath,
                    "secure": secure.upper() == "TRUE",
                    "httpOnly": False,
                    **({"expires": expires} if expires > 0 else {}),
                    "sameSite": "Lax",
                })
    except OSError:
        return []
    return cookies


def _parse_xml_captions(xml: str) -> List[dict]:
    """Parse YouTube's timed-text XML into [{start, duration, text}]."""
    out: List[dict] = []
    for m in re.finditer(
        r'<text\s+start="([\d.]+)"(?:\s+dur="([\d.]+)")?[^>]*>(.*?)</text>',
        xml, flags=re.DOTALL,
    ):
        start = float(m.group(1))
        dur = float(m.group(2)) if m.group(2) else 1.5
        body = m.group(3)
        # Strip nested HTML tags, decode entities, collapse whitespace
        body = re.sub(r"<[^>]+>", "", body)
        body = html.unescape(body)
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            out.append({"start": start, "duration": max(0.5, dur), "text": body})
    return out


def _hide_overlay_js() -> str:
    """Hide the YouTube player chrome + dismiss every consent dialog
    YouTube/Google might throw at us, then force the <video> visible
    and on-screen for screenshots."""
    return """
    // Click any consent button by aria-label or text content.
    // Covers both old YouTube consent banner and new Google unified one.
    const clickFirst = (selectors) => {
      for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn) { try { btn.click(); return true; } catch (e) {} }
      }
      return false;
    };
    clickFirst([
      // YouTube-specific
      'tp-yt-paper-button[aria-label*="Accept"]',
      'tp-yt-paper-button[aria-label*="Reject"]',
      'button[aria-label*="Accept all"]',
      'button[aria-label*="Reject all"]',
      // Google unified consent ("Before you continue to YouTube")
      'button[aria-label*="Reject the use"]',
      'button[aria-label*="Accept the use"]',
      'form[action*="consent"] button[type="submit"]',
    ]);
    // Brute-force fallback: any visible button whose text matches
    for (const b of document.querySelectorAll('button, [role="button"]')) {
      const t = (b.innerText || b.textContent || '').trim();
      if (/^(Reject all|Accept all|I agree)$/i.test(t)) {
        try { b.click(); break; } catch (e) {}
      }
    }
    // Hide player chrome
    const hideSel = [
      '.ytp-chrome-bottom', '.ytp-chrome-top', '.ytp-gradient-bottom',
      '.ytp-gradient-top', '.ytp-cc-window-container', '.ytp-pause-overlay',
      '.ytp-watermark', '.ytp-popup', '.ytp-spinner', '.ytp-watch-later-icon',
      'ytd-popup-container', 'tp-yt-paper-dialog',
      // Google consent dialog containers
      'ytd-consent-bump-v2-lightbox', 'ytd-consent-bump-lightbox',
    ];
    for (const s of hideSel) {
      document.querySelectorAll(s).forEach(e => e.style.display = 'none');
    }
    // Force the video into view
    const v = document.querySelector('video');
    if (v) {
      v.style.visibility = 'visible';
      v.style.opacity = '1';
      v.scrollIntoView({block: 'center', inline: 'center'});
    }
    """


def capture(video_id: str,
            planned_timestamps: List[float],
            screenshots_dir: Path,
            languages: Optional[List[str]] = None,
            debug_landing: bool = False) -> dict:
    """Open the YouTube watch page in a remote Chromium and pull out:
       - metadata (title, duration, uploader, thumbnail)
       - timestamped transcript (from ytInitialPlayerResponse caption tracks)
       - frames at each requested timestamp (saved into screenshots_dir)

    Returns:
        {
          "meta": {"title": str, "duration": float, "uploader": str, "thumbnail": str},
          "snippets": [{"start": float, "duration": float, "text": str}, ...],
          "frames":   [{"timestamp": float, "path": Path}, ...],
        }
    """
    from playwright.sync_api import sync_playwright

    cdp_url = os.environ.get("BD_BROWSER_URL", "").strip()
    if not cdp_url:
        raise RuntimeError(
            "BD_BROWSER_URL is not set. Create a Scraping Browser zone in "
            "Bright Data and copy the WebSocket URL into Render's env vars."
        )

    screenshots_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        try:
            # Most Scraping Browser sessions provide a default context already.
            context = browser.contexts[0] if browser.contexts else browser.new_context()

            # Preset Google's consent cookies for both .youtube.com and
            # .google.com so the "Before you continue to YouTube" unified
            # consent dialog never appears.
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
            except Exception:
                pass

            # Preload YouTube cookies if a cookies.txt was mounted as a
            # Render Secret File. This is what bypasses the "Sign in to
            # confirm you're not a bot" challenge — YouTube treats the
            # session as logged-in.
            youtube_cookies = _load_youtube_cookies()
            if youtube_cookies:
                try:
                    context.add_cookies(youtube_cookies)
                except Exception:
                    # If the cookie format is bad, continue without —
                    # we'll get the bot challenge but at least the session opens.
                    pass

            page = context.pages[0] if context.pages else context.new_page()

            page.goto(f"https://www.youtube.com/watch?v={video_id}",
                      wait_until="domcontentloaded", timeout=60_000)

            # If asked, snap a full-page screenshot RIGHT AFTER navigation
            # so we can see exactly what page Browserless landed on (consent
            # wall, sign-in page, real watch page, etc.). Captured before
            # any wait_for_function timeout so it's reliable.
            landing_info = {
                "url": page.url,
                "title": "",
                "screenshot": None,
            }
            if debug_landing:
                try:
                    landing_path = screenshots_dir / "_landing.jpg"
                    page.screenshot(
                        path=str(landing_path), type="jpeg", quality=70,
                        full_page=True,
                    )
                    landing_info["title"] = page.title()
                    landing_info["screenshot"] = landing_path
                except Exception:
                    pass

            # Try clicking a consent button right after navigation —
            # before yt's wait, in case the consent dialog is blocking
            # ytInitialPlayerResponse from loading.
            for sel in (
                'button[aria-label*="Reject the use"]',
                'button[aria-label*="Accept the use"]',
                'tp-yt-paper-button[aria-label*="Reject"]',
                'tp-yt-paper-button[aria-label*="Accept"]',
                'form[action*="consent"] button[type="submit"]',
            ):
                try:
                    page.click(sel, timeout=2_000)
                    page.wait_for_timeout(500)
                    break
                except Exception:
                    continue

            # Wait for the player JSON to be available + the video element to mount.
            try:
                page.wait_for_function(
                    "() => window.ytInitialPlayerResponse && document.querySelector('video')",
                    timeout=30_000,
                )
            except Exception as wait_err:
                # If the watch page never rendered, surface the landing info
                # in the error so the caller can show what page we got instead.
                raise RuntimeError(
                    f"Watch page didn't render. URL we ended up on: {page.url}. "
                    f"Page title: {page.title()!r}. "
                    f"Underlying: {type(wait_err).__name__}: {str(wait_err)[:200]}"
                ) from wait_err

            # ----- Metadata -----
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

            # ----- Captions URL -----
            caption_url = page.evaluate(
                """(prefs) => {
                    const tracks = window.ytInitialPlayerResponse
                        ?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
                    if (!tracks.length) return null;
                    // prefer English / preferred languages, then any non-auto-generated
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

            snippets: List[dict] = []
            if caption_url:
                # Fetch through the SAME browser context — same cookies/session,
                # so YouTube serves the timed text without anti-bot drama.
                resp = context.request.get(caption_url, timeout=30_000)
                if resp.ok:
                    snippets = _parse_xml_captions(resp.text())

            # ----- Frame captures (in-session seeking on watch page) -----
            # Reverted from per-frame navigation / embed approach.
            # In-session seeking is faster but can trigger YouTube's anti-bot
            # after multiple rapid seeks ("Video unavailable" state). We
            # mitigate with delays + recovery checks.
            try:
                page.wait_for_function(
                    "() => { const v = document.querySelector('video'); return v && v.readyState >= 2; }",
                    timeout=10_000,
                )
            except Exception:
                pass

            page.evaluate(_hide_overlay_js())

            # Mute then play (autoplay block in headless Chromium requires
            # muted), wait briefly for currentTime to advance.
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
            except Exception:
                pass

            duration = details.get("duration") or 0.0
            frames: List[dict] = []
            for t in planned_timestamps:
                t = max(0.5, min(t, max(duration - 1, 1.0)))

                # Recover if a previous seek put the player in an error
                # state ("Video unavailable"). The error overlay has class
                # "ytp-error" — when present, we re-navigate to the watch
                # page to reset the player.
                try:
                    in_error = page.evaluate(
                        """() => !!document.querySelector('.ytp-error, ytd-player-error-message-renderer')"""
                    )
                    if in_error:
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
                except Exception:
                    pass

                # Seek but DO NOT pause — the headless GPU sometimes reverts
                # to a stale decoded frame when a video is paused after seek.
                # Letting the video play for ~1.2s past the seek forces the
                # decoder to actually render fresh frames, which we then
                # screenshot mid-playback.
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
                # Wait for currentTime to land near the target.
                try:
                    page.wait_for_function(
                        f"""() => {{
                            const v = document.querySelector('video');
                            return v && Math.abs(v.currentTime - {t}) < 1.5 && v.readyState >= 3;
                        }}""",
                        timeout=5_000,
                    )
                except Exception:
                    pass
                # Let the video PLAY for 1.2s past the seek so actual new
                # frames get decoded and painted by the GPU.
                page.wait_for_timeout(1_200)
                page.evaluate(_hide_overlay_js())

                actual_t = page.evaluate("document.querySelector('video')?.currentTime")

                path = screenshots_dir / f"t{int(round(t))}.jpg"
                try:
                    # Try canvas.drawImage first — reads directly from the
                    # video element's media buffer, bypasses the compositor
                    # screenshot path that's been giving us stale frames.
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
                                // Tainted canvas (cross-origin video) or other failure
                                return null;
                            }
                        }"""
                    )
                    if isinstance(data_url, str) and data_url.startswith("data:image/"):
                        import base64
                        img_bytes = base64.b64decode(data_url.split(",", 1)[1])
                        path.write_bytes(img_bytes)
                    else:
                        # Fallback: page.screenshot via bbox clip
                        bbox = page.locator("video").first.bounding_box(timeout=5_000)
                        if bbox and bbox.get("width", 0) > 0 and bbox.get("height", 0) > 0:
                            page.screenshot(
                                path=str(path),
                                clip={
                                    "x": bbox["x"],
                                    "y": bbox["y"],
                                    "width": bbox["width"],
                                    "height": bbox["height"],
                                },
                                type="jpeg",
                                quality=80,
                            )
                        else:
                            page.screenshot(path=str(path), type="jpeg", quality=80)
                    frames.append({
                        "timestamp": float(t),
                        "actual_t": float(actual_t) if actual_t is not None else None,
                        "path": path,
                    })
                except Exception:
                    continue

            return {
                "meta": details,
                "snippets": snippets,
                "frames": frames,
                "landing": landing_info,
            }
        finally:
            try:
                browser.close()
            except Exception:
                pass
