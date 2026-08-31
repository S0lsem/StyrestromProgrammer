"""
Crash diagnostics.

The app ships frozen with ``console=False``, so ``sys.stderr`` is a dead
handle: a Python traceback printed there goes nowhere. That matters more
than it looks, because PyQt6 **aborts the process** when an exception
escapes a slot — it prints the traceback (into the void) and calls
``qFatal()``. Windows records that as an ``0xC0000409`` fail-fast inside
``Qt6Core.dll`` and the window simply disappears, with no dialog and no
log. Qt's own fatal messages ("QThread: Destroyed while thread is still
running", …) take the same route.

Installing the hooks below turns every one of those silent aborts into a
readable record on disk:

    ~/.mrs_programmer/logs/programmer.log   rolling run log
    ~/.mrs_programmer/logs/crash.log        append-only crash record

``install()`` must run before ``QApplication`` is constructed.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_LOG_DIR_NAME  = 'logs'
_RUN_LOG       = 'programmer.log'
_CRASH_LOG     = 'crash.log'

_installed = False


def log_dir() -> Path:
    """Directory holding the run log and the crash log. Created on demand."""
    d = Path.home() / '.mrs_programmer' / _LOG_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def crash_log_path() -> Path:
    return log_dir() / _CRASH_LOG


def run_log_path() -> Path:
    return log_dir() / _RUN_LOG


def _write_crash(kind: str, detail: str) -> None:
    """Append one crash record and flush it immediately.

    Flushing matters: ``qFatal()`` calls ``abort()`` the instant it returns
    to Qt, so anything still sitting in a buffer is lost with the process.
    """
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    record = (
        f'\n{"=" * 78}\n'
        f'{stamp}  {kind}\n'
        f'{"=" * 78}\n'
        f'{detail.rstrip()}\n'
    )
    try:
        with crash_log_path().open('a', encoding='utf-8') as f:
            f.write(record)
            f.flush()
    except OSError:
        pass   # nothing useful left to do — do not raise from a crash handler

    try:
        # Goes to the run log only. The 'crash' logger does not propagate to
        # root (see install()), because the GUI's log pane is attached there
        # and an operator must never be shown a raw Python traceback — least
        # of all one headed "CRITICAL crash" for a flash that actually
        # succeeded. guard() and attempt() put a plain-language line in the
        # pane instead; the detail lives here and in crash.log.
        logging.getLogger('crash').critical('%s\n%s', kind, detail.rstrip())
        for h in logging.getLogger('crash').handlers:
            h.flush()
        for h in logging.getLogger().handlers:
            h.flush()
    except Exception:   # noqa: BLE001 — a broken logger must not mask the crash
        pass


def _excepthook(exc_type, exc, tb) -> None:
    """Catch what PyQt6 is about to abort over, and anything else uncaught.

    PyQt6 routes an exception escaping a slot through ``sys.excepthook``
    before it aborts, so this runs even for the fail-fast crashes.
    """
    _write_crash(
        f'UNHANDLED EXCEPTION ({exc_type.__name__})',
        ''.join(traceback.format_exception(exc_type, exc, tb)),
    )


def _thread_excepthook(args) -> None:
    """Same, for exceptions escaping a plain ``threading.Thread``."""
    if args.exc_type is SystemExit:
        return
    _write_crash(
        f'UNHANDLED EXCEPTION in thread {args.thread.name if args.thread else "?"} '
        f'({args.exc_type.__name__})',
        ''.join(traceback.format_exception(args.exc_type, args.exc_value,
                                           args.exc_traceback)),
    )


def _install_qt_message_handler() -> None:
    """Route Qt's own diagnostics into the log, capturing fatals before abort."""
    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return

    _levels = {
        QtMsgType.QtDebugMsg:    logging.DEBUG,
        QtMsgType.QtInfoMsg:     logging.INFO,
        QtMsgType.QtWarningMsg:  logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg:    logging.CRITICAL,
    }

    def handler(msg_type, context, message) -> None:
        level = _levels.get(msg_type, logging.INFO)
        where = ''
        try:
            if context is not None and context.file:
                where = f' [{context.file}:{context.line}]'
        except (AttributeError, TypeError):
            pass

        if msg_type == QtMsgType.QtFatalMsg:
            # Qt aborts the moment this returns — record it while we still can.
            _write_crash(
                'QT FATAL',
                f'{message}{where}\n\n'
                'Python stack at the time of the fatal:\n'
                + ''.join(traceback.format_stack()),
            )
            return
        logging.getLogger('qt').log(level, '%s%s', message, where)

    qInstallMessageHandler(handler)


