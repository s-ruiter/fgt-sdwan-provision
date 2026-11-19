"""Tests that deploy the WAN emulator need an uplink, which has no default (it is lab-specific): importing this module
fills it in as a user would (VLAN 106, 10.10.106.250/24 via .1)."""
from app import model

LAB_UPLINK = {"vlan": 106, "ip": "10.10.106.250/24", "gw": "10.10.106.1"}
model.DEFAULTS["wanemu"]["uplink"] = dict(LAB_UPLINK)
