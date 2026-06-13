"""Step protocol — the contract every automation step implements.

A step is a Python object with a `run(ctx) -> StepResult` and an
`apply_edits(ctx, edits) -> StepResult`. The default `apply_edits`
implementation merges the edits into the run context (`ctx.edits[step_name]`)
and re-invokes `run`. Steps that need finer-grained edit handling override.

StepResult is the only thing the runner persists. It carries:

* `status` — `good` (advance), `verify` (pause for review), `bad` (abort).
* `live_action` — small dict shown in the upper "live action" panel.
* `verify_rows` — editable spreadsheet payload.
* `flagged_columns` — which columns the user should review.
* `outputs` — filesystem artifacts (paths) the step produced.
* `notes` — free-text explanation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from er_automations.runtime.context import RunContext

StepStatus = Literal["good", "verify", "bad"]


@dataclass(slots=True)
class ArtifactRef:
    """One on-disk output produced by a step."""

    filename: str
    payload: bytes
    mime: str | None = None
    kind: str = "output"


@dataclass(slots=True)
class Edit:
    """A single cell edit submitted by the user against a step's verify_rows."""

    row_id: str
    column: str
    old_value: Any
    new_value: Any
    remember: bool = False


@dataclass(slots=True)
class StepResult:
    status: StepStatus
    live_action: dict[str, Any] | None = None
    verify_rows: list[dict[str, Any]] | None = None
    flagged_columns: list[str] | None = None
    outputs: list[ArtifactRef] = field(default_factory=list)
    notes: str | None = None


class Step(ABC):
    """Base class for steps in an automation manifest."""

    name: str
    description: str = ""

    @abstractmethod
    def run(self, ctx: "RunContext") -> StepResult: ...

    def apply_edits(self, ctx: "RunContext", edits: list[Edit]) -> StepResult:
        """Default: stash edits in the context and re-run.

        Subclasses can override to apply edits surgically (e.g. patch a
        DataFrame in place) and produce a different StepResult without
        re-executing everything upstream.
        """
        ctx.edits.setdefault(self.name, []).extend(edits)
        return self.run(ctx)
