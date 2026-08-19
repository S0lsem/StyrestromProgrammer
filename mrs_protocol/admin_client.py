"""
HQ admin API client — backs the Distributors window in the programmer app.

Only accounts flagged ``admin`` on the server can use any of this; the proxy
re-checks that flag on every call, so this module carries no privilege of its
own. It simply wraps the ``/admin/*`` routes of the firmware proxy and turns
their JSON errors into plain-language exceptions the GUI can show verbatim.

Nothing here ever sees a password hash — the server returns accounts as
``{username, distributor, active, admin, parts}``.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .auth import AuthenticationError, get_token


class AdminError(Exception):
    """An admin call was refused or failed. The message is safe to show."""


def _request(method: str, endpoint: str, payload: dict | None = None):
    from .config import PROXY_URL, PROXY_API_KEY

    url = f'{PROXY_URL.rstrip("/")}/{endpoint.lstrip("/")}'
    headers = {'Accept': 'application/json'}
    token = get_token()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if PROXY_API_KEY:
        headers['X-Api-Key'] = PROXY_API_KEY

    body = None
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = _error_message(exc)
        if exc.code == 401:
            raise AuthenticationError(
                detail or 'Login required or expired. Please log in again.'
            ) from exc
        raise AdminError(detail or f'Server error {exc.code}.') from exc
    except URLError as exc:
        raise AdminError(
            'Cannot reach the server — check your internet connection.'
        ) from exc
    except ValueError as exc:
        raise AdminError('The server sent a reply the app could not read.') from exc


def _error_message(exc: HTTPError) -> str:
    """Pull the server's human-readable reason out of an error response."""
    try:
        body = exc.read().decode()
    except Exception:
        return ''
    try:
        return str(json.loads(body).get('error', '')).strip()
    except (ValueError, AttributeError):
        # Flask's abort() renders HTML; the description is the useful part.
        return ''


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def list_users() -> list[dict]:
    """Every account, sorted by username."""
    result = _request('GET', '/admin/users')
    return result if isinstance(result, list) else []


def list_all_parts() -> list[str]:
    """The full firmware catalogue — what an admin can hand out."""
    result = _request('GET', '/admin/parts')
    return result if isinstance(result, list) else []


def create_or_reset_user(username: str, password: str, distributor: str,
                         parts: list | None = None,
                         admin: bool | None = None) -> dict:
    """Create an account, or reset an existing one's password + distributor.

    Leaving *parts* / *admin* as None keeps an existing account's current
    values, so a password reset never silently widens someone's access.
    """
    payload: dict = {
        'username': username,
        'password': password,
        'distributor': distributor,
    }
    if parts is not None:
        payload['parts'] = list(parts)
    if admin is not None:
        payload['admin'] = bool(admin)
    return _request('POST', '/admin/users', payload)


def set_parts(username: str, parts: list) -> dict:
    """Replace which firmware *username* may see and download."""
    return _request('PATCH', f'/admin/users/{username}', {'parts': list(parts)})


def set_active(username: str, active: bool) -> dict:
    """Enable or disable an account. Takes effect on their next request."""
    return _request('PATCH', f'/admin/users/{username}', {'active': bool(active)})


def set_admin(username: str, admin: bool) -> dict:
    """Grant or revoke HQ admin rights."""
    return _request('PATCH', f'/admin/users/{username}', {'admin': bool(admin)})


def delete_user(username: str) -> dict:
    """Remove an account entirely."""
    return _request('DELETE', f'/admin/users/{username}')
