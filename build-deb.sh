#!/bin/bash
# Build SuperV .deb package
set -e

PKG_NAME="superv"
PKG_VERSION="0.0.1"
ARCH="all"
BUILD_DIR="build"

echo "==> Cleaning previous build..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/DEBIAN"
mkdir -p "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/superv"
mkdir -p "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/bin"
mkdir -p "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/applications"
mkdir -p "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/icons/hicolor/256x256/apps"

echo "==> Copying files..."
# Main application files
install -m 644 superv.py "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/superv/superv-daemon.py"
install -m 755 superv-toggle.sh "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/superv/superv-toggle"
install -m 644 superv-icon.png "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/superv/superv-icon.png"
install -m 644 seed_pin.py "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/superv/seed_pin.py"

# Icon
install -m 644 superv-icon.png "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/icons/hicolor/256x256/apps/superv.png"

# Wrapper scripts
cat > "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/bin/superv" << 'WRAPPER'
#!/bin/bash
exec /usr/bin/python3 /usr/share/superv/superv-daemon.py "$@"
WRAPPER
chmod 755 "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/bin/superv"

cat > "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/bin/superv-toggle" << 'WRAPPER'
#!/bin/bash
# Toggle the SuperV popup. We write a command line to a small file
# that the daemon polls; a FIFO was tried first but lost writes
# when the writer closed before the reader opened.
set -u
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/superv"
CMD_FILE="$DATA_DIR/command.log"

start_daemon() {
    nohup /usr/bin/python3 /usr/share/superv/superv-daemon.py >"$HOME/.cache/superv.log" 2>&1 &
    for _ in $(seq 1 30); do
        pgrep -f superv-daemon.py >/dev/null && break
        sleep 0.2
    done
    sleep 1
}

if ! pgrep -f superv-daemon.py >/dev/null; then
    start_daemon
fi

mkdir -p "$DATA_DIR"
printf 'toggle\n' >>"$CMD_FILE"
WRAPPER
chmod 755 "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/bin/superv-toggle"

# Desktop file for app launcher
cat > "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/applications/superv.desktop" << 'DESKTOP'
[Desktop Entry]
Type=Application
Name=SuperV
GenericName=Clipboard Manager
Comment=Windows-style clipboard history manager
Exec=/usr/bin/superv-toggle
Icon=superv
Terminal=false
Categories=Utility;
Keywords=clipboard;history;paste;
DESKTOP

# Autostart template
install -d "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/superv"
cat > "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/usr/share/superv/autostart.desktop" << 'AUTOSTART'
[Desktop Entry]
Type=Application
Name=SuperV
Comment=Clipboard history manager (Super+V)
Exec=/usr/bin/superv
X-GNOME-Autostart-enabled=true
Terminal=false
Hidden=false
AUTOSTART

echo "==> Creating control file..."
cat > "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/DEBIAN/control" << EOF
Package: ${PKG_NAME}
Version: ${PKG_VERSION}
Architecture: ${ARCH}
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, python3-xlib, xclip, xdotool
Recommends: cinnamon-desktop-environment
Section: utils
Priority: optional
Maintainer: Jason Cand <jasoncand@example.com>
Homepage: https://github.com/jasoncand/SuperV
Description: Windows-style clipboard manager for Linux Mint
 A lightweight clipboard-history manager that mimics Windows 10/11's
 Super+V panel. Features a dark popup with search, previews, timestamps,
 pin support, image clipboard capture, and a full library window for
 browsing all copied items.
 .
 Press Super+V to toggle the popup. Pin items for persistence across
 reboots. Supports text (plain/formatted), images, files and media.
 .
 Includes a text expander: assign short commands like /terms to any item
 via the library window, type them anywhere, and SuperV offers to expand
 them into the assigned content.
EOF

echo "==> Creating postinst script..."
cat > "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/DEBIAN/postinst" << 'POSTINST'
#!/bin/bash
set -e

