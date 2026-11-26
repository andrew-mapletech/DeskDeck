#!/bin/zsh
# Double-click to run DeskDeck in Terminal. Close the window (or Ctrl+C) to stop it.
cd "$(dirname "$0")"
exec python3 server.py
