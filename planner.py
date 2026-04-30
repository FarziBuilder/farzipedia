"""Plan capture timestamps for a video from its transcript."""

import re
from typing import List

# Phrases that suggest the speaker is pointing at something on screen.
CUE_PATTERNS = [
    r"\bas you can see\b", r"\blook at (this|that|the)\b", r"\bhere we have\b",
    r"\bon (the )?screen\b", r"\bin this (diagram|figure|chart|graph|plot|image|picture)\b",
    r"\blet me show you\b", r"\bif you look at\b", r"\bnotice (the|how|that)\b",
    r"\bsee (the|this|that|how)\b", r"\bthis is (a|an|the)\b", r"\bwatch (what|this|the)\b",
    r"\b(let'?s|i'?ll) (write|draw|sketch|plot|build)\b", r"\bon the (left|right|top|bottom)\b",
    r"\bthe (red|blue|green|yellow|orange|purple) (line|arrow|box|circle|dot|curve)\b",
    r"\b(this|that) (function|equation|formula|line|column|row|graph|diagram|chart)\b",
    r"\b(line|equation) \d+\b", r"\blet (x|y|n|t|f) (equal|be)\b",
    r"\b(let'?s )?(run|click|press|type) (it|this)\b", r"\bwatch the (output|result|screen)\b",
    r"\b(diagram|schematic|figure)\b", r"\b(tier list|tier|s tier|a tier|b tier|c tier|d tier|f tier)\b",
]
CUE_RE = re.compile("|".join(CUE_PATTERNS), re.IGNORECASE)


def plan_timestamps(snippets: List[dict], video_duration: float,
                    baseline_interval: float = 30.0,
                    cue_offset: float = 1.5,
                    max_total: int = 50) -> List[float]:
    """Pick capture timestamps from a transcript.

    Strategy:
      - Mandatory captures at start (~3s) and after long silences
      - Cue-driven captures ~1.5s after the cue word
      - Periodic baseline every `baseline_interval` seconds
      - De-duplicate within 6 seconds, cap at `max_total`
    """
    candidates: List[float] = []

    # Mandatory: a few seconds in, to grab title/setting.
    candidates.append(min(5.0, video_duration / 4))

    # Cue-driven captures.
    for s in snippets:
        if CUE_RE.search(s["text"]):
            t = s["start"] + cue_offset
            if t < video_duration - 2:
                candidates.append(t)

    # Periodic baseline.
    t = baseline_interval
    while t < video_duration - 5:
        candidates.append(t)
        t += baseline_interval

    # Long-silence captures (gap > 8s between snippets).
    for i in range(1, len(snippets)):
        gap = snippets[i]["start"] - (snippets[i-1]["start"] + snippets[i-1]["duration"])
        if gap > 8:
            candidates.append(snippets[i-1]["start"] + snippets[i-1]["duration"] + 1)

    # End frame for any conclusion/recap content.
    if video_duration > 30:
        candidates.append(max(0, video_duration - 8))

    # Dedupe (keep earlier), sort.
    candidates.sort()
    deduped: List[float] = []
    for t in candidates:
        if not deduped or t - deduped[-1] > 6.0:
            deduped.append(round(t, 1))

    # Cap by spacing if too many.
    if len(deduped) > max_total:
        step = len(deduped) / max_total
        deduped = [deduped[int(i * step)] for i in range(max_total)]

    return deduped
