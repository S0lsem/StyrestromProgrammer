"""
Local firmware mirror — takes GitHub out of the runtime path.

Why this exists
---------------
The proxy used to call the GitHub Contents API on every ``/parts`` click and
every download. GitHub's 5,000/hour budget is *per user account* and shared
across every token that account owns, so anything else busy on the account
could — and on 2026-08-19 did — leave distributors unable to fetch firmware at
all, even though the proxy itself was nearly idle.

With a mirror, publishing and serving are separated:

  * **Publishing** (rare, HQ-initiated): ``sync()`` walks the firmware repo and
    copies every ``.s19`` to local disk. Costs ~1 + 2N GitHub calls.
  * **Serving** (constant, distributor-facing): reads from that local copy and
    costs **nothing**. A GitHub outage, or a spent quota, is invisible.

The mirror lives on HQ's own server, never on a distributor's machine — the
app still receives firmware as text over an authenticated connection and never
writes a .s19 to a distributor's disk.

Layout on disk::

    <mirror>/index.json          {"synced_at": 1787…, "parts": {…}}
    <mirror>/<part>/<file>.s19

``index.json`` is written last and atomically, so a sync that dies half way
leaves the previous index — and therefore the previous, complete answer —
untouched.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time

# Where the mirror lives. Override with FIRMWARE_MIRROR (e.g. to put it on a
# different PythonAnywhere disk).
MIRROR_DIR = os.environ.get(
    'FIRMWARE_MIRROR', os.path.join(os.path.dirname(__file__), 'firmware_mirror')
)

_INDEX_NAME = 'index.json'

# A part name becomes a folder name, so refuse anything that could climb out
# of the mirror directory. GitHub folder names are already tame; this is a
# belt-and-braces check on data we did not create.
_SAFE_PART = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,99}$')


def _index_path() -> str:
    return os.path.join(MIRROR_DIR, _INDEX_NAME)


def is_safe_part(part: str) -> bool:
    return bool(_SAFE_PART.match(str(part))) and '..' not in str(part)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_index() -> dict:
    """The mirror's index, or an empty one when nothing has been synced."""
    try:
        with open(_index_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {'synced_at': 0, 'parts': {}}
    if not isinstance(data, dict) or not isinstance(data.get('parts'), dict):
        return {'synced_at': 0, 'parts': {}}
    return data


def is_populated() -> bool:
    """True when the mirror holds at least one part and can serve on its own."""
    return bool(load_index().get('parts'))


def list_parts() -> list:
    return sorted(load_index().get('parts', {}))


def read_s19(part: str) -> str | None:
    """The S-record text for *part*, or None if the mirror does not have it."""
    if not is_safe_part(part):
        return None
    entry = load_index().get('parts', {}).get(part)
    if not entry:
        return None
    path = os.path.join(MIRROR_DIR, part, entry.get('file', ''))
    try:
        with open(path, 'r', encoding='ascii', errors='strict') as f:
            return f.read()
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        # Index and disk disagree — treat as a miss so the caller can fall
        # back to a live fetch rather than serving nothing.
        return None


def status() -> dict:
    """Summary for the admin window."""
    index = load_index()
    parts = index.get('parts', {})
    return {
        'populated':  bool(parts),
        'part_count': len(parts),
        'synced_at':  int(index.get('synced_at', 0)),
        'parts':      sorted(parts),
        'directory':  MIRROR_DIR,
    }


# ---------------------------------------------------------------------------
# Syncing
# ---------------------------------------------------------------------------

def sync(list_dir, get_file) -> dict:
    """Rebuild the mirror from the firmware repo.

    *list_dir(path)* returns the GitHub contents listing for a repo path, and
    *get_file(path)* returns the file object with its base64 ``content``. Both
    are injected so this module never talks to the network itself — the proxy
    owns the token, and tests can drive it with fakes.

    Builds into a temporary directory and swaps it in only once every part has
    been fetched, so a failed sync never leaves a half-populated mirror.

    Returns {'parts': [...], 'skipped': [...], 'synced_at': int}.
    """
    listing = list_dir('')
    part_names = sorted(
        item['name'] for item in listing
        if item.get('type') == 'dir' and not item['name'].startswith('.')
    )

    parent = os.path.dirname(os.path.abspath(MIRROR_DIR)) or '.'
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix='.firmware_sync_', dir=parent)

    parts: dict = {}
    skipped: list = []
    try:
        for part in part_names:
            if not is_safe_part(part):
                skipped.append({'part': part, 'reason': 'unsafe folder name'})
                continue
            candidates = _s19_candidates(part, list_dir)
            if not candidates:
                skipped.append({'part': part, 'reason': 'no .s19 file found'})
                continue
            if len(candidates) > 1:
                names = ', '.join(sorted(n for _folder, n in candidates))
                skipped.append({
                    'part': part,
                    'reason': (f'{len(candidates)} .s19 files found ({names}) — '
                               f'cannot tell which is the current build; leave '
                               f'only one in the folder'),
                })
                continue
            folder, name = candidates[0]
            info = get_file(f'{folder}/{name}')
            try:
                text = _decode(info)
            except (ValueError, KeyError):
                skipped.append({'part': part, 'reason': 'file could not be read'})
                continue

            part_dir = os.path.join(staging, part)
            os.makedirs(part_dir, exist_ok=True)
            # LF endings: the .NET flasher reads them fine and the app rewrites
            # the file anyway, but keep the bytes exactly as GitHub had them.
            with open(os.path.join(part_dir, name), 'w',
                      encoding='ascii', newline='') as f:
                f.write(text)
            parts[part] = {
                'file': name,
                'size': len(text),
                'sha':  str(info.get('sha', '')),
            }

        index = {'synced_at': int(time.time()), 'parts': parts}
        with open(os.path.join(staging, _INDEX_NAME), 'w', encoding='utf-8') as f:
            json.dump(index, f, indent=2, sort_keys=True)

        _swap_in(staging)
        staging = None                      # ownership handed over
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)

    return {'parts': sorted(parts), 'skipped': skipped,
            'synced_at': index['synced_at']}


