"""
Shared user store + password hashing + login-token signing for the firmware
proxy. Imported by both ``flask_app.py`` (to validate logins and tokens) and
``manage_users.py`` (to create/disable accounts).

Design goals:
  * No third-party dependencies — pure standard library, so it runs on the
    PythonAnywhere free tier and in a plain ``python manage_users.py`` shell.
  * Passwords are never stored in the clear — only salted PBKDF2-SHA256 hashes.
  * Login tokens are stateless: an HMAC-SHA256 signature over
    ``{username, expiry}``. The server needs no session store, and a token is
    validated by signature + expiry + a live "is this user still active?"
    check, so disabling an account revokes access on the very next request.

The users file is JSON:
    {
      "hq":     {"pw": "pbkdf2_sha256$...", "distributor": "Styrestrom AS",
                 "active": true,  "parts": ["*"], "admin": true},
      "acme":   {"pw": "pbkdf2_sha256$...", "distributor": "Acme AS",
                 "active": true,  "parts": ["*"]},
      "bravo":  {"pw": "pbkdf2_sha256$...", "distributor": "Bravo Ltd",
                 "active": false, "parts": ["1493X-V4", "1494X*"]}
    }
Usernames are stored and looked up lower-cased.

``parts`` is the firmware allow-list: which part folders that distributor may
see in the app's Part dropdown and download. Entries are glob patterns matched
case-insensitively (``1494X*`` covers every 1494X revision). ``["*"]`` means
everything; ``[]`` means nothing. A **missing** ``parts`` key also means
everything — so accounts created before this feature keep working untouched
after the server is upgraded, and HQ narrows them down deliberately.

``admin`` (default false) marks an HQ account that may manage the others
through the ``/admin/*`` endpoints — that is what unlocks the Distributors
window in the programmer app. Grant it from the console with
``python manage_users.py admin <username>``.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import os
import secrets
import time

# Path to the users file. Override with the USERS_FILE env var (e.g. to point
# at /home/<you>/mysite/users.json on PythonAnywhere).
USERS_FILE = os.environ.get(
    'USERS_FILE', os.path.join(os.path.dirname(__file__), 'users.json')
)

_PBKDF2_ITERATIONS = 200_000


# ---------------------------------------------------------------------------
# Users file I/O
# ---------------------------------------------------------------------------

def load_users() -> dict:
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        # Corrupt file — fail closed (treat as no users) rather than crash.
        return {}


def save_users(users: dict) -> None:
    """Write atomically so a crash mid-write can't corrupt the store."""
    tmp = USERS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(users, f, indent=2, sort_keys=True)
    os.replace(tmp, USERS_FILE)


def get_user(username: str) -> dict | None:
    return load_users().get(username.strip().lower())


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), salt, _PBKDF2_ITERATIONS
    )
    return f'pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac(
            'sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Account management (used by manage_users.py)
# ---------------------------------------------------------------------------

def upsert_user(username: str, password: str, distributor: str,
                active: bool = True, parts: list | None = None,
                admin: bool | None = None) -> None:
    """Create or reset an account.

    *parts* is the firmware allow-list (see module docstring). Passing None for
    *parts* or *admin* on an **existing** user keeps their current values, so
    resetting a forgotten password neither re-opens every part to them nor
    strips an HQ account of its admin rights.
    """
    users = load_users()
    key = username.strip().lower()
    existing = users.get(key) or {}
    if parts is None:
        parts = existing.get('parts', ALL_PARTS)
    if admin is None:
        admin = bool(existing.get('admin', False))
    users[key] = {
        'pw': hash_password(password),
        'distributor': distributor,
        'active': active,
        'parts': _clean_patterns(parts),
        'admin': bool(admin),
    }
    save_users(users)


def set_active(username: str, active: bool) -> bool:
    """Enable/disable an account. Returns False if the user doesn't exist."""
    users = load_users()
    u = users.get(username.strip().lower())
    if u is None:
        return False
    u['active'] = active
    save_users(users)
    return True


