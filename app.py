"""Flask app: upload Meltwater exports, download flagged posts as Excel.

Pages are public; only POST /api/alerts needs a bearer token.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlsplit

import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from flask import (Flask, Response, redirect, render_template, request, send_file, session,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
import io

import pandas as pd

from triage.pipeline import process, to_workbook
from triage import dashboard as dash
from triage.alerts import AlertError, parse_alert
from triage.storage import Storage, persistence_enabled
from triage import jobs
from triage.seerist import SeeristError, item_key, parse_pdf, to_alert

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))


SESSION_DAYS = 30
LOGIN_MAX_FAILURES = int(os.environ.get("LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("LOGIN_WINDOW_SECONDS", "900"))
_failures: dict[str, deque] = defaultdict(deque)
_failures_lock = threading.Lock()


def _password_ok(supplied: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def _client_key() -> str:
    return request.remote_addr or "unknown"


def _recent_failures(key: str, now: float) -> deque:
    q = _failures[key]
    while q and now - q[0] > LOGIN_WINDOW_SECONDS:
        q.popleft()
    return q


def _auth_configured() -> str | None:
    if not os.environ.get("APP_PASSWORD"):
        return "Server not configured: APP_PASSWORD is not set."
    if not os.environ.get("SECRET_KEY"):
        return "Server not configured: SECRET_KEY is not set."
    return None


def require_password(view):
    """Pages are public by the owner's choice (the data is public). Kept as a no-op so the
    gate can be restored in one place. /api/alerts keeps its bearer token."""
    return view


def _safe_next(target: str | None) -> str:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/dashboard"
    return target


def _same_origin_upload() -> bool:
    """Browsers send Origin on POST. Reject cross-site uploads that would spend the API budget."""
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return True
    if origin.lower() == "null":
        return False
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and parts.netloc == request.host


def _count_rows(files: list[tuple[str, bytes]]) -> int:
    """Cheap row estimate so the UI can say File accepted with a count."""
    total = 0
    for name, data in files:
        lower = name.lower()
        try:
            if lower.endswith(".csv"):
                total += max(0, data.count(b"\n") - 1)
            elif lower.endswith(".xlsx"):
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
                total += max(0, (wb[wb.sheetnames[0]].max_row or 1) - 1)
                wb.close()
        except Exception:
            continue
    return total


def _public_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > 400:
        text = text[:400] + "…"
    return text or "Could not read the uploaded file(s)."


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    app.config.update(SECRET_KEY=os.environ.get("SECRET_KEY") or None,
                      SESSION_COOKIE_NAME="mt_session",
                      SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") != "0",
                      SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS))
    # Render terminates TLS at one proxy hop; trust its X-Forwarded-For/Proto.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        return redirect(_safe_next(request.values.get("next")))

    @app.get("/robots.txt")
    def robots():
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/")
    @require_password
    def index():
        return redirect(url_for("dashboard"))

    @app.get("/upload")
    @require_password
    def upload_page():
        return render_template("index.html", error=None)

    @app.post("/upload")
    @require_password
    def upload():
        if not _same_origin_upload():
            origin = request.headers.get("Origin", "").replace("\n", "").replace("\r", "")[:200]
            app.logger.warning("Rejected cross-site upload from origin %s", origin)
            return render_template("index.html", error="Upload blocked: submit the form from this site."), 403
        files = [(f.filename, f.read()) for f in request.files.getlist("files") if f and f.filename]
        wants_job = request.headers.get("X-Upload-Mode") == "job"
        if not files:
            if wants_job:
                return {"error": "Please choose at least one file."}, 400
            return render_template("index.html", error="Please choose at least one file."), 400
        if wants_job:
            names = ", ".join(n for n, _ in files)[:300]
            rows_hint = _count_rows(files)
            def work(progress):
                t0 = time.monotonic()
                progress("File accepted", 0, rows_hint or 0)
                try:
                    result = process(files, progress=progress)
                except ValueError as exc:
                    raise ValueError(_public_error(exc)) from exc
                except Exception as exc:
                    app.logger.warning("Upload failed: %s", type(exc).__name__)
                    raise ValueError("Could not read the uploaded file(s).") from exc
                progress("Building the Excel export", len(result.all_posts), len(result.all_posts))
                payload = to_workbook(result)
                app.logger.info("Screened %d posts in %.1fs", len(result.all_posts), time.monotonic() - t0)
                return {"payload": payload, "rows": len(result.all_posts),
                        "message": (f"File accepted: {names}. Screened {len(result.all_posts)} posts; "
                                    f"{len(result.flagged)} flagged. The dashboard has been updated.")}
            jid = jobs.start("screen", work, filenames=names, rows=rows_hint)
            return {"job": jid, "filenames": names, "rows": rows_hint}, 202
        try:
            result = process(files)
        except ValueError as exc:
            return render_template("index.html", error=_public_error(exc)), 400
        except Exception as exc:  # unreadable/corrupt file: no stack trace to the user
            app.logger.warning("Upload failed: %s", type(exc).__name__)
            return render_template("index.html", error="Could not read the uploaded file(s)."), 400
        try:
            payload = to_workbook(result)
        except Exception as exc:
            app.logger.warning("Workbook failed: %s", type(exc).__name__)
            return render_template("index.html", error="Could not build the download."), 500
        return _xlsx(payload)

    def _xlsx(payload: bytes):
        name = f"flagged_posts_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xlsx"
        return send_file(io.BytesIO(payload), as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/jobs/<jid>")
    @require_password
    def job_status(jid: str):
        j = jobs.get(jid)
        if j is None:
            return {"error": "Job not found. It may have expired."}, 404
        return jobs.public(j, jid)

    @app.get("/jobs/<jid>/download")
    @require_password
    def job_download(jid: str):
        data = jobs.payload(jid)
        if data is None:
            return Response("Download not available. It may have expired.", 404)
        return _xlsx(data)

    @app.get("/seerist")
    @require_password
    def seerist_page():
        return render_template("seerist.html", error=None, message=None)

    def _import_seerist(data: bytes, progress) -> dict:
        st = _storage()
        if st is None:
            raise ValueError("Storage is not configured, so Seerist items cannot be saved.")
        progress("Reading the PDF", 0, 0)
        try:
            items = parse_pdf(data)
        except SeeristError as exc:
            raise ValueError(str(exc)) from exc
        if not items:
            raise ValueError("No Seerist items were found in that PDF.")
        added = skipped = 0
        for n, item in enumerate(items, 1):
            _, created = st.add_alert(to_alert(item), url_key=item_key(item))
            added, skipped = added + created, skipped + (not created)
            progress("Saving items", n, len(items))
        msg = (f"Added {added} item{'s' if added != 1 else ''}, skipped {skipped} "
               f"duplicate{'s' if skipped != 1 else ''}.")
        return {"message": msg, "added": added, "skipped": skipped, "items": len(items)}

    @app.post("/seerist/upload")
    @require_password
    def seerist_upload():
        wants_job = request.headers.get("X-Upload-Mode") == "job"

        def fail(msg, code):
            if wants_job:
                return {"error": msg}, code
            return render_template("seerist.html", error=msg, message=None), code
        if not _same_origin_upload():
            return fail("Upload blocked: submit the form from this site.", 403)
        f = request.files.get("file")
        if not f or not f.filename:
            return fail("Please choose a Seerist PDF export.", 400)
        data = f.read()
        if not data.startswith(b"%PDF"):
            return fail("That file is not a PDF. Upload a Seerist PDF export.", 400)
        if wants_job:
            name = f.filename
            jid = jobs.start("seerist", lambda progress: _import_seerist(data, progress),
                             filenames=name, rows=0)
            return {"job": jid, "filenames": name, "rows": 0}, 202
        try:
            res = _import_seerist(data, lambda *a: None)
        except ValueError as exc:
            return fail(str(exc), 400)
        return render_template("seerist.html", error=None, message=res["message"])

    def _storage():
        if not persistence_enabled():
            return None
        if "storage" not in app.extensions:
            app.extensions["storage"] = Storage()
        return app.extensions["storage"]

    @app.get("/status")
    @require_password
    def status():
        st = _storage()
        if st is None:
            return render_template("status.html", error="Storage is not configured (DATABASE_URL is not set).", s=None)
        cap = os.environ.get("ANTHROPIC_MONTHLY_POST_CAP")
        return render_template("status.html", error=None, s=st.status_summary(), cap=cap)

    @app.get("/dashboard")
    @require_password
    def dashboard():
        st = _storage()
        if st is None:
            ctx = dash.build(pd.DataFrame(), map_days=30)
        else:
            days = request.args.get("map_days", "30")
            try:
                map_days = None if days in ("all", "") else max(1, min(3650, int(days)))
            except ValueError:
                map_days = 30
            ctx = dash.build(st.load_posts(), st.load_alerts(), st.meltwater_first_seen(),
                             month=request.args.get("month"), map_days=map_days)
        return render_template("dashboard.html", **ctx)

    @app.get("/api/map")
    @require_password
    def api_map():
        st = _storage()
        days = request.args.get("days", "30")
        try:
            map_days = None if days in ("all", "") else max(1, min(3650, int(days)))
        except ValueError:
            map_days = 30
        from triage.mapdata import build_map
        if st is None:
            return build_map(pd.DataFrame(), None, days=map_days)
        return build_map(st.load_posts(), st.load_alerts(limit=500), days=map_days)

    @app.post("/alerts/<int:alert_id>/delete")
    @require_password
    def delete_alert(alert_id: int):
        if not _same_origin_upload():
            return Response("Blocked: submit the form from this site.", 403)
        st = _storage()
        if st is None:
            return Response("Storage is not configured.", 503)
        if not st.delete_alert(alert_id):
            return Response("Alert not found.", 404)
        return redirect(url_for("dashboard") + "#early-warning")

    @app.post("/api/purge-test-posts")
    def purge_test_posts():
        """One-shot cleanup for synthetic load-test rows (Document ID ends with -<n>)."""
        expected = os.environ.get("ALERT_INGEST_TOKEN")
        if not expected:
            return {"error": "not configured"}, 503
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token.encode("utf-8", "ignore"), expected.encode("utf-8")):
            return {"error": "unauthorized"}, 401
        st = _storage()
        if st is None:
            return {"error": "storage is not configured"}, 503
        deleted = st.delete_test_posts()
        return {"deleted": deleted}

    @app.post("/api/alerts")
    def ingest_alert():
        expected = os.environ.get("ALERT_INGEST_TOKEN")
        if not expected:
            return {"error": "alert ingest is not configured"}, 503
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token.encode("utf-8", "ignore"), expected.encode("utf-8")):
            return {"error": "unauthorized"}, 401
        st = _storage()
        if st is None:
            return {"error": "storage is not configured"}, 503
        try:
            alert = parse_alert(request.get_json(silent=True))
        except AlertError as exc:
            return {"error": str(exc)}, 400
        alert_id, created = st.add_alert(alert)
        return {"id": alert_id, "duplicate": not created}, 201 if created else 200

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
