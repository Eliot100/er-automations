"""Sprint 1 smoke tests.

Behavior locked here:
- DB schema initialises in memory and on disk.
- User/automation upserts are idempotent.
- Step executions are APPEND-ONLY: re-recording inserts a new attempt and
  flips the prior attempt's `is_current` flag to 0.
- Multiple runs may coexist for the same (automation, period); prior runs
  are surfaced via `find_prior_runs` so the platform can present the
  remove/use/ignore prompt at run start.
- Explicit deletion (`delete_run`, `delete_step_execution`) cascades and
  promotes a surviving sibling to current.
"""

from __future__ import annotations

import er_automations
from er_automations.persistence.db import db_session, init_db
from er_automations.persistence.models import (
    create_run,
    delete_run,
    delete_step_execution,
    find_prior_runs,
    get_or_create_user,
    get_run,
    list_current_steps,
    list_step_attempts,
    record_step,
    register_automation,
    update_run_status,
)


def test_package_version() -> None:
    assert er_automations.__version__ == "0.1.0"


def test_init_db_in_memory_creates_all_tables() -> None:
    conn = init_db(":memory:")
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"user", "automation", "run", "step_execution", "artifact", "edit"} <= tables


def test_init_db_on_disk(tmp_path) -> None:
    db_path = tmp_path / "nested" / "test.db"
    conn = init_db(db_path)
    assert db_path.exists()
    conn.close()


def test_user_and_automation_idempotent(tmp_path) -> None:
    with db_session(tmp_path / "test.db") as conn:
        u1 = get_or_create_user(conn, "local", email="local@example.com")
        u2 = get_or_create_user(conn, "local")
        assert u1.id == u2.id

        a1 = register_automation(conn, "kfar.monthly", "Monthly", "Kfar HaNasi")
        a2 = register_automation(conn, "kfar.monthly", "Monthly", "Kfar HaNasi")
        assert a1.id == a2.id


def test_step_recording_is_append_only(tmp_path) -> None:
    """Re-running a step preserves the old attempt and promotes the new one."""
    with db_session(tmp_path / "test.db") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        run = create_run(conn, auto.id, user.id, "2025-11")

        first = record_step(
            conn,
            run.id,
            step_index=0,
            name="Ingest",
            status="verify",
            verify_rows=[{"row_id": "1", "value": "old"}],
            created_by_user_id=user.id,
        )
        assert first.attempt_no == 1
        assert first.is_current is True

        second = record_step(
            conn,
            run.id,
            step_index=0,
            name="Ingest",
            status="good",
            verify_rows=[{"row_id": "1", "value": "new"}],
            created_by_user_id=user.id,
        )
        assert second.attempt_no == 2
        assert second.is_current is True
        assert second.id != first.id

        attempts = list_step_attempts(conn, run.id, step_index=0)
        assert [a.attempt_no for a in attempts] == [2, 1]
        assert attempts[0].is_current is True
        assert attempts[1].is_current is False
        # old data is preserved verbatim
        assert attempts[1].verify_rows == [{"row_id": "1", "value": "old"}]

        current = list_current_steps(conn, run.id)
        assert len(current) == 1
        assert current[0].id == second.id


def test_multiple_runs_per_period_allowed(tmp_path) -> None:
    """No UNIQUE on (automation, period) — prior runs surface, not collide."""
    with db_session(tmp_path / "test.db") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        r1 = create_run(conn, auto.id, user.id, "2025-11")
        update_run_status(conn, r1.id, "aborted")
        r2 = create_run(conn, auto.id, user.id, "2025-11")

        priors = find_prior_runs(conn, auto.id, "2025-11", exclude_run_id=r2.id)
        assert [r.id for r in priors] == [r1.id]
        assert priors[0].status == "aborted"


def test_delete_run_cascades(tmp_path) -> None:
    with db_session(tmp_path / "test.db") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        run = create_run(conn, auto.id, user.id, "2025-11")
        record_step(conn, run.id, 0, "Ingest", "good", created_by_user_id=user.id)
        record_step(conn, run.id, 1, "Parse", "good", created_by_user_id=user.id)

        delete_run(conn, run.id)
        assert get_run(conn, run.id) is None
        assert list_current_steps(conn, run.id) == []


