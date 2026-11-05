"""Background upload jobs with status shared across workers via Postgres (or memory).

Status (stage, done, total, message) is stored in the database whenever
DATABASE_URL is set, so a status poll never hits an empty in-memory map on
another worker or after a deploy. The Excel download bytes are written under
JOB_DIR and cleaned up after JOB_TTL_SECONDS.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table, Text,
                        create_engine, delete, select, update)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .storage import normalize_database_url, persistence_enabled

log = logging.getLogger(__name__)
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", str(7 * 24 * 3600)))  # Excel downloads kept 7 days
JOB_DIR = Path(os.environ.get("JOB_DIR", "/tmp/mt-jobs"))
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_engine = None
metadata = MetaData()

jobs_table = Table(
    "upload_jobs", metadata,
    Column("id", String(64), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("state", String(16), nullable=False),
    Column("stage", Text, nullable=False, default=""),
    Column("done", Integer, nullable=False, default=0),
    Column("total", Integer, nullable=False, default=0),
    Column("message", Text, nullable=False, default=""),
    Column("error", Text, nullable=False, default=""),
    Column("filenames", Text, nullable=False, default=""),
    Column("rows", Integer, nullable=False, default=0),
    Column("added", Integer),
    Column("skipped", Integer),
    Column("items", Integer),
    Column("has_download", Integer, nullable=False, default=0),
    Column("started", DateTime(timezone=True), nullable=False),
    Column("updated", DateTime(timezone=True), nullable=False),
)


def _use_db() -> bool:
    return persistence_enabled()


def _eng():
    global _engine
    if _engine is None:
        from .storage import database_url
        _engine = create_engine(normalize_database_url(database_url()), pool_pre_ping=True)
        metadata.create_all(_engine)
        JOB_DIR.mkdir(parents=True, exist_ok=True)
    return _engine


def _utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _prune(now: float) -> None:
    for jid in [j for j, v in _jobs.items() if v["state"] != "running" and now - v.get("_ts", now) > JOB_TTL_SECONDS]:
        _jobs.pop(jid, None)
        p = JOB_DIR / f"{jid}.bin"
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    if _use_db():
        try:
            from datetime import timedelta
            cutoff = _utc() - timedelta(seconds=JOB_TTL_SECONDS)
            with _eng().begin() as conn:
                old = conn.execute(select(jobs_table.c.id).where(
                    jobs_table.c.state != "running", jobs_table.c.updated < cutoff)).all()
                for (jid,) in old:
                    p = JOB_DIR / f"{jid}.bin"
                    if p.exists():
                        try:
                            p.unlink()
                        except OSError:
                            pass
                conn.execute(delete(jobs_table).where(
                    jobs_table.c.state != "running", jobs_table.c.updated < cutoff))
        except Exception as exc:
            log.warning("Job prune failed: %s", type(exc).__name__)


def _write_db(jid: str, fields: dict) -> None:
    if not _use_db():
        return
    try:
        eng = _eng()
        now = _utc()
        row = {"id": jid, "started": now, "updated": now,
               "kind": fields.get("kind", ""), "state": fields.get("state", "running"),
               "stage": fields.get("stage", ""), "done": int(fields.get("done") or 0),
               "total": int(fields.get("total") or 0), "message": fields.get("message") or "",
               "error": fields.get("error") or "", "filenames": fields.get("filenames") or "",
               "rows": int(fields.get("rows") or 0), "added": fields.get("added"),
               "skipped": fields.get("skipped"), "items": fields.get("items"),
               "has_download": 1 if fields.get("has_download") else 0}
        with eng.begin() as conn:
            # Upsert works on Postgres; SQLite falls back to delete+insert.
            url = str(eng.url)
            if url.startswith("postgresql"):
                stmt = pg_insert(jobs_table).values(**row)
                updates = {k: row[k] for k in row if k not in ("id", "started")}
                conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=updates))
            else:
                conn.execute(delete(jobs_table).where(jobs_table.c.id == jid))
                conn.execute(jobs_table.insert().values(**row))
    except Exception as exc:
        log.warning("Job status write failed: %s", type(exc).__name__)


def _update_db(jid: str, fields: dict) -> None:
    if not _use_db():
        return
    try:
        vals = {k: fields[k] for k in ("state", "stage", "done", "total", "message", "error",
                                       "filenames", "rows", "added", "skipped", "items") if k in fields}
        if "has_download" in fields:
            vals["has_download"] = 1 if fields["has_download"] else 0
        vals["updated"] = _utc()
        with _eng().begin() as conn:
            conn.execute(update(jobs_table).where(jobs_table.c.id == jid).values(**vals))
    except Exception as exc:
        log.warning("Job status update failed: %s", type(exc).__name__)


def start(kind: str, work: Callable[[Callable], dict], *, filenames: str = "", rows: int = 0) -> str:
    jid = secrets.token_urlsafe(16)
    now = time.time()
    seed = {"kind": kind, "state": "running", "stage": "File accepted", "done": 0, "total": 0,
            "message": "", "error": "", "filenames": filenames, "rows": rows,
            "started": now, "updated": now, "_ts": now}
    with _lock:
        _prune(now)
        _jobs[jid] = {**seed, "id": jid}
    _write_db(jid, seed)

    def progress(stage: str, done: int = 0, total: int = 0, **extra) -> None:
        with _lock:
            _jobs[jid].update(stage=stage, done=int(done), total=int(total), updated=time.time(),
                              _ts=time.time(), **extra)
            snap = dict(_jobs[jid])
        _update_db(jid, snap)

    def run():
        try:
            result = work(progress) or {}
            payload = result.pop("payload", None)
            if payload is not None:
                JOB_DIR.mkdir(parents=True, exist_ok=True)
                (JOB_DIR / f"{jid}.bin").write_bytes(payload)
                result["has_download"] = True
            with _lock:
                _jobs[jid].update(state="done", updated=time.time(), _ts=time.time(), **result)
                snap = dict(_jobs[jid])
            _update_db(jid, snap)
        except Exception as exc:
            msg = str(exc) if isinstance(exc, ValueError) else "Could not process the upload."
            if not isinstance(exc, ValueError):
                log.warning("%s job failed: %s", kind, type(exc).__name__)
            with _lock:
                _jobs[jid].update(state="error", error=msg[:400], updated=time.time(), _ts=time.time())
                snap = dict(_jobs[jid])
            _update_db(jid, snap)
        finally:
            with _lock:
                j = _jobs[jid]
                log.info("%s job finished in %.1fs (%s)", kind, time.time() - j["started"], j["state"])

    threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
    return jid


def get(jid: str) -> dict | None:
    with _lock:
        j = _jobs.get(jid)
        if j:
            return dict(j)
    if not _use_db():
        return None
    try:
        with _eng().connect() as conn:
            row = conn.execute(select(jobs_table).where(jobs_table.c.id == jid)).mappings().first()
        if not row:
            return None
        out = dict(row)
        out["has_download"] = bool(out.get("has_download"))
        out["started"] = out["started"].timestamp() if hasattr(out["started"], "timestamp") else time.time()
        out["updated"] = out["updated"].timestamp() if hasattr(out["updated"], "timestamp") else time.time()
        return out
    except Exception as exc:
        log.warning("Job status read failed: %s", type(exc).__name__)
        return None


def payload(jid: str) -> bytes | None:
    with _lock:
        j = _jobs.get(jid)
        if j and "payload" in j:
            return j["payload"]
    p = JOB_DIR / f"{jid}.bin"
    if p.exists():
        return p.read_bytes()
    return None


def list_recent_downloads(limit: int = 10) -> list[dict]:
    """Finished screen jobs that still have an Excel file on disk (or marked has_download)."""
    out = []
    if _use_db():
        try:
            with _eng().connect() as conn:
                rows = conn.execute(
                    select(jobs_table).where(jobs_table.c.kind == "screen", jobs_table.c.state == "done",
                                             jobs_table.c.has_download == 1)
                    .order_by(jobs_table.c.updated.desc()).limit(limit)).mappings().all()
            for row in rows:
                jid = row["id"]
                if not (JOB_DIR / f"{jid}.bin").exists() and not _jobs.get(jid, {}).get("payload"):
                    continue
                out.append({"id": jid, "filenames": row["filenames"], "message": row["message"],
                            "rows": row["rows"], "updated": row["updated"].isoformat() if row["updated"] else ""})
        except Exception as exc:
            log.warning("list_recent_downloads failed: %s", type(exc).__name__)
    return out


def list_running() -> list[dict]:
    out = []
    with _lock:
        for jid, j in _jobs.items():
            if j.get("state") == "running":
                out.append({**j, "id": jid})
    if _use_db():
        try:
            with _eng().connect() as conn:
                rows = conn.execute(select(jobs_table).where(jobs_table.c.state == "running")).mappings().all()
            for row in rows:
                if not any(o.get("id") == row["id"] for o in out):
                    d = dict(row)
                    d["has_download"] = bool(d.get("has_download"))
                    out.append(d)
        except Exception as exc:
            log.warning("list_running failed: %s", type(exc).__name__)
    return out


def mark_interrupted(jid: str, reason: str = "Interrupted by a deploy. Please upload again.") -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(state="error", error=reason, updated=time.time(), _ts=time.time())
    _update_db(jid, {"state": "error", "error": reason})


def reclaim_orphans_on_boot() -> int:
    """Running rows left from a prior process cannot continue; mark them so the UI stops spinning."""
    n = 0
    if not _use_db():
        return 0
    try:
        with _eng().connect() as conn:
            rows = conn.execute(select(jobs_table.c.id).where(jobs_table.c.state == "running")).all()
        for (jid,) in rows:
            mark_interrupted(jid)
            n += 1
        if n:
            log.warning("Marked %d orphaned upload job(s) interrupted after restart", n)
    except Exception as exc:
        log.warning("reclaim_orphans_on_boot failed: %s", type(exc).__name__)
    return n


def public(j: dict, jid: str | None = None) -> dict:
    keys = ("kind", "state", "stage", "done", "total", "message", "error", "added", "skipped",
            "items", "filenames", "rows")
    out = {k: j[k] for k in keys if k in j and j[k] is not None}
    started = j.get("started") or time.time()
    updated = j.get("updated") or started
    if not isinstance(started, (int, float)):
        started = time.time()
    if not isinstance(updated, (int, float)):
        updated = time.time()
    out["elapsed"] = round((updated if j.get("state") != "running" else time.time()) - started, 1)
    jid = jid or j.get("id") or ""
    out["has_download"] = bool(j.get("has_download") or "payload" in j or (jid and (JOB_DIR / f"{jid}.bin").exists()))
    return out
