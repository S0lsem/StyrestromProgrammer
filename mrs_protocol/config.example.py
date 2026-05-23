# -------------------------------------------------------------------
# config.example.py — copy this file to config.py and fill it in.
# config.py is listed in .gitignore and will never be committed.
# -------------------------------------------------------------------

# GitHub fine-grained Personal Access Token.
# Create one at: GitHub → Settings → Developer Settings →
#   Fine-grained tokens → Generate new token
# Required permissions on the firmware repo:
#   Contents → Read-only
GITHUB_TOKEN = 'ghp_PASTE_YOUR_TOKEN_HERE'

# GitHub account that owns the firmware repository.
GITHUB_OWNER = 'S0lsem'

# Name of the private repository that contains the part folders.
# e.g. if the URL is github.com/S0lsem/MyFirmwareRepo → 'MyFirmwareRepo'
GITHUB_REPO = 'Code-for-Highbeam-X'

# Branch to download from.
GITHUB_BRANCH = 'main'

# Subfolder in the repo that contains the part folders.
GITHUB_FIRMWARE_PATH = 'mrs-firmware'
