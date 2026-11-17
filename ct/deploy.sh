#!/usr/bin/env bash
# Creates the deploy LXC (Debian 12) on a Proxmox VE host and installs the app in it. Run as root on the PVE host.
#
#  A) Private repo / from a checkout (recommended while the repo is private; uses the LOCAL code):
#       scp -r fgt-sdwan-provision root@<pve>:/root/ && ssh root@<pve> bash /root/fgt-sdwan-provision/ct/deploy.sh
#  B) Public repo (one-liner):
#       bash -c "$(curl -fsSL https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main/ct/deploy.sh)"
#  C) Private repo without checkout (needs a GitHub token with read access to the repo):
#       GITHUB_TOKEN=ghp_xxx bash -c "$(curl -fsSL -H "Authorization: token $GITHUB_TOKEN" https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main/ct/deploy.sh)"
#
# Settings via environment (all optional): CTID, CT_HOSTNAME, STORAGE (rootfs), TEMPLATE_STORAGE, BRIDGE, VLAN,
#   CT_IP (dhcp | a.b.c.d/nn), CT_GW, CT_DNS, CORES, MEMORY, DISK_GB, YES=1 (no confirmation),
#   UPDATE=1 with CTID=<existing> (update the app in an existing container instead of creating one).
set -Eeuo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main}"
CT_HOSTNAME="${CT_HOSTNAME:-fgt-sdwan}"
BRIDGE="${BRIDGE:-vmbr0}"
CT_IP="${CT_IP:-dhcp}"
CT_GW="${CT_GW:-}"
CT_DNS="${CT_DNS:-}"
VLAN="${VLAN:-}"
CORES="${CORES:-1}"
MEMORY="${MEMORY:-1024}"
DISK_GB="${DISK_GB:-12}"             # holds uploaded KVM images (zip + extracted qcow2)
UPDATE="${UPDATE:-0}"
YES="${YES:-0}"
[[ "${1:-}" == "--yes" ]] && YES=1

msg() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root on the Proxmox host."
command -v pct >/dev/null && command -v pveam >/dev/null || die "pct/pveam not found - this must run on a Proxmox VE host."

# --- local code? (script run from a checkout: ct/deploy.sh next to ct/install.sh and app/)
LOCAL_SRC=""
SELF="${BASH_SOURCE[0]:-}"
if [[ -n "$SELF" && -f "$SELF" ]]; then
  d="$(cd "$(dirname "$SELF")/.." && pwd)"
  [[ -f "$d/ct/install.sh" && -d "$d/app" ]] && LOCAL_SRC="$d"
fi

# first active storage that supports a content type, e.g. pick_storage rootdir
pick_storage() { pvesm status --content "$1" 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}'; }

install_app() {   # $1 = CTID
  local id="$1"
  if [[ -n "$LOCAL_SRC" ]]; then
    msg "Installing from local code: $LOCAL_SRC"
    local tgz; tgz="$(mktemp /tmp/fgt-sdwan-src.XXXXXX.tgz)"
    tar -C "$LOCAL_SRC" --exclude=.git --exclude=.venv --exclude=data --exclude=__pycache__ -czf "$tgz" . \
      || { rc=$?; [[ $rc -eq 1 ]] || die "tar failed (exit $rc)"; }     # exit 1 = a file changed while reading: harmless
    pct push "$id" "$tgz" /root/fgt-sdwan-src.tgz
    rm -f "$tgz"
    pct push "$id" "$LOCAL_SRC/ct/install.sh" /root/install.sh
    pct exec "$id" -- bash -c "apt-get update -qq && apt-get install -y -qq ca-certificates >/dev/null && APP_LOCAL=/root/fgt-sdwan-src.tgz bash /root/install.sh"
  else
    msg "Installing from GitHub (${REPO_RAW})"
    pct exec "$id" -- env GITHUB_TOKEN="${GITHUB_TOKEN:-}" REPO_RAW="$REPO_RAW" bash -c \
      'apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null && curl -fsSL ${GITHUB_TOKEN:+-H "Authorization: token $GITHUB_TOKEN"} "$REPO_RAW/ct/install.sh" | bash'
  fi
}

# --- update an existing container
if [[ "$UPDATE" == "1" ]]; then
  [[ -n "${CTID:-}" ]] || die "UPDATE=1 needs CTID=<container id>."
  pct status "$CTID" >/dev/null 2>&1 || die "CT $CTID does not exist."
  install_app "$CTID"
  msg "Updated CT $CTID."
  exit 0
fi

# --- new container
[[ "$CT_IP" == "dhcp" || -n "$CT_GW" ]] || die "Static CT_IP requires CT_GW."
STORAGE="${STORAGE:-$(pick_storage rootdir)}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-$(pick_storage vztmpl)}"
[[ -n "$STORAGE" ]] || die "No active storage with content 'rootdir' found; set STORAGE=<name>."
[[ -n "$TEMPLATE_STORAGE" ]] || die "No active storage with content 'vztmpl' found; set TEMPLATE_STORAGE=<name>."
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
pct status "$CTID" >/dev/null 2>&1 && die "CTID $CTID already exists (set CTID=..., or UPDATE=1 to update it)."

echo
echo "  Container ID : $CTID ($CT_HOSTNAME)"
echo "  Storage      : rootfs on $STORAGE (${DISK_GB} GB), template on $TEMPLATE_STORAGE"
echo "  Network      : bridge $BRIDGE${VLAN:+, VLAN $VLAN}, $CT_IP${CT_GW:+ gw $CT_GW}"
echo "  Resources    : $CORES core(s), ${MEMORY} MB RAM"
echo "  Code         : ${LOCAL_SRC:-GitHub ($REPO_RAW)}"
echo
if [[ "$YES" != "1" && -t 0 ]]; then
  read -r -p "Continue? [Y/n] " ans
  [[ "${ans:-y}" =~ ^[Yy] ]] || die "Cancelled."
fi

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

ROOTPW="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20 || true)"
msg "Creating LXC $CTID ($CT_HOSTNAME)"
args=(--hostname "$CT_HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap 256
      --rootfs "${STORAGE}:${DISK_GB}" --net0 "$NET"
      --unprivileged 1 --features nesting=0 --onboot 1 --password "$ROOTPW"
      --description "FGT SD-WAN provisioning tool (fgt-sdwan-provision)")
[[ -n "$CT_DNS" ]] && args+=(--nameserver "$CT_DNS")
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" "${args[@]}"
pct start "$CTID"

msg "Waiting for network in the container"
for _ in $(seq 1 60); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 || die "Container has no working network/DNS (check bridge, VLAN, gateway, CT_DNS)."

install_app "$CTID"

IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo
msg "Done. Open: http://${IP}:8000"
echo "    CT $CTID root password (shown once, not stored): $ROOTPW"
echo "    Update later: UPDATE=1 CTID=$CTID bash <this script>   (data and saved servers are kept)"
