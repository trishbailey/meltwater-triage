"""Carry an analyst's judgment to posts that match a pattern the analyst already judged."""
from __future__ import annotations

import pandas as pd

from .export import is_rejected
from .grouping import group_indices


def inherited(df: pd.DataFrame, labels: dict) -> dict[str, list[dict]]:
    """Unlabeled posts that cluster with posts the analyst judged, grouped by the label they inherit.

    A cluster passes its judgment on only when every judged member carries the same label.
    Returns {"relevant": [item, ...], "not_relevant": [...]} with items {item_type, item_id, source}.
    """
    out: dict[str, list[dict]] = {"relevant": [], "not_relevant": []}
    if df is None or df.empty:
        return out
    recs = df.to_dict(orient="records")
    keys = []
    for r in recs:
        kind = str(r.get("item_type") or "post")
        iid = r.get("item_id")
        if iid is None or (isinstance(iid, float) and iid != iid) or str(iid).strip() in ("", "nan"):
            iid = r.get("document_id") or r.get("Document ID") or ""
        keys.append((kind, str(iid)))
    texts = [str(r.get("Hit Sentence") or r.get("text") or r.get("Opening Text") or "") for r in recs]
    links = [str(r.get("link") or r.get("URL") or "") for r in recs]
    for g in group_indices(texts, links):
        if len(g) < 2:
            continue
        judged = {labels.get(keys[i]) for i in g if labels.get(keys[i])}
        if len(judged) != 1:
            continue
        label = judged.pop()
        for i in g:
            if keys[i] not in labels and keys[i][1] and not is_rejected(recs[i].get("is_genuine")):
                out[label].append({"item_type": keys[i][0], "item_id": keys[i][1],
                                   "source": "inherited"})
    return out


def apply(df: pd.DataFrame, labels: dict, save) -> dict:
    """Persist inherited labels with save(items, label) and return the updated label map."""
    labels = dict(labels)
    for label, items in inherited(df, labels).items():
        if items:
            save(items, label)
            for it in items:
                labels[(it["item_type"], it["item_id"])] = label
    return labels
