"""Flask app: upload Meltwater exports, download flagged posts as Excel.

Pages use the session login in `require_password`.
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
    """Signed session cookie set by /login; signed-out page requests go to /login."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        problem = _auth_configured()
        if problem:
            return Response(problem, 503)
        if not session.get("auth"):
            nxt = request.full_path if request.method == "GET" else "/"
            return redirect(url_for("login", next=nxt.rstrip("?")))
        return view(*args, **kwargs)
    return wrapper


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
        problem = _auth_configured()
        if problem:
            return Response(problem, 503)
        nxt = _safe_next(request.values.get("next"))
        if request.method == "GET":
            if session.get("auth"):
                return redirect(nxt)
            return render_template("login.html", error=None, next=nxt)
        if not _same_origin_upload():
            return render_template("login.html", error="Sign-in blocked: use the form on this site.", next=nxt), 403
        key, now = _client_key(), time.monotonic()
        with _failures_lock:
            if len(_recent_failures(key, now)) >= LOGIN_MAX_FAILURES:
                return render_template("login.html", next=nxt,
                                       error="Too many failed attempts. Wait 15 minutes and try again."), 429
        if _password_ok(request.form.get("password", ""), os.environ["APP_PASSWORD"]):
            with _failures_lock:
                _failures.pop(key, None)
            session.clear()
            session.permanent = True
            session["auth"] = True
            return redirect(nxt)
        with _failures_lock:
            _recent_failures(key, now).append(now)
        app.logger.warning("Failed sign-in attempt")
        return render_template("login.html", error="Incorrect password.", next=nxt), 401

    @app.post("/logout")
    def logout():
        if not _same_origin_upload():
            return Response("Blocked.", 403)
        session.clear()
        return redirect(url_for("login"))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/")
    @require_password
    def index():
        return render_template("index.html", error=None)

    @app.post("/upload")
    @require_password
    def upload():
        if not _same_origin_upload():
            origin = request.headers.get("Origin", "").replace("\n", "").replace("\r", "")[:200]
            app.logger.warning("Rejected cross-site upload from origin %s", origin)
            return render_template("index.html", error="Upload blocked: submit the form from this site."), 403
        files = [(f.filename, f.read()) for f in request.files.getlist("files") if f and f.filename]
        if not files:
            return render_template("index.html", error="Please choose at least one file."), 400
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
        name = f"flagged_posts_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xlsx"
        return send_file(io.BytesIO(payload), as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

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
            ctx = dash.build(pd.DataFrame())
        else:
            ctx = dash.build(st.load_posts(), st.load_alerts(), st.meltwater_first_seen(),
                             month=request.args.get("month"))
        return render_template("dashboard.html", **ctx)

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
