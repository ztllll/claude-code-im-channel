#!/usr/bin/env bash
# Pull, reinstall, restart the systemd unit. Safe to re-run.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/claude-code-im-channel}"
cd "$INSTALL_DIR"

git pull --ff-only
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -e . --quiet

systemctl --user daemon-reload
systemctl --user restart im-claude-channel.service
echo "==> restarted; tail with: journalctl --user -fu im-claude-channel.service"
