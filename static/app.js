"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
let T = null, IMGS = [], STEPS = [], ES = null, CUR_JOB = null;

// ---- helpers ----------------------------------------------------------------
const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
async function json(r) { try { return await r.json(); } catch (_) { return {}; } }
function errList(j) { const e = j.detail || j.errors || []; return (Array.isArray(e) ? e : [e]).map((x) => x.msg || String(x)); }
function showErr(list, target = "#errors") {
  let note = "";
  if (list.length && target === "#errors" && document.body.dataset.mode === "simple" && typeof modeSet === "function") {
    modeSet("advanced");                       // the field a message is about may only be shown in Advanced
    note = '<div class="hint">Switched to Advanced: some of these fields are only shown there.</div>';
  }
  $(target).innerHTML = list.length ? `<div class="error">${list.map((x) => "✕ " + esc(x)).join("<br>")}</div>${note}` : "";
  if (list.length) window.scrollTo({ top: 0, behavior: "smooth" });
}
const fld = (k, l, v, extra = "", cls = "") => `<div class="f${cls ? " " + cls : ""}"><label>${l}</label><input data-f="${k}" value="${esc(v)}" ${extra}></div>`;
const PORTS = ["port1", "port2", "port3", "port4", "port5", "port6"];          // FortiGate-VM ports the tool uses
const portOpts = (cur, none) => [...(none ? [["", none]] : []), ...[...new Set([...PORTS, ...(cur ? [cur] : [])])].map((p) => [p, p])]
  .map(([v, t]) => `<option value="${esc(v)}"${v === (cur || "") ? " selected" : ""}>${esc(t)}</option>`).join("");
