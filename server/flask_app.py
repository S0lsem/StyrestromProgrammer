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
"""
import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, abort

app = Flask(__name__)

# ---------- Configuration ----------
# Set these as environment variables on PythonAnywhere.
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


@app.route('/parts/<part>/files', methods=['GET'])
def list_files(part: str):
    """Return list of files in a part folder with their base64 content."""
    _check_api_key()
    try:
        folder = f'{FIRMWARE_PATH}/{part}'
        items = _github_get(folder)
        files = []
        for item in items:
            if item['type'] != 'file':
                continue
            info = _github_get(f'{folder}/{item["name"]}')
            files.append({
                'name': item['name'],
                'content': info['content'].replace('\n', ''),
            })
        return jsonify(files)
    except HTTPError as exc:
        if exc.code == 404:
            return jsonify({'error': f'Part not found: {part}'}), 404
        return jsonify({'error': f'GitHub API error: {exc.code}'}), 502
    except URLError as exc:
        return jsonify({'error': f'Network error: {exc.reason}'}), 502


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})
