"""Repository functions over the SQLite schema.

Design rules:

* Append-only history. Re-running a step inserts a new `step_execution` row
  (with `attempt_no` incremented) rather than overwriting the previous one.
  The previous row stays as audit history; only its `is_current` flag flips
  to 0. Nothing is deleted unless the user asks for it explicitly.

* Every write records who did it (`created_by_user_id`). Currently NULL or
  the single "local" user; populated for real once Outlook auth lands.

* The "remove / use / ignore" prompt at run start is built on top of
  `find_prior_runs` + `delete_run`. The runner pauses and asks before any
  data is touched.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

RunStatus = Literal["running", "paused", "completed", "aborted"]
StepStatus = Literal["pending", "running", "good", "verify", "bad", "aborted"]


def _json_or_none(v: Any) -> str | None:
    return json.dumps(v) if v is not None else None


# Column projections — kept in one place so adding a column does not require
# editing every SELECT site (and missing one would only fail at runtime inside
# the row → dataclass mapper).
_RUN_COLS = (
    "id, automation_id, user_id, period, status, current_step_index, created_at"
)
_STEP_COLS = (
    "id, run_id, step_index, attempt_no, name, status, "
    "live_action_json, verify_rows_json, flagged_columns_json, notes, "
    "created_by_user_id, created_at, is_current"
)


@dataclass(slots=True)
class User:
    id: int
    external_id: str | None
    display_name: str
    email: str | None


@dataclass(slots=True)
class Automation:
    id: int
    key: str
    name: str
    customer: str


@dataclass(slots=True)
class Run:
    id: int
    automation_id: int
    user_id: int | None
    period: str
    status: RunStatus
    current_step_index: int
    created_at: str


@dataclass(slots=True)
class StepExecution:
    id: int
    run_id: int
    step_index: int
    attempt_no: int
    name: str
    status: StepStatus
    live_action: dict[str, Any] | None
    verify_rows: list[dict[str, Any]] | None
    flagged_columns: list[str] | None
    notes: str | None
    created_by_user_id: int | None
    created_at: str
    is_current: bool


# ---------- users / automations ----------


def get_or_create_user(
    conn: sqlite3.Connection, display_name: str, email: str | None = None
) -> User:
    """Single-user MVP helper. Outlook/Entra wiring will replace this later."""
    row = conn.execute(
        "SELECT id, external_id, display_name, email FROM user WHERE display_name = ?",
        (display_name,),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO user (display_name, email) VALUES (?, ?)", (display_name, email)
        )
        return User(id=int(cur.lastrowid), external_id=None, display_name=display_name, email=email)
    user = User(**dict(row))
    # Backfill / update email when a new one is supplied. Once Outlook lands,
    # the OID is the join key and `display_name` won't carry email forward
    # automatically — promote whatever the caller knows.
    if email and user.email != email:
        conn.execute("UPDATE user SET email = ? WHERE id = ?", (email, user.id))
        user.email = email
    return user


def register_automation(
    conn: sqlite3.Connection, key: str, name: str, customer: str
) -> Automation:
    """Upsert by `key`. A second call with a different name/customer for the
    same key is treated as a rename and persists — silently dropping it
    would let typos linger forever.
    """
    conn.execute(
        "INSERT INTO automation (key, name, customer) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET name = excluded.name, customer = excluded.customer",
        (key, name, customer),
    )
    row = conn.execute(
        "SELECT id, key, name, customer FROM automation WHERE key = ?", (key,)
    ).fetchone()
    return Automation(**dict(row))


# ---------- runs ----------


def create_run(
    conn: sqlite3.Connection, automation_id: int, user_id: int | None, period: str
) -> Run:
    """Always creates a new run. Prior runs for the same (automation, period) are
    NOT touched — surface them with `find_prior_runs` first if you want the
    remove/use/ignore prompt.
    """
    cur = conn.execute(
        "INSERT INTO run (automation_id, user_id, period, status) VALUES (?, ?, ?, 'running')",
        (automation_id, user_id, period),
    )
    row = conn.execute(
        f"SELECT {_RUN_COLS} FROM run WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return Run(**dict(row))


def get_run(conn: sqlite3.Connection, run_id: int) -> Run | None:
    row = conn.execute(
        f"SELECT {_RUN_COLS} FROM run WHERE id = ?",
        (run_id,),
    ).fetchone()
    return Run(**dict(row)) if row else None


def find_prior_runs(
    conn: sqlite3.Connection,
    automation_id: int,
    period: str,
    exclude_run_id: int | None = None,
) -> list[Run]:
    """Other runs for the same (automation, period). Used to drive the
    remove/use/ignore prompt at run start. Always sorted newest-first.
    """
    sql = f"SELECT {_RUN_COLS} FROM run WHERE automation_id = ? AND period = ?"
    params: list[Any] = [automation_id, period]
    if exclude_run_id is not None:
        sql += " AND id != ?"
        params.append(exclude_run_id)
    sql += " ORDER BY created_at DESC, id DESC"
    return [Run(**dict(r)) for r in conn.execute(sql, params).fetchall()]


def update_run_status(
    conn: sqlite3.Connection,
    run_id: int,
    status: RunStatus,
    current_step_index: int | None = None,
) -> None:
    if current_step_index is None:
        conn.execute("UPDATE run SET status = ? WHERE id = ?", (status, run_id))
    else:
        conn.execute(
            "UPDATE run SET status = ?, current_step_index = ? WHERE id = ?",
            (status, current_step_index, run_id),
        )


def delete_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Explicit removal — cascades to step_execution → artifact/edit rows.

    Filesystem artifacts under data/runs/<run-id>/ must be removed by the
    caller (see `er_automations.persistence.storage`, added in Sprint 2).
    """
    conn.execute("DELETE FROM run WHERE id = ?", (run_id,))


