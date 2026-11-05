"""Heated-language analysis over ALL screened posts (not just keyword hits).

Runs only when ANTHROPIC_API_KEY is set (HEAT_DISABLED=1 turns it off).
Cost is bounded by HEAT_MAX_POSTS (default 300): when there are more posts,
a reproducible random sample of that size is scored. Batch size: HEAT_BATCH_SIZE
(default 25). Model: REFINE_MODEL (shared with the review stage).
"""
from __future__ import annotations

import json
import logging
import os

import pandas as pd

from . import refine

log = logging.getLogger(__name__)

HEAT_COLUMNS = ["heat_score", "heat_topic", "heated_phrases"]

HEAT_PROMPT = (
    "You rate the hostility (heat) of social media and news posts about a defense program and "
    "related politics. For each post return JSON {\"results\": [{\"id\": int, \"heat_score\": "
    "integer 0-10 (0 calm/neutral, 5 angry or insulting, 8+ dehumanizing, threatening, or "
    "inciting), \"topic\": string (2-5 words, reuse the same wording for the same topic), "
    "\"heated_phrases\": [short exact quotes from the post, empty if none]}]}. One result per "
    "input id. Use American spelling."
)


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("HEAT_DISABLED") != "1"


def analyze_heat(df: pd.DataFrame, text_cols: list, complete=None, max_posts: int | None = None,
                 batch_size: int | None = None, seed: int = 42) -> pd.DataFrame:
    """Return df with heat columns plus heat_sampled (True where scored)."""
    if complete is None:
        if not is_enabled():
            return df
        complete = refine.anthropic_complete()
    max_posts = max_posts if max_posts is not None else int(os.environ.get("HEAT_MAX_POSTS", "300"))
    batch_size = batch_size or int(os.environ.get("HEAT_BATCH_SIZE", "25"))
    out = df.reset_index(drop=True).copy()
    idx = list(out.index)
    if len(idx) > max_posts:
        idx = sorted(out.sample(n=max_posts, random_state=seed).index)
    results: dict[int, dict] = {}
    for start in range(0, len(idx), batch_size):
        chunk = idx[start:start + batch_size]
        payload = [{"id": int(i), "text": " ".join(str(out.at[i, c]) for c in text_cols
                                                   if pd.notna(out.at[i, c]))[:1500]} for i in chunk]
        try:
            reply = json.loads(complete(HEAT_PROMPT, json.dumps({"posts": payload})))
            for item in reply.get("results", []):
                if isinstance(item, dict) and "id" in item:
                    results[int(item["id"])] = _clean(item)
        except Exception as exc:
            log.warning("Heat batch at %d failed: %s", start, type(exc).__name__)
    sampled = set(idx)
    for col in HEAT_COLUMNS:
        out[col] = [results.get(i, {}).get(col) for i in out.index]
    out["heat_sampled"] = [i in sampled for i in out.index]
    return out


def _clean(item: dict) -> dict:
    try:
        score = max(0, min(10, int(round(float(item.get("heat_score"))))))
    except (TypeError, ValueError):
        score = None
    return {
        "heat_score": score,
        "heat_topic": str(item.get("topic") or "").strip().lower()[:100],
        "heated_phrases": refine._join_list(item.get("heated_phrases")),
    }
