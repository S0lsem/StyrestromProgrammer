"""
Local firmware cache — stores downloaded firmware files on disk
so they can be used without an internet connection.

The cache is encrypted (Fernet / AES-128-CBC + HMAC-SHA256) with a key
derived from two byte arrays XORed at runtime. The cache file is opaque
binary; the firmware bytes are never written to disk in the clear.

Cache location: ~/.mrs_programmer/cache/<part_name>/_manifest.bin
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


# Two random 32-byte arrays. XORing them yields the raw key material;
# the real key only exists in memory at runtime. Splitting it like this
# prevents a casual `strings` / hex-dump search of the bundled exe from
# turning up an obvious key.
_KEY_A = bytes.fromhex(
    '76bc366f5c1c3550d02f9f419c087f83'
    'b900fcdb7e70399f8ba5e37af68eb058'
)
_KEY_B = bytes.fromhex(
    '0057e0edb69ddbae718cc5e1c281c70b'
    '74b124c67a5b257299edbab2339132c2'
)

_MANIFEST_NAME = '_manifest.bin'
_LEGACY_MANIFEST_NAME = '_manifest.json'


def _key() -> bytes:
    raw = bytes(a ^ b for a, b in zip(_KEY_A, _KEY_B))
    return base64.urlsafe_b64encode(raw)


def _cache_root() -> Path:
    root = Path.home() / '.mrs_programmer' / 'cache'
    root.mkdir(parents=True, exist_ok=True)
    _wipe_legacy_plaintext(root)
    return root


def _wipe_legacy_plaintext(root: Path) -> None:
    """Delete any plaintext _manifest.json files left over from older builds.

    Older versions cached firmware as plaintext base64 JSON. Removing those
    files on every cache touch ensures an upgrade closes the leak without
    requiring the user to do anything.
    """
    try:
        for legacy in root.glob(f'*/{_LEGACY_MANIFEST_NAME}'):
            try:
                legacy.unlink()
            except OSError:
                pass
    except OSError:
        pass


def cache_part(part: str, files: list[dict]) -> None:
    """
    Save downloaded firmware files to the local cache (encrypted).

    Args:
        part:  Part folder name (e.g. '1493X_HB_RELAY_V2').
        files: List of dicts with 'name' and 'content' (base64) keys,
               as returned by the proxy server.
    """
    part_dir = _cache_root() / part
    part_dir.mkdir(parents=True, exist_ok=True)

    blob = json.dumps(files).encode('utf-8')
    token = Fernet(_key()).encrypt(blob)
    (part_dir / _MANIFEST_NAME).write_bytes(token)


def load_cached_part(part: str) -> Optional[list[dict]]:
    """
    Load firmware files from the local cache.

    Returns:
        List of dicts with 'name' and 'content' keys, or None if not cached
        or the cache file is corrupt / from an incompatible build.
    """
    manifest = _cache_root() / part / _MANIFEST_NAME
    if not manifest.exists():
        return None
    try:
        plaintext = Fernet(_key()).decrypt(manifest.read_bytes())
        return json.loads(plaintext)
    except (InvalidToken, json.JSONDecodeError, OSError, ValueError):
        return None


def list_cached_parts() -> list[str]:
    """Return sorted list of part names available in the local cache."""
    root = _cache_root()
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and (d / _MANIFEST_NAME).exists()
    )


def is_cached(part: str) -> bool:
    """Check if a part is available in the cache."""
    return (_cache_root() / part / _MANIFEST_NAME).exists()
