# Sprint 2 Handoff

Paste this into a fresh chat to pick up where we left off.

---

## What this project is

Building a **supervised automation platform** for Kfar HaNasi (electricity infrastructure for a kibbutz). The user gets a monthly Asik xlsx, runs a step-by-step workflow with human verification at every step, and produces invoice files for accounting.

Two folders inside `C:\Users\eli40\OneDrive\Desktop\root\projects\תשתיות חשמל כפר הנשיא\`:

- **`ER-Automations/`** — generic platform. Step protocol, runner, persistence, Asik connector, UI shell.
- **`Kfar-Hanasi-Automations/`** — customer config. Assembles the 6-step monthly automation, owns the 4 consumer groups (תושבים/חברים/ענפים/עסקים), special-case adjustments.

Full plan: `ER-Automations/PLAN.md`. Read it before doing anything else.

## Where we are

**Sprint 1: DONE.** 16/16 tests green.

Built:
- SQLite schema (6 tables: user, automation, run, step_execution, artifact, edit)
- Repository helpers in `er_automations.persistence.{db,models}`
- Customer-side `Group` enum
- Vite + React + TS skeleton (vanilla — sketch CSS not yet ported)

## Architectural rules (LOCKED — don't relitigate)

1. **Stack:** FastAPI + SQLite + React/Vite. Python 3.11+. Single-user, no auth (yet).
2. **Folder split:** platform is vendor-agnostic; customer assembles platform components into specific automations.
3. **Run identity:** `(automation_id, period YYYY-MM)`. Period is required.
4. **Append-only history. NEVER auto-overwrite.** Multiple runs allowed per (automation, period); multiple step_execution rows allowed per (run, step). Each row has `attempt_no`, `created_at`, `created_by_user_id`. Old data stays unless the user explicitly deletes it.
5. **Start-of-run prompt:** When a new run starts and prior data exists for that (automation, period), platform pauses **once** and asks **Remove / Use / Ignore**. `Use` skips re-running and treats prior state as current. Fires once at the start of the run, not per step.
6. **Output files on disk:** each attempt writes to `data/runs/<run-id>/<step-index>/<step-execution-id>/...`. Never overwritten.
7. **Outlook auth later.** The `user_id` columns exist; `get_or_create_user("local")` is the placeholder. Don't build auth now.
8. **Asik xlsx parser** lives on the platform side as a generic connector.
9. **The 4 groups** are customer-specific (Kfar enum), not platform primitives.
10. **No ORM.** Plain `sqlite3` + dataclasses in `er_automations.persistence.models`. Keep it that way unless it gets painful.

## What Sprint 2 must deliver

The plan calls Sprint 2 **"Core domain (Asik connector + runtime)"**. Specifically:

### 2A — Asik connector (`er_automations/connectors/asik.py`)

Read the monthly Asik xlsx, return normalized DataFrames:
- `consumption` (the main per-consumer rows)
- `solar` (solar systems sheet)
- `social_discounts` (parsed but flagged unused)

Reference data is at `../חשמל כפר הנשיא 01.26 - מעודכן.xlsx` (relative to project root). 3 sheets, sheet names: `כפר הנשיא`, `כפר הנשיא 2_1`, `כפר הנשיא_SocialDiscounts 01 01`. The notebook `kfar_hanasi_electricity.ipynb` has prior parsing code worth glancing at (but it's exploratory, not a blueprint).

Each row should carry at least: meter#, consumer#, consumer name, category (raw label), kWh, ₪ pre-VAT, ₪ post-VAT, period, email-or-`nonemail`. Schema TBD when looking at the sheet.

### 2B — Step protocol (`er_automations/runtime/step.py`)

```python
@dataclass
class StepResult:
    status: Literal['good','verify','bad']
    live_action: dict[str, Any] | None
    verify_rows: list[dict[str, Any]] | None
    flagged_columns: list[str] | None
    outputs: list[ArtifactRef]
    notes: str | None

class Step(ABC):
    name: str
    description: str
    @abstractmethod
    def run(self, ctx: RunContext) -> StepResult: ...
    def apply_edits(self, ctx: RunContext, edits: list[Edit]) -> StepResult:
        # default: re-run with edits merged into context
        ...
```

### 2C — Runner (`er_automations/runtime/runner.py`)

- Loads a manifest (ordered list of Step instances)
- Executes step N, persists the result, **pauses**
- Reacts to: approve (advance), edit (re-run step with edits applied), reject (abort)
- Implements the start-of-run **Remove / Use / Ignore** prompt:
  - On `start_run(automation_id, period)`: calls `find_prior_runs`. If non-empty, emits a synthetic `PriorDataDecision` step. User picks an option. Runner acts (delete prior runs / load prior state as current / leave prior runs alone) and then runs the manifest from step 0.
- Uses `record_step` after every step → DB.
- Uses a new `storage.write_artifact(run_id, step_index, step_execution_id, filename, bytes)` helper to write outputs to `data/runs/<run-id>/<step-index>/<step-execution-id>/<filename>` and record an `artifact` row. **Add this helper in Sprint 2** — it's the disk-side mirror of the "never overwrite" rule.

### 2D — RunContext (`er_automations/runtime/context.py`)

A shared blackboard between steps. Holds the parsed Asik frames (after step 2), the chosen group splits (after step 4), etc. Pickled to disk between runs is fine; not a Sprint 2 goal.

### Tests for Sprint 2

End with these green:

1. **Asik reader smoke**: feed the sample xlsx, get 3 DataFrames with the expected sheets and at least a sane row count.
2. **Step+Runner unit**: two fake steps, runner executes step 0 → pause → approve → step 1 → pause → approve → done. DB shows 2 step_executions, both is_current.
3. **Edit-and-rerun**: fake step where `apply_edits` produces a different result; assert a second attempt row was created and is now current.
4. **PriorDataDecision flow**: pre-seed a prior run; new `start_run` surfaces the decision step; pick `Remove` → prior run is deleted, fresh attempts start at attempt_no=1.
5. **PriorDataDecision: Use path**: pick `Use` → no fresh execution, prior run's current step_executions are referenced as current for the new run (exact mechanism TBD — could be a new run that references the old, or could be "resume the old run by reverting its status to running"; I lean toward the latter — discuss before implementing).
6. **Artifact disk write never collides**: write the same filename in two attempts; both files exist on disk under different paths; DB has 2 artifact rows.

### Files to create in Sprint 2

```
ER-Automations/src/er_automations/
├── runtime/
│   ├── __init__.py
│   ├── step.py        # Step, StepResult, Edit, ArtifactRef
│   ├── runner.py      # Runner with pause/approve/reject + prior-data flow
│   └── context.py     # RunContext
├── connectors/
│   ├── __init__.py
│   └── asik.py        # read_asik(path) -> AsikReport(consumption, solar, social_discounts)
└── persistence/
    └── storage.py     # write_artifact, delete_run_files
