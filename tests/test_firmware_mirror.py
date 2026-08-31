"""Tests for the local firmware mirror in server/firmware_mirror.py.

The mirror is what keeps distributors working when GitHub is unreachable or
the account's API quota is spent, so the behaviour that matters most here is
what happens when a sync goes wrong: the previous, complete copy must survive.
"""
from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent / 'server'

S19 = 'S00600004844521B\nS1130000285F245F2212226A000424290008237C2A\nS9030000FC'


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    """firmware_mirror bound to a throwaway directory."""
    monkeypatch.setenv('FIRMWARE_MIRROR', str(tmp_path / 'mirror'))
    monkeypatch.syspath_prepend(str(SERVER_DIR))
    sys.modules.pop('firmware_mirror', None)
    module = importlib.import_module('firmware_mirror')
    yield module
    sys.modules.pop('firmware_mirror', None)


def make_repo(parts, text=S19, in_src=()):
    """Fake GitHub: returns (list_dir, get_file) over *parts*.

    Names in *in_src* keep their .s19 under src/ instead of the part root.
    """
    calls = []

    def list_dir(sub):
        calls.append(sub)
        if sub == '':
            return [{'type': 'dir', 'name': p} for p in parts]
        part, _, tail = sub.partition('/')
        if part not in parts:
            raise FileNotFoundError(sub)
        wants_src = part in in_src
        if tail == 'src':
            if not wants_src:
                raise FileNotFoundError(sub)
            return [{'type': 'file', 'name': 'firmware.s19'}]
        if wants_src:
            return [{'type': 'file', 'name': 'readme.txt'}]
        return [{'type': 'file', 'name': 'firmware.s19'}]

    def get_file(path):
        calls.append(path)
        return {'content': base64.b64encode(text.encode()).decode(), 'sha': 'sha-' + path}

    return list_dir, get_file, calls


# --------------------------------------------------------------------------
# Syncing
# --------------------------------------------------------------------------

def test_sync_copies_every_part(mirror):
    list_dir, get_file, _ = make_repo(['1493X-V4', '1494X CAN FD'])
    result = mirror.sync(list_dir, get_file)
    assert result['parts'] == ['1493X-V4', '1494X CAN FD']
    assert result['skipped'] == []
    assert mirror.list_parts() == ['1493X-V4', '1494X CAN FD']
    assert mirror.read_s19('1493X-V4') == S19


def test_sync_finds_firmware_under_src(mirror):
    list_dir, get_file, _ = make_repo(['1493X-V4'], in_src=('1493X-V4',))
    mirror.sync(list_dir, get_file)
    assert mirror.read_s19('1493X-V4') == S19


def test_part_without_firmware_is_skipped_not_fatal(mirror):
    """One bad folder must not cost you the whole catalogue."""
    def list_dir(sub):
        if sub == '':
            return [{'type': 'dir', 'name': 'good'}, {'type': 'dir', 'name': 'empty'}]
        if sub == 'good':
            return [{'type': 'file', 'name': 'firmware.s19'}]
        if sub == 'empty':
            return [{'type': 'file', 'name': 'notes.txt'}]
        raise FileNotFoundError(sub)

    def get_file(path):
        return {'content': base64.b64encode(S19.encode()).decode(), 'sha': 'x'}

    result = mirror.sync(list_dir, get_file)
    assert result['parts'] == ['good']
    assert [s['part'] for s in result['skipped']] == ['empty']
    assert mirror.read_s19('good') == S19


def test_two_s19_files_are_skipped_rather_than_guessed(mirror):
    """Never silently choose between builds.

    GitHub lists a folder alphabetically, so picking "the first .s19" picks by
    filename, not by date. A stale build left beside the current one would be
    mirrored for ever and no amount of re-syncing would correct it — the exact
    failure that cannot be diagnosed from the distributor's end, because
    everything looks like it worked.
    """
    def list_dir(sub):
        if sub == '':
            return [{'type': 'dir', 'name': 'PART_A'}]
        if sub == 'PART_A':
            return [{'type': 'file', 'name': 'aaa_old_build.s19'},
                    {'type': 'file', 'name': 'zzz_current_build.s19'}]
        raise FileNotFoundError(sub)

    def get_file(path):
        raise AssertionError(f'should not have fetched anything, got {path}')

    result = mirror.sync(list_dir, get_file)

    assert result['parts'] == []
    assert len(result['skipped']) == 1
    reason = result['skipped'][0]['reason']
    assert 'aaa_old_build.s19' in reason and 'zzz_current_build.s19' in reason
    assert '2 .s19 files' in reason


def test_a_single_s19_still_syncs(mirror):
    """The guard must not break the ordinary case."""
    list_dir, get_file, _ = make_repo(['PART_A'])

    result = mirror.sync(list_dir, get_file)

    assert result['parts'] == ['PART_A']
    assert result['skipped'] == []


def test_root_s19_wins_over_src(mirror):
    """A part root holding a .s19 is answered from there; src/ is not consulted,
    so the same file appearing in both is not treated as ambiguous."""
    def list_dir(sub):
        if sub == '':
            return [{'type': 'dir', 'name': 'PART_A'}]
        if sub == 'PART_A':
            return [{'type': 'file', 'name': 'firmware.s19'}]
        if sub == 'PART_A/src':
            return [{'type': 'file', 'name': 'firmware.s19'}]
        raise FileNotFoundError(sub)

    def get_file(path):
        assert path == 'PART_A/firmware.s19', path
        return {'content': base64.b64encode(S19.encode()).decode(), 'sha': 'x'}

    result = mirror.sync(list_dir, get_file)

    assert result['parts'] == ['PART_A']
    assert result['skipped'] == []


