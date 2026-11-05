import io
import os

import pytest

from triage.seerist import item_key, parse_lines, to_alert

SAMPLE = """Search Result Report - Nov 05, 2026 SEERIST
Showing 50 of 701 results
Nov 05, 2026 · News & Social
1
Plus 2000 US AUKUS staff coming to Rockingham, housing...
Plus 2000 US AUKUS staff coming to Rockingham, housing supplied by our govt.
https://m.facebook.com/cndcymru/posts/
pfbid031msirNz1VN2
Countries: Australia · Sentiment: Negative · Author: CND Cymru
© 2026 Seerist, Inc. www.seerist.com Page 2
Nov 04, 2026 · Events
2
Protest planned outside the shipyard...
Protest planned outside the shipyard on Saturday.
Countries: United Kingdom · Sentiment: Neutral · Author: Wire
"""


def test_parse_lines_fields():
    items = parse_lines(SAMPLE.splitlines())
    assert len(items) == 2
    a, b = items
    assert a["url"] == "https://m.facebook.com/cndcymru/posts/pfbid031msirNz1VN2"
    assert a["title"].startswith("Plus 2000") and a["location"] == "Australia"
    assert a["type"] == "News & Social" and a["date"] == "Nov 05, 2026"
    assert "supplied by our govt" in a["summary"]
    assert b["url"] == "" and item_key(b).startswith("seerist:") and item_key(a) is None
    alert = to_alert(a)
    assert alert["source"] == "seerist" and alert["signal_level"] == "medium"


def test_seerist_upload_dedupes(client_factory=None, tmp_path=None):
    pytest.importorskip("pdfplumber")
    rl = pytest.importorskip("reportlab.pdfgen.canvas")
    buf = io.BytesIO()
    c = rl.Canvas(buf)
    y = 800
    for ln in SAMPLE.replace("·", "-").splitlines():
        c.drawString(20, y, ln)
        y -= 14
    c.save()
    from triage import seerist
    # reportlab's base font has no middle dot; accept a hyphen in this synthetic PDF.
    old = seerist._ITEM_HEAD
    import re
    seerist._ITEM_HEAD = re.compile(r"^([A-Z][a-z]{2} \d{1,2}, \d{4})\s*[·-]\s*(.+)$")
    try:
        items = seerist.parse_pdf(buf.getvalue())
    finally:
        seerist._ITEM_HEAD = old
    assert len(items) == 2




@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("APP_PASSWORD", "pw")
    from app import create_app
    c = create_app().test_client()
    c.post("/login", data={"password": "pw"})
    return c


def _pdf(monkeypatch):
    from triage import seerist
    monkeypatch.setattr(seerist, "pdf_lines", lambda data: SAMPLE.splitlines())
    return b"%PDF-1.4 fake"




def test_seerist_upload_form_and_dedupe(client, monkeypatch):
    data = _pdf(monkeypatch)
    page = client.get("/seerist").get_data(as_text=True)
    assert "Seerist PDF" in page and "progress-form" in page
    r = client.post("/seerist/upload", data={"file": (io.BytesIO(data), "s.pdf")})
    assert "Added 2 items, skipped 0 duplicates." in r.get_data(as_text=True)
    r = client.post("/seerist/upload", data={"file": (io.BytesIO(data), "s.pdf")})
    assert "Added 0 items, skipped 2 duplicates." in r.get_data(as_text=True)
    dash_html = client.get("/dashboard").get_data(as_text=True)
    assert "Seerist" in dash_html and "#ffee00" in dash_html


def test_seerist_job_mode(client, monkeypatch):
    import time
    data = _pdf(monkeypatch)
    r = client.post("/seerist/upload", data={"file": (io.BytesIO(data), "s.pdf")},
                    headers={"X-Upload-Mode": "job"})
    assert r.status_code == 202
    jid = r.get_json()["job"]
    for _ in range(50):
        j = client.get(f"/jobs/{jid}").get_json()
        if j["state"] != "running":
            break
        time.sleep(0.1)
    assert j["state"] == "done" and j["added"] == 2 and j["total"] == 2


def test_rejects_non_pdf(client):
    r = client.post("/seerist/upload", data={"file": (io.BytesIO(b"hello"), "s.pdf")})
    assert r.status_code == 400
