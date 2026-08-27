# voxtype-mint-setup

Komplette, reproduzierbare Einrichtung von [Voxtype](https://voxtype.io)
(lokales Push-to-talk-Diktat, Parakeet-Engine) auf **Linux Mint 22.x mit
X11/Cinnamon** — inklusive Tray-Icon mit Zustandsanzeige.

Voxtype ist primär für Wayland (Hyprland/Omarchy) gebaut; unter Mint/X11
laufen einige Dinge anders als in der Anleitung des Projekts. Dieses Repo
löst die Mint-spezifischen Probleme:

| Problem unter Mint 22.x | Lösung hier |
|---|---|
| Distro-`ydotool` 0.1.8 versteht die moderne key-Syntax nicht — beim Einfügen erscheint wörtlich `2442` statt Text | ydotool **1.0.4** (offizielle Binaries) nach `/usr/local/bin` + `ydotoold`-User-Service |
| `ydotool type` kann auf deutschem Layout keine Umlaute tippen (US-Keycode-Mapping) | [`dotool`](https://git.sr.ht/~geb/dotool) aus dem Quellcode, mit `dotool_xkb_layout = "de"` — tippt äöüß korrekt, funktioniert in Terminal **und** GUI gleich |
| Paste-Modus (Strg+V) scheitert in Terminals; Shift+Einfg fügt in kitty/gnome-terminal die Primary-Selection ein, nicht das Clipboard | Type-Modus mit dotool statt Paste — Clipboard bleibt unangetastet |
| Hotkey braucht die `input`-Gruppe; die greift erst nach vollständigem Neustart des systemd-User-Managers | udev-Regel mit `uaccess`-Tag: ACL für den aktiven Seat-User, wirkt sofort, enger gefasst als die Gruppe |
| `voxtype-osd` (Aufnahme-Overlay) setzt Wayland-Layer-Shell voraus — unter X11 nicht lauffähig | eigenes **Tray-Icon** (XApp.StatusIcon): grau = bereit, grün = Aufnahme, gelb = transkribiert; Linksklick = Aufnahme umschalten |
| Notebook ohne ScrollLock-Taste (Default-Hotkey) | Hotkey `RIGHTCTRL` (rechte Strg, Push-to-talk) |

## Installation

```bash
git clone https://github.com/ralfkuh-lab/voxtype-mint-setup.git ~/dev/voxtype-mint-setup
cd ~/dev/voxtype-mint-setup
./install.sh
```

Das Skript ist idempotent und fragt bei Bedarf per sudo nach. Danach:
**rechte Strg halten, sprechen, loslassen** — der Text wird an der
Cursor-Position getippt.

## Komponenten

- `install.sh` — idempotentes Komplett-Setup (Pakete, Voxtype-.deb,
  ydotool 1.0.4, dotool-Build, udev-Regel, Services, Config, Tray)
- `config/config.toml` — Voxtype-Konfiguration (Parakeet int8, Type-Modus
  via dotool, Layout de, Hotkey RIGHTCTRL, Feedback/OSD aus)
- `system/70-voxtype-uaccess.rules` — udev-Regel (uaccess für Input-Devices)
- `system/ydotoold.service` — systemd-User-Unit für den ydotool-Daemon
- `tray/voxtype-tray.py` — Tray-Icon; beobachtet
  `$XDG_RUNTIME_DIR/voxtype/state` per File-Monitor (kein Polling im
  Normalfall), Rechtsklick-Menü mit Neustart/Beenden
- `tray/make-icons.py` — erzeugt die Zustands-Icons aus `tray/icons/mic-base.png`
  (Basis-Glyph mit GPT Image 2 generiert, Alpha nachträglich extrahiert)

## Betrieb

```bash
systemctl --user status voxtype     # Daemon
journalctl --user -u voxtype -f     # Logs
voxtype configure                   # Konfigurations-TUI (Achtung: schreibt
                                    # die config.toml neu — Abweichungen
                                    # danach ggf. hier ins Repo zurückspielen)
voxtype record toggle               # Aufnahme per Kommando (macht auch der
                                    # Linksklick aufs Tray-Icon)
```

Modellwahl: `parakeet-tdt-0.6b-v3-int8` (~640 MB, CPU, mehrsprachig inkl.
Deutsch, transkribiert ~7 s Audio in ~0,3 s). Alternativen:
`voxtype setup model`.

## GPU-Beschleunigung

Auf CPUs **ohne AVX-512** (z. B. Ryzen 5000 „Cezanne") ist Parakeet auf der
CPU bereits die schnellste Option: Voxtypes vorgebaute ONNX-CUDA-Backends
setzen AVX-512 voraus (`voxtype setup gpu --enable` verweigert sonst mit
Fehlermeldung), und die einzige verbleibende GPU-Route — Rückwechsel auf die
Whisper-Engine mit Vulkan — ist real langsamer als Parakeet-CPU.

Auf AVX-512-Systemen schaltet der Tray-Menüpunkt **„GPU-Beschleunigung
(CUDA)"** das Backend um (sudo über zenity-Askpass, Daemon-Neustart
inklusive); ohne AVX-512 ist der Punkt ausgegraut. Zu bedenken: Das Modell
liegt dann dauerhaft im VRAM und der Daemon hält die dGPU wach (Akku).
