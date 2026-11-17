#!/bin/bash
# Same path as the one-liner inside the CT: curl the installer from GitHub and pipe it to bash (public repo).
set -e
install -m 0755 /root/stub.sh /usr/local/bin/systemctl
printf '#!/bin/sh\ncat /var/log/app.log\n' >/usr/local/bin/journalctl; chmod +x /usr/local/bin/journalctl
apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
curl -fsSL https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main/ct/install.sh | bash
echo "--- git state / token not kept:"; git -C /opt/fgt-sdwan-provision log --oneline | head -1; git -C /opt/fgt-sdwan-provision remote -v | head -1
echo "--- api:"; curl -s localhost:8000/api/about | head -c 120; echo
echo "--- UI served:"; curl -s -o /dev/null -w "%{http_code} index, " localhost:8000/; curl -s -o /dev/null -w "%{http_code} font\n" localhost:8000/fonts/Geist-Variable.woff2
echo "--- update path (git pull) idempotent:"; bash /opt/fgt-sdwan-provision/ct/install.sh | tail -1
