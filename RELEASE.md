# Cutting a release

Releases are fully automated by [`release.ps1`](release.ps1). One command bumps
the version, builds the `.exe`, pushes, publishes a GitHub release, and uploads
the `.exe` as the release asset. Once published, every app on an older version
shows the in-app **Update & Restart** button and updates itself.

## One-time setup (do this once per machine)

1. Create a **fine-grained** GitHub token: <https://github.com/settings/tokens?type=beta>
   - **Resource owner:** `S0lsem`
   - **Repository access:** Only select repositories -> `StyrestromProgrammer`
   - **Repository permissions:** **Contents = Read and write**
   - Generate, then copy the token (starts with `github_pat_...`).
2. Save the token into a file named **`.github_token`** in the repo root
   (same folder as `release.ps1`). It is gitignored and never committed.

## Cutting a release

```powershell
.\release.ps1 -Version 1.0.8
# optional release notes:
.\release.ps1 -Version 1.0.8 -Notes "What changed in this version"
```

The script will:
1. Refuse to continue if that version was already released.
2. Bump `mrs_protocol/version.py` and commit it.
3. Build `dist\Styrestrom_Programmer.exe` (PyInstaller, a few minutes).
4. Push to `main`.
5. Create the GitHub release and upload the `.exe`.
6. Verify the release + asset are live and print `SUCCESS`.

## Requirements on the build machine

- Python with the build deps (`pip install pyinstaller PyQt6 python-can[pcan] cryptography`).
- MRS Applics Studio installed (the spec bundles its console flasher), or
  `MRS_CONSOLE_FLASHER_DIR` set to the folder holding it.
- `mrs_protocol/config.py` present (gitignored proxy config).

## Verifying a release worked

Run an older build: it should show the update banner within a second of launch,
and clicking **Update & Restart** should relaunch it on the new version number
(check the title bar). See `mrs_protocol/self_update.py` for the swap mechanics.
