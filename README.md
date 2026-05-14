# tun_tray

Windows tray utility for the `tun_rotate_microservice` v2.3 (Linux side).

## Features

- **Left-click** the tray icon → rotates the tunnel for **this machine** (`GET /rotate`).
- **Right-click** → menu:
  - Rotate this machine
  - Rotate all machines (`POST /rotate_all`)
  - Show status (notification with summary from `/status`)
  - Refresh now
  - Remount SMB share (`net use` delete + reconnect with stored creds)
  - Check SMB (is the drive accessible?)
  - Open settings (opens `config.json` in the default editor)
  - Reload settings
  - Open log
  - Exit
- Tray tooltip shows current tunnel + external IP (auto-refreshes every 15 s by default).
- Tray icon color: 🟢 OK · 🟡 warning · 🔴 no connection · 🔵 busy.
- Optional SMB auto-remount if the drive becomes inaccessible.

## Install (end user)

1. Download `tun_tray.exe` from the latest [Release](../../releases) (built by GitHub Actions).
2. Run it once. It will create a config file at:

   ```
   %APPDATA%\tun_tray\config.json
   ```

3. Open the file (right-click tray → **Open settings**), fill in your settings, then **Reload settings**.

### Autostart

Press `Win + R`, type `shell:startup`, and place a shortcut to `tun_tray.exe` there.

## Configuration

Default `config.json`:

```json
{
  "server_url": "http://192.168.137.1:5000",
  "request_timeout": 30,
  "status_poll_interval": 15,
  "smb": {
    "drive_letter": "Z:",
    "unc_path": "\\\\192.168.137.1\\share",
    "username": "",
    "password": "",
    "auto_remount_on_failure": false
  },
  "notifications": true
}
```

| Key | Description |
|---|---|
| `server_url` | URL of the `tun_rotate_microservice` on your Linux box. |
| `request_timeout` | Seconds to wait for `/rotate` / `/rotate_all` (rotation can take a while). |
| `status_poll_interval` | How often (seconds) to poll `/status` and update the tooltip. |
| `smb.drive_letter` | E.g. `Z:`. |
| `smb.unc_path` | E.g. `\\\\192.168.137.1\\share`. Note: **double** backslashes in JSON. |
| `smb.username` / `smb.password` | Credentials for `net use`. Leave empty to use Windows-saved creds. |
| `smb.auto_remount_on_failure` | If `true`, the tray re-runs `net use` whenever the drive becomes unreachable (checked every poll). |
| `notifications` | Show Windows toast notifications on rotate / errors. |

### ⚠️ About storing the SMB password

The password lives in plain text in `%APPDATA%\tun_tray\config.json`. That file is in your user profile, so other (non-admin) accounts on the machine can't read it, but anyone with admin access on the same box can. If that's a concern, leave `password` empty and save the credentials in **Windows Credential Manager** instead — `net use` will pick them up automatically.

## Build locally (optional)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller==6.10.0

pyinstaller --noconfirm --onefile --windowed `
  --name tun_tray `
  --version-file version_info.txt `
  --hidden-import PIL._tkinter_finder `
  --collect-all pystray `
  --collect-all PIL `
  tun_tray.py
```

The resulting executable will be at `dist\tun_tray.exe`.

## Build on GitHub (recommended)

This repo ships with a GitHub Actions workflow at `.github/workflows/build.yml`.

- **Every push** to `main` / `master` builds the .exe and uploads it as an *artifact* (downloadable from the workflow run page).
- **Tag a release** (`git tag v1.0.0 && git push --tags`) to additionally attach the .exe to a GitHub Release automatically.
- **Manual run**: in GitHub → Actions → "Build Windows EXE" → "Run workflow".

## How it talks to the server

| Action | Endpoint | Method | Body |
|---|---|---|---|
| Left-click / Rotate this machine | `/rotate` | GET | — |
| Rotate all machines | `/rotate_all` | POST | `{}` |
| Show status / poll | `/status` | GET | — |

All requests send `Accept: application/json` so the microservice returns JSON (not the pretty-printed plain-text format).

## Logs

```
%APPDATA%\tun_tray\tun_tray.log
```

Right-click tray → **Open log**.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 🔴 red icon, tooltip "No connection" | Server URL wrong, server down, or LAN routing broken. Open log. |
| 🟡 yellow icon, "Warning: rule order" | `/rotate` succeeded but `ip rule` priority is wrong on the server. The microservice tries to auto-fix; check server logs. |
| `Rotate failed: HTTP 403` | Your machine isn't on the configured `LOCAL_NETWORK_PREFIX` (192.168.137.x by default). |
| SMB remount fails with "System error 1219" | A connection to the same share already exists under a different user. The script does `net use /delete` first, but if multiple shares from the same server are open, disconnect them and try again. |
