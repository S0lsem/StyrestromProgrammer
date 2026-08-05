"""
MRS Console Flasher wrapper.

Drives Applics Studio's ``MRS_Developers_Studio_Console.exe`` as a
subprocess so the GUI does not have to reimplement the proprietary
per-block CRC of the MRS bootloader. The flasher is a self-contained
binary that auto-detects the PCAN-USB adapter, scans for the connected
PLC, verifies article-number compatibility against the .s19, and
performs the flash with the correct CRC.

Secrecy: the flasher only accepts a real file path as input. We write
the .s19 into a fresh per-flash subdirectory of %TEMP% (user-private
on NTFS by default) and remove the directory unconditionally on exit.
The on-disk exposure window is bounded by the flash duration (~30 s).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .s19_parser import Firmware

log = logging.getLogger(__name__)

_FLASHER_EXE_NAME = 'MRS_Developers_Studio_Console.exe'
# The newer .NET "Applics Flasher" — unlike the classical console above, it
# speaks CAN FD, so it can catch a *programmed* module that boots at 500k/2000k.
_NET_FLASHER_EXE_NAME = 'CAN_Flasher_NET_Console.exe'

_PROGRESS_RE     = re.compile(r'\]\s*(\d+)\s*%')   # matches ']45%' and '] 45 %' (.NET)
_SCAN_HEADER_RE  = re.compile(r'Module\(s\) found')
_SCAN_LINE_RE    = re.compile(r'^(\d+)\s+(NO PROG|PROG|OK)\s*:\s*(.+)$')
_FINISHED_RE     = re.compile(
    r'Programm\s+finshed:\s+0x([0-9A-Fa-f]+)\s+\((\d+)\):\s*(.*)'
)

# .NET "Applics Flasher" module-info lines (printed before the reflash
# overwrites the module), used to show what was on the unit beforehand.
_NET_NAME_RE    = re.compile(r'\bName:\s*(\S.*)')
_NET_SERIAL_RE  = re.compile(r'Serial number:\s*(\d+)')
_NET_APPVER_RE  = re.compile(r'App Version:\s*(\S.*)')


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class FlashResult:
    success:        bool
    exit_code:      int
    serial:         str = ''   # parsed from SCAN line, e.g. '71895'
    module_label:   str = ''   # e.g. 'NO PROG :  uSPS-CAN : Modulname : n/a'
    error_code:     int = 0    # the 0xNN code from "Programm finshed: 0x.."
    error_message:  str = ''   # the trailing text after the code
    output:         str = ''   # full captured stdout for the GUI log panel


# ---------------------------------------------------------------------------
# Exe discovery
# ---------------------------------------------------------------------------

def find_console_flasher() -> Path:
    """Locate the MRS console flasher on this machine.

    Search order:
      1. ``MRS_FLASHER_EXE`` environment variable.
      2. Bundled next to our exe (``<exe dir>/ConsoleFlasher/...``) when
         frozen by PyInstaller.
      3. Newest ApplicsStudio install under ``%LOCALAPPDATA%``.
      4. Recursive search under Program Files (slow fallback).

    Raises FileNotFoundError if the exe cannot be located anywhere.
    """
    override = os.environ.get('MRS_FLASHER_EXE')
    if override:
        p = Path(override)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f'MRS_FLASHER_EXE points at non-existent file: {p}'
        )

    if getattr(sys, 'frozen', False):
        # PyInstaller one-file mode extracts datas to ``sys._MEIPASS``; one-dir
        # mode leaves them next to the exe. Check both so the spec can switch
        # between modes without code changes.
        for root in (getattr(sys, '_MEIPASS', None), Path(sys.executable).parent):
            if not root:
                continue
            bundled = Path(root) / 'ConsoleFlasher' / _FLASHER_EXE_NAME
            if bundled.is_file():
                return bundled

    appdata = os.environ.get('LOCALAPPDATA')
    if appdata:
        candidates = sorted(
            Path(appdata).glob(
                f'ApplicsStudio/app-*/Tools/ConsoleFlasher/{_FLASHER_EXE_NAME}'
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for c in candidates:
            if c.is_file():
                return c

    for root in ('C:/Program Files', 'C:/Program Files (x86)'):
        rp = Path(root)
        if not rp.exists():
            continue
        for c in rp.rglob(_FLASHER_EXE_NAME):
            if c.is_file():
                return c

    raise FileNotFoundError(
        f'{_FLASHER_EXE_NAME} not found. Install MRS Applics Studio or '
        'set MRS_FLASHER_EXE to its full path.'
    )


def find_net_flasher() -> Path:
    """Locate the newer CAN-FD-capable Applics Flasher (.NET console).

    Same search strategy as :func:`find_console_flasher`, but for
    ``CAN_Flasher_NET_Console.exe`` under ``Tools/ConsoleFlasherNet``. Used to
    flash modules whose firmware has switched them to CAN FD (500k/2000k), which
    the classical console cannot reach.
    """
    override = os.environ.get('MRS_NET_FLASHER_EXE')
    if override:
        p = Path(override)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f'MRS_NET_FLASHER_EXE points at non-existent file: {p}'
        )

    if getattr(sys, 'frozen', False):
        for root in (getattr(sys, '_MEIPASS', None), Path(sys.executable).parent):
            if not root:
                continue
            bundled = Path(root) / 'ConsoleFlasherNet' / _NET_FLASHER_EXE_NAME
            if bundled.is_file():
                return bundled

    appdata = os.environ.get('LOCALAPPDATA')
    if appdata:
        candidates = sorted(
            Path(appdata).glob(
                f'ApplicsStudio/app-*/Tools/ConsoleFlasherNet/{_NET_FLASHER_EXE_NAME}'
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for c in candidates:
            if c.is_file():
                return c

    for root in ('C:/Program Files', 'C:/Program Files (x86)'):
        rp = Path(root)
        if not rp.exists():
            continue
        for c in rp.rglob(_NET_FLASHER_EXE_NAME):
            if c.is_file():
                return c

    raise FileNotFoundError(
        f'{_NET_FLASHER_EXE_NAME} not found. Update MRS Applics Studio or '
        'set MRS_NET_FLASHER_EXE to its full path.'
    )


# ---------------------------------------------------------------------------
# Stdout splitting
# ---------------------------------------------------------------------------

def _split_lines(buf: str) -> tuple[list[str], str]:
    """Split on CR or LF runs, return (complete_lines, trailing_partial)."""
    parts = re.split(r'[\r\n]+', buf)
    return parts[:-1], parts[-1]


# ---------------------------------------------------------------------------
# Flash entry point
# ---------------------------------------------------------------------------

def run_flash(
    firmware: Firmware,
    *,
    progress:     Callable[[float, str], None] = lambda f, m: None,
    plc_found:    Callable[[str, str], None]   = lambda sn, label: None,
    log_line:     Callable[[str], None]        = lambda s: None,
    cancel_check: Callable[[], bool]           = lambda: False,
    flasher_exe:  Optional[Path]               = None,
    extra_args:   tuple                        = (),
    retries:      int                          = 1,
    net_baud:     Optional[str]                = None,
) -> FlashResult:
    """Run a flash via the MRS console flasher, retrying until a module is caught.

    Args:
        firmware:     Parsed firmware (must carry the original ``s19_text``).
        progress:     ``(fraction 0..1, message)`` for the GUI progress bar.
        plc_found:    ``(serial, module_label)`` invoked once SCAN identifies
                      the connected PLC, before erase begins.
        log_line:     Each raw stdout line, forwarded to the GUI log panel.
        cancel_check: Returns True to request early termination (between and
                      during attempts).
        flasher_exe:  Override exe path (uses ``find_console_flasher`` otherwise).
        extra_args:   Extra CLI args appended after ``/lngid 2``.
        retries:      Max number of scan passes. The console flasher scans once
                      and exits, so a *blank* module is caught on attempt 1, but
                      a *programmed* module only sits in the bootloader for a few
                      ms per power-up — reflashing it needs many passes while the
                      operator repeatedly power-cycles it (this is what MRS's own
                      flasher does internally). Retrying stops immediately on
                      success, on cancel, or on any failure where a module WAS
                      found (e.g. article mismatch) — retrying only helps the
                      "no module found after SCAN" case.
        net_baud:     When set (e.g. ``'500k:2000k'``), use the newer .NET
                      Applics Flasher at that CAN FD baud instead of the
                      classical console. Required to reach a *programmed* CAN FD
                      module, which boots at its firmware's baud, not 125k. The
                      ``A:B`` form selects CAN FD; ``--restart-module`` commands
                      a running module into the bootloader so no power-cycle is
                      needed. Success/failure comes from the process exit code.
    """
    if not firmware.s19_text:
        raise ValueError('Firmware has no raw .s19 text — cannot flash.')

    work_dir = Path(tempfile.mkdtemp(prefix='mrs_flash_'))
    s19_path = work_dir / 'firmware.s19'

    try:
        # Write the .s19 with plain LF endings — verified as the ONE format both
        # flashers accept: the classical console rejects any '\r' ("No data
        # found"), and the .NET flasher rejects doubled '\r\r\n' ("Index out of
        # bounds"). splitlines() strips all endings (\n, \r\n, \r) and blanks;
        # we re-join with '\n' only. (Do NOT use write_text — on Windows it
        # translates '\n' back to '\r\n' and reintroduces the bug.)
        _s19_lines = [ln for ln in firmware.s19_text.splitlines() if ln.strip()]
        s19_path.write_bytes(('\n'.join(_s19_lines) + '\n').encode('ascii'))

        if net_baud:
            exe = find_net_flasher()
            cmd = [
                str(exe), 'device', 'upload', str(s19_path),
                '--baudrate', net_baud,
                '--driver', 'PCANBasic',
                '--restart-module',
                # Fast mode checks CRC per line, not per message — the flasher's
                # own help flags it as "potentially unstable", and it caused
                # intermittent ErrorWhileFlashing mid-upload. Force it off for
                # reliable writes (slightly slower, still seconds).
                '--fastmode', 'false',
                *extra_args,
            ]
        else:
            exe = flasher_exe or find_console_flasher()
            cmd = [
                str(exe),
                '/s19_file', str(s19_path),
                '/lngid', '2',      # English — output regexes are anchored to English
                *extra_args,
            ]
        log.info('Spawning flasher: %s', ' '.join(cmd))

        total   = max(1, retries)
        result  = FlashResult(success=False, exit_code=-1)
        for attempt in range(1, total + 1):
            if cancel_check():
                break
            if total > 1 and net_baud:
                progress(0.0, f'Flashing CAN FD module (attempt {attempt}/{total})…')
            elif total > 1:
                progress(0.0, f'Searching for module — power-cycle it now '
                              f'(attempt {attempt}/{total})…')
            else:
                progress(0.0, 'Starter flasher…')

            result = _flash_attempt(exe, cmd, work_dir, progress,
                                    plc_found, log_line, cancel_check)

            if result.success:
                break
            # A module WAS found but the flash itself failed (wrong article,
            # programming aborted, …) — retrying won't help, so surface it.
            if result.serial:
                break
            # Otherwise no module was caught this pass; loop so the operator
            # can power-cycle again and we scan afresh.

        if not result.success and not cancel_check():
            msg = result.error_message or f'Flasher exited with code {result.exit_code}'
            progress(1.0, msg)

        return result

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _flash_attempt(
    exe:          Path,
    cmd:          list,
    work_dir:     Path,
    progress:     Callable[[float, str], None],
    plc_found:    Callable[[str, str], None],
    log_line:     Callable[[str], None],
    cancel_check: Callable[[], bool],
) -> FlashResult:
    """Run the console flasher exactly once and return its parsed FlashResult."""
    # CREATE_NO_WINDOW prevents a stray console window in dev runs.
    creationflags = 0x08000000 if os.name == 'nt' else 0
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        bufsize=0,
        text=True,
        encoding='utf-8',
        errors='replace',
        creationflags=creationflags,
        cwd=str(work_dir),
    )

    result      = FlashResult(success=False, exit_code=-1)
    captured: list[str] = []
    scan_pending = False     # next non-empty line after "Module(s) found"
    buf         = ''

    try:
        assert proc.stdout is not None
        while True:
            if cancel_check():
                proc.terminate()
                break

            chunk = proc.stdout.read(64)
            if not chunk:
                break
            buf += chunk
            lines, buf = _split_lines(buf)

            for line in lines:
                if not line.strip():
                    continue
                captured.append(line)
                log_line(line)
                _consume_line(line, result, scan_state := [scan_pending],
                              plc_found, progress)
                scan_pending = scan_state[0]

        if buf.strip():
            captured.append(buf)
            log_line(buf)
            _consume_line(buf, result, [scan_pending], plc_found, progress)

        exit_code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    result.exit_code = exit_code
    result.success   = (exit_code == 0)
    result.output    = '\n'.join(captured)

    if result.success:
        progress(1.0, 'Flash complete')

    return result


def _consume_line(
    line:        str,
    result:      FlashResult,
    scan_state:  list,
    plc_found:   Callable[[str, str], None],
    progress:    Callable[[float, str], None],
) -> None:
    """Update FlashResult / fire callbacks for one stdout line."""
    if scan_state[0]:
        m = _SCAN_LINE_RE.match(line.strip())
        if m:
            result.serial       = m.group(1)
            result.module_label = f'{m.group(2)} : {m.group(3)}'
            plc_found(result.serial, result.module_label)
            # plc_found's slot updates the status with SN + SW from the
            # loaded .s19. Emit progress with no text so we don't clobber it.
            progress(0.05, '')
            scan_state[0] = False
            return

    if _SCAN_HEADER_RE.search(line):
        scan_state[0] = True

    # .NET flasher module identity (before the reflash). Name comes first, then
    # serial, then app version — fire plc_found on the version line so the GUI
    # shows the SN + the firmware that was on the unit before we overwrote it.
    m = _NET_NAME_RE.search(line)
    if m:
        result.module_label = m.group(1).strip()
    m = _NET_SERIAL_RE.search(line)
    if m:
        result.serial = m.group(1)
    m = _NET_APPVER_RE.search(line)
    if m:
        ver   = m.group(1).strip()
        name  = result.module_label
        label = f'{name} : {ver}' if name else ver
        plc_found(result.serial, label)
        progress(0.05, '')

    m = _PROGRESS_RE.search(line)
    if m:
        pct  = int(m.group(1))
        frac = 0.05 + (pct / 100) * 0.93   # leave a touch above 95% for finalize
        progress(min(frac, 0.98), f'Flashing — {pct}%')

    m = _FINISHED_RE.search(line)
    if m:
        result.error_code    = int(m.group(1), 16)
        result.error_message = m.group(3).strip()
