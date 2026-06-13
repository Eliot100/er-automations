"""SQLite schema and connection helpers.

Single-file SQLite database. Stores runs / step executions / artifacts /
edits metadata. Raw input and output files live on the filesystem; the DB
keeps the paths.

Schema is created on first connect; migrations are intentionally absent
at this stage — we'll add them once the schema stabilizes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
    id           INTEGER PRIMARY KEY,
    external_id  TEXT UNIQUE,        -- placeholder for future Outlook/Entra OID
    display_name TEXT NOT NULL,
    email        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS automation (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,   -- e.g. 'kfar_hanasi.electricity_monthly'
    name        TEXT NOT NULL,
    customer    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only history model:
-- * Multiple runs may exist for the same (automation, period); old ones are
--   never silently overwritten. When the user starts a run and prior data
--   exists, the platform asks remove/use/ignore once at the start of the run.
-- * Multiple step_execution rows may exist for the same (run_id, step_index);
--   each attempt is a new row with its own timestamp and creator.
-- * Same for artifacts on disk: each attempt writes to its own subfolder.
-- * Deletions are explicit (per-row or per-run). Cascades only fire when the
--   user explicitly removes a parent run.

CREATE TABLE IF NOT EXISTS run (
    id                  INTEGER PRIMARY KEY,
    automation_id       INTEGER NOT NULL REFERENCES automation(id),
    user_id             INTEGER REFERENCES user(id),
    period              TEXT NOT NULL,         -- 'YYYY-MM'
    status              TEXT NOT NULL,         -- running|paused|completed|aborted
    current_step_index  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at         TEXT
    -- NOTE: no UNIQUE(automation_id, period). Multiple runs per month allowed;
    -- duplicates are surfaced to the user, never auto-overwritten.
);

CREATE TABLE IF NOT EXISTS step_execution (
    id                   INTEGER PRIMARY KEY,
    run_id               INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    step_index           INTEGER NOT NULL,
    attempt_no           INTEGER NOT NULL,      -- 1-based, increments per (run, step_index)
    name                 TEXT NOT NULL,
    status               TEXT NOT NULL,         -- pending|running|good|verify|bad|aborted
    live_action_json     TEXT,
    verify_rows_json     TEXT,
    flagged_columns_json TEXT,
    notes                TEXT,
    created_by_user_id   INTEGER REFERENCES user(id),
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at          TEXT,
    is_current           INTEGER NOT NULL DEFAULT 1, -- 1 = the active row for (run, step)
    UNIQUE(run_id, step_index, attempt_no)
);

CREATE TABLE IF NOT EXISTS artifact (
    id                  INTEGER PRIMARY KEY,
    step_execution_id   INTEGER NOT NULL REFERENCES step_execution(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,         -- input|output
    filename            TEXT NOT NULL,
    path                TEXT NOT NULL,         -- unique per attempt, never reused
    size                INTEGER NOT NULL,
    mime                TEXT,
    created_by_user_id  INTEGER REFERENCES user(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS edit (
    id                  INTEGER PRIMARY KEY,
    step_execution_id   INTEGER NOT NULL REFERENCES step_execution(id) ON DELETE CASCADE,
    row_id              TEXT NOT NULL,
    column              TEXT NOT NULL,
    old_value           TEXT,
    new_value           TEXT,
    remember            INTEGER NOT NULL DEFAULT 0,
    created_by_user_id  INTEGER REFERENCES user(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_run_period ON run(automation_id, period);
CREATE INDEX IF NOT EXISTS idx_run_status ON run(status);
CREATE INDEX IF NOT EXISTS idx_step_execution_run ON step_execution(run_id);
CREATE INDEX IF NOT EXISTS idx_step_execution_current
    ON step_execution(run_id, step_index, is_current);
CREATE INDEX IF NOT EXISTS idx_artifact_step ON artifact(step_execution_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys and row factory enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create schema if missing and return a connection."""
    path = Path(db_path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def db_session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context-managed connection with commit/rollback on exit."""
    conn = init_db(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
