"""
Flash log — records every flash operation to a local CSV file.

Log file location: same folder as the exe, named 'flash_log.csv'.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path


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
) -> Path:
    """
    Append one row to the flash log CSV.

    Returns the path to the log file.
    """
    path = _log_path()
    file_exists = path.exists()

    with open(path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                'Date', 'Time', 'Part', 'Module', 'Channel',
                'Serial', 'SW Version', 'Result', 'Error',
            ])
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
        ])

    return path
