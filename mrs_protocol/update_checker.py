"""
Checks GitHub Releases for newer versions of MRS Programmer.

The StyrestromProgrammer repo uses GitHub Releases to publish new .exe files.
This module compares the local version against the latest release tag.
"""
from __future__ import annotations

import json
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .version import APP_VERSION

_REPO_OWNER = 'S0lsem'
_REPO_NAME = 'StyrestromProgrammer'
_API = 'https://api.github.com'


def _parse_version(tag: str) -> tuple[int, ...]:
    """Turn 'v1.2.3' or '1.2.3' into (1, 2, 3)."""
    clean = tag.lstrip('vV')
    return tuple(int(x) for x in clean.split('.') if x.isdigit())


def check_for_update() -> dict:
    """
    Check GitHub for a newer release.

    Returns a dict:
        {
            'update_available': bool,
            'current_version': str,
            'latest_version': str,       # tag name, e.g. 'v1.1.0'
            'download_url': str | None,  # browser download URL for the .exe
            'release_notes': str,        # body of the release
            'error': str | None,
        }
    """
    result = {
        'update_available': False,
        'current_version': APP_VERSION,
        'latest_version': APP_VERSION,
        'download_url': None,
        'release_notes': '',
        'error': None,
    }

    try:
        url = f'{_API}/repos/{_REPO_OWNER}/{_REPO_NAME}/releases/latest'
        req = Request(url, headers={
            'Accept': 'application/vnd.github.v3+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except HTTPError as exc:
        if exc.code == 404:
            result['error'] = 'No releases published yet.'
        else:
            result['error'] = f'GitHub API error: {exc.code}'
        return result
    except URLError as exc:
        result['error'] = f'Network error: {exc.reason}'
        return result
    except Exception as exc:
        result['error'] = str(exc)
        return result

    tag = data.get('tag_name', '')
    result['latest_version'] = tag
    result['release_notes'] = data.get('body', '') or ''

    # Find the .exe asset
    for asset in data.get('assets', []):
        if asset['name'].lower().endswith('.exe'):
            result['download_url'] = asset.get('browser_download_url')
            break

    # Compare versions
    try:
        current = _parse_version(APP_VERSION)
        latest = _parse_version(tag)
        if latest > current:
            result['update_available'] = True
    except (ValueError, IndexError):
        pass

    return result
