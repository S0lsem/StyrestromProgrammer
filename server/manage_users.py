"""
Distributor account management for the firmware proxy.

Run this in a Bash console on PythonAnywhere (same folder as flask_app.py and
user_store.py), so it edits the same users.json the proxy reads. Passwords are
prompted for (never shown / never in shell history) and stored only as salted
PBKDF2 hashes.

Usage:
  python manage_users.py add     <username> "<Distributor name>"   # create / reset
  python manage_users.py disable <username>                        # revoke access
  python manage_users.py enable  <username>                        # restore access
  python manage_users.py list                                      # show accounts

  # Which firmware each distributor may see + download:
  python manage_users.py parts      <username> <part> [<part> ...] # only these
  python manage_users.py parts-all  <username>                     # everything
  python manage_users.py parts-none <username>                     # nothing

  # HQ accounts that may manage all of the above from inside the app:
  python manage_users.py admin    <username>                       # grant
  python manage_users.py no-admin <username>                       # revoke

Notes:
  * Usernames are case-insensitive (stored lower-case).
  * "add" on an existing username resets that user's password + distributor,
    and KEEPS their firmware list. A brand-new account starts with access to
    everything — narrow it straight away with "parts".
  * Disabling takes effect immediately — the user's next request is rejected,
    even if their login token hasn't expired yet. So does changing "parts".
  * You normally only need this console ONCE: grant your own account admin
    with "admin <username>", then manage every distributor from
    Settings -> Manage distributors in the programmer app.
  * Part names are the firmware folder names in the GitHub repo, exactly as
    they appear in the app's Part dropdown. They are matched without regard
    to upper/lower case, and "*" works as a wildcard inside a name:
        python manage_users.py parts acme 1493X-V4 "1494X*"
    ...gives Acme that one part plus every part starting with 1494X. Always
    quote a name containing "*", or the shell will expand it into filenames.
"""
from __future__ import annotations

import getpass
import sys

import user_store


def _prompt_new_password() -> str:
    pw1 = getpass.getpass('New password: ')
    if len(pw1) < 8:
        sys.exit('Password must be at least 8 characters.')
    pw2 = getpass.getpass('Repeat password: ')
    if pw1 != pw2:
        sys.exit('Passwords do not match.')
    return pw1


def cmd_add(argv: list) -> None:
    if len(argv) != 2:
        sys.exit('Usage: python manage_users.py add <username> "<Distributor name>"')
    username, distributor = argv[0], argv[1]
    existing = user_store.get_user(username)
    if existing:
        print(f"User '{username}' exists — this resets their password and distributor.")
    password = _prompt_new_password()
    # parts=None keeps an existing user's firmware list untouched; a new
    # account defaults to everything.
    user_store.upsert_user(username, password, distributor, active=True)
    print(f"OK — '{username.lower()}' ({distributor}) is active and can log in.")
    print(f'     Firmware: {_describe_parts(user_store.get_user(username))}')
    if not existing:
        print(f'     Restrict it with: python manage_users.py parts {username.lower()} <part>...')


def _describe_parts(user: dict | None) -> str:
    """Human-readable summary of a user's firmware allow-list."""
    parts = user_store.user_parts(user)
    if not parts:
        return 'NONE'
    if '*' in parts:
        return 'ALL'
    return ', '.join(parts)


def cmd_parts(argv: list) -> None:
    if len(argv) < 2:
        sys.exit('Usage: python manage_users.py parts <username> <part> [<part> ...]'
                 ' -- or parts-all / parts-none for everything / nothing.')
    username, parts = argv[0], argv[1:]
    if not user_store.set_parts(username, parts):
        sys.exit(f'No such user: {username}')
    print(f"OK — '{username.lower()}' can now see and download: {', '.join(parts)}")
    print('     Takes effect on their next Refresh — no need to re-issue a login.')


def cmd_set_admin(argv: list, admin: bool) -> None:
    if len(argv) != 1:
        verb = 'admin' if admin else 'no-admin'
        sys.exit(f'Usage: python manage_users.py {verb} <username>')
    username = argv[0]
    if not user_store.set_admin(username, admin):
        sys.exit(f'No such user: {username}')
    if admin:
        print(f"OK - '{username.lower()}' is now an HQ admin.")
        print('     They get Settings -> Manage distributors in the app after')
        print('     their next log in (Settings -> Log out, then log back in).')
    else:
        print(f"OK - '{username.lower()}' is no longer an admin.")
        print('     Takes effect on their very next request.')


def cmd_parts_preset(argv: list, parts: list, label: str) -> None:
    if len(argv) != 1:
        sys.exit(f'Usage: python manage_users.py parts-{label} <username>')
    username = argv[0]
    if not user_store.set_parts(username, parts):
        sys.exit(f'No such user: {username}')
    what = 'every part' if parts else 'no parts at all'
    print(f"OK — '{username.lower()}' can now see and download {what}.")
    print('     Takes effect on their next Refresh — no need to re-issue a login.')


def cmd_set_active(argv: list, active: bool) -> None:
    if len(argv) != 1:
        verb = 'enable' if active else 'disable'
        sys.exit(f'Usage: python manage_users.py {verb} <username>')
    if user_store.set_active(argv[0], active):
        state = 'enabled' if active else 'disabled'
        print(f"OK — '{argv[0].lower()}' is now {state}.")
    else:
        sys.exit(f"No such user: {argv[0]}")


def cmd_list() -> None:
    users = user_store.load_users()
    if not users:
        print('(no accounts yet)')
        return
    width = max(len(u) for u in users)
    dwidth = max(len(u.get('distributor', '')) for u in users.values())
    for name in sorted(users):
        u = users[name]
        state = 'active ' if u.get('active') else 'DISABLED'
        dist = u.get('distributor', '')
        role = ' [ADMIN]' if user_store.is_admin(u) else ''
        print(f'{name:<{width}}  {state}  {dist:<{dwidth}}  '
              f'firmware: {_describe_parts(u)}{role}')


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == 'add':
        cmd_add(argv)
    elif cmd == 'disable':
        cmd_set_active(argv, active=False)
    elif cmd == 'enable':
        cmd_set_active(argv, active=True)
    elif cmd == 'parts':
        cmd_parts(argv)
    elif cmd == 'parts-all':
        cmd_parts_preset(argv, list(user_store.ALL_PARTS), 'all')
    elif cmd == 'parts-none':
        cmd_parts_preset(argv, [], 'none')
    elif cmd == 'admin':
        cmd_set_admin(argv, admin=True)
    elif cmd == 'no-admin':
        cmd_set_admin(argv, admin=False)
    elif cmd == 'list':
        cmd_list()
    else:
        sys.exit(f'Unknown command: {cmd}\n{__doc__}')


if __name__ == '__main__':
    main()
