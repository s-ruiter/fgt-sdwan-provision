#!/bin/bash
# Inside a Debian container: fake Proxmox CLI tools that only log their arguments, then run ct/deploy.sh against them.
set -e
mkdir -p /fake && export PATH=/fake:$PATH LOG=/fake/calls.log
: >"$LOG"
cat >/fake/pct <<'EOS'
#!/bin/bash
echo "pct $*" >>"$LOG"
case "$1" in
  status) [ "${CT_EXISTS:-0}" = 1 ] && exit 0 || exit 2 ;;
  exec)   case "$*" in *"hostname -I"*) echo "10.0.0.9 ";; *"getent hosts"*) exit 0;; esac ;;
esac
exit 0
EOS
cat >/fake/pveam <<'EOS'
#!/bin/bash
echo "pveam $*" >>"$LOG"
case "$1" in
  available) printf 'system  debian-11-standard_11.7-1_amd64.tar.zst\nsystem  debian-12-standard_12.2-1_amd64.tar.zst\nsystem  debian-12-standard_12.7-1_amd64.tar.zst\nsystem  debian-13-standard_13.0-1_amd64.tar.zst\n';;
  list) ;;
esac
exit 0
EOS
cat >/fake/pvesh <<'EOS'
#!/bin/bash
echo 105
EOS
cat >/fake/pvesm <<'EOS'
#!/bin/bash
echo "pvesm $*" >>"$LOG"
printf 'Name Type Status Total Used Available %%\n'
case "$*" in
  *rootdir*) printf 'backup dir disabled 1 1 1 1%%\nlocal-lvm lvmthin active 100 1 99 1%%\nlocal dir active 100 1 99 1%%\n';;
  *vztmpl*)  printf 'local dir active 100 1 99 1%%\n';;
esac
EOS
chmod +x /fake/*
cd /repo
echo "=== 1. local code, defaults, --yes"
bash ct/deploy.sh --yes >/tmp/out1 2>&1 || { cat /tmp/out1; exit 1; }
grep -E "Container ID|Storage|Network|Code|Done|password" /tmp/out1
grep -E "^pct (create|start|push)|pveam (download|update)" "$LOG" | sed 's/--password [^ ]*/--password ***/'
echo "--- install step goes through the local tarball:"; grep "pct exec 105" "$LOG" | grep -c "APP_LOCAL=/root/fgt-sdwan-src.tgz" 
echo "=== 2. static IP + VLAN + dns, explicit storage"
: >"$LOG"
CT_IP=10.0.0.50/24 CT_GW=10.0.0.1 CT_DNS=10.0.0.1 VLAN=20 STORAGE=local bash ct/deploy.sh --yes >/tmp/out2 2>&1
grep "pct create" "$LOG" | sed 's/--password [^ ]*/--password ***/'
echo "=== 3. static IP without gateway must fail"
CT_IP=10.0.0.50/24 bash ct/deploy.sh --yes 2>&1 | tail -1 || true
echo "=== 4. update mode"
: >"$LOG"; CT_EXISTS=1 UPDATE=1 CTID=105 bash ct/deploy.sh 2>&1 | tail -1
grep -c "pct create" "$LOG" || true
echo "=== 5. existing CTID refused"
CT_EXISTS=1 CTID=105 bash ct/deploy.sh --yes 2>&1 | tail -1 || true
echo "=== 6. remote mode (no local checkout): token goes via env, not on the command line of curl in clear"
: >"$LOG"; cp ct/deploy.sh /tmp/deploy_only.sh
GITHUB_TOKEN=ghp_TESTTOKEN123 bash /tmp/deploy_only.sh --yes >/tmp/out6 2>&1
grep "Code" /tmp/out6; grep "pct exec 105 -- env" "$LOG" | head -1 | cut -c1-200
