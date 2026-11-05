"""Persistence for upload runs and posts (for month-over-month dashboards).

Stores every post that was keyword-flagged OR heat-scored, deduped by Meltwater
Document ID. Reporting periods come from the post date (Meltwater "Date":
post_week / post_month); the upload week/month are kept as secondary fields.

DATABASE_URL selects the database. Render "postgres://" URLs are rewritten to
"postgresql+psycopg://" so the psycopg v3 driver in requirements.txt is used.
With PERSIST_RUNS=1 and no DATABASE_URL, a local SQLite file is used
(STORAGE_SQLITE_PATH, default ./data/triage.db).

`anthropic_usage` counts posts sent to Claude per UTC month for
ANTHROPIC_MONTHLY_POST_CAP.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone

import pandas as pd
from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, MetaData,
                        String, Table, Text, create_engine, event, func, select)
from sqlalchemy.exc import IntegrityError

metadata = MetaData()

runs = Table(
    "upload_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("upload_week", String(10), nullable=False),   # ISO week, e.g. 2026-W45
    Column("upload_month", String(7), nullable=False),   # e.g. 2026-11
    Column("filenames", Text),
    Column("rows_screened", Integer),
    Column("rows_flagged", Integer),
    Column("rows_heat_scored", Integer),
)

posts = Table(
    "posts", metadata,
    Column("document_id", String(255), primary_key=True),
    Column("first_run_id", Integer, ForeignKey("upload_runs.id"), nullable=False),
    Column("last_run_id", Integer, ForeignKey("upload_runs.id"), nullable=False),
    Column("post_date", DateTime),
    Column("post_week", String(10), index=True),    # from Meltwater Date (primary period)
    Column("post_month", String(7), index=True),
    Column("upload_week", String(10), nullable=False),  # first upload (secondary)
    Column("upload_month", String(7), nullable=False),
    Column("is_flagged", Boolean, nullable=False, default=False),
    Column("category", String(16)),
    Column("refined_category", String(16)),
    Column("severity", String(16)),
    Column("threat_type", String(64)),
    Column("author_name", Text),
    Column("author_handle", Text),
    Column("source_name", Text),
    Column("link", Text),
    Column("engagement", Float),
    Column("heat_score", Float),
    Column("heat_topic", Text),
    Column("data", JSON),  # full row (all Meltwater + added columns)
)

anthropic_usage = Table(
    "anthropic_usage", metadata,
    Column("month", String(7), primary_key=True),  # UTC YYYY-MM
    Column("posts_sent", Integer, nullable=False),
)

_NUMERIC_ID = re.compile(r"-?\d+\.0+")


def normalize_database_url(url: str) -> str:
    """Select the installed psycopg v3 driver. Leave sqlite and explicit drivers alone."""
    url = url.strip()
    lowered = url.lower()
    if lowered.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
        lowered = url.lower()
    if lowered.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return normalize_database_url(url)
    path = os.environ.get("STORAGE_SQLITE_PATH", os.path.join("data", "triage.db"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return f"sqlite:///{path}"


def persistence_enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL")) or os.environ.get("PERSIST_RUNS") == "1"


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _missing(v) -> bool:
    try:
        return v is None or bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _lc(row: dict, name: str):
    for k, v in row.items():
        if str(k).strip().lower() == name:
            return None if _missing(v) else v
    return None


def _doc_id(row: dict) -> str:
    """Stable id. Excel blanks make numeric ids floats (`12345.0`); those match `12345`."""
    v = _lc(row, "document id")
    if isinstance(v, bool):
        text = ""
    elif isinstance(v, float):
        text = str(int(v)) if v.is_integer() else str(v).strip()
    elif isinstance(v, int):
        text = str(v)
    elif v not in (None, ""):
        text = str(v).strip().strip('"').strip()
        if _NUMERIC_ID.fullmatch(text):
            text = text.split(".", 1)[0]
    else:
        text = ""
    if text:
        return text[:255]
    basis = "|".join(str(_lc(row, c) or "") for c in ("url", "author handle", "hit sentence", "date"))
    return "hash:" + hashlib.sha256(basis.encode()).hexdigest()[:32]


def _jsonable(v):
    if _missing(v):
        return None
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


def _num(v):
    try:
        return None if _missing(v) else float(v)
    except (TypeError, ValueError):
        return None


def rows_to_store(all_posts: pd.DataFrame, flagged: pd.DataFrame) -> pd.DataFrame:
    """Flagged rows (with review columns) plus any other heat-scored rows."""
    frames = [flagged.assign(is_flagged=True)]
    if "heat_sampled" in all_posts:
        rest = all_posts[all_posts["heat_sampled"].fillna(False).astype(bool)
                         & all_posts["category"].fillna("").eq("")]
        frames.append(rest.assign(is_flagged=False))
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True, sort=False) if frames else flagged.assign(is_flagged=True)


def _utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


class Storage:
    def __init__(self, url: str | None = None):
        self.engine = create_engine(url or database_url(), future=True, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _busy_timeout(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()
        metadata.create_all(self.engine)

    def save_run(self, rows: pd.DataFrame, filenames: list[str], rows_screened: int,
                 now: datetime | None = None) -> int:
        """Save one upload run; `rows` should come from rows_to_store()."""
        now = now or datetime.now(timezone.utc)
        week, month = iso_week(now.date()), now.strftime("%Y-%m")
        flags = rows["is_flagged"] if "is_flagged" in rows else pd.Series(True, index=rows.index)
        heat_n = int(rows["heat_score"].notna().sum()) if "heat_score" in rows else 0
        with self.engine.begin() as conn:
            run_id = conn.execute(runs.insert().values(
                created_at=now, upload_week=week, upload_month=month,
                filenames=json.dumps(filenames), rows_screened=rows_screened,
                rows_flagged=int(flags.sum()), rows_heat_scored=heat_n)).inserted_primary_key[0]
            for rec in rows.to_dict(orient="records"):
                data = {str(k): _jsonable(v) for k, v in rec.items()}
                doc = _doc_id(data)
                dt = pd.to_datetime(_lc(data, "date"), errors="coerce")
                has_dt = not pd.isna(dt)
                values = dict(
                    last_run_id=run_id,
                    post_date=dt.to_pydatetime() if has_dt else None,
                    post_week=iso_week(dt.date()) if has_dt else None,
                    post_month=dt.strftime("%Y-%m") if has_dt else None,
                    is_flagged=bool(data.get("is_flagged")),
                    category=data.get("category") or None,
                    refined_category=data.get("refined_category") or None,
                    severity=data.get("severity") or None, threat_type=data.get("threat_type") or None,
                    author_name=_lc(data, "author name"), author_handle=_lc(data, "author handle"),
                    source_name=_lc(data, "source name"), link=data.get("link"),
                    engagement=_num(_lc(data, "engagement")), heat_score=_num(data.get("heat_score")),
                    heat_topic=data.get("heat_topic") or None, data=data)
                old = conn.execute(select(posts).where(posts.c.document_id == doc)).mappings().first()
                if old:
                    # Never lose earlier signal: keep flags and prior scores when new ones are blank.
                    values["is_flagged"] = values["is_flagged"] or bool(old["is_flagged"])
                    for k in ("category", "refined_category", "severity", "threat_type",
                              "heat_score", "heat_topic"):
                        if values[k] is None:
                            values[k] = old[k]
                    merged = dict(old["data"] or {})
                    merged.update({k: v for k, v in data.items() if v not in (None, "")})
                    values["data"] = merged
                    conn.execute(posts.update().where(posts.c.document_id == doc).values(**values))
                else:
                    conn.execute(posts.insert().values(document_id=doc, first_run_id=run_id,
                                                       upload_week=week, upload_month=month, **values))
        return run_id

    def posts_sent_this_month(self, now: datetime | None = None) -> int:
        month = _utc(now).strftime("%Y-%m")
        with self.engine.connect() as conn:
            val = conn.execute(
                select(anthropic_usage.c.posts_sent).where(anthropic_usage.c.month == month)
            ).scalar()
        return int(val or 0)

    def reserve_monthly_posts(self, requested: int, cap: int, now: datetime | None = None) -> tuple[int, int]:
        """Atomically reserve up to `requested` posts. Returns (reserved, total_this_month)."""
        requested, cap = int(requested), int(cap)
        if requested <= 0 or cap <= 0:
            return 0, self.posts_sent_this_month(now)
        month = _utc(now).strftime("%Y-%m")
        for _attempt in range(5):
            try:
                with self.engine.begin() as conn:
                    current = conn.execute(
                        select(anthropic_usage.c.posts_sent).where(anthropic_usage.c.month == month)
                    ).scalar()
                    if current is None:
                        take = min(requested, cap)
                        conn.execute(anthropic_usage.insert().values(month=month, posts_sent=take))
                        return take, take
                    current = int(current)
                    take = max(0, min(requested, cap - current))
                    if take == 0:
                        return 0, current
                    result = conn.execute(
                        anthropic_usage.update()
                        .where(anthropic_usage.c.month == month,
                               anthropic_usage.c.posts_sent == current)
                        .values(posts_sent=current + take)
                    )
                    if result.rowcount != 1:
                        continue
                    return take, current + take
            except IntegrityError:
                continue
        raise RuntimeError("Could not record monthly Claude usage")

    def monthly_counts(self) -> pd.DataFrame:
        """Flagged posts per post month and category."""
        q = (select(posts.c.post_month, posts.c.category, func.count().label("posts"),
                    func.sum(posts.c.engagement).label("engagement"))
             .where(posts.c.is_flagged.is_(True))
             .group_by(posts.c.post_month, posts.c.category)
             .order_by(posts.c.post_month, posts.c.category))
        with self.engine.connect() as conn:
            return pd.DataFrame(conn.execute(q).all(), columns=["month", "category", "posts", "engagement"])

    def monthly_heat(self) -> pd.DataFrame:
        """Heat trend: scored posts, average and total heat per post month and topic."""
        q = (select(posts.c.post_month, posts.c.heat_topic, func.count().label("posts"),
                    func.avg(posts.c.heat_score).label("avg_heat"),
                    func.sum(posts.c.heat_score).label("total_heat"),
                    func.sum(posts.c.engagement).label("engagement"))
             .where(posts.c.heat_score.is_not(None))
             .group_by(posts.c.post_month, posts.c.heat_topic)
             .order_by(posts.c.post_month, func.avg(posts.c.heat_score).desc()))
        with self.engine.connect() as conn:
            return pd.DataFrame(conn.execute(q).all(),
                                columns=["month", "topic", "posts", "avg_heat", "total_heat", "engagement"])

    def load_posts(self, month: str | None = None, flagged_only: bool = False) -> pd.DataFrame:
        """Stored posts; `month` filters by post month (Meltwater Date)."""
        q = select(posts.c.data, posts.c.post_week, posts.c.post_month, posts.c.upload_week,
                   posts.c.upload_month, posts.c.is_flagged)
        if month:
            q = q.where(posts.c.post_month == month)
        if flagged_only:
            q = q.where(posts.c.is_flagged.is_(True))
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return pd.DataFrame([{**(r.data or {}), "post_week": r.post_week, "post_month": r.post_month,
                              "upload_week": r.upload_week, "upload_month": r.upload_month,
                              "is_flagged": r.is_flagged} for r in rows])
