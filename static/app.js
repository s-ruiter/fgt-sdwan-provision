"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
let T = null, IMGS = [], STEPS = [], ES = null, CUR_JOB = null;

// ---- helpers ----------------------------------------------------------------
const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
async function json(r) { try { return await r.json(); } catch (_) { return {}; } }
function errList(j) { const e = j.detail || j.errors || []; return (Array.isArray(e) ? e : [e]).map((x) => x.msg || String(x)); }
function showErr(list, target = "#errors") {
  $(target).innerHTML = list.length ? `<div class="error">${list.map((x) => "✕ " + esc(x)).join("<br>")}</div>` : "";
  if (list.length) window.scrollTo({ top: 0, behavior: "smooth" });
}
const fld = (k, l, v, extra = "", cls = "") => `<div class="f${cls ? " " + cls : ""}"><label>${l}</label><input data-f="${k}" value="${esc(v)}" ${extra}></div>`;
const fmtBytes = (n) => (n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB");

// ---- routing ----------------------------------------------------------------
function route() {
  const [r, arg] = (location.hash.replace(/^#\//, "") || "deploy").split("?")[0].split("/");
  const VIEWS = ["deploy", "progress", "wanemu", "fortiflex", "images", "servers", "about"];
  const view = VIEWS.includes(r) ? r : "deploy";
  for (const v of VIEWS) $("#v_" + v).hidden = v !== view;
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === view));
  window.scrollTo(0, 0);
  if (view === "progress") progressOpen(arg);
  if (view === "deploy") { bannerLoad(); flexApply(); }
  if (view === "fortiflex") ffLoad();
  clearInterval(WAN_TIMER); WAN_TIMER = null;
  if (view === "wanemu") { wanLoad(); wanFwdLoad(); WAN_TIMER = setInterval(() => { if (!document.querySelector("#v_wanemu .dirty")) wanLoad(); }, 10000); }
}
window.addEventListener("hashchange", route);
let WAN_TIMER = null, WAN = { links: [], presets: {} }, FLEX = { enabled: false };

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
  if (!v) return "";
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
const KIND_BY_ID = { fm_ip: "cidr", fm_gw: "ip" };
const KIND_BY_FIELD = { wan1_ip: "cidr", wan1_gw: "ip", loopback: "ip" };
function markIpFields() {
  for (const [id, k] of Object.entries(KIND_BY_ID)) $("#" + id).dataset.ip = k;
  if (["fgt", "tpl", "dev"].includes(scope())) $("#fm_ip").dataset.ip = "ipcidr";   // existing FortiManager: only its address is used
  document.querySelectorAll(".dev [data-f]").forEach((el) => {
    const f = el.dataset.f, k = KIND_BY_FIELD[f] || (/^lan\d+_ip$/.test(f) ? "cidr" : null);
    if (k) el.dataset.ip = k;
  });
}
function checkIp(el) {
  let v = el.value.trim();
  const m = v.match(/^(.+)\/(\d+\.\d+\.\d+\.\d+)$/);   // 10.0.0.5/255.255.255.0 -> 10.0.0.5/24
  if (el.dataset.ip === "cidr" && m && maskToPrefix(m[2]) !== null) { v = `${m[1]}/${maskToPrefix(m[2])}`; el.value = v; }
  const p = ipProblem(el.dataset.ip, v);
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
    if (el.closest(".only-vm, .only-fmg, .only-fgt, .only-dev") && !el.offsetParent) { el.classList.remove("bad"); return; }   // hidden by the deploy choice
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
  setSel("#fmg_br", PVE_OPT.bridges, p.fmg.bridge, "(standaard bridge)");
  $("#pve_cpu").value = p.cpu_type;
  $("#fgt_cores").value = p.fgt.cores; $("#fgt_mem").value = p.fgt.memory_mb;
  $("#fmg_cores").value = p.fmg.cores; $("#fmg_mem").value = p.fmg.memory_mb; $("#fmg_disk").value = p.fmg.data_disk_gb; $("#fmg_vlan").value = p.fmg.vlan ?? "";
  const opt = (v) => `<option value="">(standaard bridge)</option>` + [...new Set([...PVE_OPT.bridges, ...(v ? [v] : [])])].map((x) => `<option${x === v ? " selected" : ""}>${esc(x)}</option>`).join("");
  $("#ports").innerHTML = "<thead><tr><th>Port</th><th>Bridge</th><th>VLAN</th></tr></thead><tbody>" +
    Object.entries(p.ports).map(([k, v]) => `<tr data-p="${k}"><td class="mono">${k}</td><td><select data-f="bridge">${opt(v.bridge)}</select></td><td><input data-f="vlan" type="number" value="${v.vlan ?? ""}"></td></tr>`).join("") + "</tbody>";
  if (T && T.wanemu) setSel("#wan-bridge", PVE_OPT.bridges, $("#wan-bridge").value || T.wanemu.bridge);   // bridges of this node
}
const WAN_SEGS = ["hub", "a", "b"];
function wanFill(w) {
  $("#wan-vmid").value = w.vmid ?? ""; $("#wan-template").value = w.template; $("#wan-storage").value = w.storage;
  setSel("#wan-bridge", PVE_OPT.bridges, w.bridge);
  $("#wan-uplink-vlan").value = w.uplink.vlan; $("#wan-uplink-ip").value = w.uplink.ip; $("#wan-uplink-gw").value = w.uplink.gw;
  WAN_SEGS.forEach((k) => {
    const g = w.segments[k];
    $(`#wan-seg-${k}-vlan`).value = g.vlan; $(`#wan-seg-${k}-subnet`).value = g.subnet; $(`#wan-seg-${k}-gw`).value = g.gw;
    if (k !== "hub") $(`#wan-seg-${k}-res`).value = g.res_start;
  });
}
function wanCollect() {
  const w = JSON.parse(JSON.stringify(T.wanemu)), n = (v) => (v === "" ? null : +v);
  w.vmid = n($("#wan-vmid").value); w.template = $("#wan-template").value.trim(); w.storage = $("#wan-storage").value.trim();
  w.bridge = $("#wan-bridge").value.trim();
  w.uplink = { vlan: n($("#wan-uplink-vlan").value), ip: $("#wan-uplink-ip").value.trim(), gw: $("#wan-uplink-gw").value.trim() };
  WAN_SEGS.forEach((k) => {
    Object.assign(w.segments[k], { vlan: n($(`#wan-seg-${k}-vlan`).value), subnet: $(`#wan-seg-${k}-subnet`).value.trim(), gw: $(`#wan-seg-${k}-gw`).value.trim() });
    if (k !== "hub") w.segments[k].res_start = n($(`#wan-seg-${k}-res`).value);
  });
  return w;
}
function dev(kind, d, i) {
  const hub = kind === "hubs";
  let h = `<div class="dev" data-k="${kind}" data-i="${i}"><b>${esc(d.name)}</b><div class="fields">`;
  h += fld("name", "Name", d.name) + fld("serial", "Serial number", d.serial, "", "only-dev") + fld("loopback", "Loopback (BGP, router-id)", d.loopback);
  // WAN design: hub = one wan (wan1, always static); branch = wan1 + optional wan2, both DHCP, each with a tunnel to wan1 of every hub
  if (hub) h += fld("wan1_intf", "wan1 port (ZTP)", d.wan1_intf) + fld("wan1_ip", "wan1 IP/prefix (static)", d.wan1_ip) + fld("wan1_gw", "wan1 gateway", d.wan1_gw);
  else h += fld("wan1_intf", "wan1 port (ZTP, DHCP)", d.wan1_intf) + fld("wan2_intf", "wan2 port (DHCP; empty = none)", d.wan2_intf);
  h += `<div class="f only-fgt"><label>FortiFlex token</label><input data-tok="${esc(d.name)}" autocomplete="off" spellcheck="false"></div>`;
  (T.region.multi_vrf === false ? (d.lans || []).slice(0, 1) : d.lans || []).forEach((l, j) => { h += fld("lan" + j + "_intf", "LAN" + (j + 1) + " port", l.intf) + fld("lan" + j + "_ip", "LAN" + (j + 1) + " IP/prefix", l.ip); });
  return h + "</div></div>";
}
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
  flexFields(); topoDraw();
}
function collect() {
  const t = JSON.parse(JSON.stringify(T)), n = (v) => (v === "" ? null : +v);
  t.adom = $("#adom").value.trim(); t.hub2hub = $("#hub2hub").checked;
  t.fmgr = { ...T.fmgr, name: $("#fm_name").value.trim(), ip: $("#fm_ip").value.trim(), gateway: $("#fm_gw").value.trim(), port: $("#fm_port").value.trim() || "port1" };
  t.pve = {
    server_id: $("#pve_server").value ? +$("#pve_server").value : null, url: "", token_id: "", verify_tls: true, node: $("#pve_node").value.trim(),
    disk_storage: $("#pve_dstore").value.trim(), import_storage: $("#pve_istore").value.trim(), iso_storage: $("#pve_isostore").value.trim(),
    default_bridge: $("#pve_br").value.trim(), cpu_type: $("#pve_cpu").value.trim() || "host", license_mode: "cli",   // FortiFlex: always `execute vm-license <token>` as the last CLI line
    fgt: { cores: +$("#fgt_cores").value, memory_mb: +$("#fgt_mem").value },
    fmg: { cores: +$("#fmg_cores").value, memory_mb: +$("#fmg_mem").value, data_disk_gb: +$("#fmg_disk").value, bridge: $("#fmg_br").value.trim(), vlan: n($("#fmg_vlan").value) },
    ports: Object.fromEntries([...document.querySelectorAll("[data-p]")].map((r) => [r.dataset.p, { bridge: r.querySelector("[data-f=bridge]").value.trim(), vlan: n(r.querySelector("[data-f=vlan]").value) }])),
  };
  t.wanemu = wanCollect();
  t.region.multi_vrf = $("#r_multi").checked; t.region.lan_summary = $("#r_lansum").value.trim();
  t.region.vrfs = [...document.querySelectorAll("[data-v]")].map((r) => ({ id: +r.querySelector("[data-f=id]").value, lan_summary: r.querySelector("[data-f=lan_summary]").value.trim(), dia: r.querySelector("[data-f=dia]").checked }));
  document.querySelectorAll(".dev").forEach((el) => {
    const d = t[el.dataset.k][el.dataset.i], lans = (d.lans || []).map((l) => ({ ...l }));
    el.querySelectorAll("[data-f]").forEach((inp) => {
      const k = inp.dataset.f, v = inp.value.trim(), m = k.match(/^lan(\d+)_(intf|ip)$/);
      if (m) (lans[+m[1]] ??= {})[m[2]] = v; else d[k] = v;
    });
    d.lans = lans;
  });
  return t;
}
function secrets() {
  const tok = {};
  document.querySelectorAll("[data-tok]").forEach((i) => { if (i.value.trim()) tok[i.dataset.tok] = i.value.trim(); });
  return { fmg_token: $("#fm_token").value.trim() || null, fgt_tokens: tok, fmg_initial_pw: "", fgt_initial_pw: "", fgt_new_pw: $("#fgt_newpw").value, admin_user: $("#fmg_user").value || "admin", pve_token_secret: "" };
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
function csvTokens() {
  const devices = {};
  document.querySelectorAll("[data-tok]").forEach((e) => { if (e.value.trim()) devices[e.dataset.tok] = e.value.trim(); });
  return { fmg: $("#fm_token").value.trim(), devices };
}
async function csvDownload(full) {
  const r = await post("/api/csv/export", { topology: collect(), full, tokens: csvTokens() });
  if (!r.ok) { $("#csv_res").textContent = "Export failed"; return; }
  const url = URL.createObjectURL(await r.blob()), a = document.createElement("a");
  a.href = url; a.download = full ? "sdwan-volledig.csv" : "sdwan-devices.csv"; a.click(); URL.revokeObjectURL(url);
  $("#csv_res").textContent = full ? "Full export downloaded (settings + devices)." : "Downloaded: fill it in and import it again.";
}
$("#btn_csv_dl").onclick = () => csvDownload(false);
$("#btn_csv_full").onclick = () => csvDownload(true);
const deepMerge = (a, b) => { for (const [k, v] of Object.entries(b)) a[k] = v && typeof v === "object" && !Array.isArray(v) && a[k] && typeof a[k] === "object" ? deepMerge({ ...a[k] }, v) : v; return a; };
async function csvPost(f, apply) {
  const fd = new FormData(); fd.append("file", f); fd.append("topology", JSON.stringify(collect())); fd.append("apply_settings", apply ? "1" : "");
  const r = await fetch("/api/csv/import", { method: "POST", body: fd });
  return [r, await r.json().catch(() => ({}))];
}
$("#btn_csv_up").onclick = () => $("#csv_file").click();
$("#csv_file").onchange = async (ev) => {
  const f = ev.target.files[0]; ev.target.value = "";
  if (!f) return;
  const fail = (j) => { $("#csv_res").textContent = "✕ " + (typeof j.detail === "string" ? j.detail : "import failed"); };
  $("#csv_todo").hidden = true;
  let [r, j] = await csvPost(f, false);
  if (!r.ok) return fail(j);
  let applied = false;
  if (j.has_settings && confirm("This file also contains settings (ADOM, DNS, region, VRFs, FortiManager, Proxmox).\n\nOK = take over settings and devices\nCancel = take over only the devices (settings stay as they are in the tool now)")) {
    [r, j] = await csvPost(f, true);
    if (!r.ok) return fail(j);
    applied = true;
  }
  const toks = Object.fromEntries([...document.querySelectorAll("[data-tok]")].map((e) => [e.dataset.tok, e.value]));
  let nt = { ...collect(), hubs: j.hubs, edges: j.edges }, srv = null, extra = [];
  if (applied) {
    const { pve: sp = {}, ...rest } = j.settings, sname = sp.server_name; delete sp.server_name;
    nt = deepMerge(JSON.parse(JSON.stringify(nt)), rest);
    nt.pve = deepMerge(JSON.parse(JSON.stringify(nt.pve)), sp);
    if (sname) { srv = SERVERS.find((s) => s.name === sname); if (srv) $("#pve_server").value = srv.id; else extra.push(`Proxmox server '${sname}' does not exist: pick or add one on the Servers page`); }
  }
  T = nt; draw(); markIpFields();
  const imp = j.tokens || {};
  document.querySelectorAll("[data-tok]").forEach((e) => { const v = imp[e.dataset.tok] || toks[e.dataset.tok]; if (v) e.value = v; });
  if (j.fmg_token) $("#fm_token").value = j.fmg_token;
  if (srv) await loadPve();
  $("#csv_res").textContent = `✓ ${f.name}: ${j.summary}` + (Object.keys(imp).length || j.fmg_token ? ` · FortiFlex tokens: ${Object.keys(imp).length + (j.fmg_token ? 1 : 0)}` : "") + ([...j.notes, ...extra].length ? ` (${[...j.notes, ...extra].join("; ")})` : "");
  if (j.todo.length) { $("#csv_todo").hidden = false; $("#csv_todo").textContent = "Still to fill in / correct: " + j.todo.join(" · "); }
};
// ---- scope: all / only FortiManager / only FortiGates ------------------------
const SCOPE_HINT = {
  all: "",
  fmg: "Only the FortiManager is created and prepared with ADOM, templates, policy packages and blueprints for the hubs and spokes, without model devices. Names, loopbacks, LANs and hub addresses are needed for the templates; serial numbers, wan1 gateways, FortiGate tokens and password are not (they come with 'FortiGates only').",
  tpl: "Deploys only the ADOM and the templates (metadata variables, system, SD-WAN and CLI templates, policy packages, template groups, blueprints) to an existing FortiManager at the given IP, with the admin user and password below. No VMs and no Proxmox; the FortiManager settings (time zone, timers, FortiAnalyzer) are left alone. Serial numbers and wan1 gateways are not needed; model devices follow with 'FortiGates only'.",
  dev: "Creates the ADOM, the model devices (with serial number) and the templates with the per-device values in an existing FortiManager, without a link to real FortiGates: no VMs, no Proxmox. Then the tool does a Quick install (device settings) per device and a Re-install policy per role, to the device database. The FortiManager settings are left alone. Serial numbers and hub gateways are needed; FortiGate tokens and password are not. Real FortiGates register later on their serial number, or deploy them with 'FortiGates only'.",
  fgt: "The FortiManager must already run on the given static IP and be reachable with the admin user and password below. No FortiManager VM is created.",
};
const scope = () => (document.querySelector('input[name="scope"]:checked') || {}).value || "all";
function scopeApply() {
  const s = scope();
  $("#v_deploy").dataset.scope = s;
  $("#scope_hint").textContent = SCOPE_HINT[s];
  if (T) { flexFields(); topoDraw(); markIpFields(); $("#fm_ip").classList.remove("bad"); $("#fm_ip").title = ""; }
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
  { const e = ipErrors(); if (e.length) return showErr(e); }
  const dry = false, sc = scope(), mf = ["fgt", "tpl", "dev"].includes(sc) ? "none" : "day0", mg = ["fmg", "tpl", "dev"].includes(sc) ? "none" : "day0";   // Day-0 for what is deployed
  const what = { all: "the FortiManager and FortiGate VMs", fmg: "the FortiManager VM", fgt: "the FortiGate VMs" }[sc];
  const ask = sc === "tpl" ? "This writes the ADOM and the templates to the existing FortiManager (set/update: earlier objects of this tool are overwritten). Continue?"
    : sc === "dev" ? "This writes the ADOM, the model devices and the templates to the existing FortiManager (set/update: earlier objects of this tool are overwritten) and then does a Quick install and a Re-install policy per device. Continue?"
    : `This writes to FortiManager, creates ${what} on Proxmox, configures and licenses them (they reboot). Continue?`;
  if (!dry && !confirm(ask)) return;
  const btn = $("#btn_deploy"); if (btn.disabled) return;
  btn.disabled = true;                                   // FortiFlex licenses are fetched in this request: no double click
  const body = { topology: collect(), fmg: fmgCfg(), dry_run: false, scope: sc, bootstrap: { fmg: mf, fgt: mg }, secrets: secrets(), images: { fgt: mg === "none" ? null : $("#img_fgt").value || null, fmg: mf === "none" ? null : $("#img_fmg").value || null } };
  let plan = null;
  if (!$("#btn_flexcheck").hidden) {                     // FortiFlex in this run: show what is looked up and why it takes a while
    deployStatus("FortiFlex: looking up licences for the devices of this deployment…");
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
  CUR_JOB = id;
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
    if (s.state !== "running") {
      ES.close(); ES = null;
      progressMsg(s.state === "done" ? "✔ Done" : "✕ Failed — see the log", s.state === "done" ? "ok" : "error");
    }
  });
}
async function bannerLoad() {
  const el = $("#job_banner");
  try {
    const l = await (await fetch("/api/jobs")).json(), j = l.jobs[0];
    if (!j) { el.hidden = true; return; }
    const st = { running: "is running", done: "is done", failed: "failed" }[j.state] || j.state;
    el.hidden = false; el.innerHTML = `Last deployment: <b>${esc(j.title)}</b> (${esc(j.created)}) ${st} &middot; <a href="#/progress/${j.id}" style="color:var(--accent)">View progress and log</a>`;
  } catch (_) { el.hidden = true; }
}

