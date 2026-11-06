"""Background upload jobs. Status and the Excel file live in Postgres (or SQLite).

`create_all` does not add columns to a table that already exists. `ensure_schema`
adds `payload` (and any later columns) before a status read, so a pre-existing
upload_jobs table does not raise ProgrammingError and turn into a 404.

The uploaded bytes are stored before the worker thread starts. After a process
restart, a still-running job is claimed and screened again from those bytes.
Posts already saved are reused by document id, URL, and text hash, so a second
pass does not delete anything and does not re-send those posts for review.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from sqlalchemy import (Column, DateTime, Integer, LargeBinary, MetaData, String, Table, Text,
                        create_engine, delete, func, or_, select, update)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .schema import ensure_schema
from .storage import normalize_database_url, persistence_enabled

log = logging.getLogger(__name__)
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", str(7 * 24 * 3600)))
LEASE_SECONDS = int(os.environ.get("JOB_LEASE_SECONDS", "30"))


class InputNotStored(RuntimeError):
    """The upload bytes are not durable. Do not start paid work or return 202."""


class LeaseLost(RuntimeError):
    """This worker no longer owns the job, so its writes were ignored."""
JOB_DIR = Path(os.environ.get("JOB_DIR", "/tmp/mt-jobs"))
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_engine = None
_engine_url = ""
_resume_factory: Callable | None = None
_worker_id = f"{os.getpid()}-{secrets.token_hex(4)}"
_tls = threading.local()


def current_jid() -> str | None:
    return getattr(_tls, "jid", None)
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
    Column("payload", LargeBinary),
    Column("checkpoint", Text),
    Column("resume_token", String(64)),
    Column("lease_until", DateTime(timezone=True)),
)

job_checkpoints = Table(
    "upload_job_checkpoints", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String(64), nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("body", Text, nullable=False),
)

job_inputs = Table(
    "upload_job_inputs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String(64), nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("filename", Text, nullable=False),
    Column("body", LargeBinary, nullable=False),
)

# Status polls must not load the Excel blob.
_STATUS_COLS = (
    "id", "kind", "state", "stage", "done", "total", "message", "error", "filenames",
    "rows", "added", "skipped", "items", "has_download", "started", "updated",
    "checkpoint", "resume_token", "lease_until",
)


def set_resume_factory(factory: Callable) -> None:
    """factory(files) -> work(progress) used to continue a job after a restart."""
    global _resume_factory
    _resume_factory = factory


def _use_db() -> bool:
    return persistence_enabled()


def _eng():
    global _engine, _engine_url
    url = normalize_database_url(__import__("triage.storage", fromlist=["database_url"]).database_url())
    if _engine is None or _engine_url != url:
        _engine = create_engine(url, pool_pre_ping=True)
        _engine_url = url
        ensure_schema(_engine, metadata)
        JOB_DIR.mkdir(parents=True, exist_ok=True)
    return _engine


def _utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _lease_deadline():
    from datetime import timedelta
    return _utc() + timedelta(seconds=max(5, LEASE_SECONDS))


def _as_dt(value):
    if value is None:
        return None
    if isinstance(value, str):
        from datetime import datetime, timezone
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if hasattr(value, "tzinfo"):
        if value.tzinfo is None:
            from datetime import timezone
            return value.replace(tzinfo=timezone.utc)
        return value
    return None


def _status_select():
    return select(*[jobs_table.c[name] for name in _STATUS_COLS])


def _row_to_public_dict(row) -> dict:
    out = {name: row[name] for name in _STATUS_COLS}
    out["has_download"] = bool(out.get("has_download"))
    started = out.get("started")
    updated = out.get("updated")
    out["started"] = started.timestamp() if hasattr(started, "timestamp") else time.time()
    out["updated"] = updated.timestamp() if hasattr(updated, "timestamp") else time.time()
    return out


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
                ids = [jid for (jid,) in old]
                for jid in ids:
                    p = JOB_DIR / f"{jid}.bin"
                    if p.exists():
                        try:
                            p.unlink()
                        except OSError:
                            pass
                if ids:
                    conn.execute(delete(job_inputs).where(job_inputs.c.job_id.in_(ids)))
                    conn.execute(delete(jobs_table).where(jobs_table.c.id.in_(ids)))
        except Exception as exc:
            log.warning("Job prune failed: %s", type(exc).__name__)


def _base_row(jid: str, fields: dict) -> dict:
    now = _utc()
    return {"id": jid, "started": fields.get("started_at") or now, "updated": now,
            "kind": fields.get("kind", ""), "state": fields.get("state", "running"),
            "stage": fields.get("stage", ""), "done": int(fields.get("done") or 0),
            "total": int(fields.get("total") or 0), "message": fields.get("message") or "",
            "error": fields.get("error") or "", "filenames": fields.get("filenames") or "",
            "rows": int(fields.get("rows") or 0), "added": fields.get("added"),
            "skipped": fields.get("skipped"), "items": fields.get("items"),
            "has_download": 1 if fields.get("has_download") else 0,
            "checkpoint": fields.get("checkpoint") or "",
            "resume_token": fields.get("resume_token") or _worker_id,
            "lease_until": _lease_deadline()}


def _write_db(jid: str, fields: dict) -> None:
    if not _use_db():
        return
    try:
        eng = _eng()
        row = _base_row(jid, fields)
        with eng.begin() as conn:
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


def _owns():
    """This worker may write a row it already owns, or a row nobody has claimed."""
    return or_(jobs_table.c.resume_token == _worker_id, jobs_table.c.resume_token.is_(None))


def _update_db(jid: str, fields: dict, payload: bytes | None = None, release_inputs: bool = False) -> bool:
    """Write status only while this worker holds the resume token. Returns whether the write landed."""
    if not _use_db():
        return False
    vals = {k: fields[k] for k in ("state", "stage", "done", "total", "message", "error",
                                   "filenames", "rows", "added", "skipped", "items",
                                   "checkpoint") if k in fields}
    if "has_download" in fields:
        vals["has_download"] = 1 if fields["has_download"] else 0
    if payload is not None:
        vals["payload"] = payload
        vals["has_download"] = 1
    vals["updated"] = _utc()
    vals["resume_token"] = _worker_id
    vals["lease_until"] = _lease_deadline()

    def _run() -> bool:
        with _eng().begin() as conn:
            res = conn.execute(
                update(jobs_table).where(jobs_table.c.id == jid, _owns()).values(**vals))
            if res.rowcount != 1:
                return False
            if release_inputs and fields.get("state") == "done":
                conn.execute(delete(job_inputs).where(job_inputs.c.job_id == jid))
                conn.execute(delete(job_checkpoints).where(job_checkpoints.c.job_id == jid))
            return True

    try:
        return _run()
    except Exception as exc:
        log.warning("Job status update failed: %s", type(exc).__name__)
        try:
            ensure_schema(_eng(), metadata)
            return _run()
        except Exception as exc2:
            log.warning("Job status update retry failed: %s", type(exc2).__name__)
            return False


def _store_inputs(jid: str, files: list[tuple[str, bytes]]) -> None:
    """Persist every upload byte. Raises InputNotStored instead of continuing."""
    if not files:
        raise InputNotStored("No upload bytes to store.")
    if not _use_db():
        raise InputNotStored("Database is not configured, so the upload was not accepted.")
    expected = [((name or "upload")[:300], data if data is not None else b"") for name, data in files]
    try:
        with _eng().begin() as conn:
            conn.execute(delete(job_inputs).where(job_inputs.c.job_id == jid))
            for seq, (name, data) in enumerate(expected):
                conn.execute(job_inputs.insert().values(
                    job_id=jid, seq=seq, filename=name, body=data))
        loaded = _load_inputs(jid, keep_empty=True)
        if loaded != expected:
            raise InputNotStored("Stored upload bytes did not match the file that was accepted.")
    except InputNotStored:
        raise
    except Exception as exc:
        log.warning("Storing upload bytes failed: %s", type(exc).__name__)
        raise InputNotStored("Could not store the upload.") from exc


def _load_inputs(jid: str, keep_empty: bool = False) -> list[tuple[str, bytes]]:
    if not _use_db():
        return []
    try:
        with _eng().connect() as conn:
            rows = conn.execute(
                select(job_inputs.c.filename, job_inputs.c.body)
                .where(job_inputs.c.job_id == jid)
                .order_by(job_inputs.c.seq)).all()
        out = []
        for name, body in rows:
            blob = b"" if body is None else bytes(body)
            if blob or keep_empty:
                out.append((name, blob))
        return out
    except Exception as exc:
        log.warning("Loading upload bytes failed: %s", type(exc).__name__)
        return []


def _touch_lease(jid: str) -> bool:
    if not _use_db():
        return False
    try:
        with _eng().begin() as conn:
            res = conn.execute(
                update(jobs_table)
                .where(jobs_table.c.id == jid, jobs_table.c.resume_token == _worker_id,
                       jobs_table.c.state == "running")
                .values(updated=_utc(), lease_until=_lease_deadline()))
            return res.rowcount == 1
    except Exception as exc:
        log.warning("Lease heartbeat failed: %s", type(exc).__name__)
        return False


def _heartbeat_loop(jid: str, stop: threading.Event) -> None:
    interval = max(1, LEASE_SECONDS // 3)
    while not stop.wait(interval):
        if not _touch_lease(jid):
            break


class JobCheckpoint:
    """Append-only batch store for one job. save() commits before it returns."""

    def __init__(self, jid: str):
        self.jid = jid

    def load(self) -> list[dict]:
        return load_batches(self.jid)

    def save(self, kind: str, records: list[dict]) -> None:
        append_batch(self.jid, kind, records)


def load_batches(jid: str) -> list[dict]:
    if not jid or not _use_db():
        return []
    try:
        with _eng().connect() as conn:
            rows = conn.execute(
                select(job_checkpoints.c.body)
                .where(job_checkpoints.c.job_id == jid)
                .order_by(job_checkpoints.c.seq)).all()
    except Exception as exc:
        log.warning("Loading review checkpoint failed: %s", type(exc).__name__)
        raise
    out = []
    for (body,) in rows:
        loaded = json.loads(body or "[]")
        if isinstance(loaded, list):
            out.extend(loaded)
    return out


def append_batch(jid: str, kind: str, records: list[dict]) -> None:
    """Insert one completed batch only if this worker still owns the lease."""
    if not records:
        return
    if not _use_db():
        raise LeaseLost("No database for the review checkpoint.")
    body = json.dumps(records, default=_json_default)
    with _eng().begin() as conn:
        owned = conn.execute(
            update(jobs_table)
            .where(jobs_table.c.id == jid, jobs_table.c.resume_token == _worker_id,
                   jobs_table.c.state == "running")
            .values(updated=_utc(), lease_until=_lease_deadline()))
        if owned.rowcount != 1:
            raise LeaseLost("Lost the job lease before the batch could be saved.")
        seq = conn.execute(
            select(func.max(job_checkpoints.c.seq)).where(job_checkpoints.c.job_id == jid)).scalar()
        conn.execute(job_checkpoints.insert().values(
            job_id=jid, seq=int(seq or 0) + 1, kind=kind[:16], body=body))


def _json_default(value):
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _spawn(jid: str, work: Callable) -> None:
    def run():
        _tls.jid = jid
        stop = threading.Event()
        threading.Thread(target=_heartbeat_loop, args=(jid, stop), name=f"lease-{jid[:6]}",
                         daemon=True).start()
        try:
            result = work(_progress(jid)) or {}
            payload = result.pop("payload", None)
            if payload is not None:
                JOB_DIR.mkdir(parents=True, exist_ok=True)
                (JOB_DIR / f"{jid}.bin").write_bytes(payload)
                result["has_download"] = True
            with _lock:
                if jid not in _jobs:
                    return
                _jobs[jid].update(state="done", updated=time.time(), _ts=time.time(),
                                  **{k: v for k, v in result.items() if k != "payload"})
                snap = {k: v for k, v in _jobs[jid].items() if k != "payload"}
            # Inputs stay until this fenced write says the dashboard save finished.
            _update_db(jid, snap, payload=payload, release_inputs=True)
            del payload
        except Exception as exc:
            if type(exc).__name__ == "SaveFailed" or isinstance(exc, ValueError):
                msg = str(exc)[:400]
            else:
                msg = "Could not process the upload."
                log.warning("job failed: %s", type(exc).__name__)
            with _lock:
                if jid in _jobs:
                    _jobs[jid].update(state="error", error=msg, updated=time.time(), _ts=time.time())
                    snap = dict(_jobs[jid])
                else:
                    snap = {"state": "error", "error": msg, "stage": "Interrupted"}
            # Keep the stored file and review batches so a later resume does not pay again.
            _update_db(jid, snap, release_inputs=False)
        finally:
            stop.set()
            with _lock:
                j = _jobs.get(jid) or {}
                log.info("job finished in %.1fs (%s)", time.time() - j.get("started", time.time()), j.get("state"))

    threading.Thread(target=run, name=f"job-{jid[:8]}", daemon=True).start()


def _progress(jid: str) -> Callable:
    def progress(stage: str, done: int = 0, total: int = 0, **extra) -> None:
        checkpoint = json.dumps({"stage": stage, "done": int(done), "total": int(total)})
        with _lock:
            if jid not in _jobs:
                return
            _jobs[jid].update(stage=stage, done=int(done), total=int(total), updated=time.time(),
                              _ts=time.time(), checkpoint=checkpoint, **extra)
            snap = {k: v for k, v in _jobs[jid].items() if k != "payload"}
        _update_db(jid, snap)
    return progress


def start(kind: str, work: Callable[[Callable], dict], *, filenames: str = "", rows: int = 0,
          inputs: list[tuple[str, bytes]] | None = None) -> str:
    jid = secrets.token_urlsafe(16)
    now = time.time()
    seed = {"kind": kind, "state": "running", "stage": "File accepted", "done": 0, "total": 0,
            "message": "", "error": "", "filenames": filenames, "rows": rows,
            "started": now, "updated": now, "_ts": now, "resume_token": _worker_id,
            "checkpoint": json.dumps({"stage": "File accepted", "done": 0, "total": rows})}
    with _lock:
        _prune(now)
        _jobs[jid] = {**seed, "id": jid}
    _write_db(jid, seed)
    if inputs is not None:
        try:
            _store_inputs(jid, inputs)
        except InputNotStored:
            mark_interrupted(jid, "Could not store the upload, so it was not started.")
            with _lock:
                _jobs.pop(jid, None)
            raise
    _spawn(jid, work)
    return jid


def _claim(jid: str, seen_token) -> bool:
    """Take a running job only when its lease has expired and the token is unchanged.

    The lease check is done in Python. SQLite stores datetimes as text, and a
    SQL comparison of those strings is not a time comparison.
    """
    try:
        now = _utc()
        token_cond = jobs_table.c.resume_token.is_(None) if seen_token is None else (
            jobs_table.c.resume_token == seen_token)
        with _eng().begin() as conn:
            row = conn.execute(
                select(jobs_table.c.state, jobs_table.c.resume_token, jobs_table.c.lease_until)
                .where(jobs_table.c.id == jid)).first()
            if not row or row[0] != "running":
                return False
            if (row[1] or None) != (seen_token or None):
                return False
            lease_dt = _as_dt(row[2])
            if lease_dt is not None and lease_dt > now:
                return False
            res = conn.execute(
                update(jobs_table)
                .where(jobs_table.c.id == jid, jobs_table.c.state == "running", token_cond)
                .values(resume_token=_worker_id, updated=now, lease_until=_lease_deadline(),
                        stage="Resuming after a restart"))
            return res.rowcount == 1
    except Exception as exc:
        log.warning("Job claim failed: %s", type(exc).__name__)
        return False


def resume_pending(factory: Callable | None = None) -> dict:
    """Continue running jobs whose upload bytes were stored. Others are marked interrupted.

    Jobs this process already has in memory are left alone. Returns counts; does not
    delete stored posts.
    """
    factory = factory or _resume_factory
    resumed = interrupted = 0
    if not _use_db():
        return {"resumed": 0, "interrupted": 0}
    try:
        with _eng().connect() as conn:
            rows = conn.execute(
                select(jobs_table.c.id, jobs_table.c.resume_token, jobs_table.c.lease_until)
                .where(jobs_table.c.state == "running")).all()
    except Exception as exc:
        log.warning("resume_pending failed: %s", type(exc).__name__)
        try:
            ensure_schema(_eng(), metadata)
            with _eng().connect() as conn:
                rows = conn.execute(
                    select(jobs_table.c.id, jobs_table.c.resume_token, jobs_table.c.lease_until)
                    .where(jobs_table.c.state == "running")).all()
        except Exception as exc2:
            log.warning("resume_pending retry failed: %s", type(exc2).__name__)
            return {"resumed": 0, "interrupted": 0}
    now = _utc()
    for jid, token, lease in rows:
        with _lock:
            if jid in _jobs and _jobs[jid].get("state") == "running":
                continue
        lease_dt = _as_dt(lease)
        # A live worker, including one from an overlapping deploy, still holds the lease.
        if lease_dt is not None and lease_dt > now and token != _worker_id:
            continue
        if token == _worker_id:
            continue
        if not _claim(jid, token):
            continue
        files = _load_inputs(jid)
        if not files or factory is None:
            mark_interrupted(jid, "Interrupted before the file was stored. Please upload again.")
            interrupted += 1
            continue
        now = time.time()
        with _lock:
            _jobs[jid] = {"id": jid, "kind": "screen", "state": "running",
                          "stage": "Resuming after a restart", "done": 0, "total": 0,
                          "message": "", "error": "", "filenames": ", ".join(n for n, _ in files)[:300],
                          "rows": 0, "started": now, "updated": now, "_ts": now,
                          "resume_token": _worker_id}
        _spawn(jid, factory(files))
        resumed += 1
        log.info("Resumed upload job %s after restart (%d file(s))", jid, len(files))
    return {"resumed": resumed, "interrupted": interrupted}


def get(jid: str) -> dict | None:
    with _lock:
        j = _jobs.get(jid)
        if j:
            return {k: v for k, v in j.items() if k != "payload"}
    if not _use_db():
        return None
    try:
        with _eng().connect() as conn:
            row = conn.execute(_status_select().where(jobs_table.c.id == jid)).mappings().first()
        return _row_to_public_dict(row) if row else None
    except Exception as exc:
        log.warning("Job status read failed: %s", type(exc).__name__)
        try:
            ensure_schema(_eng(), metadata)
            with _eng().connect() as conn:
                row = conn.execute(_status_select().where(jobs_table.c.id == jid)).mappings().first()
            return _row_to_public_dict(row) if row else None
        except Exception as exc2:
            log.warning("Job status read retry failed: %s", type(exc2).__name__)
            return None


def payload(jid: str) -> bytes | None:
    p = JOB_DIR / f"{jid}.bin"
    if p.exists():
        return p.read_bytes()
    if _use_db():
        try:
            with _eng().connect() as conn:
                row = conn.execute(select(jobs_table.c.payload).where(jobs_table.c.id == jid)).first()
            if row and row[0]:
                return bytes(row[0])
        except Exception as exc:
            log.warning("Job payload read failed: %s", type(exc).__name__)
            try:
                ensure_schema(_eng(), metadata)
                with _eng().connect() as conn:
                    row = conn.execute(select(jobs_table.c.payload).where(jobs_table.c.id == jid)).first()
                if row and row[0]:
                    return bytes(row[0])
            except Exception as exc2:
                log.warning("Job payload read retry failed: %s", type(exc2).__name__)
    return None


def list_recent_downloads(limit: int = 10) -> list[dict]:
    """Finished screen jobs that still have an Excel file stored."""
    out = []
    if _use_db():
        try:
            with _eng().connect() as conn:
                rows = conn.execute(
                    select(jobs_table.c.id, jobs_table.c.filenames, jobs_table.c.message,
                           jobs_table.c.rows, jobs_table.c.updated)
                    .where(jobs_table.c.kind == "screen", jobs_table.c.state == "done",
                           jobs_table.c.has_download == 1)
                    .order_by(jobs_table.c.updated.desc()).limit(limit)).mappings().all()
            for row in rows:
                out.append({"id": row["id"], "filenames": row["filenames"], "message": row["message"],
                            "rows": row["rows"],
                            "updated": row["updated"].isoformat() if row["updated"] else ""})
        except Exception as exc:
            log.warning("list_recent_downloads failed: %s", type(exc).__name__)
    return out


def list_running() -> list[dict]:
    out = []
    with _lock:
        for jid, j in _jobs.items():
            if j.get("state") == "running":
                out.append({k: v for k, v in j.items() if k != "payload"} | {"id": jid})
    if _use_db():
        try:
            with _eng().connect() as conn:
                rows = conn.execute(_status_select().where(jobs_table.c.state == "running")).mappings().all()
            for row in rows:
                if not any(o.get("id") == row["id"] for o in out):
                    out.append(_row_to_public_dict(row))
        except Exception as exc:
            log.warning("list_running failed: %s", type(exc).__name__)
    return out


def mark_interrupted(jid: str, reason: str = "Interrupted before the file was stored. Please upload again.") -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(state="error", error=reason, updated=time.time(), _ts=time.time())
    _update_db(jid, {"state": "error", "error": reason, "stage": "Interrupted"})


def reclaim_orphans_on_boot() -> int:
    """Resume stored running jobs. Jobs with no stored file are marked interrupted."""
    stats = resume_pending()
    n = stats["resumed"] + stats["interrupted"]
    if n:
        log.info("Restart recovery: resumed %d, interrupted %d", stats["resumed"], stats["interrupted"])
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
    out["has_download"] = bool(j.get("has_download") or (jid and (JOB_DIR / f"{jid}.bin").exists()))
    return out
