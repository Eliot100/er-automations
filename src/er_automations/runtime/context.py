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