def test_resync_replaces_changed_firmware(mirror):
    list_dir, get_file, _ = make_repo(['1493X-V4'])
    mirror.sync(list_dir, get_file)
    newer = S19.replace('S9030000FC', 'S9030000FD')
    list_dir2, get_file2, _ = make_repo(['1493X-V4'], text=newer)
    mirror.sync(list_dir2, get_file2)
    assert mirror.read_s19('1493X-V4') == newer


def test_resync_drops_parts_removed_upstream(mirror):
    list_dir, get_file, _ = make_repo(['old', 'kept'])
    mirror.sync(list_dir, get_file)
    list_dir2, get_file2, _ = make_repo(['kept'])
    mirror.sync(list_dir2, get_file2)
    assert mirror.list_parts() == ['kept']
    assert mirror.read_s19('old') is None


def test_failed_sync_leaves_the_previous_mirror_intact(mirror):
    """The whole point: a sync that dies must not take firmware delivery down."""
    list_dir, get_file, _ = make_repo(['1493X-V4', '1494X CAN FD'])
    mirror.sync(list_dir, get_file)
    before = mirror.list_parts()
    synced_at = mirror.load_index()['synced_at']

    def exploding_get_file(path):
        raise ConnectionError('GitHub went away mid-sync')

    with pytest.raises(ConnectionError):
        mirror.sync(list_dir, exploding_get_file)

    assert mirror.list_parts() == before
    assert mirror.read_s19('1493X-V4') == S19
    assert mirror.load_index()['synced_at'] == synced_at


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def test_empty_mirror_reports_itself_as_empty(mirror):
    assert not mirror.is_populated()
    assert mirror.list_parts() == []
    assert mirror.read_s19('anything') is None
    assert mirror.status()['populated'] is False


def test_unknown_part_reads_as_missing(mirror):
    list_dir, get_file, _ = make_repo(['1493X-V4'])
    mirror.sync(list_dir, get_file)
    assert mirror.read_s19('nosuch') is None


def test_index_pointing_at_a_deleted_file_reads_as_missing(mirror):
    """Index and disk disagreeing must be a miss, so the caller can fall back."""
    list_dir, get_file, _ = make_repo(['1493X-V4'])
    mirror.sync(list_dir, get_file)
    (Path(mirror.MIRROR_DIR) / '1493X-V4' / 'firmware.s19').unlink()
    assert mirror.read_s19('1493X-V4') is None


def test_corrupt_index_is_treated_as_empty(mirror):
    list_dir, get_file, _ = make_repo(['1493X-V4'])
    mirror.sync(list_dir, get_file)
    (Path(mirror.MIRROR_DIR) / 'index.json').write_text('{not json', encoding='utf-8')
    assert not mirror.is_populated()
    assert mirror.read_s19('1493X-V4') is None


# --------------------------------------------------------------------------
# Path safety — part names come from a repo listing, not from us
# --------------------------------------------------------------------------

@pytest.mark.parametrize('bad', [
    '../etc', '..', 'a/b', '/absolute', 'C:\\windows', '', '.hidden', 'x' * 200,
])
def test_unsafe_part_names_are_refused(mirror, bad):
    assert not mirror.is_safe_part(bad)
    assert mirror.read_s19(bad) is None


@pytest.mark.parametrize('good', ['1493X-V4', '14930 Taxi', '1494X CAN FD', 'a_b+c.d'])
def test_ordinary_part_names_are_allowed(mirror, good):
    assert mirror.is_safe_part(good)


def test_sync_skips_a_dangerous_folder_name(mirror):
    """A name that would write outside the mirror is skipped and reported.

    Names beginning with a dot never get this far — they are filtered as
    hidden folders first (see the test below) — so the guard is exercised
    with a separator instead.
    """
    def list_dir(sub):
        if sub == '':
            return [{'type': 'dir', 'name': 'bad/name'},
                    {'type': 'dir', 'name': 'fine'}]
        if sub == 'fine':
            return [{'type': 'file', 'name': 'firmware.s19'}]
        raise FileNotFoundError(sub)

    def get_file(path):
        return {'content': base64.b64encode(S19.encode()).decode(), 'sha': 'x'}

    result = mirror.sync(list_dir, get_file)
    assert result['parts'] == ['fine']
    assert [s['part'] for s in result['skipped']] == ['bad/name']


def test_sync_ignores_hidden_folders(mirror):
    """Dot-prefixed repo folders (.github and friends) are not parts."""
    def list_dir(sub):
        if sub == '':
            return [{'type': 'dir', 'name': '.github'},
                    {'type': 'dir', 'name': '../escape'},
                    {'type': 'dir', 'name': 'fine'}]
        if sub == 'fine':
            return [{'type': 'file', 'name': 'firmware.s19'}]
        raise FileNotFoundError(sub)

    def get_file(path):
        return {'content': base64.b64encode(S19.encode()).decode(), 'sha': 'x'}

    result = mirror.sync(list_dir, get_file)
    assert result['parts'] == ['fine']
    assert result['skipped'] == []
    assert mirror.list_parts() == ['fine']


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

def test_status_after_sync(mirror):
    list_dir, get_file, _ = make_repo(['a', 'b', 'c'])
    mirror.sync(list_dir, get_file)
    status = mirror.status()
    assert status['populated'] is True
    assert status['part_count'] == 3
    assert status['parts'] == ['a', 'b', 'c']
    assert status['synced_at'] > 0


def test_sync_cost_is_bounded(mirror):
    """~1 + 2 per part. This is the only routine GitHub spend left."""
    list_dir, get_file, calls = make_repo(['a', 'b', 'c'])
    mirror.sync(list_dir, get_file)
    assert len(calls) <= 1 + 2 * 3
