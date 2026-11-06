"""Plan a review backfill for flagged posts that were stored without a review.

This module prices the work and can run it only after an explicit approval.
Nothing here is called on startup, and the default command prints the estimate
and does not contact the API.

List prices below are Claude Haiku 4.5 standard Messages API rates (USD per
million tokens): $1 input, $5 output. Batch API is half of that. Source:
https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-06).
The quote uses the higher standard rate and does not assume prompt caching.
Token counts are a planning shape (system prompt once per batch, a short post
excerpt in, a short JSON result out), not a measured bill.
"""
from __future__ import annotations

import math
import os

HAIKU_INPUT_USD_PER_MTOK = 1.0
HAIKU_OUTPUT_USD_PER_MTOK = 5.0
SYSTEM_TOKENS = 700
PER_POST_INPUT_TOKENS = 220
PER_POST_OUTPUT_TOKENS = 160
DEFAULT_BATCH = 40
# Do not spend more than this without an explicit higher cap.
SPEND_LIMIT_USD = 5.0
APPROVAL_ENV = "BACKFILL_APPROVED"
APPROVAL_VALUE = "yes-i-approve-paid-backfill"


def estimate_refine_cost(n_posts: int, batch_size: int = DEFAULT_BATCH) -> dict:
    n = max(0, int(n_posts))
    batch = max(1, int(batch_size))
    batches = math.ceil(n / batch) if n else 0
    input_tokens = batches * SYSTEM_TOKENS + n * PER_POST_INPUT_TOKENS
    output_tokens = n * PER_POST_OUTPUT_TOKENS
    usd = (input_tokens / 1_000_000) * HAIKU_INPUT_USD_PER_MTOK
    usd += (output_tokens / 1_000_000) * HAIKU_OUTPUT_USD_PER_MTOK
    return {
        "posts": n,
        "batches": batches,
        "batch_size": batch,
        "model": os.environ.get("REFINE_MODEL", "claude-haiku-4-5"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_usd_per_mtok": HAIKU_INPUT_USD_PER_MTOK,
        "output_usd_per_mtok": HAIKU_OUTPUT_USD_PER_MTOK,
        "usd": round(usd, 4),
        "spend_limit_usd": SPEND_LIMIT_USD,
        "over_limit": usd > SPEND_LIMIT_USD,
        "pricing": "standard Messages API (batch API would be about half)",
    }


def count_flagged_without_review(storage) -> int:
    """Flagged rows whose stored review category is blank. Rows are not modified."""
    from sqlalchemy import func, or_, select

    from .storage import posts
    q = select(func.count()).select_from(posts).where(
        posts.c.is_flagged.is_(True),
        or_(posts.c.refined_category.is_(None), posts.c.refined_category == ""),
    )
    with storage.engine.connect() as conn:
        return int(conn.execute(q).scalar_one())


def plan(storage=None) -> dict:
    """Estimate only. Does not call the API and does not write posts."""
    if storage is None:
        examples = {str(n): estimate_refine_cost(n)["usd"] for n in (1000, 4000, 10000)}
        out = estimate_refine_cost(0)
        out.update(live_count=None, examples_usd=examples, will_run=False,
                   reason="No database was opened. Counts above are illustrative.")
        return out
    n = count_flagged_without_review(storage)
    out = estimate_refine_cost(n)
    out["live_count"] = n
    out["will_run"] = False
    if out["over_limit"]:
        out["reason"] = (
            f"Estimated cost ${out['usd']:.2f} is above ${SPEND_LIMIT_USD:.0f}. "
            "Paid backfill was not run."
        )
    else:
        out["reason"] = "Paid backfill was not run. This report is an estimate only."
    return out


def execute(storage, *, approve: bool = False, max_usd: float = SPEND_LIMIT_USD) -> dict:
    """Review unreviewed flagged posts. Refuses unless approve is set and the estimate fits."""
    report = plan(storage)
    if not approve or os.environ.get(APPROVAL_ENV) != APPROVAL_VALUE:
        report["ran"] = False
        report["reason"] = "Refusing paid backfill without explicit approval."
        return report
    if report["usd"] > max_usd or report["over_limit"]:
        report["ran"] = False
        report["reason"] = (
            f"Refusing paid backfill: estimate ${report['usd']:.2f} exceeds ${max_usd:.2f}."
        )
        return report
    if report["posts"] == 0:
        report["ran"] = False
        report["reason"] = "No flagged posts are missing a review."
        return report
    from .pipeline import backfill_flagged
    done = backfill_flagged(storage)
    report["ran"] = True
    report["reviewed"] = done
    report["reason"] = f"Reviewed {done} flagged posts."
    return report


def main() -> None:
    import json
    from .storage import Storage, persistence_enabled
    storage = Storage() if persistence_enabled() else None
    print(json.dumps(plan(storage), indent=2))


if __name__ == "__main__":
    main()
