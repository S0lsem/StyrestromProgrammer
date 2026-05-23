"""
Downloads firmware files via the Styrestrøm proxy server.

The proxy server holds the GitHub token — this module never touches it.
All requests go through PROXY_URL configured in config.py.
"""
from __future__ import annotations

import base64
import json
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _proxy_get(endpoint: str):
    from .config import PROXY_URL, PROXY_API_KEY
    url = f'{PROXY_URL.rstrip("/")}/{endpoint.lstrip("/")}'
    headers = {'Accept': 'application/json'}
    if PROXY_API_KEY:
        headers['X-Api-Key'] = PROXY_API_KEY
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        if exc.code == 403:
            raise PermissionError(
                'Access denied by proxy server. Check PROXY_API_KEY in config.py.'
            ) from exc
        if exc.code == 404:
            raise FileNotFoundError(
                f'Not found: {endpoint}'
            ) from exc
        body = ''
        try:
            body = exc.read().decode()
        except Exception:
            pass
        raise RuntimeError(f'Server error {exc.code}: {body}') from exc
    except URLError as exc:
        raise ConnectionError(
            f'Cannot reach server at {url} — check your internet connection.'
        ) from exc


def list_parts() -> list[str]:
    """Return sorted list of part folder names from the proxy."""
    return _proxy_get('/parts')


def download_part(
    part: str,
    file_set,
    progress: Optional[Callable[[float, str], None]] = None,
) -> list[str]:
    """
    Download all files for *part* via the proxy and load them into *file_set*.

    Args:
        part:      Folder name (e.g. '1493X_HB_RELAY_V2').
        file_set:  An MRSFileSet instance to load files into.
        progress:  Optional callback(fraction, message) for progress reporting.

    Returns:
        List of slot tags that were successfully loaded.
    """
    if progress:
        progress(0.0, f'Downloading {part}…')

    files = _proxy_get(f'/parts/{part}/files')

    if not files:
        raise FileNotFoundError(f"No files found for part '{part}'.")

    loaded_tags: list[str] = []

    for idx, item in enumerate(files):
        name = item['name']
        if progress:
            progress((idx + 1) / len(files), f'Loading {name}…')

        data = base64.b64decode(item['content'])

        try:
            slot = file_set.load_bytes(name, data)
            loaded_tags.append(slot.tag)
        except ValueError:
            pass  # file doesn't match any expected slot — skip silently

    if progress:
        progress(1.0, 'Download complete')

    return loaded_tags
