"""
Firmware proxy server for MRS PLC Programmer.

Deploy this on PythonAnywhere (free tier). It forwards firmware download
requests to the private GitHub repo so the GitHub token never leaves
the server.

Setup on PythonAnywhere:
  1. Create a free account at pythonanywhere.com
  2. Go to "Web" tab → "Add a new web app" → Manual configuration → Python 3.10
  3. Go to "Files" tab → open /home/yourusername/mysite/flask_app.py
  4. Paste this file's contents and save
  5. Go to "Web" tab → open the WSGI configuration file
  6. Replace its contents with:
       import sys
       sys.path.insert(0, '/home/yourusername/mysite')
       from flask_app import app as application
  7. Go to "Web" tab → click "Reload"
  8. Set environment variables (see below)

Environment variables (set in your WSGI file or in a .env file):
  GITHUB_TOKEN        = your fine-grained PAT (read-only, Contents permission)
  PROXY_API_KEY       = a random secret string (the app uses this to authenticate)

Expected repo layout (private GitHub repo, owner/name set below):
  mrs-firmware/
    <part_name>/
      *.s19                 ← the linked image produced by MRS Applics Studio.
                              Any filename ending in .s19 is fine, and the file
                              may sit at the part-folder root or under src/.
                              First .s19 found (root first, then src/) wins.
"""
import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, abort, Response

app = Flask(__name__)

# ---------- Configuration ----------
GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
PROXY_API_KEY = os.environ.get('PROXY_API_KEY', '')
GITHUB_OWNER  = 'S0lsem'
GITHUB_REPO   = 'Code-for-Highbeam-X'
FIRMWARE_PATH = 'mrs-firmware'

_API = 'https://api.github.com'


# ---------- Auth check ----------
def _check_api_key():
    key = request.headers.get('X-Api-Key', '')
    if not PROXY_API_KEY:
        return  # no key configured — skip check (dev mode)
    if key != PROXY_API_KEY:
        abort(403, 'Invalid API key')


# ---------- GitHub helper ----------
def _github_get(path: str):
    url = f'{_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}'
    req = Request(url, headers={
        'Authorization': f'Bearer {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ---------- Endpoints ----------
@app.route('/parts', methods=['GET'])
def list_parts():
    """Return list of part folder names."""
    _check_api_key()
    try:
        items = _github_get(FIRMWARE_PATH)
        parts = sorted(
            item['name']
            for item in items
            if item['type'] == 'dir' and not item['name'].startswith('.')
        )
        return jsonify(parts)
    except HTTPError as exc:
        return jsonify({'error': f'GitHub API error: {exc.code}'}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502


def _find_s19(part: str) -> tuple[str, str] | None:
    """Locate the first .s19 file in the part folder or its src/ subfolder.

    Returns (folder_path, filename) or None if no .s19 is present.
    The part-folder root takes precedence over src/ so a renamed firmware.s19
    at the root overrides any leftover build artifacts in src/.
    """
    for folder in (f'{FIRMWARE_PATH}/{part}', f'{FIRMWARE_PATH}/{part}/src'):
        try:
            items = _github_get(folder)
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        for item in items:
            if item.get('type') == 'file' and item['name'].lower().endswith('.s19'):
                return folder, item['name']
    return None


@app.route('/parts/<part>/firmware', methods=['GET'])
def get_firmware(part: str):
    """Return the raw S-record text for <part>.

    Walks the part folder (and its src/ subfolder) and serves the first
    .s19 found. Any filename works, so the file can be uploaded straight
    out of MRS's bin/ folder without renaming.
    """
    _check_api_key()
    try:
        located = _find_s19(part)
        if located is None:
            return jsonify({'error': f"No .s19 file found for '{part}'."}), 404
        folder, name = located
        info = _github_get(f'{folder}/{name}')
        content_b64 = info['content'].replace('\n', '')
        s19_text = base64.b64decode(content_b64).decode('ascii', errors='strict')
        return Response(s19_text, mimetype='text/plain; charset=us-ascii')
    except HTTPError as exc:
        return jsonify({'error': f'GitHub API error: {exc.code}'}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})
