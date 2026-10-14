#!/usr/bin/env bash
# YouSNR on a cloud server (Oracle Cloud Always Free, Ubuntu 22.04 or 24.04, ARM or x86).
# Run it as the normal user (ubuntu), not as root. Safe to run again.
#
#   git clone https://github.com/UmarPatel77/yousnr.git ~/yousnr
#   bash ~/yousnr/deploy/oracle-setup.sh
#
# What it sets up:
#   - Python packages, a browser (Chromium) and a virtual screen for it (Xvfb)
#   - Deno (yt-dlp needs it for YouTube), ffmpeg, cloudflared (the tunnel to api.yousnr.com)
#   - Cloudflare WARP in proxy mode: only YouTube traffic goes through it, because YouTube blocks
#     data-centre addresses. SSH and everything else stay on the normal connection.
#   - ~/.config/yousnr.env for your API keys (only you can read it; it's outside the project)
#   - a GitHub deploy key so the server can publish new pages to just this repo
#   - a systemd service so YouSNR runs all the time and comes back after a reboot
set -euo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/yousnr-venv"
ENVF="$HOME/.config/yousnr.env"
WARP_PORT=40000
PY="$VENV/bin/python"

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

if [ "$(id -u)" = "0" ]; then
  echo "Run this as your normal user (ubuntu), not root:  bash $0"; exit 1
fi
. /etc/os-release
case "${VERSION_ID:-}" in 22.04|24.04|26.04) ;; *) warn "Made for Ubuntu 22.04/24.04; this is ${PRETTY_NAME:-unknown}. Trying anyway." ;; esac

say "1/8  System packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3-venv python3-pip git openssh-client ffmpeg xvfb xauth curl gpg lsb-release unzip ca-certificates
sudo timedatectl set-timezone America/Toronto 2>/dev/null || true   # log times and the monthly budget in your time

say "2/8  Python packages and the browser"
[ -x "$PY" ] || python3 -m venv "$VENV"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$APP/requirements.txt"
sudo "$PY" -m playwright install-deps chromium
"$PY" -m playwright install chromium

say "3/8  Deno (yt-dlp uses it to read YouTube)"
if ! command -v deno >/dev/null 2>&1; then
  case "$(uname -m)" in aarch64|arm64) target=aarch64-unknown-linux-gnu ;; *) target=x86_64-unknown-linux-gnu ;; esac
  curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-$target.zip"
  sudo unzip -o -q /tmp/deno.zip -d /usr/local/bin
  sudo chmod 755 /usr/local/bin/deno
fi
deno --version | head -1

say "4/8  cloudflared (the tunnel to api.yousnr.com)"
if ! command -v cloudflared >/dev/null 2>&1; then
  arch="$(dpkg --print-architecture)"                     # arm64 on Oracle's Ampere servers
  curl -fsSL -o /tmp/cloudflared.deb \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch.deb"
  sudo dpkg -i /tmp/cloudflared.deb
fi
cloudflared --version

say "5/8  Cloudflare WARP, in proxy mode (YouTube traffic only)"
if ! command -v warp-cli >/dev/null 2>&1; then
  curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
    | sudo gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
    | sudo tee /etc/apt/sources.list.d/cloudflare-client.list >/dev/null
  sudo apt-get update -y
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y cloudflare-warp
fi
sudo systemctl enable --now warp-svc >/dev/null 2>&1 || true
sleep 3
# Proxy mode is set BEFORE connecting, and it only connects if that worked: full WARP mode would
# send SSH through WARP too and could lock you out of the server.
warp_ready=""
if { warp-cli --accept-tos registration show >/dev/null 2>&1 || warp-cli --accept-tos registration new; } \
   && warp-cli --accept-tos mode proxy && warp-cli --accept-tos proxy port "$WARP_PORT"; then
  warp_ready=1
  warp-cli --accept-tos connect || warn "warp-cli connect failed"
else
  warn "Couldn't put WARP in proxy mode, so it was left disconnected. Check: warp-cli status"
fi
ok=""
for _ in $(seq 1 20); do
  [ -n "$warp_ready" ] || break
  if curl -s --max-time 8 -x "socks5h://127.0.0.1:$WARP_PORT" https://www.cloudflare.com/cdn-cgi/trace | grep -q '^warp=on'; then
    ok=1; break
  fi
  sleep 3
