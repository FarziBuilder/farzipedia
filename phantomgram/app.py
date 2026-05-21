"""Phantomgram — a self-hosted PhantomBuster-style automation suite for Instagram.

DEMO/EDUCATIONAL build: phantom runners simulate execution and return realistic
synthetic data so the platform can be explored end-to-end without violating
Instagram's Terms of Service. The architecture is structured so the runners in
phantoms.py can be swapped for real implementations where you have proper
authorization (e.g. Instagram Graph API for Business/Creator accounts).
"""

import csv
import io
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import phantoms
import scheduler
import store

APP_ROOT = Path(__file__).parent.resolve()

app = FastAPI(title="Phantomgram")
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")


def _fmt_ts(ts):
    if not ts:
        return "—"
    return time.strftime("%b %d, %H:%M", time.localtime(float(ts)))


def _fmt_duration(start, end):
    if not start:
        return "—"
    end = end or time.time()
    d = int(end - start)
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m {d % 60}s"
    return f"{d // 3600}h {(d % 3600) // 60}m"


def _status_color(status: str) -> str:
    return {
        "queued": "amber",
        "running": "indigo",
        "done": "emerald",
        "error": "rose",
    }.get(status, "slate")


templates.env.filters["fmt_ts"] = _fmt_ts
templates.env.filters["fmt_duration"] = _fmt_duration
templates.env.filters["status_color"] = _status_color
templates.env.globals["CATEGORY_LABELS"] = phantoms.CATEGORY_LABELS


@app.on_event("startup")
def _startup():
    store.init()
    scheduler.start_background()


# ---- Pages ------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    stats = store.run_stats()
    recent = store.list_runs(limit=12)
    by_cat = phantoms.by_category()
    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": stats,
        "recent": recent,
        "by_cat": by_cat,
        "phantoms_total": len(phantoms.all_phantoms()),
        "accounts_total": len(store.list_accounts()),
        "schedules_total": len(store.list_schedules()),
        "get_phantom": phantoms.get,
        "active": "dashboard",
    })


@app.get("/phantoms", response_class=HTMLResponse)
def catalog(request: Request):
    by_cat = phantoms.by_category()
    return templates.TemplateResponse(request, "catalog.html", {
        "by_cat": by_cat,
        "active": "catalog",
    })


@app.get("/phantoms/{phantom_id}", response_class=HTMLResponse)
def phantom_detail(request: Request, phantom_id: str):
    p = phantoms.get(phantom_id)
    if not p:
        raise HTTPException(404, "Unknown phantom")
    return templates.TemplateResponse(request, "phantom.html", {
        "phantom": p,
        "accounts": store.list_accounts(),
        "recent_runs": store.list_runs(limit=10, phantom_id=phantom_id),
        "active": "catalog",
    })


@app.post("/phantoms/{phantom_id}/run")
async def launch_run(request: Request, phantom_id: str):
    p = phantoms.get(phantom_id)
    if not p:
        raise HTTPException(404, "Unknown phantom")
    form = await request.form()
    inputs = {}
    for f in p.fields:
        v = form.get(f.name)
        if f.kind == "checkbox":
            inputs[f.name] = bool(v)
        else:
            inputs[f.name] = (v or "").strip() if isinstance(v, str) else v
    account_id = form.get("account_id") or None
    if p.needs_account and not account_id:
        raise HTTPException(400, "This phantom requires an Instagram account.")

    cadence = form.get("schedule") or "once"
    if cadence != "once":
        store.add_schedule(phantom_id, inputs, cadence, account_id=account_id)
    run = store.create_run(phantom_id, inputs, account_id=account_id)
    store.append_log(run["id"], "Run queued from web UI")
    scheduler.enqueue(run["id"])
    return RedirectResponse(f"/runs/{run['id']}", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs_index(request: Request, phantom_id: str | None = None,
               status: str | None = None):
    runs = store.list_runs(limit=200, phantom_id=phantom_id, status=status)
    return templates.TemplateResponse(request, "runs.html", {
        "runs": runs,
        "phantoms": phantoms.all_phantoms(),
        "filter_phantom": phantom_id or "",
        "filter_status": status or "",
        "get_phantom": phantoms.get,
        "active": "runs",
    })


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    p = phantoms.get(run["phantom_id"])
    account = store.get_account(run["account_id"]) if run["account_id"] else None
    results = store.read_results(run_id) if run["status"] == "done" else []
    return templates.TemplateResponse(request, "run.html", {
        "run": run,
        "phantom": p,
        "account": account,
        "results": results,
        "log": store.read_log(run_id),
        "active": "runs",
    })


@app.get("/runs/{run_id}/status")
def run_status(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    return JSONResponse({
        "status": run["status"],
        "progress": run["progress"],
        "message": run["message"] or "",
        "result_count": run["result_count"],
        "elapsed": int(time.time() - (run["started_at"] or run["created_at"])),
        "log_tail": store.read_log(run_id).splitlines()[-30:],
    })


@app.get("/runs/{run_id}/download.csv")
def download_csv(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    rows = store.read_results(run_id)
    if not rows:
        return PlainTextResponse("(no results yet)", status_code=404)
    buf = io.StringIO()
    cols = list(rows[0].keys())
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in cols})
    fname = f"{run['phantom_id']}_{run_id}.csv"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/runs/{run_id}/download.json")
def download_json(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    rows = store.read_results(run_id)
    fname = f"{run['phantom_id']}_{run_id}.json"
    return Response(
        content=__import__("json").dumps(rows, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---- Accounts ---------------------------------------------------------

@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    return templates.TemplateResponse(request, "accounts.html", {
        "accounts": store.list_accounts(),
        "active": "accounts",
    })


@app.post("/accounts")
def accounts_add(username: str = Form(...), label: str = Form(""),
                 session_cookie: str = Form(""), proxy: str = Form("")):
    username = username.strip().lstrip("@")
    if not username:
        raise HTTPException(400, "Username is required")
    try:
        store.add_account(username, label.strip(), session_cookie.strip(), proxy.strip())
    except Exception as e:
        raise HTTPException(400, f"Could not add account: {e}")
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/delete")
def accounts_delete(account_id: str):
    store.delete_account(account_id)
    return RedirectResponse("/accounts", status_code=303)


# ---- Schedules --------------------------------------------------------

@app.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request):
    return templates.TemplateResponse(request, "schedules.html", {
        "schedules": store.list_schedules(),
        "get_phantom": phantoms.get,
        "get_account": store.get_account,
        "active": "schedules",
    })


@app.post("/schedules/{schedule_id}/toggle")
def schedules_toggle(schedule_id: str):
    s = store.get_schedule(schedule_id)
    if not s:
        raise HTTPException(404)
    store.toggle_schedule(schedule_id, not bool(s["enabled"]))
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{schedule_id}/delete")
def schedules_delete(schedule_id: str):
    store.delete_schedule(schedule_id)
    return RedirectResponse("/schedules", status_code=303)


# ---- Health -----------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True, "phantoms": len(phantoms.all_phantoms())}


if __name__ == "__main__":
    import os
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run("app:app", host=host, port=port, reload=False)
