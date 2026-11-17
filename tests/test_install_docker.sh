#!/usr/bin/env bash
# Manual test: run ct/install.sh (local-code mode) in a clean Debian 12 container (needs Docker/OrbStack).
set -Eeuo pipefail
cd "$(dirname "$0")/.."
TGZ="$(mktemp -t fgtsrc).tgz"
COPYFILE_DISABLE=1 tar --exclude=.git --exclude=.venv --exclude=data --exclude=__pycache__ -czf "$TGZ" .
docker run --rm -v "$TGZ":/root/src.tgz -v "$PWD/ct/install.sh":/root/install.sh:ro \
  -v "$PWD/tests/docker_stub_systemctl.sh":/root/stub.sh:ro -v "$PWD/tests/docker_inner.sh":/root/inner.sh:ro \
  debian:12 bash /root/inner.sh
rm -f "$TGZ"
