"""Standing rules the analyst has set. Each one is told to Claude and also applied to existing posts."""
from __future__ import annotations

import re

import pandas as pd

from .export import is_rejected

AUKUS_CONTEXT = re.compile(
    r"aukus|submarine|nuclear|\bhmas\b|pine gap|stirling|henderson|osborne|garden island|defen[cs]e|"
    r"navy|naval|marles|\bsub\b|\bsubs\b|australian government|albanese", re.I)

RULES = [
    {
        "id": "other-causes",
        "reason": "off_topic",
        "pattern": re.compile(r"palestin|\bgaza\b|\bnakba\b|zionis|\bisrael", re.I),
        "unless": AUKUS_CONTEXT,
        "prompt": ("Discard posts that promote rallies or protests for other causes, such as Palestine, "
                   "when the post does not mention AUKUS, submarines, or Australian defence sites. Example "
                   "to discard: \"Join the next Palestine rally, November 15th, 3pm, as part of the "
                   "national day of action against repression.\""),
    },
]


def prompt_text() -> str:
    return "\n".join("- " + r["prompt"] for r in RULES)


def matches(rule: dict, text: str) -> bool:
    return bool(rule["pattern"].search(text or "")) and not rule["unless"].search(text or "")


def apply(df: pd.DataFrame, labels: dict, save) -> dict:
    """Mark unjudged posts that match a rule as not relevant. save(items, reason) persists them."""
    labels = dict(labels)
    if df is None or df.empty:
        return labels
    for rule in RULES:
        items = []
        for rec in df.to_dict(orient="records"):
            if is_rejected(rec.get("is_genuine")):
                continue
            kind = str(rec.get("item_type") or "post")
            iid = rec.get("item_id")
            if iid is None or (isinstance(iid, float) and iid != iid) or str(iid).strip() in ("", "nan"):
                iid = rec.get("document_id") or rec.get("Document ID") or ""
            iid = str(iid)
            text = str(rec.get("Hit Sentence") or rec.get("text") or rec.get("Opening Text") or "")
            if iid and (kind, iid) not in labels and matches(rule, text):
                items.append({"item_type": kind, "item_id": iid, "source": "rule"})
        if items:
            save(items, rule["reason"])
            for it in items:
                labels[(it["item_type"], it["item_id"])] = "not_relevant"
    return labels
