# tun_tray

Windows tray utility for `tun_rotate_microservice` v2.8+ (Linux side).

> Для общей инструкции по всей системе см. главный `README.md` в корне репозитория. Этот файл — только про tray.

## Features

- **Left-click** the tray icon → rotates the tunnel for **this machine** (`GET /rotate`).
- **Right-click** → menu:
  - Rotate this machine
  - Rotate all machines (`POST /rotate_all`)
  - **Switch to →** — submenu with all available tunnels, current marked `✓`. Clicking any tunnel calls `GET /rotate?tunnel=wgXXX` (this machine only).
  - Show status (notification summary from `/status`)
  - Refresh now (force-refresh status immediately)
  - Remount SMB share (`net use` delete + reconnect)
  - Check SMB (is the drive accessible?)
  - Open settings (opens `config.json`)
  - Reload settings
  - Open log
  - Exit
- Tray tooltip shows current tunnel + external IP (auto-refreshes every 120 s by default; configurable).
- Tray icon color: 🟢 OK · 🟡 warning · 🔴 no connection · 🔵 busy.
- Optional SMB auto-remount if the drive becomes inaccessible.

## Install (end user)

1. Download `tun_tray.exe` from the latest [Release](../../releases) or from Actions artifacts (built by GitHub Actions on every push).
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
  "status_poll_interval": 120,
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
| `status_poll_interval` | How often (seconds) to poll `/status` and update the tooltip. Default 120. |
| `smb.drive_letter` | E.g. `Z:`. |
| `smb.unc_path` | E.g. `\\\\192.168.137.1\\share`. Note: **double** backslashes in JSON. |
| `smb.username` / `smb.password` | Credentials for `net use`. Leave empty to use Windows-saved creds. |
| `smb.auto_remount_on_failure` | If `true`, the tray re-runs `net use` whenever the drive becomes unreachable (checked every poll). |
| `notifications` | Show Windows toast notifications on rotate / errors. |

### ⚠️ About storing the SMB password

The password lives in plain text in `%APPDATA%\tun_tray\config.json`. The file is in your user profile, so other (non-admin) accounts on the machine can't read it, but anyone with admin access on the same box can. If that's a concern, leave `password` empty and save the credentials in **Windows Credential Manager** instead — `net use` will pick them up automatically:

```cmd
cmdkey /add:192.168.137.1 /user:youruser /pass:yourpassword
```

## Update procedure

When a new tray version is released:

1. **Close the running tray** (right-click → Exit). Otherwise the .exe is locked.
2. Replace `tun_tray.exe` with the new build.
3. Start it again.

The config file in `%APPDATA%\tun_tray\config.json` is preserved across updates. New config keys are merged with defaults on load — existing values are kept, missing keys get defaults. If you want to reset to fresh defaults: delete `config.json` and it'll be recreated on next start.

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
- **Tag a release** (`git tag v1.2.0 && git push --tags`) to additionally attach the .exe to a GitHub Release automatically.
- **Manual run**: GitHub → Actions → "Build Windows EXE" → "Run workflow".

The workflow opts into Node 24 for JS actions ahead of GitHub's 2026 deprecation, and includes a diagnostic step that lists the workspace contents (handy if you forget to commit a required file like `version_info.txt`).

## How it talks to the server

| Action | Endpoint | Method | Body / Query |
|---|---|---|---|
| Left-click / Rotate this machine | `/rotate` | GET | — |
| Switch to → wgXXX | `/rotate?tunnel=wgXXX` | GET | — |
| Rotate all machines | `/rotate_all` | POST | `{}` |
| Show status / poll | `/status` | GET | — |

All requests send `Accept: application/json` so the microservice returns JSON (not plain text).

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
| `Rotate failed: HTTP 404` (после Switch to →) | Туннеля больше нет в `/etc/wireguard/`. Refresh now → подменю обновится. |
| `Rotate failed: HTTP 503` (после Switch to →) | Туннель есть, но не отвечает (curl через него не работает). |
| SMB remount fails with "System error 1219" | A connection to the same share already exists under a different user. The script does `net use /delete` first, but if multiple shares from the same server are open, disconnect them and try again. |
| Switch to → пустое или "no tunnels — click 'Refresh now'" | Список не загрузился. Жми **Refresh now**. Если не помогает — проблема с подключением к серверу. |

## Version history

- **v1.2** — добавлено подменю "Switch to →" со списком доступных туннелей (`/rotate?tunnel=`).
- **v1.1** — дефолтный `status_poll_interval` поднят с 15 до 120 секунд (снижение нагрузки на сервер).
- **v1.0** — базовый функционал: rotate, rotate_all, status, SMB remount.
