"""Sprint 4 — FastAPI server.

Drives the runner end-to-end via the HTTP surface. Locked behavior:
- POST /automations registers an automation; GET /automations lists them.
- POST /runs uploads a file + period, registers the run, returns either
  a run_id OR a prior_data_prompt when priors exist.
- POST /runs/{id}/steps/{idx}/{execute,approve,edits,reject} drive the
  step lifecycle.
- GET  /runs/{id}/artifacts and download endpoint surface outputs.
"""

from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient

from er_automations.runtime import ArtifactRef, Edit, RunContext, Step, StepResult
from er_automations.server.app import build_app


class _ManualStep(Step):
    name = "manual"
    description = "verify step that needs an edit"

    def run(self, ctx: RunContext) -> StepResult:
        return StepResult(
            status="verify",
            live_action={"hi": "there"},
            verify_rows=[{"row_id": "1", "value": "before"}],
            flagged_columns=["value"],
        )

    def apply_edits(self, ctx: RunContext, edits: list[Edit]) -> StepResult:
        return StepResult(
            status="good",
            verify_rows=[{"row_id": e.row_id, "value": e.new_value} for e in edits],
            outputs=[ArtifactRef(filename="result.txt", payload=b"OK", mime="text/plain")],
        )


def _fresh_app(tmp_path: Path):
    app = build_app(tmp_path / "db.sqlite", tmp_path / "data")
    app.state.manifests["t.manual"] = [_ManualStep()]
    return app


def test_healthz(tmp_path) -> None:
    with TestClient(_fresh_app(tmp_path)) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_register_and_list_automations(tmp_path) -> None:
    with TestClient(_fresh_app(tmp_path)) as c:
        r = c.post(
            "/automations",
            json={"key": "t.manual", "name": "T", "customer": "Acme"},
        )
        assert r.status_code == 201
        r = c.get("/automations")
        assert r.status_code == 200
        keys = [a["key"] for a in r.json()]
        assert keys == ["t.manual"]


def test_full_run_flow(tmp_path) -> None:
    """Upload → execute step → edit → approve → list artifacts → download."""
    with TestClient(_fresh_app(tmp_path)) as c:
        c.post(
            "/automations",
            json={"key": "t.manual", "name": "T", "customer": "Acme"},
        )
        r = c.post(
            "/runs",
            data={"automation_key": "t.manual", "period": "2025-11"},
            files={"upload": ("in.xlsx", io.BytesIO(b"FAKE"), "application/octet-stream")},
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]
        assert run_id is not None

        # Run step 0 — should land in verify.
        r = c.post(f"/runs/{run_id}/steps/0/execute")
        assert r.status_code == 200
        step = r.json()
        assert step["status"] == "verify"
        assert step["attempt_no"] == 1

        # Submit an edit; the manual step's apply_edits emits an artifact.
        r = c.post(
            f"/runs/{run_id}/steps/0/edits",
            json=[{"row_id": "1", "column": "value", "old_value": "before", "new_value": "after"}],
        )
        assert r.status_code == 200
        step = r.json()
        assert step["status"] == "good"
        assert step["attempt_no"] == 2

        # Approve the last step → run completes.
        r = c.post(f"/runs/{run_id}/steps/0/approve")
        assert r.status_code == 200

        r = c.get(f"/runs/{run_id}")
        body = r.json()
        assert body["run"]["status"] == "completed"
        assert len(body["steps"]) == 1
        assert body["steps"][0]["status"] == "good"

        # The artifact written by apply_edits is listed and downloadable.
        r = c.get(f"/runs/{run_id}/artifacts")
        arts = r.json()
        assert len(arts) == 1
        assert arts[0]["filename"] == "result.txt"
        r = c.get(f"/runs/{run_id}/artifacts/{arts[0]['id']}")
        assert r.status_code == 200
        assert r.content == b"OK"


