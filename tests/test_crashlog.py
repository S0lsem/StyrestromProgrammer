"""Tests for the crash diagnostics.

The behaviour under test is the one that made the programmer disappear
mid-batch: PyQt6 aborts the process when an exception escapes a slot, and
with ``console=False`` the traceback goes to a dead stderr, so the operator
sees a vanished window and we get nothing to debug with.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mrs_protocol import crashlog

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def crash_log(tmp_path, monkeypatch):
    """Redirect the crash log into tmp so tests never touch the real one."""
    path = tmp_path / 'crash.log'
    monkeypatch.setattr(crashlog, 'crash_log_path', lambda: path)
    monkeypatch.setattr(crashlog, 'log_dir', lambda: tmp_path)
    return path


# ---------------------------------------------------------------------------
# guard()
# ---------------------------------------------------------------------------

class _FakeWindow:
    """Stands in for MainWindow — guard() only needs _append_log."""

    def __init__(self) -> None:
        self.logged: list = []

    def _append_log(self, text: str) -> None:
        self.logged.append(text)

    @crashlog.guard
    def boom(self, which: str) -> None:
        raise PermissionError(f'[Errno 13] {which} is open in another program')

    @crashlog.guard
    def fine(self, value: int) -> int:
        return value * 2


def test_guard_returns_value_when_nothing_raises(crash_log):
    assert _FakeWindow().fine(21) == 42
    assert not crash_log.exists()


def test_guard_swallows_the_exception(crash_log):
    win = _FakeWindow()
    assert win.boom('flash_log.csv') is None      # must not propagate


def test_guard_records_the_traceback(crash_log):
    _FakeWindow().boom('flash_log.csv')

    text = crash_log.read_text(encoding='utf-8')
    assert 'SLOT ERROR in _FakeWindow.boom' in text
    assert 'PermissionError' in text
    assert 'flash_log.csv is open in another program' in text
    # The traceback itself, not just the exception line.
    assert 'Traceback (most recent call last)' in text


def test_guard_tells_the_operator_where_to_look(crash_log):
    win = _FakeWindow()
    win.boom('flash_log.csv')

    assert len(win.logged) == 1
    message = win.logged[0]
    assert 'INTERNAL ERROR in _FakeWindow.boom' in message
    assert 'flash itself was not affected' in message
    assert str(crash_log) in message


def test_guard_survives_a_window_that_cannot_log(crash_log):
    """A broken GUI must not turn a slot error into a second failure."""

    class Broken:
        @crashlog.guard
        def boom(self) -> None:
            raise ValueError('inner')

        def _append_log(self, text: str) -> None:
            raise RuntimeError('the log widget is already gone')

    Broken().boom()   # must not raise
    assert 'ValueError' in crash_log.read_text(encoding='utf-8')


def test_each_crash_is_appended_not_overwritten(crash_log):
    win = _FakeWindow()
    win.boom('first')
    win.boom('second')

    text = crash_log.read_text(encoding='utf-8')
    assert 'first is open' in text and 'second is open' in text


# ---------------------------------------------------------------------------
# attempt()
# ---------------------------------------------------------------------------

def test_attempt_returns_the_result(crash_log):
    win = _FakeWindow()
    assert crashlog.attempt(win, 'do the thing', lambda a, b: a + b, 2, 3) == 5
    assert win.logged == []


def test_attempt_returns_none_on_failure(crash_log):
    def locked():
        raise PermissionError('flash_log.csv is open in Excel')

    assert crashlog.attempt(_FakeWindow(), 'write the flash log', locked) is None


def test_attempt_keeps_later_steps_running(crash_log):
    """The point of the helper: a locked CSV must not cost us the HQ event."""
    win = _FakeWindow()
    done: list = []

    def locked():
        raise PermissionError('flash_log.csv is open in Excel')

    crashlog.attempt(win, 'write the flash log', locked)
    crashlog.attempt(win, 'report the flash to HQ', lambda: done.append('event'))
    crashlog.attempt(win, 'save the flash report', lambda: done.append('report'))

    assert done == ['event', 'report']


def test_attempt_tells_the_operator_the_flash_was_fine(crash_log):
    def locked():
        raise PermissionError('flash_log.csv is open in Excel')

    win = _FakeWindow()
    crashlog.attempt(win, 'write the flash log', locked)

    assert len(win.logged) == 1
    assert 'could not write the flash log' in win.logged[0]
    assert 'PLC was flashed correctly' in win.logged[0]
    assert 'STEP FAILED: write the flash log' in crash_log.read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# excepthook
# ---------------------------------------------------------------------------

def test_excepthook_records_uncaught_exceptions(crash_log):
    try:
        raise OSError('disk full')
    except OSError as exc:
        crashlog._excepthook(type(exc), exc, exc.__traceback__)

    text = crash_log.read_text(encoding='utf-8')
    assert 'UNHANDLED EXCEPTION (OSError)' in text
    assert 'disk full' in text


def test_crash_detail_never_reaches_the_gui_log(monkeypatch, tmp_path):
    """An operator must not be shown a raw traceback for a recoverable step.

    The GUI attaches its log pane to the root logger. A locked flash_log.csv
    on a PLC that flashed fine once printed 'CRITICAL crash' plus a full
    Python traceback at the person standing at the bench.
    """
    monkeypatch.setattr(crashlog, 'log_dir', lambda: tmp_path)
    monkeypatch.setattr(crashlog, '_installed', False)

    crashlog.install()

    gui_records: list = []

    class FakeGuiHandler(logging.Handler):
        def emit(self, record):
            gui_records.append(record.getMessage())

    handler = FakeGuiHandler()
    logging.getLogger().addHandler(handler)
    try:
        crashlog.attempt(_FakeWindow(), 'write the flash log',
                         lambda: (_ for _ in ()).throw(PermissionError('locked')))
    finally:
        logging.getLogger().removeHandler(handler)

    assert not any('Traceback' in m for m in gui_records), gui_records
    assert not any('STEP FAILED' in m for m in gui_records), gui_records
    # but it is still on disk
    assert 'STEP FAILED' in (tmp_path / 'crash.log').read_text(encoding='utf-8')


import logging   # noqa: E402 — used by the test above


def test_install_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(crashlog, 'log_dir', lambda: tmp_path)
    monkeypatch.setattr(crashlog, '_installed', False)
    import logging

    before = len(logging.getLogger().handlers)
    crashlog.install()
    after_first = len(logging.getLogger().handlers)
    crashlog.install()
    assert len(logging.getLogger().handlers) == after_first
    assert after_first == before + 1


# ---------------------------------------------------------------------------
# The real thing: PyQt6 must not abort the process
# ---------------------------------------------------------------------------

# The exception must reach the slot the way it does in the real app: from
# inside the Qt event loop. Raising it from a plain Python-side ``emit()``
# propagates back to the caller instead and never reaches the abort path.
_CHILD = textwrap.dedent(
    '''
    import os, sys
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    sys.path.insert(0, {repo!r})

    from PyQt6.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal
    {install}

    class Emitter(QObject):
        fired = pyqtSignal(str)

    class Window(QObject):
        def _append_log(self, text):
            pass

        {decorator}
        def on_fired(self, which):
            raise PermissionError(which + ' is locked')

    app = QCoreApplication([])
    emitter, window = Emitter(), Window()
    emitter.fired.connect(window.on_fired)
    QTimer.singleShot(0, lambda: emitter.fired.emit('flash_log.csv'))
    QTimer.singleShot(2000, app.quit)
    app.exec()
    print('SURVIVED')
    '''
)

# STATUS_STACK_BUFFER_OVERRUN — what Windows reports for a fail-fast abort,
# and the exception code in the WER record for the crash this fixes. Reported
# unsigned by subprocess and signed elsewhere; accept either spelling.
_ABORT_EXIT_CODES = (0xC0000409, -1073740791)


def _run_child(home: Path, *, install: bool, guard: bool):
    script = _CHILD.format(
        repo=str(REPO_ROOT),
        # dedent() has already run, so the placeholder sits at column 0.
        install=('from mrs_protocol import crashlog\ncrashlog.install()'
                 if install else ''),
        decorator='@crashlog.guard' if guard else '',
    )
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home))
    return subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True, timeout=120, env=env,
    )


import os   # noqa: E402 — kept next to its only user, _run_child


def test_unhandled_slot_exception_kills_the_process(tmp_path):
    """The bug itself, reproduced: this is why the programmer vanished.

    PyQt6 responds to an exception escaping a slot by calling ``qFatal()``.
    Windows records it against Qt6Core.dll as 0xC0000409 — and note the empty
    stderr, which is why a frozen console=False build left no trace at all.
    """
    pytest.importorskip('PyQt6')

    result = _run_child(tmp_path, install=False, guard=False)

    assert result.returncode in _ABORT_EXIT_CODES
    assert 'SURVIVED' not in result.stdout
    assert result.stderr == ''      # nothing to debug with — the whole problem


def test_installing_the_hooks_prevents_the_abort(tmp_path):
    """``install()`` alone is enough: PyQt6 defers to an installed excepthook."""
    pytest.importorskip('PyQt6')

    result = _run_child(tmp_path, install=True, guard=False)

    assert result.returncode == 0, f'still aborting: {result.returncode!r}'
    assert 'SURVIVED' in result.stdout

    crash_log = tmp_path / '.mrs_programmer' / 'logs' / 'crash.log'
    assert crash_log.exists(), 'the crash must be recorded even though we survived'
    assert 'PermissionError' in crash_log.read_text(encoding='utf-8')


def test_guarded_slot_survives_and_is_recorded(tmp_path):
    """The guard adds which slot failed, and keeps the slot's caller running."""
    pytest.importorskip('PyQt6')

    result = _run_child(tmp_path, install=True, guard=True)

    assert result.returncode == 0
    assert 'SURVIVED' in result.stdout

    text = (tmp_path / '.mrs_programmer' / 'logs' / 'crash.log').read_text(
        encoding='utf-8'
    )
    assert 'SLOT ERROR in Window.on_fired' in text
    assert 'PermissionError' in text