const pfld = (k, l, v, none, cls = "") => `<div class="f ${cls}"><label>${l}</label><select data-f="${k}">${portOpts(v, none)}</select></div>`;
const fmtBytes = (n) => (n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB");

// ---- routing ----------------------------------------------------------------
function route() {
  const [r, arg] = (location.hash.replace(/^#\//, "") || "deploy").split("?")[0].split("/");
  const VIEWS = ["deploy", "progress", "wanemu", "dashboard", "traffic", "fortiflex", "images", "servers", "cleanup"];
  const view = VIEWS.includes(r) ? r : "deploy";
  for (const v of VIEWS) $("#v_" + v).hidden = v !== view;
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === view));
  window.scrollTo(0, 0);
  if (view === "progress") progressOpen(arg);
  if (view === "deploy") { bannerLoad(); flexApply(); }
  if (view === "fortiflex") { ffLoad(); licLoad(); }
  if (view === "cleanup") clLoad();
  clearInterval(WAN_TIMER); WAN_TIMER = null;
  if (view === "wanemu") { wanLoad(); wanFwdLoad(); WAN_TIMER = setInterval(() => { if (!document.querySelector("#v_wanemu .dirty")) wanLoad(); }, 10000); }
  clearInterval(DASH_TIMER); DASH_TIMER = null;
  if (view === "dashboard" && typeof dashLoad === "function") {   // refresh while open, never while a slider is held
    dashLoad(); DASH_TIMER = setInterval(() => { if (!document.querySelector("#v_dashboard .dirty")) dashLoad(); }, 10000);
  } else if (document.body.classList.contains("dash-full")) {     // leaving the dashboard: never keep the full-screen look
    document.body.classList.remove("dash-full");
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }
  clearInterval(TR_TIMER); TR_TIMER = null;
  if (view === "traffic") { trOpen(); TR_TIMER = setInterval(trPoll, 2000); }
}
window.addEventListener("hashchange", route);
let WAN_TIMER = null, DASH_TIMER = null, WAN = { links: [], presets: {} }, FLEX = { enabled: false };

// ---- IP format checks ---------------------------------------------------------
// kinds: "ip" = a.b.c.d, "cidr" = a.b.c.d/nn (host address + prefix). Empty is allowed here; required-ness is checked by the server.
const OCT = "(25[0-5]|2[0-4]\\d|1?\\d?\\d)";
const RE_IP = new RegExp(`^${OCT}(\\.${OCT}){3}$`);
const RE_CIDR = new RegExp(`^${OCT}(\\.${OCT}){3}/(3[0-2]|[12]?\\d)$`);
const ipNum = (s) => s.split(".").reduce((a, o) => a * 256 + +o, 0);
function maskToPrefix(m) {                     // 255.255.255.0 -> 24 (null if not a contiguous mask)
  if (!RE_IP.test(m)) return null;
  const bits = ipNum(m).toString(2).padStart(32, "0");
  return /^1*0*$/.test(bits) ? bits.indexOf("0") === -1 ? 32 : bits.indexOf("0") : null;
}
function ipProblem(kind, v) {
  if (!v) return kind === "octet" ? "Fill in the last octet (1-254)" : "";
  if (kind === "octet") return /^\d+$/.test(v) && +v >= 1 && +v <= 254 ? "" : "Last octet: a number 1-254";
  if (kind === "ip" || (kind === "ipcidr" && !v.includes("/"))) return RE_IP.test(v) ? "" : "Not a valid IPv4 address (a.b.c.d)";
  if (!RE_CIDR.test(v)) return "Use a.b.c.d/nn, for example 10.0.0.5/24";
  const [a, p] = v.split("/"), pre = +p;
  if (pre <= 30) {
    const size = 2 ** (32 - pre), net = Math.floor(ipNum(a) / size) * size;
    if (ipNum(a) === net) return "This is the network address, not a host address";
    if (ipNum(a) === net + size - 1) return "This is the broadcast address, not a host address";
  }
  return "";
}
const KIND_BY_ID = { fm_ip: "cidr", fm_gw: "ip", tg_ip: "cidr", "wan-uplink-ip": "cidr", "wan-uplink-gw": "ip" };
const KIND_BY_FIELD = { wan1_ip: "cidr", wan1_gw: "ip", loopback: "ip" };
function markIpFields() {
  for (const [id, k] of Object.entries(KIND_BY_ID)) $("#" + id).dataset.ip = k;
  if (["fgt", "tpl", "dev"].includes(scope())) $("#fm_ip").dataset.ip = "ipcidr";   // existing FortiManager: only its address is used
  document.querySelectorAll(".dev [data-f]").forEach((el) => {
    const f = el.dataset.f, k = KIND_BY_FIELD[f] || (/^lan\d+_ip$/.test(f) ? "cidr" : null);
    if (k) el.dataset.ip = k;
  });
  prefixFields();
}
// a.b.c.d/nn -> [address, prefix]; a mask (255.255.255.0) counts as its prefix; no or an invalid prefix -> [text, null]
function splitCidr(v) {
  const s = String(v || "").trim(), i = s.lastIndexOf("/");
  if (i < 0) return [s, null];
  const a = s.slice(0, i), p = s.slice(i + 1);
  if (/^\d{1,2}$/.test(p) && +p <= 32) return [a, +p];
  const m = maskToPrefix(p);
  return m === null ? [s, null] : [a, m];
}
// IP/prefix fields: the address in the field, the prefix in a dropdown next to it (default /24). The field's .value
// stays "a.b.c.d/nn" for all other code (collect, checks, gateway); an existing FortiManager ("ipcidr") only uses the address.
const NATIVE_VALUE = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
function prefixFields() {
  document.querySelectorAll('input[data-ip="cidr"], input[data-ip="ipcidr"]').forEach((el) => {
    if (el.dataset.pfx || el.disabled) return;
    el.dataset.pfx = "1";
    const sel = document.createElement("select");
    sel.className = "pfx" + (el.id === "fm_ip" ? " only-fmg" : "");
    sel.title = "prefix (subnet mask)";
    for (let n = 32; n >= 8; n--) sel.add(new Option("/" + n, String(n)));
    sel.value = "24";
    const wrap = document.createElement("span");
    wrap.className = "pfx-wrap";
    el.replaceWith(wrap); wrap.append(el, sel);
    const put = (v) => {                                   // "a.b.c.d/nn" or an address: split over field and dropdown
      const [a, n] = splitCidr(v);
      if (n !== null && n < 8) { NATIVE_VALUE.set.call(el, String(v || "").trim()); return; }   // no such option: leave it for the check
      NATIVE_VALUE.set.call(el, a);
      if (n !== null) sel.value = String(n); else if (!a) sel.value = "24";
    };
    const raw = NATIVE_VALUE.get.call(el);
    Object.defineProperty(el, "value", {
      configurable: true,
      get() {
        const a = NATIVE_VALUE.get.call(el).trim();
        return !a || el.dataset.ip === "ipcidr" || a.includes("/") ? a : `${a}/${sel.value}`;
      },
      set: put,
    });
    put(raw);
    el.placeholder = (el.placeholder || "").replace(/\/\d+$/, "");
    el.addEventListener("change", () => {                  // typed or pasted a/nn: split when leaving the field
      if (!NATIVE_VALUE.get.call(el).includes("/")) return;
      put(NATIVE_VALUE.get.call(el)); el.dispatchEvent(new Event("input", { bubbles: true }));
    });
    sel.addEventListener("change", () => { el.dispatchEvent(new Event("input", { bubbles: true })); if (el.classList.contains("bad") || NATIVE_VALUE.get.call(el)) checkIp(el); });
  });
}
function checkIp(el) {
  let v = el.value.trim();
  const m = v.match(/^(.+)\/(\d+\.\d+\.\d+\.\d+)$/);   // 10.0.0.5/255.255.255.0 -> 10.0.0.5/24
  if (el.dataset.ip === "cidr" && m && maskToPrefix(m[2]) !== null) { v = `${m[1]}/${maskToPrefix(m[2])}`; el.value = v; }
  let p = ipProblem(el.dataset.ip, v);
  if (p.startsWith("Use a.b.c.d/nn") && el.dataset.pfx) p = "Not a valid IPv4 address (a.b.c.d)";   // the prefix is the dropdown
  el.classList.toggle("bad", !!p); el.title = p;
  return p;
}
document.addEventListener("input", (e) => { if (e.target.dataset && e.target.dataset.ip && e.target.classList.contains("bad")) checkIp(e.target); });
document.addEventListener("focusout", (e) => { if (e.target.dataset && e.target.dataset.ip) checkIp(e.target); });
// visible label text only (labels hold a variant per deploy choice; textContent would glue them together)
function labelText(el) {
  const l = (el.closest(".f") || el.parentElement).querySelector("label");
  if (!l) return el.id;
  return [...l.childNodes].filter((n) => n.nodeType === 3 || n.offsetParent !== null).map((n) => n.textContent).join("").trim();
}
// static WAN fields only matter when the device uses a static wan1
function ipErrors() {
  const out = [];
  document.querySelectorAll("[data-ip]").forEach((el) => {
    const dev = el.closest(".dev");
    if (el.closest(".only-vm, .only-fmg, .only-fgt, .only-dev, .not-lan") && !el.offsetParent) { el.classList.remove("bad"); return; }   // hidden by the deploy choice
    const p = checkIp(el);
    if (p) out.push(`${dev ? dev.querySelector("b").textContent + ": " : ""}${labelText(el)} — ${p}`);
  });
  return out;
}

// ---- topology form ----------------------------------------------------------
let PVE_OPT = { nodes: [], image_storages: [], import_storages: [], iso_storages: [], bridges: [] };
const names = (l) => (l || []).map((x) => (typeof x === "string" ? x : x.name));
// fill a <select>: keep `cur` (even when the server did not list it), else fall back to the first entry
function setSel(id, values, cur, blank) {
  const vals = [...values]; if (cur && !vals.includes(cur)) vals.unshift(cur);
  $(id).innerHTML = (blank ? `<option value="">${blank}</option>` : "") + vals.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  $(id).value = cur || (blank ? "" : vals[0] || "");
}
function pveFill(p) {
  setSel("#pve_node", PVE_OPT.nodes, p.node);
  setSel("#pve_dstore", names(PVE_OPT.image_storages), p.disk_storage);
  setSel("#pve_istore", names(PVE_OPT.import_storages), p.import_storage);
  setSel("#pve_isostore", names(PVE_OPT.iso_storages), p.iso_storage);
  setSel("#pve_br", PVE_OPT.bridges, p.default_bridge);
  setSel("#fmg_br", PVE_OPT.bridges, p.fmg.bridge, "(default bridge)");
  $("#pve_cpu").value = p.cpu_type; $("#pve_first").value = p.first_vmid ?? ""; $("#pve_prefix").value = p.name_prefix ?? ""; prefixHint();
  $("#fgt_cores").value = p.fgt.cores; $("#fgt_mem").value = p.fgt.memory_mb;
  $("#fmg_cores").value = p.fmg.cores; $("#fmg_mem").value = p.fmg.memory_mb; $("#fmg_disk").value = p.fmg.data_disk_gb; $("#fmg_vlan").value = p.fmg.vlan ?? "";
  const opt = (v) => `<option value="">(default bridge)</option>` + [...new Set([...PVE_OPT.bridges, ...(v ? [v] : [])])].map((x) => `<option${x === v ? " selected" : ""}>${esc(x)}</option>`).join("");
  $("#ports").innerHTML = "<thead><tr><th>Port</th><th>Bridge</th><th>VLAN</th><th>Set by the tool</th></tr></thead><tbody>" +
    Object.entries(p.ports).map(([k, v]) => `<tr data-p="${k}"><td class="mono">${k}</td><td><select data-f="bridge">${opt(v.bridge)}</select></td><td><input data-f="vlan" type="number" value="${v.vlan ?? ""}"></td><td class="hint" data-fix></td></tr>`).join("") + "</tbody>";
  portsFixed();
  if (T && T.wanemu) {                                                                                   // bridges of this node
    setSel("#wan-bridge", PVE_OPT.bridges, $("#wan-bridge").value || T.wanemu.bridge);
    setSel("#wan-uplink-bridge", PVE_OPT.bridges, $("#wan-uplink-bridge").value || T.wanemu.uplink.bridge || "", "(same as WAN emulator bridge)");
  }
}
// Proxmox names of this deployment: prefix (a "-" is added) + name; FortiGate hostnames stay as they are
function prefixHint() {
  const raw = $("#pve_prefix").value.trim(), pre = raw && !raw.endsWith("-") ? raw + "-" : raw, ok = !raw || /^[A-Za-z0-9][A-Za-z0-9-]{0,20}$/.test(pre);
  $("#pve_prefix").classList.toggle("bad", !ok);
  const hub = (T && T.hubs && T.hubs[0] && T.hubs[0].name) || "site1-H1";
  $("#pve_prefix_hint").textContent = !raw ? "" : ok ? `e.g. ${pre}${hub}, ${pre}wan-emu` : "Only letters, digits and '-' (max 20)";
}
$("#pve_prefix").addEventListener("input", prefixHint);
const WAN_SEGS = ["hub", "a", "b"];
function wanFill(w) {
  $("#wan-vmid").value = w.vmid ?? ""; $("#wan-storage").value = w.storage;
  setSel("#wan-bridge", PVE_OPT.bridges, w.bridge);
  setSel("#wan-uplink-bridge", PVE_OPT.bridges, w.uplink.bridge || "", "(same as WAN emulator bridge)");
  $("#wan-uplink-vlan").value = w.uplink.vlan ?? ""; $("#wan-uplink-ip").value = w.uplink.ip; $("#wan-uplink-gw").value = w.uplink.gw;
  WAN_SEGS.forEach((k) => { const g = w.segments[k]; $(`#wan-seg-${k}-block`).value = g.block; $(`#wan-seg-${k}-vlanbase`).value = g.vlan_base; });
  segPerDraw();
}
function wanCollect() {
  const w = JSON.parse(JSON.stringify(T.wanemu)), n = (v) => (v === "" ? null : +v);
  w.vmid = n($("#wan-vmid").value); w.storage = $("#wan-storage").value.trim();
  w.bridge = $("#wan-bridge").value.trim();
  w.uplink = { bridge: $("#wan-uplink-bridge").value.trim(), vlan: n($("#wan-uplink-vlan").value), ip: $("#wan-uplink-ip").value.trim(), gw: $("#wan-uplink-gw").value.trim() };
  WAN_SEGS.forEach((k) => { w.segments[k] = { name: w.segments[k].name, block: $(`#wan-seg-${k}-block`).value.trim(), vlan_base: n($(`#wan-seg-${k}-vlanbase`).value) }; });
  return w;
}
function dev(kind, d, i) {
  const hub = kind === "hubs";
  let h = `<div class="dev" data-k="${kind}" data-i="${i}"><b>${esc(d.name)}</b><div class="fields">`;
  // same order on hub and branch cards, so their columns line up: identity first, then WAN, then LAN
  h += fld("name", "Name", d.name) + fld("serial", "Serial number", d.serial, "", "only-dev") +
    `<div class="f only-fgt flex-only"><label>FortiFlex token</label><input data-tok="${esc(d.name)}" autocomplete="off" spellcheck="false"></div>` +
    fld("loopback", "Loopback (BGP, router-id)", d.loopback);
  // WAN design: hub = one wan (wan1, always static); branch = wan1 + optional wan2, both DHCP, each with a tunnel to wan1 of every hub
  if (hub) {                                   // wan1 = .2 of its own /24 in ISP-HUB, gateway = wan-emu (.1): derived
    h += pfld("wan1_intf", "wan1 port (ZTP)", d.wan1_intf) +
      `<div class="f"><label>wan1 IP (own /24, ISP-HUB)</label><input data-hubaddr="${i}" class="mono" disabled></div>` +
      `<div class="f"><label>wan1 gateway (wan-emu)</label><input data-hubgw="${i}" class="mono" disabled></div>`;
  } else h += pfld("wan1_intf", "wan1 port (ZTP, DHCP)", d.wan1_intf) + pfld("wan2_intf", "wan2 port (DHCP)", d.wan2_intf, "(none)");
  // single-VRF: one LAN; multi-VRF: one LAN per VRF (LAN2+ optional: port "(none)" = no LAN in that VRF)
  const vrfs = T.region.multi_vrf === false ? [null] : T.region.vrfs.length ? T.region.vrfs : [null];
  vrfs.forEach((v, j) => {
    const l = (d.lans || [])[j] || {}, tag = v ? ` (VRF ${v.id})` : "";
    h += pfld("lan" + j + "_intf", `LAN${j + 1} port${tag}`, l.intf, j ? "(none)" : "", j ? "" : "lan-first") + fld("lan" + j + "_ip", `LAN${j + 1} IP${tag}`, l.ip);
  });
  return h + "</div></div>";
}
// one VLAN + /24 per FortiGate WAN (mirror of model.wan_links): hub n wan1 -> ISP-HUB, spoke n wan1 -> ISP-A, wan2 ->
// ISP-B; link n = n-th /24 of the block (.1 wan-emu, .2 FortiGate), VLAN vlan_base+n
function linkNet(block, n) {
  const m = String(block || "").trim().match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/);
  if (!m || +m[5] > 23) return null;
  const base = ((+m[1] << 24) >>> 0) + (+m[2] << 16) + (+m[3] << 8) + +m[4], net = base + n * 256;
  if (n >= 2 ** (24 - +m[5])) return null;
  const ip = (x) => [x >>> 24, (x >>> 16) & 255, (x >>> 8) & 255, x & 255].join(".");
  return { subnet: ip(net) + "/24", gw: ip(net + 1), ip: ip(net + 2) };
}
function wanLinks(t) {
  const seg = t.wanemu.segments, out = [];
  const add = (name, s, n, w) => { const x = linkNet(seg[s].block, n); if (x) out.push({ id: `${name}-${w}`, name, seg: s, n, vlan: seg[s].vlan_base + n, ...x }); };
  t.hubs.forEach((h, i) => add(h.name, "hub", i + 1, "wan1"));
  t.edges.forEach((e, i) => { add(e.name, "a", i + 1, "wan1"); if (e.wan2_intf) add(e.name, "b", i + 1, "wan2"); });
  return out;
}
function segPerDraw() {
  let t = null; try { t = collect(); } catch (_) { return; }
  const L = wanLinks(t);
  document.querySelectorAll("[data-segper]").forEach((c) => {
    const s = c.dataset.segper, l = L.filter((x) => x.seg === s);
    c.textContent = l.length ? `${l.length} WAN(s): ${l[0].subnet} … · VLAN ${l[0].vlan}${l.length > 1 ? "–" + l[l.length - 1].vlan : ""}` : "no WANs";
  });
  T.hubs.forEach((h, i) => {
    const x = L.find((l) => l.seg === "hub" && l.n === i + 1);
    const a = document.querySelector(`[data-hubaddr="${i}"]`), g = document.querySelector(`[data-hubgw="${i}"]`);
    if (a) a.value = x ? `${x.ip}/24` : "(invalid ISP-HUB block)";
    if (g) g.value = x ? x.gw : "";
  });
}
WAN_SEGS.forEach((k) => ["block", "vlanbase"].forEach((f) => $(`#wan-seg-${k}-${f}`).addEventListener("input", segPerDraw)));
function draw() {
  const p = T.pve;
  $("#adom").value = T.adom; $("#hub2hub").checked = !!T.hub2hub;
  $("#fm_name").value = T.fmgr.name; $("#fm_ip").value = T.fmgr.ip; $("#fm_gw").value = T.fmgr.gateway; $("#fm_port").value = T.fmgr.port;
  pveFill(p); wanFill(T.wanemu);
  const mv = T.region.multi_vrf !== false;
  $("#r_multi").checked = mv; $("#sv_box").hidden = mv; $("#vrfs").hidden = !mv; $("#r_lansum").value = T.region.lan_summary || "10.0.0.0/8";
  $("#vrfs").innerHTML = "<thead><tr><th>VRF id</th><th>LAN summary</th><th>DIA</th></tr></thead><tbody>" +
    T.region.vrfs.map((v, i) => `<tr data-v="${i}"><td><input data-f="id" type="number" value="${v.id}"></td><td><input data-f="lan_summary" value="${esc(v.lan_summary)}"></td><td><input data-f="dia" type="checkbox" ${v.dia ? "checked" : ""}></td></tr>`).join("") + "</tbody>";
  ["hubs", "edges"].forEach((k) => ($("#" + k).innerHTML = T[k].map((d, i) => dev(k, d, i)).join("")));
  const g = T.trafficgen || { ip: "" };
  $("#tg_ip").value = g.ip || ""; tgDraw();
  flexFields(); topoDraw(); segPerDraw(); modeNote();
}
// Traffic-Gen: management on the uplink network, traffic from the first spoke's LAN (.20-.29, spoke as gateway)
function tgDraw() {
  const up = (($("#wan-uplink-ip") && $("#wan-uplink-ip").value) || "").trim(), gw = (($("#wan-uplink-gw") && $("#wan-uplink-gw").value) || "").trim();
  const vlan = (($("#wan-uplink-vlan") && $("#wan-uplink-vlan").value) || "").trim();
  if (!$("#tg_ip").value && /^\d+\.\d+\.\d+\.\d+\/\d+$/.test(up)) $("#tg_ip").placeholder = up.replace(/\.(\d+)\/\d+$/, (m, o) => "." + Math.max(2, +o - 1));
  const sp = (T.edges || []).find((e) => (e.lans || [])[0] && e.lans[0].ip), lan = sp ? sp.lans[0].ip.split("/")[0].split(".").slice(0, 3).join(".") : "";
  $("#tg_hint").textContent = `Management (web GUI https://<this IP>:8443, user admin) via the WAN emulation uplink: ${vlan ? "VLAN " + vlan : "untagged"}${gw ? ", gateway " + gw : ""}. ` +
    (sp ? `Test traffic from ${sp.name}'s LAN: ${lan}.20-${lan}.29 with ${sp.lans[0].ip.split("/")[0]} (the spoke) as gateway; adjustable later in the Traffic-Gen GUI.` : "Needs a spoke with a LAN.") +
    " Comes along with the LAN clients (Everything, FortiGates only, LAN clients only); deleted by Cleanup.";
  pwHint("tg_pw");
}
// gateway follows the IP: the first address of its subnet (10.10.106.150/24 -> 10.10.106.1), until the user types their own
function gwFor(cidr) {
  const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(String(cidr || "").trim());
  if (!m) return "";
  const o = m.slice(1, 5).map(Number), len = +m[5];
  if (o.some((x) => x > 255) || len < 8 || len > 30) return "";
  const ip = ((o[0] << 24) >>> 0) + (o[1] << 16) + (o[2] << 8) + o[3], mask = (0xffffffff << (32 - len)) >>> 0;
  const gw = ((ip & mask) >>> 0) + 1;
  if (gw === ip) return "";                                    // the address is the first one itself: no guess
  return [24, 16, 8, 0].map((s) => (gw >>> s) & 255).join(".");
}
function gwAuto(ipSel, gwSel) {
  const ip = $(ipSel), gw = $(gwSel);
  ip.addEventListener("input", () => {
    if (gw.value.trim() && gw.value !== gw.dataset.auto) return;   // a gateway the user typed (or loaded) stays
    const g = gwFor(ip.value);
    gw.value = g; gw.dataset.auto = g;
    gw.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
gwAuto("#fm_ip", "#fm_gw");
gwAuto("#wan-uplink-ip", "#wan-uplink-gw");
["#wan-uplink-ip", "#wan-uplink-gw", "#wan-uplink-vlan"].forEach((id) => $(id).addEventListener("input", tgDraw));
function collect() {
  const t = JSON.parse(JSON.stringify(T)), n = (v) => (v === "" ? null : +v);
  t.adom = $("#adom").value.trim(); t.hub2hub = $("#hub2hub").checked;
  t.fmgr = { ...T.fmgr, name: $("#fm_name").value.trim(), ip: $("#fm_ip").value.trim(), gateway: $("#fm_gw").value.trim(), port: $("#fm_port").value.trim() || "port1" };
  t.pve = {
    server_id: $("#pve_server").value ? +$("#pve_server").value : null, url: "", token_id: "", verify_tls: true, node: $("#pve_node").value.trim(),
    disk_storage: $("#pve_dstore").value.trim(), import_storage: $("#pve_istore").value.trim(), iso_storage: $("#pve_isostore").value.trim(),
    default_bridge: $("#pve_br").value.trim(), cpu_type: $("#pve_cpu").value.trim() || "host", license_mode: "cli", first_vmid: n($("#pve_first").value), name_prefix: $("#pve_prefix").value.trim(),   // FortiFlex: always `execute vm-license <token>` as the last CLI line
    fgt: { cores: +$("#fgt_cores").value, memory_mb: +$("#fgt_mem").value },
    fmg: { cores: +$("#fmg_cores").value, memory_mb: +$("#fmg_mem").value, data_disk_gb: +$("#fmg_disk").value, bridge: $("#fmg_br").value.trim(), vlan: n($("#fmg_vlan").value) },
    ports: Object.fromEntries([...document.querySelectorAll("[data-p]")].map((r) => [r.dataset.p, { bridge: r.querySelector("[data-f=bridge]").value.trim(), vlan: n(r.querySelector("[data-f=vlan]").value) }])),
  };
  t.wanemu = wanCollect();
  t.trafficgen = { enabled: true, ip: $("#tg_ip").value.trim(), count: 10 };     // always comes along
  t.region.multi_vrf = $("#r_multi").checked; t.region.lan_summary = $("#r_lansum").value.trim();
  t.region.vrfs = [...document.querySelectorAll("[data-v]")].map((r) => ({ id: +r.querySelector("[data-f=id]").value, lan_summary: r.querySelector("[data-f=lan_summary]").value.trim(), dia: r.querySelector("[data-f=dia]").checked }));
  document.querySelectorAll(".dev").forEach((el) => {
    const d = t[el.dataset.k][el.dataset.i], lans = (d.lans || []).map((l) => ({ ...l }));
    el.querySelectorAll("[data-f]").forEach((inp) => {
      const k = inp.dataset.f, v = inp.value.trim(), m = k.match(/^lan(\d+)_(intf|ip)$/);
      if (m) (lans[+m[1]] ??= {})[m[2]] = v;
      else d[k] = v;
    });
    // only what is on screen: single-VRF = LAN1 only, multi-VRF = LAN per VRF, an optional LAN without a port is no LAN
    const nl = t.region.multi_vrf ? Math.max(1, t.region.vrfs.length) : 1;
    d.lans = lans.slice(0, nl).filter((l, j) => j === 0 || l.intf);
  });
  return t;
}
function secrets() {
  const tok = {};
  document.querySelectorAll("[data-tok]").forEach((i) => { if (i.value.trim()) tok[i.dataset.tok] = i.value.trim(); });
  return { fmg_token: $("#fm_token").value.trim() || null, fgt_tokens: tok, fmg_initial_pw: "", fgt_initial_pw: "", fgt_new_pw: $("#fgt_newpw").value, client_pw: $("#client_pw").value, tg_pw: $("#tg_pw").value, admin_user: $("#fmg_user").value || "admin", pve_token_secret: "" };
}
const fmgCfg = () => ({ host: null, username: $("#fmg_user").value, password: $("#fmg_pass").value });

// ---- images -----------------------------------------------------------------
function imgDraw() {
  for (const r of ["fgt", "fmg"]) {
    const s = $("#img_" + r), cur = s.value, l = IMGS.filter((i) => i.role === r);
    s.innerHTML = l.length ? l.map((i) => `<option value="${i.id}">${esc(i.filename)}${i.version ? " (v" + i.version + ")" : ""}</option>`).join("") : '<option value="">— no image yet —</option>';
    if (cur && l.some((i) => i.id === cur)) s.value = cur;
  }
  $("#img_list").innerHTML = IMGS.length
    ? `<table class="imgtbl"><thead><tr><th>Role</th><th>File</th><th>Version</th><th>Virtual</th><th>SHA-256</th><th></th></tr></thead><tbody>` +
      IMGS.map((i) => `<tr><td>${i.role === "fgt" ? "FortiGate" : "FortiManager"}</td><td class="mono">${esc(i.filename)}${i.warnings.map((w) => `<div class="hint warn">⚠ ${esc(w)}</div>`).join("")}</td><td class="mono">${i.version ? "v" + i.version + (i.build ? " b" + i.build : "") : "—"}</td><td class="mono">${i.virtual_gb} GB</td><td class="mono" title="${i.sha256}">${i.sha256.slice(0, 16)}…</td><td class="actions-cell"><button class="icon danger" data-del="${i.id}" title="Delete"><span class="i"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg></span></button></td></tr>`).join("") + "</tbody></table>"
    : '<p class="muted">No images uploaded yet.</p>';
}
async function imgLoad() { IMGS = await (await fetch("/api/images")).json(); imgDraw(); }
function upload(role, file) {
  if (!file) return;
  const fd = new FormData(); fd.append("role", role); fd.append("file", file);
  const x = new XMLHttpRequest(), dz = $("#dz_" + role), bar = $("#bar_" + role);
  dz.classList.add("has"); dz.querySelector("b").textContent = file.name; dz.querySelector("small").textContent = fmtBytes(file.size) + " · uploading…";
  x.open("POST", "/api/images");
  x.upload.onprogress = (e) => { if (e.lengthComputable) bar.style.transform = `scaleX(${e.loaded / e.total})`; };
  x.onload = async () => {
    bar.style.transform = "scaleX(0)";
    if (x.status === 200) {
      const m = JSON.parse(x.responseText);
      dz.querySelector("small").textContent = `${fmtBytes(file.size)} · ${m.virtual_gb} GB virtual · done`;
      showErr([], "#img_err"); await imgLoad();
    } else {
      let d; try { d = JSON.parse(x.responseText).detail; } catch (_) { d = x.responseText; }
      dz.classList.remove("has"); dz.querySelector("b").textContent = "Drop the file here or click"; dz.querySelector("small").textContent = ".zip or .qcow2";
      showErr(["Upload failed: " + d], "#img_err");
    }
  };
  x.send(fd);
}
for (const role of ["fgt", "fmg"]) {
  const dz = $("#dz_" + role), inp = $("#up_" + role);
  dz.addEventListener("click", () => inp.click());
  inp.addEventListener("change", () => { upload(role, inp.files[0]); inp.value = ""; });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); upload(role, e.dataTransfer.files[0]); });
}
$("#img_list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del]");
  if (b && confirm("Delete this image?")) { await fetch("/api/images/" + b.dataset.del, { method: "DELETE" }); imgLoad(); }
});

