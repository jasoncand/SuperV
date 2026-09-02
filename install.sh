#!/usr/bin/env bash
# Install SuperV on Linux Mint (Cinnamon).
set -e

echo "==> Installing dependencies (python3-gi, xclip, xdotool)…"
sudo apt-get update -qq
sudo apt-get install -y python3-gi gir1.2-gtk-3.0 xclip xdotool

echo "==> Installing scripts (SuperV)…"
mkdir -p "$HOME/.local/bin"
install -m 755 superv.py        "$HOME/.local/bin/superv-daemon.py"
install -m 755 superv-toggle.sh "$HOME/.local/bin/superv-toggle"

# Remove leftovers from the old ClipVault install
rm -f "$HOME/.local/bin/clipvault-daemon.py" \
      "$HOME/.local/bin/clipvault-toggle" \
      "$HOME/.config/autostart/clipvault.desktop"

echo "==> Adding autostart entry…"
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/superv.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SuperV
Comment=Clipboard history manager (Super+V)
Exec=/usr/bin/python3 $HOME/.local/bin/superv-daemon.py
X-GNOME-Autostart-enabled=true
Terminal=false
Hidden=false
EOF

echo "==> Binding Super+V shortcut…"
TOGGLE_CMD="$HOME/.local/bin/superv-toggle"
CUSTOM_BASE="org.cinnamon.desktop.keybindings.custom-keybinding:/org/cinnamon/desktop/keybindings/custom-keybindings"

EXISTING=$(gsettings get org.cinnamon.desktop.keybindings custom-list 2>/dev/null || echo "[]")

# Check if Super+V is already bound
ALREADY_BOUND=false
if echo "$EXISTING" | grep -q "'<Super>v'" || echo "$EXISTING" | grep -q '"<Super>v"'; then
    ALREADY_BOUND=true
fi

if [ "$ALREADY_BOUND" = true ]; then
    echo "  ✅ Super+V shortcut already bound."
else
    # Check if we already have a SuperV slot to reuse
    REUSE_SLOT=""
    for i in $(seq 0 20); do
        if echo "$EXISTING" | grep -q "'custom$i'"; then
            NAME=$(gsettings get "${CUSTOM_BASE}/custom${i}/" name 2>/dev/null || echo "")
            if echo "$NAME" | grep -q "SuperV"; then
                REUSE_SLOT="custom$i"
                break
            fi
        fi
    done

    if [ -n "$REUSE_SLOT" ]; then
        gsettings set "${CUSTOM_BASE}/${REUSE_SLOT}/" command "$TOGGLE_CMD"
        gsettings set "${CUSTOM_BASE}/${REUSE_SLOT}/" binding "['<Super>v']"
        echo "  ✅ Rebound Super+V to existing slot ${REUSE_SLOT}"
    else
        # Find a free slot
        SLOT=""
        for i in $(seq 0 20); do
            if ! echo "$EXISTING" | grep -q "'custom$i'"; then
                SLOT="custom$i"
                break
            fi
        done

        if [ -n "$SLOT" ]; then
            NEW_LIST=$(echo "$EXISTING" | sed "s/]$/, '${SLOT}']/")
            case "$EXISTING" in
                "@as []"|"[]") NEW_LIST="['${SLOT}']" ;;
            esac
            gsettings set org.cinnamon.desktop.keybindings custom-list "$NEW_LIST"
            gsettings set "${CUSTOM_BASE}/${SLOT}/" name "SuperV"
            gsettings set "${CUSTOM_BASE}/${SLOT}/" command "$TOGGLE_CMD"
            gsettings set "${CUSTOM_BASE}/${SLOT}/" binding "['<Super>v']"
            echo "  ✅ Bound Super+V to ${SLOT}"
        else
            echo "  ⚠️  Could not find a free custom shortcut slot."
            echo "  Please bind manually: System Settings → Keyboard → Shortcuts → Custom Shortcuts"
        fi
    fi
fi

echo "==> Stopping old daemon…"
pkill -f superv-daemon.py 2>/dev/null || true
sleep 0.5

echo "==> Starting daemon…"
nohup /usr/bin/python3 "$HOME/.local/bin/superv-daemon.py" >"$HOME/.cache/superv.log" 2>&1 &
sleep 1

echo ""
echo "✅ SuperV is running. Super+V is ready!"
