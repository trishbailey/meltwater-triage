"""Heated-language analysis over ALL screened posts (not just keyword hits).

Runs only when ANTHROPIC_API_KEY is set (HEAT_DISABLED=1 turns it off).
Cost is bounded by HEAT_MAX_POSTS (default 300): when there are more posts,
the highest-engagement posts are scored first (ties keep file order). Batch size: HEAT_BATCH_SIZE
(default 25). Model: REFINE_MODEL (shared with the review stage).
"""
from __future__ import annotations

import json
import logging
import os

import pandas as pd

from . import keywords as kw
from . import refine

log = logging.getLogger(__name__)

HEAT_COLUMNS = ["heat_score", "heat_topic", "heat_subtopic", "heated_phrases"]

_HEAT_PROMPT_TEMPLATE = (
    "You rate the hostility (heat) of social media and news posts about a defense program and "
    "related politics. For each post return JSON {\"results\": [{\"id\": int, \"heat_score\": "
    "integer 0-10 (0 calm/neutral, 5 angry or insulting, 8+ dehumanizing, threatening, or "
    "inciting), \"topic\": exactly one of {topics}, \"subtopic\": string (2-6 words, free text), "
    "\"heated_phrases\": [short exact quotes from the post, empty if none]}]}. One result per "
    "input id. Use American spelling."
)


def heat_prompt() -> str:
    return _HEAT_PROMPT_TEMPLATE.replace("{topics}", json.dumps(kw.HEAT_TOPICS))


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("HEAT_DISABLED") != "1"


def _bounded_int(value, default: int, *, positive: bool) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if positive:
        return parsed if parsed > 0 else default
    return parsed if parsed >= 0 else default


def analyze_heat(df: pd.DataFrame, text_cols: list, complete=None, max_posts: int | None = None,
                 batch_size: int | None = None, budget=None, progress=None) -> pd.DataFrame:
    """Return df with heat columns plus heat_sampled (True where a request was sent)."""
    if complete is None:
        if not is_enabled():
            return df
        complete = refine.anthropic_complete()
    if budget is None:
        from .budget import resolve_budget
        budget = resolve_budget()
    max_posts = _bounded_int(
        max_posts if max_posts is not None else os.environ.get("HEAT_MAX_POSTS", "300"), 300,
        positive=False)
    batch_size = _bounded_int(
        batch_size if batch_size is not None else os.environ.get("HEAT_BATCH_SIZE", "40"), 40,
        positive=True)
    out = df.reset_index(drop=True).copy()
    eng_col = next((c for c in out.columns if str(c).strip().lower() == "engagement"), None)
    eng = (pd.to_numeric(out[eng_col], errors="coerce").fillna(0) if eng_col
           else pd.Series(0, index=out.index))
    ranked = list(eng.sort_values(ascending=False, kind="stable").index)
    idx = ranked[:max_posts]
    # Over the per-upload cap, or under the monthly cap, send the hottest posts first.
    # Under both caps, keep file order so batch grouping stays stable.
    if len(ranked) <= max_posts and not getattr(budget, "enforcing", False):
        idx = list(out.index)
    results: dict[int, dict] = {}
    sent: list[int] = []
    jobs = []
    for start in range(0, len(idx), batch_size):
        chunk = idx[start:start + batch_size]
        allowed = budget.take(len(chunk))  # reserved serially so the caps hold
        if allowed <= 0:
            break
        chunk = chunk[:allowed]
        sent.extend(chunk)
        payload = [{"id": int(i), "text": " ".join(str(out.at[i, c]) for c in text_cols
                                                   if pd.notna(out.at[i, c]))[:1000]} for i in chunk]
        jobs.append((start, len(chunk), json.dumps({"posts": payload})))

    def _run(job):
        start, n, body = job
        try:
            return complete(heat_prompt(), body)
        except Exception as exc:
            log.warning("Heat batch at %d failed: %s", start, type(exc).__name__)
            return None
        finally:
            if progress:
                progress(n)
    for reply in refine.run_concurrently(_run, jobs):
        if reply is not None:
            refine._absorb(results, reply, _clean)
    sampled = set(sent)
    for col in HEAT_COLUMNS:
        out[col] = [results.get(i, {}).get(col) for i in out.index]
    out["heat_sampled"] = [i in sampled for i in out.index]
    return out


def _topic(value) -> str:
    """Map to a fixed HEAT_TOPICS entry (case-insensitive); anything else becomes "other"."""
    v = str(value or "").strip().lower()
    if not v:
        return ""
    lookup = {t.lower(): t for t in kw.HEAT_TOPICS}
    return lookup.get(v, "other")


def _clean(item: dict) -> dict:
    try:
        score = max(0, min(10, int(round(float(item.get("heat_score"))))))
    except (TypeError, ValueError):
        score = None
    return {
        "heat_score": score,
        "heat_topic": _topic(item.get("topic")),
        "heat_subtopic": str(item.get("subtopic") or "").strip().lower()[:100],
        "heated_phrases": refine._join_list(item.get("heated_phrases")),
    }