// ---- actions ----------------------------------------------------------------

// ---- actions ----------------------------------------------------------------
// Proxmox data (nodes, storages per node, bridges) is fetched from the saved server and shown as dropdowns
async function loadPve(msg) {
  const sid = $("#pve_server").value, out = $("#pve_res");
  out.className = "hint";
  if (!sid) { PVE_OPT = { nodes: [], image_storages: [], import_storages: [], iso_storages: [], bridges: [] }; pveFill(collect().pve); out.textContent = ""; return; }
  out.textContent = "Fetching data…";
  const p = collect().pve;
  const r = await post(`/api/servers/${sid}/test`, {}), j = await json(r);
  if (!r.ok) { out.className = "hint error"; out.textContent = "Error: " + j.detail; return; }
  PVE_OPT.nodes = j.nodes;
  const node = j.nodes.includes(p.node) ? p.node : j.nodes[0];
  const r2 = await fetch(`/api/servers/${sid}/nodes/${encodeURIComponent(node)}/options`), o = await json(r2);
  if (!r2.ok) { out.className = "hint error"; out.textContent = "Error: " + o.detail; return; }
  Object.assign(PVE_OPT, o);
  const pick = (cur, list) => (names(list).includes(cur) ? cur : names(list)[0] || "");
  pveFill(Object.assign(p, { node, disk_storage: pick(p.disk_storage, o.image_storages), import_storage: pick(p.import_storage, o.import_storages), iso_storage: pick(p.iso_storage, o.iso_storages), default_bridge: o.bridges.includes(p.default_bridge) ? p.default_bridge : o.bridges[0] || "" }));
  out.className = "hint " + (j.warning ? "warn" : "ok");
  out.textContent = `Proxmox VE ${j.version} · ${j.nodes.length} node(s)` + (j.warning ? " · ⚠ " + j.warning : "");
}
$("#btn_pve").onclick = () => loadPve();
$("#pve_node").addEventListener("change", () => loadPve());
// ---- passwords: strong rule (same as model.password_problems) + show/hide ------------------------------------------
function pwProblems(pw) {
  pw = pw || "";
  return [["at least 8 characters", pw.length >= 8], ["an uppercase letter", /[A-Z]/.test(pw)], ["a lowercase letter", /[a-z]/.test(pw)],
    ["a digit", /[0-9]/.test(pw)], ["a special character", /[^A-Za-z0-9]/.test(pw)]].filter(([, ok]) => !ok).map(([w]) => w);
}
const PW_EXISTING = ["fgt", "tpl", "dev"];        // an existing FortiManager: its password is not set by this tool
const PW_STRONG = { pw_all: () => ["all", "fmg", "fgt", "lan"].includes(scope()), fmg_pass: () => ["all", "fmg"].includes(scope()), fgt_newpw: () => true, tg_pw: () => true };   // passwords the tool sets
function pwHint(id) {
  const el = $("#" + id), h = $("#" + id + "_hint"), v = el.value, p = pwProblems(v);
  if (!PW_STRONG[id]()) { h.textContent = ""; el.classList.remove("bad"); return; }
  h.className = "hint" + (id === "fmg_pass" ? " only-fmg" : "") + (v && !p.length ? " okc" : "");
  h.textContent = !v ? "Strong password: 8+ characters with upper and lower case, a digit and a special character" : p.length ? "Still needs " + p.join(", ") : "✓ strong";
  el.classList.toggle("bad", !!(v && p.length));
}
function pwErrors() {
  const out = [];
  for (const [id, need] of Object.entries(PW_STRONG)) {
    const el = $("#" + id);
    if (!need() || !el.offsetParent || !el.value) continue;
    const p = pwProblems(el.value);
    if (p.length) out.push(`${{ pw_all: "Password", fmg_pass: "FortiManager", fgt_newpw: "FortiGates", tg_pw: "Traffic-Gen" }[id]}: ${id === "tg_pw" ? "GUI password" : id === "pw_all" ? "it" : "admin password"} is not strong, it needs ${p.join(", ")}`);
  }
  return out;
}
Object.keys(PW_STRONG).forEach((id) => $("#" + id).addEventListener("input", () => pwHint(id)));
// one password for everything: fills FortiManager, FortiGates, LAN clients and Traffic-Gen, except a field that was
// changed by hand in Advanced
const PW_EACH = ["fmg_pass", "fgt_newpw", "client_pw", "tg_pw"];
$("#pw_all").addEventListener("input", () => {
  PW_EACH.forEach((id) => {
    if (id === "fmg_pass" && PW_EXISTING.includes(scope())) return;     // the existing FortiManager keeps its own login
    const el = $("#" + id); if (!el.dataset.own) { el.value = $("#pw_all").value; if (PW_STRONG[id]) pwHint(id); }
  });
});
PW_EACH.forEach((id) => $("#" + id).addEventListener("input", (e) => { if (e.isTrusted) e.target.dataset.own = "1"; }));
function pwAllFrom() {               // after loading settings: one shared password shows in the generic field
  const vals = PW_EACH.map((id) => $("#" + id).value).filter(Boolean), same = vals.length && vals.every((v) => v === vals[0]);
  $("#pw_all").value = same ? vals[0] : "";
  if (same) PW_EACH.forEach((id) => { if (!$("#" + id).value) $("#" + id).value = vals[0]; });   // e.g. no Traffic-Gen password yet
  PW_EACH.forEach((id) => { if (same || !$("#" + id).value) delete $("#" + id).dataset.own; else $("#" + id).dataset.own = "1"; });
  pwHint("pw_all");
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-eye]"); if (!b) return;
  const i = $("#" + b.dataset.eye), show = i.type === "password";
  i.type = show ? "text" : "password"; b.classList.toggle("on", show); b.setAttribute("aria-label", show ? "Hide password" : "Show password");
});

// ---- settings: export / import a JSON file, saved settings in the tool ------------------------------------------
const setMsg = (m, bad) => { $("#set_res").className = "hint" + (bad ? " error" : ""); $("#set_res").textContent = m; };
const setUi = () => ({ scope: scope(), fmg_user: $("#fmg_user").value });
const SET_PW = ["fmg_pass", "fgt_newpw", "client_pw", "tg_pw"];                   // kept with a save in the tool
const setPw = () => Object.fromEntries(SET_PW.map((id) => [id, $("#" + id).value]).filter(([, v]) => v));
async function setApply(j, src) {
  T = j.topology; draw(); markIpFields();
  if (j.ui && j.ui.fmg_user) $("#fmg_user").value = j.ui.fmg_user;
  const sc = j.ui && document.querySelector(`input[name="scope"][value="${j.ui.scope}"]`);
  if (sc) { sc.checked = true; scopeApply(); }
  if (T.pve.server_id) { $("#pve_server").value = T.pve.server_id; await loadPve(); }
  const pw = j.passwords || {};
  SET_PW.forEach((id) => { if (pw[id]) $("#" + id).value = pw[id]; });
  pwAllFrom();
  Object.keys(PW_STRONG).forEach(pwHint);
  setMsg(`✓ ${src} loaded` + (j.notes && j.notes.length ? ` (${j.notes.join("; ")})` : "") +
    (Object.keys(pw).length ? ", passwords filled in. FortiFlex tokens are not part of settings." : ". Passwords and FortiFlex tokens were not part of it."));
}
$("#btn_set_export").onclick = async () => {
  const r = await post("/api/settings/export", { topology: collect(), ui: setUi() });
  if (!r.ok) return setMsg("Export failed", true);
  const name = (r.headers.get("content-disposition") || "").match(/filename="([^"]+)"/), url = URL.createObjectURL(await r.blob()), a = document.createElement("a");
  a.href = url; a.download = name ? name[1] : "fgt-sdwan-settings.json"; a.click(); URL.revokeObjectURL(url);
  setMsg("Settings exported (without passwords). Import the file again to rebuild this form.");
};
$("#btn_set_import").onclick = () => $("#set_file").click();
$("#set_file").onchange = async (ev) => {
  const f = ev.target.files[0]; ev.target.value = "";
  if (!f) return;
  let data;
  try { data = JSON.parse(await f.text()); } catch (_) { return setMsg(`✕ ${f.name}: not a JSON file`, true); }
  const r = await post("/api/settings/import", data), j = await json(r);
  if (!r.ok) return setMsg(`✕ ${f.name}: ${typeof j.detail === "string" ? j.detail : "import failed"}`, true);
  await setApply(j, f.name);
};
async function setList() {
  const l = await (await fetch("/api/settings/saved")).json();
  $("#set_list").innerHTML = l.length
    ? `<table class="tbl"><thead><tr><th>Description</th><th>ADOM</th><th>FortiGates</th><th>Saved</th><th></th></tr></thead><tbody>` +
      l.map((x) => `<tr><td>${esc(x.description)}</td><td class="mono">${esc(x.adom)}</td><td>${x.devices}</td><td class="mono">${esc(x.updated)}</td>` +
        `<td class="actions-cell"><button data-sload="${x.id}">Load</button> <button data-supd="${x.id}" title="Overwrite with the current form">Update</button> <button class="icon danger" data-sdel="${x.id}" title="Delete"><span class="i"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg></span></button></td></tr>`).join("") + "</tbody></table>"
    : '<p class="muted">Nothing saved yet.</p>';
  SAVED = l;
}
let SAVED = [];
$("#btn_set_save").onclick = async () => {
  const d = $("#set_desc").value.trim();
  if (!d) { $("#set_desc").classList.add("bad"); return setMsg("Give the settings a description first", true); }
  $("#set_desc").classList.remove("bad");
  const r = await post("/api/settings/saved", { description: d, topology: collect(), ui: setUi(), passwords: setPw() }), j = await json(r);
  if (!r.ok) return setMsg("✕ " + (typeof j.detail === "string" ? j.detail : "save failed"), true);
  $("#set_desc").value = ""; setMsg(`✓ Saved as '${d}'`); await setList();
};
$("#set_list").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.sload) {
    const r = await fetch("/api/settings/saved/" + b.dataset.sload), j = await json(r);
    if (!r.ok) return setMsg("✕ " + (j.detail || "load failed"), true);
    await setApply(j, `'${j.description}'`);
  } else if (b.dataset.supd) {
    const x = SAVED.find((s) => String(s.id) === b.dataset.supd);
    if (!confirm(`Overwrite '${x.description}' with the current form?`)) return;
    const r = await fetch("/api/settings/saved/" + x.id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ description: x.description, topology: collect(), ui: setUi(), passwords: setPw() }) });
    setMsg(r.ok ? `✓ '${x.description}' updated` : "✕ update failed", !r.ok); await setList();
  } else if (b.dataset.sdel) {
    const x = SAVED.find((s) => String(s.id) === b.dataset.sdel);
    if (!confirm(`Delete saved settings '${x.description}'?`)) return;
    await fetch("/api/settings/saved/" + x.id, { method: "DELETE" }); setMsg(`'${x.description}' deleted`); await setList();
  }
});

