"""Optional second-pass review of flagged posts with the Anthropic API.

Enabled only when ANTHROPIC_API_KEY is set (and REFINE_DISABLED is not "1").
The key is read from the environment and is never logged or printed.
Model: REFINE_MODEL (default claude-haiku-4-5). Batch size: REFINE_BATCH_SIZE (default 40).
Output limit: ANTHROPIC_MAX_TOKENS (default 8192). Timeout: ANTHROPIC_TIMEOUT_SECONDS
(default 120). ANTHROPIC_MAX_RETRIES defaults to 0 so one batch is one request.
ANTHROPIC_MONTHLY_POST_CAP (see triage.budget) can skip this stage.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
REFINE_COLUMNS = [
    "is_genuine", "refined_category", "narrative_summary", "theme", "severity",
    "threat_type", "targets", "named_individuals",
    "protest_location", "protest_topic", "organizers_or_groups", "event_date", "scale_or_turnout",
]
THREAT_TYPES = {
    "violence", "death threat", "bombing or arson", "sabotage", "doxxing", "harassment",
    "call to protest", "call to disruption", "other", "none",
}
VALID_CATEGORIES = {"threat", "protest", "both", "none"}
VALID_SEVERITY = {"low", "medium", "high", "critical"}

SYSTEM_PROMPT = (
    "You review social media and news posts that a keyword filter flagged as possible threats "
    "(violence, sabotage, intimidation against a defense program, facilities, or people) or "
    "protest mobilization (calls to rally, blockade, or take direct action). For each post, decide "
    "whether the flag is genuine. Return JSON: {\"results\": [{\"id\": int, \"is_genuine\": bool, "
    "\"refined_category\": \"threat\"|\"protest\"|\"both\"|\"none\", \"narrative_summary\": "
    "string (max 30 words), \"theme\": string (2-5 words), \"severity\": "
    "\"low\"|\"medium\"|\"high\"|\"critical\", \"threat_type\": one of \"violence\"|"
    "\"death threat\"|\"bombing or arson\"|\"sabotage\"|\"doxxing\"|\"harassment\"|"
    "\"call to protest\"|\"call to disruption\"|\"other\"|\"none\", \"targets\": [strings: "
    "people, groups, places, or institutions the post targets or mentions as targets], "
    "\"named_individuals\": [strings: named politicians and other named people], "
    "\"protest_location\": string (city, region, or site; empty if none), \"protest_topic\": "
    "string (2-6 words; empty if not a protest), \"organizers_or_groups\": [strings: who is "
    "behind the protest], \"event_date\": string (only if stated, else empty), "
    "\"scale_or_turnout\": string (only if stated, else empty)}]}. "
    "One result per input id. Use American spelling."
)

# A completion function takes (system, user) and returns the JSON string reply.
CompleteFn = Callable[[str, str], str]


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("REFINE_DISABLED") != "1"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return min(hi, max(lo, value))


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def anthropic_complete(max_tokens: int | None = None) -> CompleteFn:
    import anthropic  # imported lazily so the dependency is optional

    # Reads ANTHROPIC_API_KEY from the environment. Retries default to 0 so the
    # monthly post cap counts each request once.
    client = anthropic.Anthropic(
        timeout=_env_float("ANTHROPIC_TIMEOUT_SECONDS", 120.0),
        max_retries=_env_int("ANTHROPIC_MAX_RETRIES", 0, 0, 5),
    )
    model = os.environ.get("REFINE_MODEL", DEFAULT_MODEL)
    limit = max_tokens if max_tokens is not None else _env_int("ANTHROPIC_MAX_TOKENS", 8192, 256, 64000)

    def complete(system: str, user: str) -> str:
        resp = client.messages.create(
            model=model,
            max_tokens=limit,
            system=system + " Reply with the JSON object only, no other text.",
            messages=[{"role": "user", "content": user}],
        )
        if getattr(resp, "stop_reason", None) == "max_tokens":
            log.warning("Claude response hit the output token limit")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        start, end = text.find("{"), text.rfind("}")
        return text[start:end + 1] if start != -1 else "{}"

    return complete


def concurrency() -> int:
    """Parallel Claude requests per stage (ANTHROPIC_CONCURRENCY, default 8, max 12)."""
    return _env_int("ANTHROPIC_CONCURRENCY", 8, 1, 12)


def run_concurrently(fn, jobs: list) -> list:
    """Run fn over jobs with a small thread pool; results keep job order."""
    if len(jobs) <= 1 or concurrency() == 1:
        return [fn(j) for j in jobs]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(concurrency(), len(jobs))) as pool:
        return list(pool.map(fn, jobs))


def _post_payload(i: int, row: pd.Series, text_cols: list) -> dict:
    text = " ".join(str(row[c]) for c in text_cols if pd.notna(row[c]))
    return {"id": i, "text": text[:1200],
            "author": " ".join(str(row[c]) for c in row.index
                               if str(c).lower() in ("author name", "author handle") and pd.notna(row[c])), "keyword_category": row.get("category", ""),
            "matched_keywords": row.get("matched_keywords", "")}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
    return None


def _clean(item: dict) -> dict:
    cat = str(item.get("refined_category", "")).lower()
    sev = str(item.get("severity", "")).lower()
    tt = str(item.get("threat_type", "")).strip().lower()
    return {
        "is_genuine": _as_bool(item.get("is_genuine")) if "is_genuine" in item else None,
        "refined_category": cat if cat in VALID_CATEGORIES else "",
        "narrative_summary": str(item.get("narrative_summary", ""))[:500],
        "theme": str(item.get("theme", ""))[:100],
        "severity": sev if sev in VALID_SEVERITY else "",
        "threat_type": tt if tt in THREAT_TYPES else ("other" if tt else ""),
        "targets": _join_list(item.get("targets")),
        "named_individuals": _join_list(item.get("named_individuals")),
        "protest_location": _text(item.get("protest_location"), 200),
        "protest_topic": _text(item.get("protest_topic"), 100),
        "organizers_or_groups": _join_list(item.get("organizers_or_groups")),
        "event_date": _text(item.get("event_date"), 100),
        "scale_or_turnout": _text(item.get("scale_or_turnout"), 100),
    }


def _text(value, limit: int) -> str:
    return "" if value is None else str(value).strip()[:limit]


def _join_list(value) -> str:
    """Store lists as "; "-joined text (split on "; " for dashboard aggregation)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ""
    seen = []
    for v in value:
        v = str(v).strip()
        if v and v.lower() not in {s.lower() for s in seen}:
            seen.append(v)
    return "; ".join(seen)[:500]


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _absorb(results: dict, reply_text: str, clean) -> None:
    reply = json.loads(reply_text)
    if not isinstance(reply, dict):
        return
    for item in reply.get("results", []):
        if not isinstance(item, dict) or "id" not in item:
            continue
        try:
            results[int(item["id"])] = clean(item)
        except (TypeError, ValueError, OverflowError):
            continue