def set_parts(username: str, parts: list) -> bool:
    """Replace a user's firmware allow-list. False if the user doesn't exist."""
    users = load_users()
    u = users.get(username.strip().lower())
    if u is None:
        return False
    u['parts'] = _clean_patterns(parts)
    save_users(users)
    return True


def set_admin(username: str, admin: bool) -> bool:
    """Grant/revoke HQ admin rights. False if the user doesn't exist."""
    users = load_users()
    u = users.get(username.strip().lower())
    if u is None:
        return False
    u['admin'] = bool(admin)
    save_users(users)
    return True


def delete_user(username: str) -> bool:
    """Remove an account entirely. False if it didn't exist."""
    users = load_users()
    if users.pop(username.strip().lower(), None) is None:
        return False
    save_users(users)
    return True


def is_admin(user: dict | None) -> bool:
    """True if *user* may manage other accounts. Defaults to False, so an
    upgraded server hands nobody admin rights by accident."""
    return bool(user and user.get('admin', False))


def describe_user(username: str, user: dict) -> dict:
    """The account as the admin UI sees it — never includes the password hash."""
    return {
        'username':    username,
        'distributor': user.get('distributor', ''),
        'active':      bool(user.get('active', False)),
        'admin':       is_admin(user),
        'parts':       user_parts(user),
    }


# ---------------------------------------------------------------------------
# Firmware allow-list
# ---------------------------------------------------------------------------

ALL_PARTS = ['*']


def _clean_patterns(parts) -> list:
    """Normalise an allow-list: strings only, trimmed, blanks dropped."""
    if not isinstance(parts, (list, tuple)):
        return list(ALL_PARTS)
    return [str(p).strip() for p in parts if str(p).strip()]


def user_parts(user: dict | None) -> list:
    """The allow-list for *user*. A missing key means 'everything' so that
    pre-existing accounts keep working after the server is upgraded."""
    if not user or 'parts' not in user:
        return list(ALL_PARTS)
    return _clean_patterns(user.get('parts'))


def is_part_allowed(user: dict | None, part: str) -> bool:
    """True if *user* may see/download the firmware folder *part*.

    Patterns are globs matched case-insensitively, so '1494X*' covers every
    1494X revision and an exact name matches only itself.
    """
    name = str(part).strip().lower()
    if not name:
        return False
    return any(
        fnmatch.fnmatchcase(name, pattern.lower())
        for pattern in user_parts(user)
    )


def filter_parts(user: dict | None, parts) -> list:
    """Keep only the parts *user* is allowed to see, preserving order."""
    return [p for p in parts if is_part_allowed(user, p)]


# ---------------------------------------------------------------------------
# Login-token signing / verification
# ---------------------------------------------------------------------------

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def make_token(username: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    """Return (token, expiry_unix) for *username*, signed with *secret*."""
    exp = int(time.time()) + int(ttl_seconds)
    payload = json.dumps(
        {'u': username.strip().lower(), 'exp': exp}, separators=(',', ':')
    ).encode('utf-8')
    sig = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).digest()
    return f'{_b64u(payload)}.{_b64u(sig)}', exp


def verify_token(token: str, secret: str) -> str | None:
    """Return the username if *token* is validly signed and unexpired, else None.

    Note: this does NOT check whether the user is still active — callers must
    re-check ``get_user(...)['active']`` so that disabling an account revokes
    access immediately, before the token's natural expiry.
    """
    if not token or not secret:
        return None
    try:
        payload_b64, sig_b64 = token.split('.')
        payload = _b64u_decode(payload_b64)
        expected = hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(_b64u_decode(sig_b64), expected):
            return None
        data = json.loads(payload)
        if int(data.get('exp', 0)) < int(time.time()):
            return None
        return data.get('u')
    except (ValueError, KeyError, AttributeError):
        return None
