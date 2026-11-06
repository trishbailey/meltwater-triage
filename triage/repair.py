"""Deterministic repair of stored labels. Dry-run by default. Never calls a model.

Paid reanalysis is a separate step. This module only prices that step. A reviewed
merge is not approval to spend, and an estimate above $5 is not run from here.
Rows are not deleted. Relevance labels are not read or written.

    python -m triage.repair           # estimate and planned changes, no writes
    python -m triage.repair --apply   # write the rule corrections, still no model call
"""
from __future__ import annotations

import json

from .backfill import SPEND_LIMIT_USD, estimate_refine_cost
from .judgment import display_flags
from .textnorm import storage_dedupe_key


def _cat(value) -> str:
    return (value or "").strip().lower()


def _decision(row: dict) -> dict:
    flags = display_flags(row)
    threat, protest = flags["threat"], flags["protest"]
    if threat and protest:
        cat = "both"
    elif threat:
        cat = "threat"
    elif protest:
        cat = "protest"
    else:
        cat = ""
    raw = flags["raw"]
    tt = _cat(row.get("threat_type"))
    if cat == "" and (raw.get("suppress_threat") or raw.get("suppress_protest") or raw.get("judgment_reason")):
        new_tt = "none" if tt in {"", "none", "call to protest", "unspecified", "unknown"} else (row.get("threat_type") or None)
        genuine = False
        refined = "none"
    elif cat:
        new_tt = row.get("threat_type") or None
        if tt == "call to protest" and not threat:
            new_tt = "none"
        genuine = row.get("is_genuine")
        refined = cat
    else:
        new_tt = row.get("threat_type") or None
        genuine = row.get("is_genuine")
        refined = row.get("refined_category") or None
    return {
        "category": cat or None,
        "refined_category": refined or None,
        "threat_type": new_tt or None,
        "is_genuine": genuine,
        "is_flagged": bool(row.get("is_flagged")) or bool(cat),
        "reason": raw.get("judgment_reason") or "",
        "protest_location": raw.get("protest_location") or row.get("protest_location") or "",
        "event_date": raw.get("event_date") or row.get("event_date") or "",
        "text_present": bool(flags.get("text")),
    }


def _changed(row: dict, decision: dict) -> bool:
    if not decision["text_present"] and not decision["reason"]:
        return False
    if _cat(row.get("category")) != _cat(decision["category"]):
        return True
    if _cat(row.get("refined_category")) != _cat(decision["refined_category"]):
        return True
    if _cat(row.get("threat_type")) != _cat(decision["threat_type"]):
        return True
    return False


def paid_reanalysis_report(n_posts: int) -> dict:
    """Price a model rerun and record that it was not started."""
    est = estimate_refine_cost(n_posts)
    est["will_run"] = False
    est["ran"] = False
    est["approval"] = False
    if est["over_limit"] or est["usd"] > SPEND_LIMIT_USD:
        est["reason"] = (
            f"Estimated cost ${est['usd']:.2f} is above ${SPEND_LIMIT_USD:.0f}. "
            "Paid reanalysis was not run. A reviewed merge does not approve model charges."
        )
    else:
        est["reason"] = (
            "Paid reanalysis was not run. A reviewed merge does not approve model charges. "
            "Obtain an explicit approval and run the backfill estimate as its own step "
            f"before any paid call. The ${SPEND_LIMIT_USD:.0f} ceiling still applies."
        )
    return est


def plan_repair(storage) -> dict:
    """Read posts and report what a free repair would change. Writes nothing."""
    from sqlalchemy import func, select

    from .storage import posts
    reasons: dict[str, int] = {}
    examined = changed = missing_text = duplicates = 0
    seen: dict[tuple, str] = {}
    with storage.engine.connect() as conn:
        rows = conn.execute(select(posts)).mappings().all()
        flagged = conn.execute(
            select(func.count()).select_from(posts).where(posts.c.is_flagged.is_(True))
        ).scalar_one()
    for row in rows:
        examined += 1
        data = dict(row["data"] or {})
        flat = {**data, "document_id": row["document_id"], "category": row["category"],
                "refined_category": row["refined_category"], "threat_type": row["threat_type"],
                "is_genuine": data.get("is_genuine"), "is_flagged": row["is_flagged"],
                "link": row["link"] or data.get("link")}
        if not str(flat.get("Hit Sentence") or flat.get("hit sentence") or flat.get("Opening Text")
                   or flat.get("opening text") or flat.get("text") or flat.get("content") or ""):
            # single_post_text also sees other fields; decision checks the joined body
            pass
        decision = _decision(flat)
        if not decision["text_present"]:
            missing_text += 1
        key = storage_dedupe_key(flat)
        if key:
            if key in seen and seen[key] != row["document_id"] and not data.get("duplicate_of"):
                duplicates += 1
            else:
                seen.setdefault(key, row["document_id"])
        if _changed(flat, decision) or (key and key in seen and seen[key] != row["document_id"]
                                        and not data.get("duplicate_of")):
            changed += 1
            for part in (decision["reason"] or "label_align").split(","):
                if part:
                    reasons[part] = reasons.get(part, 0) + 1
            if key and seen.get(key) not in (None, row["document_id"]):
                reasons["duplicate_representation"] = reasons.get("duplicate_representation", 0) + 1
    return {
        "mode": "dry-run",
        "writes": False,
        "paid_model_called": False,
        "rows_deleted": 0,
        "relevance_labels_touched": 0,
        "posts_examined": examined,
        "posts_would_update": changed,
        "posts_missing_text": missing_text,
        "duplicate_representations": duplicates,
        "reasons": reasons,
        "note": (
            "Corrections use the on-site rules. Stored text is kept. "
            "Rows are not deleted. Relevance labels are not changed. "
            "Posts with no stored text are left as they are."
        ),
        "paid_reanalysis": paid_reanalysis_report(int(flagged or 0)),
    }


