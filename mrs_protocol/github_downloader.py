"""
Downloads firmware (a single .s19 file) via the Styrestrøm proxy server.

The proxy holds the GitHub token; this module never touches it. All
requests go through PROXY_URL configured in config.py. Downloaded
firmware text is cached locally (encrypted) for offline use.
"""
from __future__ import annotations

import json
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .firmware_cache import cache_part, load_cached_part
from .s19_parser import Firmware, parse_s19


def _proxy_request(endpoint: str) -> tuple[int, bytes, str]:
    """Send a GET to the proxy. Returns (status, body, content_type)."""
    from .config import PROXY_URL, PROXY_API_KEY
    url = f'{PROXY_URL.rstrip("/")}/{endpoint.lstrip("/")}'
    headers = {'Accept': '*/*'}
    if PROXY_API_KEY:
        headers['X-Api-Key'] = PROXY_API_KEY
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers.get_content_type()
    except HTTPError as exc:
        if exc.code == 403:
            raise PermissionError(
                'Access denied by proxy server. Check PROXY_API_KEY in config.py.'
            ) from exc
        if exc.code == 404:
            raise FileNotFoundError(f'Not found: {endpoint}') from exc
        body = ''
        try:
            body = exc.read().decode()
        except Exception:
            pass
        raise RuntimeError(f'Server error {exc.code}: {body}') from exc
    except URLError as exc:
        raise ConnectionError(
            'Cannot reach server — check your internet connection.'
        ) from exc


def list_parts() -> list[str]:
    """Return sorted list of part folder names from the proxy."""
    _, body, _ = _proxy_request('/parts')
    return json.loads(body)


def download_part(
    part: str,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Firmware:
    """Download the firmware for *part* via the proxy, cache it, and parse it.

    Returns the parsed :class:`Firmware` image ready to be passed to
    :func:`mrs_protocol.console_flasher.run_flash`.
    """
    if progress:
        progress(0.0, f'Downloading {part}…')

    _, body, _ = _proxy_request(f'/parts/{part}/firmware')
    s19_text = body.decode('ascii', errors='strict')

    if progress:
        progress(0.7, 'Parsing firmware…')

    firmware = parse_s19(s19_text)

    # Cache the raw S19 text (encrypted at rest) for offline use.
    cache_part(part, s19_text)

    if progress:
        progress(1.0, 'Download complete')

    return firmware


def load_part_from_cache(
    part: str,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Firmware:
    """Load the firmware for *part* from the local cache (no internet needed).

    Raises FileNotFoundError if the part isn't cached.
    """
    s19_text = load_cached_part(part)
    if s19_text is None:
        raise FileNotFoundError(f"Part '{part}' is not in the local cache.")

    if progress:
        progress(0.5, f'Loading {part} from cache…')

    firmware = parse_s19(s19_text)

    if progress:
        progress(1.0, 'Loaded from cache')

    return firmware
