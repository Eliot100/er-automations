"""Asik xlsx reader.

The Asik monthly electricity export ships as a multi-sheet xlsx with a fixed,
slightly awkward layout:

* Rows 0-10 are metadata (report date, site, "תקופת דוח", reading dates,
  total row count). These rows have data only in columns 0-1.
* Rows 11-12 carry section headers (`פרטים`, `קבוצות`, tariff group labels).
* Row 13 is the actual column header row.
* Row 14 is a blank separator.
* Rows 15..N are consumption rows, one per (consumer, meter).

Sheet names in the January 2026 sample:
* `כפר הנשיא`                     — main consumption report
* `כפר הנשיא 2_1`                 — "page 2" / continuation (deferred; the
                                    spec only references one consumption page)
* `כפר הנשיא_SocialDiscounts ...` — social-discount eligibles (same layout)

The customer file has no dedicated `solar` sheet — solar accounting is done
against a separate input. We surface it as an empty frame with the expected
columns so downstream code can treat it uniformly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

HEADER_HINT = "מספר שורה"  # always the first column of the real header row
METADATA_LAST_ROW = 10
MAX_HEADER_SCAN_ROW = 20  # SocialDiscounts uses row 4; main sheet uses row 13

# Sheet name patterns. Asik renames sheets by report period each month;
# match by substring rather than literal equality so a Feb 2026 file with
# `כפר הנשיא_SocialDiscounts 02 02` still parses.
_MAIN_SHEET_HINTS = ("כפר הנשיא",)
_SOCIAL_HINT = "SocialDiscounts"
_PAGE2_HINT = "2_1"


@dataclass(slots=True)
class AsikReport:
    """Normalized output of a single Asik xlsx."""

    period: str | None  # 'YYYY-MM', derived from the report's reading dates
    consumption: pd.DataFrame
    solar: pd.DataFrame
    social_discounts: pd.DataFrame
    metadata: dict[str, str] = field(default_factory=dict)
    sheet_names: list[str] = field(default_factory=list)


def read_asik(path: str | Path) -> AsikReport:
    """Load an Asik monthly xlsx and return normalized frames.

    Raises FileNotFoundError if the path does not resolve. The parser is
    tolerant of extra trailing blank rows and of the page-2 / social-discount
    sheets being absent.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    # Use a context manager so the underlying file handle is released as soon
    # as we're done. Leaving it open kept the xlsx locked on Windows, which
    # later broke deleting the run's folder (REMOVE prior-data → WinError 32).
    with pd.ExcelFile(path) as xl:
        sheets = list(xl.sheet_names)

        main_name = _find_main_sheet(sheets)
        social_name = _find_first(sheets, _SOCIAL_HINT)

        consumption = _read_consumption_sheet(xl, main_name) if main_name else _empty_consumption()
        social = _read_consumption_sheet(xl, social_name) if social_name else _empty_consumption()
        metadata = _read_metadata(xl, main_name) if main_name else {}
        period = _derive_period(metadata)

    return AsikReport(
        period=period,
        consumption=consumption,
        solar=_empty_solar(),
        social_discounts=social,
        metadata=metadata,
        sheet_names=sheets,
    )


# ---------- sheet location ----------


def _find_main_sheet(sheets: list[str]) -> str | None:
    # Prefer the sheet that matches the main hint AND is NOT the page-2 or
    # social-discount sheet.
    for s in sheets:
        if _SOCIAL_HINT in s or _PAGE2_HINT in s:
            continue
        if any(h in s for h in _MAIN_SHEET_HINTS):
            return s
    return None


def _find_first(sheets: list[str], hint: str) -> str | None:
    for s in sheets:
        if hint in s:
            return s
    return None


# ---------- sheet → frame ----------


def _read_consumption_sheet(xl: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    raw = pd.read_excel(xl, sheet_name=sheet_name, header=None, dtype=object)
    header_row = _find_header_row(raw)
    if header_row is None:
        return _empty_consumption()
    header = raw.iloc[header_row].tolist()
    columns = [str(c).strip() if pd.notna(c) else f"_unnamed_{i}" for i, c in enumerate(header)]
    # Data is everything below; skip blank separator rows.
    body = raw.iloc[header_row + 1 :].copy()
    body.columns = columns
    # Drop rows where every renamed column is NaN (Asik pads with empties).
    body = body.dropna(how="all").reset_index(drop=True)
    # The first column (`מספר שורה`) is just a 1..N index; the integer Asik
    # writes there is the only thing distinguishing real rows from totals.
    # Keep rows whose first column parses as a positive integer.
    first_col = columns[0]
    body = body[body[first_col].apply(_looks_like_row_number)].reset_index(drop=True)
    return body


def _find_header_row(raw: pd.DataFrame) -> int | None:
    """Locate the row whose first cell is `מספר שורה` — the real header.

    Asik places this row at index 13 in the main consumption sheet but at
    index 4 in the social-discounts sheet, so we scan rather than hardcode.
    """
    limit = min(MAX_HEADER_SCAN_ROW, len(raw))
    for i in range(limit):
        cell = raw.iat[i, 0]
        if isinstance(cell, str) and cell.strip() == HEADER_HINT:
            return i
    return None


def _looks_like_row_number(v: Any) -> bool:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    try:
        n = int(v)
    except (TypeError, ValueError):
        return False
    return n > 0


def _empty_consumption() -> pd.DataFrame:
    # Empty frame with the columns most callers reach for. Downstream code
    # should still tolerate missing columns — Asik changes column order
    # between report types.
    return pd.DataFrame(
        columns=[
            "מספר שורה",
            "שם לקוח",
            "מספר מונה",
            "כתובת",
            "מספר חוזה \\ תקציב",
            "מספר בית",
            'דוא"ל',
            "תעריף",
            "סה\"כ כולל מע\"מ ₪",
            "קבוצת משתמשים",
        ]
    )


def _empty_solar() -> pd.DataFrame:
    return pd.DataFrame(columns=["מספר מונה", "שם לקוח", "kWh", "תעריף", "₪"])


# ---------- metadata + period ----------


def _read_metadata(xl: pd.ExcelFile, sheet_name: str) -> dict[str, str]:
    raw = pd.read_excel(xl, sheet_name=sheet_name, header=None, nrows=METADATA_LAST_ROW + 1)
    out: dict[str, str] = {}
    for _, row in raw.iterrows():
        key = row.iloc[0]
        val = row.iloc[1] if len(row) > 1 else None
        if pd.notna(key) and pd.notna(val):
            out[str(key).strip()] = str(val).strip()
    return out


_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _derive_period(metadata: dict[str, str]) -> str | None:
    """Pick the reading-period month from the metadata block.

    Prefer `תאריך קריאה נוכחית` (current reading) — that's the month the
    report is *for*. Falls back to `נערך ב` (report-prepared date) only as
    a last resort, since reports for January can be prepared in February.
    """
    for key in ("תאריך קריאה נוכחית", "תאריך קריאה קודמת", "נערך ב"):
        v = metadata.get(key)
        if not v:
            continue
        m = _DATE_RE.search(v)
        if m:
            _, mm, yyyy = m.groups()
            return f"{yyyy}-{mm}"
    return None
