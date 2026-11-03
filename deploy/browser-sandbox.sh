#!/usr/bin/env bash
# Lets YouSNR's browser (Playwright's Chromium) run inside Chromium's sandbox on Ubuntu 23.10 and later.
# Run it on the server as the normal user (ubuntu):
#   bash ~/yousnr/deploy/browser-sandbox.sh
# Safe to run again. oracle-setup.sh and update.sh run it for you.
#
# Why: the sandbox is what keeps a web page that exploits a browser bug shut away from the server's
# files and keys. It needs "user namespaces", and Ubuntu 23.10+ only lets a program create those if an
# AppArmor profile allows it. Google Chrome and the Chromium snap get such a profile from Ubuntu;
# Playwright's own Chromium (in ~/.cache/ms-playwright) doesn't. This adds one for that folder only
# (every Chromium version Playwright downloads there), as Chromium's own docs describe:
# https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$HOME/yousnr-venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
BROWSERS="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
NAME=yousnr-playwright-chromium
AAFILE="/etc/apparmor.d/$NAME"

if [ "$(id -u)" = "0" ]; then
  echo "Run this as your normal user (ubuntu), not root:  bash $0"; exit 1
fi

echo "== browser sandbox"
restrict="$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)"
if [ "$restrict" = "1" ]; then
  command -v apparmor_parser >/dev/null 2>&1 || [ -x /usr/sbin/apparmor_parser ] \
    || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q apparmor
  profile="$(cat <<EOF
# Added by YouSNR (deploy/browser-sandbox.sh): lets Playwright's Chromium create the user
# namespaces its sandbox needs. Remove this file and run  sudo apparmor_parser -R  on it to undo.
abi <abi/4.0>,
include <tunables/global>

profile $NAME "$BROWSERS/chromium*/**" flags=(unconfined) {
  userns,

  include if exists <local/$NAME>
}
EOF
)"
  if [ "$(sudo cat "$AAFILE" 2>/dev/null || true)" != "$profile" ]; then
    printf '%s\n' "$profile" | sudo tee "$AAFILE" >/dev/null
    echo "   wrote $AAFILE"
  fi
  sudo apparmor_parser -r "$AAFILE"
  echo "   AppArmor allows Playwright's Chromium (in $BROWSERS) to sandbox itself"
else
  echo "   this system doesn't restrict user namespaces, so the sandbox needs nothing extra"
fi

# Prove it: start the same browser the service uses, with the sandbox required, and check that a web
# page's process really runs walled off (in its own user namespace).
TEST='
import os, subprocess, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=not os.environ.get("DISPLAY"), chromium_sandbox=True)
    page = b.new_page()
    page.goto("data:text/html,<p>ok</p>")
    me = os.readlink("/proc/self/ns/user")
    ps = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "pid=,args="], capture_output=True, text=True).stdout
    walled = []
    for line in ps.splitlines():
        if "--type=renderer" in line and "chrom" in os.path.basename(line.split()[1]):
            try:
                walled.append(os.readlink("/proc/%s/ns/user" % line.split()[0]) != me)
            except OSError:
                pass
    print("   OK: Chromium %s starts with its sandbox on%s" % (b.version,
          "; web pages run walled off" if walled and all(walled) else ""))
    b.close()
'
run=("$PY" -c "$TEST")
command -v xvfb-run >/dev/null 2>&1 && run=(xvfb-run -a --server-args="-screen 0 1366x900x24" "${run[@]}")
if out="$("${run[@]}" 2>&1)"; then
  printf '%s\n' "$out" | tail -1
  rm -f "$APP/run/browser-sandbox.txt"          # `yousnr status` re-checks the next time the browser opens
else
  printf '%s\n' "$out" | grep -iE "sandbox|namespace|error" | head -5
  echo "   !! Chromium still can't use its sandbox here. YouSNR keeps working without it (status shows OFF)."
  exit 1
fi

if [ "${1:-}" != "--no-restart" ] && systemctl is-active --quiet yousnr 2>/dev/null; then
  echo "== restarting YouSNR so it picks this up"
  sudo systemctl restart yousnr
fi
