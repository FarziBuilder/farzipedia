"""Background workers for running phantoms and processing schedules."""

import threading
import time
import traceback

import phantoms
import store


def _run_phantom(rid: str):
    run = store.get_run(rid)
    if not run:
        return
    phantom = phantoms.get(run["phantom_id"])
    if not phantom:
        store.update_run(rid, status="error", message="Unknown phantom",
                         finished_at=time.time())
        store.append_log(rid, f"Unknown phantom {run['phantom_id']}")
        return

    account = store.get_account(run["account_id"]) if run["account_id"] else None
    store.update_run(rid, status="running", started_at=time.time(),
                     message="Running…")
    store.append_log(rid, f"Starting phantom: {phantom.name}")

    def progress(frac: float, msg: str = ""):
        store.update_run(rid, progress=float(frac), message=msg or "")

    def log(line: str):
        store.append_log(rid, line)

    try:
        results = phantom.runner(run["inputs"], account, progress, log)
        store.write_results(rid, results)
        store.update_run(
            rid,
            status="done",
            progress=1.0,
            message=f"Done — {len(results)} result(s)",
            finished_at=time.time(),
            result_count=len(results),
        )
        store.append_log(rid, f"Finished. {len(results)} result(s).")
    except Exception as e:
        store.update_run(rid, status="error", message=str(e),
                         finished_at=time.time())
        store.append_log(rid, f"ERROR: {e}")
        store.append_log(rid, traceback.format_exc())


def enqueue(rid: str):
    """Launch a run on its own thread (simple in-process job queue)."""
    threading.Thread(target=_run_phantom, args=(rid,), daemon=True).start()


def _scheduler_loop():
    while True:
        try:
            now = time.time()
            for sched in store.due_schedules(now):
                run = store.create_run(
                    sched["phantom_id"], sched["inputs"],
                    account_id=sched["account_id"], schedule_id=sched["id"],
                )
                store.mark_schedule_ran(sched["id"])
                store.append_log(run["id"], f"Triggered by schedule {sched['id']}")
                enqueue(run["id"])
        except Exception as e:
            print("scheduler error:", e)
        time.sleep(30)


def start_background():
    threading.Thread(target=_scheduler_loop, daemon=True).start()
