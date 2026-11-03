#!/usr/bin/env bash
# Run on the server after you push an update from your laptop:
#   bash ~/yousnr/deploy/update.sh
# Turns YouSNR off (it finishes what it's doing), gets the new code, updates Playwright and its
# Chromium, yt-dlp and the other packages to their newest versions, keeps the browser sandbox set up,
# rebuilds and publishes the pages, and turns YouSNR back on (also if a step fails).
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$HOME/yousnr-venv/bin/python"
cd "$APP"

if [ "${1:-}" = "--after-pull" ]; then
  was_on="${2:-}"
else
  was_on="$(systemctl is-active yousnr 2>/dev/null || true)"
fi
turn_back_on() {
  if [ "$was_on" = "active" ] && ! systemctl is-active --quiet yousnr; then
    echo "== turning on"
    sudo systemctl start yousnr
    sleep 8
  fi
}
trap turn_back_on EXIT

if [ "${1:-}" != "--after-pull" ]; then
  echo "== turning off"
  sudo systemctl stop yousnr || true
  echo "== getting the update"
  git pull --no-rebase --no-edit
  trap - EXIT
  exec bash "$APP/deploy/update.sh" --after-pull "$was_on"     # the rest runs from the version just pulled
fi

echo "== updating Playwright and its browser, yt-dlp and the rest"
pw_before="$("$PY" -m pip show playwright 2>/dev/null | awk '/^Version:/{print $2}')"
"$PY" -m pip install -q -U -r requirements.txt
pw_after="$("$PY" -m pip show playwright 2>/dev/null | awk '/^Version:/{print $2}')"
if [ "$pw_before" != "$pw_after" ]; then
  echo "   Playwright $pw_before -> $pw_after"
  sudo "$PY" -m playwright install-deps chromium >/dev/null \
    || echo "   !! couldn't install the system libraries the new browser needs (see above)"
fi
"$PY" -m playwright install chromium \
  || echo "   !! couldn't download Playwright's Chromium (see above)"   # only downloads when there's a new one
"$PY" - <<'EOF' || true
import json, pathlib, playwright
f = pathlib.Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
b = next(x for x in json.loads(f.read_text())["browsers"] if x["name"] == "chromium")
print(f"   Playwright's Chromium: {b['browserVersion']}")
EOF
bash "$APP/deploy/browser-sandbox.sh" --no-restart || true

echo "== rebuilding pages"
"$PY" yousnr.py build
git add -A data docs
if ! git diff --cached --quiet; then
  git commit -q -m "Rebuild pages after update"
  git push -q
  echo "   published"
fi
turn_back_on
"$PY" yousnr.py status
