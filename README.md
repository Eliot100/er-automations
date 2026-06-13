# ER Automations — Platform

Supervised, step-by-step automation runner. Each automation step pauses with a "verify" view; nothing advances without the operator's approval.

## Layout

```
ER-Automations/
├── PLAN.md                 # full architecture + sprint roadmap
├── pyproject.toml
├── src/er_automations/
│   ├── persistence/        # SQLite schema, repository helpers
│   ├── runtime/            # (Sprint 2) Step protocol, runner
│   ├── connectors/         # (Sprint 2) Asik xlsx connector
│   └── server/             # (Sprint 4) FastAPI app
├── ui/                     # Vite + React + TS (Sprint 5)
└── tests/
```

## Sprint 1 — done

- Python package scaffold + pytest
- SQLite schema (`user`, `automation`, `run`, `step_execution`, `artifact`, `edit`)
- Repository round-trip helpers
- Vite + React + TS UI skeleton, builds clean
- 5 smoke tests green

## Run tests (Windows)

The parent folder's `venv` already has every dependency. Until PyPI access is reliable, use that one:

```pwsh
$env:PYTHONIOENCODING="utf-8"
$env:PYTHONPATH="src"
..\venv\Scripts\python -m pytest
```

## Run the UI dev server

```pwsh
cd ui
npm run dev
```
