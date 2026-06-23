"""Filesystem side of the never-overwrite rule.

Outputs produced by a step execution live at:

    <root>/runs/<run_id>/<step_index>/<step_execution_id>/<filename>

Each attempt gets its own folder (keyed by `step_execution_id`), so two
attempts that produce the same filename never collide. The DB `artifact`
row records the path so the UI can serve it back.

Deleting a run removes the entire `runs/<run_id>/` subtree. Deleting a
single step execution removes just its leaf folder.
"""

from __future__ import annotations

import gc
import os
import shutil
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ArtifactRecord:
    id: int
    step_execution_id: int
    kind: str
    filename: str
    path: str
    size: int
    mime: str | None


def runs_root(data_root: str | Path) -> Path:
    return Path(data_root) / "runs"


def step_dir(data_root: str | Path, run_id: int, step_index: int, step_execution_id: int) -> Path:
    return runs_root(data_root) / str(run_id) / str(step_index) / str(step_execution_id)


def write_artifact(
    conn: sqlite3.Connection,
    data_root: str | Path,
    *,
    run_id: int,
    step_index: int,
    step_execution_id: int,
    filename: str,
    payload: bytes,
    kind: str = "output",
    mime: str | None = None,
    created_by_user_id: int | None = None,
) -> ArtifactRecord:
    """Write payload to the per-attempt folder and record the artifact row.

    The folder is created on demand. Two attempts that ship a file with the
    same `filename` get distinct paths because the folder is keyed by
    `step_execution_id`. If the same (attempt, filename) is written twice
    the existing file IS overwritten — that's within a single attempt, which
    is not what the never-overwrite rule is about.
    """
    folder = step_dir(data_root, run_id, step_index, step_execution_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    target.write_bytes(payload)
    cur = conn.execute(
        "INSERT INTO artifact (step_execution_id, kind, filename, path, size, mime, "
        "created_by_user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            step_execution_id,
            kind,
            filename,
            str(target),
            len(payload),
            mime,
            created_by_user_id,
        ),
    )
    return ArtifactRecord(
        id=int(cur.lastrowid),
        step_execution_id=step_execution_id,
        kind=kind,
        filename=filename,
        path=str(target),
        size=len(payload),
        mime=mime,
    )


def _on_rm_error(func, path, excinfo):  # type: ignore[no-untyped-def]
    """rmtree error handler: clear a read-only bit, then retry the op once.

    If the file is still unremovable (e.g. a live lock), the retry raises and
    propagates to `_rmtree_resilient`'s loop, which waits and tries again.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _rmtree_resilient(folder: Path) -> None:
    """`shutil.rmtree` that tolerates transient Windows file locks.

    A just-read xlsx can keep a handle open for a moment after the reader
    returns; on Windows that yields WinError 32 ("used by another process").
    Force a GC pass and retry a few times before giving up.
    """
    for attempt in range(5):
        try:
            shutil.rmtree(folder, onexc=_on_rm_error)
            return
        except OSError:
            gc.collect()  # drop any lingering ExcelFile / file objects
            time.sleep(0.2 * (attempt + 1))
    # Final attempt: let the error surface if the lock truly never released.
    shutil.rmtree(folder, onexc=_on_rm_error)


def delete_run_files(data_root: str | Path, run_id: int) -> None:
    """Remove the on-disk subtree for a run. The DB cascade is the caller's job."""
    folder = runs_root(data_root) / str(run_id)
    if folder.exists():
        _rmtree_resilient(folder)


def delete_step_files(
    data_root: str | Path, run_id: int, step_index: int, step_execution_id: int
) -> None:
    folder = step_dir(data_root, run_id, step_index, step_execution_id)
    if folder.exists():
        _rmtree_resilient(folder)