```

## How to run things

**All tests** (from project root):

```pwsh
cd ER-Automations
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="src"
..\venv\Scripts\python -m pytest -v

cd ..\Kfar-Hanasi-Automations
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="src;..\ER-Automations\src"
..\venv\Scripts\python -m pytest -v
```

**Important env quirks (Windows):**

- `PYTHONIOENCODING=utf-8` is required — Hebrew filenames/paths blow up the default cp1252.
- PyPI was flaky during Sprint 1, so packages are NOT pip-installed. The parent `..\venv\` already has every dep (fastapi, pandas, openpyxl, xlrd, pyyaml, pydantic, uvicorn, httpx, pytest). Use it via `PYTHONPATH` until a clean venv works.

**UI:**

```pwsh
cd ER-Automations\ui
npm run dev      # vanilla Vite scaffold for now — sketch CSS port is Sprint 5
```

## Open questions Sprint 2 needs to resolve

Ask the user before implementing each of these:

1. **`Use` semantics, concretely:** when the user picks "Use" on the start-of-run prompt, do we (a) revert the prior run's status from `aborted`/`paused` back to `running` and resume it, or (b) create a new run that copies the prior run's step_executions as references? I lean (a) — simpler, less duplication, matches "Use the old result." But it means deleted prior runs… wait, you can't `Use` a deleted run. So this only applies to prior runs that exist. Confirm.
2. **Asik schema specifics:** what are the actual column names in the `כפר הנשיא` sheet? The notebook references `קבוצת משתמשים` — need to see the real header row to write a robust parser. Open `kfar_hanasi_electricity.ipynb` cell 1 output or just read the first 5 rows of the xlsx.
3. **`כפר הנשיא 2_1` sheet** — what is it? אפיון.txt mentions only 3 sheets total but the names don't match the spec exactly. Ignore for MVP unless user clarifies?
4. **Solar sheet:** the spec says the solar summary should "contain entry/check of tariffs." Are tariffs stored anywhere or always entered by hand each month? Probably a `data/tariffs.yaml` that the user can confirm/edit each month — but ask.

## File map (post Sprint 1)

```
תשתיות חשמל כפר הנשיא/
├── ER-Automations/                       ← PLATFORM
│   ├── PLAN.md                            (full architecture + sprint roadmap)
│   ├── README.md
│   ├── HANDOFF.md                         (this file)
│   ├── pyproject.toml
│   ├── src/er_automations/
│   │   ├── __init__.py
│   │   └── persistence/
│   │       ├── __init__.py
│   │       ├── db.py                      (SQLite schema + session helpers)
│   │       └── models.py                  (dataclasses + repository fns)
│   ├── tests/test_smoke.py                (12 tests, all green)
│   └── ui/                                (Vite+React+TS, vanilla scaffold)
├── Kfar-Hanasi-Automations/              ← CUSTOMER
│   ├── pyproject.toml
│   ├── src/kfar_hanasi/
│   │   ├── __init__.py
│   │   └── groups.py                      (4-group enum + label matcher)
│   └── tests/test_smoke.py                (4 tests, all green)
├── design/
│   ├── ER Automations.html                (the sketch — UI direction)
│   └── Wireframes.html                    (alt layouts)
├── אפיון.txt                              (the canonical spec — Hebrew)
├── מסמך תכנון והיתכנות.docx                (planning doc — automations 1, 2.1–2.5, 7–9)
├── חשמל כפר הנשיא 01.26 - מעודכן.xlsx     (sample Asik file — Sprint 2 input)
├── _print_files/                          (reference output files)
├── kfar_hanasi_electricity.ipynb         (scratch notebook — exploratory)
└── venv/                                  (parent venv, has all deps)
```

Files in the project root above `_print_files/`, the notebook, and old `inspect_*.py`, `tkinterApp.py`, `gradio_*.py`, `simple_excel_viewer.py`, `excel_viewer.html`, `example_app/` are pre-existing experiments and not part of the new platform. Don't delete them without asking.

## Tip for the new chat

The system has a memory file at `~/.claude/projects/<projhash>/memory/project_kfar_hanasi_platform.md` that captures the architecture summary. A fresh Claude should pick it up automatically. If anything's confusing, point them at `PLAN.md` first.