def refine(flagged: pd.DataFrame, text_cols: list, complete: CompleteFn | None = None,
           batch_size: int | None = None, budget=None, progress=None) -> pd.DataFrame:
    """Add REFINE_COLUMNS to `flagged`. Returns input unchanged if disabled."""
    if complete is None:
        if not is_enabled():
            return flagged
        complete = anthropic_complete()
    if budget is None:
        from .budget import resolve_budget
        budget = resolve_budget()
    if flagged.empty:
        return flagged.assign(**{c: pd.Series(dtype=object) for c in REFINE_COLUMNS})
    batch_size = _positive_int(
        batch_size if batch_size is not None else os.environ.get("REFINE_BATCH_SIZE", "40"), 40)
    out = flagged.reset_index(drop=True).copy()
    results: dict[int, dict] = {}
    ids = list(range(len(out)))
    jobs = []
    for start in range(0, len(ids), batch_size):
        chunk_ids = ids[start:start + batch_size]
        allowed = budget.take(len(chunk_ids))  # reserved serially so the caps hold
        if allowed <= 0:
            break
        chunk_ids = chunk_ids[:allowed]
        chunk = out.iloc[chunk_ids]
        payload = [_post_payload(int(i), r, text_cols) for i, r in chunk.iterrows()]
        jobs.append((start, len(chunk_ids), json.dumps({"posts": payload})))

    def _run(job):
        start, n, body = job
        try:
            return complete(SYSTEM_PROMPT, body)
        except Exception as exc:  # keep keyword results if the review fails
            log.warning("Refinement batch at row %d failed: %s", start, type(exc).__name__)
            return None
        finally:
            if progress:
                progress(n)
    for reply in run_concurrently(_run, jobs):
        if reply is not None:
            _absorb(results, reply, _clean)
    for col in REFINE_COLUMNS:
        out[col] = [results.get(i, {}).get(col) for i in range(len(out))]
    return out
