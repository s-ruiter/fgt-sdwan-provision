#!/usr/bin/env bash
# One-liner (run as root on a Proxmox VE host):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main/ct/deploy.sh)"
# Optional overrides via environment, e.g.:
#   CTID=210 CT_IP=10.0.0.50/24 CT_GW=10.0.0.1 STORAGE=local-zfs BRIDGE=vmbr1 VLAN=100 bash -c "$(curl ...)"
set -Eeuo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main}"
CT_HOSTNAME="${CT_HOSTNAME:-fgt-sdwan}"
STORAGE="${STORAGE:-local-lvm}"          # rootfs storage
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
CT_IP="${CT_IP:-dhcp}"                   # dhcp or a.b.c.d/nn
CT_GW="${CT_GW:-}"                       # required when CT_IP is static
VLAN="${VLAN:-}"
CORES="${CORES:-1}"
MEMORY="${MEMORY:-1024}"
DISK_GB="${DISK_GB:-6}"

msg() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root on the Proxmox host."
command -v pct >/dev/null && command -v pveam >/dev/null || die "pct/pveam not found - this must run on a Proxmox VE host."
[[ "$CT_IP" == "dhcp" || -n "$CT_GW" ]] || die "Static CT_IP requires CT_GW."

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
pct status "$CTID" >/dev/null 2>&1 && die "CTID $CTID already exists (set CTID=...)."

msg "Looking up Debian 12 template"
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)"
[[ -n "$TEMPLATE" ]] || die "No debian-12-standard template found."
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  msg "Downloading $TEMPLATE to $TEMPLATE_STORAGE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

NET="name=eth0,bridge=${BRIDGE}"
[[ -n "$VLAN" ]] && NET+=",tag=${VLAN}"
if [[ "$CT_IP" == "dhcp" ]]; then NET+=",ip=dhcp"; else NET+=",ip=${CT_IP},gw=${CT_GW}"; fi

ROOTPW="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)"
msg "Creating LXC $CTID ($CT_HOSTNAME)"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$CT_HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap 256 \
  --rootfs "${STORAGE}:${DISK_GB}" --net0 "$NET" \
  --unprivileged 1 --features nesting=0 --onboot 1 --password "$ROOTPW" \
  --description "FGT SD-WAN provisioning tool (fgt-sdwan-provision)"
pct start "$CTID"

msg "Waiting for network in the container"
for _ in $(seq 1 60); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 || die "Container has no working network/DNS (check bridge/VLAN/gateway)."

msg "Installing the application inside the container"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null && curl -fsSL '${REPO_RAW}/ct/install.sh' | bash"

IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo
msg "Done. Open: http://${IP}:8000"
echo "    CT $CTID root password (not stored anywhere else): $ROOTPW"
echo "    Update later:   pct exec $CTID -- bash /opt/fgt-sdwan-provision/ct/install.sh"
