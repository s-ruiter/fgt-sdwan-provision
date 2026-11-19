#!/usr/bin/env bash
# Runs INSIDE the LXC (Debian 12). Idempotent: re-run to update. Data in /var/lib/fgt-sdwan is never touched.
#   APP_LOCAL=<tgz>   install the app from a tarball (private repo / local code)
#   GITHUB_TOKEN=...  read access token when cloning/pulling a private repo
#   PORT=<n>          web port (new install: 80; an update keeps the port of the existing service unless PORT is set)
set -Eeuo pipefail

APP_REPO="${APP_REPO:-https://github.com/s-ruiter/fgt-sdwan-provision.git}"
APP_REF="${APP_REF:-main}"
APP_LOCAL="${APP_LOCAL:-}"
APP_DIR=/opt/fgt-sdwan-provision
# Fortinet's Jinja Orchestrator has no license file, so it is cloned at install time
# (pinned to the commit this tool was tested with) instead of being redistributed here.
ORCH_REPO="${ORCH_REPO:-https://github.com/fortinet-solutions-cse/sdwan-advpn-reference.git}"
ORCH_COMMIT="${ORCH_COMMIT:-252f9c1f6cc2c8d5fc87682c046d16468baee123}"
ORCH_DIR=/opt/sdwan-advpn-reference
DATA_DIR=/var/lib/fgt-sdwan          # uploaded KVM images + saved Proxmox servers live here
UNIT=/etc/systemd/system/fgt-sdwan.service
if [[ -z "${PORT:-}" && -f "$UNIT" ]]; then
  PORT="$(sed -n 's/^ExecStart=.*--port \([0-9][0-9]*\).*/\1/p' "$UNIT" | head -1)"
fi
PORT="${PORT:-80}"
[[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]] || { echo "ERROR: PORT must be 1-65535" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates >/dev/null

if [[ -n "$APP_LOCAL" ]]; then
  install -d "$APP_DIR"
  find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
  tar -xzf "$APP_LOCAL" -C "$APP_DIR"
  rm -f "$APP_LOCAL" || true
else
  url="$APP_REPO"
  [[ -n "${GITHUB_TOKEN:-}" ]] && url="${APP_REPO/https:\/\//https://x-access-token:${GITHUB_TOKEN}@}"
  if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" remote set-url origin "$url"
    git -C "$APP_DIR" fetch -q origin && git -C "$APP_DIR" checkout -q "$APP_REF" && git -C "$APP_DIR" pull -q --ff-only
  else
    git clone -q --branch "$APP_REF" "$url" "$APP_DIR"
  fi
  git -C "$APP_DIR" remote set-url origin "$APP_REPO"      # do not keep the token in .git/config
fi

if [[ ! -d "$ORCH_DIR/.git" ]]; then git clone -q "$ORCH_REPO" "$ORCH_DIR"; fi
git -C "$ORCH_DIR" fetch -q origin
git -C "$ORCH_DIR" checkout -q "$ORCH_COMMIT"

install -d -m 0700 "$DATA_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

cat >"$UNIT" <<EOF
[Unit]
Description=FGT SD-WAN provisioning tool
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR
Environment=ORCH_DIR=$ORCH_DIR
Environment=DATA_DIR=$DATA_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now fgt-sdwan.service
systemctl restart fgt-sdwan.service

for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:${PORT}/api/about" >/dev/null 2>&1 && { echo "fgt-sdwan is up on :${PORT}"; exit 0; }
  sleep 1
done
journalctl -u fgt-sdwan -n 30 --no-pager
echo "ERROR: service did not become healthy" >&2
exit 1
