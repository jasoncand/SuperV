#!/usr/bin/env bash
# Toggle the SuperV popup. Bind this to Super+V in Cinnamon.
# It starts the daemon on first use and writes a command to a small
# file that the daemon polls. (We used to use a FIFO but that
# suffered from a race where quick successive writes were lost
# because the writer closed before the reader opened.)
set -u
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/superv"
CMD_FILE="$DATA_DIR/command.log"

start_daemon() {
    nohup /usr/bin/python3 /usr/share/superv/superv-daemon.py \
        >"$HOME/.cache/superv.log" 2>&1 &
    # Wait up to 6s for the daemon to appear
    for _ in $(seq 1 30); do
        pgrep -f superv-daemon.py >/dev/null && break
        sleep 0.2
    done
    # Give GTK time to initialise its FIFO reader
    sleep 1
}

if ! pgrep -f superv-daemon.py >/dev/null; then
    start_daemon
fi

mkdir -p "$DATA_DIR"
printf 'toggle\n' >>"$CMD_FILE"