// ---- servers ----------------------------------------------------------------
let SERVERS = [], EDIT = null;
function srvReset() {
  EDIT = null; $("#srv_form_title").textContent = "Add server";
  for (const id of ["name", "url", "tid", "secret"]) $("#srv_" + id).value = "";
  $("#srv_secret").placeholder = ""; $("#srv_tls").checked = true; $("#srv_cancel").hidden = true; $("#srv_res").textContent = ""; $("#srv_res").className = "hint";
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
  T.pve = Object.assign({}, T.pve, last, { ports: Object.assign({}, T.pve.ports, last.ports || {}) });
  pveFill(T.pve);
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
    $("#srv_tls").checked = s.verify_tls; $("#srv_cancel").hidden = false; $("#srv_name").scrollIntoView({ block: "center" });
  } else if (t.dataset.st) {
    const out = $("#srv_t" + t.dataset.st); out.className = "hint"; out.textContent = "…";
    const r = await post(`/api/servers/${t.dataset.st}/test`, {}), j = await json(r);
    out.className = "hint " + (r.ok ? (j.warning ? "warn" : "ok") : "error"); out.textContent = r.ok ? `OK · PVE ${j.version}` + (j.warning ? " ⚠" : "") : "Error: " + j.detail;
  }
});
$("#srv_cancel").onclick = srvReset;
const srvBody = () => ({ name: $("#srv_name").value.trim(), url: $("#srv_url").value.trim(), token_id: $("#srv_tid").value.trim(), token_secret: $("#srv_secret").value || null, verify_tls: $("#srv_tls").checked });
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

