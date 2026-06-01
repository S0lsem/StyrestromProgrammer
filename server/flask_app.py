"""
Firmware proxy server for MRS PLC Programmer.

Deploy this on PythonAnywhere (free tier). It forwards firmware download
requests to the private GitHub repo so the GitHub token never leaves
the server.

Setup on PythonAnywhere:
  1. Create a free account at pythonanywhere.com
  2. Go to "Web" tab → "Add a new web app" → Manual configuration → Python 3.10
  3. Go to "Files" tab → open /home/yourusername/mysite/flask_app.py
  4. Paste this file's contents and save
  5. Go to "Web" tab → open the WSGI configuration file
  6. Replace its contents with:
       import sys
       sys.path.insert(0, '/home/yourusername/mysite')
       from flask_app import app as application
  7. Go to "Web" tab → click "Reload"
  8. Set environment variables (see below)

Environment variables (set in your WSGI file or in a .env file):
  GITHUB_TOKEN        = your fine-grained PAT (read-only, Contents permission)
  PROXY_API_KEY       = a random secret string (the app uses this to authenticate)

Expected repo layout (private GitHub repo, owner/name set below):
  mrs-firmware/
    <part_name>/
      *.s19                 ← the linked image produced by MRS Applics Studio.
                              Any filename ending in .s19 is fine, and the file
                              may sit at the part-folder root or under src/.
                              First .s19 found (root first, then src/) wins.
"""
from __future__ import annotations

import base64
import html
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, abort, Response

app = Flask(__name__)

# ---------- Configuration ----------
GITHUB_TOKEN    = os.environ.get('GITHUB_TOKEN', '')
PROXY_API_KEY   = os.environ.get('PROXY_API_KEY', '')
ADMIN_API_KEY   = os.environ.get('ADMIN_API_KEY', '')
GITHUB_OWNER = 'S0lsem'
GITHUB_REPO = 'Code-for-Highbeam-X'
FIRMWARE_PATH = 'mrs-firmware'

_API = 'https://api.github.com'

# SQLite for flash-event tracking. Created next to this file.
_DB_PATH = str(Path(__file__).parent / 'events.db')


def _check_api_key():
    key = request.headers.get('X-Api-Key', '')
    if not PROXY_API_KEY:
        return
    if key != PROXY_API_KEY:
        abort(403, 'Invalid API key')


