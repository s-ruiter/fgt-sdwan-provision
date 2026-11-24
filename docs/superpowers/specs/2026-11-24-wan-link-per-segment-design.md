# One segment (VLAN + subnet) per FortiGate WAN

## Why
Spokes now share one segment per ISP: every spoke wan1 is in ISP-A (100.127.201.0/24, VLAN 3201), every wan2 in
ISP-B. An ADVPN shortcut between two spokes therefore runs directly over that shared layer 2 and never passes the
wan-emu: latency/loss/link down have no effect on spoke-to-spoke traffic (measured: via the hub 204 ms, after the
shortcut 0.8 ms with 100 ms on both WANs of site1-1). Sander chose option A: each FortiGate WAN gets its own VLAN
**and its own subnet**, the wan-emu routes between all of them. It is the most transparent for a demo (every ISP line
is visibly its own network; no proxy-ARP to explain).

## Goals
- All traffic of a WAN (internet, hub tunnels, spoke-to-spoke shortcuts) passes the wan-emu, so the link sliders
  apply to everything.
- A readable address plan (Sander approved):

  | line | subnet | FortiGate | gateway (wan-emu) | VLAN |
  |---|---|---|---|---|
  | hub n wan1 (ISP-HUB) | 100.64.n.0/24 | 100.64.n.2 (static) | 100.64.n.1 | 3200+n |
  | spoke n wan1 (ISP-A) | 100.65.n.0/24 | 100.65.n.2 (DHCP reservation) | 100.65.n.1 | 3210+n |
  | spoke n wan2 (ISP-B) | 100.66.n.0/24 | 100.66.n.2 (DHCP reservation) | 100.66.n.1 | 3230+n |

  n = 1-based position of the hub/spoke in the topology. Second octet = ISP, third octet = device number.
- The topology diagram on the Deploy page shows the new plan.

## Non-goals
- No change to the SD-WAN/overlay design, the templates' logic, or the LAN VLANs (3300+n / 3400+n).
- No in-place migration of a running deployment: the new layout needs a new deployment (wan-emu and FortiGate NICs
  change).

## Approaches considered
- **A. Own VLAN + own subnet per WAN (chosen)**: transparent, every line visibly routed by the wan-emu; addresses change.
- B. Own VLAN per WAN, same subnets, wan-emu proxy-ARP: no address change, but a hidden trick to explain in a demo.
- C. Proxmox bridge port isolation: needs host config outside the API; dropped.

## Design
**Model / form**
- `wanemu.segments.{hub,a,b}` becomes per ISP: `name`, `block` (default `100.64.0.0/16`, `100.65.0.0/16`,
  `100.66.0.0/16`) and `vlan_base` (3200, 3210, 3230). Link n of that ISP: subnet = n-th /24 of the block
  (`block.network_address + n*256`, /24), gateway .1, FortiGate .2, VLAN `vlan_base + n`.
- Hub wan1 IP and gateway are derived (no octet field; `wan1_ip`/`wan1_gw` are filled by `normalize_wans`); the hub
  card shows them read-only.
- `res_start`/`pool` disappear (every spoke WAN is the .2 of its own /24 via a DHCP reservation on its MAC; a small
  pool .100-.199 per line stays for anything else).
- Validation: block is a valid IPv4 network of prefix ≤ /23 holding enough /24s (n ≤ number of /24s − 1); blocks
  don't overlap each other, the uplink subnet, or LAN subnets; VLAN ranges `vlan_base+1 .. vlan_base+count` stay inside
  1-4094 and do not overlap each other, the uplink VLAN or the LAN VLAN ranges; total WAN links ≤ 30 (wan-emu NICs:
  net0 uplink + net1..net30).
- Saved settings / import of an old file with `subnet`/`gw`/`vlan` per segment: replaced by the new defaults, with a
  note in the import result ("WAN emulation now uses one subnet per WAN: ISP blocks reset to the defaults").

**wan-emu**
- One NIC per link: `netK` (K = 1..), `name=wl<K>` (or ethK), bridge = WAN-emulation bridge, `tag=<link VLAN>`,
  `ip=<gw>/24`. eth0 stays the uplink.
