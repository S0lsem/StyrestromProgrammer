"""Tests for the per-distributor firmware allow-list in server/user_store.py.

The store is deliberately dependency-free stdlib, so it can be imported and
exercised straight from the repo without Flask or a running proxy.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent / 'server'


@pytest.fixture
def store(tmp_path, monkeypatch):
    """user_store bound to a throwaway users.json."""
    monkeypatch.setenv('USERS_FILE', str(tmp_path / 'users.json'))
    monkeypatch.syspath_prepend(str(SERVER_DIR))
    sys.modules.pop('user_store', None)
    module = importlib.import_module('user_store')
    yield module
    sys.modules.pop('user_store', None)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def test_missing_parts_key_means_everything(store):
    """Accounts created before this feature must keep working after upgrade."""
    legacy = {'pw': 'x', 'distributor': 'Old AS', 'active': True}
    assert store.is_part_allowed(legacy, '1493X-V4')
    assert store.filter_parts(legacy, ['a', 'b']) == ['a', 'b']


def test_star_allows_everything(store):
    user = {'parts': ['*']}
    assert store.is_part_allowed(user, 'anything-at-all')


def test_empty_list_allows_nothing(store):
    """An explicit empty list is a real 'no access', not a missing key."""
    user = {'parts': []}
    assert not store.is_part_allowed(user, '1493X-V4')
    assert store.filter_parts(user, ['1493X-V4', '1494X']) == []


def test_exact_name_matches_only_itself(store):
    user = {'parts': ['1493X-V4']}
    assert store.is_part_allowed(user, '1493X-V4')
    assert not store.is_part_allowed(user, '1494X')
    assert not store.is_part_allowed(user, '1493X-V4-beta')


def test_matching_ignores_case(store):
    user = {'parts': ['1493x-v4']}
    assert store.is_part_allowed(user, '1493X-V4')


def test_wildcard_covers_revisions(store):
    user = {'parts': ['1494X*']}
    assert store.is_part_allowed(user, '1494X CAN FD')
    assert store.is_part_allowed(user, '1494X')
    assert not store.is_part_allowed(user, '1493X-V4')


def test_filter_preserves_order(store):
    user = {'parts': ['b', 'c']}
    assert store.filter_parts(user, ['a', 'b', 'c', 'd']) == ['b', 'c']


def test_blank_part_is_never_allowed(store):
    assert not store.is_part_allowed({'parts': ['*']}, '   ')


def test_corrupt_parts_value_falls_back_to_all(store):
    """A hand-edited users.json must not silently lock a distributor out."""
    assert store.is_part_allowed({'parts': 'not-a-list'}, 'anything')


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def test_new_account_starts_with_everything(store):
    store.upsert_user('acme', 'hunter2!!', 'Acme AS')
    assert store.user_parts(store.get_user('acme')) == ['*']


def test_set_parts_round_trips(store):
    store.upsert_user('acme', 'hunter2!!', 'Acme AS')
    assert store.set_parts('acme', ['1493X-V4', ' 1494X* '])
    assert store.user_parts(store.get_user('acme')) == ['1493X-V4', '1494X*']


def test_set_parts_on_unknown_user_reports_failure(store):
    assert not store.set_parts('nobody', ['1493X-V4'])


def test_password_reset_keeps_the_allow_list(store):
    """`add` on an existing user must not silently re-open every part."""
    store.upsert_user('acme', 'hunter2!!', 'Acme AS')
    store.set_parts('acme', ['1493X-V4'])
    store.upsert_user('acme', 'new-password', 'Acme AS')
    assert store.user_parts(store.get_user('acme')) == ['1493X-V4']


def test_disabled_account_keeps_its_list_for_when_it_is_re_enabled(store):
    store.upsert_user('acme', 'hunter2!!', 'Acme AS')
    store.set_parts('acme', ['1493X-V4'])
    store.set_active('acme', False)
    store.set_active('acme', True)
    assert store.user_parts(store.get_user('acme')) == ['1493X-V4']


# --------------------------------------------------------------------------
# HQ admin rights
# --------------------------------------------------------------------------

def test_accounts_are_not_admin_by_default(store):
    """Upgrading the server must not hand anybody admin rights by accident."""
    store.upsert_user('acme', 'hunter2!!', 'Acme AS')
    assert not store.is_admin(store.get_user('acme'))
    assert not store.is_admin({'pw': 'x', 'active': True})   # pre-upgrade record
    assert not store.is_admin(None)


def test_set_admin_round_trips(store):
    store.upsert_user('hq', 'hunter2!!', 'Styrestrom AS')
    assert store.set_admin('hq', True)
    assert store.is_admin(store.get_user('hq'))
    assert store.set_admin('hq', False)
    assert not store.is_admin(store.get_user('hq'))


def test_set_admin_on_unknown_user_reports_failure(store):
    assert not store.set_admin('nobody', True)


def test_password_reset_keeps_admin_rights(store):
    """HQ resetting their own password must not lock them out of the admin UI."""
    store.upsert_user('hq', 'hunter2!!', 'Styrestrom AS', admin=True)
    store.upsert_user('hq', 'new-password', 'Styrestrom AS')
    assert store.is_admin(store.get_user('hq'))


def test_delete_user(store):
    store.upsert_user('acme', 'hunter2!!', 'Acme AS')
    assert store.delete_user('acme')
    assert store.get_user('acme') is None
    assert not store.delete_user('acme')


def test_describe_user_never_leaks_the_password_hash(store):
    store.upsert_user('acme', 'hunter2!!', 'Acme AS', admin=True)
    store.set_parts('acme', ['1493X-V4'])
    described = store.describe_user('acme', store.get_user('acme'))
    assert described == {
        'username': 'acme',
        'distributor': 'Acme AS',
        'active': True,
        'admin': True,
        'parts': ['1493X-V4'],
    }
    assert 'pw' not in described
