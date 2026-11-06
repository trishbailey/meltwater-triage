"""Add missing columns in place. SQLAlchemy create_all() does not alter existing tables.

The live upload_jobs table was created before `payload` existed. Selecting that
column raises ProgrammingError, and the job status endpoint then returns 404.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

log = logging.getLogger(__name__)


def ensure_schema(engine, metadata) -> list[str]:
    """Create missing tables and ADD any columns defined in metadata but absent on disk.

    Existing rows are kept. New columns are nullable so SQLite can add them to a
    table that already has data. Safe to call on every process start.
    """
    metadata.create_all(engine)
    added: list[str] = []
    insp = inspect(engine)
    for table in metadata.tables.values():
        if not insp.has_table(table.name):
            continue
        have = {col["name"] for col in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            col_type = col.type.compile(dialect=engine.dialect)
            if engine.dialect.name == "postgresql":
                stmt = f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{col.name}" {col_type}'
            else:
                stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                added.append(f"{table.name}.{col.name}")
                have.add(col.name)
            except Exception as exc:
                # Another process may have added the column between inspect and ALTER.
                log.warning("Schema add %s.%s failed: %s", table.name, col.name, type(exc).__name__)
                insp = inspect(engine)
                have = {c["name"] for c in insp.get_columns(table.name)}
                if col.name not in have:
                    raise
    if added:
        log.info("Added columns: %s", ", ".join(added))
    return added
