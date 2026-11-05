"""Persistence for upload runs and flagged posts (for month-over-month dashboards).

DATABASE_URL selects the database (Render Postgres URLs starting with postgres:// are
accepted). Without it, a local SQLite file (STORAGE_SQLITE_PATH, default
./data/triage.db) is used. Flagged posts are deduped by Meltwater Document ID; a
re-uploaded post updates its row and keeps its first upload week.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone

import pandas as pd
from sqlalchemy import (JSON, Column, DateTime, Float, ForeignKey, Integer, MetaData, String,
                        Table, Text, create_engine, func, select)

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
)

posts = Table(
    "flagged_posts", metadata,
    Column("document_id", String(255), primary_key=True),
    Column("first_run_id", Integer, ForeignKey("upload_runs.id"), nullable=False),
    Column("last_run_id", Integer, ForeignKey("upload_runs.id"), nullable=False),
    Column("upload_week", String(10), nullable=False),
    Column("upload_month", String(7), nullable=False),
    Column("post_date", DateTime),
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

ID_COLUMNS = ["document id"]


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url.replace("postgres://", "postgresql://", 1) if url.startswith("postgres://") else url
    path = os.environ.get("STORAGE_SQLITE_PATH", os.path.join("data", "triage.db"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return f"sqlite:///{path}"


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _lc(row: dict, name: str):
    for k, v in row.items():
        if str(k).strip().lower() == name:
            return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v
    return None


def _doc_id(row: dict) -> str:
    v = _lc(row, "document id")
    if v not in (None, ""):
        return str(v).strip().strip('"')
    basis = "|".join(str(_lc(row, c) or "") for c in ("url", "author handle", "hit sentence", "date"))
    return "hash:" + hashlib.sha256(basis.encode()).hexdigest()[:32]


def _jsonable(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


class Storage:
    def __init__(self, url: str | None = None):
        self.engine = create_engine(url or database_url(), future=True)
        metadata.create_all(self.engine)

    def save_run(self, flagged: pd.DataFrame, filenames: list[str], rows_screened: int,
                 now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        week, month = iso_week(now.date()), now.strftime("%Y-%m")
        with self.engine.begin() as conn:
            run_id = conn.execute(runs.insert().values(
                created_at=now, upload_week=week, upload_month=month,
                filenames=json.dumps(filenames), rows_screened=rows_screened,
                rows_flagged=len(flagged))).inserted_primary_key[0]
            for rec in flagged.to_dict(orient="records"):
                data = {str(k): _jsonable(v) for k, v in rec.items()}
                doc = _doc_id(data)
                pd_dt = pd.to_datetime(_lc(data, "date"), errors="coerce")
                values = dict(
                    last_run_id=run_id,
                    post_date=None if pd.isna(pd_dt) else pd_dt.to_pydatetime(),
                    category=data.get("category"), refined_category=data.get("refined_category"),
                    severity=data.get("severity"), threat_type=data.get("threat_type"),
                    author_name=_lc(data, "author name"), author_handle=_lc(data, "author handle"),
                    source_name=_lc(data, "source name"), link=data.get("link"),
                    engagement=_lc(data, "engagement"), heat_score=data.get("heat_score"),
                    heat_topic=data.get("heat_topic"), data=data)
                exists = conn.execute(select(posts.c.document_id).where(posts.c.document_id == doc)).first()
                if exists:
                    conn.execute(posts.update().where(posts.c.document_id == doc).values(**values))
                else:
                    conn.execute(posts.insert().values(document_id=doc, first_run_id=run_id,
                                                       upload_week=week, upload_month=month, **values))
        return run_id

    def monthly_counts(self) -> pd.DataFrame:
        """Flagged posts per upload month and category (for month-over-month views)."""
        q = (select(posts.c.upload_month, posts.c.category, func.count().label("posts"),
                    func.sum(posts.c.engagement).label("engagement"))
             .group_by(posts.c.upload_month, posts.c.category)
             .order_by(posts.c.upload_month, posts.c.category))
        with self.engine.connect() as conn:
            return pd.DataFrame(conn.execute(q).all(), columns=["month", "category", "posts", "engagement"])

    def load_posts(self, month: str | None = None) -> pd.DataFrame:
        q = select(posts.c.data, posts.c.upload_week, posts.c.upload_month)
        if month:
            q = q.where(posts.c.upload_month == month)
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return pd.DataFrame([{**r.data, "upload_week": r.upload_week, "upload_month": r.upload_month}
                             for r in rows])
