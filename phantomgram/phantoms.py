"""Instagram phantom registry and (simulated) implementations.

Each phantom is a callable that takes (inputs, account, progress_cb, log_cb)
and returns a list of result rows. The implementations here run in DEMO MODE:
they produce realistic-looking synthetic data rather than hitting Instagram
directly. This is intentional — real automation against Instagram violates
its ToS and gets accounts banned. The architecture below cleanly supports
swapping in real implementations if you have proper authorization (e.g.
Instagram Graph API for Business/Creator accounts).
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Callable

ProgressCB = Callable[[float, str], None]
LogCB = Callable[[str], None]


@dataclass
class Field:
    name: str
    label: str
    kind: str = "text"  # text, textarea, number, select, checkbox, url
    placeholder: str = ""
    required: bool = False
    default: str | int | bool | None = None
    choices: list[tuple[str, str]] = field(default_factory=list)
    help: str = ""


@dataclass
class Phantom:
    id: str
    name: str
    tagline: str
    category: str  # scraper | action | engagement
    description: str
    icon: str  # emoji for now
    needs_account: bool
    fields: list[Field]
    runner: Callable
    output_columns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "alex", "jordan", "sam", "taylor", "morgan", "casey", "riley", "jamie",
    "quinn", "avery", "skyler", "rowan", "kai", "remy", "blake", "drew",
    "harper", "phoenix", "sage", "noor", "aanya", "ravi", "priya", "diego",
    "luna", "milo", "nova", "zane", "iris", "yuki", "leo", "maya",
]
LAST_TAGS = [
    "_official", ".ig", "_studio", "co", ".art", "_daily", "hq", "x",
    "_world", ".live", "_club", "_lab", "_co", ".social", "",
]
BIO_BITS = [
    "📍 Mumbai", "📍 Berlin", "📍 NYC", "📍 Tokyo", "📍 SF", "📍 London",
    "Coffee · Code · Cameras", "Designer at @studio", "Founder",
    "Photographer 📷", "Building things", "DM for collabs",
    "she/her", "he/him", "they/them", "✌️", "🌱", "🎧",
]
HASHTAG_BANK = [
    "art", "photography", "design", "travel", "food", "fitness", "fashion",
    "music", "tech", "startup", "coffee", "minimal", "architecture", "nature",
    "portrait", "streetphotography", "ux", "branding", "illustration", "ai",
]


def _rng(*seed_parts) -> random.Random:
    h = hashlib.sha256("|".join(str(p) for p in seed_parts).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _username(r: random.Random) -> str:
    return f"{r.choice(FIRST_NAMES)}{r.randint(2, 999)}{r.choice(LAST_TAGS)}"


def _full_name(r: random.Random) -> str:
    return f"{r.choice(FIRST_NAMES).capitalize()} {chr(r.randint(65, 90))}."


def _bio(r: random.Random) -> str:
    return " · ".join(r.sample(BIO_BITS, r.randint(1, 3)))


def _profile_pic(username: str) -> str:
    return f"https://i.pravatar.cc/150?u={username}"


def _post_url(shortcode: str) -> str:
    return f"https://www.instagram.com/p/{shortcode}/"


def _shortcode(r: random.Random) -> str:
    alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(r.choice(alpha) for _ in range(11))


def _humanize_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _simulate_step(progress_cb: ProgressCB, log_cb: LogCB, frac: float, msg: str,
                   delay_min=0.15, delay_max=0.45):
    log_cb(msg)
    progress_cb(frac, msg)
    time.sleep(random.uniform(delay_min, delay_max))


def _make_profile_row(username: str, r: random.Random) -> dict:
    followers = r.randint(120, 850_000)
    following = r.randint(50, 2500)
    posts = r.randint(3, 4200)
    private = r.random() < 0.15
    verified = followers > 50_000 and r.random() < 0.4
    return {
        "username": username,
        "full_name": _full_name(r),
        "bio": _bio(r),
        "profile_url": f"https://www.instagram.com/{username}/",
        "profile_pic": _profile_pic(username),
        "followers": followers,
        "followers_pretty": _humanize_count(followers),
        "following": following,
        "posts": posts,
        "is_private": private,
        "is_verified": verified,
        "external_url": "" if r.random() < 0.6 else f"https://{username}.example.com",
    }


def _make_post_row(r: random.Random, author: str | None = None,
                   hashtag: str | None = None) -> dict:
    sc = _shortcode(r)
    likes = r.randint(5, 80_000)
    comments = r.randint(0, max(1, likes // 30))
    author = author or _username(r)
    caption_words = []
    if hashtag:
        caption_words.append(f"#{hashtag}")
    caption_words += [f"#{r.choice(HASHTAG_BANK)}" for _ in range(r.randint(2, 6))]
    return {
        "post_url": _post_url(sc),
        "shortcode": sc,
        "author": author,
        "caption": "Loving this moment ✨ " + " ".join(caption_words),
        "likes": likes,
        "comments": comments,
        "timestamp": int(time.time() - r.randint(60, 60 * 60 * 24 * 90)),
        "media_type": r.choice(["image", "video", "carousel"]),
        "thumbnail": f"https://picsum.photos/seed/{sc}/400/400",
    }


def _make_comment_row(r: random.Random, post_url: str) -> dict:
    u = _username(r)
    return {
        "post_url": post_url,
        "commenter": u,
        "commenter_url": f"https://www.instagram.com/{u}/",
        "comment": r.choice([
            "Love this 🔥", "So cool!", "Where is this?", "Incredible shot",
            "DMing you", "This is gold", "🙌🙌🙌", "Need this in my life",
            "Saving this", "What camera?", "Vibes", "👏👏👏",
        ]),
        "likes": r.randint(0, 120),
        "timestamp": int(time.time() - r.randint(60, 60 * 60 * 24 * 14)),
    }


# ---------------------------------------------------------------------------
# Phantom implementations
# ---------------------------------------------------------------------------

def _clamp_limit(raw, default=25, hi=500) -> int:
    try:
        v = int(raw or default)
    except (TypeError, ValueError):
        v = default
    return max(1, min(hi, v))


def run_profile_scraper(inputs, account, progress, log) -> list[dict]:
    raw = inputs.get("usernames", "") or ""
    names = [u.strip().lstrip("@") for u in raw.replace(",", "\n").splitlines() if u.strip()]
    if not names:
        raise ValueError("Provide at least one username.")
    log(f"Resolving {len(names)} profile(s)…")
    out = []
    for i, u in enumerate(names, 1):
        _simulate_step(progress, log, i / len(names), f"Fetching @{u}")
        out.append(_make_profile_row(u, _rng("profile", u)))
    log(f"Scraped {len(out)} profile(s).")
    return out


def run_hashtag_scraper(inputs, account, progress, log) -> list[dict]:
    tag = (inputs.get("hashtag", "") or "").lstrip("#").strip()
    if not tag:
        raise ValueError("Provide a hashtag.")
    limit = _clamp_limit(inputs.get("limit"), default=30)
    log(f"Searching #{tag} (target {limit} posts)…")
    out = []
    r = _rng("hashtag", tag)
    for i in range(limit):
        if i % max(1, limit // 10) == 0:
            _simulate_step(progress, log, (i + 1) / limit,
                           f"Page {i // 10 + 1}: fetched {i} posts")
        out.append(_make_post_row(r, hashtag=tag))
    progress(1.0, "Done")
    log(f"Collected {len(out)} posts for #{tag}.")
    return out


def run_post_scraper(inputs, account, progress, log) -> list[dict]:
    urls_raw = inputs.get("post_urls", "") or ""
    urls = [u.strip() for u in urls_raw.replace(",", "\n").splitlines() if u.strip()]
    if not urls:
        raise ValueError("Provide at least one post URL.")
    log(f"Loading {len(urls)} post(s)…")
    out = []
    for i, url in enumerate(urls, 1):
        _simulate_step(progress, log, i / len(urls), f"Reading {url}")
        r = _rng("post", url)
        sc = url.rstrip("/").split("/")[-1] or _shortcode(r)
        out.append({
            "post_url": url,
            "shortcode": sc,
            "author": _username(r),
            "caption": "Sample caption " + " ".join(f"#{r.choice(HASHTAG_BANK)}" for _ in range(3)),
            "likes": r.randint(20, 50_000),
            "comments": r.randint(0, 4000),
            "timestamp": int(time.time() - r.randint(60, 60 * 60 * 24 * 30)),
            "media_type": r.choice(["image", "video", "carousel"]),
        })
    return out


def run_post_likers(inputs, account, progress, log) -> list[dict]:
    url = (inputs.get("post_url") or "").strip()
    if not url:
        raise ValueError("Provide a post URL.")
    limit = _clamp_limit(inputs.get("limit"), default=50, hi=1000)
    log(f"Fetching likers of {url}…")
    out = []
    r = _rng("likers", url)
    for i in range(limit):
        if i % 25 == 0:
            _simulate_step(progress, log, (i + 1) / limit,
                           f"Page {i // 25 + 1}: {i} likers")
        u = _username(r)
        out.append({
            "post_url": url,
            "username": u,
            "profile_url": f"https://www.instagram.com/{u}/",
            "full_name": _full_name(r),
        })
    progress(1.0, "Done")
    log(f"Collected {len(out)} likers.")
    return out


def run_post_commenters(inputs, account, progress, log) -> list[dict]:
    url = (inputs.get("post_url") or "").strip()
    if not url:
        raise ValueError("Provide a post URL.")
    limit = _clamp_limit(inputs.get("limit"), default=50, hi=1000)
    log(f"Extracting comments from {url}…")
    out = []
    r = _rng("comments", url)
    for i in range(limit):
        if i % 20 == 0:
            _simulate_step(progress, log, (i + 1) / limit,
                           f"Page {i // 20 + 1}: {i} comments")
        out.append(_make_comment_row(r, url))
    progress(1.0, "Done")
    log(f"Collected {len(out)} comments.")
    return out


def run_followers_extractor(inputs, account, progress, log) -> list[dict]:
    if not account:
        raise ValueError("This phantom needs an Instagram account.")
    target = (inputs.get("target_username") or "").lstrip("@").strip()
    if not target:
        raise ValueError("Provide a target username.")
    limit = _clamp_limit(inputs.get("limit"), default=100, hi=5000)
    log(f"Listing followers of @{target} (target {limit})…")
    out = []
    r = _rng("followers", target)
    pages = max(1, limit // 50)
    for page in range(pages):
        _simulate_step(progress, log, (page + 1) / pages,
                       f"Page {page + 1}/{pages}", 0.3, 0.6)
        for _ in range(min(50, limit - len(out))):
            u = _username(r)
            out.append({
                "target": target,
                "username": u,
                "full_name": _full_name(r),
                "profile_url": f"https://www.instagram.com/{u}/",
                "is_verified": r.random() < 0.05,
            })
        if len(out) >= limit:
            break
    progress(1.0, "Done")
    log(f"Extracted {len(out)} followers.")
    return out


def run_following_extractor(inputs, account, progress, log) -> list[dict]:
    if not account:
        raise ValueError("This phantom needs an Instagram account.")
    target = (inputs.get("target_username") or "").lstrip("@").strip()
    if not target:
        raise ValueError("Provide a target username.")
    limit = _clamp_limit(inputs.get("limit"), default=100, hi=5000)
    log(f"Listing accounts followed by @{target} (target {limit})…")
    out = []
    r = _rng("following", target)
    pages = max(1, limit // 50)
    for page in range(pages):
        _simulate_step(progress, log, (page + 1) / pages,
                       f"Page {page + 1}/{pages}", 0.3, 0.6)
        for _ in range(min(50, limit - len(out))):
            u = _username(r)
            out.append({
                "target": target,
                "username": u,
                "full_name": _full_name(r),
                "profile_url": f"https://www.instagram.com/{u}/",
            })
        if len(out) >= limit:
            break
    progress(1.0, "Done")
    return out


def _action_runner(action: str, verb: str):
    def runner(inputs, account, progress, log) -> list[dict]:
        if not account:
            raise ValueError("This phantom needs an Instagram account.")
        raw = inputs.get("targets", "") or ""
        targets = [t.strip() for t in raw.replace(",", "\n").splitlines() if t.strip()]
        if not targets:
            raise ValueError("Provide at least one target.")
        delay = max(2, int(inputs.get("delay_seconds") or 8))
        log(f"Account @{account['username']} will {verb} {len(targets)} target(s) "
            f"with ~{delay}s delay (simulated).")
        out = []
        for i, t in enumerate(targets, 1):
            _simulate_step(progress, log, i / len(targets),
                           f"{verb.capitalize()} {t}",
                           delay_min=0.1, delay_max=0.3)
            r = _rng(action, t, i)
            outcome = "ok" if r.random() > 0.05 else "skipped"
            out.append({
                "target": t,
                "action": action,
                "by": account["username"],
                "outcome": outcome,
                "note": "" if outcome == "ok" else "rate-limit cooldown (simulated)",
                "timestamp": int(time.time()),
            })
        ok = sum(1 for r in out if r["outcome"] == "ok")
        log(f"{verb.capitalize()}: {ok}/{len(out)} targets succeeded.")
        return out
    return runner


run_auto_follow = _action_runner("follow", "follow")
run_auto_unfollow = _action_runner("unfollow", "unfollow")
run_auto_like = _action_runner("like", "like")


def run_auto_commenter(inputs, account, progress, log) -> list[dict]:
    if not account:
        raise ValueError("This phantom needs an Instagram account.")
    raw = inputs.get("post_urls", "") or ""
    urls = [u.strip() for u in raw.replace(",", "\n").splitlines() if u.strip()]
    if not urls:
        raise ValueError("Provide at least one post URL.")
    pool_raw = inputs.get("comments", "") or ""
    pool = [p.strip() for p in pool_raw.splitlines() if p.strip()]
    if not pool:
        raise ValueError("Provide at least one comment template.")
    log(f"Account @{account['username']} will comment on {len(urls)} post(s) (simulated).")
    out = []
    for i, url in enumerate(urls, 1):
        _simulate_step(progress, log, i / len(urls), f"Commenting on {url}",
                       delay_min=0.15, delay_max=0.35)
        r = _rng("comment", url, i)
        text = r.choice(pool)
        out.append({
            "post_url": url,
            "by": account["username"],
            "comment": text,
            "outcome": "ok",
            "timestamp": int(time.time()),
        })
    return out


def run_dm_sender(inputs, account, progress, log) -> list[dict]:
    if not account:
        raise ValueError("This phantom needs an Instagram account.")
    raw = inputs.get("recipients", "") or ""
    recipients = [u.strip().lstrip("@") for u in raw.replace(",", "\n").splitlines() if u.strip()]
    if not recipients:
        raise ValueError("Provide at least one recipient.")
    template = (inputs.get("template") or "").strip()
    if not template:
        raise ValueError("Provide a message template.")
    log(f"Account @{account['username']} will DM {len(recipients)} recipient(s) (simulated).")
    out = []
    for i, u in enumerate(recipients, 1):
        _simulate_step(progress, log, i / len(recipients), f"DM @{u}",
                       delay_min=0.2, delay_max=0.5)
        r = _rng("dm", u, i)
        rendered = template.replace("{username}", u).replace("{name}", u.split(".")[0].capitalize())
        out.append({
            "recipient": u,
            "by": account["username"],
            "message": rendered,
            "outcome": "ok" if r.random() > 0.08 else "skipped",
            "timestamp": int(time.time()),
        })
    return out


def run_story_viewer(inputs, account, progress, log) -> list[dict]:
    if not account:
        raise ValueError("This phantom needs an Instagram account.")
    raw = inputs.get("usernames", "") or ""
    targets = [t.strip().lstrip("@") for t in raw.replace(",", "\n").splitlines() if t.strip()]
    if not targets:
        raise ValueError("Provide at least one username.")
    out = []
    for i, t in enumerate(targets, 1):
        _simulate_step(progress, log, i / len(targets), f"Viewing stories of @{t}",
                       delay_min=0.1, delay_max=0.3)
        r = _rng("story", t)
        viewed = r.randint(0, 6)
        out.append({
            "target": t,
            "by": account["username"],
            "stories_viewed": viewed,
            "outcome": "ok" if viewed > 0 else "no_stories",
            "timestamp": int(time.time()),
        })
    return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Phantom] = {}


def _register(p: Phantom):
    REGISTRY[p.id] = p


_register(Phantom(
    id="profile_scraper",
    name="Profile Scraper",
    tagline="Extract profile info for a list of usernames.",
    category="scraper",
    description="Given a list of usernames, returns bio, follower/following counts, "
                "verification status, external URL and more.",
    icon="🔎",
    needs_account=False,
    fields=[
        Field("usernames", "Usernames", kind="textarea", required=True,
              placeholder="natgeo\nnasa\nzuck",
              help="One per line or comma-separated. Without the @."),
    ],
    output_columns=["username", "full_name", "followers", "following", "posts",
                    "is_verified", "is_private", "bio", "profile_url"],
    runner=run_profile_scraper,
))

_register(Phantom(
    id="hashtag_scraper",
    name="Hashtag Search Export",
    tagline="Collect recent posts for a hashtag.",
    category="scraper",
    description="Search a hashtag and export the top/recent posts with engagement metrics.",
    icon="#️⃣",
    needs_account=False,
    fields=[
        Field("hashtag", "Hashtag", required=True, placeholder="streetphotography"),
        Field("limit", "Max posts", kind="number", default=30,
              help="Up to 500."),
    ],
    output_columns=["post_url", "author", "likes", "comments", "media_type",
                    "timestamp", "caption"],
    runner=run_hashtag_scraper,
))

_register(Phantom(
    id="post_scraper",
    name="Post Scraper",
    tagline="Fetch metadata for a list of post URLs.",
    category="scraper",
    description="Provide post URLs (https://instagram.com/p/...) and export caption, "
                "engagement, media type and author.",
    icon="🖼️",
    needs_account=False,
    fields=[
        Field("post_urls", "Post URLs", kind="textarea", required=True,
              placeholder="https://www.instagram.com/p/AbCdEf12345/"),
    ],
    output_columns=["post_url", "author", "likes", "comments", "media_type",
                    "timestamp", "caption"],
    runner=run_post_scraper,
))

_register(Phantom(
    id="post_likers",
    name="Post Likers Export",
    tagline="List everyone who liked a post.",
    category="scraper",
    description="Paginate through the likers of a single post and export usernames + "
                "profile URLs.",
    icon="❤️",
    needs_account=False,
    fields=[
        Field("post_url", "Post URL", required=True,
              placeholder="https://www.instagram.com/p/AbCdEf12345/"),
        Field("limit", "Max likers", kind="number", default=50),
    ],
    output_columns=["username", "full_name", "profile_url"],
    runner=run_post_likers,
))

_register(Phantom(
    id="post_commenters",
    name="Comment Extractor",
    tagline="Pull all comments from a post.",
    category="scraper",
    description="Extract the comment thread under a post, including commenter username, "
                "comment text, likes, and timestamp.",
    icon="💬",
    needs_account=False,
    fields=[
        Field("post_url", "Post URL", required=True),
        Field("limit", "Max comments", kind="number", default=50),
    ],
    output_columns=["commenter", "comment", "likes", "timestamp"],
    runner=run_post_commenters,
))

_register(Phantom(
    id="followers_extractor",
    name="Followers Extractor",
    tagline="List followers of any account.",
    category="scraper",
    description="Paginate through an account's follower list. Requires a logged-in "
                "Instagram account.",
    icon="👥",
    needs_account=True,
    fields=[
        Field("target_username", "Target username", required=True, placeholder="natgeo"),
        Field("limit", "Max followers", kind="number", default=200),
    ],
    output_columns=["username", "full_name", "profile_url", "is_verified"],
    runner=run_followers_extractor,
))

_register(Phantom(
    id="following_extractor",
    name="Following Extractor",
    tagline="List who an account follows.",
    category="scraper",
    description="Paginate through who a target account follows. Requires a logged-in "
                "Instagram account.",
    icon="🧭",
    needs_account=True,
    fields=[
        Field("target_username", "Target username", required=True),
        Field("limit", "Max accounts", kind="number", default=200),
    ],
    output_columns=["username", "full_name", "profile_url"],
    runner=run_following_extractor,
))

_register(Phantom(
    id="auto_follow",
    name="Auto Follow",
    tagline="Follow a list of usernames from your account.",
    category="action",
    description="Follows a list of accounts at a polite cadence. Use after extracting "
                "targets with a scraper phantom.",
    icon="➕",
    needs_account=True,
    fields=[
        Field("targets", "Usernames to follow", kind="textarea", required=True,
              placeholder="user_one\nuser_two"),
        Field("delay_seconds", "Delay between actions (sec)", kind="number", default=10),
    ],
    output_columns=["target", "outcome", "timestamp"],
    runner=run_auto_follow,
))

_register(Phantom(
    id="auto_unfollow",
    name="Auto Unfollow",
    tagline="Unfollow accounts you no longer want to follow.",
    category="action",
    description="Unfollows a list of accounts with a configurable delay.",
    icon="➖",
    needs_account=True,
    fields=[
        Field("targets", "Usernames to unfollow", kind="textarea", required=True),
        Field("delay_seconds", "Delay between actions (sec)", kind="number", default=10),
    ],
    output_columns=["target", "outcome", "timestamp"],
    runner=run_auto_unfollow,
))

_register(Phantom(
    id="auto_like",
    name="Auto Liker",
    tagline="Like a list of posts.",
    category="action",
    description="Likes each post URL in the list.",
    icon="👍",
    needs_account=True,
    fields=[
        Field("targets", "Post URLs to like", kind="textarea", required=True),
        Field("delay_seconds", "Delay between actions (sec)", kind="number", default=8),
    ],
    output_columns=["target", "outcome", "timestamp"],
    runner=run_auto_like,
))

_register(Phantom(
    id="auto_commenter",
    name="Auto Commenter",
    tagline="Drop comments on posts using a template pool.",
    category="action",
    description="Comments on each post URL with a randomly-chosen line from your pool.",
    icon="🗣️",
    needs_account=True,
    fields=[
        Field("post_urls", "Post URLs", kind="textarea", required=True),
        Field("comments", "Comment pool (one per line)", kind="textarea", required=True,
              placeholder="Love this 🔥\nIncredible shot\nSaving this one"),
    ],
    output_columns=["post_url", "comment", "outcome", "timestamp"],
    runner=run_auto_commenter,
))

_register(Phantom(
    id="dm_sender",
    name="DM Sender",
    tagline="Send a personalized DM to a list of accounts.",
    category="engagement",
    description="Renders a template per recipient (supports {username}, {name}) and "
                "sends a DM from your Instagram account.",
    icon="✉️",
    needs_account=True,
    fields=[
        Field("recipients", "Recipients", kind="textarea", required=True,
              placeholder="user_one\nuser_two"),
        Field("template", "Message template", kind="textarea", required=True,
              placeholder="Hey {name}, loved your last post — would you be open to a collab?"),
    ],
    output_columns=["recipient", "message", "outcome", "timestamp"],
    runner=run_dm_sender,
))

_register(Phantom(
    id="story_viewer",
    name="Story Viewer",
    tagline="Quietly view stories of target accounts.",
    category="engagement",
    description="Views every active story of each target account from your logged-in "
                "Instagram session.",
    icon="👁️",
    needs_account=True,
    fields=[
        Field("usernames", "Target usernames", kind="textarea", required=True),
    ],
    output_columns=["target", "stories_viewed", "outcome", "timestamp"],
    runner=run_story_viewer,
))


def get(phantom_id: str) -> Phantom | None:
    return REGISTRY.get(phantom_id)


def all_phantoms() -> list[Phantom]:
    return list(REGISTRY.values())


def by_category() -> dict[str, list[Phantom]]:
    out: dict[str, list[Phantom]] = {}
    for p in REGISTRY.values():
        out.setdefault(p.category, []).append(p)
    return out


CATEGORY_LABELS = {
    "scraper": "Scrapers",
    "action": "Action Bots",
    "engagement": "Engagement",
}
