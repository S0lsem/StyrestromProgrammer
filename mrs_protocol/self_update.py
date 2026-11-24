"""
In-app self-update for the packaged Windows .exe.

:func:`mrs_protocol.update_checker.check_for_update` finds the latest GitHub
release and its .exe asset URL. This module downloads that asset and swaps it
in for the currently-running exe.

Windows locks a running .exe, so a process cannot overwrite its own image on
disk. Instead we download the new exe, then hand off to a tiny batch script
that waits for our process to exit, moves the new exe over the old one,
relaunches it, and deletes itself. The app must quit immediately after
calling :func:`install_and_restart` so the file lock is released.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen

# CreateProcess flags — keep the helper alive after the app (its parent) dies,
# without flashing a console window.
_CREATE_NO_WINDOW         = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def is_frozen() -> bool:
    """True when running as a PyInstaller-built .exe (self-install possible)."""
    return bool(getattr(sys, 'frozen', False))


def current_exe() -> Path:
    """Absolute path of the running .exe (the file we replace on update)."""
    return Path(sys.executable)


def download_update(
    url: str,
    dest: Path,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Path:
    """Download the .exe at *url* to *dest* and return *dest*.

    Streams to disk so the GUI can show progress. Raises if the payload is
    not a Windows executable (guards against a login/error HTML page being
    swapped in for the real binary) or is implausibly small.
    """
    req = Request(url, headers={'Accept': 'application/octet-stream'})
    with urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get('Content-Length', 0) or 0)
        read = 0
        with open(dest, 'wb') as f:
            # Verify the PE 'MZ' magic before writing the bulk of the file.
            head = resp.read(2)
            if head[:2] != b'MZ':
                raise ValueError(
                    'Downloaded file is not a Windows executable (missing MZ '
                    'header). Update aborted — the running app is untouched.'
                )
            f.write(head)
            read += len(head)
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                read += len(chunk)
                if progress and total:
                    progress(min(read / total, 1.0), 'Downloading update…')

    if dest.stat().st_size < 1024:
        raise ValueError('Downloaded update is implausibly small — aborting.')
    if progress:
        progress(1.0, 'Download complete')
    return dest


# Swaps the exe once we have exited, relaunches, self-deletes.
#
# Windows will not replace an exe while any process still runs it, so the
# move itself is the "has the app exited?" test: retry it once a second until
# it succeeds. That also waits out PyInstaller's onefile launcher, which holds
# the exe for a moment after the Python process has gone.
#
# Do not go back to polling `tasklist /fi "PID eq ..." | find "..."`: spawned
# from the windowed exe, `find` never saw the end of its input and hung, so
# the swap never ran and the app never came back (v1.0.19 -> v1.0.20).
#
# After ~2 minutes it gives up and relaunches whatever is there, so the user
# is never left without a programmer.
_UPDATER_BAT = """@echo off
setlocal
set "SRC={src}"
set "DST={dst}"
set /a TRIES=0
:retry
move /y "%SRC%" "%DST%" >nul 2>&1
if not errorlevel 1 goto launch
set /a TRIES+=1
if %TRIES% geq 120 goto launch
ping -n 2 127.0.0.1 >nul
goto retry
:launch
start "" "%DST%"
endlocal
(goto) 2>nul & del "%~f0"
"""


def default_download_path() -> Path:
    """Where to stage the downloaded exe: next to the running exe when that
    directory is writable (same-volume move is fast and stays on the machine),
    otherwise the temp dir."""
    target = current_exe()
    staged = target.with_name(target.stem + '.new-download.exe')
    if os.access(str(target.parent), os.W_OK):
        return staged
    return Path(tempfile.gettempdir()) / staged.name


def install_and_restart(new_exe: Path) -> None:
    """Spawn a detached helper that replaces the running exe with *new_exe*
    and relaunches it. The caller MUST quit the app right after this returns
    so Windows releases the lock on the old exe.

    Only valid in a frozen build; raises otherwise.
    """
    if not is_frozen():
        raise RuntimeError('Self-install is only available in the packaged .exe.')

    target = current_exe()
    fd, bat_str = tempfile.mkstemp(suffix='.bat', prefix='mrs_update_')
    os.close(fd)
    bat_path = Path(bat_str)
    bat_path.write_text(
        _UPDATER_BAT.format(src=str(new_exe), dst=str(target)),
        encoding='ascii',
    )

    # A windowed exe has no usable std handles to pass on, so give the helper
    # its own (DEVNULL) and a hidden console for the commands it runs.
    subprocess.Popen(
        ['cmd', '/c', str(bat_path)],
        creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=_fresh_app_env(),
    )


def _fresh_app_env() -> dict:
    """Our environment, minus PyInstaller's onefile bookkeeping.

    The onefile launcher tells its Python process where it unpacked itself
    through ``_PYI_*`` variables (``_MEIPASS2`` before PyInstaller 6). Passed
    on to the relaunched exe, they make it reuse our ``_MEI...`` folder,
    which is deleted as we exit: "Failed to load Python DLL ... python312.dll".
    """
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith('_PYI_') and k.upper() != '_MEIPASS2'}
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return env
