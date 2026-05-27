"""
Flash report — generates a text receipt after a successful flash.

Can be saved to disk or displayed in the app.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def generate_report(
    part: str,
    module: str,
    channel: str,
    serial: str = '',
    sw_version: str = '',
) -> str:
    """Generate a human-readable flash report as a string.

    The report intentionally omits firmware filenames or any per-file
    detail — that information is a trade secret and must not leak to
    distributors via the saved report.
    """
    now = datetime.now()
    lines = [
        '═' * 50,
        '     MRS PLC FLASH REPORT',
        '     Styrestrøm AS',
        '═' * 50,
        '',
        f'  Date:         {now.strftime("%Y-%m-%d")}',
        f'  Time:         {now.strftime("%H:%M:%S")}',
        '',
        f'  Part:         {part}',
        f'  Module:       {module}',
        f'  Channel:      {channel}',
    ]

    if serial:
        lines.append(f'  Serial:       {serial}')
    if sw_version:
        lines.append(f'  SW Version:   {sw_version}')

    lines.extend([
        '',
        f'  Result:       OK',
        '',
        '═' * 50,
    ])

    return '\n'.join(lines)


def save_report(report: str, directory: Path | str | None = None) -> Path:
    """
    Save a flash report to a timestamped .txt file.

    Returns the path to the saved file.
    """
    now = datetime.now()
    filename = f'flash_report_{now.strftime("%Y%m%d_%H%M%S")}.txt'

    if directory:
        folder = Path(directory)
    else:
        folder = Path.home() / '.mrs_programmer' / 'reports'

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(report, encoding='utf-8')
    return path
