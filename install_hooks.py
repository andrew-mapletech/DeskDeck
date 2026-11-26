#!/usr/bin/env python3
"""Add (or remove) DeskDeck's Claude Code hooks in ~/.claude/settings.json.

  python3 install_hooks.py            # install
  python3 install_hooks.py --remove   # uninstall

The hooks only send each event to DeskDeck on this Mac (127.0.0.1). If DeskDeck
isn't running, the hook gives up after 1 second and Claude Code carries on.
A backup of settings.json is written next to it before any change.
"""
import json
import os
import shutil
import sys
import time

SETTINGS = os.path.expanduser("~/.claude/settings.json")
PORT = os.environ.get("DESKDECK_PORT", "8770")
URL = f"http://127.0.0.1:{PORT}/api/hook"
CMD = (f"curl -s -m 1 -X POST -H 'Content-Type: application/json' --data-binary @- {URL} "
       f">/dev/null 2>&1 || true")
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "Stop", "SessionEnd"]


def is_ours(group):
    return any("/api/hook" in h.get("command", "") and "127.0.0.1" in h.get("command", "")
               for h in group.get("hooks", []))


def main():
    remove = "--remove" in sys.argv
    settings = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS) as f:
            settings = json.load(f)
        backup = f"{SETTINGS}.deskdeck-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(SETTINGS, backup)
        print("Backup:", backup)
    hooks = settings.setdefault("hooks", {})
    for ev in EVENTS:
        groups = [g for g in hooks.get(ev, []) if not is_ours(g)]
        if not remove:
            entry = {"hooks": [{"type": "command", "command": CMD, "timeout": 3}]}
            if ev == "PreToolUse":
                entry["matcher"] = "*"
            groups.append(entry)
        if groups:
            hooks[ev] = groups
        else:
            hooks.pop(ev, None)
    if not hooks:
        settings.pop("hooks")
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("Removed DeskDeck hooks." if remove else
          f"Installed DeskDeck hooks for: {', '.join(EVENTS)}.\n"
          "New Claude Code sessions will report to DeskDeck (restart open ones).")


if __name__ == "__main__":
    main()