done
if [ -n "$ok" ]; then
  echo "WARP proxy is up on 127.0.0.1:$WARP_PORT"
else
  warn "WARP didn't come up yet. Check with: warp-cli status   (then run this script again)"
fi

say "6/8  Settings file for your API keys: $ENVF"
mkdir -p "$(dirname "$ENVF")"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<EOF
# YouSNR settings for this server. Only you can read this file, and it's outside the project,
# so it never gets pushed to GitHub. Paste each key right after its = sign (no spaces, no quotes).
ELEVENLABS_API_KEY=
ASSEMBLYAI_API_KEY=
YOUSNR_MONTHLY_BUDGET=5
# Send YouTube traffic through Cloudflare WARP (blank = go direct)
YOUSNR_PROXY=socks5://127.0.0.1:$WARP_PORT
EOF
fi
chmod 600 "$ENVF"

say "7/8  GitHub: a key that can push to this one repo (and nothing else)"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
[ -f "$HOME/.ssh/yousnr_deploy" ] || ssh-keygen -t ed25519 -N "" -C "yousnr-server" -f "$HOME/.ssh/yousnr_deploy" -q
touch "$HOME/.ssh/known_hosts" "$HOME/.ssh/config"
grep -q "^github.com " "$HOME/.ssh/known_hosts" || ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null \
  || warn "couldn't reach github.com over SSH to save its key; publishing will ask about it later"
if ! grep -q "yousnr_deploy" "$HOME/.ssh/config"; then
  printf '\nHost github.com\n  IdentityFile ~/.ssh/yousnr_deploy\n  IdentitiesOnly yes\n' >> "$HOME/.ssh/config"
fi
chmod 600 "$HOME/.ssh/config"
cd "$APP"
origin="$(git remote get-url origin)"
ssh_url="$(printf '%s' "$origin" | sed -E 's#^https://github.com/([^/]+)/([^/.]+)(\.git)?/?$#git@github.com:\1/\2.git#')"
git remote set-url origin "$ssh_url"
git config user.name  "YouSNR server"
git config user.email "yousnr-server@users.noreply.github.com"
git config pull.rebase false
mkdir -p "$APP/data"

say "8/8  Always on: the yousnr service"
sudo tee /etc/systemd/system/yousnr.service >/dev/null <<EOF
[Unit]
Description=YouSNR transcript server
Wants=network-online.target
After=network-online.target warp-svc.service

[Service]
User=$USER
WorkingDirectory=$APP
EnvironmentFile=$ENVF
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1366x900x24" $PY $APP/yousnr.py serve --log
Restart=always
RestartSec=10
# exit code 4 = turned off on purpose (yousnr stop): do not restart
SuccessExitStatus=4
RestartPreventExitStatus=4
TimeoutStopSec=200

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
# A handy alias: `yousnr status` instead of the full path
grep -q "alias yousnr=" "$HOME/.bashrc" || echo "alias yousnr='cd $APP && $PY $APP/yousnr.py'" >> "$HOME/.bashrc"

say "Can this server get transcripts from YouTube through WARP?"
set +e
xvfb-run -a --server-args="-screen 0 1366x900x24" "$PY" "$APP/yousnr.py" check-youtube
check=$?
set -e

say "Set up. A few things only you can do:"
cat <<EOF

 1. Let the server publish pages to GitHub. Open
      https://github.com/$(printf '%s' "$ssh_url" | sed -E 's#git@github.com:(.*)\.git#\1#')/settings/keys/new
    Title: yousnr-server   Key: the line below   Tick "Allow write access"   Add key

$(cat "$HOME/.ssh/yousnr_deploy.pub")

 2. Your API keys:   nano $ENVF
    Paste each key after its = sign. Save: Ctrl+O, Enter. Exit: Ctrl+X.

 3. From your laptop, copy over the tunnel token and this month's spending (see the README).

 4. Turn it on (and on at every reboot):
      source ~/.bashrc
      yousnr autostart on
      yousnr status
    ("yousnr" is a shortcut this script added for:  cd $APP && $PY yousnr.py)
EOF
[ "$check" = "0" ] || warn "YouTube check didn't fully pass. See above; the README has what to try."
