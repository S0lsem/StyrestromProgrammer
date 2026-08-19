# Distributor login — how it works and how to roll it out

The proxy now refuses to serve firmware unless the request carries a valid
**login token**. Each distributor gets their own username + password. Log in
once in the app and it stays logged in for ~30 days; disabling an account on
the server cuts that distributor off on their very next request.

- **Server enforcement:** [server/flask_app.py](server/flask_app.py) +
  [server/user_store.py](server/user_store.py)
- **Accounts:** [server/manage_users.py](server/manage_users.py) (never edit
  `users.json` by hand)
- **App side:** login dialog + token handling in
  [programmer_app.py](programmer_app.py) and [mrs_protocol/auth.py](mrs_protocol/auth.py)

---

## Rollout — do these in order (safe, no distributor gets locked out mid-way)

The trick: deploy with enforcement **off** so old apps keep working, get
everyone updated to the login-enabled app, then flip enforcement **on**.

### 1. Upload the three server files to PythonAnywhere
On the **Files** tab, in your `mysite/` folder (next to the existing
`flask_app.py`), upload/replace:
- `flask_app.py`
- `user_store.py`   ← new
- `manage_users.py` ← new

### 2. Make a token-signing secret
Open a **Bash console** on PythonAnywhere and run:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```
Copy the long random string it prints.

### 3. Set the environment variables (enforcement OFF for now)
On the **Web** tab, open your **WSGI configuration file** and make sure these
lines are set **above** the `from flask_app import app as application` line
(keep your existing GITHUB_TOKEN / PROXY_API_KEY lines):
```python
import os
os.environ['TOKEN_SECRET']   = 'PASTE-THE-SECRET-FROM-STEP-2'
os.environ['LOGIN_ENFORCED'] = '0'   # off during rollout
```
Save, then go to the **Web** tab and click **Reload**.

At this point: old apps still work (via the API key), and login also works.

### 4. Create the distributor accounts
In a Bash console:
```bash
cd ~/mysite
python3 manage_users.py add acme "Acme Norway AS"
# it prompts for a password (twice); nothing is echoed
python3 manage_users.py list          # see all accounts
```
Repeat `add` for each distributor. To cut someone off later:
```bash
python3 manage_users.py disable acme  # instant — even before their token expires
python3 manage_users.py enable  acme  # restore
```

### 5. Ship the login-enabled app
Cut a release so every distributor's app self-updates to the version with the
login screen:
```powershell
.\release.ps1 -Version 1.0.8 -Notes "Login required + friendly CAN FD scan"
```
Give people a day or two (and confirm) so everyone has updated and logged in.

### 6. Turn enforcement ON
Back in the WSGI file, change:
```python
os.environ['LOGIN_ENFORCED'] = '1'
```
Save and **Reload**. Now the proxy serves firmware **only** to a valid login.
Anyone on an old (pre-login) app, or without an account, gets nothing.

---

## Good to know

- **No client config change.** The app talks to the same `PROXY_URL`; it just
  calls `/login` now. Keep `PROXY_URL` on **https** — passwords travel to
  `/login`, so http would expose them.
- **Passwords** are stored only as salted PBKDF2-SHA256 hashes in
  `users.json`. If someone forgets theirs, run `add <user> "<Distributor>"`
  again to set a new one.
- **The signing secret is sensitive.** Changing `TOKEN_SECRET` logs everyone
  out (all existing tokens become invalid) — they just log in again.
- **Identity in logs/events** now comes from the account: the username is the
  operator and the account's distributor name is filled in automatically. The
  old free-text "Operator identity" box is replaced by **Settings → Log out**.
- **`users.json` lives on the server only** and is never committed.

---

## Managing everything from inside the app (the easy way)

HQ accounts get **Settings → Manage distributors…** in the programmer app.
That one window does everything the console commands below do — create
accounts, reset passwords, enable/disable, and tick which firmware each
distributor may see. Distributors never see the menu item.

### One-time setup

You only need a PythonAnywhere console **once**, to make your own account an
admin:

```bash
cd ~/mysite
python3 manage_users.py admin <your-username>
```

Then in the app: **Settings → Log out**, log back in, and the menu item
appears. (The admin flag arrives with the login, so it shows up after the next
login, not immediately.)

### Using the window

| What you want | What to do |
|---|---|
| Add a distributor | **New distributor…** — username, company, password. Starts with **no** firmware. |
| Give them firmware | Select them, tick parts on the right, **Save firmware access** |
| Give them everything | Tick **All firmware (including parts added later)**, then Save |
| Forgotten password | Select them, **Reset password…** — their firmware list is untouched |
| Stop them temporarily | Select them, **Disable** — they are cut off immediately |
| Remove them for good | Select them, **Delete…** |
| Another HQ person | Select them, **Make HQ admin** |

Changes reach a distributor the next time they click **Refresh list** — they
do not need to log out, log in, or update the app.

**You cannot disable, demote, or delete your own account** — those buttons are
greyed out on your own row. That is deliberate: it is the one mistake that
would lock HQ out of the admin window entirely, and undoing it would need a
PythonAnywhere console again.

**A part ticked here is an exact name.** If you previously set someone up from
the console with a wildcard like `1494X*`, the window shows an orange note
saying so, because saving replaces the wildcard with exactly the ticked names.
Use the console if you want to keep the wildcard.

---

## Choosing which firmware each distributor sees (from the console)

Not every part is relevant to every distributor, so each account carries a
**firmware list**. The app's Part dropdown shows only what that account is
entitled to, and a download of anything else is refused by the server. The
filtering happens on the server, not in the app — a part a distributor isn't
entitled to never travels over the wire at all, not even as a name.

### The three commands

Run these in a Bash console on PythonAnywhere, in `~/mysite`:

```bash
cd ~/mysite