def test_delete_step_promotes_prior_attempt(tmp_path) -> None:
    """Deleting the current attempt promotes the next-most-recent one."""
    with db_session(tmp_path / "test.db") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        run = create_run(conn, auto.id, user.id, "2025-11")

        a1 = record_step(conn, run.id, 0, "Ingest", "verify", created_by_user_id=user.id)
        a2 = record_step(conn, run.id, 0, "Ingest", "good", created_by_user_id=user.id)
        assert a2.is_current and not _is_current(conn, a1.id)

        delete_step_execution(conn, a2.id)
        assert _is_current(conn, a1.id)
        attempts = list_step_attempts(conn, run.id, 0)
        assert [a.attempt_no for a in attempts] == [1]


def test_attempt_no_is_scoped_per_step_not_global(tmp_path) -> None:
    """attempt_no resets to 1 for each (run, step_index), independent of other steps."""
    with db_session(tmp_path / "test.db") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        run = create_run(conn, auto.id, user.id, "2025-11")
        s1 = record_step(conn, run.id, 0, "Ingest", "good", created_by_user_id=user.id)
        s2 = record_step(conn, run.id, 1, "Parse", "good", created_by_user_id=user.id)
        s3 = record_step(conn, run.id, 0, "Ingest", "verify", created_by_user_id=user.id)
        assert (s1.attempt_no, s2.attempt_no, s3.attempt_no) == (1, 1, 2)


def test_hebrew_payloads_round_trip(tmp_path) -> None:
    """Hebrew text in live_action/verify_rows/flagged_columns/notes survives JSON."""
    with db_session(tmp_path / "test.db") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "kfar.monthly", "חשבונית חודשית", "כפר הנשיא")
        run = create_run(conn, auto.id, user.id, "2025-11")
        record_step(
            conn,
            run.id,
            step_index=2,
            name="סיכום סולרי",
            status="verify",
            live_action={"תעריף": 0.55, "יחידה": "קוט״ש"},
            verify_rows=[{"row_id": "1", "שם": "ענבר כהן", "קטגוריה": "תושבים"}],
            flagged_columns=["תעריף", "שם"],
            notes="פיצול ענבר/שמואלי לבדוק",
            created_by_user_id=user.id,
        )
        loaded = list_step_attempts(conn, run.id, 2)[0]
        assert loaded.live_action == {"תעריף": 0.55, "יחידה": "קוט״ש"}
        assert loaded.verify_rows[0]["שם"] == "ענבר כהן"
        assert loaded.flagged_columns == ["תעריף", "שם"]
        assert loaded.notes == "פיצול ענבר/שמואלי לבדוק"


def test_delete_non_current_attempt_does_not_promote(tmp_path) -> None:
    with db_session(tmp_path / "test.db") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        run = create_run(conn, auto.id, user.id, "2025-11")
        a1 = record_step(conn, run.id, 0, "Ingest", "verify", created_by_user_id=user.id)
        a2 = record_step(conn, run.id, 0, "Ingest", "good", created_by_user_id=user.id)
        # a2 is current, a1 is not. Deleting a1 should not change which row is current.
        delete_step_execution(conn, a1.id)
        assert _is_current(conn, a2.id)
        assert [a.attempt_no for a in list_step_attempts(conn, run.id, 0)] == [2]


def test_delete_only_attempt_clears_current(tmp_path) -> None:
    with db_session(tmp_path / "test.db") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        run = create_run(conn, auto.id, user.id, "2025-11")
        only = record_step(conn, run.id, 7, "Solo", "verify", created_by_user_id=user.id)
        delete_step_execution(conn, only.id)
        assert list_step_attempts(conn, run.id, 7) == []
        assert 7 not in {s.step_index for s in list_current_steps(conn, run.id)}


def _is_current(conn, step_id: int) -> bool:
    row = conn.execute(
        "SELECT is_current FROM step_execution WHERE id = ?", (step_id,)
    ).fetchone()
    return bool(row["is_current"]) if row else False
