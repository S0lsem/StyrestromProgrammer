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
