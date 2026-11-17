#!/usr/bin/env bash
# Runs INSIDE the LXC (Debian 12). Idempotent: re-run to update.
set -Eeuo pipefail

APP_REPO="${APP_REPO:-https://github.com/s-ruiter/fgt-sdwan-provision.git}"
APP_REF="${APP_REF:-main}"
APP_DIR=/opt/fgt-sdwan-provision
# Fortinet's Jinja Orchestrator has no license file, so it is cloned at install time
# (pinned to the commit this tool was tested with) instead of being redistributed here.
ORCH_REPO="${ORCH_REPO:-https://github.com/fortinet-solutions-cse/sdwan-advpn-reference.git}"
ORCH_COMMIT="${ORCH_COMMIT:-252f9c1f6cc2c8d5fc87682c046d16468baee123}"
ORCH_DIR=/opt/sdwan-advpn-reference
DATA_DIR=/var/lib/fgt-sdwan          # uploaded KVM images live here

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates >/dev/null

if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch -q origin && git -C "$APP_DIR" checkout -q "$APP_REF" && git -C "$APP_DIR" pull -q --ff-only || true
else
  git clone -q --branch "$APP_REF" "$APP_REPO" "$APP_DIR"
fi

if [[ ! -d "$ORCH_DIR/.git" ]]; then git clone -q "$ORCH_REPO" "$ORCH_DIR"; fi
git -C "$ORCH_DIR" fetch -q origin
git -C "$ORCH_DIR" checkout -q "$ORCH_COMMIT"

install -d -m 0700 "$DATA_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

cat >/etc/systemd/system/fgt-sdwan.service <<EOF
[Unit]
Description=FGT SD-WAN provisioning tool
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR
Environment=ORCH_DIR=$ORCH_DIR
Environment=DATA_DIR=$DATA_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now fgt-sdwan.service
systemctl restart fgt-sdwan.service

for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8000/api/about >/dev/null 2>&1 && { echo "fgt-sdwan is up on :8000"; exit 0; }
  sleep 1
done
journalctl -u fgt-sdwan -n 30 --no-pager
echo "ERROR: service did not become healthy" >&2
exit 1
