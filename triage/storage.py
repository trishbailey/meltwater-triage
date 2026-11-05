"""Persistence for upload runs and posts (for month-over-month dashboards).

Stores every post that was keyword-flagged OR heat-scored, deduped by Meltwater
Document ID. Reporting periods come from the post date (Meltwater "Date":
post_week / post_month); the upload week/month are kept as secondary fields.

DATABASE_URL selects the database (Render "postgres://" URLs are accepted). With
PERSIST_RUNS=1 and no DATABASE_URL, a local SQLite file is used
(STORAGE_SQLITE_PATH, default ./data/triage.db).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone

import pandas as pd
from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, MetaData,
                        String, Table, Text, create_engine, func, select)

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


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return "postgresql://" + url[len("postgres://"):] if url.startswith("postgres://") else url
    path = os.environ.get("STORAGE_SQLITE_PATH", os.path.join("data", "triage.db"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return f"sqlite:///{path}"


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
    v = _lc(row, "document id")
    if v not in (None, ""):
        return str(v).strip().strip('"')[:255]
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


class Storage:
    def __init__(self, url: str | None = None):
        self.engine = create_engine(url or database_url(), future=True, pool_pre_ping=True)
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
