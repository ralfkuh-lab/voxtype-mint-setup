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
            ("Tray beenden", lambda *_: Gtk.main_quit()),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        menu.show_all()
        return menu

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
