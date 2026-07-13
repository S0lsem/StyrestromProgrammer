"""
Client-side login for the firmware proxy.

The app logs in once (username + password) and receives a signed token from the
proxy. That token is held here for the process and attached to every firmware
request by :mod:`mrs_protocol.github_downloader`. The raw password is never
stored — only the token, which the app persists (with its expiry) via QSettings.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LoginError(Exception):
    """Raised when a /login attempt fails (bad credentials, server, network)."""


class AuthenticationError(Exception):
    """Raised when a firmware request is rejected for auth reasons (401) —
    i.e. the token is missing, expired, or the account was disabled. The app
    should prompt for login again."""


# Process-wide current token (set on login / restored from settings at startup).
_current_token = ''


def set_token(token: str) -> None:
    global _current_token
    _current_token = token or ''


def get_token() -> str:
    return _current_token


def clear_token() -> None:
    set_token('')


def login(username: str, password: str) -> dict:
    """POST credentials to the proxy ``/login``.

    On success sets the process token and returns the server payload
    ``{token, expires_at, username, distributor}``. Raises :class:`LoginError`
    on any failure.
    """
    from .config import PROXY_URL, PROXY_API_KEY

    url = f'{PROXY_URL.rstrip("/")}/login'
    body = json.dumps({
        'username': username.strip(),
        'password': password,
    }).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if PROXY_API_KEY:
        headers['X-Api-Key'] = PROXY_API_KEY

    req = Request(url, data=body, headers=headers, method='POST')
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except HTTPError as exc:
        if exc.code == 401:
            raise LoginError('Invalid username or password.') from exc
        if exc.code == 500:
            raise LoginError(
                'Server login is not configured yet. Contact HQ.'
            ) from exc
        raise LoginError(f'Login failed (server error {exc.code}).') from exc
    except URLError as exc:
        raise LoginError(
            'Cannot reach the server — check your internet connection.'
        ) from exc

    token = data.get('token', '')
    if not token:
        raise LoginError('Server did not return a login token.')
    set_token(token)
    return data
