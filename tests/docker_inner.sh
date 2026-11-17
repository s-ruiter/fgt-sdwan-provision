#!/bin/bash
# Runs inside the Debian 12 container (see test_install_docker.sh).
set -e
install -m 0755 /root/stub.sh /usr/local/bin/systemctl
printf '#!/bin/sh\ncat /var/log/app.log\n' >/usr/local/bin/journalctl; chmod +x /usr/local/bin/journalctl
APP_LOCAL=/root/src.tgz bash /root/install.sh
echo "--- unit file:"; grep -E "Environment|ExecStart" /etc/systemd/system/fgt-sdwan.service
echo "--- data dir:"; stat -c "%a %n" /var/lib/fgt-sdwan
echo "--- about:"; curl -s localhost:8000/api/about | head -c 300; echo
echo "--- render via API:"
/opt/fgt-sdwan-provision/.venv/bin/python - <<'PY'
import json, urllib.request
def call(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8000" + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))
t = call("/api/defaults")
t["dns_primary"] = "1.1.1.1"; t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
t["hubs"][0]["wan1_gw"] = "100.64.1.6"; t["hubs"][1]["wan1_gw"] = "100.64.2.6"
for i, d in enumerate(t["hubs"] + t["edges"]): d["serial"] = "FGVMMLTM2600%04d" % i
print(call("/api/render", {"topology": t})["lines"])
PY
echo "--- second run (update path): data must survive, install must be idempotent"
echo keep > /var/lib/fgt-sdwan/marker
cp /root/src.tgz /root/src2.tgz
APP_LOCAL=/root/src2.tgz bash /root/install.sh | tail -1
cat /var/lib/fgt-sdwan/marker
