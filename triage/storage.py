"""Persistence for upload runs and posts.

Stores every post the pre-sort flagged as a threat or protest, deduped by Document ID
and by normalized URL. Reporting periods come from the post date (Meltwater "Date":
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
                        String, Table, Text, bindparam, case, create_engine, event, func, select)
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
    Column("url_key", Text, index=True),  # normalized URL; ties a Seerist item to its Meltwater twin
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

alerts = Table(
    "alerts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("source", String(16), nullable=False, index=True),
    Column("url", Text, nullable=False),
    Column("url_key", Text, nullable=False, index=True),  # normalized URL for dedupe
    Column("author", Text),
    Column("text", Text),
    Column("posted_at", DateTime(timezone=True), index=True),
    Column("matched_terms", Text),       # "; "-joined
    Column("signal_level", String(16), nullable=False),
)

alert_reviews = Table(
    "alert_reviews", metadata,
    Column("alert_id", Integer, primary_key=True),
    Column("data", JSON, nullable=False),  # review fields: is_genuine, refined_category, analysis
)

notified_items = Table(
    "notified_items", metadata,
    Column("item_type", String(16), primary_key=True),
    Column("item_id", String(255), primary_key=True),
    Column("notified_at", DateTime(timezone=True), nullable=False),
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
        from .schema import ensure_schema
        ensure_schema(self.engine, metadata)

    @staticmethod
    def _chunks(items: list, size: int = 400):
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def _fetch(self, conn, column, values: list) -> dict:
        """Existing post rows keyed by `column` (document_id or url_key), in a few IN queries."""
        out: dict = {}
        for part in self._chunks(sorted({v for v in values if v})):
            for row in conn.execute(select(posts).where(column.in_(part))).mappings().all():
                out.setdefault(row[column.key], dict(row))
        return out

    def _fetch_all(self, conn, column, values: list) -> dict:
        """Every existing row for each value of `column`: {value: [row, ...]}."""
        out: dict = {}
        for part in self._chunks(sorted({v for v in values if v})):
            for row in conn.execute(select(posts).where(column.in_(part))).mappings().all():
                out.setdefault(row[column.key], []).append(dict(row))
        return out

    def save_run(self, rows: pd.DataFrame, filenames: list[str], rows_screened: int,
                 now: datetime | None = None) -> int:
        """Save one upload run; `rows` should come from rows_to_store().

        Rows are written in bulk: one lookup per few hundred posts and one executemany for
        inserts and for updates. A post that already exists under the same Document ID, or
        under the same normalized URL (a Seerist item and its Meltwater twin), is merged
        into the stored row instead of added again.
        """
        from .alerts import normalize_url
        from .origin import origin_label, publisher_label
        now = now or datetime.now(timezone.utc)
        week, month = iso_week(now.date()), now.strftime("%Y-%m")
        flags = rows["is_flagged"] if "is_flagged" in rows else pd.Series(True, index=rows.index)
        heat_n = int(rows["heat_score"].notna().sum()) if "heat_score" in rows else 0

        prepared: dict[str, dict] = {}
        url_owner: dict[str, str] = {}
        for rec in rows.to_dict(orient="records"):
            data = {str(k): _jsonable(v) for k, v in rec.items()}
            doc = _doc_id(data)
            link = data.get("link") or _lc(data, "url")
            ukey = normalize_url(str(link)) if link else ""
            source = str(data.get("export_source") or "Meltwater")
            owner = url_owner.get(ukey) if ukey else None
            if owner and owner[0] != doc and owner[1] != source:
                doc = owner[0]  # the same post from the other export, seen in this upload
            elif ukey and not owner:
                url_owner[ukey] = (doc, source)
            origin = origin_label(data)
            publisher = publisher_label(data)
            data["origin_source"] = origin
            if publisher:
                data["publisher"] = publisher
            if doc in prepared:
                first = prepared[doc]
                first["data"].update({k: v for k, v in data.items() if v not in (None, "")})
                continue
            dt = pd.to_datetime(_lc(data, "date"), errors="coerce")
            has_dt = not pd.isna(dt)
            prepared[doc] = {"data": data, "ukey": ukey, "origin": origin, "values": dict(
                post_date=dt.to_pydatetime() if has_dt else None,
                post_week=iso_week(dt.date()) if has_dt else None,
                post_month=dt.strftime("%Y-%m") if has_dt else None,
                is_flagged=bool(data.get("is_flagged")),
                category=data.get("category") or None,
                refined_category=data.get("refined_category") or None,
                severity=data.get("severity") or None, threat_type=data.get("threat_type") or None,
                author_name=_lc(data, "author name"), author_handle=_lc(data, "author handle"),
                source_name=origin, link=data.get("link"), url_key=ukey or None,
                engagement=_num(_lc(data, "engagement")), heat_score=_num(data.get("heat_score")),
                heat_topic=data.get("heat_topic") or None)}

        with self.engine.begin() as conn:
            run_id = conn.execute(runs.insert().values(
                created_at=now, upload_week=week, upload_month=month,
                filenames=json.dumps(filenames), rows_screened=rows_screened,
                rows_flagged=int(flags.sum()), rows_heat_scored=heat_n)).inserted_primary_key[0]
            existing = self._fetch(conn, posts.c.document_id, list(prepared))
            by_url = self._fetch_all(conn, posts.c.url_key, [
                p["ukey"] for d, p in prepared.items() if d not in existing and p["ukey"]])
            inserts, updates = [], []
            for doc, item in prepared.items():
                data, values, origin = item["data"], item["values"], item["origin"]
                old = existing.get(doc)
                if old is None:
                    # Same URL stored from the other export (Seerist vs Meltwater): same post.
                    mine = str(data.get("export_source") or "Meltwater")
                    old = next((r for r in by_url.get(item["ukey"], [])
                                if str((r["data"] or {}).get("export_source") or "Meltwater") != mine), None)
                if old is None:
                    inserts.append({"document_id": doc, "first_run_id": run_id, "upload_week": week,
                                    "upload_month": month, "last_run_id": run_id, "data": data, **values})
                    continue
                # Never lose earlier signal: keep flags and prior scores when new ones are blank.
                values["is_flagged"] = values["is_flagged"] or bool(old["is_flagged"])
                for k in ("category", "refined_category", "severity", "threat_type",
                          "heat_score", "heat_topic", "link", "author_name", "author_handle"):
                    if values[k] is None:
                        values[k] = old[k]
                merged = dict(old["data"] or {})
                merged.update({k: v for k, v in data.items() if v not in (None, "")})
                prev_source = (old["source_name"] or "").strip()
                if origin == "Unknown source" and prev_source and "meltwater" not in prev_source.lower():
                    values["source_name"] = prev_source
                    merged["origin_source"] = prev_source
                if old["document_id"] != doc:  # Seerist twin of a stored Meltwater post
                    seen = [x for x in str(merged.get("also_in") or "").split("; ") if x]
                    tag = str(data.get("export_source") or "").strip()
                    if tag and tag not in seen:
                        seen.append(tag)
                    merged["also_in"] = "; ".join(seen)
                values["data"] = merged
                updates.append({"b_doc": old["document_id"], "last_run_id": run_id, **values})
            if inserts:
                conn.execute(posts.insert(), inserts)
            if updates:
                conn.execute(posts.update().where(posts.c.document_id == bindparam("b_doc")), updates)
        return run_id

    def merge_reviews(self, recs: list[dict]) -> int:
        """Merge review fields into existing posts in bulk. Never inserts or deletes."""
        prepared: dict[str, dict] = {}
        for rec in recs:
            data_in = {str(k): _jsonable(v) for k, v in rec.items()}
            prepared.setdefault(_doc_id(data_in), {}).update(
                {k: v for k, v in data_in.items() if v not in (None, "")})
        if not prepared:
            return 0
        with self.engine.begin() as conn:
            existing = self._fetch(conn, posts.c.document_id, list(prepared))
            updates = []
            for doc, data_in in prepared.items():
                old = existing.get(doc)
                if not old:
                    continue
                merged = dict(old["data"] or {})
                merged.update(data_in)
                heat = _num(data_in.get("heat_score"))
                updates.append({
                    "b_doc": doc, "data": merged, "last_run_id": old["last_run_id"],
                    "refined_category": data_in.get("refined_category") or old["refined_category"],
                    "severity": data_in.get("severity") or old["severity"],
                    "threat_type": data_in.get("threat_type") or old["threat_type"],
                    "category": data_in.get("category") or old["category"],
                    "heat_score": heat if heat is not None else old["heat_score"],
                    "heat_topic": data_in.get("heat_topic") or old["heat_topic"]})
            if updates:
                conn.execute(posts.update().where(posts.c.document_id == bindparam("b_doc")), updates)
        return len(updates)

    def merge_review(self, rec: dict) -> bool:
        """Merge review fields into one existing post."""
        return self.merge_reviews([rec]) == 1

    def posts_for_ids(self, ids: list[str]) -> dict[str, dict]:
        """Stored row data for a few document ids (used to show labeled examples to Claude)."""
        out: dict[str, dict] = {}
        ids = [str(i) for i in ids if i]
        if not ids:
            return out
        with self.engine.connect() as conn:
            for part in self._chunks(ids):
                for doc, data in conn.execute(
                        select(posts.c.document_id, posts.c.data).where(posts.c.document_id.in_(part))).all():
                    out[doc] = data if isinstance(data, dict) else {}
        return out

    def backfill_url_keys(self) -> int:
        """Fill url_key on rows stored before the column existed, in one pass."""
        from .alerts import normalize_url
        with self.engine.begin() as conn:
            rows = conn.execute(select(posts.c.document_id, posts.c.link, posts.c.data)
                                .where(posts.c.url_key.is_(None))).all()
            updates = []
            for doc, link, data in rows:
                link = link or _lc(data or {}, "url")
                key = normalize_url(str(link)) if link else ""
                if key:
                    updates.append({"b_doc": doc, "url_key": key})
            if updates:
                conn.execute(posts.update().where(posts.c.document_id == bindparam("b_doc"))
                             .values(url_key=bindparam("url_key")), updates)
        return len(updates)

    def reuse_index(self, text_cols: list | None = None) -> "ReuseIndex":
        """Load prior classifications for skipping Claude on re-uploads and retweets."""
        from .reuse import ReuseIndex, _payload_from_stored, _url_key, text_fingerprint, _row_text
        index = ReuseIndex()
        text_cols = text_cols or []
        with self.engine.connect() as conn:
            rows = conn.execute(select(
                posts.c.document_id, posts.c.category, posts.c.refined_category,
                posts.c.severity, posts.c.threat_type, posts.c.heat_score, posts.c.heat_topic,
                posts.c.link, posts.c.data).where(posts.c.is_flagged.is_(True))).all()
        for doc, cat, refined, sev, tt, heat, topic, link, data in rows:
            payload = _payload_from_stored(doc, cat, refined, sev, tt, heat, topic, data)
            blob = dict(data) if isinstance(data, dict) else {}
            if link:
                payload.setdefault("link", link)
                blob.setdefault("link", link)
                blob.setdefault("url", link)
            payload["_url_key"] = _url_key({**blob, **payload})
            merged = {**blob, **payload}
            if text_cols:
                fp = text_fingerprint(_row_text(merged, text_cols))
            else:
                fp = text_fingerprint(str(
                    blob.get("Hit Sentence") or blob.get("hit sentence")
                    or blob.get("Content") or blob.get("content") or ""))
            payload["_fp"] = fp
            index.add(payload)
        return index

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

    def load_posts(self, month: str | None = None, flagged_only: bool = False,
                   since: datetime | None = None) -> pd.DataFrame:
        """Stored posts; `month` filters by post month (Meltwater Date), `since` by post date."""
        q = select(posts.c.document_id, posts.c.data, posts.c.post_date, posts.c.post_week,
                   posts.c.post_month, posts.c.upload_week, posts.c.upload_month, posts.c.is_flagged)
        if month:
            q = q.where(posts.c.post_month == month)
        if flagged_only:
            q = q.where(posts.c.is_flagged.is_(True))
        if since is not None:
            q = q.where(posts.c.post_date >= since.replace(tzinfo=None))
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return pd.DataFrame([{**(r.data or {}), "document_id": r.document_id, "post_date": r.post_date,
                              "post_week": r.post_week, "post_month": r.post_month,
                              "upload_week": r.upload_week, "upload_month": r.upload_month,
                              "is_flagged": r.is_flagged} for r in rows])

    # --- status and alerts -------------------------------------------------
    def status_summary(self, now: datetime | None = None) -> dict:
        """Row counts by post month and flag type, plus this month's Claude usage."""
        flag = case((posts.c.is_flagged.is_(True), func.coalesce(posts.c.category, "flagged")),
                    else_="heat only")
        q = (select(posts.c.post_month, flag.label("kind"), func.count().label("rows"))
             .group_by(posts.c.post_month, flag).order_by(posts.c.post_month.desc(), flag))
        with self.engine.connect() as conn:
            rows = [{"month": r.post_month or "no date", "kind": r.kind, "rows": r.rows}
                    for r in conn.execute(q).all()]
            n_runs = conn.execute(select(func.count()).select_from(runs)).scalar_one()
            last_run = conn.execute(select(func.max(runs.c.created_at))).scalar_one()
            n_alerts = conn.execute(select(func.count()).select_from(alerts)).scalar_one()
        return {"rows": rows, "runs": n_runs, "last_run": last_run, "alerts": n_alerts,
                "claude_month": _utc(now).strftime("%Y-%m"),
                "claude_posts": self.posts_sent_this_month(now)}

    def add_alert(self, alert: dict, now: datetime | None = None,
                  url_key: str | None = None) -> tuple[int, bool]:
        """Insert an alert; returns (id, created). A repeat of the same normalized URL keeps
        the first-seen row (and its received_at). url_key overrides the key for items with
        no URL (Seerist items use a hash of title plus date)."""
        from .alerts import normalize_url
        alert = {**alert, "url_key": url_key or normalize_url(alert["url"])}
        with self.engine.begin() as conn:
            old = conn.execute(select(alerts.c.id).where(alerts.c.url_key == alert["url_key"])).first()
            if old:
                return old[0], False
            new_id = conn.execute(alerts.insert().values(received_at=_utc(now), **alert)).inserted_primary_key[0]
            return new_id, True

    def load_alerts(self, limit: int = 200) -> pd.DataFrame:
        q = (select(alerts).order_by(func.coalesce(alerts.c.posted_at, alerts.c.received_at).desc())
             .limit(limit))
        with self.engine.connect() as conn:
            return pd.DataFrame([dict(r) for r in conn.execute(q).mappings().all()],
                                columns=[c.name for c in alerts.columns])

    def load_alert_reviews(self) -> dict[int, dict]:
        with self.engine.connect() as conn:
            return {r[0]: (r[1] if isinstance(r[1], dict) else {})
                    for r in conn.execute(select(alert_reviews.c.alert_id, alert_reviews.c.data)).all()}

    def merge_alert_reviews(self, reviews: dict[int, dict]) -> int:
        """Merge review fields into the stored review of each alert (created when missing)."""
        n = 0
        with self.engine.begin() as conn:
            for aid, rec in reviews.items():
                new = {str(k): _jsonable(v) for k, v in rec.items() if v not in (None, "")}
                if not new:
                    continue
                old = conn.execute(select(alert_reviews.c.data).where(alert_reviews.c.alert_id == aid)).first()
                if old:
                    merged = dict(old[0] or {})
                    merged.update(new)
                    conn.execute(alert_reviews.update().where(alert_reviews.c.alert_id == aid).values(data=merged))
                else:
                    conn.execute(alert_reviews.insert().values(alert_id=aid, data=new))
                n += 1
        return n

    def notified_keys(self) -> set[tuple[str, str]]:
        with self.engine.connect() as conn:
            return {(r[0], r[1]) for r in conn.execute(
                select(notified_items.c.item_type, notified_items.c.item_id)).all()}

    def mark_notified(self, keys, now: datetime | None = None) -> int:
        keys = list(keys)
        if not keys:
            return 0
        when = _utc(now)
        with self.engine.begin() as conn:
            have = {(r[0], r[1]) for r in conn.execute(
                select(notified_items.c.item_type, notified_items.c.item_id)).all()}
            fresh = [{"item_type": t, "item_id": i, "notified_at": when} for t, i in keys if (t, i) not in have]
            if fresh:
                conn.execute(notified_items.insert(), fresh)
        return len(fresh)

    def meltwater_first_seen(self) -> dict:
        """Normalized post URL -> when the post first arrived via a Meltwater upload."""
        from .alerts import normalize_url
        q = select(posts.c.link, posts.c.data, runs.c.created_at).join(runs, runs.c.id == posts.c.first_run_id)
        out: dict = {}
        with self.engine.connect() as conn:
            for link, data, created in conn.execute(q).all():
                link = link or _lc(data or {}, "url")
                if link:
                    k = normalize_url(str(link))
                    if k and (k not in out or created < out[k]):
                        out[k] = created
        return out

    def delete_test_posts(self) -> int:
        """Remove synthetic load-test posts whose Document ID ends with -<digits>."""
        import re
        with self.engine.begin() as conn:
            ids = [r[0] for r in conn.execute(select(posts.c.document_id)).all()]
            doomed = [i for i in ids if re.search(r"-\d+$", str(i) or "")]
            if not doomed:
                return 0
            conn.execute(posts.delete().where(posts.c.document_id.in_(doomed)))
            return len(doomed)

    def delete_alert(self, alert_id: int) -> bool:
        with self.engine.begin() as conn:
            conn.execute(alert_reviews.delete().where(alert_reviews.c.alert_id == alert_id))
            return conn.execute(alerts.delete().where(alerts.c.id == alert_id)).rowcount > 0