def apply_repair(storage) -> dict:
    """Write rule corrections. Does not delete rows, does not call a model."""
    from sqlalchemy import func, select

    from .storage import _jsonable, posts
    report = plan_repair(storage)
    updated = 0
    seen: dict[tuple, str] = {}
    with storage.engine.begin() as conn:
        rows = conn.execute(select(posts)).mappings().all()
        # Stable order so the first copy of a duplicate is the one we keep.
        rows = sorted(rows, key=lambda r: (r["first_run_id"] or 0, r["document_id"] or ""))
        for row in rows:
            data = dict(row["data"] or {})
            flat = {**data, "document_id": row["document_id"], "category": row["category"],
                    "refined_category": row["refined_category"], "threat_type": row["threat_type"],
                    "is_genuine": data.get("is_genuine"), "is_flagged": row["is_flagged"],
                    "link": row["link"] or data.get("link")}
            decision = _decision(flat)
            key = storage_dedupe_key(flat)
            duplicate_of = data.get("duplicate_of") or ""
            if key:
                prior = seen.get(key)
                if prior and prior != row["document_id"]:
                    duplicate_of = prior
                else:
                    seen[key] = row["document_id"]
            if not decision["text_present"] and not duplicate_of:
                continue
            if not _changed(flat, decision) and (data.get("duplicate_of") or "") == (duplicate_of or ""):
                continue
            if "classification_before_repair" not in data:
                data["classification_before_repair"] = {
                    "category": row["category"],
                    "refined_category": row["refined_category"],
                    "threat_type": row["threat_type"],
                    "is_genuine": data.get("is_genuine"),
                    "is_flagged": row["is_flagged"],
                }
            if decision["text_present"]:
                data["category"] = decision["category"] or ""
                data["refined_category"] = decision["refined_category"] or ""
                data["threat_type"] = decision["threat_type"] or ""
                data["is_genuine"] = decision["is_genuine"]
                data["judgment_reason"] = decision["reason"]
                if decision["protest_location"]:
                    data["protest_location"] = decision["protest_location"]
                if decision["event_date"]:
                    data["event_date"] = decision["event_date"]
            if duplicate_of:
                data["duplicate_of"] = duplicate_of
            payload = {k: _jsonable(v) for k, v in data.items()}
            values = {
                "data": payload,
                "category": decision["category"] if decision["text_present"] else row["category"],
                "refined_category": decision["refined_category"] if decision["text_present"] else row["refined_category"],
                "threat_type": decision["threat_type"] if decision["text_present"] else row["threat_type"],
                "is_flagged": True if row["is_flagged"] or decision["category"] else False,
            }
            conn.execute(posts.update().where(posts.c.document_id == row["document_id"]).values(**values))
            updated += 1
        flagged = conn.execute(
            select(func.count()).select_from(posts).where(posts.c.is_flagged.is_(True))
        ).scalar_one()
    report.update(mode="apply", writes=True, posts_updated=updated, paid_model_called=False,
                  rows_deleted=0, relevance_labels_touched=0)
    report["paid_reanalysis"] = paid_reanalysis_report(int(flagged or 0))
    return report


def main(argv=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Dry-run stored-label repair. Does not call a model.")
    parser.add_argument("--apply", action="store_true",
                        help="Write rule corrections. Still does not call a model or delete rows.")
    args = parser.parse_args(argv)
    from .storage import Storage, persistence_enabled
    if not persistence_enabled():
        print(json.dumps({
            "mode": "dry-run", "writes": False, "paid_model_called": False,
            "reason": "No database is configured. Nothing was read or written.",
            "paid_reanalysis": paid_reanalysis_report(0),
        }, indent=2))
        return
    storage = Storage()
    report = apply_repair(storage) if args.apply else plan_repair(storage)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
