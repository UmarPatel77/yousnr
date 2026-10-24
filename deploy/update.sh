#!/usr/bin/env bash
# Run on the server after you push an update from your laptop:
#   bash ~/yousnr/deploy/update.sh
# Turns YouSNR off (it finishes what it's doing), gets the new code, rebuilds the pages,
# publishes them, and turns YouSNR back on.
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$HOME/yousnr-venv/bin/python"
cd "$APP"
was_on="$(systemctl is-active yousnr 2>/dev/null || true)"
echo "== turning off"
sudo systemctl stop yousnr || true
echo "== getting the update"
git pull --no-rebase --no-edit
"$PY" -m pip install -q -r requirements.txt
echo "== rebuilding pages"
"$PY" yousnr.py build
git add -A data docs
if ! git diff --cached --quiet; then
  git commit -q -m "Rebuild pages after update"
  git push -q
  echo "   published"
fi
if [ "$was_on" = "active" ]; then
  echo "== turning on"
  sudo systemctl start yousnr
  sleep 8
fi
"$PY" yousnr.py status
