#!/usr/bin/env bash
# Bootstrap install — clone (or pull) the repo, create venv, install package.
#
# Usage:
#   ./deploy/install.sh
# Or fresh from internet:
#   curl -fsSL https://raw.githubusercontent.com/ztllll/claude-code-im-channel/main/deploy/install.sh | bash

set -euo pipefail

REPO_URL="https://github.com/ztllll/claude-code-im-channel.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/claude-code-im-channel}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> install dir: $INSTALL_DIR"

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    git clone "$REPO_URL" "$INSTALL_DIR"
else
    echo "==> repo exists, pulling"
    git -C "$INSTALL_DIR" pull --ff-only
fi

cd "$INSTALL_DIR"

if [[ ! -d .venv ]]; then
    echo "==> creating venv with $PYTHON_BIN"
    "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

echo ""
echo "==> done"
echo ""
echo "Next steps:"
echo "  1. cp config.example.yaml config.yaml && edit it (bot token, allowed_user_ids)"
echo "  2. (optional) python -m im_claude_channel import-tmux-sessions --dry-run"
echo "     to preview which existing tmux sessions will be inherited."
echo "  3. mkdir -p ~/.config/systemd/user && cp systemd/im-claude-channel.service ~/.config/systemd/user/"
echo "  4. loginctl enable-linger \$USER"
echo "  5. systemctl --user daemon-reload && systemctl --user enable --now im-claude-channel.service"
echo ""
echo "Logs:"
echo "  journalctl --user -fu im-claude-channel.service"
