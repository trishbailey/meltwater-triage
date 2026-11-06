"""Alert the analyst to a dangerous threat or a spike in posts. No routine roundups.

Triggers (checked after Claude's filter, on posts it kept):
  1. Clear threat: Claude kept it as a threat AND it names a person or facility AND uses violent
     language with intent (not news, quote, or hypothetical). Sent once per post.
  2. Spike: kept posts in the last ALERT_SPIKE_WINDOW_HOURS (6) are at least ALERT_SPIKE_MULTIPLIER
     (3) times the same-length window average over the prior 7 days AND at least
     ALERT_SPIKE_MIN_POSTS (30). Threats and protests are counted separately; each category
     alerts at most once per window.

Channels (set any or all):
  ALERT_WEBHOOK_URL   an https webhook URL. Microsoft Teams (a Workflows "when a webhook request is
                      received" URL) gets an Adaptive Card; other URLs (Slack, Discord /slack) get
                      {"text": "..."}
  ALERT_EMAIL_TO      comma-separated recipients, with SMTP_HOST, SMTP_PORT (587), SMTP_USER,
                      SMTP_PASSWORD and SMTP_FROM (defaults to SMTP_USER)
The first run records the posts already on the dashboard and sends nothing, so the backlog does not
trigger a flood. After that, each run sends one message for the posts that are new.
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import urllib.request
from urllib.parse import urlsplit
from email.message import EmailMessage

import pandas as pd

from . import unified
from .export import PROTEST_CATS, THREAT_CATS, is_rejected, post_text

log = logging.getLogger(__name__)
SNIPPETS = 5


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or str(v).strip() == ""


def ready_for_review(df: pd.DataFrame) -> pd.DataFrame:
    """Posts Claude kept: reviewed and not rejected."""
    if df is None or df.empty:
        return pd.DataFrame()
    ref = df["refined_category"] if "refined_category" in df.columns else pd.Series("", index=df.index)
    summ = df["narrative_summary"] if "narrative_summary" in df.columns else pd.Series("", index=df.index)
    reviewed = ~(ref.map(_blank) & summ.map(_blank))
    genuine = (~df["is_genuine"].map(is_rejected)) if "is_genuine" in df.columns else True
    return df[reviewed & genuine]


def _key(rec: dict) -> tuple[str, str]:
    kind = str(rec.get("item_type") or "post")
    iid = rec.get("item_id")
    if _blank(iid):
        iid = rec.get("document_id") or rec.get("Document ID") or ""
    return kind, str(iid)


_TEAMS_HOSTS = ("webhook.office.com", "office.com", "logic.azure.com", "powerplatform.com", "powerautomate.com")


def is_teams(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _TEAMS_HOSTS)


def payload_for(url: str, text: str, title: str = "") -> dict:
    """Teams (Workflows and legacy connectors) takes an Adaptive Card; Slack-style hooks take {"text"}."""
    if not is_teams(url):
        return {"text": text}
    body = []
    if title:
        body.append({"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True})
    for line in text.split("\n"):
        if line.strip():
            body.append({"type": "TextBlock", "text": line, "wrap": True})
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                    "version": "1.4", "body": body}}]}


def _post_webhook(url: str, text: str, title: str = "") -> None:
    if not url.lower().startswith("https://"):
        raise ValueError("ALERT_WEBHOOK_URL must be https")
    req = urllib.request.Request(url, data=json.dumps(payload_for(url, text, title)).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def _send_email(env, subject: str, text: str) -> None:
    to = [a.strip() for a in env.get("ALERT_EMAIL_TO", "").split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, ", ".join(to)
    msg["From"] = env.get("SMTP_FROM") or env.get("SMTP_USER", "")
    msg.set_content(text)
    with smtplib.SMTP(env["SMTP_HOST"], int(env.get("SMTP_PORT", "587")), timeout=15) as smtp:
        smtp.starttls()
        if env.get("SMTP_USER"):
            smtp.login(env["SMTP_USER"], env.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def configured(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("ALERT_WEBHOOK_URL") or (env.get("ALERT_EMAIL_TO") and env.get("SMTP_HOST")))



_VIOLENT = re.compile(
    r"\b(?:bomb(?:ed|ing)?|blow(?:ing)? up|assassinat\w*|behead\w*|lynch\w*|sabotag\w*|burn(?:ed|ing)? down|"
    r"set fire to|shoot|shot|shooting|kill(?:ed|ing)?|murder\w*|stab(?:bed|bing)?|execute[d]?|hang(?:ed)?|"
    r"poison\w*|massacre|kidnap\w*|take hostage|hunt down|know where you live|cut the power)\b", re.I)
_INTENT = re.compile(
    r"\b(?:i(?:'ll| will| am going to|'m going to)|we(?:'ll| will| are going to|'re going to)|gonna|"
    r"should be|must be|need(?:s)? to be|someone (?:should|needs to)|let'?s|deserve\w*|time to|"
    r"better watch|will pay)\b|^\W*(?:kill|shoot|bomb|burn|blow up|hang|assassinate|sabotage|murder)\b", re.I)
_FRAMING = re.compile(
    r"\b(?:said|says|according to|reported|reports|alleg\w+|claims?|warned|police|arrested|charged|"
    r"analyst|could|might|would|if|news|quote|headline)\b|\?", re.I)


def _concrete_target(text: str) -> bool:
    from .filters import ABSTRACT_TARGETS
    p = _patterns()
    hits = [m.group(0) for pat in (p.target, p.target_cs) if pat for m in pat.finditer(text)]
    return any(h.lower() not in ABSTRACT_TARGETS for h in hits)


_P = None


def _patterns():
    global _P
    if _P is None:
        from .filters import Patterns
        _P = Patterns()
    return _P


def is_clear_threat(rec: dict) -> bool:
    """Violent language with intent aimed at a named person or facility; not news or hypotheticals."""
    if str(rec.get("category") or "") not in THREAT_CATS:
        return False
    text = " ".join(str(post_text(rec)).split())
    return bool(_VIOLENT.search(text) and _INTENT.search(text) and not _FRAMING.search(text)
                and _concrete_target(text))


def _num(env, name: str, default: float) -> float:
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        return default


def spikes(ready: pd.DataFrame, env, now=None) -> list[dict]:
    """Categories whose recent post count is far above the prior-week average for the same window."""
    if ready is None or ready.empty or "post_date" not in ready.columns:
        return []
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC").tz_localize(None)
    hours = max(1.0, _num(env, "ALERT_SPIKE_WINDOW_HOURS", 6))
    mult = _num(env, "ALERT_SPIKE_MULTIPLIER", 3)
    floor = _num(env, "ALERT_SPIKE_MIN_POSTS", 30)
    base_days = 7
    when = pd.to_datetime(ready["post_date"], errors="coerce", utc=True).dt.tz_localize(None)
    cats = ready["category"].fillna("")
    out = []
    for name, group in (("threat", THREAT_CATS), ("protest", PROTEST_CATS)):
        sel = cats.isin(group)
        recent = int((sel & (when > now - pd.Timedelta(hours=hours)) & (when <= now)).sum())
        prior_mask = sel & (when > now - pd.Timedelta(days=base_days, hours=hours)) & \
            (when <= now - pd.Timedelta(hours=hours))
        avg = int(prior_mask.sum()) / (base_days * 24 / hours)
        if recent >= floor and recent >= mult * max(avg, 1e-9):
            out.append({"category": name, "recent": recent, "average": round(avg, 1), "hours": hours,
                        "bucket": str(now.floor(f"{int(hours)}h"))})
    return out


def _snips(df: pd.DataFrame, n: int = SNIPPETS) -> list[str]:
    lines = []
    for rec in df.head(n).to_dict(orient="records"):
        who = str(rec.get("origin_source") or "") or "post"
        lines.append(f"- [{who}] " + " ".join(str(post_text(rec)).split())[:160])
    return lines


def compose(threats: pd.DataFrame, spike_list: list[dict], ready: pd.DataFrame, base_url: str):
    link = base_url.rstrip("/") + "/dashboard"
    lines, subject = [], ""
    if len(threats):
        subject = "AUKUS monitor: clear threat" + ("s" if len(threats) != 1 else "") + " flagged"
        lines += [subject + ".", "Violent language with intent aimed at a named person or facility.", ""]
        lines += _snips(threats, 3) + [""]
    for sp in spike_list:
        head = (f"Spike: {sp['recent']} {sp['category']} posts in {sp['hours']:g}h "
                f"vs {sp['average']} average")
        subject = subject or "AUKUS monitor: " + head
        lines += [head + ".", ""]
        cats = THREAT_CATS if sp["category"] == "threat" else PROTEST_CATS
        lines += _snips(ready[ready["category"].fillna("").isin(cats)]) + [""]
    return subject, "\n".join(lines + [link])


def announce(storage, env=None, df: pd.DataFrame | None = None, now=None) -> dict:
    """Send an alert only for a clear threat or a spike; everything else waits on the dashboard."""
    env = os.environ if env is None else env
    df = unified.load(storage) if df is None else df
    ready = ready_for_review(df)
    if ready.empty:
        return {"new": 0}
    recs = ready.to_dict(orient="records")
    keys = [_key(r) for r in recs]
    seen = storage.notified_keys()
    danger = [bool(is_clear_threat(r)) for r in recs]
    if not seen:  # first run: record the backlog quietly, but never swallow a clear threat
        storage.mark_notified([k for k, d in zip(keys, danger) if not d])
        seen = storage.notified_keys()
    mask = [bool(d and k[1] and k not in seen) for k, d in zip(keys, danger)]
    threats = ready[mask]
    spike_list = []
    for sp in spikes(ready, env, now):
        if ("spike", f"{sp['category']}:{sp['bucket']}") not in seen:
            spike_list.append(sp)
    if threats.empty and not spike_list:
        return {"new": 0}
    subject, text = compose(threats, spike_list, ready, env.get("RENDER_EXTERNAL_URL") or env.get("APP_URL") or "")
    sent = []
    if env.get("ALERT_WEBHOOK_URL"):
        try:
            _post_webhook(env["ALERT_WEBHOOK_URL"], text, subject)
            sent.append("webhook")
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook alert failed: %s", type(exc).__name__)
    if env.get("ALERT_EMAIL_TO") and env.get("SMTP_HOST"):
        try:
            _send_email(env, subject, text)
            sent.append("email")
        except Exception as exc:  # noqa: BLE001
            log.warning("email alert failed: %s", type(exc).__name__)
    if sent:  # unsent alerts stay pending so the next run retries
        storage.mark_notified([k for k, m in zip(keys, mask) if m]
                              + [("spike", f"{sp['category']}:{sp['bucket']}") for sp in spike_list])
    return {"threats": len(threats), "spikes": [s["category"] for s in spike_list], "sent": sent}
