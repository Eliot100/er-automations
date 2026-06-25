"""Sprint 2 — Asik connector, step protocol, runner, storage.

Behavior locked here:
- Asik reader yields normalised consumption / solar / social_discounts
  frames and a `YYYY-MM` period derived from the reading-date metadata.
- Runner persists every step attempt; re-running via `submit_edits`
  appends a new attempt and flips the prior one's `is_current` to 0.
- The start-of-run prior-data prompt fires only when priors exist and a
  choice has not yet been made; REMOVE wipes prior runs (DB + disk), USE
  resumes the newest prior in place, IGNORE creates a fresh run alongside.
- Artifacts written under `data/runs/<run_id>/<step_index>/<step_exec_id>/`
  never collide across attempts of the same step.
"""

from __future__ import annotations

from pathlib import Path

from er_automations.connectors.asik import read_asik
from er_automations.persistence.db import db_session
from er_automations.persistence.models import (
    create_run,
    get_or_create_user,
    list_current_steps,
    list_step_attempts,
    register_automation,
)
from er_automations.persistence.storage import runs_root, write_artifact
from er_automations.runtime import (
    ArtifactRef,
    Edit,
    PriorDataChoice,
    PriorDataPrompt,
    RunContext,
    Runner,
    Step,
    StepResult,
)

SAMPLE_XLSX = (
    Path(__file__).resolve().parents[2]
    / "חשמל כפר הנשיא 01.26 - מעודכן.xlsx"
)


# ---------- Asik reader ----------


def test_asik_reader_smoke() -> None:
    report = read_asik(SAMPLE_XLSX)
    assert report.period == "2026-01"
    # 3 sheets in the sample file: main, 2_1 (solar consumers), social-discounts.
    assert any("SocialDiscounts" in s for s in report.sheet_names)
    assert not report.consumption.empty
    # Asik reports its own row count under סך הכל שורות; the parser should
    # return exactly that many real rows (no totals, no padding).
    expected = int(report.metadata["סך הכל שורות"])
    assert len(report.consumption) == expected
    # The customer-facing columns we'll lean on downstream are present.
    cols = set(report.consumption.columns)
    for required in ("שם לקוח", "מספר מונה", "קבוצת משתמשים", 'דוא"ל'):
        assert required in cols, f"missing column {required!r}"
    # Social discounts are smaller but use the same layout.
    assert not report.social_discounts.empty
    assert "שם לקוח" in report.social_discounts.columns
    # Solar (from the dedicated solar-input file) is absent — empty frame, not None.
    assert report.solar.empty
    # page2 (2_1 tab — solar consumer rows) is present in the sample.
    assert not report.page2.empty
    assert "שם לקוח" in report.page2.columns