def test_run_lists_filtered_by_period(tmp_path) -> None:
    with TestClient(_fresh_app(tmp_path)) as c:
        c.post(
            "/automations",
            json={"key": "t.manual", "name": "T", "customer": "Acme"},
        )
        for period in ("2025-10", "2025-11"):
            c.post(
                "/runs",
                data={"automation_key": "t.manual", "period": period},
                files={"upload": ("in.xlsx", io.BytesIO(b""), "application/octet-stream")},
            )
        r = c.get("/runs", params={"automation_key": "t.manual", "period": "2025-11"})
        assert r.status_code == 200
        periods = {row["period"] for row in r.json()}
        assert periods == {"2025-11"}


def test_prior_data_prompt_round_trips_through_http(tmp_path) -> None:
    with TestClient(_fresh_app(tmp_path)) as c:
        c.post(
            "/automations",
            json={"key": "t.manual", "name": "T", "customer": "Acme"},
        )
        # First run lands as a fresh run_id.
        c.post(
            "/runs",
            data={"automation_key": "t.manual", "period": "2025-11"},
            files={"upload": ("in.xlsx", io.BytesIO(b""), "application/octet-stream")},
        )
        # Second run for the same period should surface the prior-data prompt.
        r = c.post(
            "/runs",
            data={"automation_key": "t.manual", "period": "2025-11"},
            files={"upload": ("in.xlsx", io.BytesIO(b""), "application/octet-stream")},
        )
        body = r.json()
        assert body["run_id"] is None
        assert body["prior_data_prompt"] is not None
        assert len(body["prior_data_prompt"]["prior_runs"]) == 1

        # Client picks IGNORE and resubmits.
        r = c.post(
            "/runs",
            data={
                "automation_key": "t.manual",
                "period": "2025-11",
                "prior_data": "ignore",
            },
            files={"upload": ("in.xlsx", io.BytesIO(b""), "application/octet-stream")},
        )
        assert r.json()["run_id"] is not None


def test_unknown_automation_is_404(tmp_path) -> None:
    with TestClient(_fresh_app(tmp_path)) as c:
        r = c.post(
            "/runs",
            data={"automation_key": "nope", "period": "2025-11"},
            files={"upload": ("in.xlsx", io.BytesIO(b""), "application/octet-stream")},
        )
        assert r.status_code == 404


def test_run_context_endpoint_round_trips(tmp_path) -> None:
    """GET /runs/{id}/context surfaces the JSON-safe blackboard, including
    the registered asik input path."""
    with TestClient(_fresh_app(tmp_path)) as c:
        c.post(
            "/automations",
            json={"key": "t.manual", "name": "T", "customer": "Acme"},
        )
        r = c.post(
            "/runs",
            data={"automation_key": "t.manual", "period": "2025-11"},
            files={"upload": ("in.xlsx", io.BytesIO(b"FAKE"), "application/octet-stream")},
        )
        run_id = r.json()["run_id"]

        r = c.get(f"/runs/{run_id}/context")
        assert r.status_code == 200
        ctx = r.json()
        assert ctx["run_id"] == run_id
        assert ctx["period"] == "2025-11"
        assert ctx["inputs"]["asik"].endswith("in.xlsx")
        # Unknown run id is a clean 404.
        assert c.get("/runs/99999/context").status_code == 404


def test_optional_solar_upload_is_registered_as_input(tmp_path) -> None:
    """A second file upload lands as ctx.inputs['solar'] alongside asik."""
    with TestClient(_fresh_app(tmp_path)) as c:
        c.post(
            "/automations",
            json={"key": "t.manual", "name": "T", "customer": "Acme"},
        )
        r = c.post(
            "/runs",
            data={"automation_key": "t.manual", "period": "2025-11"},
            files={
                "upload": ("in.xlsx", io.BytesIO(b"FAKE"), "application/octet-stream"),
                "solar_upload": ("solar.xlsx", io.BytesIO(b"SUN"), "application/octet-stream"),
            },
        )
        run_id = r.json()["run_id"]
        ctx = c.get(f"/runs/{run_id}/context").json()
        assert ctx["inputs"]["asik"].endswith("in.xlsx")
        assert ctx["inputs"]["solar"].endswith("solar_solar.xlsx")
