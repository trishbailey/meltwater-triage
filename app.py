"""Flask app: upload Meltwater exports, download flagged posts as Excel.

A /dashboard blueprint can be registered later reusing `require_password`
and `triage.filters.screen_dataframe`.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, Response, render_template, request, send_file
import io

from triage.pipeline import process, to_workbook

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))


def require_password(view):
    """HTTP basic auth; any username, password must equal APP_PASSWORD."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        expected = os.environ.get("APP_PASSWORD")
        if not expected:
            return Response("Server not configured: APP_PASSWORD is not set.", 503)
        auth = request.authorization
        if not auth or not hmac.compare_digest((auth.password or "").encode(), expected.encode()):
            return Response("Authentication required.", 401,
                            {"WWW-Authenticate": 'Basic realm="triage"'})
        return view(*args, **kwargs)
    return wrapper


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

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
        files = [(f.filename, f.read()) for f in request.files.getlist("files") if f and f.filename]
        if not files:
            return render_template("index.html", error="Please choose at least one file."), 400
        try:
            result = process(files)
        except ValueError as exc:
            return render_template("index.html", error=str(exc)), 400
        except Exception as exc:  # unreadable/corrupt file: no stack trace to the user
            app.logger.warning("Upload failed: %s", type(exc).__name__)
            return render_template("index.html", error="Could not read the uploaded file(s)."), 400
        name = f"flagged_posts_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.xlsx"
        return send_file(io.BytesIO(to_workbook(result)), as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
