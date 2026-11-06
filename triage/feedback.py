"""User relevance labels for Meltwater posts and early-warning alerts."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine, delete, select)

from .storage import database_url, normalize_database_url, persistence_enabled

LABELS = ("relevant", "not_relevant")
ITEM_TYPES = ("post", "alert")

metadata = MetaData()
labels = Table(
    "relevance_labels", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("item_type", String(16), nullable=False),  # post | alert
    Column("item_id", String(255), nullable=False),
    Column("source", String(64)),
    Column("label", String(16), nullable=False),
    Column("labeled_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("item_type", "item_id", name="uq_relevance_item"),
)

reasons = Table(
    "label_reasons", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("item_type", String(16), nullable=False),
    Column("item_id", String(255), nullable=False),
    Column("reason", String(32), nullable=False),
    Column("note", Text),
    Column("saved_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("item_type", "item_id", name="uq_reason_item"),
)

# One click after a mark tells Claude why. Codes are stored; labels are shown.
NOT_RELEVANT_REASONS = {
    "advocacy": "Advocacy or petition",
    "commentary": "Opinion and commentary",
    "news": "News",
    "past_event": "Past event",
    "off_topic": "Not about AUKUS",
    "joke": "Joke, hyperbole, or irony",
    "outside_australia": "Outside Australia",
    "other": "Other",
}
RELEVANT_REASONS = {"wrong_section": "Relevant, but wrong section"}
ALL_REASONS = {**NOT_RELEVANT_REASONS, **RELEVANT_REASONS}

_engine = None


def _eng():
    global _engine
    if _engine is None:
        _engine = create_engine(normalize_database_url(database_url()), pool_pre_ping=True)
        metadata.create_all(_engine)
    return _engine


def _utc():
    return datetime.now(timezone.utc)


def set_label(item_type: str, item_id: str, label: str, source: str | None = None) -> dict:
    if item_type not in ITEM_TYPES:
        raise ValueError("item_type must be post or alert")
    if label not in LABELS:
        raise ValueError("label must be relevant or not_relevant")
    item_id = str(item_id)[:255]
    source = (source or "")[:64] or None
    if not persistence_enabled():
        raise RuntimeError("storage is not configured")
    eng = _eng()
    now = _utc()
    with eng.begin() as conn:
        old = conn.execute(select(labels).where(labels.c.item_type == item_type,
                                                labels.c.item_id == item_id)).mappings().first()
        if old:
            conn.execute(labels.update().where(labels.c.id == old["id"]).values(
                label=label, source=source or old["source"], labeled_at=now))
        else:
            conn.execute(labels.insert().values(item_type=item_type, item_id=item_id,
                                                source=source, label=label, labeled_at=now))
    return {"item_type": item_type, "item_id": item_id, "label": label, "source": source}


def set_labels(items: list[dict], label: str) -> int:
    """Apply one label to many items (a group of similar posts). Returns how many were saved."""
    if label not in LABELS:
        raise ValueError("label must be relevant or not_relevant")
    clean = []
    for it in items[:500]:
        t = str(it.get("item_type") or "")
        if t not in ITEM_TYPES or not it.get("item_id"):
            raise ValueError("each item needs item_type (post or alert) and item_id")
        clean.append((t, str(it["item_id"])[:255], (str(it.get("source") or "")[:64] or None)))
    if not persistence_enabled():
        raise RuntimeError("storage is not configured")
    now = _utc()
    with _eng().begin() as conn:
        for t, i, src in clean:
            old = conn.execute(select(labels.c.id, labels.c.source).where(
                labels.c.item_type == t, labels.c.item_id == i)).first()
            if old:
                conn.execute(labels.update().where(labels.c.id == old[0]).values(
                    label=label, source=src or old[1], labeled_at=now))
            else:
                conn.execute(labels.insert().values(item_type=t, item_id=i, source=src,
                                                    label=label, labeled_at=now))
    return len(clean)


def all_labels() -> dict[tuple[str, str], str]:
    if not persistence_enabled():
        return {}
    try:
        with _eng().connect() as conn:
            rows = conn.execute(select(labels.c.item_type, labels.c.item_id, labels.c.label)).all()
        return {(t, i): lab for t, i, lab in rows}
    except Exception:
        return {}


def recent_examples(label: str, limit: int = 20) -> list[dict]:
    """Return recent labels of one kind for few-shot prompts (ids only; text joined by caller)."""
    if not persistence_enabled() or label not in LABELS:
        return []
    with _eng().connect() as conn:
        rows = conn.execute(
            select(labels).where(labels.c.label == label)
            .order_by(labels.c.labeled_at.desc()).limit(limit)).mappings().all()
    return [dict(r) for r in rows]


def noisy_terms(limit: int = 15) -> list[dict]:
    """Precision by keyword/source from labels joined lightly via matched text in alert terms.

    Uses label rows' source field; keyword breakdown needs post data from caller.
    """
    if not persistence_enabled():
        return []
    with _eng().connect() as conn:
        rows = conn.execute(select(labels.c.source, labels.c.label)).all()
    bag: dict[str, list[int]] = {}
    for source, label in rows:
        key = source or "unknown"
        rec = bag.setdefault(key, [0, 0])  # relevant, not_relevant
        if label == "relevant":
            rec[0] += 1
        else:
            rec[1] += 1
    out = []
    for source, (rel, bad) in bag.items():
        total = rel + bad
        if total < 2:
            continue
        precision = rel / total if total else 0
        out.append({"term": source, "kind": "source", "relevant": rel, "not_relevant": bad,
                    "total": total, "precision": round(precision, 2)})
    out.sort(key=lambda r: (r["precision"], -r["not_relevant"]))
    return out[:limit]


def set_reasons(items: list[dict], reason: str, note: str | None = None) -> int:
    """Attach one reason (and an optional short note) to many items. An empty reason clears it."""
    if reason and reason not in ALL_REASONS:
        raise ValueError("unknown reason")
    clean = []
    for it in items[:500]:
        t = str(it.get("item_type") or "")
        if t not in ITEM_TYPES or not it.get("item_id"):
            raise ValueError("each item needs item_type (post or alert) and item_id")
        clean.append((t, str(it["item_id"])[:255]))
    if not persistence_enabled():
        raise RuntimeError("storage is not configured")
    note = (note or "").strip()[:300] or None
    now = _utc()
    with _eng().begin() as conn:
        for t, i in clean:
            conn.execute(delete(reasons).where(reasons.c.item_type == t, reasons.c.item_id == i))
            if reason:
                conn.execute(reasons.insert().values(item_type=t, item_id=i, reason=reason, note=note, saved_at=now))
    return len(clean)


def all_reasons() -> dict[tuple[str, str], dict]:
    if not persistence_enabled():
        return {}
    try:
        with _eng().connect() as conn:
            rows = conn.execute(select(reasons.c.item_type, reasons.c.item_id, reasons.c.reason,
                                       reasons.c.note)).all()
        return {(t, i): {"reason": r, "note": n or ""} for t, i, r, n in rows}
    except Exception:
        return {}


def auto_keys() -> dict[tuple[str, str], str]:
    """Items labeled by the system, not clicked: "inherited" (a similar post you judged) or "rule"."""
    if not persistence_enabled():
        return {}
    try:
        with _eng().connect() as conn:
            return {(t, i): src for t, i, src in conn.execute(
                select(labels.c.item_type, labels.c.item_id, labels.c.source).where(
                    labels.c.source.in_(["inherited", "rule"]))).all()}
    except Exception:
        return {}


def reason_counts(label: str = "not_relevant") -> dict[str, int]:
    """How often each reason was given for items currently carrying `label`."""
    labs = all_labels()
    family = NOT_RELEVANT_REASONS if label == "not_relevant" else RELEVANT_REASONS
    out: dict[str, int] = {}
    for key, rec in all_reasons().items():
        if labs.get(key) == label and rec["reason"] in family:
            out[rec["reason"]] = out.get(rec["reason"], 0) + 1
    return out
