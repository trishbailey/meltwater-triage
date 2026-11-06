"""The current upload can be downloaded. Past exports are not listed."""
import io


def _xlsx():
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame({
        "Date": ["2026-11-01"],
        "Document ID": ["one"],
        "URL": ["https://x.com/a/status/1"],
        "Hit Sentence": ["I will bomb the AUKUS base at HMAS Stirling tomorrow."],
        "Author Handle": ["@a"],
        "Engagement": [1],
    }).to_excel(buf, index=False)
    return buf.getvalue()


def _app(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REFINE_DISABLED", "1")
    monkeypatch.setenv("HEAT_DISABLED", "1")
    from app import create_app
    return create_app()


def test_dashboard_has_no_export_history(tmp_path, monkeypatch):
    page = _app(tmp_path, monkeypatch).test_client().get("/dashboard").get_data(as_text=True)
    assert "Recent Excel exports" not in page
    assert 'id="recent-downloads"' not in page
    assert "recent-exports.js" not in page
    assert 'id="current-download"' in page
    assert "Download flagged posts" in page


def test_current_job_can_be_downloaded_once(tmp_path, monkeypatch):
    import time
    client = _app(tmp_path, monkeypatch).test_client()
    posted = client.post(
        "/upload",
        data={"files": (io.BytesIO(_xlsx()), "one.xlsx")},
        headers={"X-Upload-Mode": "job"},
        content_type="multipart/form-data",
    )
    assert posted.status_code == 202
    jid = posted.get_json()["job"]
    last = {}
    for _ in range(100):
        last = client.get(f"/jobs/{jid}").get_json()
        if last.get("state") != "running":
            break
        time.sleep(0.1)
    assert last.get("state") == "done" and last.get("has_download"), last
    download = client.get(f"/jobs/{jid}/download")
    assert download.status_code == 200
    assert download.data[:2] == b"PK"
    page = client.get("/dashboard").get_data(as_text=True)
    assert "one.xlsx" not in page
    assert "Recent Excel exports" not in page
