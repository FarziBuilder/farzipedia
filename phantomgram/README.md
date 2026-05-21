# Phantomgram 👻

A self-hosted, PhantomBuster-style automation suite for Instagram. Browse a
catalog of "phantoms" (scrapers + action bots), launch runs from a web UI,
download results as CSV/JSON, and schedule recurring jobs.

> ⚠️ **Demo / educational build.** Phantom runners in `phantoms.py` simulate
> Instagram interactions and produce realistic synthetic data, so the full
> platform is explorable without violating Instagram's Terms of Service or
> getting accounts banned. The architecture cleanly supports swapping in
> real implementations where you have proper authorization (e.g. the
> [Instagram Graph API](https://developers.facebook.com/docs/instagram-api)
> for Business / Creator accounts).

## Features

- **13 Instagram phantoms** across three categories
  - **Scrapers** — Profile Scraper, Hashtag Export, Post Scraper,
    Post Likers, Comment Extractor
  - **Action bots** — Followers / Following Extractor, Auto Follow,
    Auto Unfollow, Auto Liker, Auto Commenter
  - **Engagement** — DM Sender, Story Viewer
- **Dashboard** with stats, recent activity and a category-grouped catalog
- **Per-phantom configuration** with typed input fields and an output schema
- **Live run page** with progress, console logs, and a results table
- **CSV + JSON downloads** of every run's output
- **Account management** for the Instagram identities action phantoms run as
- **Scheduling** — fire any phantom hourly / every 6 h / daily / weekly
- **Background scheduler** that picks up due jobs every 30 s
- **SQLite-backed** (`data/phantomgram.db`) — no external services

## Run it

```bash
cd phantomgram
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:8001>.

Override the bind address with `HOST` / `PORT` env vars.

## Project layout

```
phantomgram/
├── app.py            # FastAPI app & routes
├── store.py          # SQLite + filesystem storage
├── phantoms.py       # Phantom registry & (simulated) runners
├── scheduler.py      # Background scheduler + in-process job queue
├── templates/        # Jinja2 templates
├── static/           # CSS
└── data/             # SQLite db + per-run results & logs (created at runtime)
```

## Plugging in real implementations

Every phantom's runner has the signature

```python
def runner(inputs: dict, account: dict | None,
           progress: Callable[[float, str], None],
           log: Callable[[str], None]) -> list[dict]:
    ...
```

Replace the bodies in `phantoms.py` with calls against the
[Instagram Graph API](https://developers.facebook.com/docs/instagram-api) (or
any third-party API you have authorization to use). The web UI, job queue,
storage, scheduler and downloads all work unchanged — they only care about
the shape of the return value.