# Only these parts (replace with the real folder names):
python3 manage_users.py parts acme "1493X-V4" "14930 Taxi"

# Everything:
python3 manage_users.py parts-all acme

# Nothing (keeps the account alive but hands out no firmware):
python3 manage_users.py parts-none acme
```

Check the result at any time with:

```bash
python3 manage_users.py list
```

```
acme    active   Acme Norway AS  firmware: 1493X-V4, 14930 Taxi
bravo   active   Bravo Ltd       firmware: ALL
carla   DISABLED Carla Oy        firmware: NONE
```

### What to type for a part name

Use the **firmware folder name from the GitHub repo** — the same text the
distributor sees in the app's Part dropdown. Upper/lower case doesn't matter.

`*` works as a wildcard, which saves re-editing the list every time a part
gets a new revision folder:

```bash
python3 manage_users.py parts acme "1494X*"      # every 1494X revision
```

**Always put quotes around a name containing `*`** — without them the shell
expands it into a list of filenames before the script ever sees it. Quotes are
also required for any name containing a space.

### Good to know

- **Changes are instant.** The distributor doesn't need to log out or update
  the app — the next time they click **Refresh list**, they see the new set.
  Removing a part also blocks a download they had queued up, immediately.
- **A new account starts with access to everything.** Narrow it right after
  creating it. `manage_users.py add` prints a reminder.
- **Resetting a password keeps the firmware list.** Running `add` again on an
  existing user changes their password and distributor name only.
- **Existing accounts are unaffected by the upgrade.** An account with no
  firmware list set is treated as "everything", exactly as before, so nobody
  loses access the moment you upload the new server files.
- **A distributor with no firmware** sees an empty dropdown and the message
  *"No firmware assigned to your account"* — not an error, so they'll know to
  call you rather than chase a connection problem.
- **This is not a substitute for `disable`.** `parts-none` stops firmware but
  the account can still log in; `disable <username>` cuts them off entirely.

---

## Admin rights, in one place

| | |
|---|---|
| Grant | `python3 manage_users.py admin <username>` |
| Revoke | `python3 manage_users.py no-admin <username>` |
| See who has it | `python3 manage_users.py list` — admins are tagged `[ADMIN]` |

An admin account can manage every other account and all firmware access, so
keep it to Styrestrøm staff. Revoking bites on that person's very next action
in the window — they do not have to log out first.

The app only *shows* the menu item based on the flag it received at login; the
server re-checks it on every single admin call. A tampered client gets an
empty window and a refusal, not access.
