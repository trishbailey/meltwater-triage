# Meltwater Triage

Private web app that screens Meltwater exports for threat and protest language.

## Use
Upload one or more Meltwater downloads (`.xlsx` or `.csv`). The app returns an Excel
file of flagged posts with all original columns plus `uploaded_file`, `worksheet`, `link`,
`matched_keywords`, `category` (`threat` / `protest` / `both`) and `mobilization_status`.

Engagement and reach columns (Engagement, Shares, Likes, Views, Reach, Social Echo, etc.;
see `NUMERIC_COLUMNS`) are kept and parsed as numbers.

## Logic
- **Threat**: a threat-language term (`THREAT_TERMS`) AND a target term
  (`AUKUS_TARGET_TERMS` + `PERSON_ROLE_TERMS` + `PERSON_NAMES`) in the same post.
- **Protest**: mobilization rules over action, call-to-action, disruption, organizer,
  hashtag, logistics, date/time/address, past/future and inquiry cues. Union action
  without AUKUS/defense context is excluded.
- Matching is case-insensitive, whole word/phrase, over the text columns listed in
  `TEXT_COLUMN_OPTIONS`.

Edit all keyword lists in **`triage/keywords.py`**.

## Optional review stage
If `ANTHROPIC_API_KEY` is set, each flagged post is reviewed by Anthropic Claude in batches and gains columns
`is_genuine`, `refined_category`, `narrative_summary`, `theme`, `severity`,
`threat_type`, `targets`, `named_individuals` (lists stored as `; `-separated text). Without a key
the stage is skipped. Settings: `REFINE_MODEL` (default `claude-haiku-4-5`),
`REFINE_BATCH_SIZE` (default 25), `REFINE_DISABLED=1` to turn it off. A failed batch
leaves those columns blank and keeps the keyword results. Each request uses
`ANTHROPIC_MAX_TOKENS` (default 8192), `ANTHROPIC_TIMEOUT_SECONDS` (default 120),
and `ANTHROPIC_MAX_RETRIES` (default 0, so one batch is one API call). Code: `triage/refine.py`.

### Monthly Claude cap
`ANTHROPIC_MONTHLY_POST_CAP` limits how many posts are sent to Claude in a UTC month.
Every post included in a review request counts, and the total is stored in
the `anthropic_usage` table. After the cap is reached, later posts are not sent and
the workbook's **Warnings** sheet says so. Keyword flags are still returned. Review
only ever covers flagged posts. The guard does
nothing when the variable is unset. It skips Claude entirely when the variable is
set but `DATABASE_URL` / `PERSIST_RUNS` is not, because the count would reset on the
next process. Invalid values also skip Claude.

Protest fields from the review: `protest_location`, `protest_topic`, `organizers_or_groups`,
`event_date`, `scale_or_turnout`.

### Noisy terms (in `triage/keywords.py`)
- `CASE_SENSITIVE_TARGET_TERMS`: "MP" matches only as an uppercase whole word.
- `CONTEXT_REQUIRED_THREAT_TERMS` ("hang", "attack", "destroy") count only when a target
  term is within `THREAT_PROXIMITY_WORDS` (8) words.
- `WEAK_PROTEST_TERMS` ("demo", "register", "training", "outside") count only alongside
  another protest signal.

## Output workbook
Sheets: `Warnings` (only when the monthly cap skipped Claude, or the database save
failed), `Flagged`, `Recurring threat posters` (handle, name, threat post count, total
engagement, first/last seen; 2+ threat posts), and the sheets `Threat posts` and `Protest posts` (all original columns, AI columns, analyst relevance). Cell text is
written as text, including values that start with `=`.

## Uploads, migrations, and duplicates
`create_all` does not add columns. On startup the app adds any missing columns
(including `upload_jobs.payload`) without deleting rows. The uploaded file is
stored before screening starts, so a restart resumes a running job from those
bytes. Excel is read in chunks (`SCREEN_CHUNK_ROWS`, default 2000). Files larger
than `SCREEN_KEEP_ROWS` (default 2500) keep flagged rows in
memory; the screened-row count is unchanged.

Before review, a post already stored under the same Document ID, normalized URL,
or normalized-text hash is not sent again. Identical wording later in the same
file reuses that result. The publishing platform or site is stored as the source.
The export product name is never shown as the source.

`python -m triage.backfill` prints a Haiku 4.5 price estimate for flagged posts
that have no stored review. It does not call the API. A paid run stays refused
unless an explicit approval value is set, and an estimate above $5 is refused.

The map is Australia only. Low zoom draws the Natural Earth 110m Australia
outline on a dark navy ocean (public CARTO basemap tiles currently answer with
an API-key watermark, so they are not used). Zoom level 7 and closer adds
darkened OpenStreetMap streets.

## Storage
`triage/storage.py` (SQLAlchemy) stores each upload run and every flagged post, deduped by
Document ID. Reporting periods use the post date (`post_week`, `post_month` from Meltwater Date), with upload week/month kept as secondary fields. Helpers: `monthly_counts`, `load_posts`. Enabled when
`DATABASE_URL` is set (Postgres; `postgres://` URLs are rewritten to
`postgresql+psycopg://`) or `PERSIST_RUNS=1` (local SQLite at
`STORAGE_SQLITE_PATH`, default `data/triage.db`). Render's free filesystem is temporary,
so lasting storage and the monthly Claude cap need a Render Postgres database.
Uploads are limited by `MAX_UPLOAD_MB` (default 50), `MAX_UNCOMPRESSED_MB` (default 200,
xlsx zip expansion), and `MAX_SCREEN_ROWS` (default 100000). Document IDs are read as
text so a blank cell does not turn `12345` into `12345.0`.

