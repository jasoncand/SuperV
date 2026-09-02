# SuperV — Windows-style clipboard manager for Linux Mint

A lightweight clipboard-history manager that mimics **Windows 10/11's
Super+V** panel: dark popup with search, previews and timestamps.

## Features
- Records everything you copy (Ctrl+C / Ctrl+X) — unlimited history
  by default (set `SUPERV_MAX_ITEMS=N` to cap at N entries)
- Persists across reboots (`~/.local/share/superv/history.json`)
- Press **Super+V** to toggle the popup
- Type to filter, ↑/↓ to move, **Enter** copies it back **and pastes**
  into whatever window has focus (via Ctrl+V)
- Esc or clicking away dismisses it
- Pin items so they persist forever
- Library window to browse, search, edit, and organise all history
- Text expander: assign short commands like `/terms` to any item

## Install

### Via .deb package (recommended)
```bash
sudo dpkg -i build/superv_0.0.1_all.deb
sudo apt-get install -f
```

### Manual install
```bash
cd ~/Downloads/Trae/SuperV
./install.sh
```

After install, press **Super+V** to open the clipboard history popup.

## Usage
| Key | Action |
|-----|--------|
| Super+V | Toggle popup |
| type | Search history |
| ↑ / ↓ | Move selection |
| Enter | Copy selection & auto-paste |
| Esc | Close |
| Click outside | Close (when unpinned) |

## Files
- Daemon: `/usr/share/superv/superv-daemon.py` (autostarts at login)
- Toggle script: `/usr/bin/superv-toggle`
- History data: `~/.local/share/superv/history.json`
- Settings: `~/.local/share/superv/settings.json`
- Log: `~/.cache/superv.log`

## Notes
- Requires X11 (Cinnamon, GNOME, XFCE, etc.). Wayland is not supported.
- To stop autostarting: delete `~/.config/autostart/superv.desktop`.
- Data from the old "ClipVault" install is migrated automatically on first run.

## License
MIT — see [LICENSE](LICENSE).