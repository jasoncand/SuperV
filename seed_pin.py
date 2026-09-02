#!/usr/bin/python3
import json, time, os
p = os.path.expanduser("~/.local/share/superv/history.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump([{"text": "demo pin — survives reboot", "ts": time.time(), "pinned": True}],
          open(p, "w"))
print("seeded")