## Sign-in

Pages use a `/login` form with a single password field (checked against `APP_PASSWORD`). Success sets a signed,
HttpOnly, Secure, SameSite=Lax session cookie for 30 days, signed with `SECRET_KEY` (required; pages return 503
without it). Five failed attempts from one address within 15 minutes are blocked (per worker process). Signed-out
page requests redirect to `/login`; `/healthz` is open and `/api/alerts` uses its bearer token. Sign-in, sign-out,
uploads and alert deletion reject cross-site Origin headers. For local http testing set `SESSION_COOKIE_SECURE=0`.

## Status, dashboard and alerts

- `POST /alerts/<id>/delete` (signed in): deletes one alert; the dashboard has a Delete button on each alert.
- `/status` (password): stored rows by post month and flag type, upload runs, alert count, and posts sent for review this month against the monthly cap.
- `/dashboard` (password): three parts. (1) A map of posts since November 1 (`DASHBOARD_START_DATE`) with Threats and Protests toggles only. (2) Threats and (3) Protests, each with topic analysis, top targets, top authors (with their posts) and a Flagged posts dropdown showing platform, handle, date, text and Relevant / Not relevant buttons. Meltwater posts, Seerist PDF items and alerts share one dataset and the same pre-sort. Each section has a download button (`/download/threats.xlsx`, `/download/protests.xlsx`, `?days=` 1-365) with the original columns plus the AI review.
- `POST /api/alerts` with `Authorization: Bearer $ALERT_INGEST_TOKEN` and a JSON body:
  `source` (x, bluesky, facebook, instagram, newsletter), `url`, `author`, `text`, `posted_at` (ISO 8601),
  `matched_terms` (list or `;`-separated), `signal_level` (critical, high, medium, low).
  Returns 201 when stored, 200 for a duplicate, 400 on bad input, 401 on a bad token, 503 if unconfigured.
- Alerts are deduped by normalized URL (host lowercased, `www.` dropped, twitter.com = x.com, query, fragment
  and trailing slash removed). The first-seen time is kept. An alert whose URL later arrives in a Meltwater
  upload is marked "Confirmed in Meltwater" with its lead time. Alerts are a separate layer and never enter
  Meltwater threat or protest totals.

## Run locally
```
pip install -r requirements.txt -r requirements-dev.txt
APP_PASSWORD=... flask --app app run
pytest                       # synthetic fixtures only
MELTWATER_SAMPLE_PATH=/path/to/export.xlsx pytest -s   # also checks a real export
```

## Deploy (Render)
`render.yaml` defines a Python web service (gunicorn). Set `APP_PASSWORD` in the Render
dashboard, along with `ANTHROPIC_MONTHLY_POST_CAP` if you want a monthly Claude limit.
Requests use HTTP basic auth (any username, password = `APP_PASSWORD`). Browsers that
send a cross-site `Origin` on `POST /upload` are rejected.

## Data
Never commit client exports. `.gitignore` excludes `*.xlsx`, `*.xls`, `*.csv`.

## Roadmap
Password-protected `/dashboard` (volume over time, threat vs protest split, top keywords,
top sources/authors, drill-down) will reuse `require_password` and `screen_dataframe`.


## Scheduled collection (Facebook, Instagram, Bluesky)
`POST /api/collect` (same bearer token as `/api/alerts`) starts `triage/collect.py` in the background; `GET /api/collect` reports the last result.
The web service also runs the pull itself at 06:00 and 20:00 UTC (about 5 pm and 7 am Sydney time in summer, 4 pm and 6 am in winter) (`COLLECT_HOURS`; `COLLECT_SCHEDULE=0` turns it off), starting only when `APIFY_TOKEN` or the Bluesky variables are set. No separate cron job is needed. Facebook pages and Instagram accounts run through the Apify actors
`apify/facebook-posts-scraper` and `apify/instagram-post-scraper` (`APIFY_TOKEN`); the account lists are constants in `triage/collect.py`.
Bluesky searches use `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`. `COLLECT_WINDOW_HOURS` (default 16) sets the look-back.
Posts are stored as alerts, pre-sorted like uploads, and de-duplicated by URL. X is not wired yet.

## Pipeline

1. Posts arrive from Meltwater uploads, Seerist PDF uploads, and the scheduled Apify (Facebook, Instagram) and Bluesky pulls.
2. The Python pre-sort keeps threat and protest candidates (`triage/filters.py`).
3. Claude filters the candidates: keep or discard only, no analysis (`triage/refine.py`, filter mode). Stored posts and collected alerts are both filtered (`triage/stages.py`).
4. The dashboard lists the survivors in clusters of similar posts (`triage/grouping.py`). One mark on a cluster labels every post in it, and new posts that match a judged cluster inherit its label (`triage/propagate.py`).
5. Claude receives the analyst's judgments as examples (one per pattern) in every filter prompt.
6. Topic and target analysis (Claude, analyze mode), the map, and both Excel downloads use only posts marked Relevant. Analysis runs in the background after posts are marked Relevant.
