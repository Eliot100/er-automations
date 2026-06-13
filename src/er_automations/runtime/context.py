"""RunContext — shared blackboard between steps.

Steps read what previous steps put here and write their own derived data.
Nothing in `RunContext` is persisted automatically; if a step wants its
result to survive across attempts, it ships an `ArtifactRef` in `StepResult.outputs`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from er_automations.runtime.step import Edit


@dataclass(slots=True)
class RunContext:
    run_id: int
    period: str
    user_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    edits: dict[str, list[Edit]] = field(default_factory=dict)
    inputs: dict[str, str] = field(default_factory=dict)  # name -> path of uploaded input

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    # ---- persistence (Sprint 4 review #1) ----
    # The server keeps a per-run blackboard in memory between requests.
    # A restart erases it, so we mirror the JSON-serialisable part to disk
    # under data/runs/<run_id>/context.json. Steps that need to stash a
    # DataFrame should write a file artifact and put the *path* in `data`
    # — DataFrames themselves do not survive JSON.

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "period": self.period,
            "user_id": self.user_id,
            "data": _json_safe(self.data),
            "inputs": dict(self.inputs),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "RunContext":
        return cls(
            run_id=int(payload["run_id"]),
            period=str(payload["period"]),
            user_id=payload.get("user_id"),
            data=dict(payload.get("data") or {}),
            inputs=dict(payload.get("inputs") or {}),
        )


def _json_safe(d: dict[str, Any]) -> dict[str, Any]:
    """Drop non-serialisable values (DataFrames, bytes, etc.) — the on-disk
    context is a recovery aid, not an exact mirror.
    """
    import json as _json

    out: dict[str, Any] = {}
    for k, v in d.items():
        try:
            _json.dumps(v)
        except (TypeError, ValueError):
            continue
        out[k] = v
    return out
