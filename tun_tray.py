# -*- coding: utf-8 -*-
"""
tun_tray.py — tray utility for tun_rotate_microservice v2.3

Features:
  - Left-click on tray icon: call /rotate (rotate this machine's tunnel)
  - Right-click: menu with extra actions
      * Rotate this machine
      * Rotate all machines
      * Show status
      * Remount SMB share
      * Open settings (open config file in notepad)
      * Reload settings
      * Exit
  - Tray tooltip shows external IP + current tunnel, updated periodically
  - Tray icon color reflects state:
      * green  — OK, tunnel working
      * yellow — warning (e.g. rule order wrong, or no data yet)
      * red    — error (no connection to microservice)
  - SMB remount: net use disconnect + reconnect with stored credentials

Config file: %APPDATA%\\tun_tray\\config.json
Created on first run with default values, user can edit.
"""

import os
import sys
import json
import time
import threading
import subprocess
import logging
from pathlib import Path

import requests
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as Item, Menu


# ─────────────────────────────────────────────
# Paths and constants
# ─────────────────────────────────────────────

APP_NAME = "tun_tray"
APP_VERSION = "1.2"

if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
else:
    CONFIG_DIR = Path.home() / ".config" / APP_NAME

CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "tun_tray.log"

DEFAULT_CONFIG = {
    "server_url": "http://192.168.137.1:5000",
    "request_timeout": 30,
    "status_poll_interval": 120,
    "smb": {
        "drive_letter": "Z:",
        "unc_path": r"\\192.168.137.1\share",
        "username": "",
        "password": "",
        "auto_remount_on_failure": False
    },
    "notifications": True
}


# ─────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────

def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    ensure_config_dir()
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # merge with defaults to backfill new keys
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        # nested merge for smb
        smb = dict(DEFAULT_CONFIG["smb"])
        smb.update(cfg.get("smb", {}))
        merged["smb"] = smb
        return merged
    except (OSError, json.JSONDecodeError) as e:
        logging.error(f"Failed to load config: {e}, using defaults")
        return dict(DEFAULT_CONFIG)


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def setup_logging():
    ensure_config_dir()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ─────────────────────────────────────────────
# Tray icon generation (colored circle, no external assets)
# ─────────────────────────────────────────────

