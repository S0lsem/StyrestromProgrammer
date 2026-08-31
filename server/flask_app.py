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
  3. Go to "Files" tab → open /home/<yourusername>/flask_app.py (these three
     files live wherever your WSGI file points; on the Styrestrom account
     that is the home folder itself, not a mysite/ subfolder)
  4. Paste this file's contents and save
  5. Go to "Web" tab → open the WSGI configuration file
  6. Replace its contents with:
       import sys
       sys.path.insert(0, '/home/<yourusername>')
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
Firmware access per distributor: python manage_users.py parts <username> <part>...
(a new account starts with access to everything; narrow it with "parts")

HQ can do all of that from the programmer app instead, via the /admin/* routes
below. Unlock it once per HQ account with:  python manage_users.py admin <username>

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
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, abort, Response

import firmware_mirror
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

# ---------------------------------------------------------------------------
# GitHub response cache
#
# Every /parts click cost one GitHub call and every download two or three, so
# a burst of activity — or anything else on the account eating the shared
# 5,000/hour user budget — took firmware delivery down completely. Cache what
# GitHub returns, and on a rate-limit refusal keep serving the last good copy
# rather than failing: stale firmware metadata beats no firmware at all.
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = int(os.environ.get('GITHUB_CACHE_TTL', 300))
# Don't let a huge file sit in the free tier's memory forever.
_CACHE_MAX_BYTES = 8 * 1024 * 1024
_cache: dict = {}          # path -> (fetched_at, payload, approx_size)


def _cache_get(path: str, max_age: float):
    entry = _cache.get(path)
    if entry is None:
        return None
    fetched_at, payload, _ = entry
    if max_age is not None and (time.time() - fetched_at) > max_age:
        return None
    return payload


def _cache_put(path: str, payload) -> None:
    size = len(json.dumps(payload)) if payload is not None else 0
    if size <= _CACHE_MAX_BYTES:
        _cache[path] = (time.time(), payload, size)


def _is_rate_limited(exc: HTTPError) -> bool:
    """True when GitHub refused because the hourly budget is spent.

    GitHub answers 403 both for 'no permission' and for 'rate limit exceeded',
    so the remaining-count header is what separates them.
    """
    if exc.code not in (403, 429):
        return False
    remaining = exc.headers.get('x-ratelimit-remaining') if exc.headers else None
    return remaining is None or remaining == '0'


def _github_error(exc: HTTPError) -> str:
    """Plain-language reason, so the operator is not left staring at '403'."""
    if not _is_rate_limited(exc):
        return f'GitHub API error: {exc.code}'
    reset = (exc.headers or {}).get('x-ratelimit-reset')
    when = ''
    if reset:
        try:
            when = time.strftime(' Try again after %H:%M UTC.',
                                 time.gmtime(int(reset)))
        except (TypeError, ValueError):
            when = ''
    return (
        'The firmware server has used up its hourly GitHub quota, so the '
        'part list is temporarily unavailable.' + when
    )


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


def _require_auth() -> tuple[str, dict | None]:
    """Authorize a firmware request. Returns (username, user_record).

    Aborts 401/403 if the caller isn't allowed. A valid login token wins. When
    LOGIN_ENFORCED is off, a request with no token falls back to the legacy
    PROXY_API_KEY check so old apps keep working during the rollout window —
    that path returns ('', None), which the firmware allow-list treats as
    "everything" because there is no account to look the list up on.
    """
    token = _bearer_token()
    if token:
        username = user_store.verify_token(token, TOKEN_SECRET)
        if username:
            user = user_store.get_user(username)
            if user and user.get('active', False):
                return username, user
        abort(401, 'Login expired or revoked. Please log in again.')

    if not LOGIN_ENFORCED:
        _check_api_key()   # legacy migration path
        return '', None
    abort(401, 'Login required.')


def _github_get(path: str):
    """Fetch a repo path, preferring a fresh cache entry.

    A 404 is passed straight through — callers rely on it to probe for an
    optional src/ folder. A rate-limit refusal falls back to the last good
    copy of *this* path if we have one, however old.
    """
    cached = _cache_get(path, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached

    url = f'{_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}'
    req = Request(url, headers={
        'Authorization': f'Bearer {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except HTTPError as exc:
        stale = _cache_get(path, None) if _is_rate_limited(exc) else None
        if stale is not None:
            return stale
        raise
    _cache_put(path, payload)
    return payload


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
        'admin':       user_store.is_admin(user),
    })


@app.route('/parts', methods=['GET'])
def list_parts():
    """List the firmware parts this distributor is allowed to see.

    Filtering happens here, not in the app: a part the account isn't entitled
    to must never travel over the wire, not even as a name in a list the
    client would hide.
    """
    _, user = _require_auth()
    # The mirror is the normal path and costs no GitHub calls at all. Falling
    # back to a live listing only matters before the first sync.
    if firmware_mirror.is_populated():
        return jsonify(user_store.filter_parts(user, firmware_mirror.list_parts()))
    try:
        items = _github_get(FIRMWARE_PATH)
        parts = sorted(
            item['name']
            for item in items
            if item['type'] == 'dir' and not item['name'].startswith('.')
        )
        return jsonify(user_store.filter_parts(user, parts))
    except HTTPError as exc:
        return jsonify({'error': _github_error(exc)}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502


@app.route('/parts/<part>/firmware', methods=['GET'])
def get_firmware(part: str):
    """Return the raw S-record text for <part>.

    Walks the part folder (and its src/ subfolder) and serves the first
    .s19 found. Any filename works, so the file can be uploaded straight
    out of MRS's bin/ folder without renaming.
    """
    _, user = _require_auth()
    if not user_store.is_part_allowed(user, part):
        # Same wording whether the part is unassigned or does not exist, so a
        # distributor cannot probe the catalogue by guessing folder names.
        return jsonify({
            'error': f"'{part}' is not enabled for your account. "
                     f'Contact Styrestrøm to have it added.',
        }), 403
    mirrored = firmware_mirror.read_s19(part)
    if mirrored is not None:
        return Response(mirrored, mimetype='text/plain; charset=us-ascii')

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
        return jsonify({'error': _github_error(exc)}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502


# ---------------------------------------------------------------------------
# Admin API — backs the Distributors window in the programmer app.
#
# Every route re-checks the admin flag on the live account, so revoking admin
# (or disabling the account) takes effect on the very next request rather than
# whenever the token happens to expire. Password hashes are never returned.
# ---------------------------------------------------------------------------

def _require_admin() -> tuple[str, dict]:
    """Authorize an admin request. Returns (username, user_record)."""
    username, user = _require_auth()
    if not user_store.is_admin(user):
        abort(403, 'This account is not allowed to manage distributors.')
    return username, user


def _json_body() -> dict:
    return request.get_json(silent=True) or {}


# Distributor logins are often e-mail addresses, so '.', '@' and '+' have to be
# legal. Everything else is rejected: a username ends up in a URL path, so a
# slash or whitespace would silently address the wrong account.
_USERNAME_RE = re.compile(r'^[a-z0-9._+@-]{1,64}$')


def _valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username))


@app.route('/admin/users', methods=['GET'])
def admin_list_users():
    _require_admin()
    users = user_store.load_users()
    return jsonify([
        user_store.describe_user(name, users[name]) for name in sorted(users)
    ])


@app.route('/admin/parts', methods=['GET'])
def admin_list_all_parts():
    """The full firmware catalogue, unfiltered — the admin picks from this."""
    _require_admin()
    if firmware_mirror.is_populated():
        return jsonify(firmware_mirror.list_parts())
    try:
        items = _github_get(FIRMWARE_PATH)
        return jsonify(sorted(
            item['name']
            for item in items
            if item['type'] == 'dir' and not item['name'].startswith('.')
        ))
    except HTTPError as exc:
        return jsonify({'error': _github_error(exc)}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502


@app.route('/admin/users', methods=['POST'])
def admin_upsert_user():
    """Create an account, or reset an existing one's password + distributor.

    Body: {username, password, distributor, parts?, admin?}
    Omitting parts/admin on an existing account leaves those untouched.
    """
    _require_admin()
    data = _json_body()
    username    = str(data.get('username', '')).strip().lower()
    password    = str(data.get('password', ''))
    distributor = str(data.get('distributor', '')).strip()

    if not _valid_username(username):
        return jsonify({
            'error': 'Username may use letters, digits and . _ - + @ '
                     '(an e-mail address is fine), with no spaces.',
        }), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400
    if not distributor:
        return jsonify({'error': 'Distributor name is required.'}), 400

    parts = data.get('parts')
    admin = data.get('admin')
    user_store.upsert_user(
        username, password, distributor, active=True,
        parts=list(parts) if isinstance(parts, list) else None,
        admin=bool(admin) if admin is not None else None,
    )
    users = user_store.load_users()
    return jsonify(user_store.describe_user(username, users[username]))


@app.route('/admin/users/<username>', methods=['PATCH'])
def admin_update_user(username: str):
    """Change active / parts / admin on an existing account.

    Body may carry any of: {active: bool, parts: [...], admin: bool}.
    An admin cannot disable or demote their own account — that is the one
    mistake that would lock HQ out of this API entirely.
    """
    me, _ = _require_admin()
    target = username.strip().lower()
    if user_store.get_user(target) is None:
        return jsonify({'error': f"No such account: '{username}'."}), 404

    data = _json_body()
    self_edit = (target == me)

    if 'active' in data:
        if self_edit and not data['active']:
            return jsonify({'error': 'You cannot disable your own account.'}), 400
        user_store.set_active(target, bool(data['active']))
    if 'admin' in data:
        if self_edit and not data['admin']:
            return jsonify({'error': 'You cannot remove your own admin rights.'}), 400
        user_store.set_admin(target, bool(data['admin']))
    if 'parts' in data:
        parts = data['parts']
        if not isinstance(parts, list):
            return jsonify({'error': 'parts must be a list of names.'}), 400
        user_store.set_parts(target, parts)

    users = user_store.load_users()
    return jsonify(user_store.describe_user(target, users[target]))


@app.route('/admin/users/<username>', methods=['DELETE'])
def admin_delete_user(username: str):
    me, _ = _require_admin()
    target = username.strip().lower()
    if target == me:
        return jsonify({'error': 'You cannot delete your own account.'}), 400
    if not user_store.delete_user(target):
        return jsonify({'error': f"No such account: '{username}'."}), 404
    return jsonify({'deleted': target})


@app.route('/admin/sync', methods=['POST'])
def admin_sync():
    """Copy every .s19 from the firmware repo into the local mirror.

    This is the only routine operation that spends GitHub quota (~1 + 2 per
    part). Everything a distributor does is served from the result.
    """
    _require_admin()
    try:
        result = firmware_mirror.sync(
            list_dir=lambda sub: _github_get(
                FIRMWARE_PATH + (f'/{sub}' if sub else '')
            ),
            get_file=lambda path: _github_get(f'{FIRMWARE_PATH}/{path}'),
        )
    except HTTPError as exc:
        return jsonify({'error': _github_error(exc)}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502
    except OSError as exc:
        return jsonify({'error': f'Could not write the mirror: {exc}'}), 500
    return jsonify(result)


@app.route('/admin/status', methods=['GET'])
def admin_status():
    """Mirror state plus the GitHub budget, so HQ can see trouble coming.

    The /rate_limit endpoint is itself exempt from rate limiting, so asking
    costs nothing.
    """
    _require_admin()
    if not GITHUB_TOKEN:
        quota = {'available': False,
                 'reason': 'GITHUB_TOKEN is not set on the server. Add it to the '
                           'WSGI configuration file and press Reload.'}
        return jsonify({'mirror': firmware_mirror.status(), 'github': quota})

    try:
        req = Request(f'{_API}/rate_limit', headers={
            'Authorization': f'Bearer {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        with urlopen(req, timeout=10) as resp:
            core = json.loads(resp.read()).get('resources', {}).get('core', {})
        quota = {
            'available': True,
            'limit':     int(core.get('limit', 0)),
            'remaining': int(core.get('remaining', 0)),
            'used':      int(core.get('used', 0)),
            'reset':     int(core.get('reset', 0)),
        }
    # A failed reading must never break the window, but it must not be silent
    # either. Reporting a bare available:false cost a day once: the token had
    # expired, and the only way to discover that was to press Sync and read the
    # 401 off the error dialog. The status call already knew.
    except HTTPError as exc:
        quota = {'available': False, 'reason': _quota_failure_reason(exc.code)}
    except URLError as exc:
        quota = {'available': False,
                 'reason': f'Could not reach GitHub: {exc.reason}.'}
    except (ValueError, KeyError):
        quota = {'available': False,
                 'reason': 'GitHub replied with something this server could not read.'}
    return jsonify({'mirror': firmware_mirror.status(), 'github': quota})


def _quota_failure_reason(code: int) -> str:
    """Plain-language cause for a failed /rate_limit call, for the admin window."""
    if code == 401:
        return ('GitHub rejected the token (401) — it has expired or been revoked. '
                'Put a new one in GITHUB_TOKEN in the WSGI file and press Reload. '
                'Syncing will fail until you do.')
    if code == 403:
        return ('GitHub refused the token (403) — either the hourly quota is spent '
                'or the token has lost access to the firmware repo.')
    if code == 404:
        return ('GitHub returned 404 — the token cannot see the firmware repo. '
                'Check its repository access includes the firmware repository.')
    return f'GitHub returned HTTP {code}.'


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})
