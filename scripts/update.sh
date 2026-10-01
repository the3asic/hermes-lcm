#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if command -v git >/dev/null 2>&1; then
  git -C "$REPO_ROOT" pull --ff-only
fi

"$SCRIPT_DIR/install.sh"

echo "Update complete. Runtime changes require the controlled Hermes restart procedure."
echo "For docs/history-only changes, verify unchanged runtime files and keep Hermes running."
