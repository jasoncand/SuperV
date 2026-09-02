# SuperV v0.0.1

A lightweight clipboard-history manager for **Linux Mint (Cinnamon)** that mimics
**Windows 10/11's Super+V** panel — dark popup, search, previews, timestamps, and
pin-to-keep. Works on any X11 desktop (GNOME, XFCE, etc.).

[Download the latest release](https://github.com/jasoncand/SuperV/releases/tag/v0.0.1)

## Features

### Clipboard history
- **Captures everything** — text, rich text (HTML), images, file URIs, GNOME file
  copies — as you copy (Ctrl+C / Ctrl+X)
- **Unlimited history** by default; set `SUPERV_MAX_ITEMS=N` to cap at N entries
- **Persists across reboots** — `~/.local/share/superv/history.json`
- **Pin items** — pinned entries survive startup and stay forever
- **Tabbed popup** — All / Recent / Pinned views
- **Search** — type to filter in real time across all content

### Super+V popup
- **Dark rounded theme** — matches Windows 11 aesthetic
- **Enter to paste** — auto-pastes (Ctrl+V) into the active window
- **Click outside or Esc** — closes the popup (when unpinned)
- **Pin button** — toggle "always on top" so the popup stays on screen
- **Resize grip** — drag the right edge to adjust popup width (persisted)
- **Logo button** — click to enter drag/grab mode, move the popup anywhere
- **Right-click context menu** — Pin / Unpin, Edit, Delete, Delete Before, Clear All
- **Multi-select** — Ctrl+Space to select multiple items, Enter pastes them together
- **PrtSc support** — pressing Print Screen while popup is open launches a
  screenshot tool and captures the result

### Library window
- **Browse all history** — grid or list view
- **Search & filter** — by text or short command
- **Folders** — organise items into custom folders
- **Rich-text editor** — edit HTML content with a live preview
- **Edit, Pin, Delete** — full item management
- **Import / Export** — backup and restore your clipboard history and folders
- **Sort by short command** — view only items with an assigned shortcut

### Text expander
- Assign **short commands** like `/terms` or `/addr` to any item in the library
- Type the command anywhere and a suggestion bubble appears
- Press Enter to expand it into the full content

### Auto-start
- Installed as a user autostart entry — runs silently in the background

## Install

### Via .deb package (recommended)
```bash
# Download from https://github.com/jasoncand/SuperV/releases
sudo dpkg -i superv_0.0.1_all.deb
sudo apt-get install -f
```

After install, press **Super+V** to toggle the clipboard history popup.
The post-install script binds the shortcut and starts the daemon automatically.

### Manual install from source
```bash
cd SuperV
./install.sh
```

### Build from source
```bash
./build-deb.sh
sudo dpkg -i build/superv_0.0.1_all.deb
```

## Usage

| Key / Action | Result |
|---|---|
| **Super+V** | Toggle clipboard history popup |
| **Type** | Search clipboard history (all content) |
| **↑ / ↓** | Move selection |
| **Enter** | Copy & auto-paste into active window |
| **Ctrl+Space** | Toggle multi-select on current item |
| **Esc** | Close popup |
| **Click outside** | Close popup (when unpinned) |
| **Click pin icon** | Toggle always-on-top (pinned / unpinned) |
| **Drag resize grip** | Adjust popup width |
| **Click logo** | Enter grab mode to drag-reposition popup |
| **Right-click** item | Context menu (pin, edit, delete, clear all) |
| **PrtSc** while popup open | Launch screenshot tool |

## Files

| Path | Purpose |
|---|---|
| `/usr/share/superv/superv-daemon.py` | Background daemon (autostarted) |
| `/usr/bin/superv-toggle` | Toggle script bound to Super+V |
| `~/.local/share/superv/history.json` | Clipboard history data |
| `~/.local/share/superv/settings.json` | Popup width & preferences |
| `~/.local/share/superv/folders.json` | Library folder definitions |
| `~/.local/share/superv/images/` | Captured clipboard images |
| `~/.cache/superv.log` | Daemon log |

## Requirements

- **X11 desktop** (Cinnamon, GNOME, XFCE, etc.) — Wayland is not supported
- Python 3.8+, PyGObject (GTK 3), xclip, xdotool
- Linux Mint Cinnamon recommended for best experience

## Uninstall

```bash
sudo dpkg -r superv
rm -rf ~/.local/share/superv ~/.cache/superv.log
```

## License

MIT — see [LICENSE](LICENSE).