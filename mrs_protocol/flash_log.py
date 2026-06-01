"""
Flash log — records every flash operation to a local CSV file.

Log file location: same folder as the exe, named 'flash_log.csv'.

The header includes Distributor and Operator columns so a distributor's
local CSV is self-contained for accountability — even when HQ never sees
the centralized proxy log (e.g. in a support ticket with a screenshot
of the CSV). On first append against an older CSV that lacks those
columns, the file is rewritten with the new schema and existing rows
are padded with empty identity cells.
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path


_HEADER = (
    'Date', 'Time', 'Part', 'Module', 'Channel',
    'Serial', 'SW Version', 'Result', 'Error',
    'Distributor', 'Operator',
)


def _log_path() -> Path:
    """Return path to the log file (next to the exe or script)."""
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path.cwd()
    return base / 'flash_log.csv'


def write_entry(
    part: str,
    module: str,
    channel: str,
    success: bool,
    serial: str = '',
    sw_version: str = '',
    error_msg: str = '',
    distributor: str = '',
    operator: str = '',
) -> Path:
    """
    Append one row to the flash log CSV.

    Returns the path to the log file.
    """
    path = _log_path()
    _migrate_if_needed(path)

    file_exists = path.exists()
    with open(path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(_HEADER)
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d'),
            datetime.now().strftime('%H:%M:%S'),
            part,
            module,
            channel,
            serial,
            sw_version,
            'OK' if success else 'FAIL',
            error_msg,
            distributor,
            operator,
        ])

    return path


def _migrate_if_needed(path: Path) -> None:
    """Rewrite an older flash_log.csv to include Distributor + Operator.

    Existing rows get empty identity cells appended; the header on disk
    becomes the current ``_HEADER``. Idempotent — does nothing once the
    file is already on the new schema.
    """
    if not path.exists():
        return
    try:
        with open(path, 'r', newline='', encoding='utf-8') as f:
            rows = list(csv.reader(f))
    except OSError:
        return
    if not rows or tuple(rows[0]) == _HEADER:
        return

    new_width = len(_HEADER)
    new_rows: list[list[str]] = [list(_HEADER)]
    for row in rows[1:]:
        if len(row) < new_width:
            row = row + [''] * (new_width - len(row))
        new_rows.append(row)
    try:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerows(new_rows)
    except OSError:
        pass
