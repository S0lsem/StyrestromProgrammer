"""The admin window's health line must say *why* GitHub is unreachable.

/admin/status used to catch HTTPError, URLError, ValueError and KeyError alike
and report a bare ``available: false``. When the proxy's token expired, that
flag was the only signal — and it reads like a harmless hiccup rather than
"syncing is broken until you replace the token". The cause was only discovered
by pressing Sync and reading the 401 off an error dialog, though this call had
known it all along.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent / 'server'


@pytest.fixture
def flask_app(monkeypatch):
    monkeypatch.syspath_prepend(str(SERVER_DIR))
    sys.modules.pop('flask_app', None)
    module = importlib.import_module('flask_app')
    yield module
    sys.modules.pop('flask_app', None)


def test_expired_token_says_so_and_says_what_to_do(flask_app):
    reason = flask_app._quota_failure_reason(401)

    assert '401' in reason
    assert 'expired' in reason.lower() or 'revoked' in reason.lower()
    # The operator has to know where to fix it and that syncing is affected.
    assert 'GITHUB_TOKEN' in reason
    assert 'reload' in reason.lower()
    assert 'sync' in reason.lower()


def test_forbidden_distinguishes_quota_from_access(flask_app):
    reason = flask_app._quota_failure_reason(403)

    assert '403' in reason
    assert 'quota' in reason.lower()
    assert 'access' in reason.lower()


def test_not_found_points_at_repository_access(flask_app):
    reason = flask_app._quota_failure_reason(404)

    assert '404' in reason
    assert 'repo' in reason.lower()


def test_an_unexpected_code_still_names_it(flask_app):
    """Never swallow a status we did not anticipate."""
    reason = flask_app._quota_failure_reason(500)

    assert '500' in reason


@pytest.mark.parametrize('code', [401, 403, 404, 500, 502])
def test_every_reason_is_a_usable_sentence(flask_app, code):
    reason = flask_app._quota_failure_reason(code)

    assert reason and reason[0].isupper() and reason.rstrip().endswith('.')
    assert len(reason) > 20, 'a bare code is what we are replacing'
