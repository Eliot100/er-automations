"""FastAPI surface over the supervised runner.

The server is intentionally thin — it owns no business logic. Each
endpoint is a translation between HTTP payloads and runner / repository
calls. Authentication is deferred (single-user MVP); the future Outlook
flow plugs in at `current_user`.

Endpoints:

    GET  /healthz                              — liveness probe
    GET  /automations                          — registered automations
    POST /automations                          — register one
    GET  /runs                                 — list (optional filter by automation+period)
    POST /runs                                 — start a run (uploads xlsx + period)
    GET  /runs/{id}                            — current state (steps + statuses)
    POST /runs/{id}/steps/{idx}/execute        — run step idx, return its state
    POST /runs/{id}/steps/{idx}/approve        — advance
    POST /runs/{id}/steps/{idx}/edits          — submit edits, re-run step
    POST /runs/{id}/steps/{idx}/reject         — abort with notes
    GET  /runs/{id}/artifacts                  — list artifacts
    GET  /runs/{id}/artifacts/{aid}            — download a generated file

The platform exposes the runner; customer-specific manifests are
registered via `app.state.manifests[automation_key] = [Step, ...]`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from er_automations.persistence import models, storage
from er_automations.persistence.db import init_db
from er_automations.runtime import (
    Edit,
    PriorDataChoice,
    PriorDataPrompt,
    RunContext,
    Runner,
    Step,
)


# ---------- request / response shapes ----------


class EditIn(BaseModel):
    row_id: str
    column: str
    old_value: Any = None
    new_value: Any = None
    remember: bool = False


class RejectIn(BaseModel):
    step_execution_id: int
    notes: str | None = None


class AutomationIn(BaseModel):
    key: str
    name: str
    customer: str


class StartRunOut(BaseModel):
    run_id: int | None = None
    prior_data_prompt: dict[str, Any] | None = None


# ---------- factory ----------


def build_app(db_path: str | Path, data_root: str | Path) -> FastAPI:
    """Construct an app instance bound to a specific DB + data directory.

    Tests construct one per tmp_path; production uses `create_app()`.
    """
    app = FastAPI(title="ER Automations")
    app.state.db_path = str(db_path)
    app.state.data_root = Path(data_root)
    app.state.manifests: dict[str, list[Step]] = {}
    app.state.contexts: dict[int, RunContext] = {}  # run_id -> RunContext
    # Ensure schema exists; close the connection immediately — handlers
    # open per-request connections.
    init_db(db_path).close()
    _register_routes(app)
    return app


def create_app() -> FastAPI:
    """Production entry point — DB + data live under ./data/."""
    root = Path("./data")
    root.mkdir(parents=True, exist_ok=True)
    return build_app(root / "automations.sqlite", root)


# ---------- request-scoped helpers ----------


def _open_conn(app: FastAPI) -> sqlite3.Connection:
    conn = init_db(app.state.db_path)
    return conn


def _runner(app: FastAPI, conn: sqlite3.Connection) -> Runner:
    return Runner(conn, app.state.data_root)


def _current_user(app: FastAPI, conn: sqlite3.Connection) -> models.User:
    """Single-user placeholder. Replaced by Outlook/Entra session later."""
    return models.get_or_create_user(conn, "local")


# ---------- routes ----------


def _register_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/automations")
    def list_automations() -> list[dict[str, Any]]:
        conn = _open_conn(app)
        try:
            rows = conn.execute(
                "SELECT id, key, name, customer FROM automation ORDER BY key"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @app.post("/automations", status_code=201)
    def create_automation(body: AutomationIn) -> dict[str, Any]:
        conn = _open_conn(app)
        try:
            auto = models.register_automation(conn, body.key, body.name, body.customer)
            conn.commit()
            return asdict(auto)
        finally:
            conn.close()

    @app.post("/runs", response_model=StartRunOut)
    async def start_run(
        automation_key: str = Form(...),
        period: str = Form(...),
        prior_data: str | None = Form(None),
        upload: UploadFile = File(...),
    ) -> StartRunOut:
        conn = _open_conn(app)
        try:
            auto = _automation_or_404(conn, automation_key)
            manifest = app.state.manifests.get(automation_key)
            if manifest is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"no manifest registered for automation {automation_key!r}",
                )
            user = _current_user(app, conn)
            runner = _runner(app, conn)
            choice = _parse_prior_data(prior_data)
            result = runner.start_run(auto.id, period, user.id, manifest, prior_data=choice)
            if isinstance(result, PriorDataPrompt):
                return StartRunOut(
                    prior_data_prompt={
                        "automation_id": result.automation_id,
                        "period": result.period,
                        "prior_runs": [asdict(r) for r in result.prior_runs],
                    }
                )
            # Stash the uploaded xlsx under data/runs/<id>/inputs/ and
            # record its path on the run's RunContext.
            run = result.run
            payload = await upload.read()
            input_dir = app.state.data_root / "runs" / str(run.id) / "inputs"
            input_dir.mkdir(parents=True, exist_ok=True)
            input_path = input_dir / (upload.filename or "input.xlsx")
            input_path.write_bytes(payload)
            ctx = RunContext(
                run_id=run.id, period=period, user_id=user.id,
                inputs={"asik": str(input_path)},
            )
            app.state.contexts[run.id] = ctx
            conn.commit()
            return StartRunOut(run_id=run.id)
        finally:
            conn.close()

    @app.get("/runs")
    def list_runs(
        automation_key: str | None = None, period: str | None = None
    ) -> list[dict[str, Any]]:
        conn = _open_conn(app)
        try:
            sql = (
                "SELECT r.id, r.automation_id, r.user_id, r.period, r.status, "
                "r.current_step_index, r.created_at, a.key AS automation_key "
                "FROM run r JOIN automation a ON a.id = r.automation_id WHERE 1=1"
            )
            params: list[Any] = []
            if automation_key:
                sql += " AND a.key = ?"
                params.append(automation_key)
            if period:
                sql += " AND r.period = ?"
                params.append(period)
            sql += " ORDER BY r.created_at DESC, r.id DESC"
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    @app.get("/runs/{run_id}")
    def get_run(run_id: int) -> dict[str, Any]:
        conn = _open_conn(app)
        try:
            run = models.get_run(conn, run_id)
            if run is None:
                raise HTTPException(404, "run not found")
            steps = models.list_current_steps(conn, run_id)
            return {
                "run": asdict(run),
                "steps": [_step_dict(s) for s in steps],
            }
        finally:
            conn.close()

    @app.post("/runs/{run_id}/steps/{step_index}/execute")
    def execute_step(run_id: int, step_index: int) -> dict[str, Any]:
        conn = _open_conn(app)
        try:
            handle = _rehydrate_handle(app, conn, run_id)
            ctx = _ctx_for(app, run_id, handle.run.period)
            runner = _runner(app, conn)
            ex = runner.execute_step(handle, step_index, ctx)
            conn.commit()
            return _step_dict(ex)
        finally:
            conn.close()

    @app.post("/runs/{run_id}/steps/{step_index}/approve")
    def approve_step(run_id: int, step_index: int) -> dict[str, str]:
        conn = _open_conn(app)
        try:
            handle = _rehydrate_handle(app, conn, run_id)
            runner = _runner(app, conn)
            runner.approve(handle, step_index)
            conn.commit()
            return {"status": "ok"}
        finally:
            conn.close()

    @app.post("/runs/{run_id}/steps/{step_index}/edits")
    def submit_edits(
        run_id: int, step_index: int, edits: list[EditIn]
    ) -> dict[str, Any]:
        conn = _open_conn(app)
        try:
            handle = _rehydrate_handle(app, conn, run_id)
            ctx = _ctx_for(app, run_id, handle.run.period)
            runner = _runner(app, conn)
            ex = runner.submit_edits(
                handle, step_index, ctx,
                [Edit(**e.model_dump()) for e in edits],
            )
            conn.commit()
            return _step_dict(ex)
        finally:
            conn.close()

    @app.post("/runs/{run_id}/steps/{step_index}/reject")
    def reject_step(run_id: int, step_index: int, body: RejectIn) -> dict[str, str]:
        conn = _open_conn(app)
        try:
            handle = _rehydrate_handle(app, conn, run_id)
            runner = _runner(app, conn)
            runner.reject(handle, step_index, body.step_execution_id, notes=body.notes)
            conn.commit()
            return {"status": "ok"}
        finally:
            conn.close()

    @app.get("/runs/{run_id}/artifacts")
    def list_artifacts(run_id: int) -> list[dict[str, Any]]:
        conn = _open_conn(app)
        try:
            rows = conn.execute(
                "SELECT a.id, a.step_execution_id, a.kind, a.filename, a.path, a.size, a.mime "
                "FROM artifact a JOIN step_execution s ON s.id = a.step_execution_id "
                "WHERE s.run_id = ? ORDER BY a.id",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @app.get("/runs/{run_id}/artifacts/{artifact_id}")
    def download_artifact(run_id: int, artifact_id: int) -> FileResponse:
        conn = _open_conn(app)
        try:
            row = conn.execute(
                "SELECT a.filename, a.path, a.mime FROM artifact a "
                "JOIN step_execution s ON s.id = a.step_execution_id "
                "WHERE s.run_id = ? AND a.id = ?",
                (run_id, artifact_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "artifact not found")
            return FileResponse(
                row["path"], filename=row["filename"], media_type=row["mime"] or None
            )
        finally:
            conn.close()


# ---------- helpers ----------


def _automation_or_404(conn: sqlite3.Connection, key: str) -> models.Automation:
    row = conn.execute(
        "SELECT id, key, name, customer FROM automation WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"automation {key!r} not registered")
    return models.Automation(**dict(row))


def _rehydrate_handle(app: FastAPI, conn: sqlite3.Connection, run_id: int):
    from er_automations.runtime.runner import RunHandle

    run = models.get_run(conn, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    # We need the automation key to look up the manifest.
    row = conn.execute(
        "SELECT key FROM automation WHERE id = ?", (run.automation_id,)
    ).fetchone()
    key = row["key"]
    manifest = app.state.manifests.get(key)
    if manifest is None:
        raise HTTPException(409, f"no manifest registered for {key!r}")
    return RunHandle(run=run, manifest=manifest)


def _ctx_for(app: FastAPI, run_id: int, period: str) -> RunContext:
    ctx = app.state.contexts.get(run_id)
    if ctx is None:
        # USE / IGNORE flows that resumed an existing run do not preload
        # a context; construct an empty one (steps that need uploads
        # will read them from disk via `inputs`).
        ctx = RunContext(run_id=run_id, period=period)
        app.state.contexts[run_id] = ctx
    return ctx


def _step_dict(s: models.StepExecution) -> dict[str, Any]:
    return {
        "id": s.id,
        "run_id": s.run_id,
        "step_index": s.step_index,
        "attempt_no": s.attempt_no,
        "name": s.name,
        "status": s.status,
        "live_action": s.live_action,
        "verify_rows": s.verify_rows,
        "flagged_columns": s.flagged_columns,
        "notes": s.notes,
        "created_at": s.created_at,
        "is_current": s.is_current,
    }


def _parse_prior_data(s: str | None) -> PriorDataChoice | None:
    if s is None or s == "":
        return None
    try:
        return PriorDataChoice(s)
    except ValueError as e:
        raise HTTPException(400, f"invalid prior_data choice: {s!r}") from e
