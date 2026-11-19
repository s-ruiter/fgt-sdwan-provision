#!/bin/sh
# Stand-in for systemctl inside a plain Debian container (no systemd): a "res*" action (re-start) launches uvicorn directly.
case "$1" in
  res*)
    pkill -f "uvicorn app.main" 2>/dev/null || true
    cd /opt/fgt-sdwan-provision
    ORCH_DIR=/opt/sdwan-advpn-reference DATA_DIR=/var/lib/fgt-sdwan nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$(sed -n 's/.*--port \([0-9]*\).*/\1/p' /etc/systemd/system/fgt-sdwan.service)" >/var/log/app.log 2>&1 &
    ;;
esac
exit 0