// ---- scope: all / only FortiManager / only FortiGates ------------------------
const SCOPE_HINT = {
  all: "",
  fmg: "Only the FortiManager is created and prepared with ADOM, templates, policy packages and blueprints for the hubs and spokes, without model devices. Names, loopbacks, LANs and hub addresses are needed for the templates; serial numbers, wan1 gateways, FortiGate tokens and password are not (they come with 'FortiGates only').",
  tpl: "Deploys only the ADOM and the templates (metadata variables, system, SD-WAN and CLI templates, policy packages, template groups, blueprints) to an existing FortiManager at the given IP, with the admin user and password below. No VMs and no Proxmox; the FortiManager settings (time zone, timers, FortiAnalyzer) are left alone. Serial numbers and wan1 gateways are not needed; model devices follow with 'FortiGates only'.",
  dev: "Creates the ADOM, the model devices (with serial number) and the templates with the per-device values in an existing FortiManager, without a link to real FortiGates: no VMs, no Proxmox. Then the tool does a Quick install (device settings) per device and a Re-install policy per role, to the device database. The FortiManager settings are left alone. Serial numbers and hub gateways are needed; FortiGate tokens and password are not. Real FortiGates register later on their serial number, or deploy them with 'FortiGates only'.",
  fgt: "The FortiManager must already run on the given static IP and be reachable with the admin user and password below. No FortiManager VM is created.",
  lan: "Creates (or updates) only the LAN client containers client-<spoke> on Proxmox, one per spoke on its LAN1 VLAN with .10 and the spoke as gateway. Nothing is sent to FortiManager or the FortiGates and no licences are used. The password below becomes the clients' root password.",
};
const scope = () => (document.querySelector('input[name="scope"]:checked') || {}).value || "all";
function scopeApply() {
  const s = scope();
  if (s !== "all" && document.body.dataset.mode === "simple") modeSet("advanced");   // only Advanced offers the others
  $("#v_deploy").dataset.scope = s;
  $("#scope_hint").textContent = SCOPE_HINT[s];
  if (T) { flexFields(); topoDraw(); markIpFields(); $("#fm_ip").classList.remove("bad"); $("#fm_ip").title = ""; }
  Object.keys(PW_STRONG).forEach(pwHint);
  try { localStorage.setItem("fgt-sdwan-scope", s); } catch (_) {}
}
document.querySelectorAll('input[name="scope"]').forEach((i) => (i.onchange = scopeApply));
function scopeInit() {
  let s = "all";
  try { s = localStorage.getItem("fgt-sdwan-scope") || "all"; } catch (_) {}
  const el = document.querySelector(`input[name="scope"][value="${s}"]`);
  if (el) el.checked = true;
  scopeApply();
}

// ---- deploy + progress ------------------------------------------------------
function stepsDraw(states = {}) {
  $("#pl").innerHTML = STEPS.map((s) => { const st = states[s.id] || "pending"; return `<li data-id="${s.id}" class="st-${st}"><span class="ic"><span class="dot st-${st}"></span></span><span class="t">${esc(s.title)}</span><span class="msg"></span></li>`; }).join("");
}
$("#btn_deploy").onclick = async () => {
  showErr([]);
  { const e = [...ipErrors(), ...pwErrors()]; if (e.length) return showErr(e); }
  const dry = false, sc = scope(), mf = ["fgt", "tpl", "dev", "lan"].includes(sc) ? "none" : "day0", mg = ["fmg", "tpl", "dev", "lan"].includes(sc) ? "none" : "day0";   // Day-0 for what is deployed
  const what = { all: "the FortiManager and FortiGate VMs", fmg: "the FortiManager VM", fgt: "the FortiGate VMs" }[sc];
  const ask = sc === "lan" ? "This creates or updates the LAN client containers on Proxmox (one per spoke). Continue?"
    : sc === "tpl" ? "This writes the ADOM and the templates to the existing FortiManager (set/update: earlier objects of this tool are overwritten). Continue?"
    : sc === "dev" ? "This writes the ADOM, the model devices and the templates to the existing FortiManager (set/update: earlier objects of this tool are overwritten) and then does a Quick install and a Re-install policy per device. Continue?"
    : `This writes to FortiManager, creates ${what} on Proxmox, configures and licenses them (they reboot). Continue?`;
  if (!dry && !confirm(ask)) return;
  const btn = $("#btn_deploy"); if (btn.disabled) return;
  btn.disabled = true;                                   // FortiFlex licenses are fetched in this request: no double click
  const body = { topology: collect(), fmg: fmgCfg(), dry_run: false, scope: sc, bootstrap: { fmg: mf, fgt: mg }, secrets: secrets(), images: { fgt: mg === "none" ? null : $("#img_fgt").value || null, fmg: mf === "none" ? null : $("#img_fmg").value || null } };
  let plan = null;
  if (!$("#btn_flexcheck").hidden) {                     // FortiFlex in this run: show what is looked up and why it takes a while
    deployStatus(LIC.mode === "files" ? "Licence files: checking a file per device…" : "FortiFlex: looking up licences for the devices of this deployment…");
    const c = await post("/api/fortiflex/check", { ...body, dry_run: true }), cj = await json(c);
    if (!c.ok) { deployStatus(null); btn.disabled = false; return showErr(errList(cj)); }
    plan = cj.devices;
  }
  const t0 = Date.now(), msg = () => (plan && plan.length
    ? (dry ? "Dry run: lookup only, nothing is created or renewed. " : `FortiFlex: fetching the licence per device (${plan.filter((d) => d.action !== "use").length} with a new token or a new licence), then checking everything. `)
      + `Starting deployment… ${Math.round((Date.now() - t0) / 1000)} s` : `Starting deployment… ${Math.round((Date.now() - t0) / 1000)} s`);
  deployStatus(msg(), plan);
  const tick = setInterval(() => deployStatus(msg(), plan), 1000);
  const r = await post("/api/deploy", body);
  clearInterval(tick);
  const j = await json(r);
  btn.disabled = false; deployStatus(null);
  if (!r.ok) return showErr(errList(j));
  if (ES) ES.close();
  location.hash = "#/progress/" + j.job;                 // route() attaches to the job; a refresh keeps working
};
function progressMsg(t, cls) { $("#p_sum").className = "summary" + (cls ? " " + cls : ""); $("#p_sum").textContent = t; $("#p_sum").style.padding = cls ? "12px 16px" : ""; }
async function progressOpen(id) {
  if (!id) {                                              // "Voortgang" without a job: the most recent one
    const l = await (await fetch("/api/jobs")).json();
    if (!l.latest) { if (ES) { ES.close(); ES = null; } CUR_JOB = null; $("#p_title").textContent = "Progress"; stepsDraw(); $("#log").textContent = ""; return progressMsg("No deployment has been started yet. Start one on the Deploy page."); }
    location.replace("#/progress/" + l.latest); return;
  }
  if (id === CUR_JOB && ES) return;                       // already showing this one
  if (ES) ES.close();
  CUR_JOB = id; $("#p_cancel").hidden = true; $("#p_cancel").disabled = false;
  const r = await fetch("/api/jobs/" + id);
  if (!r.ok) { ES = null; CUR_JOB = null; $("#p_title").textContent = "Progress"; stepsDraw(); $("#log").textContent = ""; return progressMsg("This deployment no longer exists (the list of deployments is empty or cleared). Start a new one on the Deploy page."); }
  const info = await r.json();
  $("#p_title").textContent = info.title || "Deployment";
  progressMsg("Working…"); $("#log").textContent = ""; stepsDraw();
  ES = new EventSource(`/api/jobs/${id}/stream`);        // replays the whole log, then follows a running job
  ES.addEventListener("log", (e) => {
    const x = JSON.parse(e.data), d = document.createElement("div");
    d.className = "e-" + x.status; d.textContent = `${x.t} ${x.step ? "[" + x.step + "] " : ""}${x.msg}`;
    $("#log").append(d); $("#log").scrollTop = 1e9;
    if (x.step) { const li = $(`#pl li[data-id="${x.step}"] .msg`); if (li) li.textContent = x.msg; }
  });
  ES.addEventListener("state", (e) => {
    const s = JSON.parse(e.data);
    document.querySelectorAll("#pl li").forEach((li) => { const st = s.steps[li.dataset.id]; li.className = "st-" + st; li.querySelector(".dot").className = "dot st-" + st; });
    $("#p_cancel").hidden = s.state !== "running";
    if (s.state !== "running") {
      ES.close(); ES = null;
      if (s.state === "cancelled") progressMsg("■ Cancelled — what was already created stays; remove it with Cleanup", "warn");
      else progressMsg(s.state === "done" ? "✔ Done" : "✕ Failed — see the log", s.state === "done" ? "ok" : "error");
    }
  });
}
$("#p_cancel").onclick = async () => {
  if (!CUR_JOB || !confirm("Cancel this deployment? It stops at its next step or wait. What was already created (VMs, licences, FortiManager objects) stays; remove it with Cleanup.")) return;
  const btn = $("#p_cancel"); btn.disabled = true; btn.textContent = "Cancelling…";
  const r = await fetch(`/api/jobs/${CUR_JOB}/cancel`, { method: "POST" });
  if (!r.ok) { btn.disabled = false; progressMsg("✕ " + errList(await json(r)).join("; "), "error"); }
  btn.textContent = "Cancel deployment";
};
async function bannerLoad() {
  const el = $("#job_banner");
  try {
    const l = await (await fetch("/api/jobs")).json(), j = l.jobs[0];
    if (!j) { el.hidden = true; return; }
    const st = { running: "is running", done: "is done", failed: "failed", cancelled: "was cancelled" }[j.state] || j.state;
    el.hidden = false; el.innerHTML = `Last deployment: <b>${esc(j.title)}</b> (${esc(j.created)}) ${st} &middot; <a href="#/progress/${j.id}" style="color:var(--accent)">View progress and log</a>`;
  } catch (_) { el.hidden = true; }
}

// ---- servers ----------------------------------------------------------------
let SERVERS = [], EDIT = null;
function srvReset() {
  EDIT = null; $("#srv_form_title").textContent = "Add server";
  for (const id of ["name", "url", "tid", "secret"]) $("#srv_" + id).value = "";
  $("#srv_secret").placeholder = ""; $("#srv_console_user").value = ""; $("#srv_console_pw").value = ""; $("#srv_console_pw").placeholder = ""; $("#srv_tls").checked = true; $("#srv_cancel").hidden = true; $("#srv_res").textContent = ""; $("#srv_res").className = "hint";
}
function srvDraw() {
  $("#srv_list").innerHTML = SERVERS.length
    ? `<table><thead><tr><th>Name</th><th>URL</th><th>Token ID</th><th>TLS</th><th></th></tr></thead><tbody>` +
      SERVERS.map((s) => `<tr><td>${esc(s.name)}</td><td class="mono">${esc(s.url)}</td><td class="mono">${esc(s.token_id)}</td><td>${s.verify_tls ? "verified" : "not verified"}</td><td class="actions-cell"><span class="hint" id="srv_t${s.id}"></span> <button class="link" data-st="${s.id}">Test</button><button class="link" data-se="${s.id}">Edit</button><button class="icon danger" data-sd="${s.id}" title="Delete"><span class="i"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg></span></button></td></tr>`).join("") + "</tbody></table>"
    : '<p class="muted">No servers yet. Add your first Proxmox server below.</p>';
  const sel = $("#pve_server"), cur = sel.value;
  sel.innerHTML = '<option value="">— choose a server —</option>' + SERVERS.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  if (cur && SERVERS.some((s) => String(s.id) === cur)) sel.value = cur;
  $("#pve_none").hidden = SERVERS.length > 0;
}
async function serversLoad(selectLast) {
  SERVERS = await (await fetch("/api/servers")).json();
  srvDraw();
  if (selectLast && !$("#pve_server").value) {
    const l = SERVERS.find((s) => s.last) || (SERVERS.length === 1 ? SERVERS[0] : null);
    if (l) { $("#pve_server").value = l.id; await applyLast(l.id); await loadPve(); }
  }
}
async function applyLast(sid) {
  if (!sid) return;
  const last = await (await fetch(`/api/servers/${sid}/last-used`)).json();
  if (!Object.keys(last).length) return;
  const { wanemu_uplink: up, wanemu_bridge: wb, ...pve } = last;
  T.pve = Object.assign({}, T.pve, pve, { ports: Object.assign({}, T.pve.ports, pve.ports || {}) });
  pveFill(T.pve);
  if (wb) { T.wanemu.bridge = wb; setSel("#wan-bridge", PVE_OPT.bridges, wb); }     // this server's last bridges
  if (up && !$("#wan-uplink-ip").value.trim()) {          // uplink of this server's last deployment (it has no default)
    T.wanemu.uplink.bridge = up.bridge || ""; setSel("#wan-uplink-bridge", PVE_OPT.bridges, up.bridge || "", "(same as WAN emulator bridge)");
    $("#wan-uplink-vlan").value = up.vlan ?? ""; $("#wan-uplink-ip").value = up.ip || ""; $("#wan-uplink-gw").value = up.gw || "";
    topoDraw();
  }
}
$("#pve_server").addEventListener("change", async () => { $("#pve_res").textContent = ""; await applyLast($("#pve_server").value); await loadPve(); });
$("#srv_list").addEventListener("click", async (e) => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.dataset.sd) {
    const s = SERVERS.find((x) => String(x.id) === t.dataset.sd);
    if (confirm(`Delete server '${s.name}'?`)) { await fetch("/api/servers/" + s.id, { method: "DELETE" }); if (EDIT === s.id) srvReset(); await serversLoad(); }
  } else if (t.dataset.se) {
    const s = SERVERS.find((x) => String(x.id) === t.dataset.se);
    EDIT = s.id; $("#srv_form_title").textContent = `Edit server: ${s.name}`;
    $("#srv_name").value = s.name; $("#srv_url").value = s.url; $("#srv_tid").value = s.token_id; $("#srv_secret").value = ""; $("#srv_secret").placeholder = "leave unchanged";
    $("#srv_console_user").value = s.console_user || ""; $("#srv_console_pw").value = ""; $("#srv_console_pw").placeholder = s.has_console_password ? "leave unchanged" : "";
    $("#srv_tls").checked = s.verify_tls; $("#srv_cancel").hidden = false; $("#srv_name").scrollIntoView({ block: "center" });
  } else if (t.dataset.st) {
    const out = $("#srv_t" + t.dataset.st); out.className = "hint"; out.textContent = "…";
    const r = await post(`/api/servers/${t.dataset.st}/test`, {}), j = await json(r);
    out.className = "hint " + (r.ok ? (j.warning ? "warn" : "ok") : "error"); out.textContent = r.ok ? `OK · PVE ${j.version}` + (j.warning ? " ⚠" : "") : "Error: " + j.detail;
  }
});
$("#srv_cancel").onclick = srvReset;
const srvBody = () => ({ name: $("#srv_name").value.trim(), url: $("#srv_url").value.trim(), token_id: $("#srv_tid").value.trim(), token_secret: $("#srv_secret").value || null, verify_tls: $("#srv_tls").checked,
  console_user: $("#srv_console_user").value.trim(), console_password: $("#srv_console_pw").value || null });
