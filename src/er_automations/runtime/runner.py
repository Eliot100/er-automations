"""Supervised runner.

The runner walks a manifest one step at a time and pauses after every step
so a human can approve / edit / reject. The DB is the source of truth for
"where is this run now"; the in-memory `RunHandle` is just a convenience
returned to the caller.

Start-of-run prior-data flow:

1. Caller invokes `runner.start_run(automation_id, period, user_id)`.
2. If `find_prior_runs` returns nothing, a fresh `run` row is created and
   the runner is ready to execute step 0.
3. If priors exist, the runner does NOT create the new run yet. It returns
   a `PriorDataPrompt` describing the priors. The caller surfaces it to the
   user, then re-invokes `start_run` with `prior_data=<choice>`:

   * `REMOVE`  — delete prior runs (DB cascade) and their on-disk files,
                 then create a fresh run.
   * `USE`     — adopt the newest prior run as the active run by flipping
                 its status back to `running`. No fresh execution; the
                 prior step_executions remain marked `is_current = 1`.
   * `IGNORE`  — leave priors alone and create a brand-new run alongside.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from er_automations.persistence import models, storage
from er_automations.runtime.context import RunContext
from er_automations.runtime.step import Edit, Step, StepResult


class PriorDataChoice(str, Enum):
    REMOVE = "remove"
    USE = "use"
    IGNORE = "ignore"


@dataclass(slots=True)
class PriorDataPrompt:
    """Returned by `start_run` when prior runs exist for (automation, period)."""

    automation_id: int
    period: str
    prior_runs: list[models.Run]


@dataclass(slots=True)
class RunHandle:
    run: models.Run
    manifest: list[Step]


class Runner:
    """Drives a manifest against the DB + filesystem.

    Single user, single in-flight step at a time. Concurrency is out of
    scope for the MVP; we treat the SQLite connection as the lock.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        data_root: str | Path,
    ) -> None:
        self.conn = conn
        self.data_root = Path(data_root)

    # ---------- run lifecycle ----------

    def start_run(
        self,
        automation_id: int,
        period: str,
        user_id: int | None,
        manifest: list[Step],
        *,
        prior_data: PriorDataChoice | None = None,
    ) -> RunHandle | PriorDataPrompt:
        """Begin a run, surfacing the prior-data prompt the first time around."""
        priors = models.find_prior_runs(self.conn, automation_id, period)
        if priors and prior_data is None:
            return PriorDataPrompt(
                automation_id=automation_id, period=period, prior_runs=priors
            )

        if priors and prior_data is PriorDataChoice.REMOVE:
            for r in priors:
                storage.delete_run_files(self.data_root, r.id)
                models.delete_run(self.conn, r.id)
            run = models.create_run(self.conn, automation_id, user_id, period)
        elif priors and prior_data is PriorDataChoice.USE:
            # Resume the newest prior run rather than creating a new one.
            chosen = priors[0]
            models.update_run_status(self.conn, chosen.id, "running")
            run = models.get_run(self.conn, chosen.id)
            assert run is not None
        else:
            # IGNORE, or no priors — create a fresh run.
            run = models.create_run(self.conn, automation_id, user_id, period)

        return RunHandle(run=run, manifest=manifest)

    # ---------- step lifecycle ----------

    def execute_step(
        self,
        handle: RunHandle,
        step_index: int,
        ctx: RunContext,
    ) -> models.StepExecution:
        """Run step `step_index`, persist the result, return the step_execution row."""
        step = handle.manifest[step_index]
        result = step.run(ctx)
        return self._persist(handle.run, step_index, step, result, ctx.user_id)

    def approve(self, handle: RunHandle, step_index: int) -> None:
        """Advance: move the run's current_step_index forward. Marks the
        run completed once the last step is approved.
        """
        next_index = step_index + 1
        if next_index >= len(handle.manifest):
            models.update_run_status(
                self.conn, handle.run.id, "completed", current_step_index=step_index
            )
        else:
            models.update_run_status(
                self.conn, handle.run.id, "paused", current_step_index=next_index
            )

    def submit_edits(
        self,
        handle: RunHandle,
        step_index: int,
        ctx: RunContext,
        edits: list[Edit],
    ) -> models.StepExecution:
        """Apply edits and re-run the step. A new step_execution attempt is recorded;
        the prior attempt is preserved with `is_current = 0` (see `record_step`).
        """
        step = handle.manifest[step_index]
        result = step.apply_edits(ctx, edits)
        return self._persist(handle.run, step_index, step, result, ctx.user_id)

    def reject(self, handle: RunHandle, step_index: int, notes: str | None = None) -> None:
        models.update_run_status(
            self.conn, handle.run.id, "aborted", current_step_index=step_index
        )
        if notes:
            # Record the rejection as an aborted step attempt so the audit
            # log shows *why* the user bailed.
            models.record_step(
                self.conn,
                handle.run.id,
                step_index,
                name=handle.manifest[step_index].name,
                status="bad",
                notes=notes,
            )

    # ---------- internals ----------

    def _persist(
        self,
        run: models.Run,
        step_index: int,
        step: Step,
        result: StepResult,
        user_id: int | None,
    ) -> models.StepExecution:
        execution = models.record_step(
            self.conn,
            run.id,
            step_index,
            name=step.name,
            status=result.status,
            live_action=result.live_action,
            verify_rows=result.verify_rows,
            flagged_columns=result.flagged_columns,
            notes=result.notes,
            created_by_user_id=user_id,
        )
        for art in result.outputs:
            storage.write_artifact(
                self.conn,
                self.data_root,
                run_id=run.id,
                step_index=step_index,
                step_execution_id=execution.id,
                filename=art.filename,
                payload=art.payload,
                kind=art.kind,
                mime=art.mime,
                created_by_user_id=user_id,
            )
        return execution