def test_asik_reader_missing_file_raises(tmp_path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        read_asik(tmp_path / "nope.xlsx")


def test_asik_reader_releases_file_handle(tmp_path) -> None:
    """Reading must not leave the xlsx locked.

    Regression for the REMOVE-prior-data 500 on Windows: read_asik left the
    pd.ExcelFile open, so deleting the run folder later raised WinError 32.
    Copy the sample into a temp dir, read it, and assert the whole dir can be
    removed immediately afterwards.
    """
    import shutil

    folder = tmp_path / "inputs"
    folder.mkdir()
    target = folder / "sample.xlsx"
    shutil.copy(SAMPLE_XLSX, target)

    report = read_asik(target)
    assert not report.consumption.empty  # actually read it

    # If the handle leaked, this raises PermissionError on Windows.
    shutil.rmtree(folder)
    assert not folder.exists()


# ---------- fake steps used by Runner tests ----------


class _Counter(Step):
    """Fake step. Returns `verify` on the first call, `good` after edits."""

    name = "counter"
    description = "increments a counter in the context"

    def run(self, ctx: RunContext) -> StepResult:
        n = ctx.get("count", 0) + 1
        ctx.set("count", n)
        return StepResult(
            status="verify",
            live_action={"count": n},
            verify_rows=[{"row_id": "1", "value": n}],
            flagged_columns=["value"],
        )

    def apply_edits(self, ctx: RunContext, edits: list[Edit]) -> StepResult:
        ctx.edits.setdefault(self.name, []).extend(edits)
        return StepResult(
            status="good",
            live_action={"edits_applied": len(edits)},
            verify_rows=[{"row_id": e.row_id, "value": e.new_value} for e in edits],
        )


class _Sink(Step):
    """Fake step. Always passes; emits a single output file."""

    name = "sink"
    description = "writes an artifact"

    def __init__(self, payload: bytes = b"hello") -> None:
        self.payload = payload

    def run(self, ctx: RunContext) -> StepResult:
        return StepResult(
            status="good",
            live_action={"size": len(self.payload)},
            outputs=[ArtifactRef(filename="out.txt", payload=self.payload, mime="text/plain")],
        )


# ---------- Runner ----------


def test_runner_executes_manifest(tmp_path) -> None:
    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        runner = Runner(conn, tmp_path / "data")

        handle = runner.start_run(auto.id, "2025-11", user.id, [_Counter(), _Sink()])
        assert not isinstance(handle, PriorDataPrompt)

        ctx = RunContext(run_id=handle.run.id, period="2025-11", user_id=user.id)
        s0 = runner.execute_step(handle, 0, ctx)
        assert s0.status == "verify"
        assert s0.live_action == {"count": 1}
        runner.approve(handle, 0)

        s1 = runner.execute_step(handle, 1, ctx)
        assert s1.status == "good"
        runner.approve(handle, 1)

        current = list_current_steps(conn, handle.run.id)
        assert [s.step_index for s in current] == [0, 1]


def test_runner_edit_and_rerun_creates_new_attempt(tmp_path) -> None:
    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        runner = Runner(conn, tmp_path / "data")
        handle = runner.start_run(auto.id, "2025-11", user.id, [_Counter()])
        assert not isinstance(handle, PriorDataPrompt)
        ctx = RunContext(run_id=handle.run.id, period="2025-11", user_id=user.id)

        first = runner.execute_step(handle, 0, ctx)
        assert first.status == "verify"
        assert first.attempt_no == 1

        second = runner.submit_edits(
            handle, 0, ctx, [Edit(row_id="1", column="value", old_value=1, new_value=42)]
        )
        assert second.attempt_no == 2
        assert second.status == "good"
        assert second.is_current

        attempts = list_step_attempts(conn, handle.run.id, 0)
        assert [a.attempt_no for a in attempts] == [2, 1]
        assert attempts[1].is_current is False  # old attempt preserved


# ---------- Prior-data prompt ----------


def test_prior_data_prompt_surfaces_when_priors_exist(tmp_path) -> None:
    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        # Pre-seed a prior run.
        prior = create_run(conn, auto.id, user.id, "2025-11")

        runner = Runner(conn, tmp_path / "data")
        prompt = runner.start_run(auto.id, "2025-11", user.id, [_Counter()])
        assert isinstance(prompt, PriorDataPrompt)
        assert [r.id for r in prompt.prior_runs] == [prior.id]


def test_prior_data_remove_deletes_priors_and_files(tmp_path) -> None:
    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        data_root = tmp_path / "data"

        # Seed a prior run with an artifact on disk.
        prior = create_run(conn, auto.id, user.id, "2025-11")
        prior_id = prior.id
        runner = Runner(conn, data_root)
        prior_handle = type("H", (), {"run": prior, "manifest": [_Sink()]})()
        ctx = RunContext(run_id=prior.id, period="2025-11", user_id=user.id)
        runner.execute_step(prior_handle, 0, ctx)
        assert any((runs_root(data_root) / str(prior_id)).rglob("out.txt"))

        # New run with REMOVE — prior gone from DB and disk; fresh attempt #1.
        # (SQLite may reuse the integer id after DELETE, so prove it via DB state.)
        handle = runner.start_run(
            auto.id, "2025-11", user.id, [_Counter()], prior_data=PriorDataChoice.REMOVE
        )
        assert not isinstance(handle, PriorDataPrompt)
        assert not (runs_root(data_root) / str(prior_id)).exists()
        # The handle's run has no step_executions yet — proving it's a fresh row.
        assert list_current_steps(conn, handle.run.id) == []
        s0 = runner.execute_step(handle, 0, ctx=RunContext(handle.run.id, "2025-11", user.id))
        assert s0.attempt_no == 1


def test_prior_data_use_resumes_prior_run(tmp_path) -> None:
    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        prior = create_run(conn, auto.id, user.id, "2025-11")
        runner = Runner(conn, tmp_path / "data")
        # Mark the prior as paused (the realistic resume case — user
        # walked away mid-run). USE explicitly refuses `aborted` and
        # `completed` priors; those are tested below.
        from er_automations.persistence.models import get_run, update_run_status

        update_run_status(conn, prior.id, "paused")

        handle = runner.start_run(
            auto.id, "2025-11", user.id, [_Counter()], prior_data=PriorDataChoice.USE
        )
        assert not isinstance(handle, PriorDataPrompt)
        assert handle.run.id == prior.id
        reloaded = get_run(conn, prior.id)
        assert reloaded is not None and reloaded.status == "running"


def test_prior_data_use_refuses_completed_prior(tmp_path) -> None:
    """Sprint 2 review #2: USE must not silently downgrade `completed`."""
    import pytest

    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        prior = create_run(conn, auto.id, user.id, "2025-11")
        from er_automations.persistence.models import update_run_status

        update_run_status(conn, prior.id, "completed")
        runner = Runner(conn, tmp_path / "data")
        with pytest.raises(ValueError, match="completed"):
            runner.start_run(
                auto.id, "2025-11", user.id, [_Counter()],
                prior_data=PriorDataChoice.USE,
            )


def test_reject_patches_current_attempt_in_place(tmp_path) -> None:
    """Sprint 2 review #1: reject annotates the existing row, not a new one."""
    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        runner = Runner(conn, tmp_path / "data")
        handle = runner.start_run(auto.id, "2025-11", user.id, [_Counter()])
        assert not isinstance(handle, PriorDataPrompt)
        ctx = RunContext(handle.run.id, "2025-11", user.id)
        attempt = runner.execute_step(handle, 0, ctx)
        # The verify_rows the user saw should still be present on the row
        # after rejection — we just annotate it.
        runner.reject(handle, 0, attempt.id, notes="numbers look off")
        attempts = list_step_attempts(conn, handle.run.id, 0)
        assert len(attempts) == 1
        assert attempts[0].status == "bad"
        assert attempts[0].notes == "numbers look off"
        assert attempts[0].verify_rows == [{"row_id": "1", "value": 1}]


def test_prior_data_ignore_creates_fresh_run_alongside(tmp_path) -> None:
    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        prior = create_run(conn, auto.id, user.id, "2025-11")
        runner = Runner(conn, tmp_path / "data")
        handle = runner.start_run(
            auto.id, "2025-11", user.id, [_Counter()], prior_data=PriorDataChoice.IGNORE
        )
        assert not isinstance(handle, PriorDataPrompt)
        assert handle.run.id != prior.id
        # Both runs exist.
        from er_automations.persistence.models import find_prior_runs

        priors = find_prior_runs(conn, auto.id, "2025-11", exclude_run_id=handle.run.id)
        assert [r.id for r in priors] == [prior.id]


# ---------- Storage ----------


def test_artifacts_from_two_attempts_do_not_collide(tmp_path) -> None:
    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        data_root = tmp_path / "data"
        runner = Runner(conn, data_root)
        handle = runner.start_run(auto.id, "2025-11", user.id, [_Sink(b"A"), _Sink(b"B")])
        assert not isinstance(handle, PriorDataPrompt)
        ctx = RunContext(handle.run.id, "2025-11", user.id)

        # Same step_index 0 twice with different payloads.
        first = runner.execute_step(handle, 0, ctx)
        second = runner.submit_edits(handle, 0, ctx, [])
        assert first.id != second.id

        paths = [
            r["path"]
            for r in conn.execute(
                "SELECT path FROM artifact WHERE step_execution_id IN (?, ?) ORDER BY id",
                (first.id, second.id),
            ).fetchall()
        ]
        assert len(paths) == 2
        assert paths[0] != paths[1]
        assert all(Path(p).exists() for p in paths)
        # Each attempt's folder is keyed by step_execution_id.
        assert str(first.id) in paths[0]
        assert str(second.id) in paths[1]


def test_write_artifact_records_size_and_path(tmp_path) -> None:
    from er_automations.persistence.models import record_step

    with db_session(tmp_path / "db.sqlite") as conn:
        user = get_or_create_user(conn, "local")
        auto = register_automation(conn, "x.y", "X", "Acme")
        run = create_run(conn, auto.id, user.id, "2025-11")
        step = record_step(conn, run.id, 0, "S", "good", created_by_user_id=user.id)
        rec = write_artifact(
            conn,
            tmp_path / "data",
            run_id=run.id,
            step_index=0,
            step_execution_id=step.id,
            filename="x.bin",
            payload=b"\x00\x01\x02",
        )
        assert rec.size == 3
        assert Path(rec.path).read_bytes() == b"\x00\x01\x02"
