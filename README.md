# Meltwater Triage

Private web app that screens Meltwater exports for threat and protest language.

## Use
Upload one or more Meltwater downloads (`.xlsx`, `.xls`, `.csv`). The app returns an Excel
file of flagged posts with all original columns plus `uploaded_file`, `worksheet`, `link`,
`matched_keywords`, `category` (`threat` / `protest` / `both`) and `mobilization_status`.

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
If `OPENAI_API_KEY` is set, each flagged post is reviewed in batches and gains columns
`is_genuine`, `refined_category`, `narrative_summary`, `theme`, `severity`. Without a key
the stage is skipped. Settings: `OPENAI_MODEL` (default `gpt-4o-mini`),
`REFINE_BATCH_SIZE` (default 20), `REFINE_DISABLED=1` to turn it off. A failed batch
leaves those columns blank and keeps the keyword results. Code: `triage/refine.py`.

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
