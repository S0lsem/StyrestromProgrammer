"""
Downloads firmware files via the Styrestrøm proxy server.

The proxy server holds the GitHub token — this module never touches it.
All requests go through PROXY_URL configured in config.py.

Downloaded files are cached locally for offline use.
"""
from __future__ import annotations

import base64
import json
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .firmware_cache import cache_part, load_cached_part


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
            f'Cannot reach server — check your internet connection.'
        ) from exc


def list_parts() -> list[str]:
    """Return sorted list of part folder names from the proxy."""
    return _proxy_get('/parts')


def _load_files_into_fileset(files: list[dict], file_set, progress, label: str) -> list[str]:
    """Load a list of file dicts (name + base64 content) into a file set."""
    loaded_tags: list[str] = []
    for idx, item in enumerate(files):
        name = item['name']
        if progress:
            progress((idx + 1) / len(files), f'{label} {name}…')
        data = base64.b64decode(item['content'])
        try:
            slot = file_set.load_bytes(name, data)
            loaded_tags.append(slot.tag)
        except ValueError:
            pass
    return loaded_tags


def download_part(
    part: str,
    file_set,
    progress: Optional[Callable[[float, str], None]] = None,
) -> list[str]:
    """
    Download files for *part* via the proxy, cache them, and load into *file_set*.
    """
    if progress:
        progress(0.0, f'Downloading {part}…')

    files = _proxy_get(f'/parts/{part}/files')

    if not files:
        raise FileNotFoundError(f"No files found for part '{part}'.")

    # Cache for offline use
    cache_part(part, files)

    loaded_tags = _load_files_into_fileset(files, file_set, progress, 'Loading')

    if progress:
        progress(1.0, 'Download complete')

    return loaded_tags


def load_part_from_cache(
    part: str,
    file_set,
    progress: Optional[Callable[[float, str], None]] = None,
) -> list[str]:
    """
    Load files for *part* from the local cache (no internet needed).

    Raises FileNotFoundError if not cached.
    """
    files = load_cached_part(part)
    if files is None:
        raise FileNotFoundError(f"Part '{part}' is not in the local cache.")

    if progress:
        progress(0.0, f'Loading {part} from cache…')

    loaded_tags = _load_files_into_fileset(files, file_set, progress, 'Loading')

    if progress:
        progress(1.0, 'Loaded from cache')

    return loaded_tags
