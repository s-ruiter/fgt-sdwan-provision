#!/usr/bin/env bash
# Manual test: ct/deploy.sh against fake pct/pveam/pvesh/pvesm (logs only) in a Debian container. Needs Docker/OrbStack.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD":/repo:ro -v "$PWD/tests/fake_pve_inner.sh":/root/inner.sh:ro debian:12 bash /root/inner.sh
