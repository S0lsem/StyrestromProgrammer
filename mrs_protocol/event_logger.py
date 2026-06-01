"""
Flash event reporter — posts every flash attempt to the Styrestrøm proxy
so HQ can see what each distributor is doing.

The proxy stores events in SQLite keyed by PLC serial; the response
indicates whether this was the first time that SN was successfully
programmed by anyone (any distributor) or a reflash.

Identity is self-reported via two QSettings values:
    distributor_name  — set once per install on first run
    operator_initials — set once per install on first run
Both are writable from the GUI's Settings dialog.

Offline behaviour: if the POST fails (network down, proxy unreachable,
proxy down), the event is appended to ``pending_events.jsonl`` in the
user's local data dir and replayed on the next launch / next successful
POST. The local cache never expires — events eventually reach HQ.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

_POST_TIMEOUT = 4.0  # seconds — short so a flaky network doesn't block the GUI


def _pending_path() -> Path:
    root = Path.home() / '.mrs_programmer'
    root.mkdir(parents=True, exist_ok=True)
    return root / 'pending_events.jsonl'


def build_event(
    *,
    distributor:    str,
    operator:       str,
    plc_serial:     str,
    part:           str,
    module:         str,
    channel:        str,
    result:         str,           # 'OK' | 'FAIL'
    error_message:  str  = '',
    flasher_exit:   int  = 0,
    scan_label:     str  = '',     # the full SCAN line from the flasher
) -> dict:
    """Build a flash-event dict ready to POST to /log_flash."""
    return {
        'timestamp_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'distributor':   distributor.strip(),
        'operator':      operator.strip(),
        'plc_serial':    str(plc_serial).strip(),
        'part':          part,
        'module':        module,
        'channel':       channel,
        'result':        result,
        'error_message': error_message,
        'flasher_exit':  flasher_exit,
        'scan_label':    scan_label,
    }


def report_event(event: dict) -> Optional[dict]:
    """Try to POST one event to the proxy. Returns the proxy's JSON
    response on success, None on failure. On failure, the event is
    appended to the local pending queue for later retry."""
    response = _post(event)
    if response is not None:
        # Successful POST is a good moment to drain anything queued.
        _drain_pending()
        return response
    _enqueue(event)
    return None


def replay_pending() -> int:
    """Drain the pending queue. Returns the number of events sent.

    Safe to call at app startup — does nothing if the queue is empty
    or the proxy is still unreachable.
    """
    return _drain_pending()


def _post(event: dict) -> Optional[dict]:
    try:
        from .config import PROXY_URL, PROXY_API_KEY
    except ImportError:
        log.warning('mrs_protocol.config not available — event not sent')
        return None

    url = f'{PROXY_URL.rstrip("/")}/log_flash'
    body = json.dumps(event).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Accept':       'application/json',
    }
    if PROXY_API_KEY:
        headers['X-Api-Key'] = PROXY_API_KEY

    req = Request(url, data=body, headers=headers, method='POST')
    try:
        with urlopen(req, timeout=_POST_TIMEOUT) as resp:
            payload = resp.read()
            try:
                return json.loads(payload) if payload else {}
            except ValueError:
                return {}
    except HTTPError as exc:
        log.warning('Event POST returned HTTP %d', exc.code)
        return None
    except URLError as exc:
        log.warning('Event POST network error: %s', exc.reason)
        return None
    except Exception as exc:   # noqa: BLE001 — never let this break the GUI
        log.warning('Event POST unexpected error: %s', exc)
        return None


def _enqueue(event: dict) -> None:
    try:
        with _pending_path().open('a', encoding='utf-8') as f:
            f.write(json.dumps(event) + '\n')
    except OSError as exc:
        log.warning('Could not queue pending event: %s', exc)


def _drain_pending() -> int:
    path = _pending_path()
    if not path.exists() or path.stat().st_size == 0:
        return 0

    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        log.warning('Could not read pending queue: %s', exc)
        return 0

    sent = 0
    remaining: list[str] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue   # drop malformed line
        if _post(event) is not None:
            sent += 1
        else:
            # Proxy still down — keep this and everything after, in order.
            remaining = lines[i:]
            break

    try:
        if remaining:
            path.write_text('\n'.join(remaining) + '\n', encoding='utf-8')
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning('Could not rewrite pending queue: %s', exc)

    if sent:
        log.info('Drained %d queued event(s) to proxy', sent)
    return sent