def install(level: int = logging.INFO) -> Path:
    """Install the file log and the crash hooks. Returns the log directory.

    Call once, before ``QApplication`` is created. Safe to call again.
    """
    global _installed
    if _installed:
        return log_dir()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler: Optional[logging.Handler] = None
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            run_log_path(), maxBytes=2_000_000, backupCount=3, encoding='utf-8',
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s — %(message)s'
        ))
        root.addHandler(file_handler)
    except OSError:
        file_handler = None   # read-only home — the crash hooks below still work

    # The GUI hangs its log pane off the root logger, so anything logged there
    # is shown to whoever is standing at the machine. Crash detail is for us,
    # not for them: keep the 'crash' logger off root and give it the file
    # handler directly. An operator who has just flashed a PLC correctly should
    # see "could not write the flash log", not a CRITICAL Python traceback.
    crash_logger = logging.getLogger('crash')
    crash_logger.propagate = False
    if file_handler is not None and not crash_logger.handlers:
        crash_logger.addHandler(file_handler)

    # python-can logs every frame at DEBUG. On a 500k/2M CAN FD bus that is
    # thousands of records a second, each one crossing into the GUI thread as
    # a queued signal — enough on its own to wedge the event loop.
    logging.getLogger('can').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    _install_qt_message_handler()

    _installed = True
    log.info('Diagnostics installed — logs in %s', log_dir())
    return log_dir()


def guard(fn):
    """Decorator: keep an exception from escaping a Qt slot.

    An exception that leaves a slot kills the process (see the module
    docstring). Every slot that touches the disk, the network or QSettings
    wears this, so a locked ``flash_log.csv`` or an unreachable proxy
    produces a log line instead of a vanished window.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:   # noqa: BLE001 — the whole point is to catch everything
            detail = traceback.format_exc()
            _write_crash(f'SLOT ERROR in {fn.__qualname__}', detail)
            _notify(self, fn.__qualname__, detail)
            return None

    return wrapper


def attempt(window, what: str, fn, *args, **kwargs):
    """Run one non-critical bookkeeping step; log and carry on if it fails.

    The steps after a flash — CSV row, HQ event, saved report — are
    independent, and a distributor hits real reasons for one of them to fail
    (``flash_log.csv`` open in Excel, an exe installed read-only under
    Program Files, the proxy down). Losing all three because the first one
    raised is worse than losing one, and letting the exception escape the
    slot loses the whole application.

    Returns the call's result, or ``None`` if it failed.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:   # noqa: BLE001 — one step failing is not fatal
        _write_crash(f'STEP FAILED: {what}', traceback.format_exc())
        try:
            window._append_log(
                f'WARNING — could not {what}: {exc}\n'
                f'The PLC was flashed correctly; only this step failed.'
            )
        except Exception:   # noqa: BLE001
            pass
        return None


def _notify(window, where: str, detail: str) -> None:
    """Tell the operator, without assuming the GUI is still in one piece."""
    first_line = detail.strip().splitlines()[-1] if detail.strip() else '?'
    try:
        window._append_log(
            f'INTERNAL ERROR in {where}: {first_line}\n'
            f'The flash itself was not affected. Details: {crash_log_path()}'
        )
    except Exception:   # noqa: BLE001
        pass