- dnsmasq: per spoke link interface a dhcp-range in that /24 (pool .100-.199), `dhcp-host=<mac>,<.2>`, router = .1,
  DNS as today. Hub links: no DHCP (static).
- Agent: a link = an interface. Shaping per interface: egress netem (+ htb rate) on the link NIC = towards the
  FortiGate, ingress mirred to `ifb-<nic>` = from the FortiGate. No u32 IP filters any more. "link down" stays nftables
  (drop on that interface).
- NAT: masquerade towards eth0 for the three ISP blocks (not for the lab subnet), as today. Port forwards to each
  FortiGate's wan1 address unchanged in principle (address = the new .2).
- IP forwarding between all link NICs (routing between the ISP lines). No NAT between links.

**FortiGate VMs / Day-0**
- `fgt_nics`: wan ports get `tag=<link VLAN>` (fixed MAC as today).
- Day-0 static hub wan1 = derived 100.64.n.2/24 + gateway 100.64.n.1; branches DHCP as today.

**FortiManager**
- `fmg_routes`: one route per ISP block (3 routes) via the wan-emu uplink address (instead of three /24s).

**Templates**
- Hub overlay `wan_ip` = derived hub address (no other template change). Check that nothing else hard-codes
  100.127.x (health-check servers use loopbacks/hostnames).

**Proxmox bridge checks**
- `bridge_needs` lists every link VLAN on the WAN-emulation bridge (`bridge-vids` must contain them; the existing
  pre-check reports missing ones).

**Topology diagram (Deploy page)**
- One horizontal line per ISP labelled `ISP-A · 100.65.0.0/16 · VLAN 3211-3213` (actual range for this topology).
- Each FortiGate drop carries its own label `100.65.1.2 · VLAN 3211` (wan2 likewise); hubs `100.64.1.2 · VLAN 3201`.

**Access & WAN control**
- Link cards keep their id (`site1-1-wan1`) and show the new address; the "ISP" columns stay (HUB / A / B).

## Tests
- Model: defaults, derivation per link (hub 1 → 100.64.1.2/24 gw .1 VLAN 3201; spoke 2 wan2 → 100.66.2.2 VLAN 3232),
  validations (overlap, too many links, VLAN clash, bad block), old settings import → defaults + note.
- net: links/dnsmasq/ct_params/fmg_routes/forwards/bridge_needs for the default topology.
- agent: tc commands per interface (no u32), nft drop per interface, NAT for the blocks; existing agent tests adjusted.
- Templates: hub `wan_ip` 100.64.n.2.
- UI static: no octet field, diagram labels.
- Lab check: after a new deployment, client site1-1 → client site1-3 with 100 ms on site1-1 wan1+wan2 keeps ~200 ms
  after the shortcut is up; WAN sliders still work for internet traffic.

## Calls made after the builder check (author's calls, check them)
- Port forwards (Access GUI/SSH): the existing `ct status dnat oifname { <link NICs> } masquerade` stays; masquerade
  takes the address of the outgoing link NIC, so the source is automatically the .1 of the target FortiGate's line.
- Link ids (`<device>-wan1/2`), the agent API and the Access & WAN control cards stay the same; only address/VLAN change.
- A hub has exactly one WAN (the model already enforces it): one link per hub.
- Re-deploy over an existing deployment: when the tool reuses an existing wan-emu CT or FortiGate VM, it rewrites
  their NIC config (`netK` with the new VLAN/IP) instead of leaving the old layout; the wan-emu is re-provisioned
  (dnsmasq, agent topology). If a reused VM's NICs cannot be changed, the deployment stops with a message to run
  Cleanup first.
- Proxmox CT NIC limit: verified in the implementation (spike on PVE01: net0..net31); the cap and its message follow
  what Proxmox accepts. CT NICs get `firewall=0` and a fixed MAC (stable naming).
- Lab acceptance: latency AND link down on a spoke-to-spoke shortcut (ping keeps ~200 ms with 100 ms on both WANs;
  with wan1+wan2 of site1-1 down the shortcut stops).

## Left to the builder
- NIC naming inside the CT (`ethK` vs `wlK`), exact column/label layout in the diagram.
- Whether the htb root stays (needed for rate) or netem alone is used when no rate is set.
