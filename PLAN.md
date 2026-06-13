# Automation Platform — Plan

Two-folder split inside the current project:

- `ER-Automations/` — **the platform**. Generic, customer-agnostic. Step protocol, runner, persistence (SQLite + filesystem), connectors (Asik xlsx reader is a platform connector), UI shell (React, sketch CSS from `design/ER Automations.html`), FastAPI server.
- `Kfar-Hanasi-Automations/` — **the customer**. Imports the platform, registers the monthly electricity automation, declares Kfar's group taxonomy (תושבים/חברים/ענפים/עסקים), special-case adjustments (electrician 50 kWh, Inbar/Shmueli split, Nitzan street-light 10 kWh credit), and the consumer registry.

## Architecture decisions

- **Stack:** FastAPI (server) + SQLite (state) + React/Vite (UI) + Python domain code.
- **Storage:** SQLite stores runs / steps / artifacts metadata. Raw inputs and generated outputs live on disk under `./data/runs/<run-id>/`. DB stores filesystem paths.
- **Single user, no auth** for MVP. **Future:** Outlook / Microsoft Entra ID sign-in is on the roadmap — design the user/session boundary (a thin `current_user` dependency in FastAPI, user_id column on `run`) so we can plug it in cleanly without a rewrite. Don't build it now.
- **Run identity:** `(automation_id, period, status)`. Period required (e.g. `2025-11`). Multiple runs per (automation, period) are allowed; prior runs are **never silently overwritten**. When the user starts a new run and prior data exists, the platform pauses **once at the start of the run** and asks: **Remove / Use / Ignore** — then continues based on the answer. `Use` skips re-running and treats the prior run's state as current.
- **Append-only history.** Every step execution is an immutable row with `attempt_no`, `created_at`, `created_by_user_id`. Re-running a step inserts a new attempt and flips the old one's `is_current = 0`. Old attempts stay forever unless the user explicitly deletes them.
- **Output files on disk follow the same rule.** Each attempt writes to `data/runs/<run-id>/<step-index>/<step-execution-id>/...`. Nothing is overwritten.
- **Asik parser** lives on the platform side as a generic connector.
- **Group taxonomy** is customer-specific. Platform exposes a generic "group" primitive; Kfar configures the 4 labels.
- **Annual aggregation** reads from saved monthly runs plus new annual-only sheets (e.g. electric-company numbers for energy balance).

## Step protocol (what every automation step looks like)

A step is a Python class:

```python
class Step:
    name: str
    description: str
    def run(self, context: RunContext) -> StepResult: ...
    def apply_edits(self, context: RunContext, edits: dict) -> StepResult: ...
```

`StepResult` carries:

- `status`: `good` / `verify` / `bad`
- `live_action`: small dict shown in the upper "live action" panel (input/output fields)
- `verify_rows`: list of dicts shown as the editable spreadsheet
- `flagged_columns`: which columns the user should review/edit
- `outputs`: filesystem artifacts produced by the step (paths)
- `notes`: free-text explanation

The runner pauses after every step. The user clicks **Good** (advance), **Verify** (edit cells in verify_rows, runner re-runs the step with edits applied), or **Bad** (abort with notes).

## Database schema (SQLite, conceptual)

- `automation` — id, name, customer, manifest path
- `run` — id, automation_id, period (`YYYY-MM`), status (`running`/`paused`/`completed`/`aborted`), created_at, current_step_index
- `step_execution` — id, run_id, step_index, name, status, live_action_json, verify_rows_json, flagged_columns_json, notes, started_at, finished_at
- `artifact` — id, step_execution_id, kind (`input`/`output`), filename, path, size, mime
- `edit` — id, step_execution_id, row_id, column, old_value, new_value, remember (bool), created_at

`edits.remember=true` writes back to the consumer registry (the system gets smarter month over month).

## Execution roadmap

Each sprint ends with green tests.

### Sprint 1 — Bootstrap
- Create `ER-Automations/` and `Kfar-Hanasi-Automations/` Python packages with `pyproject.toml`.
- Set up pytest, ruff, basic CI hooks.
- Vite + React project for the UI.
- SQLite schema + Alembic-lite migrations (or plain SQL on init).
- Smoke test: import the package, create an in-memory DB, write/read a run.

