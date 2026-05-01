"""Send transcript + frames to Claude and get a structured blog back."""

import base64
import json
from pathlib import Path
from typing import List

import anthropic

MODEL = "claude-sonnet-4-5"  # vision-capable, fast, cheap relative to opus

SYSTEM = """You are a careful, visually-literate technical writer.

You receive a YouTube video as: (1) a timestamped transcript and (2) screenshots
sampled at specific timestamps. Your job is to write a detailed, beautifully
structured blog post about what was actually taught in the video.

Hard constraints:
- Only use information present in the transcript or visible in the provided
  screenshots. Do NOT add outside facts or context.
- Every image you reference must be one of the provided screenshot timestamps.
  Pick the timestamp that best matches what the surrounding text describes.
- If two adjacent screenshots show the same thing, pick the clearer one and
  skip the duplicate.
- If a screenshot is a black frame, transition, or otherwise unhelpful, do not
  reference it.
- Transcribe any equations, code, or important on-screen text into the body so
  the post stands alone without the images.
- Lightly clean transcript captions for readability (punctuation, casing,
  remove "um/uh"). Preserve the speaker's meaning. Do not paraphrase to the
  point of changing claims.

CRITICAL — IGNORE ADS AND SPONSOR SEGMENTS:

Many YouTube videos contain integrated advertisements or sponsor segments —
the creator pivots from the main topic to promote a product/service that has
nothing to do with the video's subject. You MUST detect these and exclude
them entirely from the blog post.

Signals that a transcript region is a sponsor/ad segment:
  - "This video is sponsored by ...", "Today's video is brought to you by ..."
  - "Big thanks to <brand> for sponsoring", "I'd like to thank <brand>"
  - "Use code <X> to get <Y>% off", "First N viewers get free shipping"
  - Sudden pivot to a completely different topic (VPN, mobile game, finance app,
    razor service, mattress, language-learning app, etc.) inserted in a
    technical or storytelling video
  - "Anyway, back to the video", "now back to ..." — typical re-entry phrase
    after an ad segment
  - "Click the link in the description below" combined with brand promotion
  - Discount codes, affiliate-style call-to-action language

Signals that a screenshot is from an ad:
  - Visible product/brand logos that are unrelated to the video subject
  - Price graphics, "limited-time offer", QR codes, CTA buttons
  - A drastic visual style change (e.g. cinematic shot of a phone interface
    in the middle of an aviation video)
  - The host directly addressing the camera with a brand-logo overlay

How to handle them:
  - DROP the entire transcript range corresponding to the ad/sponsor — do
    not summarise, quote, or reference it in any blog section.
  - DO NOT use any screenshot whose timestamp falls inside the ad range.
  - If the ad is at the start or end of the video, simply begin/end the
    blog at the actual content.
  - If the ad is in the middle, the blog should flow continuously across
    the gap — do NOT mention "the creator paused for a sponsor".
  - Pre-roll/post-roll outros that just say "thanks for watching, subscribe"
    are NOT ads — keep them if they contain a meaningful conclusion, drop
    them if they're pure subscribe-pleas.

When in doubt: if the content is on-topic for the video's subject, keep it.
If it's promotional content for an unrelated product/service, drop it.

Output a single JSON object — no commentary, no markdown fence — with this shape:

{
  "title": "string (compelling, under 80 chars)",
  "subtitle": "string (one sentence, sets the stage)",
  "hero_timestamp": number (seconds; one of the provided screenshots),
  "estimated_read_minutes": number,
  "sections": [
    {
      "heading": "string (H2 section title)",
      "blocks": [
        {"type": "paragraph", "text": "..."},
        {"type": "image", "timestamp": number, "caption": "string (1 short line, original wording)"},
        {"type": "callout", "kind": "key|warning|aside", "text": "..."},
        {"type": "code", "language": "string", "text": "..."},
        {"type": "quote", "text": "...", "timestamp": number}
      ]
    }
  ],
  "key_takeaways": ["bullet 1", "bullet 2", ...]
}

Aim for 5-12 sections. Use images liberally — at least one image per section
when relevant material was on screen. Captions describe what's actually shown
(original phrasing, not a transcript quote)."""


def _encode_image(path: Path) -> dict:
    data = path.read_bytes()
    b64 = base64.standard_b64encode(data).decode()
    media_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": b64},
    }


def analyze(transcript_snippets: List[dict],
            frames: List[dict],
            video_title: str = "") -> dict:
    """frames: [{timestamp: float, path: Path}, ...]"""
    client = anthropic.Anthropic()

    # Build content: alternating image + label, then transcript, then instruction.
    content = []
    for f in frames:
        content.append(_encode_image(f["path"]))
        ts = f["timestamp"]
        m, s = divmod(int(ts), 60)
        content.append({
            "type": "text",
            "text": f"^^ screenshot at {ts:.1f}s ({m}:{s:02d})",
        })

    transcript_text = "\n".join(
        f"[{s['start']:.2f}s] {s['text']}" for s in transcript_snippets
    )
    content.append({
        "type": "text",
        "text": (
            f"VIDEO TITLE (best guess): {video_title or 'unknown'}\n\n"
            f"TIMESTAMPED TRANSCRIPT:\n{transcript_text}\n\n"
            "Now write the blog post as a single JSON object per the schema in "
            "the system prompt. Reference only the screenshot timestamps shown "
            "above. Output JSON only."
        ),
    })

    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
    )

    raw = resp.content[0].text.strip()
    # Strip optional code fence.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        if raw.startswith("json"):
            raw = raw[4:].lstrip("\n")
    return json.loads(raw)
