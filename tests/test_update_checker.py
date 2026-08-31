"""The staged-rollout gate.

HQ publishes with ``release.ps1 -Prerelease`` and tries the build on its own
bench. Distributors must not be offered it until ``promote.ps1`` clears the
flag. The whole gate rests on one GitHub behaviour: ``/releases/latest`` skips
prereleases, so a distributor's client simply cannot see one.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

from mrs_protocol import update_checker


def _release(tag, *, prerelease=False, draft=False, asset=True):
    return {
        'tag_name': tag,
        'prerelease': prerelease,
        'draft': draft,
        'body': f'notes for {tag}',
        'assets': (
            [{'name': 'Styrestrom_Programmer.exe',
              'browser_download_url': f'https://example.invalid/{tag}.exe'}]
            if asset else []
        ),
    }


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub. Records the URLs asked for, replies from `state`."""
    state = {'latest': None, 'list': [], 'urls': []}

    class FakeResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        state['urls'].append(url)
        if '/releases/latest' in url:
            if state['latest'] is None:
                raise HTTPError(url, 404, 'Not Found', None, None)
            return FakeResponse(state['latest'])
        if '/releases?' in url:
            return FakeResponse(state['list'])
        raise AssertionError(f'unexpected url {url}')

    monkeypatch.setattr(update_checker, 'urlopen', fake_urlopen)
    monkeypatch.setattr(update_checker, 'APP_VERSION', '1.0.17')
    return state


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_distributor_is_not_offered_a_prerelease(github):
    """The point of the whole feature."""
    github['latest'] = None                                  # GitHub hides it
    github['list'] = [_release('v1.0.18', prerelease=True)]

    result = update_checker.check_for_update(include_prerelease=False)

    assert result['update_available'] is False
    assert all('/releases/latest' in u for u in github['urls'])


def test_hq_is_offered_the_prerelease(github):
    github['list'] = [_release('v1.0.18', prerelease=True)]

    result = update_checker.check_for_update(include_prerelease=True)

    assert result['update_available'] is True
    assert result['latest_version'] == 'v1.0.18'
    assert result['download_url'] == 'https://example.invalid/v1.0.18.exe'


def test_distributor_is_offered_a_promoted_release(github):
    """After promote.ps1 clears the flag, GitHub returns it from /latest."""
    github['latest'] = _release('v1.0.18', prerelease=False)

    result = update_checker.check_for_update(include_prerelease=False)

    assert result['update_available'] is True
    assert result['latest_version'] == 'v1.0.18'


def test_default_is_the_gated_path(github):
    """A caller that forgets the flag must get the safe behaviour."""
    github['latest'] = None
    github['list'] = [_release('v1.0.18', prerelease=True)]

    assert update_checker.check_for_update()['update_available'] is False


# ---------------------------------------------------------------------------
# Selection rules
# ---------------------------------------------------------------------------

def test_drafts_are_never_offered(github):
    """A draft has no uploaded asset — offering it would be a dead button."""
    github['list'] = [_release('v1.0.19', draft=True),
                      _release('v1.0.18', prerelease=True)]

    result = update_checker.check_for_update(include_prerelease=True)

    assert result['latest_version'] == 'v1.0.18'


def test_hq_takes_the_newest_release_github_returns(github):
    github['list'] = [_release('v1.0.19', prerelease=True),
                      _release('v1.0.18', prerelease=False)]

    assert update_checker.check_for_update(
        include_prerelease=True)['latest_version'] == 'v1.0.19'


def test_older_release_is_not_an_update(github):
    github['latest'] = _release('v1.0.16')

    assert update_checker.check_for_update()['update_available'] is False


def test_same_version_is_not_an_update(github):
    github['latest'] = _release('v1.0.17')

    assert update_checker.check_for_update()['update_available'] is False


# ---------------------------------------------------------------------------
# Failure handling — an update check must never be load-bearing
# ---------------------------------------------------------------------------

def test_no_releases_at_all_is_reported_not_raised(github):
    github['latest'] = None

    result = update_checker.check_for_update()

    assert result['update_available'] is False
    assert result['error']


def test_empty_release_list_for_hq(github):
    github['list'] = []

    result = update_checker.check_for_update(include_prerelease=True)

    assert result['update_available'] is False
    assert result['error']