$("#srv_save").onclick = async () => {
  showErr([], "#srv_err");
  const r = await fetch(EDIT ? "/api/servers/" + EDIT : "/api/servers", { method: EDIT ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(srvBody()) }), j = await json(r);
  if (!r.ok) return showErr(errList(j), "#srv_err");
  srvReset(); await serversLoad(true);   // first server: select it on the deploy page right away
};
$("#srv_test").onclick = async () => {
  const b = srvBody(), out = $("#srv_res"); out.className = "hint"; out.textContent = "…";
  const r = EDIT && !b.token_secret ? await post(`/api/servers/${EDIT}/test`, {}) : await post("/api/pve/test", { url: b.url, token_id: b.token_id, token_secret: b.token_secret || "", verify_tls: b.verify_tls }), j = await json(r);
  out.className = "hint " + (r.ok ? (j.warning ? "warn" : "ok") : "error"); out.textContent = r.ok ? `OK — Proxmox VE ${j.version}` + (j.warning ? " · ⚠ " + j.warning : "") : "Error: " + j.detail;
};

// ---- Simple (default) / Advanced deploy form: Advanced-only parts carry class "adv" ------------------------------
const modeGet = () => { try { return localStorage.getItem("fgt-sdwan-mode") === "advanced" ? "advanced" : "simple"; } catch (_) { return "simple"; } };
function modeSet(m) {
  document.body.dataset.mode = m;
  if (m === "simple" && scope() !== "all") { $("#scope_all").checked = true; scopeApply(); }   // Simple deploys everything
  try { localStorage.setItem("fgt-sdwan-mode", m); } catch (_) {}
  $("#mode_simple").classList.toggle("on", m === "simple"); $("#mode_adv").classList.toggle("on", m === "advanced");
  modeNote();
}
function modeNote() {               // Simple = the standard 2 hubs + 3 spokes; another topology stays, shown in Advanced
  const custom = T && (T.hubs.length !== 2 || T.edges.length !== 3);
  $("#mode_note").hidden = !(custom && document.body.dataset.mode === "simple");
}
$("#mode_simple").onclick = () => modeSet("simple");
$("#mode_adv").onclick = () => modeSet("advanced");

// ---- self-update: badge when GitHub main is newer; the update runs ct/install.sh and the tool restarts -----------
let UPD = null;
async function updCheck() {
  try { const r = await fetch("/api/version", { cache: "no-store" }); UPD = await r.json(); } catch (_) { UPD = null; }
  $("#upd_badge").hidden = !(UPD && UPD.behind > 0);
}
setInterval(updCheck, 5 * 60 * 1000);                     // an open page also shows a new version (the server caches the check)
function updShow() {
  const v = UPD || {};
  $("#upd_body").textContent = v.error ? v.error : v.behind ? `Version ${v.current} → ${v.latest} (${v.behind} change${v.behind > 1 ? "s" : ""}). The tool restarts; a running deployment must be finished first.` : `Up to date (${v.current || "unknown"}).`;
  $("#upd_list").innerHTML = (v.commits || []).map((c) => `<li><span class="mono">${esc(c.sha)}</span> ${esc(c.title)}</li>`).join("");
  $("#upd_go").hidden = !v.behind; $("#upd_go").disabled = false; $("#upd_panel").hidden = false;
}
$("#upd_badge").onclick = (e) => { e.preventDefault(); updShow(); };
$("#upd_close").onclick = () => ($("#upd_panel").hidden = true);
$("#upd_go").onclick = async () => {
  const from = UPD && UPD.current;
  $("#upd_go").disabled = true;
  const r = await fetch("/api/update", { method: "POST" }), j = await json(r);
  if (!r.ok) { $("#upd_body").textContent = "Update not started: " + (j.detail || r.status); $("#upd_go").disabled = false; return; }
  $("#upd_body").textContent = "Updating… the tool restarts, this page reloads when the new version answers.";
  const t0 = Date.now();
  const poll = setInterval(async () => {
    try {
      const v = await (await fetch("/api/version", { cache: "no-store" })).json();
      if (v.current && v.current !== from) { clearInterval(poll); location.reload(); return; }
      const lu = v.last_update;                // install.sh failed: the service was not restarted, the old version runs on
      if (lu && lu.at * 1000 > t0 && lu.rc !== 0 && !v.updating) {
        clearInterval(poll);
        $("#upd_body").textContent = `The update failed (exit code ${lu.rc}); this version keeps running. Details on the tool host: journalctl -u fgt-sdwan-update`;
        $("#upd_go").disabled = false;
      }
    } catch (_) { /* restarting */ }
    if (Date.now() - t0 > 10 * 60 * 1000) { clearInterval(poll); $("#upd_body").textContent = "The update takes longer than expected: reload the page later."; }
  }, 5000);
};

// ---- init -------------------------------------------------------------------
(async () => {
  const a = await (await fetch("/api/about")).json();
  STEPS = a.steps;
  T = await (await fetch("/api/defaults")).json();
  $("#r_multi").addEventListener("change", () => { T = collect(); if (T.region.multi_vrf && !T.region.vrfs.length) T.region.vrfs = [{ id: 11, lan_summary: "10.0.0.0/14", dia: true }, { id: 12, lan_summary: "10.0.0.0/14", dia: true }]; draw(); markIpFields(); });
  modeSet(modeGet()); await licMode(); draw(); markIpFields(); scopeInit(); stepsDraw(); await imgLoad(); await serversLoad(true); setList(); route();
  updCheck(); setInterval(updCheck, 30 * 60 * 1000);
})();


// ---- WAN-emulatie: one card per link (ISP-HUB / ISP-A / ISP-B), values go to the agent through the tool ----------
const WAN_SEG = { hub: "ISP-HUB", a: "ISP-A", b: "ISP-B" };
const WAN_F = [["delay_ms", "Latency", "ms", 0, 1000, 1], ["jitter_ms", "Jitter", "ms", 0, 200, 1], ["loss_pct", "Loss", "%", 0, 100, 0.1], ["rate_kbit", "Bandwidth", "kbit/s (0 = unlimited)", 0, 1000000, 1000]];
function wanErr(msg) { $("#wanemu-err").innerHTML = msg ? `<div class="error">✕ ${esc(msg)}</div>` : ""; }   // no scroll: the page refreshes every 10 s
// ---- FortiGates via wan-emu: GUI via port forward, SSH in the browser (xterm.js) ----------------------------------
const XTERM = "https://cdn.jsdelivr.net/npm/xterm@5.3.0/", XFIT = "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/";
// remote access: FortiManager first, then the FortiGates; GUI link, copy the kept password, SSH (auto-login when kept)
async function wanFwdLoad() {
  const r = await fetch("/api/access"), j = await json(r);
  if (!r.ok) { $("#wanemu-fgts").innerHTML = `<div class="hint">${esc(errList(j).join(" "))}</div>`; return; }
  // one row per device in fixed columns: name · how it is reached · IP (click = copy) · actions (same slots on every row) · SSH command (click = copy)
  const slot = (html) => html || '<span class="acc-slot"></span>';
  const row = (d, key, via) => `<div class="acc-row"><span class="acc-name"><b>${esc(d.name)}</b><span class="hint">${esc(via)}</span></span>` +
    `<button class="acc-copy mono" data-copy="${esc(d.ip)}" title="Copy ${esc(d.ip)} to the clipboard">${esc(d.ip)}</button><span class="acc-act">` +
    slot(d.gui ? `<a href="${esc(d.gui)}" target="_blank" rel="noopener"><button>GUI</button></a>` : "") +
    slot(d.ssh ? `<button data-ssh="${esc(key)}" data-auto="${d.password ? 1 : ""}">SSH</button>` : "") +
    `<button data-console="${esc(key)}" title="Proxmox console (also when the network to the device is down)">Console</button>` +
    slot(d.password ? `<button data-cpw="${esc(key)}" title="Copy the password to the clipboard">Copy password</button>` : "") +
    `</span>` + (d.ssh ? `<button class="acc-copy acc-cmd mono" data-copy="${esc(d.ssh)}" title="Copy: ${esc(d.ssh)}">${esc(d.ssh)}</button>` : "<span></span>") + `</div>`;
  const group = (title, list) => (list.length ? `<div class="acc-group"><h3>${title}</h3>${list.join("")}</div>` : "");
  const port = (d) => ((d.gui || "").match(/:(\d+)\/?$/) || [])[1];
  const fmgs = j.fmgs || (j.fmg ? [{ ...j.fmg, key: "@fmg" }] : []);
  const rows = group(fmgs.length > 1 ? "FortiManagers" : "FortiManager", fmgs.map((f) => row(f, f.key, "direct"))) +
    group("FortiGates", j.fgts.map((f) => row(f, f.name, port(f) ? `via wan-emu :${port(f)}` : "via wan-emu"))) +
    group("LAN clients", (j.clients || []).map((c) => row(c, c.name, `LAN of ${c.spoke}`)));
  $("#wanemu-fgts").innerHTML = rows || '<div class="hint">Nothing deployed yet: deploy the FortiManager and FortiGates first.</div>';
}
// clipboard: the modern API needs https (the tool usually runs on plain http) and may refuse after an await, so fall
// back to execCommand; returns true when copied
async function copyText(t) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(t); return true; } } catch (_) {}
  const a = document.createElement("textarea"); a.value = t; a.setAttribute("readonly", ""); a.style.position = "fixed"; a.style.opacity = "0";
  document.body.appendChild(a); a.focus(); a.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) {}
  a.remove();
  return ok;
}
function loadOnce(tag, attrs) {
  return new Promise((ok, bad) => {
    if (document.querySelector(`${tag}[${tag === "script" ? "src" : "href"}="${attrs.src || attrs.href}"]`)) return ok();
    const e = document.createElement(tag); Object.assign(e, attrs); e.onload = ok; e.onerror = () => bad(new Error("failed to load: " + (attrs.src || attrs.href)));
    document.head.appendChild(e);
  });
}
// sessions: several terminals (SSH via /api/term, Proxmox console via /api/console) in tabs; minimize keeps them open
const SESS = new Map();
let SESS_CUR = null, XTERM_OK = false;
const sessLabel = (n) => (n === "@fmg" ? "FortiManager" : n.startsWith("@fmg:") ? n.slice(5) : n);
function sessTabs() {
  const tab = (s) => `<button data-stab="${esc(s.key)}" class="${s.key === SESS_CUR ? "on" : ""} ${s.closed ? "closed" : ""}">${esc(s.title)}<span class="x" data-sclose="${esc(s.key)}" title="End this session">×</span></button>`;
  $("#sess_tabs").innerHTML = [...SESS.values()].map(tab).join("");
  $("#sess_bar").innerHTML = [...SESS.values()].map((s) => `<button data-stab="${esc(s.key)}" class="${s.closed ? "closed" : ""}">▴ ${esc(s.title)}</button>`).join("");
}
function sessOpen(kind, name, auto) {
  const key = `${kind}:${name}`;
  const old = SESS.get(key);
  if (old && old.closed) sessClose(key);                        // ended (e.g. the CT restarted): connect again
  else if (old) return sessShow(key);                           // the same device again: its existing session
  const el = document.createElement("div"); el.className = "term-pane"; $("#term_box").appendChild(el);
  SESS.set(key, { key, kind, name, el, title: `${kind === "console" ? "Console" : "SSH"} ${sessLabel(name)}`, term: null, ws: null, fit: null, closed: false, needsForm: !auto });
  sessShow(key);
  if (auto) sessStart(SESS.get(key), "", "");                    // the tool logs in with the kept password
}
async function sessStart(s, user, pw) {
  if (!s || s.term) return;
  try {
    if (!XTERM_OK) {
      await loadOnce("link", { rel: "stylesheet", href: XTERM + "css/xterm.css" });
      await loadOnce("script", { src: XTERM + "lib/xterm.js" }); await loadOnce("script", { src: XFIT + "lib/xterm-addon-fit.js" });
      XTERM_OK = true;
    }
  } catch (e) { s.el.innerHTML = `<div class="error">✕ ${esc(e.message)} (does the browser have internet access?)</div>`; return; }
  if (SESS.get(s.key) !== s || s.term) return;                   // closed (or started) while xterm was loading
  const term = s.term = new window.Terminal({ cursorBlink: true, fontSize: 13 });
  s.fit = new window.FitAddon.FitAddon(); term.loadAddon(s.fit); term.open(s.el); s.fit.fit();
  const path = s.kind === "console" ? "/api/console/" : "/api/term/";
  const ws = s.ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}${encodeURIComponent(s.name)}`);
  ws.onopen = () => ws.send(JSON.stringify({ user, password: pw, cols: term.cols, rows: term.rows }));
  ws.onmessage = (e) => term.write(e.data);
  ws.onclose = () => { if (SESS.get(s.key) === s) { term.write("\r\n[connection closed]\r\n"); s.closed = true; sessTabs(); } };
  term.onData((d) => ws.readyState === 1 && ws.send(JSON.stringify({ data: d })));
  term.onResize(({ cols, rows }) => ws.readyState === 1 && ws.send(JSON.stringify({ resize: [cols, rows] })));
  if (SESS_CUR === s.key) term.focus();
}
function sessShow(key) {
  if (!SESS.has(key)) return;
  SESS_CUR = key; $("#sess_panel").hidden = false; $("#sess_bar").hidden = true;
  const cur = SESS.get(key);
  $("#term_form").hidden = !cur.needsForm;                       // the password form belongs to this session only
  if (cur.needsForm) { $("#term_pw").value = ""; $("#term_user").value = cur.kind === "console" && cur.name.startsWith("client-") ? "root" : "admin"; setTimeout(() => $("#term_pw").focus(), 0); }
  for (const s of SESS.values()) s.el.hidden = s.key !== key;
  sessTabs();
  const s = SESS.get(key);
  if (s.fit) { s.fit.fit(); s.term.focus(); }
}
function sessMin() {
  $("#sess_panel").hidden = true; $("#sess_bar").hidden = SESS.size === 0; sessTabs();
}
function sessClose(key) {
  const s = SESS.get(key || SESS_CUR);
  if (!s) return;
  SESS.delete(s.key);                                           // first: onclose of this session no longer redraws
  if (s.ws) { try { s.ws.close(); } catch (_) {} }
  if (s.term) s.term.dispose();
  s.el.remove();
  if (s.needsForm) $("#term_pw").value = "";
  const next = [...SESS.keys()].pop();
  if (next) sessShow(next); else { SESS_CUR = null; $("#sess_panel").hidden = true; $("#sess_bar").hidden = true; }
}
window.addEventListener("resize", () => { const s = SESS.get(SESS_CUR); if (s && s.fit && !$("#sess_panel").hidden) s.fit.fit(); });
function sessGo() {
  const s = SESS.get(SESS_CUR); if (!s || !s.needsForm) return;
  const user = $("#term_user").value.trim() || "admin", pw = $("#term_pw").value;
  $("#term_pw").value = "";                                     // not kept in the page
  s.needsForm = false; $("#term_form").hidden = true;
  sessStart(s, user, pw);
}
const sessClick = (e) => {
  const x = e.target.closest("[data-sclose]");
  if (x) { e.stopPropagation(); return sessClose(x.dataset.sclose); }
  const t = e.target.closest("[data-stab]");
  if (t) sessShow(t.dataset.stab);
};
$("#sess_tabs").addEventListener("click", sessClick);
$("#sess_bar").addEventListener("click", sessClick);
$("#sess_min").onclick = sessMin;
// shared by Access & WAN control and the dashboard panel: copy, copy password, SSH, console
async function accessClick(e, errFn) {
  const cp = e.target.closest("[data-copy]");
  if (cp) {                                                    // IP address or SSH command -> clipboard
    const ok = await copyText(cp.dataset.copy);
    if (!ok) return window.prompt("Copy it with Ctrl/Cmd+C", cp.dataset.copy);
    const txt = cp.textContent;                                 // short confirmation in place (fits the column)
    cp.classList.add("copied"); cp.textContent = "✓ Copied";
    setTimeout(() => { cp.classList.remove("copied"); cp.textContent = txt; }, 1200);
    return;
  }
  const c = e.target.closest("[data-cpw]");
  if (c) {
    const r = await post("/api/access/password", { name: c.dataset.cpw }), j = await json(r);
    if (!r.ok) return errFn(j.detail || "no password kept");
    if (await copyText(j.password)) c.textContent = "✓ Copied";
    else window.prompt(`Admin password (user ${j.user}): copy it with Ctrl/Cmd+C`, j.password);   // browser refused: copy by hand
    setTimeout(() => (c.textContent = "Copy password"), 2000);
    return;
  }
  const b = e.target.closest("[data-ssh]");
  if (b) return sessOpen("ssh", b.dataset.ssh, !!b.dataset.auto);
  const k = e.target.closest("[data-console]");
  if (k) sessOpen("console", k.dataset.console, true);          // the tool logs in itself when it knows the password
}
$("#wanemu-fgts").addEventListener("click", (e) => accessClick(e, wanErr));
$("#term_go").onclick = sessGo;
$("#term_pw").addEventListener("keydown", (e) => { if (e.key === "Enter") sessGo(); });
$("#term_close").onclick = () => sessClose();
async function wanLoad() {
  const r = await fetch("/api/wanemu/links"), j = await r.json().catch(() => ({}));
  if (!r.ok) { wanErr(j.detail || `HTTP ${r.status}`); $("#wanemu-links").innerHTML = ""; return; }
  wanErr(""); WAN = j; $("#wanemu-addr").textContent = `agent ${j.addr}`;
  $("#wanemu-presets").innerHTML = Object.entries(j.presets).map(([k, l]) => `<button data-wp="${k}">${esc(l)}</button>`).join("") + `<button data-wreset="1">All neutral</button>`;
  $("#wanemu-links").innerHTML = Object.entries(WAN_SEG).map(([s, n]) => `<div class="wan-col"><h3>${n}</h3>${j.links.filter((l) => l.seg === s).map(wanCard).join("") || '<div class="hint">no links</div>'}</div>`).join("");
  wanShowDraw();
}
// which FortiGates' link cards are shown (per browser); default only the first spoke
function wanShown() {
  let v = null;
  try { v = JSON.parse(localStorage.getItem("fgt-sdwan-wan-show") || "null"); } catch (_) {}
  if (Array.isArray(v)) return v;
  const first = (WAN.links || []).find((l) => l.seg !== "hub");
  return first ? [first.name] : [];
}
function wanShowSet(v) { try { localStorage.setItem("fgt-sdwan-wan-show", JSON.stringify(v)); } catch (_) {} wanShowDraw(); }
function wanShowDraw() {
  const names = [...new Set((WAN.links || []).map((l) => l.name))], on = wanShown();
  $("#wanemu-show").innerHTML = names.map((n) => `<button class="chip${on.includes(n) ? " on" : ""}" data-wshow="${esc(n)}">${esc(n)}</button>`).join("") +
    `<button data-wshowall="1">Show all</button><button data-wshowall="0">Hide all</button>`;
  document.querySelectorAll("#wanemu-links [data-wl]").forEach((c) => (c.hidden = !on.includes(c.querySelector("b").textContent)));
  document.querySelectorAll("#wanemu-links .wan-col").forEach((col) => {
    const any = [...col.querySelectorAll("[data-wl]")].some((c) => !c.hidden);
    let h = col.querySelector(".wan-none");
    if (!any && !h) { h = document.createElement("div"); h.className = "hint wan-none"; h.textContent = "hidden: pick a FortiGate above"; col.appendChild(h); }
    if (h) h.hidden = any;
  });
}
$("#wanemu-show").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.wshowall) return wanShowSet(b.dataset.wshowall === "1" ? [...new Set(WAN.links.map((l) => l.name))] : []);
  const n = b.dataset.wshow, on = wanShown();
  wanShowSet(on.includes(n) ? on.filter((x) => x !== n) : [...on, n]);
});
function wanCard(l) {
  const st = l.state, rtt = 2 * st.delay_ms;
  return `<div class="card wan-card${st.down ? " down" : ""}" data-wl="${esc(l.id)}"><b>${esc(l.name)}</b> <span class="mono">${esc(l.ip)}</span> <small>${esc(l.id.split("-").pop())}</small>` +
    WAN_F.map(([k, lbl, unit, lo, hi, step]) => `<div class="wan-f"><label>${lbl} <output>${st[k]}</output> ${unit}</label><input type="range" data-wf="${k}" min="${lo}" max="${hi}" step="${step}" value="${st[k]}"></div>`).join("") +
    `<div class="wan-f"><label><input type="checkbox" data-wf="down" ${st.down ? "checked" : ""}> link down</label></div>` +
    `<div class="btnrow"><button class="primary" data-wa="1">Apply</button><span class="hint">RTT +${rtt} ms</span></div></div>`;
}
// WAN writes, shared with the dashboard: true when the agent took it; errFn gets the message (or "")
async function wanPut(id, values, errFn) {
  const r = await fetch("/api/wanemu/links/" + encodeURIComponent(id), { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
  const j = await r.json().catch(() => ({}));
  errFn(r.ok ? "" : j.detail || `HTTP ${r.status}`);
  return r.ok;
}
async function wanPresetRun(name, errFn, link) {
  const r = await fetch(`/api/wanemu/preset/${name}` + (link ? `?link=${encodeURIComponent(link)}` : ""), { method: "POST" });
  const j = await r.json().catch(() => ({}));
  errFn(r.ok ? "" : j.detail || `HTTP ${r.status}`);
  return r.ok;
}
async function wanResetAll(errFn) {
  const r = await fetch("/api/wanemu/reset", { method: "POST" }), j = await r.json().catch(() => ({}));
  errFn(r.ok ? "" : j.detail || `HTTP ${r.status}`);
  return r.ok;
}
async function wanApply(card) {
  const v = {};
  card.querySelectorAll("[data-wf]").forEach((i) => (v[i.dataset.wf] = i.type === "checkbox" ? i.checked : +i.value));
  if (!(await wanPut(card.dataset.wl, v, wanErr))) return;
  card.classList.remove("dirty"); await wanLoad();
  const nc = document.querySelector(`#v_wanemu [data-wl="${CSS.escape(card.dataset.wl)}"]`);   // re-rendered card
  if (nc) { nc.classList.add("wan-ok"); setTimeout(() => nc.classList.remove("wan-ok"), 1500); }
}
async function wanPreset(name, link) {
  if (await wanPresetRun(name, wanErr, link)) await wanLoad();
}
$("#v_wanemu").addEventListener("input", (e) => {
  const i = e.target.closest("[data-wf]"); if (!i) return;
  const card = i.closest("[data-wl]"); card.classList.add("dirty");
  if (i.type === "range") i.previousElementSibling.querySelector("output").textContent = i.value;
});
$("#v_wanemu").addEventListener("change", (e) => {
  const f = e.target.closest("[data-wf]");                     // slider released / "link down" toggled: apply at once
  if (f) wanApply(f.closest("[data-wl]"));
});
$("#v_wanemu").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.wa) return wanApply(b.closest("[data-wl]"));
  if (b.dataset.wp) return wanPreset(b.dataset.wp);
  if (b.dataset.wreset && confirm("Reset all links to neutral (no delay, no loss, everything up)?")) {
    if (await wanResetAll(wanErr)) await wanLoad();
  }
});


