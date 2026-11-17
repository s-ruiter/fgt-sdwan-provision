#!/usr/bin/env bash
# Manual test: the public one-liner path of ct/install.sh (curl | bash + git clone) in a clean Debian 12 container.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD/tests/docker_stub_systemctl.sh":/root/stub.sh:ro -v "$PWD/tests/docker_inner_remote.sh":/root/inner.sh:ro debian:12 bash /root/inner.sh