def _s19_candidates(part: str, list_dir) -> list:
    """Every .s19 in the part folder, else every .s19 in its src/ subfolder.

    Returns a list of ``(folder, name)``. The part root wins outright: if it
    holds any .s19 at all, ``src/`` is not consulted.
    """
    for folder in (part, f'{part}/src'):
        try:
            items = list_dir(folder)
        except Exception:               # 404 for an absent src/ is normal
            continue
        found = [
            (folder, item['name']) for item in items
            if item.get('type') == 'file' and item['name'].lower().endswith('.s19')
        ]
        if found:
            return found
    return []


def _find_s19(part: str, list_dir):
    """The one .s19 for *part*, or None if there isn't exactly one.

    Deliberately refuses to choose between several. It used to return whichever
    came first in GitHub's listing — which is alphabetical, not newest — so a
    stale build left beside the current one would be mirrored silently and for
    ever, and no amount of re-syncing would correct it. A part that is skipped
    is visible in the sync result; a part quietly pinned to the wrong firmware
    is not.
    """
    candidates = _s19_candidates(part, list_dir)
    return candidates[0] if len(candidates) == 1 else None


def _decode(info: dict) -> str:
    import base64
    content = str(info['content']).replace('\n', '')
    return base64.b64decode(content).decode('ascii', errors='strict')


def _swap_in(staging: str) -> None:
    """Replace the live mirror with *staging* as nearly atomically as the
    filesystem allows, keeping the old copy until the new one is in place."""
    target = os.path.abspath(MIRROR_DIR)
    previous = target + '.old'
    shutil.rmtree(previous, ignore_errors=True)
    if os.path.isdir(target):
        os.replace(target, previous)
    os.replace(staging, target)
    shutil.rmtree(previous, ignore_errors=True)
