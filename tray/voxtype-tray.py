#!/usr/bin/env python3
"""Tray-Icon fuer Voxtype (Linux Mint / Cinnamon, XApp.StatusIcon).

Ueberwacht die State-Datei des Voxtype-Daemons
($XDG_RUNTIME_DIR/voxtype/state) und zeigt den Zustand als Mikrofon-Icon:

  idle          - graues Mikrofon
  recording     - weisses Mikrofon auf gruenem Kreis
  transcribing  - weisses Mikrofon auf gelbem Kreis
  (Datei fehlt) - graues Mikrofon, Tooltip weist auf inaktiven Daemon hin

Linksklick schaltet die Aufnahme um (voxtype record toggle).
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
    "idle": "Voxtype bereit — Hotkey halten zum Diktieren, Klick zum Umschalten",
    "recording": "Voxtype: Aufnahme läuft …",
    "transcribing": "Voxtype: transkribiert …",
}
TOOLTIP_OFF = "Voxtype-Daemon läuft nicht (systemctl --user start voxtype)"

# Der Backend-Wechsel tauscht einen Symlink unter /usr/lib/voxtype -> sudo.
# Beim Autostart fehlt SUDO_ASKPASS (steht nur in der .bashrc), deshalb
# explizit setzen, damit sudo -A das grafische zenity-Askpass nutzt.
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

        # Verzeichnis ueberwachen: die State-Datei wird bei jedem
        # Zustandswechsel neu geschrieben, bei Daemon-Neustart neu angelegt.
        self.monitor = Gio.File.new_for_path(str(STATE_FILE.parent)).monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        self.monitor.connect("changed", self.on_fs_event)

        # Sicherheitsnetz, falls ein Ereignis verloren geht
        GLib.timeout_add_seconds(10, self.refresh)

    def build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        for label, cb in (
            ("Aufnahme umschalten", lambda *_: self.toggle()),
            ("Einstellungen (voxtype configure)", lambda *_: self.run_bg(
                ["x-terminal-emulator", "-e", "voxtype configure"])),
            ("Voxtype neu starten", lambda *_: self.run_bg(
                ["systemctl", "--user", "restart", "voxtype"])),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)

        self._gpu_guard = False
        self.gpu_item = Gtk.CheckMenuItem(label="GPU-Beschleunigung (CUDA)")
        self.gpu_item.connect("toggled", self.on_gpu_toggled)
        # Voxtypes vorgebaute ONNX-CUDA-Binaries setzen AVX-512 voraus —
        # ohne das schlaegt --enable immer fehl, also gleich ausgrauen.
        if not self._cpu_has_avx512():
            self.gpu_item.set_label("GPU-Beschleunigung (benötigt AVX-512-CPU)")
            self.gpu_item.set_sensitive(False)
        menu.append(self.gpu_item)

        quit_item = Gtk.MenuItem(label="Tray beenden")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        threading.Thread(target=self._sync_gpu_state, daemon=True).start()
        return menu

    # --- GPU-Backend-Umschaltung -----------------------------------------

    @staticmethod
    def _cpu_has_avx512() -> bool:
        try:
            return "avx512" in Path("/proc/cpuinfo").read_text()
        except OSError:
            return False

    def query_gpu_active(self):
        """True/False = aktives Backend ist GPU/CPU, None = nicht ermittelbar."""
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
        return False  # idle_add nicht wiederholen

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
            print(f"GPU-Umschaltung fehlgeschlagen: {exc}", file=sys.stderr)
        if result is not None and result.returncode == 0:
            subprocess.run(
                ["systemctl", "--user", "restart", "voxtype"],
                capture_output=True, timeout=60,
            )
        else:
            # Abbruch (z. B. zenity-Dialog geschlossen) oder Fehler -> nur
            # bei echtem Fehler stoeren, das Haekchen wird unten zurueckgesetzt
            if result is not None and result.stderr.strip():
                self.run_bg(["notify-send", "-u", "critical", "Voxtype",
                             f"GPU-Umschaltung fehlgeschlagen:\n{result.stderr.strip()[:200]}"])
        self._sync_gpu_state()

    def read_state(self) -> str | None:
        try:
            return STATE_FILE.read_text().strip() or "idle"
        except OSError:
            return None  # Daemon laeuft nicht

    def refresh(self, *_args) -> bool:
        state = self.read_state()
        if state != self.current:
            self.current = state
            key = state if state in ICONS else "idle"
            self.icon.set_icon_name(str(ICONS[key]))
            self.icon.set_tooltip_text(
                TOOLTIPS.get(state, TOOLTIPS["idle"]) if state else TOOLTIP_OFF
            )
            print(f"Zustand: {state or 'daemon aus'}", file=sys.stderr)
        return True  # Timeout aktiv halten

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
            print(f"Kommando fehlgeschlagen: {cmd}: {exc}", file=sys.stderr)


def main():
    VoxtypeTray()
    Gtk.main()


if __name__ == "__main__":
    main()
