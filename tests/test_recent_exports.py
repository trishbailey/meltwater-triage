"""Recent Excel exports update when a job finishes, without a page reload."""
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "recent-exports.js"
BROWSER = ROOT / "tests" / "browser_recent_exports.js"


def _xlsx(name_hint="export"):
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame({
        "Date": ["2026-11-01"] * 3,
        "Document ID": [f"{name_hint}-{i}" for i in range(3)],
        "URL": [f"https://x.com/a/status/{name_hint}{i}" for i in range(3)],
        "Hit Sentence": ["bomb the base now"] * 3,
        "Author Handle": ["@a"] * 3,
        "Engagement": [1] * 3,
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


def _wait(client, jid):
    last = {}
    for _ in range(100):
        last = client.get(f"/jobs/{jid}").get_json()
        if last.get("state") != "running":
            return last
        time.sleep(0.1)
    return last


def _chrome():
    return shutil.which("google-chrome") or shutil.which("google-chrome-stable") or shutil.which("chromium")


def test_recent_export_rules():
    node = shutil.which("node")
    assert node, "node is required"
    script = r"""
const api = require(process.argv[1]);
const checks = [
  [api.shouldList({state:"done", has_download:true}), true],
  [api.shouldList({state:"running", has_download:true}), false],
  [api.shouldList({state:"error", has_download:true}), false],
  [api.shouldList({state:"done", has_download:false}), false],
  [api.shouldList(null), false],
  [api.labelFor({filenames:""}), "Flagged posts"],
  [api.labelFor({}), "Flagged posts"],
  [api.labelFor({filenames:"latest.xlsx"}), "latest.xlsx"],
  [api.safeId("w9Rf3AwFYFp7uV5Y5oos6g"), "w9Rf3AwFYFp7uV5Y5oos6g"],
  [api.safeId("../etc/passwd"), ""],
  [api.safeId("a b"), ""],
  [api.safeId(""), ""],
];
const bad = checks.filter((pair) => pair[0] !== pair[1]);
if (bad.length) {
  process.stderr.write(JSON.stringify(bad));
  process.exit(1);
}
"""
    proc = subprocess.run([node, "-e", script, str(JS)], check=False, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_dashboard_lists_finished_export_on_reload(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch).test_client()
    first = client.get("/dashboard").get_data(as_text=True)
    assert 'id="recent-downloads" hidden' in first
    assert "/static/recent-exports.js" in first
    done = first.index('okEl.textContent=j.message||"Done.";okEl.hidden=false;finish();')
    refresh = first.index("mtRecentExports.noteCompleted(id,j)")
    download = first.index("downloadBlob(id).then")
    assert done < refresh < download
    assert "location.reload" not in first

    body = _xlsx("earlier")
    posted = client.post(
        "/upload",
        data={"files": (io.BytesIO(body), "earlier.xlsx")},
        headers={"X-Upload-Mode": "job"},
        content_type="multipart/form-data",
    )
    assert posted.status_code == 202
    jid = posted.get_json()["job"]
    job = _wait(client, jid)
    assert job["state"] == "done" and job["has_download"], job
    page = client.get("/dashboard").get_data(as_text=True)
    assert 'id="recent-downloads" hidden' not in page
    assert f'data-job="{jid}"' in page
    assert f'href="/jobs/{jid}/download"' in page
    assert "earlier.xlsx" in page


def test_recent_export_dom_keeps_progress_and_download_link(tmp_path):
    chrome = _chrome()
    if not chrome:
        pytest.skip("Chrome is not installed")
    page = tmp_path / "recent.html"
    page.write_text(
        """<!doctype html><meta charset="utf-8"><title>recent</title>
<div class="upload-status"><div class="progress">
<p class="pstage">Done</p>
<p class="okmsg presult">Screened 3 posts. Downloading the Excel export…</p>
<p class="err perror" hidden>nope</p>
</div></div>
<div class="card" id="recent-downloads" hidden>
<ul class="dl-list">
<li data-job="oldjob"><a href="/jobs/oldjob/download">earlier.xlsx</a></li>
"""
        + "\n".join(
            f'<li data-job="pad{i}"><a href="/jobs/pad{i}/download">pad{i}.xlsx</a></li>'
            for i in range(9)
        )
        + """
</ul></div>
<script>"""
        + JS.read_text()
        + """</script>
<script>
var job = {state:"done", has_download:true, filenames:"<img src=x onerror=alert(1)>.xlsx"};
var added = mtRecentExports.noteCompleted("w9Rf3AwFYFp7uV5Y5oos6g", job);
var again = mtRecentExports.noteCompleted("w9Rf3AwFYFp7uV5Y5oos6g", job);
mtRecentExports.noteCompleted("runningjob", {state:"running", has_download:true, filenames:"nope.xlsx"});
mtRecentExports.noteCompleted("nodl", {state:"done", has_download:false, filenames:"nodl.xlsx"});
mtRecentExports.noteCompleted("../bad", {state:"done", has_download:true, filenames:"bad.xlsx"});
document.documentElement.dataset.added = added ? "yes" : "no";
document.documentElement.dataset.again = again ? "yes" : "no";
</script>
""",
        encoding="utf-8",
    )
    # Headless Chrome prints the DOM, then can sit on a missing system bus.
    # Read stdout and stop the process once the document is out.
    user_dir = tmp_path / "dump-chrome"
    user_dir.mkdir()
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
         f"--user-data-dir={user_dir}", "--dump-dom", page.as_uri()],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks = []

    def _read():
        while True:
            block = proc.stdout.read(8192)
            if not block:
                break
            chunks.append(block)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    deadline = time.time() + 25
    while time.time() < deadline and proc.poll() is None and b"</html>" not in b"".join(chunks):
        time.sleep(0.2)
    proc.kill()
    reader.join(5)
    html = b"".join(chunks).decode()
    assert "</html>" in html
    visible = html.split("<script>", 1)[0]
    assert 'data-added="yes"' in html
    assert 'data-again="no"' in html
    assert 'id="recent-downloads" hidden' not in visible
    assert visible.index("w9Rf3AwFYFp7uV5Y5oos6g") < visible.index("oldjob")
    assert "&lt;img src=x onerror=alert(1)&gt;.xlsx" in visible
    assert "<img" not in visible
    assert "nope.xlsx" not in visible
    assert "nodl.xlsx" not in visible
    assert "bad.xlsx" not in visible
    assert "pad8.xlsx" not in html
    assert "pad7.xlsx" in visible
    assert "Screened 3 posts. Downloading the Excel export…" in visible
    assert ">Done<" in visible
    assert visible.count("/jobs/w9Rf3AwFYFp7uV5Y5oos6g/download") == 1


def test_completed_job_appears_in_recent_exports_without_reload(tmp_path, monkeypatch):
    chrome = _chrome()
    node = shutil.which("node")
    if not chrome or not node:
        pytest.skip("Chrome and node are required for the browser check")
    app = _app(tmp_path, monkeypatch)
    client = app.test_client()
    posted = client.post(
        "/upload",
        data={"files": (io.BytesIO(_xlsx("earlier")), "earlier.xlsx")},
        headers={"X-Upload-Mode": "job"},
        content_type="multipart/form-data",
    )
    earlier = _wait(client, posted.get_json()["job"])
    assert earlier["state"] == "done", earlier

    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    latest = tmp_path / "latest.xlsx"
    latest.write_bytes(_xlsx("latest"))
    report_path = tmp_path / "browser-report.json"
    user_dir = tmp_path / "chrome"
    user_dir.mkdir()
    shot = tmp_path / "recent-exports.png"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        debug_port = sock.getsockname()[1]
    env = os.environ.copy()
    env["CHROME_BIN"] = chrome
    try:
        proc = subprocess.run(
            [node, str(BROWSER), f"http://127.0.0.1:{port}/dashboard", str(latest),
             str(report_path), str(debug_port), str(user_dir), str(shot)],
            check=False, capture_output=True, text=True, timeout=90, env=env,
        )
    finally:
        server.shutdown()
    report = json.loads(report_path.read_text()) if report_path.exists() else {"stderr": proc.stderr}
    assert proc.returncode == 0, report
    assert report["ok"] is True
    assert report["before"]["links"][0]["text"] == "earlier.xlsx"
    assert shot.exists() and shot.stat().st_size > 1000
