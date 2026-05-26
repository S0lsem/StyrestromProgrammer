"""
Local firmware cache — stores downloaded firmware files on disk
so they can be used without an internet connection.

Cache location: ~/.mrs_programmer/cache/<part_name>/
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def _cache_root() -> Path:
    root = Path.home() / '.mrs_programmer' / 'cache'
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_part(part: str, files: list[dict]) -> None:
    """
    Save downloaded firmware files to the local cache.

    Args:
        part:  Part folder name (e.g. '1493X_HB_RELAY_V2').
        files: List of dicts with 'name' and 'content' (base64) keys,
               as returned by the proxy server.
    """
    part_dir = _cache_root() / part
    part_dir.mkdir(parents=True, exist_ok=True)

    # Write the raw file list as JSON (preserves base64 content)
    manifest = part_dir / '_manifest.json'
    manifest.write_text(json.dumps(files, indent=2), encoding='utf-8')


def load_cached_part(part: str) -> Optional[list[dict]]:
    """
    Load firmware files from the local cache.

    Returns:
        List of dicts with 'name' and 'content' keys, or None if not cached.
    """
    manifest = _cache_root() / part / '_manifest.json'
    if not manifest.exists():
        return None
    try:
        return json.loads(manifest.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None


def list_cached_parts() -> list[str]:
    """Return sorted list of part names available in the local cache."""
    root = _cache_root()
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and (d / '_manifest.json').exists()
    )


def is_cached(part: str) -> bool:
    """Check if a part is available in the cache."""
    return (_cache_root() / part / '_manifest.json').exists()
