#!/usr/bin/env bash
# Complete Voxtype setup for Linux Mint 22.x (X11/Cinnamon).
#
# Idempotent: steps that are already done are skipped.
# Details and rationale: README.md
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VOXTYPE_VERSION="0.7.5"
YDOTOOL_VERSION="v1.0.4"
PARAKEET_MODEL="parakeet-tdt-0.6b-v3-int8"

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# --- Preconditions --------------------------------------------------------
grep -q 'ID=linuxmint' /etc/os-release || warn "Not Linux Mint — this setup targets Mint 22.x."
[ "${XDG_SESSION_TYPE:-}" = x11 ] || warn "Not an X11 session — this setup targets X11 (dotool/xclip)."

# --- Package dependencies -------------------------------------------------
log "Package dependencies"
sudo apt-get install -y xclip libnotify-bin wget golang-go libxkbcommon-dev \
    python3-gi gir1.2-xapp-1.0 gir1.2-gtk-3.0

# --- Voxtype (.deb from the official release) -----------------------------
if ! command -v voxtype >/dev/null; then
    log "Installing Voxtype ${VOXTYPE_VERSION}"
    deb="$(mktemp -d)/voxtype_${VOXTYPE_VERSION}-1_amd64.deb"
    wget -qO "$deb" "https://github.com/peteonrails/voxtype/releases/download/v${VOXTYPE_VERSION}/voxtype_${VOXTYPE_VERSION}-1_amd64.deb"
    sudo apt-get install -y "$deb"
else
    log "Voxtype present: $(voxtype --version)"
fi

# --- ydotool >= 1.0 (distro package 0.1.8 is incompatible -> types '2442') -
if dpkg -s ydotool >/dev/null 2>&1; then
    log "Removing distro ydotool 0.1.8 (incompatible key syntax)"
    sudo apt-get purge -y ydotool
fi
if [ ! -x /usr/local/bin/ydotool ]; then
    log "Installing ydotool ${YDOTOOL_VERSION}"
    tmp="$(mktemp -d)"
    wget -qO "$tmp/ydotool"  "https://github.com/ReimuNotMoe/ydotool/releases/download/${YDOTOOL_VERSION}/ydotool-release-ubuntu-latest"
    wget -qO "$tmp/ydotoold" "https://github.com/ReimuNotMoe/ydotool/releases/download/${YDOTOOL_VERSION}/ydotoold-release-ubuntu-latest"
    sudo install -m 755 "$tmp/ydotool" "$tmp/ydotoold" /usr/local/bin/
fi

# --- dotool (layout-aware typing, incl. umlauts) --------------------------
if [ ! -x /usr/local/bin/dotool ]; then
    log "Building dotool from source"
    tmp="$(mktemp -d)"
    git clone --depth 1 https://git.sr.ht/~geb/dotool "$tmp/dotool"
    (cd "$tmp/dotool" && go build -o dotool .)
    sudo install -m 755 "$tmp/dotool/dotool" /usr/local/bin/dotool
fi

# --- udev: uaccess instead of the input group -----------------------------
if [ ! -f /etc/udev/rules.d/70-voxtype-uaccess.rules ]; then
    log "Installing udev rule (ACL for input devices + uinput)"
    sudo install -m 644 "$REPO_DIR/system/70-voxtype-uaccess.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=input
fi

# --- systemd user services ------------------------------------------------
log "ydotoold user service"
install -m 644 "$REPO_DIR/system/ydotoold.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ydotoold

# --- Voxtype configuration ------------------------------------------------
if [ -f ~/.config/voxtype/config.toml ] && ! cmp -s "$REPO_DIR/config/config.toml" ~/.config/voxtype/config.toml; then
    warn "Existing config.toml differs — backing up to config.toml.bak"
    cp ~/.config/voxtype/config.toml ~/.config/voxtype/config.toml.bak
fi
mkdir -p ~/.config/voxtype
install -m 644 "$REPO_DIR/config/config.toml" ~/.config/voxtype/config.toml

# --- Parakeet engine + model ----------------------------------------------
if ! voxtype setup onnx --status 2>/dev/null | grep -qi onnx; then
    log "Switching to the ONNX/Parakeet backend"
    sudo voxtype setup onnx --enable || voxtype setup onnx --enable
fi
if [ ! -d ~/.local/share/voxtype/models/"$PARAKEET_MODEL" ]; then
    log "Downloading the Parakeet model (~670 MB)"
    voxtype setup --download --model "$PARAKEET_MODEL" --no-post-install
fi

log "Enabling the Voxtype daemon"
voxtype setup systemd >/dev/null 2>&1 || true
systemctl --user daemon-reload
# The upstream unit is WantedBy=graphical-session.target, a target Cinnamon
# (X11) never activates — enabled that way, the daemon never starts at login.
# Hook it into default.target instead (like ydotoold). disable must come
# first: it removes ALL wants-symlinks, including one added by add-wants.
systemctl --user disable voxtype >/dev/null 2>&1 || true
systemctl --user add-wants default.target voxtype.service
systemctl --user restart voxtype

# --- Tray icon ------------------------------------------------------------
log "Setting up the tray autostart"
mkdir -p ~/.config/autostart
sed "s|@TRAY@|$REPO_DIR/tray/voxtype-tray.py|" "$REPO_DIR/tray/voxtype-tray.desktop.in" \
    > ~/.config/autostart/voxtype-tray.desktop
pgrep -f voxtype-tray.py >/dev/null || nohup "$REPO_DIR/tray/voxtype-tray.py" >/dev/null 2>&1 &

log "Done. To dictate: hold right Ctrl, speak, release."
