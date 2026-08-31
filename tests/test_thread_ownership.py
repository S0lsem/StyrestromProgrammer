"""Every QThread in the GUI must be owned by Qt, not by its Python wrapper.

An unparented ``QThread()`` is owned by its sip wrapper. Dropping the last
Python reference then destroys the C++ object — and if the thread has not
actually stopped, Qt answers with ``qFatal()`` and the process dies with no
dialog and no traceback.

That is not hypothetical. On 2026-08-31 19:59:23 the app died exactly this
way, from ``_on_batch_thread_finished`` setting ``self._batch_thread = None``:

    QThread: Destroyed while thread '' is still running

``finished`` is emitted while the thread is still winding down, so the
reference was being dropped on a live thread. Parenting to the window hands
ownership to Qt, and dropping our reference becomes harmless.

This is a source-level check on purpose: reproducing the fatal would abort
the test process, and the property we care about ("no orphan QThread is
ever constructed") is exactly what the source says.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / 'programmer_app.py'

# QThread(...) with an empty argument list.
_ORPHAN = re.compile(r'\bQThread\(\s*\)')
_PARENTED = re.compile(r'\bQThread\(\s*self\s*\)')


def _source() -> str:
    return APP.read_text(encoding='utf-8')


def test_no_unparented_qthread_is_constructed():
    src = _source()
    orphans = _ORPHAN.findall(src)
    assert not orphans, (
        f'{len(orphans)} unparented QThread() in programmer_app.py. '
        'Pass a parent — QThread(self) — or the Python GC can destroy a '
        'running thread and Qt will abort the process.'
    )


def test_every_qthread_is_parented_to_the_window():
    src = _source()
    total = len(re.findall(r'\bQThread\(', src))
    parented = len(_PARENTED.findall(src))
    assert total == parented, (
        f'{total} QThread(...) constructions but only {parented} parented'
    )
    assert parented >= 10, 'expected the app to still create its worker threads'


def test_shutdown_drains_threads_before_the_window_is_destroyed():
    """Parenting makes Qt destroy the threads with the window, so they must
    be stopped first — otherwise the fix trades a GC race for a shutdown one."""
    src = _source()
    assert 'def _drain_threads' in src

    # There is more than one closeEvent in the file (a dialog has one too);
    # the window's is the one that stops the batch listener.
    blocks = [b for b in src.split('def closeEvent')[1:]
              if '_stop_batch_listener()' in b.split('\n    def ', 1)[0]]
    assert len(blocks) == 1, 'could not identify the main window closeEvent'
    close = blocks[0].split('\n    def ', 1)[0]

    assert '_drain_threads()' in close, 'closeEvent must drain the worker threads'


def test_threads_that_will_not_stop_are_disowned_not_destroyed():
    """A scan cannot be cancelled, so shutdown must cope with a live thread."""
    src = _source()
    drain = src.split('def _drain_threads', 1)[1].split('\n    def ', 1)[0]
    assert 'setParent(None)' in drain, (
        'a thread that misses the timeout must be disowned, not left for Qt '
        'to destroy while running'
    )
    assert '_abandoned' in drain, 'and kept referenced so the GC cannot destroy it'