def _github_get(path: str):
    url = f'{_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}'
    req = Request(url, headers={
        'Authorization': f'Bearer {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _find_s19(part: str) -> tuple[str, str] | None:
    """Locate the first .s19 file in the part folder or its src/ subfolder.

    Returns (folder_path, filename) or None if no .s19 is present.
    The part-folder root takes precedence over src/ so a renamed firmware.s19
    at the root overrides any leftover build artifacts in src/.
    """
    for folder in (f'{FIRMWARE_PATH}/{part}', f'{FIRMWARE_PATH}/{part}/src'):
        try:
            items = _github_get(folder)
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        for item in items:
            if item.get('type') == 'file' and item['name'].lower().endswith('.s19'):
                return folder, item['name']
    return None


@app.route('/parts', methods=['GET'])
def list_parts():
    _check_api_key()
    try:
        items = _github_get(FIRMWARE_PATH)
        parts = sorted(
            item['name']
            for item in items
            if item['type'] == 'dir' and not item['name'].startswith('.')
        )
        return jsonify(parts)
    except HTTPError as exc:
        return jsonify({'error': f'GitHub API error: {exc.code}'}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502


@app.route('/parts/<part>/firmware', methods=['GET'])
def get_firmware(part: str):
    """Return the raw S-record text for <part>.

    Walks the part folder (and its src/ subfolder) and serves the first
    .s19 found. Any filename works, so the file can be uploaded straight
    out of MRS's bin/ folder without renaming.
    """
    _check_api_key()
    try:
        located = _find_s19(part)
        if located is None:
            return jsonify({'error': f"No .s19 file found for '{part}'."}), 404
        folder, name = located
        info = _github_get(f'{folder}/{name}')
        content_b64 = info['content'].replace('\n', '')
        s19_text = base64.b64decode(content_b64).decode('ascii', errors='strict')
        return Response(s19_text, mimetype='text/plain; charset=us-ascii')
    except HTTPError as exc:
        return jsonify({'error': f'GitHub API error: {exc.code}'}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


# ---------------------------------------------------------------------------
# Flash-event tracking — every flash attempt posted by the programmer lands
# here so Styrestrøm HQ can see who programmed what.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    received_ts_utc      TEXT    NOT NULL,
    client_ts_utc        TEXT    NOT NULL,
    distributor          TEXT    NOT NULL,
    operator             TEXT    NOT NULL,
    plc_serial           TEXT    NOT NULL,
    part                 TEXT    NOT NULL,
    module               TEXT,
    channel              TEXT,
    result               TEXT    NOT NULL,
    error_message        TEXT,
    flasher_exit         INTEGER,
    scan_label           TEXT,
    first_program_for_sn INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plc_serial ON events(plc_serial);
CREATE INDEX IF NOT EXISTS idx_received   ON events(received_ts_utc);
"""


def _db():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _db() as conn:
        conn.executescript(_SCHEMA)


_init_db()


def _check_admin_key():
    """Return None if the caller is authenticated as admin, else a Response.

    Accepts two equivalent credentials so both browser and curl work:
      - ``X-Admin-Key: <ADMIN_API_KEY>`` header (curl / scripting).
      - HTTP Basic Auth where the password equals ``ADMIN_API_KEY``
        (the username can be anything — browser prompts you for both).
    """
    if not ADMIN_API_KEY:
        return Response('Server has no ADMIN_API_KEY set.', status=503)

    key = request.headers.get('X-Admin-Key', '')
    if key and key == ADMIN_API_KEY:
        return None

    auth = request.authorization
    if auth and auth.password == ADMIN_API_KEY:
        return None

    return Response(
        'Authentication required.',
        status=401,
        headers={'WWW-Authenticate': 'Basic realm="MRS Flash Events"'},
    )


@app.route('/log_flash', methods=['POST'])
def log_flash():
    """Ingest one flash event from a distributor's programmer."""
    _check_api_key()

    if not request.is_json:
        return jsonify({'error': 'expected application/json'}), 400
    event = request.get_json(silent=True) or {}

    required = ('distributor', 'operator', 'plc_serial', 'part', 'result')
    missing = [k for k in required if not str(event.get(k, '')).strip()]
    if missing:
        return jsonify({'error': f'missing fields: {", ".join(missing)}'}), 400

    plc_serial = str(event['plc_serial']).strip()
    result     = str(event['result']).strip().upper()
    received   = datetime.now(timezone.utc).isoformat(timespec='seconds')

    with _db() as conn:
        prior_ok = conn.execute(
            "SELECT 1 FROM events WHERE plc_serial = ? AND result = 'OK' LIMIT 1",
            (plc_serial,),
        ).fetchone()
        first = 0 if prior_ok else (1 if result == 'OK' else 0)

        conn.execute(
            """
            INSERT INTO events (
                received_ts_utc, client_ts_utc, distributor, operator,
                plc_serial, part, module, channel, result, error_message,
                flasher_exit, scan_label, first_program_for_sn
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                received,
                str(event.get('timestamp_utc', '')).strip(),
                str(event['distributor']).strip(),
                str(event['operator']).strip(),
                plc_serial,
                str(event['part']).strip(),
                str(event.get('module', '')).strip(),
                str(event.get('channel', '')).strip(),
                result,
                str(event.get('error_message', '')),
                int(event.get('flasher_exit') or 0),
                str(event.get('scan_label', '')),
                first,
            ),
        )

    return jsonify({'ok': True, 'first_program_for_sn': bool(first)})


@app.route('/admin/events', methods=['GET'])
def admin_events():
    """Minimal HTML table of recent events. Auth via X-Admin-Key header.

    Query params: distributor, part, plc_serial, since (ISO date),
    limit (default 200, max 1000), offset (default 0).
    """
    denied = _check_admin_key()
    if denied is not None:
        return denied

    args = request.args
    where: list[str] = []
    params: list = []
    for col, key in (
        ('distributor', 'distributor'),
        ('part', 'part'),
        ('plc_serial', 'plc_serial'),
    ):
        if args.get(key):
            where.append(f'{col} LIKE ?')
            params.append(f'%{args[key]}%')
    if args.get('since'):
        where.append('received_ts_utc >= ?')
        params.append(args['since'])

    try:
        limit = max(1, min(int(args.get('limit', 200)), 1000))
    except ValueError:
        limit = 200
    try:
        offset = max(0, int(args.get('offset', 0)))
    except ValueError:
        offset = 0

    sql = 'SELECT * FROM events'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])

    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]

    return Response(_render_events_html(rows, total, args, limit, offset),
                    mimetype='text/html; charset=utf-8')


def _render_events_html(rows, total, args, limit, offset) -> str:
    cols = ('id', 'received_ts_utc', 'distributor', 'operator', 'plc_serial',
            'part', 'result', 'first_program_for_sn', 'scan_label',
            'error_message')
    head = ''.join(f'<th>{c}</th>' for c in cols)

    body_rows = []
    for r in rows:
        cells = []
        for c in cols:
            v = r[c]
            if c == 'first_program_for_sn':
                v = 'FIRST' if v else ''
            cells.append(f'<td>{html.escape(str(v or ""))}</td>')
        cls = 'first' if r['first_program_for_sn'] else ''
        body_rows.append(f'<tr class="{cls}">{"".join(cells)}</tr>')
    body = '\n'.join(body_rows) or '<tr><td colspan="10">No events match.</td></tr>'

    def hidden_inputs():
        out = []
        for k in ('distributor', 'part', 'plc_serial', 'since'):
            if args.get(k):
                out.append(
                    f'<input type="hidden" name="{k}" value="{html.escape(args[k])}">'
                )
        return ''.join(out)

    prev_off = max(0, offset - limit)
    next_off = offset + limit
    pager = (
        f'<p>Showing rows {offset+1}–{offset+len(rows)} of {total}. '
        f'<a href="?{_qs(args, offset=prev_off, limit=limit)}">« prev</a> '
        f'<a href="?{_qs(args, offset=next_off, limit=limit)}">next »</a></p>'
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>MRS Flash Events</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 20px; }}
 form  {{ margin-bottom: 12px; }}
 input {{ margin-right: 6px; padding: 4px; }}
 table {{ border-collapse: collapse; font-size: 12px; }}
 th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left;
          vertical-align: top; }}
 th    {{ background: #eee; position: sticky; top: 0; }}
 tr.first td {{ background: #e6ffe6; font-weight: bold; }}
 a {{ text-decoration: none; color: #1a7fd4; }}
</style></head><body>
<h1>MRS Flash Events</h1>
<form method="get">
  <input name="distributor" placeholder="distributor" value="{html.escape(args.get('distributor',''))}">
  <input name="part"        placeholder="part"        value="{html.escape(args.get('part',''))}">
  <input name="plc_serial"  placeholder="PLC serial"  value="{html.escape(args.get('plc_serial',''))}">
  <input name="since"       placeholder="since (YYYY-MM-DD)" value="{html.escape(args.get('since',''))}">
  <input name="limit"       value="{limit}" size="4">
  <button type="submit">Filter</button>
  <a href="/admin/events">clear</a>
</form>
{pager}
<table>
  <thead><tr>{head}</tr></thead>
  <tbody>{body}</tbody>
</table>
{pager}
</body></html>"""


def _qs(args, **overrides) -> str:
    """Build a query string preserving filters but overriding offset/limit."""
    from urllib.parse import urlencode
    out = {k: v for k, v in args.items() if v}
    out.update({k: v for k, v in overrides.items() if v is not None})
    return urlencode(out)
