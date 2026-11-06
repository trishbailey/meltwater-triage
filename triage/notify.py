"""Tell the analyst when Claude has passed new posts on for human review.

Channels (set any or all):
  ALERT_WEBHOOK_URL   an https URL that accepts {"text": "..."} (Slack, Teams and Discord-compatible
                      incoming webhooks; Discord needs the /slack suffix)
  ALERT_EMAIL_TO      comma-separated recipients, with SMTP_HOST, SMTP_PORT (587), SMTP_USER,
                      SMTP_PASSWORD and SMTP_FROM (defaults to SMTP_USER)
The first run records the posts already on the dashboard and sends nothing, so the backlog does not
trigger a flood. After that, each run sends one message for the posts that are new.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.request
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


def _post_webhook(url: str, text: str) -> None:
    if not url.lower().startswith("https://"):
        raise ValueError("ALERT_WEBHOOK_URL must be https")
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
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


def compose(new: pd.DataFrame, base_url: str) -> tuple[str, str]:
    cats = new["category"].fillna("") if "category" in new.columns else pd.Series("", index=new.index)
    threats, protests = int(cats.isin(THREAT_CATS).sum()), int(cats.isin(PROTEST_CATS).sum())
    parts = []
    if threats:
        parts.append(f"{threats} threat{'s' if threats != 1 else ''}")
    if protests:
        parts.append(f"{protests} protest{'s' if protests != 1 else ''}")
    subject = "AUKUS monitor: " + (", ".join(parts) or f"{len(new)} posts") + " ready for review"
    lines = [subject + ".", ""]
    top = new[cats.isin(THREAT_CATS)].head(SNIPPETS) if threats else new.head(SNIPPETS)
    for rec in top.to_dict(orient="records"):
        who = str(rec.get("origin_source") or "") or "post"
        lines.append(f"- [{who}] " + " ".join(str(post_text(rec)).split())[:160])
    lines += ["", base_url.rstrip("/") + "/dashboard"]
    return subject, "\n".join(lines)


def announce(storage, env=None, df: pd.DataFrame | None = None) -> dict:
    """Send one message for posts that passed Claude's filter and were not announced before."""
    env = os.environ if env is None else env
    df = unified.load(storage) if df is None else df
    ready = ready_for_review(df)
    if ready.empty:
        return {"new": 0}
    recs = ready.to_dict(orient="records")
    keys = [_key(r) for r in recs]
    seen = storage.notified_keys()
    if not seen:  # first run: record what is already there and stay quiet
        storage.mark_notified(keys)
        return {"seeded": len(keys)}
    mask = [bool(k not in seen and k[1]) for k in keys]
    new = ready[mask]
    if new.empty:
        return {"new": 0}
    subject, text = compose(new, env.get("RENDER_EXTERNAL_URL") or env.get("APP_URL") or "")
    sent = []
    if env.get("ALERT_WEBHOOK_URL"):
        try:
            _post_webhook(env["ALERT_WEBHOOK_URL"], text)
            sent.append("webhook")
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook alert failed: %s", type(exc).__name__)
    if env.get("ALERT_EMAIL_TO") and env.get("SMTP_HOST"):
        try:
            _send_email(env, subject, text)
            sent.append("email")
        except Exception as exc:  # noqa: BLE001
            log.warning("email alert failed: %s", type(exc).__name__)
    if sent:  # keep unsent posts pending so the next run retries
        storage.mark_notified([k for k, m in zip(keys, mask) if m])
    return {"new": len(new), "sent": sent}
