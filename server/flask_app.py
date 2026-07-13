"""
Firmware proxy server for MRS PLC Programmer.

Deploy this on PythonAnywhere (free tier). It forwards firmware download
requests to the private GitHub repo so the GitHub token never leaves
the server.

Flash-event tracking lives elsewhere — see
``server/apps_script_events.gs``, deployed as a Google Apps Script
Web App writing into an HQ-owned Google Sheet.

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

This file needs two companions in the same folder (upload all three):
  user_store.py    ← password hashing + login-token signing (no dependencies)
  manage_users.py  ← CLI to create / disable distributor accounts

Environment variables (set in your WSGI file or in a .env file):
  GITHUB_TOKEN        = your fine-grained PAT (read-only, Contents permission)
  PROXY_API_KEY       = a random secret string (legacy app-level key)
  TOKEN_SECRET        = a long random string used to sign login tokens. REQUIRED
                        for logins to work. Keep it secret; changing it logs
                        everyone out.
  LOGIN_ENFORCED      = '1' (default) requires a valid login for firmware.
                        Set '0' during rollout so old (pre-login) apps keep
                        working via PROXY_API_KEY; flip to '1' once everyone
                        has updated to the login-enabled app.
  TOKEN_TTL_SECONDS   = how long a login lasts (default 2592000 = 30 days).
  USERS_FILE          = path to users.json (default: next to these files).

Accounts are created with:  python manage_users.py add <username> "<Distributor>"

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
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, abort, Response

import user_store

app = Flask(__name__)

# ---------- Configuration ----------
GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
PROXY_API_KEY = os.environ.get('PROXY_API_KEY', '')
GITHUB_OWNER = 'S0lsem'
GITHUB_REPO = 'Code-for-Highbeam-X'
FIRMWARE_PATH = 'mrs-firmware'

# ---------- Login / access control ----------
TOKEN_SECRET      = os.environ.get('TOKEN_SECRET', '')
TOKEN_TTL_SECONDS = int(os.environ.get('TOKEN_TTL_SECONDS', 30 * 24 * 3600))
# Default: enforce login. Set LOGIN_ENFORCED=0 during rollout to also accept the
# legacy PROXY_API_KEY so pre-login apps keep working until everyone updates.
LOGIN_ENFORCED = os.environ.get('LOGIN_ENFORCED', '1').strip().lower() \
    not in ('0', 'false', 'no', '')

_API = 'https://api.github.com'


def _check_api_key():
    key = request.headers.get('X-Api-Key', '')
    if not PROXY_API_KEY:
        return
    if key != PROXY_API_KEY:
        abort(403, 'Invalid API key')


def _bearer_token() -> str:
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[len('Bearer '):].strip()
    return ''


def _require_auth() -> str:
    """Authorize a firmware request. Returns the username (or '' for a legacy
    key match). Aborts 401/403 if the caller isn't allowed.

    A valid login token wins. When LOGIN_ENFORCED is off, a request with no
    token falls back to the legacy PROXY_API_KEY check so old apps keep working
    during the rollout window.
    """
    token = _bearer_token()
    if token:
        username = user_store.verify_token(token, TOKEN_SECRET)
        if username:
            user = user_store.get_user(username)
            if user and user.get('active', False):
                return username
        abort(401, 'Login expired or revoked. Please log in again.')

    if not LOGIN_ENFORCED:
        _check_api_key()   # legacy migration path
        return ''
    abort(401, 'Login required.')


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


@app.route('/login', methods=['POST'])
def login():
    """Validate username + password, return a signed login token.

    Request JSON:  {"username": "...", "password": "..."}
    Response JSON: {"token", "expires_at", "username", "distributor"}
    """
    if not TOKEN_SECRET:
        abort(500, 'Server login is not configured (TOKEN_SECRET is unset).')

    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip().lower()
    password = str(data.get('password', ''))

    user = user_store.get_user(username)
    ok = (
        user is not None
        and user.get('active', False)
        and user_store.verify_password(password, user.get('pw', ''))
    )
    if not ok:
        # Uniform message — don't reveal whether the user exists or is disabled.
        abort(401, 'Invalid username or password.')

    token, exp = user_store.make_token(username, TOKEN_SECRET, TOKEN_TTL_SECONDS)
    return jsonify({
        'token':       token,
        'expires_at':  exp,
        'username':    username,
        'distributor': user.get('distributor', ''),
    })


@app.route('/parts', methods=['GET'])
def list_parts():
    _require_auth()
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
    _require_auth()
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