// ---- init -------------------------------------------------------------------
(async () => {
  const a = await (await fetch("/api/about")).json();
  STEPS = a.steps;
  $("#steps").innerHTML = a.steps.map((s) => `<li><b>${esc(s.title)}</b><span>${esc(s.desc)}</span></li>`).join("");
  $("#orch").textContent = a.orchestrator_present ? "Orchestrator templates found: " + a.orchestrator : "⚠ Orchestrator templates not found at " + a.orchestrator;
  T = await (await fetch("/api/defaults")).json();
  $("#r_multi").addEventListener("change", () => { T = collect(); if (T.region.multi_vrf && !T.region.vrfs.length) T.region.vrfs = [{ id: 11, lan_summary: "10.0.0.0/14", dia: true }, { id: 12, lan_summary: "10.0.0.0/14", dia: false }]; draw(); markIpFields(); });
  draw(); markIpFields(); scopeInit(); stepsDraw(); await imgLoad(); await serversLoad(true); route();
})();


// ---- WAN-emulatie: one card per link (ISP-HUB / ISP-A / ISP-B), values go to the agent through the tool ----------
const WAN_SEG = { hub: "ISP-HUB", a: "ISP-A", b: "ISP-B" };
const WAN_F = [["delay_ms", "Latency", "ms", 0, 1000, 1], ["jitter_ms", "Jitter", "ms", 0, 200, 1], ["loss_pct", "Loss", "%", 0, 100, 0.1], ["rate_kbit", "Bandwidth", "kbit/s (0 = unlimited)", 0, 1000000, 1000]];
function wanErr(msg) { $("#wanemu-err").innerHTML = msg ? `<div class="error">✕ ${esc(msg)}</div>` : ""; }   // no scroll: the page refreshes every 10 s
// ---- FortiGates via wan-emu: GUI via port forward, SSH in the browser (xterm.js) ----------------------------------
const XTERM = "https://cdn.jsdelivr.net/npm/xterm@5.3.0/", XFIT = "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/";
let TERM = null, TERM_WS = null, TERM_NAME = "";
async function wanFwdLoad() {
  const r = await fetch("/api/wanemu/forwards"), j = await json(r);
  if (!r.ok) { $("#wanemu-fgts").innerHTML = `<div class="hint">${esc(errList(j).join(" "))}</div>`; return; }
  $("#wanemu-fgts").innerHTML = j.forwards.map((f) => `<div class="fgt-row"><b>${esc(f.name)}</b><span class="mono">${esc(f.ip)}</span>` +
    `<a href="https://${esc(j.host)}:${f.https}/" target="_blank" rel="noopener"><button>GUI openen</button></a>` +
    `<button data-ssh="${esc(f.name)}">SSH</button><span class="hint mono">ssh -p ${f.ssh} admin@${esc(j.host)}</span></div>`).join("");
}
function loadOnce(tag, attrs) {
  return new Promise((ok, bad) => {
    if (document.querySelector(`${tag}[${tag === "script" ? "src" : "href"}="${attrs.src || attrs.href}"]`)) return ok();
    const e = document.createElement(tag); Object.assign(e, attrs); e.onload = ok; e.onerror = () => bad(new Error("failed to load: " + (attrs.src || attrs.href)));
    document.head.appendChild(e);
  });
}
function termOpen(name) {
  TERM_NAME = name; $("#term_title").textContent = `SSH to ${name}`;
  $("#term_overlay").hidden = false; $("#term_form").hidden = false; $("#term_box").innerHTML = ""; $("#term_pw").focus();
}
let TERM_FIT = null, TERM_BUSY = false;
const termResize = () => TERM && TERM_FIT && TERM_FIT.fit();
window.addEventListener("resize", termResize);                  // one listener for every session
async function termStart() {
  if (TERM_BUSY || TERM) return;                                // Enter twice while loading: one session only
  TERM_BUSY = true;
  try {
    await loadOnce("link", { rel: "stylesheet", href: XTERM + "css/xterm.css" });
    await loadOnce("script", { src: XTERM + "lib/xterm.js" }); await loadOnce("script", { src: XFIT + "lib/xterm-addon-fit.js" });
  } catch (e) { TERM_BUSY = false; $("#term_box").innerHTML = `<div class="error">✕ ${esc(e.message)} (does the browser have internet access?)</div>`; return; }
  const user = $("#term_user").value.trim() || "admin", pw = $("#term_pw").value;
  $("#term_pw").value = "";                                     // not kept in the page
  $("#term_form").hidden = true;
  TERM = new window.Terminal({ cursorBlink: true, fontSize: 13 });
  $("#term_box").innerHTML = "";
  TERM_FIT = new window.FitAddon.FitAddon(); TERM.loadAddon(TERM_FIT); TERM.open($("#term_box")); TERM_FIT.fit();
  TERM_BUSY = false;
  const term = TERM, ws = TERM_WS = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/term/${encodeURIComponent(TERM_NAME)}`);
  ws.onopen = () => ws.send(JSON.stringify({ user, password: pw, cols: term.cols, rows: term.rows }));
  ws.onmessage = (e) => term.write(e.data);                     // bound to this session's terminal, not a later one
  ws.onclose = () => { if (TERM === term) term.write("\r\n[connection closed]\r\n"); };
  term.onData((d) => ws.readyState === 1 && ws.send(JSON.stringify({ data: d })));
  term.onResize(({ cols, rows }) => ws.readyState === 1 && ws.send(JSON.stringify({ resize: [cols, rows] })));
  term.focus();
}
function termClose() {
  if (TERM_WS) { try { TERM_WS.close(); } catch (_) {} TERM_WS = null; }
  if (TERM) { TERM.dispose(); TERM = null; TERM_FIT = null; }
  TERM_BUSY = false;
  $("#term_pw").value = ""; $("#term_overlay").hidden = true;
}
$("#wanemu-fgts").addEventListener("click", (e) => { const b = e.target.closest("[data-ssh]"); if (b) termOpen(b.dataset.ssh); });
$("#term_go").onclick = termStart;
$("#term_pw").addEventListener("keydown", (e) => { if (e.key === "Enter") termStart(); });
$("#term_close").onclick = termClose;
async function wanLoad() {
  const r = await fetch("/api/wanemu/links"), j = await r.json().catch(() => ({}));
  if (!r.ok) { wanErr(j.detail || `HTTP ${r.status}`); $("#wanemu-links").innerHTML = ""; return; }
  wanErr(""); WAN = j; $("#wanemu-addr").textContent = `agent ${j.addr}`;
  $("#wanemu-presets").innerHTML = Object.entries(j.presets).map(([k, l]) => `<button data-wp="${k}">${esc(l)}</button>`).join("") + `<button data-wreset="1">All neutral</button>`;
  $("#wanemu-links").innerHTML = Object.entries(WAN_SEG).map(([s, n]) => `<div class="wan-col"><h3>${n}</h3>${j.links.filter((l) => l.seg === s).map(wanCard).join("") || '<div class="hint">no links</div>'}</div>`).join("");
}
function wanCard(l) {
  const st = l.state, rtt = 2 * st.delay_ms;
  return `<div class="card wan-card${st.down ? " down" : ""}" data-wl="${esc(l.id)}"><b>${esc(l.name)}</b> <span class="mono">${esc(l.ip)}</span> <small>${esc(l.id.split("-").pop())}</small>` +
    WAN_F.map(([k, lbl, unit, lo, hi, step]) => `<div class="wan-f"><label>${lbl} <output>${st[k]}</output> ${unit}</label><input type="range" data-wf="${k}" min="${lo}" max="${hi}" step="${step}" value="${st[k]}"></div>`).join("") +
    `<div class="wan-f"><label><input type="checkbox" data-wf="down" ${st.down ? "checked" : ""}> link down</label></div>` +
    `<div class="btnrow"><button class="primary" data-wa="1">Apply</button><span class="hint">RTT +${rtt} ms</span></div></div>`;
}
async function wanApply(card) {
  const v = {};
  card.querySelectorAll("[data-wf]").forEach((i) => (v[i.dataset.wf] = i.type === "checkbox" ? i.checked : +i.value));
  const r = await fetch("/api/wanemu/links/" + encodeURIComponent(card.dataset.wl), { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(v) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return wanErr(j.detail || `HTTP ${r.status}`);
  wanErr(""); card.classList.remove("dirty"); await wanLoad();
  const nc = document.querySelector(`#v_wanemu [data-wl="${CSS.escape(card.dataset.wl)}"]`);   // re-rendered card
  if (nc) { nc.classList.add("wan-ok"); setTimeout(() => nc.classList.remove("wan-ok"), 1500); }
}
async function wanPreset(name, link) {
  const r = await fetch(`/api/wanemu/preset/${name}` + (link ? `?link=${encodeURIComponent(link)}` : ""), { method: "POST" });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return wanErr(j.detail || `HTTP ${r.status}`);
  await wanLoad();
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
    const r = await fetch("/api/wanemu/reset", { method: "POST" }), j = await r.json().catch(() => ({}));
    if (!r.ok) return wanErr(j.detail || `HTTP ${r.status}`);
    await wanLoad();
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
  const sc = scope(), mf = ["fgt", "tpl", "dev"].includes(sc) ? "none" : "day0", mg = ["fmg", "tpl", "dev"].includes(sc) ? "none" : "day0";
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
  const oct = (subnet, n) => subnet.split("/")[0].split(".").slice(0, 3).join(".") + "." + n;
  const fgtOn = ["all", "fgt"].includes(sc), modelOnly = ["dev", "tpl"].includes(sc), fmgNew = ["all", "fmg"].includes(sc);
  const dim = (on) => (on ? "" : ' opacity="0.35"');
  const box = (x, y, bw, bh, title, lines, cls, on = true) =>
    `<g${dim(on)}><rect x="${x}" y="${y}" width="${bw}" height="${bh}" rx="7" class="tp-box ${cls || ""}"/>` +
    `<text x="${x + 8}" y="${y + 17}" class="tp-title">${esc(title)}</text>` +
    lines.map((l, i) => `<text x="${x + 8}" y="${y + 33 + i * 14}" class="tp-txt">${esc(l)}</text>`).join("") + "</g>";
  const line = (x1, y1, x2, y2, on = true, dash) => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" class="tp-line"${dash ? ' stroke-dasharray="4 3"' : ""}${dim(on)}/>`;
  const bar = (y, name, vlan, subnet, on) => `<g${dim(on)}><line x1="${L}" y1="${y}" x2="${W - 10}" y2="${y}" class="tp-bar"/>` +
    `<text x="10" y="${y - 3}" class="tp-title">${esc(name)}</text><text x="10" y="${y + 11}" class="tp-txt">VLAN ${vlan} · ${esc(subnet)}</text></g>`;
  const yLab = 92, yHub = 170, yA = 205, yB = 240, yF = 290, yLan = 395, yCli = 470;
  let o = "";
  o += bar(yLab, "Lab / uplink", w.uplink.vlan, w.uplink.ip.replace(/\.\d+\/(\d+)$/, ".0/$1"), true);
  o += box(L, 14, 200, 56, "FortiManager", [`${(t.fmgr.ip || "").split("/")[0] || "(IP)"}`, fmgNew ? "new VM (Day-0)" : "existing"], "", true) + line(L + 100, 70, L + 100, yLab);
  o += box(L + 230, 14, 230, 56, "wan-emu (LXC)", [`${w.uplink.ip.split("/")[0]} · NAT, DHCP, tc`, "forwards :" + w.fwd_https_base + "+ / :" + w.fwd_ssh_base + "+"], "tp-emu", fgtOn) + line(L + 345, 70, L + 345, yLab, fgtOn);
  o += line(L + 345, yLab, L + 345, yB, fgtOn);
  o += bar(yHub, seg.hub.name, seg.hub.vlan, seg.hub.subnet, fgtOn) + bar(yA, seg.a.name, seg.a.vlan, seg.a.subnet, fgtOn) + bar(yB, seg.b.name, seg.b.vlan, seg.b.subnet, fgtOn);
  fgts.forEach(([d, hub], n) => {
    const x = L + 10 + n * COL, cx = x + 80, ei = n - t.hubs.length;
    const wan1 = hub ? (d.wan1_ip || "").split("/")[0] : oct(seg.a.subnet, seg.a.res_start + ei);
    const wan2 = !hub && d.wan2_intf ? oct(seg.b.subnet, seg.b.res_start + ei) : null;
    const lan = (d.lans || [])[0] || {}, vlan = w.lan_vlan_base + 1 + n;
    o += line(cx - 20, yF, cx - 20, hub ? yHub : yA, fgtOn);
    if (wan2) o += line(cx + 20, yF, cx + 20, yB, fgtOn);
    o += box(x, yF, 160, 72, `${d.name || "?"} · ${hub ? "hub" : "spoke"}`, [`WAN1 ${wan1}`, wan2 ? `WAN2 ${wan2}` : hub ? "gw " + (d.wan1_gw || "") : "no WAN2", modelOnly ? "model device" : `Lo ${d.loopback || ""}`], hub ? "tp-hub" : "tp-spoke", fgtOn || modelOnly);
    if (lan.ip) {
      o += line(cx, yF + 72, cx, yLan, fgtOn || modelOnly);
      o += box(x, yLan, 160, 50, `LAN ${lan.intf || ""}`, [lan.ip, `VLAN ${vlan}`], "tp-lan", fgtOn || modelOnly);
      if (!hub) {
        const ip = lan.ip.split("/")[0].split(".").slice(0, 3).join(".") + ".10";
        o += line(cx, yLan + 50, cx, yCli, fgtOn) + box(x, yCli, 160, 46, `client-${d.name}`, [`${ip} (LXC)`], "tp-cli", fgtOn);
      }
    }
  });
  o += `<text x="${L}" y="${yCli + 70}" class="tp-txt">IPsec overlay: every spoke WAN → WAN1 of every hub · BGP on loopbacks · ${t.region.multi_vrf === false ? "single VRF (VRF 0)" : "multi-VRF"}</text>`;
  return `<svg viewBox="0 0 ${W} ${yCli + 84}" width="100%" role="img" aria-label="Topology" xmlns="http://www.w3.org/2000/svg">${o}</svg>`;
}
let TOPO_T = null;
function topoDraw() {
  clearTimeout(TOPO_T);
  TOPO_T = setTimeout(() => { try { $("#topo").innerHTML = topoSvg(collect(), scope()); } catch (e) { $("#topo").innerHTML = ""; } }, 150);
}
$("#v_deploy").addEventListener("input", (e) => { if (!e.target.closest("#topo")) topoDraw(); });
