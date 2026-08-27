#!/usr/bin/env python3
"""Tray icon for Voxtype (Linux Mint / Cinnamon, XApp.StatusIcon).

Watches the Voxtype daemon's state file
($XDG_RUNTIME_DIR/voxtype/state) and shows the state as a microphone icon:

  idle           - grey microphone
  recording      - white microphone on a green circle
  transcribing   - white microphone on a yellow circle
  (file missing) - grey microphone, tooltip points out the inactive daemon

Left-click toggles recording (voxtype record toggle).
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gio, GLib, Gtk, XApp

ICON_DIR = Path(__file__).resolve().parent / "icons"
STATE_FILE = Path(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
) / "voxtype" / "state"

ICONS = {
    "idle": ICON_DIR / "mic-idle.png",
    "recording": ICON_DIR / "mic-recording.png",
    "transcribing": ICON_DIR / "mic-transcribing.png",
}
TOOLTIPS = {
    "idle": "Voxtype ready — hold the hotkey to dictate, click to toggle",
    "recording": "Voxtype: recording …",
    "transcribing": "Voxtype: transcribing …",
}
TOOLTIP_OFF = "Voxtype daemon not running (systemctl --user start voxtype)"

# Switching the backend swaps a symlink under /usr/lib/voxtype -> sudo.
# Autostarted processes lack SUDO_ASKPASS (it is only exported in .bashrc),
# so set it explicitly to let sudo -A use the graphical askpass helper.
ASKPASS = Path.home() / ".local/bin/sudo-askpass"


def sudo_env():
    env = os.environ.copy()
    if "SUDO_ASKPASS" not in env and ASKPASS.exists():
        env["SUDO_ASKPASS"] = str(ASKPASS)
    return env


class VoxtypeTray:
    def __init__(self):
        self.icon = XApp.StatusIcon()
        self.icon.set_name("voxtype")
        self.icon.connect("activate", self.on_activate)
        self.icon.set_secondary_menu(self.build_menu())
        self.current = None

        self.refresh()

        # Watch the directory: the state file is rewritten on every state
        # change and recreated when the daemon restarts.
        self.monitor = Gio.File.new_for_path(str(STATE_FILE.parent)).monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        self.monitor.connect("changed", self.on_fs_event)

        # Safety net in case a file monitor event gets lost
        GLib.timeout_add_seconds(10, self.refresh)

    def build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        for label, cb in (
            ("Toggle recording", lambda *_: self.toggle()),
            ("Settings (voxtype configure)", lambda *_: self.run_bg(
                ["x-terminal-emulator", "-e", "voxtype configure"])),
            ("Restart Voxtype", lambda *_: self.run_bg(
                ["systemctl", "--user", "restart", "voxtype"])),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)

        self._gpu_guard = False
        self.gpu_item = Gtk.CheckMenuItem(label="GPU acceleration (CUDA)")
        self.gpu_item.connect("toggled", self.on_gpu_toggled)
        # Voxtype's prebuilt ONNX CUDA binaries require AVX-512 — without it
        # --enable always fails, so grey the item out right away.
        if not self._cpu_has_avx512():
            self.gpu_item.set_label("GPU acceleration (requires AVX-512 CPU)")
            self.gpu_item.set_sensitive(False)
        menu.append(self.gpu_item)

        quit_item = Gtk.MenuItem(label="Quit tray")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        threading.Thread(target=self._sync_gpu_state, daemon=True).start()
        return menu

    # --- GPU backend switching -------------------------------------------

    @staticmethod
    def _cpu_has_avx512() -> bool:
        try:
            return "avx512" in Path("/proc/cpuinfo").read_text()
        except OSError:
            return False

    def query_gpu_active(self):
        """True/False = active backend is GPU/CPU, None = not determinable."""
        try:
            out = subprocess.run(
                ["voxtype", "setup", "gpu", "--status"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in out.splitlines():
            if line.startswith("Active backend:"):
                return "GPU" in line
        return None

    def _sync_gpu_state(self):
        active = self.query_gpu_active()
        if active is not None:
            GLib.idle_add(self._set_gpu_check, active)

    def _set_gpu_check(self, active: bool) -> bool:
        self._gpu_guard = True
        self.gpu_item.set_active(active)
        self._gpu_guard = False
        return False  # do not repeat the idle_add

    def on_gpu_toggled(self, item):
        if self._gpu_guard:
            return
        threading.Thread(
            target=self._switch_gpu, args=(item.get_active(),), daemon=True
        ).start()

    def _switch_gpu(self, enable: bool):
        flag = "--enable" if enable else "--disable"
        try:
            result = subprocess.run(
                ["sudo", "-A", "voxtype", "setup", "gpu", flag],
                env=sudo_env(), capture_output=True, text=True, timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = None
            print(f"GPU switch failed: {exc}", file=sys.stderr)
        if result is not None and result.returncode == 0:
            subprocess.run(
                ["systemctl", "--user", "restart", "voxtype"],
                capture_output=True, timeout=60,
            )
        else:
            # Cancelled (e.g. askpass dialog closed) or failed — only bother
            # the user on a real error; the checkbox is resynced below.
            if result is not None and result.stderr.strip():
                self.run_bg(["notify-send", "-u", "critical", "Voxtype",
                             f"GPU switch failed:\n{result.stderr.strip()[:200]}"])
        self._sync_gpu_state()

    # --- State handling ---------------------------------------------------

    def read_state(self) -> str | None:
        try:
            return STATE_FILE.read_text().strip() or "idle"
        except OSError:
            return None  # daemon not running

    def refresh(self, *_args) -> bool:
        state = self.read_state()
        if state != self.current:
            self.current = state
            key = state if state in ICONS else "idle"
            self.icon.set_icon_name(str(ICONS[key]))
            self.icon.set_tooltip_text(
                TOOLTIPS.get(state, TOOLTIPS["idle"]) if state else TOOLTIP_OFF
            )
            print(f"State: {state or 'daemon off'}", file=sys.stderr)
        return True  # keep the timeout alive

    def on_fs_event(self, _monitor, changed, _other, _event):
        if changed.get_basename() == "state":
            self.refresh()

    def on_activate(self, _icon, _button, _time):
        self.toggle()

    def toggle(self):
        self.run_bg(["voxtype", "record", "toggle"])

    @staticmethod
    def run_bg(cmd):
        try:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            print(f"Command failed: {cmd}: {exc}", file=sys.stderr)


def main():
    VoxtypeTray()
    Gtk.main()


if __name__ == "__main__":
    main()