def make_icon(color):
    """Create a 64x64 RGBA icon: filled circle in given color."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # outer ring (darker)
    draw.ellipse((2, 2, size - 2, size - 2), fill=color, outline=(30, 30, 30, 255), width=2)
    # small highlight
    draw.ellipse((14, 12, 28, 26), fill=(255, 255, 255, 110))
    return img


ICON_OK = make_icon((50, 180, 70, 255))      # green
ICON_WARN = make_icon((230, 180, 30, 255))   # yellow
ICON_ERR = make_icon((210, 60, 60, 255))     # red
ICON_BUSY = make_icon((90, 130, 220, 255))   # blue (during request)


# ─────────────────────────────────────────────
# API client
# ─────────────────────────────────────────────

class ApiClient:
    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def base(self):
        return self.cfg["server_url"].rstrip("/")

    @property
    def timeout(self):
        return self.cfg.get("request_timeout", 30)

    def _get(self, path):
        url = f"{self.base}{path}"
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=self.timeout)
        return r

    def _post(self, path, body=None):
        url = f"{self.base}{path}"
        r = requests.post(
            url,
            json=body if body is not None else {},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        return r

    def rotate(self):
        return self._get("/rotate")

    def rotate_to(self, tunnel):
        return self._get(f"/rotate?tunnel={tunnel}")

    def rotate_all(self, tunnel=None):
        body = {"tunnel": tunnel} if tunnel else {}
        return self._post("/rotate_all", body)

    def status(self):
        return self._get("/status")


# ─────────────────────────────────────────────
# SMB helpers
# ─────────────────────────────────────────────

def _run(cmd):
    """Run command, return (rc, stdout+stderr)."""
    try:
        # hide console window when packaged as windowed exe
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return result.returncode, out.strip()
    except Exception as e:
        return -1, str(e)


def smb_remount(smb_cfg):
    """
    Disconnect drive if mounted, then reconnect with stored credentials.
    Returns (ok: bool, message: str)
    """
    drive = smb_cfg["drive_letter"].strip()
    unc = smb_cfg["unc_path"].strip()
    user = smb_cfg.get("username", "").strip()
    pwd = smb_cfg.get("password", "")

    if not drive or not unc:
        return False, "SMB: drive_letter or unc_path is empty in config"

    # 1. force disconnect (ignore errors)
    _run(["net", "use", drive, "/delete", "/y"])

    # 2. reconnect
    cmd = ["net", "use", drive, unc]
    if user:
        cmd.append(f"/user:{user}")
        if pwd:
            cmd.append(pwd)
    cmd += ["/persistent:no"]

    rc, out = _run(cmd)
    if rc == 0:
        return True, f"SMB: mounted {drive} -> {unc}"
    return False, f"SMB: failed to mount {drive} -> {unc}\n{out}"


def smb_is_alive(smb_cfg):
    """Quick check: is the drive accessible?"""
    drive = smb_cfg["drive_letter"].strip()
    if not drive:
        return False
    p = drive if drive.endswith("\\") else drive + "\\"
    try:
        return os.path.isdir(p)
    except Exception:
        return False


# ─────────────────────────────────────────────
# Tray application
# ─────────────────────────────────────────────

class TrayApp:
    def __init__(self):
        self.cfg = load_config()
        self.api = ApiClient(self.cfg)

        self._lock = threading.Lock()
        self._busy = False
        self._stop = threading.Event()

        # last known state for tooltip
        self.state = {
            "external_ip": None,
            "current_tunnel": None,
            "status_text": "Starting...",
            "ok": None,  # True/False/None
            "available_tunnels": [],
        }

        self.icon = pystray.Icon(
            APP_NAME,
            ICON_WARN,
            self._make_tooltip(),
            menu=self._build_menu(),
        )
        # left-click = default action; pystray triggers default Item on activate
        self.icon.default_action = self._on_left_click

    # ── helpers ─────────────────────────────

    def _make_tooltip(self):
        s = self.state
        tunnel = s.get("current_tunnel") or "?"
        ip = s.get("external_ip") or "?"
        status = s.get("status_text") or ""
        # tray tooltip max 127 chars on Windows
        text = f"{APP_NAME} {APP_VERSION}\nTunnel: {tunnel}\nIP: {ip}\n{status}"
        return text[:127]

    def _refresh_tooltip(self):
        try:
            self.icon.title = self._make_tooltip()
        except Exception:
            pass

    def _set_icon(self, img):
        try:
            self.icon.icon = img
        except Exception:
            pass

    def _notify(self, title, msg):
        if not self.cfg.get("notifications", True):
            return
        try:
            self.icon.notify(msg, title)
        except Exception as e:
            logging.warning(f"notify failed: {e}")

    # ── menu ────────────────────────────────

    def _build_menu(self):
        return Menu(
            Item("Rotate this machine", self._on_rotate, default=True),
            Item("Rotate all machines", self._on_rotate_all),
            Item(
                "Switch to",
                Menu(self._switch_to_items),  # callable -> recomputed on open
            ),
            Menu.SEPARATOR,
            Item("Show status", self._on_show_status),
            Item("Refresh now", self._on_refresh_now),
            Menu.SEPARATOR,
            Item("Remount SMB share", self._on_smb_remount),
            Item("Check SMB", self._on_smb_check),
            Menu.SEPARATOR,
            Item("Open settings", self._on_open_settings),
            Item("Reload settings", self._on_reload_settings),
            Item("Open log", self._on_open_log),
            Menu.SEPARATOR,
            Item(f"{APP_NAME} v{APP_VERSION}", None, enabled=False),
            Item("Exit", self._on_exit),
        )

    def _switch_to_items(self):
        """Dynamic submenu generator: rebuilt each time the menu is opened.
        Returns an iterable of pystray.MenuItem."""
        tunnels = list(self.state.get("available_tunnels") or [])
        current = self.state.get("current_tunnel")
        if not tunnels:
            return [Item("(no tunnels — click 'Refresh now')", None, enabled=False)]
        items = []
        for t in tunnels:
            label = f"\u2713 {t}" if t == current else f"   {t}"
            items.append(Item(label, self._on_switch_to(t)))
        return items

    # ── busy guard ──────────────────────────

    def _try_busy(self):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._set_icon(ICON_BUSY)
            return True

    def _release_busy(self):
        with self._lock:
            self._busy = False

    # ── actions ─────────────────────────────

    def _on_left_click(self, icon, item):
        # Same as "Rotate this machine"
        self._on_rotate(icon, item)

    def _on_rotate(self, icon, item):
        if not self._try_busy():
            return
        threading.Thread(target=self._do_rotate, daemon=True).start()

    def _do_rotate(self):
        try:
            logging.info("rotate: requesting /rotate")
            r = self.api.rotate()
            data = self._safe_json(r)
            if r.status_code == 200 and data.get("status") in ("ok", "warning"):
                tun = data.get("new_tunnel")
                ip = data.get("external_ip")
                self.state["current_tunnel"] = tun
                self.state["external_ip"] = ip
                if data.get("status") == "ok":
                    self.state["ok"] = True
                    self.state["status_text"] = "OK"
                    self._set_icon(ICON_OK)
                    self._notify("Tunnel rotated", f"{tun}\nIP: {ip}")
                else:
                    self.state["ok"] = False
                    self.state["status_text"] = "Warning: rule order"
                    self._set_icon(ICON_WARN)
                    self._notify("Tunnel rotated (warning)", data.get("message", ""))
            else:
                self.state["ok"] = False
                self.state["status_text"] = f"Error {r.status_code}"
                self._set_icon(ICON_ERR)
                msg = data.get("message") if isinstance(data, dict) else r.text[:200]
                self._notify("Rotate failed", str(msg))
            self._refresh_tooltip()
        except requests.RequestException as e:
            logging.error(f"rotate request failed: {e}")
            self.state["ok"] = False
            self.state["status_text"] = "No connection"
            self._set_icon(ICON_ERR)
            self._notify("Rotate failed", f"No connection to server\n{e}")
            self._refresh_tooltip()
        finally:
            self._release_busy()

    def _on_rotate_all(self, icon, item):
        if not self._try_busy():
            return
        threading.Thread(target=self._do_rotate_all, daemon=True).start()

    def _on_switch_to(self, tunnel):
        """Factory for menu callbacks. Returns a callback bound to a specific
        tunnel name. pystray calls it with (icon, item)."""
        def _cb(icon, item):
            if not self._try_busy():
                return
            threading.Thread(target=self._do_switch_to, args=(tunnel,), daemon=True).start()
        return _cb

    def _do_switch_to(self, tunnel):
        try:
            logging.info(f"switch_to: requesting /rotate?tunnel={tunnel}")
            r = self.api.rotate_to(tunnel)
            data = self._safe_json(r)
            if r.status_code == 200 and data.get("status") in ("ok", "warning"):
                tun = data.get("new_tunnel") or tunnel
                ip = data.get("external_ip")
                self.state["current_tunnel"] = tun
                self.state["external_ip"] = ip
                if data.get("status") == "ok":
                    self.state["ok"] = True
                    self.state["status_text"] = "OK"
                    self._set_icon(ICON_OK)
                    self._notify(f"Switched to {tun}", f"IP: {ip}")
                else:
                    self.state["ok"] = False
                    self.state["status_text"] = "Warning: rule order"
                    self._set_icon(ICON_WARN)
                    self._notify(f"Switched to {tun} (warning)", data.get("message", ""))
            else:
                self.state["ok"] = False
                self.state["status_text"] = f"Error {r.status_code}"
                self._set_icon(ICON_ERR)
                msg = data.get("message") if isinstance(data, dict) else r.text[:200]
                self._notify(f"Switch to {tunnel} failed", str(msg))
            self._refresh_tooltip()
        except requests.RequestException as e:
            logging.error(f"switch_to request failed: {e}")
            self.state["ok"] = False
            self.state["status_text"] = "No connection"
            self._set_icon(ICON_ERR)
            self._notify(f"Switch to {tunnel} failed", f"No connection to server\n{e}")
            self._refresh_tooltip()
        finally:
            self._release_busy()

    def _do_rotate_all(self):
        try:
            logging.info("rotate_all: requesting /rotate_all")
            r = self.api.rotate_all()
            data = self._safe_json(r)
            if r.status_code == 200 and data.get("status") in ("ok", "warning"):
                tun = data.get("new_tunnel")
                ip = data.get("external_ip")
                self.state["current_tunnel"] = tun
                self.state["external_ip"] = ip
                ok = data.get("status") == "ok"
                self.state["ok"] = ok
                self.state["status_text"] = "OK (all)" if ok else "Warning (all)"
                self._set_icon(ICON_OK if ok else ICON_WARN)
                machines = data.get("machines") or []
                self._notify(
                    "All machines rotated",
                    f"{tun}\nIP: {ip}\nMachines: {', '.join(machines)}",
                )
            else:
                self.state["ok"] = False
                self.state["status_text"] = f"Error {r.status_code}"
                self._set_icon(ICON_ERR)
                msg = data.get("message") if isinstance(data, dict) else r.text[:200]
                self._notify("Rotate all failed", str(msg))
            self._refresh_tooltip()
        except requests.RequestException as e:
            logging.error(f"rotate_all request failed: {e}")
            self.state["ok"] = False
            self.state["status_text"] = "No connection"
            self._set_icon(ICON_ERR)
            self._notify("Rotate all failed", f"No connection to server\n{e}")
            self._refresh_tooltip()
        finally:
            self._release_busy()

    def _on_show_status(self, icon, item):
        threading.Thread(target=self._do_show_status, daemon=True).start()

    def _do_show_status(self):
        ok, text = self._fetch_status_text()
        title = "Status: OK" if ok else "Status"
        self._notify(title, text)

    def _on_refresh_now(self, icon, item):
        threading.Thread(target=self._poll_status_once, daemon=True).start()

    # ── SMB ─────────────────────────────────

    def _on_smb_remount(self, icon, item):
        threading.Thread(target=self._do_smb_remount, daemon=True).start()

    def _do_smb_remount(self):
        logging.info("smb remount requested")
        ok, msg = smb_remount(self.cfg["smb"])
        logging.info(f"smb remount result: ok={ok} msg={msg}")
        self._notify("SMB Remount" + (" OK" if ok else " failed"), msg)

    def _on_smb_check(self, icon, item):
        alive = smb_is_alive(self.cfg["smb"])
        drive = self.cfg["smb"]["drive_letter"]
        self._notify(
            "SMB Check",
            f"Drive {drive} is {'accessible' if alive else 'NOT accessible'}",
        )

    # ── settings ────────────────────────────

    def _on_open_settings(self, icon, item):
        ensure_config_dir()
        if not CONFIG_FILE.exists():
            load_config()  # creates file
        try:
            if sys.platform == "win32":
                os.startfile(str(CONFIG_FILE))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(CONFIG_FILE)])
        except Exception as e:
            logging.error(f"open settings failed: {e}")
            self._notify("Open settings failed", str(e))

    def _on_reload_settings(self, icon, item):
        self.cfg = load_config()
        self.api = ApiClient(self.cfg)
        self._notify("Settings reloaded", f"Server: {self.cfg['server_url']}")
        self._refresh_tooltip()

    def _on_open_log(self, icon, item):
        try:
            if sys.platform == "win32":
                os.startfile(str(LOG_FILE))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(LOG_FILE)])
        except Exception as e:
            self._notify("Open log failed", str(e))

    def _on_exit(self, icon, item):
        logging.info("exit requested")
        self._stop.set()
        self.icon.stop()

    # ── status polling ──────────────────────

    def _safe_json(self, response):
        try:
            return response.json()
        except Exception:
            return {}

    def _fetch_status_text(self):
        try:
            r = self.api.status()
            data = self._safe_json(r)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            available = data.get("available_tunnels", [])
            machines = data.get("machines", {}) or {}
            gl = data.get("global_last_tunnel") or "(none)"

            lines = [f"Available: {', '.join(available) if available else '(none)'}",
                     f"Last global: {gl}"]
            for num, info in machines.items():
                mark = "OK" if info.get("rule_order_ok") else "!!"
                lines.append(f"#{num} [{mark}] {info.get('current_tunnel')}")
            return True, "\n".join(lines)[:1000]
        except requests.RequestException as e:
            return False, f"No connection: {e}"

    def _poll_status_once(self):
        """One status poll: refresh state from /status, then probe external IP from server."""
        try:
            r = self.api.status()
            if r.status_code != 200:
                self.state["ok"] = False
                self.state["status_text"] = f"HTTP {r.status_code}"
                self._set_icon(ICON_ERR)
                self._refresh_tooltip()
                return

            data = self._safe_json(r)
            machines = data.get("machines", {}) or {}
            # Cache available tunnels for the dynamic "Switch to" submenu.
            self.state["available_tunnels"] = data.get("available_tunnels", []) or []

            # Try to find "our" machine entry. Server identifies the requester by
            # source IP, but /status returns all known machines. We pick by the
            # last octet of our LAN IP if discoverable, otherwise fall back to
            # global_last_tunnel.
            current_tun = None
            rule_ok = True
            our_num = self._guess_our_machine_number()
            if our_num and str(our_num) in machines:
                info = machines[str(our_num)]
                current_tun = info.get("current_tunnel")
                rule_ok = bool(info.get("rule_order_ok"))
            else:
                current_tun = data.get("global_last_tunnel")

            self.state["current_tunnel"] = current_tun

            # external IP — call /rotate? No, that rotates. Instead, ask one quick
            # ipify call via system; but our external IP through tunnel only
            # matches what server sees on the same interface. Server's /rotate
            # already returns external_ip. /status doesn't. Cheapest reliable
            # option: do our own ipify call from the client.
            self.state["external_ip"] = self._fetch_own_external_ip()

            if rule_ok and current_tun:
                self.state["ok"] = True
                self.state["status_text"] = "OK"
                self._set_icon(ICON_OK)
            elif current_tun:
                self.state["ok"] = False
                self.state["status_text"] = "Warning: rule order"
                self._set_icon(ICON_WARN)
            else:
                self.state["ok"] = None
                self.state["status_text"] = "No tunnel"
                self._set_icon(ICON_WARN)

            self._refresh_tooltip()

        except requests.RequestException as e:
            logging.warning(f"status poll failed: {e}")
            self.state["ok"] = False
            self.state["status_text"] = "No connection"
            self._set_icon(ICON_ERR)
            self._refresh_tooltip()

    def _guess_our_machine_number(self):
        """
        Find the last octet of our LAN IP that matches the configured network
        prefix. Server uses 192.168.137.X — we look for our interface in that
        subnet.
        """
        try:
            # extract prefix from server_url host (rough but works)
            from urllib.parse import urlparse
            host = urlparse(self.cfg["server_url"]).hostname or ""
            parts = host.split(".")
            if len(parts) != 4:
                return None
            prefix = ".".join(parts[:3]) + "."

            import socket
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip.startswith(prefix):
                    return int(ip.split(".")[-1])
        except Exception as e:
            logging.debug(f"guess_our_machine_number failed: {e}")
        return None

    def _fetch_own_external_ip(self):
        try:
            r = requests.get("https://api.ipify.org", timeout=5)
            if r.status_code == 200:
                ip = r.text.strip()
                if ip and len(ip) <= 45:
                    return ip
        except requests.RequestException:
            pass
        return None

    # ── background loop ─────────────────────

    def _poll_loop(self):
        # initial delay to let tray icon appear
        time.sleep(1)
        interval = max(5, int(self.cfg.get("status_poll_interval", 120)))
        while not self._stop.is_set():
            if not self._busy:
                self._poll_status_once()
                # optional SMB auto-remount
                smb_cfg = self.cfg.get("smb", {})
                if smb_cfg.get("auto_remount_on_failure"):
                    if not smb_is_alive(smb_cfg):
                        logging.info("SMB auto-remount: drive not accessible, remounting")
                        smb_remount(smb_cfg)
            # use Event.wait so exit is responsive
            if self._stop.wait(timeout=interval):
                break

    # ── run ─────────────────────────────────

    def run(self):
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        self.icon.run()


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    setup_logging()
    logging.info(f"{APP_NAME} v{APP_VERSION} starting")
    logging.info(f"config: {CONFIG_FILE}")
    app = TrayApp()
    app.run()


if __name__ == "__main__":
    main()