// ---- FortiFlex: credentials + configurations (page), licenses at deployment ---------------------------------------
const ffErr = (m) => showErr(m ? [m] : [], "#ff_err");
async function flexApply() {          // deploy page: serial + token fields come from FortiFlex when it is on
  try { FLEX = await (await fetch("/api/fortiflex")).json(); } catch (_) { FLEX = { enabled: false }; }
  flexFields();
}
function flexFields() {               // sync: after draw() and on a scope change; FortiFlex only where a token is needed
  const sc = typeof scope === "function" ? scope() : "all";
  if (document.body.dataset.lic === "files") {   // licence files: serials come from the files, no tokens
    const fromFiles = ["all", "fgt"].includes(sc);              // only where the tool deploys the FortiGates itself
    document.querySelectorAll('#v_deploy [data-f="serial"]').forEach((i) => { i.readOnly = fromFiles; i.placeholder = fromFiles ? "from licence file" : ""; i.classList.toggle("flex", fromFiles); });
    $("#btn_flexcheck").hidden = !["all", "fgt", "fmg"].includes(sc);
    return;
  }
  const fgtOn = !!(FLEX.enabled && FLEX.fgt_config && ["all", "fgt"].includes(sc));
  const fmgOn = !!(FLEX.enabled && FLEX.fmg_config && ["all", "fmg"].includes(sc));
  const set = (i, on) => {
    i.readOnly = on; i.placeholder = on ? "via FortiFlex" : ""; i.classList.toggle("flex", on);
    if (on && i.dataset.f !== "serial") i.value = "";           // a typed serial stays: it is the hint to reuse that license
  };
  document.querySelectorAll('#v_deploy [data-f="serial"], #v_deploy [data-tok]').forEach((i) => set(i, fgtOn));
  set($("#fm_token"), fmgOn);
  $("#btn_flexcheck").hidden = !(fgtOn || fmgOn);
  if (!(fgtOn || fmgOn)) $("#flex_check").hidden = true;
}
$("#btn_flexcheck").onclick = async () => {
  const sc = scope(), mf = ["fgt", "tpl", "dev", "lan"].includes(sc) ? "none" : "day0", mg = ["fmg", "tpl", "dev", "lan"].includes(sc) ? "none" : "day0";
  $("#flex_check").hidden = false; $("#flex_check").innerHTML = '<div class="hint">looking up licences at FortiFlex&hellip;</div>';
  const r = await post("/api/fortiflex/check", { topology: collect(), fmg: fmgCfg(), dry_run: true, scope: sc, bootstrap: { fmg: mf, fgt: mg } });
  const j = await json(r);
  if (!r.ok) { $("#flex_check").innerHTML = `<div class="error">✕ ${esc(errList(j).join(" · "))}</div>`; return; }
  $("#flex_check").innerHTML = j.devices.length ? flexPlanTable(j.devices) : '<div class="hint">This choice needs no FortiFlex licence.</div>';
};
function flexPlanTable(devs) {
  return `<table class="tbl"><thead><tr><th>Device</th><th>Serial number</th><th>Action</th></tr></thead><tbody>` +
    devs.map((d) => `<tr><td>${esc(d.name)}</td><td class="mono">${esc(d.serial)}</td><td>${d.action === "create" ? "<b>" + esc(d.text) + "</b>" : esc(d.text)}</td></tr>`).join("") + "</tbody></table>";
}
function deployStatus(text, plan) {      // the deploy request can take a while (FortiFlex): say what is happening
  const box = $("#deploy_status");
  if (!text) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = `<div class="hint"><span class="dot st-running"></span> ${esc(text)}</div>` + (plan && plan.length ? flexPlanTable(plan) : "");
}
// ---- licences: FortiFlex or licence files (per device, serial from the file name, kept for the next deployments) ----
let LIC = { mode: "fortiflex", files: {} };
async function licMode() {
  try { LIC = await (await fetch("/api/licenses")).json(); } catch (_) { /* keep */ }
  document.body.dataset.lic = LIC.mode;
  const r = document.querySelector(`input[name="lic_mode"][value="${LIC.mode}"]`); if (r) r.checked = true;
  if (typeof flexFields === "function" && T) flexFields();
}
function licDevices() {               // the names as they are in the form now (a renamed device gets its own row)
  let t = T || { fmgr: {}, hubs: [], edges: [] };
  try { t = collect(); } catch (_) { /* form not complete yet */ }
  return [[t.fmgr.name || "fmg", "fmg", "FortiManager"], ...t.hubs.map((d) => [d.name, "fgt", "FortiGate hub"]), ...t.edges.map((d) => [d.name, "fgt", "FortiGate spoke"])];
}
async function licLoad() {
  await licMode();
  const devs = licDevices(), names = new Set(devs.map((d) => d[0]));
  const row = (n, kind, what, m, unused) => `<tr><td><b>${esc(n)}</b> <span class="hint">${esc(what)}</span></td><td class="mono">${m ? esc(m.serial) : '<span class="hint">no file</span>'}</td>` +
    `<td class="mono">${m ? esc(m.file) : ""}</td><td class="hint">${m ? esc(m.uploaded) : ""}</td><td class="actions-cell">` +
    (unused ? "" : `<button data-licup="${esc(n)}" data-kind="${kind}">${m ? "Replace" : "Upload"}</button>`) +
    (m ? `<button class="icon danger" data-licdel="${esc(n)}" title="Delete this licence file">✕</button>` : "") + `</td></tr>`;
  const extra = Object.keys(LIC.files).filter((n) => !names.has(n));
  $("#lic_files").innerHTML = "<thead><tr><th>Device</th><th>Serial</th><th>File</th><th>Uploaded</th><th></th></tr></thead><tbody>" +
    devs.map(([n, k, w]) => row(n, k, w, LIC.files[n])).join("") +
    extra.map((n) => row(n, "", "not in the current topology (kept)", LIC.files[n], true)).join("") + "</tbody>";
}
document.querySelectorAll('input[name="lic_mode"]').forEach((r) => r.addEventListener("change", async () => {
  const res = await fetch("/api/licenses/mode", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: r.value }) });
  if (!res.ok) { const j = await json(res); return showErr([j.detail || "could not change the licensing mode"], "#ff_err"); }
  await licLoad();
}));
let LIC_UP = null;
$("#lic_files").addEventListener("click", async (e) => {
  const u = e.target.closest("[data-licup]");
  if (u) { LIC_UP = { device: u.dataset.licup, kind: u.dataset.kind }; $("#lic_file").value = ""; $("#lic_file").click(); return; }
  const d = e.target.closest("[data-licdel]");
  if (d && confirm(`Delete the licence file of ${d.dataset.licdel}?`)) {
    const r = await fetch("/api/licenses/" + encodeURIComponent(d.dataset.licdel), { method: "DELETE" });
    if (!r.ok) { const j = await json(r); showErr([j.detail || "delete failed"], "#ff_err"); }
    await licLoad();
  }
});
$("#lic_file").addEventListener("change", async () => {
  const f = $("#lic_file").files[0]; if (!f || !LIC_UP) return;
  const fd = new FormData(); fd.append("file", f, f.name);
  const r = await fetch(`/api/licenses/${encodeURIComponent(LIC_UP.device)}?kind=${LIC_UP.kind}`, { method: "POST", body: fd }), j = await json(r);
  showErr(r.ok ? [] : [j.detail || "upload failed"], "#ff_err");
  await licLoad();
});