# Detect the real (non-root) user
REAL_USER="${SUDO_USER:-}"
if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    REAL_USER=$(logname 2>/dev/null || true)
fi
if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    REAL_USER=$(who 2>/dev/null | grep -v '^root ' | head -1 | awk '{print $1}' || true)
fi
if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    for d in /home/*; do
        u=$(basename "$d")
        if [ "$u" != "root" ] && [ -d "$d" ]; then
            REAL_USER="$u"
            break
        fi
    done
fi

if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    echo "WARNING: Could not detect user."
    echo "Please bind Super+V manually:"
    echo "  System Settings -> Keyboard -> Shortcuts -> Custom Shortcuts"
    echo "  Command: /usr/bin/superv-toggle"
    exit 0
fi

USER_UID=$(id -u "$REAL_USER")
USER_HOME=$(eval echo "~${REAL_USER}")

# Create autostart directory and link autostart file
AUTOSTART_DIR="${USER_HOME}/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
if [ -f /usr/share/superv/autostart.desktop ]; then
    cp /usr/share/superv/autostart.desktop "$AUTOSTART_DIR/superv.desktop"
fi

# Create data directory
mkdir -p "${USER_HOME}/.local/share/superv"

# Fix dconf cache ownership (previous broken installs may have set it to root)
mkdir -p "${USER_HOME}/.cache/dconf"
chown -R "$REAL_USER:$REAL_USER" "${USER_HOME}/.cache/dconf" 2>/dev/null || true

DBUS_SOCK="unix:path=/run/user/${USER_UID}/bus"
RUN_ENV="env DISPLAY=${DISPLAY:-:0} DBUS_SESSION_BUS_ADDRESS=${DBUS_SOCK} HOME=${USER_HOME} XDG_RUNTIME_DIR=/run/user/${USER_UID}"

# Write a bind script and run it as the real user
BIND_SCRIPT=$(mktemp /tmp/superv-bind-XXXXXX.sh)
cat > "$BIND_SCRIPT" << 'BINDSCRIPT'
#!/bin/bash
# Auto-bind Super+V in Cinnamon
CUSTOM_BASE="org.cinnamon.desktop.keybindings.custom-keybinding:/org/cinnamon/desktop/keybindings/custom-keybindings"

EXISTING=$(gsettings get org.cinnamon.desktop.keybindings custom-list 2>/dev/null || echo "[]")

if echo "$EXISTING" | grep -q "'<Super>v'" || echo "$EXISTING" | grep -q '"<Super>v"'; then
    echo "Super+V shortcut already bound."
    exit 0
fi

for i in $(seq 0 20); do
    if echo "$EXISTING" | grep -q "'custom$i'"; then
        NAME=$(gsettings get "${CUSTOM_BASE}/custom${i}/" name 2>/dev/null || echo "")
        if echo "$NAME" | grep -q "SuperV"; then
            gsettings set "${CUSTOM_BASE}/custom${i}/" command '/usr/bin/superv-toggle'
            gsettings set "${CUSTOM_BASE}/custom${i}/" binding "['<Super>v']"
            echo "Rebound Super+V to existing slot custom${i}."
            exit 0
        fi
    fi
done

SLOT=""
for i in $(seq 0 20); do
    if ! echo "$EXISTING" | grep -q "'custom$i'"; then
        SLOT="custom$i"
        break
    fi
done

if [ -z "$SLOT" ]; then
    echo "No free custom shortcut slot found."
    exit 1
fi

NEW_LIST=$(echo "$EXISTING" | sed "s/]$/, '${SLOT}']/")
case "$EXISTING" in
    "@as []"|"[]") NEW_LIST="['${SLOT}']" ;;
esac
gsettings set org.cinnamon.desktop.keybindings custom-list "$NEW_LIST"
gsettings set "${CUSTOM_BASE}/${SLOT}/" name "SuperV"
gsettings set "${CUSTOM_BASE}/${SLOT}/" command "/usr/bin/superv-toggle"
gsettings set "${CUSTOM_BASE}/${SLOT}/" binding "['<Super>v']"
echo "Bound Super+V to ${SLOT}."
BINDSCRIPT
chmod 755 "$BIND_SCRIPT"
chown "$REAL_USER:$REAL_USER" "$BIND_SCRIPT"

sudo -u "$REAL_USER" $RUN_ENV bash "$BIND_SCRIPT" 2>/dev/null || \
    echo "NOTE: Bind Super+V manually in System Settings -> Keyboard -> Shortcuts"
rm -f "$BIND_SCRIPT"

# Kill old daemon, start new one
sudo -u "$REAL_USER" $RUN_ENV bash -c \
    "pkill -f 'superv-daemon.py' 2>/dev/null; sleep 0.5; nohup /usr/bin/python3 /usr/share/superv/superv-daemon.py >'\$HOME/.cache/superv.log' 2>&1 &" 2>/dev/null || true

echo ""
echo "SuperV installed! Super+V is ready."
echo ""
POSTINST
chmod 755 "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/DEBIAN/postinst"

echo "==> Creating prerm script..."
cat > "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/DEBIAN/prerm" << 'PRERM'
#!/bin/bash
set -e

# Kill running daemon if any
if pgrep -f "superv-daemon.py" >/dev/null 2>&1; then
    pkill -f "superv-daemon.py" 2>/dev/null || true
fi
PRERM
chmod 755 "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/DEBIAN/prerm"

echo "==> Creating postrm script..."
cat > "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/DEBIAN/postrm" << 'POSTRM'
#!/bin/bash
set -e

# Remove autostart entry
rm -f "$HOME/.config/autostart/superv.desktop"

# Kill running daemon if any
if pgrep -f "superv-daemon.py" >/dev/null 2>&1; then
    pkill -f "superv-daemon.py" 2>/dev/null || true
fi
POSTRM
chmod 755 "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}/DEBIAN/postrm"

echo "==> Building .deb package..."
dpkg-deb --build "$BUILD_DIR/${PKG_NAME}_${PKG_VERSION}_${ARCH}"

echo ""
echo "==> Package built: ${BUILD_DIR}/${PKG_NAME}_${PKG_VERSION}_${ARCH}.deb"
echo ""
echo "To install:"
echo "  sudo dpkg -i ${BUILD_DIR}/${PKG_NAME}_${PKG_VERSION}_${ARCH}.deb"
echo "  sudo apt-get install -f   # fix any missing dependencies"
echo ""
echo "To uninstall:"
echo "  sudo dpkg -r ${PKG_NAME}"
echo ""
