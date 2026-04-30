"""Generate fun, topic-relevant trivia for the loading screen."""

import json
import re
from typing import List

import anthropic

MODEL = "claude-haiku-4-5"  # cheap and fast — trivia is throwaway

SYSTEM = """You write playful, factually-grounded trivia for a loading screen.

You'll get a YouTube video's title, channel, and a snippet of its description.
Generate 14 short trivia items related to the topic. Each item:
- Starts with a single emoji that fits the fact.
- Is one or two short sentences (under 200 characters total).
- Is genuinely interesting — surprising numbers, weird history, fun comparisons.
- Stays accurate — no made-up facts. If unsure, skip and pick a different one.
- Is original wording — never quote sources directly.
- Skips anything political, NSFW, or potentially harmful.

Output ONLY a JSON array of strings, each starting with the emoji. No prose.

Example: ["🚀 Wan Hu's chair-rocket stunt in 1390 ended badly enough that NASA named a Moon crater after him as a posthumous apology.", ...]"""


def generate(title: str, channel: str = "", description: str = "") -> List[str]:
    """Returns a list of trivia strings. Empty list on any error."""
    if not title.strip():
        return []
    desc_snip = (description or "").strip()[:1200]
    user = (
        f"Video title: {title}\n"
        f"Channel: {channel or 'unknown'}\n"
        f"Description excerpt: {desc_snip or '(none)'}\n\n"
        "Return the JSON array of 14 trivia strings now."
    )
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        raw = resp.content[0].text.strip()
        # Strip optional code fence.
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n", "", raw)
            raw = re.sub(r"\n```$", "", raw)
        items = json.loads(raw)
        if isinstance(items, list):
            return [str(x) for x in items if isinstance(x, str)][:20]
    except Exception:
        pass
    return []
