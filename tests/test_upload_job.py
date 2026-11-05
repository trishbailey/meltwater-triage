import io, time
import pytest

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("JOB_DIR", str(tmp_path / "jobs"))
    from app import create_app
    return create_app().test_client()


def _xlsx_bytes():
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame({
        "Date": ["2026-11-01"] * 5,
        "Document ID": [f"d{i}" for i in range(5)],
        "URL": [f"https://x.com/a/status/{i}" for i in range(5)],
        "Hit Sentence": ["bomb the base now"] * 5,
        "Author Handle": ["@a"] * 5,
        "Engagement": [1] * 5,
    }).to_excel(buf, index=False)
    return buf.getvalue()


def test_upload_job_keeps_page_and_saves(client):
    data = _xlsx_bytes()
    r = client.post("/upload", data={"files": (io.BytesIO(data), "t.xlsx")},
                    headers={"X-Upload-Mode": "job"}, content_type="multipart/form-data")
    assert r.status_code == 202
    body = r.get_json()
    assert body["rows"] >= 5 and body["job"]
    jid = body["job"]
    for _ in range(100):
        j = client.get(f"/jobs/{jid}").get_json()
        if j["state"] != "running":
            break
        time.sleep(0.1)
    assert j["state"] == "done", j
    assert j["has_download"]
    d = client.get(f"/jobs/{jid}/download")
    assert d.status_code == 200
    assert client.get("/dashboard").status_code == 200
