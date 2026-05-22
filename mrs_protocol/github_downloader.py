"""
Downloads firmware files from the private Styrestrøm GitHub repository.

The repo is expected to have one top-level folder per part number, each
containing the files needed by MRSFileSet (usercode.c, usercode.h, etc.).

Example repo layout:
    14934X_HB_REALY_V2/
        usercode.c
        usercode.h
        candb.c
        candb.h
        Dsl_cfg
    1494X_32BIT_CANFD_REALY/
        usercode.c
        ...
"""
from __future__ import annotations

import base64
import json
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_API = 'https://api.github.com'


def _headers() -> dict:
    from .config import GITHUB_TOKEN
    return {
        'Authorization': f'Bearer {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }


def _get(path: str):
    from .config import GITHUB_OWNER, GITHUB_REPO
    url = f'{_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}'
    req = Request(url, headers=_headers())
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        if exc.code == 401:
            raise PermissionError(
                'GitHub token is invalid or expired. '
                'Update GITHUB_TOKEN in mrs_protocol/config.py.'
            ) from exc
        if exc.code == 404:
            raise FileNotFoundError(
                f'Repository or path not found: {path!r}. '
                'Check GITHUB_REPO in mrs_protocol/config.py.'
            ) from exc
        raise RuntimeError(f'GitHub API error {exc.code}: {exc.reason}') from exc
    except URLError as exc:
        raise ConnectionError(f'Network error: {exc.reason}') from exc


def list_parts() -> list[str]:
    """Return sorted list of part folder names from the firmware repo root."""
    items = _get('')
    return sorted(
        item['name']
        for item in items
        if item['type'] == 'dir' and not item['name'].startswith('.')
    )


def download_part(
    part: str,
    file_set,
    progress: Optional[Callable[[float, str], None]] = None,
) -> list[str]:
    """
    Download all files for *part* from GitHub and load them into *file_set*.

    Args:
        part:      Folder name in the repo (e.g. '14934X_HB_REALY_V2').
        file_set:  An MRSFileSet instance to load files into.
        progress:  Optional callback(fraction, message) for progress reporting.

    Returns:
        List of slot tags that were successfully loaded.
    """
    items = _get(part)
    files = [item for item in items if item['type'] == 'file']

    if not files:
        raise FileNotFoundError(f"No files found in part folder '{part}'.")

    loaded_tags: list[str] = []

    for idx, item in enumerate(files):
        name = item['name']
        if progress:
            progress(idx / len(files), f'Downloading {name}…')

        # The contents API returns base64-encoded file data inline (<1 MB).
        info = _get(f'{part}/{name}')
        data = base64.b64decode(info['content'].replace('\n', ''))

        try:
            slot = file_set.load_bytes(name, data)
            loaded_tags.append(slot.tag)
        except ValueError:
            pass  # file doesn't match any expected slot — skip silently

    if progress:
        progress(1.0, 'Download complete')

    return loaded_tags
