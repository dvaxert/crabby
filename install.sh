#!/bin/sh
# Install crabby for the current user: CLI, systemd user units, Noctalia plugin source.
set -eu
repo=$(cd "$(dirname "$0")" && pwd)

mkdir -p ~/.local/bin ~/.config/systemd/user
ln -sf "$repo/bridge/crabby.py" ~/.local/bin/crabby
chmod +x "$repo/bridge/crabby.py"
ln -sf "$repo/systemd/crabby-bridge.socket" ~/.config/systemd/user/
ln -sf "$repo/systemd/crabby-bridge.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crabby-bridge.socket

if command -v noctalia >/dev/null; then
  noctalia msg plugins source list | grep -q crabby-dev ||
    noctalia msg plugins source add crabby-dev path "$repo/plugin"
  noctalia msg plugins enable dvaxert/crabby || true
fi
echo "ok. Next: add the dvaxert/crabby:bar widget and hotkeys (see README)"
