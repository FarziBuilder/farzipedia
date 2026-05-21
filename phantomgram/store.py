"""SQLite-backed storage for accounts, runs, and schedules."""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "phantomgram.db"
RUNS_DIR = DATA_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

_LOCK = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    label TEXT,
    session_cookie TEXT,
    proxy TEXT,
    status TEXT DEFAULT 'active',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    phantom_id TEXT NOT NULL,
    account_id TEXT,
    inputs TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL DEFAULT 0,
    message TEXT,
    result_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    schedule_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_phantom ON runs(phantom_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    phantom_id TEXT NOT NULL,
    account_id TEXT,
    inputs TEXT NOT NULL,
    cadence TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    last_run_at REAL,
    next_run_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def conn():
    with _LOCK:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def _row(r):
    return dict(r) if r else None


# ---- Accounts ----------------------------------------------------------

def add_account(username: str, label: str = "", session_cookie: str = "", proxy: str = "") -> dict:
    aid = uuid.uuid4().hex[:12]
    with conn() as c:
        c.execute(
            "INSERT INTO accounts(id, username, label, session_cookie, proxy, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (aid, username, label, session_cookie, proxy, time.time()),
        )
    return get_account(aid)


def get_account(aid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE id = ?", (aid,)).fetchone()
    return _row(r)


def list_accounts() -> list[dict]:
    with conn() as c:
        rs = c.execute("SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rs]


def delete_account(aid: str):
    with conn() as c:
        c.execute("DELETE FROM accounts WHERE id = ?", (aid,))


# ---- Runs --------------------------------------------------------------

def create_run(phantom_id: str, inputs: dict, account_id: str | None = None,
               schedule_id: str | None = None) -> dict:
    rid = uuid.uuid4().hex[:12]
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT INTO runs(id, phantom_id, account_id, inputs, status, message, created_at, schedule_id) "
            "VALUES (?, ?, ?, ?, 'queued', 'Queued', ?, ?)",
            (rid, phantom_id, account_id, json.dumps(inputs), now, schedule_id),
        )
    (RUNS_DIR / rid).mkdir(exist_ok=True)
    return get_run(rid)


def update_run(rid: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), rid))


def get_run(rid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["inputs"] = json.loads(d["inputs"]) if d.get("inputs") else {}
    return d


def list_runs(limit: int = 100, phantom_id: str | None = None,
              status: str | None = None) -> list[dict]:
    q = "SELECT * FROM runs"
    args: list = []
    where = []
    if phantom_id:
        where.append("phantom_id = ?")
        args.append(phantom_id)
    if status:
        where.append("status = ?")
        args.append(status)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        rs = c.execute(q, args).fetchall()
    out = []
    for r in rs:
        d = dict(r)
        d["inputs"] = json.loads(d["inputs"]) if d.get("inputs") else {}
        out.append(d)
    return out


def run_stats() -> dict:
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        done = c.execute("SELECT COUNT(*) FROM runs WHERE status = 'done'").fetchone()[0]
        running = c.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
        errored = c.execute("SELECT COUNT(*) FROM runs WHERE status = 'error'").fetchone()[0]
        total_results = c.execute("SELECT COALESCE(SUM(result_count), 0) FROM runs").fetchone()[0]
    return {
        "total": total,
        "done": done,
        "running": running,
        "errored": errored,
        "results": total_results,
    }


def append_log(rid: str, line: str):
    p = RUNS_DIR / rid / "log.txt"
    with p.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")


def read_log(rid: str) -> str:
    p = RUNS_DIR / rid / "log.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_results(rid: str, results: list[dict]):
    p = RUNS_DIR / rid / "results.json"
    p.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


def read_results(rid: str) -> list[dict]:
    p = RUNS_DIR / rid / "results.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


# ---- Schedules ---------------------------------------------------------

CADENCE_SECONDS = {
    "hourly": 3600,
    "every_6h": 21600,
    "daily": 86400,
    "weekly": 604800,
}


def add_schedule(phantom_id: str, inputs: dict, cadence: str,
                 account_id: str | None = None) -> dict:
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    nxt = now + CADENCE_SECONDS.get(cadence, 86400)
    with conn() as c:
        c.execute(
            "INSERT INTO schedules(id, phantom_id, account_id, inputs, cadence, "
            "next_run_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, phantom_id, account_id, json.dumps(inputs), cadence, nxt, now),
        )
    return get_schedule(sid)


def get_schedule(sid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["inputs"] = json.loads(d["inputs"]) if d.get("inputs") else {}
    return d


def list_schedules() -> list[dict]:
    with conn() as c:
        rs = c.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    out = []
    for r in rs:
        d = dict(r)
        d["inputs"] = json.loads(d["inputs"]) if d.get("inputs") else {}
        out.append(d)
    return out


def due_schedules(now: float) -> list[dict]:
    with conn() as c:
        rs = c.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ?",
            (now,),
        ).fetchall()
    out = []
    for r in rs:
        d = dict(r)
        d["inputs"] = json.loads(d["inputs"]) if d.get("inputs") else {}
        out.append(d)
    return out


def mark_schedule_ran(sid: str):
    now = time.time()
    sched = get_schedule(sid)
    if not sched:
        return
    nxt = now + CADENCE_SECONDS.get(sched["cadence"], 86400)
    with conn() as c:
        c.execute(
            "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
            (now, nxt, sid),
        )


def toggle_schedule(sid: str, enabled: bool):
    with conn() as c:
        c.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (1 if enabled else 0, sid))


def delete_schedule(sid: str):
    with conn() as c:
        c.execute("DELETE FROM schedules WHERE id = ?", (sid,))


# ---- Settings ----------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str):
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