async function ffLoad() {
  ffErr("");
  FLEX = await (await fetch("/api/fortiflex")).json();
  $("#ff_user").value = FLEX.user; $("#ff_pw").value = ""; $("#ff_pw").placeholder = FLEX.has_password ? "(saved; leave empty = keep)" : "";
  $("#ff_enabled").checked = FLEX.enabled; $("#ff_folder").value = FLEX.folder || "";
  $("#ff_program").innerHTML = FLEX.program ? `<option>${esc(FLEX.program)}</option>` : "";
  if (FLEX.user && FLEX.has_password) { await ffPrograms(); await ffList(); }
}
async function ffPrograms(creds) {
  const r = await post("/api/fortiflex/test", creds || {}), j = await json(r);
  if (!r.ok) { $("#ff_res").textContent = ""; return ffErr(errList(j).join(" · ")); }
  ffErr(""); $("#ff_res").textContent = `✓ logged in, ${j.programs.length} program(s)`;
  setSel("#ff_program", j.programs.map((p) => p.serialNumber), FLEX.program || (j.programs[0] || {}).serialNumber);
  await ffConfigs(); await ffFolders();
}
async function ffFolders() {           // Asset Management folders as suggestions; typing a path stays possible
  const prog = $("#ff_program").value; if (!prog) return;
  const r = await post("/api/fortiflex/folders", { program: prog, user: $("#ff_user").value.trim(), password: $("#ff_pw").value }), j = await json(r);
  if (!r.ok) { $("#ff_folders").innerHTML = ""; $("#ff_res").textContent += " · asset folders not readable: " + errList(j).join(" "); return; }
  $("#ff_folders").innerHTML = j.folders.map((f) => `<option value="${esc(f.path)}">`).join("");
}
let FF_CONFIGS = [];
async function ffConfigs() {
  const prog = $("#ff_program").value; if (!prog) return;
  const r = await post("/api/fortiflex/configs", { program: prog, user: $("#ff_user").value.trim(), password: $("#ff_pw").value }), j = await json(r);
  if (!r.ok) return ffErr(errList(j).join(" · "));
  FF_CONFIGS = j;
  const opts = (want, cur) => '<option value="">(none)</option>' + j.filter((c) => c.product.includes(want)).map((c) => `<option value="${c.id}"${c.id === cur ? " selected" : ""}>${esc(c.name)} (${esc(c.status || "")})</option>`).join("");
  $("#ff_fmg").innerHTML = opts("FortiManager", FLEX.fmg_config); $("#ff_fgt").innerHTML = opts("FortiGate", FLEX.fgt_config);
  ffParams();
}
function ffParams() {
  const show = (id, lbl) => { const c = FF_CONFIGS.find((x) => String(x.id) === $(id).value); return c ? `${lbl}: ` + (Object.entries(c.parameters).map(([k, v]) => `${k} ${v}`).join(", ") || "no parameters") : ""; };
  $("#ff_params").textContent = [show("#ff_fmg", "FortiManager"), show("#ff_fgt", "FortiGates")].filter(Boolean).join(" · ");
}
async function ffSave() {
  const n = (v) => (v ? +v : null);
  const body = { user: $("#ff_user").value.trim(), password: $("#ff_pw").value, enabled: $("#ff_enabled").checked, program: $("#ff_program").value || "", fmg_config: n($("#ff_fmg").value), fgt_config: n($("#ff_fgt").value), folder: $("#ff_folder").value.trim() };
  if (body.enabled && !body.fgt_config && !body.fmg_config) return ffErr("Choose at least one configuration before turning FortiFlex on");
  const r = await fetch("/api/fortiflex", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await json(r);
  if (!r.ok) return ffErr(errList(j).join(" · "));
  FLEX = j; $("#ff_pw").value = ""; $("#ff_pw").placeholder = j.has_password ? "(saved; leave empty = keep)" : "";
  ffErr(""); $("#ff_res").textContent = "✓ saved"; await ffList();
}
async function ffList() {
  const r = await fetch("/api/fortiflex/entitlements"), j = await json(r);
  if (!r.ok) { $("#ff_list").innerHTML = ""; return ffErr(errList(j).join(" · ")); }
  $("#ff_list").innerHTML = j.entitlements.length ? `<table class="tbl"><thead><tr><th>Description</th><th>Serial number</th><th>Status</th><th>Token</th><th>End date</th><th></th></tr></thead><tbody>` +
    j.entitlements.map((e) => `<tr><td>${esc(e.description)}</td><td class="mono">${esc(e.serialNumber)}</td><td>${esc(e.status)}</td><td>${esc(e.tokenStatus || "")}</td><td>${esc((e.endDate || "").slice(0, 10))}</td><td>${e.status === "STOPPED" || e.status === "EXPIRED" ? "" : `<button data-ffstop="${esc(e.serialNumber)}">Stop</button>`}</td></tr>`).join("") + "</tbody></table>"
    : '<div class="hint">No licences of this tool in the chosen configurations yet.</div>';
}
$("#ff_test").onclick = () => ffPrograms({ user: $("#ff_user").value.trim(), password: $("#ff_pw").value });
$("#ff_program").onchange = async () => { await ffConfigs(); await ffFolders(); };
$("#ff_fmg").onchange = ffParams; $("#ff_fgt").onchange = ffParams;
$("#ff_save").onclick = ffSave;
$("#ff_list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-ffstop]"); if (!b) return;
  if (!confirm(`Stop licence ${b.dataset.ffstop}? The FortiGate/FortiManager with this serial number loses its licence.`)) return;
  const r = await fetch("/api/fortiflex/stop/" + encodeURIComponent(b.dataset.ffstop), { method: "POST" }), j = await json(r);
  if (!r.ok) return ffErr(errList(j).join(" · "));
  await ffList();
});


// ---- topology picture: what this deployment builds (from the form; parts outside the chosen scope are dimmed) -----
function topoSvg(t, sc) {
  const w = t.wanemu, seg = w.segments, fgts = [...t.hubs.map((d) => [d, true]), ...t.edges.map((d) => [d, false])];
  const L = 150, COL = 180, W = Math.max(900, L + fgts.length * COL + 20);
  const links = wanLinks(t), lk = (name, wan) => links.find((x) => x.id === `${name}-${wan}`);
  const range = (s) => { const v = links.filter((x) => x.seg === s).map((x) => x.vlan); return v.length ? (v.length > 1 ? `${Math.min(...v)}–${Math.max(...v)}` : `${v[0]}`) : ""; };
  const fgtOn = ["all", "fgt"].includes(sc), modelOnly = ["dev", "tpl"].includes(sc), fmgNew = ["all", "fmg"].includes(sc), lanOnly = sc === "lan";
  const dim = (on) => (on ? "" : ' opacity="0.35"');
  const box = (x, y, bw, bh, title, lines, cls, on = true) =>
    `<g${dim(on)}><rect x="${x}" y="${y}" width="${bw}" height="${bh}" rx="7" class="tp-box ${cls || ""}"/>` +
    `<text x="${x + 8}" y="${y + 17}" class="tp-title">${esc(title)}</text>` +
    lines.map((l, i) => `<text x="${x + 8}" y="${y + 33 + i * 14}" class="tp-txt">${esc(l)}</text>`).join("") + "</g>";
  const line = (x1, y1, x2, y2, on = true, dash) => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" class="tp-line"${dash ? ' stroke-dasharray="4 3"' : ""}${dim(on)}/>`;
  const bar = (y, name, vlan, subnet, on) => `<g${dim(on)}><line x1="${L}" y1="${y}" x2="${W - 10}" y2="${y}" class="tp-bar"/>` +
    `<text x="10" y="${y - 3}" class="tp-title">${esc(name)}</text><text x="10" y="${y + 11}" class="tp-txt">${vlan ? "VLAN " + vlan : "untagged"} · ${esc(subnet || "(not set)")}</text></g>`;
  // an ISP: one /24 + VLAN per FortiGate WAN, all routed by the wan-emu (the bar shows the block and the VLAN range)
  const isp = (y, s, on) => bar(y, seg[s].name, range(s), seg[s].block, on);
  const yLab = 92, yHub = 170, yA = 205, yB = 240, yF = 290, yLan = 395, yCli = 470;
  let o = "";
  o += bar(yLab, "Lab / uplink", w.uplink.vlan, w.uplink.ip.replace(/\.\d+\/(\d+)$/, ".0/$1"), true);
  o += box(L, 14, 200, 56, "FortiManager", [`${(t.fmgr.ip || "").split("/")[0] || "(IP)"}`, fmgNew ? "new VM (Day-0)" : "existing"], "", !lanOnly) + line(L + 100, 70, L + 100, yLab);
  o += box(L + 230, 14, 230, 56, "wan-emu (LXC)", [`${w.uplink.ip.split("/")[0] || "(uplink IP)"} · NAT, DHCP, tc`, "forwards :" + w.fwd_https_base + "+ / :" + w.fwd_ssh_base + "+"], "tp-emu", fgtOn) + line(L + 345, 70, L + 345, yLab, fgtOn);
  o += line(L + 345, yLab, L + 345, yB, fgtOn);
  o += isp(yHub, "hub", fgtOn) + isp(yA, "a", fgtOn) + isp(yB, "b", fgtOn);
  fgts.forEach(([d, hub], n) => {
    const x = L + 10 + n * COL, cx = x + 80, ei = n - t.hubs.length;
    const l1 = lk(d.name, "wan1"), l2 = !hub && d.wan2_intf ? lk(d.name, "wan2") : null;
    const wan1 = l1 ? l1.ip : "(no address)", wan2 = l2 ? l2.ip : null;
    // the VLAN of each WAN line next to its drop (wan1 left of the line, wan2 right of it)
    const vlab = (x, v, end) => `<text x="${x + (end ? -4 : 4)}" y="${yF - 6}" class="tp-txt"${end ? ' text-anchor="end"' : ""}${dim(fgtOn)}>VLAN ${v}</text>`;
    const lan = (d.lans || [])[0] || {}, vlan = w.lan_vlan_base + 1 + n;
    o += line(cx - 20, yF, cx - 20, hub ? yHub : yA, fgtOn) + (l1 ? vlab(cx - 20, l1.vlan, true) : "");
    if (wan2) o += line(cx + 20, yF, cx + 20, yB, fgtOn) + vlab(cx + 20, l2.vlan, false);
    o += box(x, yF, 160, 72, `${d.name || "?"} · ${hub ? "hub" : "spoke"}`, [`WAN1 ${wan1}`, wan2 ? `WAN2 ${wan2}` : hub ? "gw " + (l1 ? l1.gw : "") : "no WAN2", modelOnly ? "model device" : `Lo ${d.loopback || ""}`], hub ? "tp-hub" : "tp-spoke", fgtOn || modelOnly);
    if (lan.ip) {
      o += line(cx, yF + 72, cx, yLan, fgtOn || modelOnly);
      o += box(x, yLan, 160, 50, `LAN ${lan.intf || ""}`, [lan.ip, `VLAN ${vlan}`], "tp-lan", fgtOn || modelOnly || lanOnly);
      if (!hub) {
        const ip = lan.ip.split("/")[0].split(".").slice(0, 3).join(".") + ".10";
        const tgHere = t.trafficgen && t.trafficgen.enabled && d === t.edges.find((e) => (e.lans || [])[0] && e.lans[0].ip);
        const pre = ip.split(".").slice(0, 3).join(".");
        o += line(cx, yLan + 50, cx, yCli, fgtOn || lanOnly) + box(x, yCli, 160, 46, `client-${d.name}`, tgHere ? [`${ip} (LXC)`, `+ Traffic-Gen ${pre}.20-.29`] : [`${ip} (LXC)`], "tp-cli", fgtOn || lanOnly);
      }
    }
  });
  o += `<text x="${L}" y="${yCli + 70}" class="tp-txt">IPsec overlay: every spoke WAN → WAN1 of every hub · BGP on loopbacks · ${t.region.multi_vrf === false ? "single VRF (VRF 0)" : "multi-VRF"}</text>`;
  return `<svg viewBox="0 0 ${W} ${yCli + 84}" width="100%" role="img" aria-label="Topology" xmlns="http://www.w3.org/2000/svg">${o}</svg>`;
}
// FortiGate ports whose VLAN the tool sets itself (same rules as provision_vms.fgt_nics): hub wan1 -> ISP-HUB, spoke
// wan1/wan2 -> ISP-A/ISP-B, LAN1/LAN2 -> lan(2)_vlan_base+1+n. {port: {txt: [...], all: true when every FortiGate}}
function portRoles(t) {
  const w = t.wanemu, seg = w.segments, fgts = [...t.hubs.map((d) => [d, true]), ...t.edges.map((d) => [d, false])], out = {};
  const add = (port, key, txt) => { const o = (out[port] ||= { m: new Map(), n: new Set() }); if (!o.m.has(key)) o.m.set(key, []); o.m.get(key).push(txt); };
  fgts.forEach(([d, hub], n) => {
    const L = wanLinks(t), vl = (w) => (L.find((x) => x.id === `${d.name}-${w}`) || {}).vlan;
    const wans = hub ? { [d.wan1_intf]: ["hub", vl("wan1")] } : { [d.wan1_intf]: ["a", vl("wan1")], ...(d.wan2_intf ? { [d.wan2_intf]: ["b", vl("wan2")] } : {}) };
    Object.entries(wans).forEach(([p, [s, v]]) => { if (p && p !== "undefined" && v) { add(p, s, v); out[p].n.add(n); } });
    (d.lans || []).slice(0, 2).forEach((l, k) => {
      if (l.intf && !(l.intf in wans)) { add(l.intf, "lan" + (k + 1), (k ? w.lan2_vlan_base : w.lan_vlan_base) + 1 + n); out[l.intf].n.add(n); }
    });
  });
  const name = { hub: `${seg.hub.name} (hubs)`, a: `${seg.a.name} (spokes)`, b: `${seg.b.name} (spokes)`, lan1: "LAN1", lan2: "LAN2" };
  return Object.fromEntries(Object.entries(out).map(([p, o]) => [p, {
    txt: [...o.m].map(([k, v]) => { const u = [...new Set(v)], lo = Math.min(...u), hi = Math.max(...u); return `${name[k]} VLAN ${lo === hi ? lo : lo + "–" + hi}`; }),
    all: o.n.size === fgts.length,
  }]));
}
function portsFixed() {
  let r = {}; try { r = portRoles(collect()); } catch (e) { /* form not complete yet */ }
  document.querySelectorAll("#ports [data-p]").forEach((tr) => {
    const f = r[tr.dataset.p], cell = tr.querySelector("[data-fix]");
    cell.textContent = f ? f.txt.join(" · ") + (f.all ? "" : " · other FortiGates: as set here") : "";
    tr.querySelectorAll("select, input").forEach((el) => { el.disabled = !!(f && f.all); el.title = f && f.all ? "set by the tool: " + f.txt.join(", ") : ""; });
  });
}
let TOPO_T = null;
function topoDraw() {
  clearTimeout(TOPO_T);
  TOPO_T = setTimeout(() => { try { $("#topo").innerHTML = topoSvg(collect(), scope()); } catch (e) { $("#topo").innerHTML = ""; } portsFixed(); }, 150);
}
$("#v_deploy").addEventListener("input", (e) => { if (!e.target.closest("#topo")) topoDraw(); });