### Sprint 2 — Core domain library (platform)
- `er_automations.connectors.asik` — load the Asik monthly xlsx, return normalized frames (`consumption`, `solar`, `social_discounts`).
- `er_automations.runtime.step` — Step base class + StepResult.
- `er_automations.runtime.runner` — runs a manifest, persists each StepResult, supports pause/resume/edit.
- `er_automations.runtime.context` — RunContext (shared blackboard between steps).
- Tests: Asik reader against the sample xlsx; runner with two fake steps.

### Sprint 3 — Kfar Hanasi domain
- `kfar_hanasi.registry` — consumer registry (consumer# → group + flags), loaded from yaml.
- `kfar_hanasi.adjustments` — applies the special cases declared in the registry, returns audit rows.
- `kfar_hanasi.groups` — splits frame by the 4 groups; emits the nonemail/PrintNotice bucket.
- `kfar_hanasi.writers` — produces Asik-style `_PRINT.xls`, shared-meters distribution, PrintNotice, solar monthly.
- Tests: end-to-end "given sample xlsx, produce the four `_PRINT` files," checking totals match a golden reference.

### Sprint 4 — FastAPI server + DB persistence
- `POST /runs` — start a run (upload xlsx + period).
- `GET /runs/{id}` — current state (steps + statuses).
- `POST /runs/{id}/steps/{idx}/approve` — advance.
- `POST /runs/{id}/steps/{idx}/edits` — apply edits, re-run step.
- `POST /runs/{id}/steps/{idx}/reject` — abort with notes.
- `GET /runs/{id}/artifacts/{aid}` — download a generated file.
- Tests: FastAPI TestClient driving a full Kfar monthly run from upload to download.

### Sprint 5 — UI shell
- Port the WF1 sketch from `design/ER Automations.html` to React components.
- Type rail (Electricity active, Water stubbed).
- Automation list reads from API.
- Steps column wires to step status pips.
- Live-action panel and verify-rows editable sheet.
- Approve / Verify (edit) / Reject buttons.
- Tests: Playwright/Vitest smoke — load app, upload sample xlsx, drive through all steps.

### Sprint 6 — Kfar monthly automation assembly
- Register the 6 steps from `אפיון.txt` as a manifest in `Kfar-Hanasi-Automations/`.
- Steps:
  1. Ingest Asik file (verify period + sheet shape)
  2. Parse consumption (flag anomalies)
  3. Apply special cases (audit row-by-row)
  4. Split into 4 groups + nonemail bucket (totals reconciliation)
  5. Solar monthly summary (tariff confirmation)
  6. Generate output files (totals reconciliation across all outputs)
- Full E2E test with the existing sample xlsx producing files numerically equivalent to `_print_files/*.xls`.

### Sprint 7+ (later)
- Electricity Annual automation (uses saved monthly runs + annual-only inputs).
- Solar annual.
- Powercom integration (TBD with user).
- Manual notices, discount eligibles (out of MVP scope).

## Open items deferred

- Powercom — what is it, what data does it add? Captured for Phase 7.
- `כפר הנשיא 2_1` sheet purpose — not in spec, ask before parsing.
- WhatsApp / email sending — manual notices automation, deferred.

## Folder layout

```
תשתיות חשמל כפר הנשיא/
├── ER-Automations/
│   ├── PLAN.md                     (this file)
│   ├── pyproject.toml
│   ├── src/er_automations/
│   │   ├── connectors/asik.py
│   │   ├── runtime/{step,runner,context}.py
│   │   ├── persistence/{db,models}.py
│   │   └── server/app.py           (FastAPI)
│   ├── ui/                         (Vite + React)
│   └── tests/
├── Kfar-Hanasi-Automations/
│   ├── pyproject.toml
│   ├── src/kfar_hanasi/
│   │   ├── manifest.py             (the 6-step monthly automation)
│   │   ├── registry.py
│   │   ├── adjustments.py
│   │   ├── groups.py
│   │   ├── writers.py
│   │   └── data/consumers.yaml
│   ├── tests/
│   └── fixtures/                   (sample Asik xlsx + golden outputs)
└── data/                           (gitignored, per-install runtime data)
    └── runs/<run-id>/...
```
