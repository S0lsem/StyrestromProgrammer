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

Notes:
  * Usernames are case-insensitive (stored lower-case).
  * "add" on an existing username resets that user's password + distributor.
  * Disabling takes effect immediately — the user's next request is rejected,
    even if their login token hasn't expired yet.
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
    user_store.upsert_user(username, password, distributor, active=True)
    print(f"OK — '{username.lower()}' ({distributor}) is active and can log in.")


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
    for name in sorted(users):
        u = users[name]
        state = 'active ' if u.get('active') else 'DISABLED'
        print(f'{name:<{width}}  {state}  {u.get("distributor", "")}')


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
    elif cmd == 'list':
        cmd_list()
    else:
        sys.exit(f'Unknown command: {cmd}\n{__doc__}')


if __name__ == '__main__':
    main()