// ---- cleanup: everything the tool deployed on Proxmox (never the deploy container) ---------------------------------
const clErr = (m) => showErr(m ? [m] : [], "#cl_err");
async function clLoad() {
  clErr(""); $("#cl_list").innerHTML = ""; $("#cl_res").innerHTML = ""; $("#cl_confirm").value = "";
  setSel("#cl_server", SERVERS.map((x) => String(x.id)), $("#pve_server").value || (SERVERS[0] ? String(SERVERS[0].id) : ""));
  $("#cl_server").querySelectorAll("option").forEach((o) => { const x = SERVERS.find((s) => String(s.id) === o.value); if (x) o.textContent = x.name; });
  if (!SERVERS.length) return clErr("No Proxmox server saved yet (Servers page)");
  await clNodes();
}
async function clNodes() {
  const sid = $("#cl_server").value; if (!sid) return;
  const r = await post(`/api/servers/${sid}/test`, {}), j = await json(r);
  if (!r.ok) return clErr(errList(j).join(" · "));
  setSel("#cl_node", j.nodes, $("#pve_node").value || j.nodes[0]);
}
let CL_SHOWN = null;
async function clShow() {
  clErr(""); $("#cl_list").innerHTML = '<div class="hint">looking at Proxmox…</div>'; CL_SHOWN = null;
  const r = await post("/api/cleanup/plan", { server_id: +$("#cl_server").value, node: $("#cl_node").value, stop_licences: $("#cl_licences").checked }), j = await json(r);
  if (!r.ok) { $("#cl_list").innerHTML = ""; clErr(errList(j).join(" · ")); return null; }
  const kind = { vm: "VM", ct: "Container", file: "File" };
  $("#cl_list").innerHTML = (j.items.length ? `<table class="tbl"><thead><tr><th>Type</th><th>ID</th><th>Name</th><th>Status</th></tr></thead><tbody>` +
    j.items.map((i) => `<tr><td>${kind[i.kind]}</td><td class="mono">${i.vmid ?? ""}</td><td class="mono">${esc(i.name)}</td><td>${esc(i.status || "")}</td></tr>`).join("") + "</tbody></table>"
    : '<div class="hint">Nothing of this tool found on this node.</div>') +
    (j.licences.length ? `<div class="hint">FortiFlex licences that will be stopped: ${j.licences.map((l) => esc(`${l.serialNumber} (${l.description})`)).join(", ")}</div>` : "");
  CL_SHOWN = j;
  return j;
}
async function clRun() {
  clErr(""); $("#cl_res").innerHTML = "";
  if ($("#cl_confirm").value !== "DELETE") return clErr("Type DELETE to confirm");
  const j = await clShow(); if (!j) return;
  if (!j.items.length && !j.licences.length) return clErr("Nothing to delete on this node");
  if (!confirm(`Delete ${j.items.length} item(s) on ${$("#cl_node").value}` + (j.licences.length ? ` and stop ${j.licences.length} licence(s)` : "") + "? This cannot be undone.")) return;
  const btn = $("#cl_run"); btn.disabled = true; $("#cl_res").innerHTML = '<div class="hint">deleting… (stopping and deleting VMs can take a minute)</div>';
  try {
    const r = await post("/api/cleanup", { server_id: +$("#cl_server").value, node: $("#cl_node").value, confirm: $("#cl_confirm").value,
      stop_licences: $("#cl_licences").checked, only: j.items.map((i) => i.name) }), res = await json(r);
    if (!r.ok) { $("#cl_res").innerHTML = ""; return clErr(errList(res).join(" · ")); }
    const bad = res.results.filter((x) => !x.ok).length;
    $("#cl_res").innerHTML = `<div class="${bad ? "error" : "ok"}">${bad ? `✕ ${bad} item(s) failed` : "✔ Everything deleted"}</div><table class="tbl"><tbody>` +
      res.results.map((x) => `<tr><td>${x.ok ? "✔" : "✕"}</td><td class="mono">${esc(x.name)}</td><td>${esc(x.error || "")}</td></tr>`).join("") + "</tbody></table>";
    await clShow();
  } catch (e) {
    clErr("The request was interrupted (" + e.message + "); deleting may still be going on: press 'Show what will be deleted' again in a minute");
  } finally {
    btn.disabled = false; $("#cl_confirm").value = "";
  }
}
$("#cl_licences").addEventListener("change", () => { if (CL_SHOWN) clShow(); });
$("#cl_server").addEventListener("change", clNodes);
$("#cl_show").onclick = clShow;
$("#cl_run").onclick = clRun;


// ---- theme: light / dark, remembered per browser (otherwise the system setting, applied in index.html) ------------
function themeSet(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("fgt-sdwan-theme", t); } catch (_) {}
  $("#theme_btn").title = t === "dark" ? "Light mode" : "Dark mode";
}
$("#theme_btn").title = document.documentElement.dataset.theme === "dark" ? "Light mode" : "Dark mode";
$("#theme_btn").onclick = () => themeSet(document.documentElement.dataset.theme === "dark" ? "light" : "dark");


// ---- Traffic tab: the deployed Traffic-Gen through the tool (only the traffic test) --------------------------------
let TR_TIMER = null, TR_CFG = null, TR_ON = false;          // TR_ON: a reachable Traffic-Gen is deployed
const TR_GROUPS = [["office365", "Microsoft 365"], ["collab", "Collaboration"], ["streaming", "Streaming"], ["social", "Social"],
  ["saas", "SaaS"], ["web", "Web"], ["updates", "Updates"], ["download", "Downloads"], ["sdwan", "SD-WAN probes"],
  ["webfilter", "Web filter test"], ["malware", "Malware test"], ["ioc", "IOC / botnet"]];
const TR_SEC = ["webfilter", "malware", "ioc"];
const TR_DEF = { office365: 25, collab: 20, streaming: 20, social: 10, saas: 10, web: 10, updates: 3, download: 2, sdwan: 5, webfilter: 0, malware: 0, ioc: 0 };
const trErr = (m) => { $("#tr_err").innerHTML = m ? `<div class="error">✕ ${esc(m)}</div>` : ""; };
const trMB = (b) => ((b || 0) / 1e6).toFixed(1);
function trMixDraw(groups) {
  $("#tr_mix").innerHTML = TR_GROUPS.map(([g, l]) => `<div class="wan-f"><label>${l} <output>${groups[g] ?? 0}</output></label><input type="range" min="0" max="100" step="1" data-trg="${g}" value="${groups[g] ?? 0}"></div>`).join("");
}
const trMix = () => Object.fromEntries([...document.querySelectorAll("[data-trg]")].map((i) => [i.dataset.trg, +i.value]));
$("#tr_mix").addEventListener("input", (e) => { if (e.target.dataset.trg) e.target.previousElementSibling.querySelector("output").textContent = e.target.value; });
document.querySelectorAll("[data-trp]").forEach((b) => (b.onclick = () => {
  const m = trMix(), k = b.dataset.trp;
  if (k === "default") trMixDraw(TR_DEF);
  else if (k === "nosec") trMixDraw(Object.fromEntries(Object.entries(m).map(([g, w]) => [g, TR_SEC.includes(g) ? 0 : w])));
  else trMixDraw(Object.fromEntries(Object.entries(m).map(([g, w]) => [g, TR_SEC.includes(g) ? (w || 10) : 0])));
  $("#tr_mixmsg").textContent = "Mix changed: click Save mix or start a test to use it.";
}));
async function trOpen() {
  trErr("");
  const r = await fetch("/api/traffic/status"), j = await json(r);
  const off = !j.deployed;
  $("#tr_where").innerHTML = off ? "" : `(<a href="${esc(j.url)}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(j.url.replace(/^https?:\/\//, ""))}</a>, its own GUI)`;
  TR_ON = !off && j.reachable;
  ["#tr_start", "#tr_stop", "#tr_save"].forEach((id) => ($(id).disabled = !TR_ON));
  if (off) { trErr("No Traffic-Gen deployed yet: on the Deploy page tick 'Also deploy Traffic-Gen' (card LAN clients) and deploy."); trMixDraw(TR_DEF); return; }
  if (!j.reachable) { trErr(j.error); return; }
  const c = await (await fetch("/api/traffic/config")).json();
  TR_CFG = c;
  $("#tr_dur").value = [5, 10, 30, 60, 120, 0].includes(c.duration_minutes) ? c.duration_minutes : 10;
  $("#tr_int").value = c.intensity || "medium"; $("#tr_thr").value = c.threads_per_ip || 2;
  trMixDraw(c.groups || TR_DEF);
  trShow(j.state);
}
async function trPoll() {
  if (!TR_ON) return;
  let r;
  try { r = await fetch("/api/traffic/state"); } catch (_) { return trErr("The tool is not reachable (restarting?)"); }
  const j = await json(r);
  if (!r.ok) return trErr(typeof j.detail === "string" ? j.detail : "Traffic-Gen not reachable");
  trErr(""); trShow(j);
  const l = await fetch("/api/traffic/log?n=25").catch(() => null);
  if (l && l.ok) trLogDraw(await l.json());
}
function trShow(s) {
  if (!s) return;
  const t = s.totals || {}, run = !!s.running;
  $("#tr_start").disabled = run; $("#tr_stop").disabled = !run;
  const left = run && s.deadline ? Math.max(0, Math.round((s.deadline - s.now) / 60)) : null;
  $("#tr_run").innerHTML = run ? `<span class="dot st-running"></span> Running from ${s.sources || "?"} source IP(s), intensity ${esc(s.intensity || "")}` + (left !== null ? `, ${left} min left` : ", until stopped")
    : s.has_run ? "Stopped. The figures below are from the last run." : "Not started yet.";
  const ser = s.series || [], a = ser[ser.length - 1], b = ser[Math.max(0, ser.length - 6)];
  const rps = a && b && a[0] > b[0] ? ((a[1] - b[1]) / (a[0] - b[0])).toFixed(1) : "0";
  const mbps = a && b && a[0] > b[0] ? (((a[2] - b[2]) * 8) / 1e6 / (a[0] - b[0])).toFixed(2) : "0";
  const tile = (v, l) => `<div class="tr-tile"><b>${v}</b><span>${l}</span></div>`;
  $("#tr_tiles").innerHTML = tile(t.requests || 0, "requests") + tile(t.ok || 0, "ok") + tile(t.blocked || 0, "blocked") +
    tile(t.error || 0, "errors") + tile(rps, "req/s") + tile(mbps, "Mbit/s") + tile(trMB(t.bytes), "MB total");
  const rows = (o) => Object.entries(o || {}).sort((x, y) => (y[1].requests || 0) - (x[1].requests || 0));
  const tbl = (o, first) => rows(o).length ? `<table class="tbl"><thead><tr><th>${first}</th><th>Requests</th><th>OK</th><th>Blocked</th><th>Errors</th><th>MB</th></tr></thead><tbody>` +
    rows(o).slice(0, 25).map(([k, c]) => `<tr><td class="mono">${esc(k)}</td><td>${c.requests || 0}</td><td>${c.ok || 0}</td><td>${c.blocked || 0}</td><td>${(c.error || 0) + (c.missed || 0)}</td><td>${trMB(c.bytes)}</td></tr>`).join("") + "</tbody></table>"
    : '<p class="muted">No traffic yet.</p>';
  $("#tr_apps").innerHTML = tbl(s.apps, "Application"); $("#tr_src").innerHTML = tbl(s.src, "Source IP");
}
function trLogDraw(l) {
  $("#tr_log").innerHTML = l.length ? `<table class="tbl"><thead><tr><th>Time</th><th>Source</th><th>Application</th><th>Target</th><th>Result</th></tr></thead><tbody>` +
    l.map((x) => `<tr><td class="mono">${esc(x.ts || "")}</td><td class="mono">${esc(x.src || "")}</td><td>${esc(x.app || "")}</td><td class="mono tr-url">${esc(x.url || "")}</td><td>${esc(x.result || "")}${x.status ? " " + esc(String(x.status)) : ""}</td></tr>`).join("") + "</tbody></table>"
    : '<p class="muted">No requests yet.</p>';
}
const trBody = () => ({ duration_minutes: +$("#tr_dur").value, intensity: $("#tr_int").value, threads_per_ip: Math.min(20, Math.max(1, +$("#tr_thr").value || 2)), groups: trMix() });
$("#tr_start").onclick = async () => {
  if (!Object.values(trMix()).some((w) => w > 0)) return trErr("Every group is at 0: give at least one group a weight.");
  $("#tr_start").disabled = true;
  const r = await post("/api/traffic/start", trBody()), j = await json(r);
  if (!r.ok) { $("#tr_start").disabled = false; return trErr(typeof j.detail === "string" ? j.detail : "start failed"); }
  trErr(""); $("#tr_mixmsg").textContent = ""; trPoll();
};
$("#tr_stop").onclick = async () => { const r = await post("/api/traffic/stop", {}); if (!r.ok) trErr("stop failed"); trPoll(); };
$("#tr_save").onclick = async () => {
  const r = await post("/api/traffic/config", trBody()), j = await json(r);
  $("#tr_mixmsg").textContent = r.ok ? "✓ Mix saved in Traffic-Gen" : "✕ " + (j.detail || "save failed");
};
