"""In-process background jobs so long uploads don't hit the request timeout.

The app runs one gunicorn worker, so a module-level registry is shared by every request.
Finished jobs are kept for JOB_TTL_SECONDS so the browser can fetch the result.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)
JOB_TTL_SECONDS = 3600
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _prune(now: float) -> None:
    for jid in [j for j, v in _jobs.items() if v["state"] != "running" and now - v["updated"] > JOB_TTL_SECONDS]:
        _jobs.pop(jid, None)


def start(kind: str, work: Callable[[Callable], dict]) -> str:
    """Run work(progress) in a thread. work returns a dict merged into the job on success."""
    jid = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        _prune(now)
        _jobs[jid] = {"kind": kind, "state": "running", "stage": "Starting", "done": 0, "total": 0,
                      "message": "", "error": "", "started": now, "updated": now}

    def progress(stage: str, done: int = 0, total: int = 0) -> None:
        with _lock:
            _jobs[jid].update(stage=stage, done=int(done), total=int(total), updated=time.time())

    def run():
        try:
            result = work(progress) or {}
            with _lock:
                _jobs[jid].update(state="done", updated=time.time(), **result)
        except Exception as exc:  # message is already user-safe when raised as ValueError
            msg = str(exc) if isinstance(exc, ValueError) else "Could not process the upload."
            if not isinstance(exc, ValueError):
                log.warning("%s job failed: %s", kind, type(exc).__name__)
            with _lock:
                _jobs[jid].update(state="error", error=msg[:400], updated=time.time())
        finally:
            with _lock:
                j = _jobs[jid]
                log.info("%s job finished in %.1fs (%s)", kind, time.time() - j["started"], j["state"])

    threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
    return jid


def get(jid: str) -> dict | None:
    with _lock:
        j = _jobs.get(jid)
        return dict(j) if j else None


def public(j: dict) -> dict:
    keys = ("kind", "state", "stage", "done", "total", "message", "error", "added", "skipped", "items")
    out = {k: j[k] for k in keys if k in j}
    out["elapsed"] = round((j["updated"] if j["state"] != "running" else time.time()) - j["started"], 1)
    out["has_download"] = "payload" in j
    return out
