# Meltwater Triage

Private web app that screens Meltwater exports for threat and protest language.

## Use
Upload one or more Meltwater downloads (`.xlsx`, `.xls`, `.csv`). The app returns an Excel
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
`REFINE_BATCH_SIZE` (default 20), `REFINE_DISABLED=1` to turn it off. A failed batch
leaves those columns blank and keeps the keyword results. Code: `triage/refine.py`.

Protest fields from the review: `protest_location`, `protest_topic`, `organizers_or_groups`,
`event_date`, `scale_or_turnout`.

### Noisy terms (in `triage/keywords.py`)
- `CASE_SENSITIVE_TARGET_TERMS`: "MP" matches only as an uppercase whole word.
- `CONTEXT_REQUIRED_THREAT_TERMS` ("hang", "attack", "destroy") count only when a target
  term is within `THREAT_PROXIMITY_WORDS` (8) words.
- `WEAK_PROTEST_TERMS` ("demo", "register", "training", "outside") count only alongside
  another protest signal.

## Heated-language analysis
Covers all screened posts, not just keyword hits. Each scored post gets `heat_score` (0-10),
`heat_topic`, `heated_phrases`, `heat_sampled`. Cost is capped by `HEAT_MAX_POSTS`
(default 300; a reproducible random sample is scored above that) and batched by
`HEAT_BATCH_SIZE` (default 25). `HEAT_DISABLED=1` turns it off; it is skipped without
`ANTHROPIC_API_KEY`.

## Output workbook
Sheets: `Flagged`, `Recurring threat posters` (handle, name, threat post count, total
engagement, first/last seen; 2+ threat posts), and, when heat ran, `Heat by topic` (ranked
by average then total heat, with engagement) and `Heat (all posts)`.

## Storage
`triage/storage.py` (SQLAlchemy) stores each upload run and its flagged posts, deduped by
Document ID, with upload week and month for month-over-month comparisons. Enabled when
`DATABASE_URL` is set (Postgres) or `PERSIST_RUNS=1` (local SQLite at
`STORAGE_SQLITE_PATH`, default `data/triage.db`). Render's free filesystem is temporary,
so lasting storage needs a Render Postgres database.

## Run locally
```
pip install -r requirements.txt -r requirements-dev.txt
APP_PASSWORD=... flask --app app run
pytest                       # synthetic fixtures only
MELTWATER_SAMPLE_PATH=/path/to/export.xlsx pytest -s   # also checks a real export
```

## Deploy (Render)
`render.yaml` defines a Python web service (gunicorn). Set `APP_PASSWORD` in the Render
dashboard. Requests use HTTP basic auth (any username, password = `APP_PASSWORD`).

## Data
Never commit client exports. `.gitignore` excludes `*.xlsx`, `*.xls`, `*.csv`.

## Roadmap
Password-protected `/dashboard` (volume over time, threat vs protest split, top keywords,
top sources/authors, drill-down) will reuse `require_password` and `screen_dataframe`.
