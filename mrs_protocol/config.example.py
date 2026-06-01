# -------------------------------------------------------------------
# config.example.py — copy this file to config.py and fill it in.
# config.py is listed in .gitignore and will never be committed.
# -------------------------------------------------------------------

# Proxy server URL (your PythonAnywhere app — serves firmware downloads).
# Example: 'https://styrestrom.pythonanywhere.com'
PROXY_URL = 'https://yourusername.pythonanywhere.com'

# Proxy API key — must match the PROXY_API_KEY set on the server.
# Leave empty if you haven't set one on the server.
PROXY_API_KEY = ''

# Flash-event receiver (Google Apps Script Web App URL).
# Get it by deploying server/apps_script_events.gs as a Web App.
# Example: 'https://script.google.com/macros/s/AKfycb…/exec'
EVENTS_URL = ''

# Shared secret embedded in every event POST — must match the
# SHARED_SECRET constant inside the Apps Script.
EVENTS_SECRET = ''
