#!/usr/bin/env bash
# Voxtype-Komplett-Setup fuer Linux Mint 22.x (X11/Cinnamon).
#
# Idempotent: bereits erledigte Schritte werden uebersprungen.
# Details und Begruendungen: README.md
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VOXTYPE_VERSION="0.7.5"
YDOTOOL_VERSION="v1.0.4"
PARAKEET_MODEL="parakeet-tdt-0.6b-v3-int8"

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# --- Vorbedingungen -------------------------------------------------------
grep -q 'ID=linuxmint' /etc/os-release || warn "Kein Linux Mint erkannt — Setup ist fuer Mint 22.x gedacht."
[ "${XDG_SESSION_TYPE:-}" = x11 ] || warn "Keine X11-Session — dieses Setup ist fuer X11 ausgelegt (dotool/xclip)."

# --- Paketabhaengigkeiten -------------------------------------------------
log "Paketabhaengigkeiten"
sudo apt-get install -y xclip libnotify-bin wget golang-go libxkbcommon-dev \
    python3-gi gir1.2-xapp-1.0 gir1.2-gtk-3.0

# --- Voxtype (.deb vom offiziellen Release) -------------------------------
if ! command -v voxtype >/dev/null; then
    log "Voxtype ${VOXTYPE_VERSION} installieren"
    deb="$(mktemp -d)/voxtype_${VOXTYPE_VERSION}-1_amd64.deb"
    wget -qO "$deb" "https://github.com/peteonrails/voxtype/releases/download/v${VOXTYPE_VERSION}/voxtype_${VOXTYPE_VERSION}-1_amd64.deb"
    sudo apt-get install -y "$deb"
else
    log "Voxtype vorhanden: $(voxtype --version)"
fi

# --- ydotool >= 1.0 (Distro-Paket 0.1.8 ist inkompatibel -> tippt '2442') -
if dpkg -s ydotool >/dev/null 2>&1; then
    log "Distro-ydotool 0.1.8 entfernen (inkompatible key-Syntax)"
    sudo apt-get purge -y ydotool
fi
if [ ! -x /usr/local/bin/ydotool ]; then
    log "ydotool ${YDOTOOL_VERSION} installieren"
    tmp="$(mktemp -d)"
    wget -qO "$tmp/ydotool"  "https://github.com/ReimuNotMoe/ydotool/releases/download/${YDOTOOL_VERSION}/ydotool-release-ubuntu-latest"
    wget -qO "$tmp/ydotoold" "https://github.com/ReimuNotMoe/ydotool/releases/download/${YDOTOOL_VERSION}/ydotoold-release-ubuntu-latest"
    sudo install -m 755 "$tmp/ydotool" "$tmp/ydotoold" /usr/local/bin/
fi

# --- dotool (tippt layoutbewusst, inkl. Umlaute) --------------------------
if [ ! -x /usr/local/bin/dotool ]; then
    log "dotool aus dem Quellcode bauen"
    tmp="$(mktemp -d)"
    git clone --depth 1 https://git.sr.ht/~geb/dotool "$tmp/dotool"
    (cd "$tmp/dotool" && go build -o dotool .)
    sudo install -m 755 "$tmp/dotool/dotool" /usr/local/bin/dotool
fi

# --- udev: uaccess statt input-Gruppe -------------------------------------
if [ ! -f /etc/udev/rules.d/70-voxtype-uaccess.rules ]; then
    log "udev-Regel installieren (ACL fuer Input-Devices + uinput)"
    sudo install -m 644 "$REPO_DIR/system/70-voxtype-uaccess.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=input
fi

# --- systemd-User-Services ------------------------------------------------
log "ydotoold-User-Service"
install -m 644 "$REPO_DIR/system/ydotoold.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ydotoold

# --- Voxtype-Konfiguration ------------------------------------------------
if [ -f ~/.config/voxtype/config.toml ] && ! cmp -s "$REPO_DIR/config/config.toml" ~/.config/voxtype/config.toml; then
    warn "Bestehende config.toml weicht ab — Sicherung nach config.toml.bak"
    cp ~/.config/voxtype/config.toml ~/.config/voxtype/config.toml.bak
fi
mkdir -p ~/.config/voxtype
install -m 644 "$REPO_DIR/config/config.toml" ~/.config/voxtype/config.toml

# --- Parakeet-Engine + Modell ---------------------------------------------
if ! voxtype setup onnx --status 2>/dev/null | grep -qi onnx; then
    log "Auf ONNX/Parakeet-Backend umschalten"
    sudo voxtype setup onnx --enable || voxtype setup onnx --enable
fi
if [ ! -d ~/.local/share/voxtype/models/"$PARAKEET_MODEL" ]; then
    log "Parakeet-Modell herunterladen (~670 MB)"
    voxtype setup --download --model "$PARAKEET_MODEL" --no-post-install
fi

log "Voxtype-Daemon aktivieren"
voxtype setup systemd >/dev/null 2>&1 || true
systemctl --user enable --now voxtype
systemctl --user restart voxtype

# --- Tray-Icon ------------------------------------------------------------
log "Tray-Autostart einrichten"
mkdir -p ~/.config/autostart
sed "s|@TRAY@|$REPO_DIR/tray/voxtype-tray.py|" "$REPO_DIR/tray/voxtype-tray.desktop.in" \
    > ~/.config/autostart/voxtype-tray.desktop
pgrep -f voxtype-tray.py >/dev/null || nohup "$REPO_DIR/tray/voxtype-tray.py" >/dev/null 2>&1 &

log "Fertig. Diktat: rechte Strg halten, sprechen, loslassen."
