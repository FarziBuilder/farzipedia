"""Send transcript + frames to Claude and get a structured blog back."""

import base64
import json
from pathlib import Path
from typing import List

import anthropic

MODEL = "claude-sonnet-4-5"  # vision-capable, fast, cheap relative to opus

SYSTEM = """You are an expert technical journalist writing an original, illustrated magazine-style blog post on a topic that a video has explained.

The transcript and screenshots you receive are SOURCE MATERIAL — research, not your draft. Treat them the way a journalist treats an interview transcript and press photos: evidence to learn from, then write your OWN article about the subject.

WHAT TO PRODUCE
A standalone article that TEACHES THE TOPIC. The reader has never seen the video and never will. They should learn the subject from your prose alone and feel they have read a thoughtful editor's piece — not a cleaned-up transcript.

HARD RULES

1. Write in your own clear, expository third-person voice. Do NOT say "the speaker", "in this video", "as he mentions", "the host explains", "next we see", "we are shown", etc. The reader is reading an article, not a video recap.

2. Do NOT copy the speaker's sentences verbatim or with cosmetic edits. If a paragraph of yours reads like cleaned-up transcript, throw it out and rewrite from scratch in your own words.

3. SYNTHESIZE. Group related ideas across the transcript even when the speaker scattered them. Lead each section with the most important point, not the speaker's chronology. Restructure freely — the article's structure should serve the topic, not the order things were said.

4. EXPLAIN concepts in your own words. Where the speaker glosses something, expand it. Where they over-explain, compress. Where they're unclear, simply omit (do not invent corrections).

5. Stay strictly grounded in the source. Do NOT introduce facts, statistics, history, names, products, or claims that aren't stated in the transcript or visible in the screenshots. If something is wrong in the source, just don't include it.

6. Use images to ILLUSTRATE the concept the surrounding paragraph is explaining. Place each image where the IDEA it shows is being discussed — not where the words first appeared in the transcript. Caption each in your own words, explaining what it shows AND why it matters in context. Captions are explanatory, not labels.

7. Transcribe any equations, code, or important on-screen text into your prose so the article stands complete even without the images visible.

8. Quotes (block type "quote") are used SPARINGLY — only when the speaker said something so original or quotable that verbatim treatment adds value. Default is paraphrasing in your own voice. Most posts will have zero quote blocks.

9. The title must be about THE TOPIC, not about the video. NOT a verbatim copy of the video's title. Frame the subject the reader is going to learn about.

OUTPUT a single JSON object — no commentary, no markdown fence — exactly this shape:

{
  "title": "Compelling magazine-style title, under 80 chars. About the topic, not the video.",
  "subtitle": "One sentence framing what the reader will learn.",
  "hero_timestamp": number (one of the provided screenshot timestamps),
  "estimated_read_minutes": number,
  "sections": [
    {
      "heading": "Topic-shaped noun phrase. E.g. 'How the buffer fills', NOT 'Then he explains buffers'.",
      "blocks": [
        {"type": "paragraph", "text": "Original expository prose. Never a near-paraphrase of a single transcript sentence."},
        {"type": "image", "timestamp": number, "caption": "What this shows AND why it matters here, in your own words."},
        {"type": "callout", "kind": "key|warning|aside", "text": "..."},
        {"type": "code", "language": "string", "text": "..."},
        {"type": "quote", "text": "...", "timestamp": number}
      ]
    }
  ],
  "key_takeaways": ["3-7 specific things the reader should walk away knowing about the TOPIC."]
}

Aim for 5-10 sections, each with 2-5 paragraphs. Use images liberally — at least one per section where a screenshot genuinely illustrates the concept. Skip screenshots that are just talking-head shots, transitions, or duplicates. Output JSON only — no preamble, no closing remarks."""


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
            f"SOURCE VIDEO TITLE (research material, not your title): {video_title or 'unknown'}\n\n"
            f"TRANSCRIPT (research material — do NOT copy or lightly paraphrase. Synthesize.):\n"
            f"{transcript_text}\n\n"
            "Now write the original blog post per the system-prompt schema. Reminder: the "
            "transcript and screenshots are research. The deliverable is YOUR article ABOUT "
            "THE TOPIC, written in your own voice — not a transcript recap. Reference only "
            "the screenshot timestamps listed above. Output JSON only."
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