# ---------- step executions ----------


def _next_attempt_no(conn: sqlite3.Connection, run_id: int, step_index: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next FROM step_execution "
        "WHERE run_id = ? AND step_index = ?",
        (run_id, step_index),
    ).fetchone()
    return int(row["next"])


def record_step(
    conn: sqlite3.Connection,
    run_id: int,
    step_index: int,
    name: str,
    status: StepStatus,
    live_action: dict[str, Any] | None = None,
    verify_rows: list[dict[str, Any]] | None = None,
    flagged_columns: list[str] | None = None,
    notes: str | None = None,
    created_by_user_id: int | None = None,
) -> StepExecution:
    """APPEND a new step execution attempt.

    Prior attempts for the same (run_id, step_index) are NOT overwritten.
    Their `is_current` is flipped to 0; the newly inserted row becomes the
    current one. Use `delete_step_execution` to remove an attempt explicitly.
    """
    attempt_no = _next_attempt_no(conn, run_id, step_index)
    conn.execute(
        "UPDATE step_execution SET is_current = 0 WHERE run_id = ? AND step_index = ?",
        (run_id, step_index),
    )
    cur = conn.execute(
        """
        INSERT INTO step_execution
            (run_id, step_index, attempt_no, name, status,
             live_action_json, verify_rows_json, flagged_columns_json,
             notes, created_by_user_id, finished_at, is_current)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 1)
        """,
        (
            run_id,
            step_index,
            attempt_no,
            name,
            status,
            _json_or_none(live_action),
            _json_or_none(verify_rows),
            _json_or_none(flagged_columns),
            notes,
            created_by_user_id,
        ),
    )
    r = conn.execute(
        f"SELECT {_STEP_COLS} FROM step_execution WHERE id = ?",
        (int(cur.lastrowid),),
    ).fetchone()
    return _row_to_step(r)


def _row_to_step(r: sqlite3.Row) -> StepExecution:
    return StepExecution(
        id=int(r["id"]),
        run_id=int(r["run_id"]),
        step_index=int(r["step_index"]),
        attempt_no=int(r["attempt_no"]),
        name=r["name"],
        status=r["status"],
        live_action=json.loads(r["live_action_json"]) if r["live_action_json"] else None,
        verify_rows=json.loads(r["verify_rows_json"]) if r["verify_rows_json"] else None,
        flagged_columns=json.loads(r["flagged_columns_json"]) if r["flagged_columns_json"] else None,
        notes=r["notes"],
        created_by_user_id=r["created_by_user_id"],
        created_at=r["created_at"],
        is_current=bool(r["is_current"]),
    )


def list_current_steps(conn: sqlite3.Connection, run_id: int) -> list[StepExecution]:
    """Latest attempt per step_index — what the UI shows by default."""
    rows = conn.execute(
        f"SELECT {_STEP_COLS} FROM step_execution "
        "WHERE run_id = ? AND is_current = 1 ORDER BY step_index",
        (run_id,),
    ).fetchall()
    return [_row_to_step(r) for r in rows]


def list_step_attempts(
    conn: sqlite3.Connection, run_id: int, step_index: int
) -> list[StepExecution]:
    """All attempts for a (run, step), newest first. For the audit/history view."""
    rows = conn.execute(
        f"SELECT {_STEP_COLS} FROM step_execution "
        "WHERE run_id = ? AND step_index = ? ORDER BY attempt_no DESC",
        (run_id, step_index),
    ).fetchall()
    return [_row_to_step(r) for r in rows]


def annotate_step(
    conn: sqlite3.Connection,
    step_execution_id: int,
    *,
    status: StepStatus | None = None,
    notes: str | None = None,
) -> None:
    """Update status / notes on an existing step attempt in place.

    Unlike `record_step`, this does NOT create a new attempt — it patches
    the row the user is actively looking at. Use for user metadata flips
    (Reject with a comment) that are not the result of re-running the
    step with new inputs.
    """
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes)
    if not sets:
        return
    params.append(step_execution_id)
    conn.execute(
        f"UPDATE step_execution SET {', '.join(sets)} WHERE id = ?",
        params,
    )


def delete_step_execution(conn: sqlite3.Connection, step_execution_id: int) -> None:
    """Explicit removal of one attempt. If it was the current one, the
    most recent surviving attempt (if any) is promoted to current.
    Cascades to its artifacts/edits in the DB; on-disk files are the caller's
    responsibility (see `er_automations.persistence.storage`).
    """
    row = conn.execute(
        "SELECT run_id, step_index, is_current FROM step_execution WHERE id = ?",
        (step_execution_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM step_execution WHERE id = ?", (step_execution_id,))
    if not row["is_current"]:
        return
    # promote the next-most-recent surviving attempt to current, if any.
    nxt = conn.execute(
        "SELECT id FROM step_execution WHERE run_id = ? AND step_index = ? "
        "ORDER BY attempt_no DESC LIMIT 1",
        (row["run_id"], row["step_index"]),
    ).fetchone()
    if nxt is not None:
        conn.execute("UPDATE step_execution SET is_current = 1 WHERE id = ?", (nxt["id"],))
