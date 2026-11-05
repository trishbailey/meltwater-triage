"""Monthly cap on posts sent to Claude.

ANTHROPIC_MONTHLY_POST_CAP counts each post included in a Claude request
(review and heat each count) during the UTC month. The total is stored in
`anthropic_usage`. Once the cap is reached, further analysis is skipped.
The guard fails closed when the cap is set but usage cannot be recorded.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_INVALID = "ANTHROPIC_MONTHLY_POST_CAP must be a non-negative integer; Claude analysis was skipped."
_NO_DB = (
    "Claude analysis was skipped because ANTHROPIC_MONTHLY_POST_CAP is set but no database "
    "is configured to count posts sent this month. Set DATABASE_URL or PERSIST_RUNS=1. "
    "Keyword screening results are still included."
)
_DB_ERROR = (
    "Claude analysis was skipped because monthly Claude usage could not be recorded. "
    "Keyword screening results are still included."
)


def _parse_cap() -> tuple[int | None, str | None]:
    """Return (cap, error). cap is None when the guard is off."""
    raw = os.environ.get("ANTHROPIC_MONTHLY_POST_CAP")
    if raw is None or not str(raw).strip():
        return None, None
    try:
        cap = int(str(raw).strip())
    except ValueError:
        return 0, _INVALID
    if cap < 0:
        return 0, _INVALID
    return cap, None


def _persistence_on() -> bool:
    return bool(os.environ.get("DATABASE_URL")) or os.environ.get("PERSIST_RUNS") == "1"


class PostBudget:
    """Shared allowance for one upload. `take` reserves posts before they are sent."""

    def __init__(self, storage, cap: int | None, *, invalid: str | None = None,
                 storage_problem: str | None = None, now: datetime | None = None):
        self.storage = storage
        self.cap = cap
        self.invalid = invalid
        self.storage_problem = storage_problem
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self.now = now.astimezone(timezone.utc)
        self.sent = 0
        self.skipped = 0
        self.used: int | None = None
        self.blocked_reason: str | None = storage_problem
        self._logged = False
        self._dead = storage_problem is not None

    @property
    def enforcing(self) -> bool:
        return self.cap is not None or self.invalid is not None or self.storage_problem is not None

    def take(self, n: int) -> int:
        """Reserve up to n posts. Returns how many may be sent."""
        n = int(n)
        if n <= 0:
            return 0
        if not self.enforcing:
            return n
        if self.invalid:
            self.skipped += n
            self.blocked_reason = "invalid"
            return 0
        if self._dead:
            self.skipped += n
            self.blocked_reason = self.storage_problem or self.blocked_reason or "dberror"
            return 0
        if self.cap is not None and self.cap <= 0:
            self.skipped += n
            self.blocked_reason = "cap"
            self._note_once()
            return 0
        if self.storage is None:
            self.skipped += n
            self.blocked_reason = "nodb"
            self.storage_problem = "nodb"
            self._dead = True
            return 0
        try:
            allowed, used = self.storage.reserve_monthly_posts(n, int(self.cap), now=self.now)
        except Exception as exc:
            log.error("Monthly Claude usage update failed: %s", type(exc).__name__)
            self.skipped += n
            self.blocked_reason = "dberror"
            self.storage_problem = "dberror"
            self._dead = True
            return 0
        self.sent += allowed
        self.skipped += n - allowed
        self.used = used
        if allowed < n:
            self.blocked_reason = "cap"
            self._note_once()
        return allowed

    def _note_once(self) -> None:
        if not self._logged:
            log.warning("Claude monthly post cap reached")
            self._logged = True

    def warning(self) -> str | None:
        if self.skipped <= 0:
            return None
        if self.invalid or self.blocked_reason == "invalid":
            return self.invalid or _INVALID
        reason = self.blocked_reason or self.storage_problem
        if reason == "nodb":
            return _NO_DB
        if reason == "dberror":
            return _DB_ERROR
        month = self.now.strftime("%Y-%m")
        used = self.used if self.used is not None else self.sent
        noun = "post" if self.skipped == 1 else "posts"
        return (
            f"Claude analysis was skipped for {self.skipped} {noun} because the monthly limit of "
            f"{self.cap} posts sent to Claude was reached ({used} sent in {month} UTC). "
            "Keyword screening results are still included."
        )


def resolve_budget(storage=None, *, open_storage: bool = True, now: datetime | None = None) -> PostBudget:
    """Build the budget for this process. Opens the database only when the cap is set."""
    cap, invalid = _parse_cap()
    if cap is None and invalid is None:
        return PostBudget(storage, None, now=now)
    if invalid:
        return PostBudget(storage, 0, invalid=invalid, now=now)
    if storage is None and open_storage and _persistence_on():
        try:
            from .storage import Storage
            storage = Storage()
        except Exception as exc:
            log.error("Could not open database for Claude usage cap: %s", type(exc).__name__)
            return PostBudget(None, cap, storage_problem="dberror", now=now)
    if storage is None:
        problem = "dberror" if _persistence_on() else "nodb"
        return PostBudget(None, cap, storage_problem=problem, now=now)
    return PostBudget(storage, cap, now=now)
