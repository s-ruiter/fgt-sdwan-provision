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
  const VIEWS = ["deploy", "progress", "images", "servers", "about"];
  const view = VIEWS.includes(r) ? r : "deploy";
  for (const v of VIEWS) $("#v_" + v).hidden = v !== view;
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === view));
  window.scrollTo(0, 0);
  if (view === "progress") progressOpen(arg);
  if (view === "deploy") bannerLoad();
}
window.addEventListener("hashchange", route);

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
  if (kind === "ip" || (kind === "ipcidr" && !v.includes("/"))) return RE_IP.test(v) ? "" : "Geen geldig IPv4-adres (a.b.c.d)";
  if (!RE_CIDR.test(v)) return "Gebruik a.b.c.d/nn, bijvoorbeeld 10.0.0.5/24";
  const [a, p] = v.split("/"), pre = +p;
  if (pre <= 30) {
    const size = 2 ** (32 - pre), net = Math.floor(ipNum(a) / size) * size;
    if (ipNum(a) === net) return "Dit is het netwerkadres, geen host-adres";
    if (ipNum(a) === net + size - 1) return "Dit is het broadcast-adres, geen host-adres";
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
  $("#ports").innerHTML = "<thead><tr><th>Poort</th><th>Bridge</th><th>VLAN</th></tr></thead><tbody>" +
    Object.entries(p.ports).map(([k, v]) => `<tr data-p="${k}"><td class="mono">${k}</td><td><select data-f="bridge">${opt(v.bridge)}</select></td><td><input data-f="vlan" type="number" value="${v.vlan ?? ""}"></td></tr>`).join("") + "</tbody>";
}
function dev(kind, d, i) {
  const hub = kind === "hubs";
  let h = `<div class="dev" data-k="${kind}" data-i="${i}"><b>${esc(d.name)}</b><div class="fields">`;
  h += fld("name", "Naam", d.name) + fld("serial", "Serienummer", d.serial, "", "only-dev") + fld("loopback", "Loopback (BGP, router-id)", d.loopback);
  // WAN design: hub = one wan (wan1, always static); branch = wan1 + optional wan2, both DHCP, each with a tunnel to wan1 of every hub
  if (hub) h += fld("wan1_intf", "wan1-poort (ZTP)", d.wan1_intf) + fld("wan1_ip", "wan1 IP/prefix (statisch)", d.wan1_ip) + fld("wan1_gw", "wan1 gateway", d.wan1_gw);
  else h += fld("wan1_intf", "wan1-poort (ZTP, DHCP)", d.wan1_intf) + fld("wan2_intf", "wan2-poort (DHCP; leeg = geen)", d.wan2_intf);
  h += `<div class="f only-fgt"><label>FortiFlex-token</label><input data-tok="${esc(d.name)}" autocomplete="off" spellcheck="false"></div>`;
  (T.region.multi_vrf === false ? (d.lans || []).slice(0, 1) : d.lans || []).forEach((l, j) => { h += fld("lan" + j + "_intf", "LAN" + (j + 1) + "-poort", l.intf) + fld("lan" + j + "_ip", "LAN" + (j + 1) + " IP/prefix", l.ip); });
  return h + "</div></div>";
}
function draw() {
  const p = T.pve;
  $("#adom").value = T.adom; $("#hub2hub").checked = !!T.hub2hub;
  $("#fm_name").value = T.fmgr.name; $("#fm_ip").value = T.fmgr.ip; $("#fm_gw").value = T.fmgr.gateway; $("#fm_port").value = T.fmgr.port;
  pveFill(p);
  const mv = T.region.multi_vrf !== false;
  $("#r_multi").checked = mv; $("#sv_box").hidden = mv; $("#vrfs").hidden = !mv; $("#r_lansum").value = T.region.lan_summary || "10.0.0.0/8";
  $("#vrfs").innerHTML = "<thead><tr><th>VRF-id</th><th>LAN-summary</th><th>DIA</th></tr></thead><tbody>" +
    T.region.vrfs.map((v, i) => `<tr data-v="${i}"><td><input data-f="id" type="number" value="${v.id}"></td><td><input data-f="lan_summary" value="${esc(v.lan_summary)}"></td><td><input data-f="dia" type="checkbox" ${v.dia ? "checked" : ""}></td></tr>`).join("") + "</tbody>";
  ["hubs", "edges"].forEach((k) => ($("#" + k).innerHTML = T[k].map((d, i) => dev(k, d, i)).join("")));
  d0Opts();
}
// Day-0 preview: only the VMs the chosen deploy option creates
function d0Opts() {
  const s = scope(), cur = $("#d0_dev").value;
  const l = [...(s === "all" || s === "fmg" ? ["fmg"] : []), ...(s === "all" || s === "fgt" ? [...T.hubs, ...T.edges].map((d) => d.name) : [])];
  $("#d0_dev").innerHTML = l.map((n) => `<option>${esc(n)}</option>`).join("");
  if (l.includes(cur)) $("#d0_dev").value = cur;
  $("#d0_out").hidden = true;
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
    s.innerHTML = l.length ? l.map((i) => `<option value="${i.id}">${esc(i.filename)}${i.version ? " (v" + i.version + ")" : ""}</option>`).join("") : '<option value="">— nog geen image —</option>';
    if (cur && l.some((i) => i.id === cur)) s.value = cur;
  }
  $("#img_list").innerHTML = IMGS.length
    ? `<table class="imgtbl"><thead><tr><th>Rol</th><th>Bestand</th><th>Versie</th><th>Virtueel</th><th>SHA-256</th><th></th></tr></thead><tbody>` +
      IMGS.map((i) => `<tr><td>${i.role === "fgt" ? "FortiGate" : "FortiManager"}</td><td class="mono">${esc(i.filename)}${i.warnings.map((w) => `<div class="hint warn">⚠ ${esc(w)}</div>`).join("")}</td><td class="mono">${i.version ? "v" + i.version + (i.build ? " b" + i.build : "") : "—"}</td><td class="mono">${i.virtual_gb} GB</td><td class="mono" title="${i.sha256}">${i.sha256.slice(0, 16)}…</td><td class="actions-cell"><button class="icon danger" data-del="${i.id}" title="Verwijderen"><span class="i"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg></span></button></td></tr>`).join("") + "</tbody></table>"
    : '<p class="muted">Nog geen images geüpload.</p>';
}
async function imgLoad() { IMGS = await (await fetch("/api/images")).json(); imgDraw(); }
function upload(role, file) {
  if (!file) return;
  const fd = new FormData(); fd.append("role", role); fd.append("file", file);
  const x = new XMLHttpRequest(), dz = $("#dz_" + role), bar = $("#bar_" + role);
  dz.classList.add("has"); dz.querySelector("b").textContent = file.name; dz.querySelector("small").textContent = fmtBytes(file.size) + " · uploaden…";
  x.open("POST", "/api/images");
  x.upload.onprogress = (e) => { if (e.lengthComputable) bar.style.transform = `scaleX(${e.loaded / e.total})`; };
  x.onload = async () => {
    bar.style.transform = "scaleX(0)";
    if (x.status === 200) {
      const m = JSON.parse(x.responseText);
      dz.querySelector("small").textContent = `${fmtBytes(file.size)} · ${m.virtual_gb} GB virtueel · klaar`;
      showErr([], "#img_err"); await imgLoad();
    } else {
      let d; try { d = JSON.parse(x.responseText).detail; } catch (_) { d = x.responseText; }
      dz.classList.remove("has"); dz.querySelector("b").textContent = "Sleep het bestand hierheen of klik"; dz.querySelector("small").textContent = ".zip of .qcow2";
      showErr(["Upload mislukt: " + d], "#img_err");
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
  if (b && confirm("Image verwijderen?")) { await fetch("/api/images/" + b.dataset.del, { method: "DELETE" }); imgLoad(); }
});

// ---- actions ----------------------------------------------------------------

// ---- actions ----------------------------------------------------------------
// Proxmox data (nodes, storages per node, bridges) is fetched from the saved server and shown as dropdowns
async function loadPve(msg) {
  const sid = $("#pve_server").value, out = $("#pve_res");
  out.className = "hint";
  if (!sid) { PVE_OPT = { nodes: [], image_storages: [], import_storages: [], iso_storages: [], bridges: [] }; pveFill(collect().pve); out.textContent = ""; return; }
  out.textContent = "Gegevens ophalen…";
  const p = collect().pve;
  const r = await post(`/api/servers/${sid}/test`, {}), j = await json(r);
  if (!r.ok) { out.className = "hint error"; out.textContent = "Fout: " + j.detail; return; }
  PVE_OPT.nodes = j.nodes;
  const node = j.nodes.includes(p.node) ? p.node : j.nodes[0];
  const r2 = await fetch(`/api/servers/${sid}/nodes/${encodeURIComponent(node)}/options`), o = await json(r2);
  if (!r2.ok) { out.className = "hint error"; out.textContent = "Fout: " + o.detail; return; }
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
  if (!r.ok) { $("#csv_res").textContent = "Export mislukt"; return; }
  const url = URL.createObjectURL(await r.blob()), a = document.createElement("a");
  a.href = url; a.download = full ? "sdwan-volledig.csv" : "sdwan-devices.csv"; a.click(); URL.revokeObjectURL(url);
  $("#csv_res").textContent = full ? "Volledige export gedownload (instellingen + devices)." : "Gedownload: vul het in en importeer het weer.";
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
  const fail = (j) => { $("#csv_res").textContent = "✕ " + (typeof j.detail === "string" ? j.detail : "import mislukt"); };
  $("#csv_todo").hidden = true;
  let [r, j] = await csvPost(f, false);
  if (!r.ok) return fail(j);
  let applied = false;
  if (j.has_settings && confirm("Dit bestand bevat ook instellingen (ADOM, DNS, regio, VRF's, FortiManager, Proxmox).\n\nOK = instellingen én devices overnemen\nAnnuleren = alleen de devices overnemen (instellingen blijven zoals ze nu in de tool staan)")) {
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
    if (sname) { srv = SERVERS.find((s) => s.name === sname); if (srv) $("#pve_server").value = srv.id; else extra.push(`Proxmox-server '${sname}' bestaat niet: kies of voeg er een toe op de pagina Servers`); }
  }
  T = nt; draw(); markIpFields();
  const imp = j.tokens || {};
  document.querySelectorAll("[data-tok]").forEach((e) => { const v = imp[e.dataset.tok] || toks[e.dataset.tok]; if (v) e.value = v; });
  if (j.fmg_token) $("#fm_token").value = j.fmg_token;
  if (srv) await loadPve();
  $("#csv_res").textContent = `✓ ${f.name}: ${j.summary}` + (Object.keys(imp).length || j.fmg_token ? ` · FortiFlex-tokens: ${Object.keys(imp).length + (j.fmg_token ? 1 : 0)}` : "") + ([...j.notes, ...extra].length ? ` (${[...j.notes, ...extra].join("; ")})` : "");
  if (j.todo.length) { $("#csv_todo").hidden = false; $("#csv_todo").textContent = "Nog in te vullen/te corrigeren: " + j.todo.join(" · "); }
};
$("#btn_render").onclick = async () => {
  showErr([]);
  { const e = ipErrors(); if (e.length) return showErr(e); }
  const r = await post("/api/render", { topology: collect() }), j = await json(r);
  if (!r.ok) return showErr(errList(j));
  $("#preview").hidden = false;
  const names = Object.keys(j.configs);
  $("#tabs").innerHTML = names.map((n) => `<button data-n="${esc(n)}">${esc(n)} (${j.lines[n]})</button>`).join("");
  const sel = (n) => { $("#cfg").textContent = j.configs[n]; document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.n === n)); };
  $("#tabs").onclick = (e) => e.target.dataset.n && sel(e.target.dataset.n);
  sel(names[0]);
};
$("#btn_d0").onclick = async () => {
  showErr([]);
  { const e = ipErrors(); if (e.length) return showErr(e); }
  const r = await post("/api/cloudinit/preview", { topology: collect(), device: $("#d0_dev").value }), j = await json(r);
  if (!r.ok) return showErr(errList(j));
  $("#d0_out").hidden = false;
  $("#d0_out").textContent = `# ${j.layout} · licentiemodus: ${j.mode} · token/wachtwoord gemaskeerd\n` + j.user_data;
};

// ---- scope: all / only FortiManager / only FortiGates ------------------------
const SCOPE_HINT = {
  all: "",
  fmg: "Alleen de FortiManager wordt gemaakt en voorbereid met ADOM, templates, policy packages en blueprints voor de hubs en spokes, zonder model devices. Namen, loopbacks, LAN's en hub-adressen zijn nodig voor de templates; serienummers, wan1-gateways, FortiGate-tokens en -wachtwoord niet (die komen bij 'Alleen FortiGates').",
  tpl: "Rolt alleen de ADOM en de templates (metadata-variabelen, system-, SD-WAN- en CLI-templates, policy packages, template groups, blueprints) uit naar een bestaande FortiManager op het opgegeven IP, met de admin-gebruiker en het wachtwoord hieronder. Geen VM's en geen Proxmox; de FortiManager-instellingen (tijdzone, timers, FortiAnalyzer) blijven ongemoeid. Serienummers en wan1-gateways zijn niet nodig; model devices volgen met 'Alleen FortiGates'.",
  dev: "Maakt in een bestaande FortiManager de ADOM, de model devices (met serienummer) en de templates met de waarden per device aan, zonder koppeling naar echte FortiGates: geen VM's, geen Proxmox. Daarna doet de tool per device een Quick install (device settings) en per rol een Re-install policy, naar de device-database. De FortiManager-instellingen blijven ongemoeid. Serienummers en hub-gateways zijn nodig; FortiGate-tokens en -wachtwoord niet. Echte FortiGates melden zich later op hun serienummer, of rol ze uit met 'Alleen FortiGates'.",
  fgt: "De FortiManager moet al draaien op het opgegeven vaste IP en bereikbaar zijn met de admin-gebruiker en het wachtwoord hieronder. Er wordt geen FortiManager-VM gemaakt.",
};
const scope = () => (document.querySelector('input[name="scope"]:checked') || {}).value || "all";
function scopeApply() {
  const s = scope();
  $("#v_deploy").dataset.scope = s;
  $("#scope_hint").textContent = SCOPE_HINT[s];
  if (T) { d0Opts(); markIpFields(); $("#fm_ip").classList.remove("bad"); $("#fm_ip").title = ""; }
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
  const dry = $("#dry").checked, sc = scope(), mf = ["fgt", "tpl", "dev"].includes(sc) ? "none" : "day0", mg = ["fmg", "tpl", "dev"].includes(sc) ? "none" : "day0";   // Day-0 for what is deployed
  const what = { all: "de FortiManager- en FortiGate-VM's", fmg: "de FortiManager-VM", fgt: "de FortiGate-VM's" }[sc];
  const ask = sc === "tpl" ? "Dit schrijft de ADOM en de templates naar de bestaande FortiManager (set/update: eerdere objecten van deze tool worden overschreven). Doorgaan?"
    : sc === "dev" ? "Dit schrijft de ADOM, de model devices en de templates naar de bestaande FortiManager (set/update: eerdere objecten van deze tool worden overschreven) en doet daarna per device een Quick install en een Re-install policy. Doorgaan?"
    : `Dit schrijft naar FortiManager, maakt ${what} op Proxmox, configureert en licenseert ze (ze herstarten). Doorgaan?`;
  if (!dry && !confirm(ask)) return;
  const r = await post("/api/deploy", { topology: collect(), fmg: fmgCfg(), dry_run: dry, scope: sc, bootstrap: { fmg: mf, fgt: mg }, secrets: secrets(), images: { fgt: mg === "none" ? null : $("#img_fgt").value || null, fmg: mf === "none" ? null : $("#img_fmg").value || null } });
  const j = await json(r);
  if (!r.ok) return showErr(errList(j));
  if (ES) ES.close();
  location.hash = "#/progress/" + j.job;                 // route() attaches to the job; a refresh keeps working
};
function progressMsg(t, cls) { $("#p_sum").className = "summary" + (cls ? " " + cls : ""); $("#p_sum").textContent = t; $("#p_sum").style.padding = cls ? "12px 16px" : ""; }
async function progressOpen(id) {
  if (!id) {                                              // "Voortgang" without a job: the most recent one
    const l = await (await fetch("/api/jobs")).json();
    if (!l.latest) { if (ES) { ES.close(); ES = null; } CUR_JOB = null; $("#p_title").textContent = "Voortgang"; stepsDraw(); $("#log").textContent = ""; return progressMsg("Er is nog geen uitrol gestart. Start er een op de pagina Uitrol."); }
    location.replace("#/progress/" + l.latest); return;
  }
  if (id === CUR_JOB && ES) return;                       // already showing this one
  if (ES) ES.close();
  CUR_JOB = id;
  const r = await fetch("/api/jobs/" + id);
  if (!r.ok) { ES = null; CUR_JOB = null; $("#p_title").textContent = "Voortgang"; stepsDraw(); $("#log").textContent = ""; return progressMsg("Deze uitrol bestaat niet meer (de lijst met uitrollen is leeg of gewist). Start een nieuwe op de pagina Uitrol."); }
  const info = await r.json();
  $("#p_title").textContent = info.title || "Uitrol";
  progressMsg("Bezig…"); $("#log").textContent = ""; stepsDraw();
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
      progressMsg(s.state === "done" ? "✔ Klaar" : "✕ Mislukt — zie de log", s.state === "done" ? "ok" : "error");
    }
  });
}
async function bannerLoad() {
  const el = $("#job_banner");
  try {
    const l = await (await fetch("/api/jobs")).json(), j = l.jobs[0];
    if (!j) { el.hidden = true; return; }
    const st = { running: "loopt nu", done: "is klaar", failed: "is mislukt" }[j.state] || j.state;
    el.hidden = false; el.innerHTML = `Laatste uitrol: <b>${esc(j.title)}</b> (${esc(j.created)}) ${st} &middot; <a href="#/progress/${j.id}" style="color:var(--accent)">Voortgang en log bekijken</a>`;
  } catch (_) { el.hidden = true; }
}

// ---- servers ----------------------------------------------------------------
let SERVERS = [], EDIT = null;
function srvReset() {
  EDIT = null; $("#srv_form_title").textContent = "Server toevoegen";
  for (const id of ["name", "url", "tid", "secret"]) $("#srv_" + id).value = "";
  $("#srv_secret").placeholder = ""; $("#srv_tls").checked = true; $("#srv_cancel").hidden = true; $("#srv_res").textContent = ""; $("#srv_res").className = "hint";
}
function srvDraw() {
  $("#srv_list").innerHTML = SERVERS.length
    ? `<table><thead><tr><th>Naam</th><th>URL</th><th>Token-ID</th><th>TLS</th><th></th></tr></thead><tbody>` +
      SERVERS.map((s) => `<tr><td>${esc(s.name)}</td><td class="mono">${esc(s.url)}</td><td class="mono">${esc(s.token_id)}</td><td>${s.verify_tls ? "gecontroleerd" : "niet gecontroleerd"}</td><td class="actions-cell"><span class="hint" id="srv_t${s.id}"></span> <button class="link" data-st="${s.id}">Testen</button><button class="link" data-se="${s.id}">Bewerken</button><button class="icon danger" data-sd="${s.id}" title="Verwijderen"><span class="i"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg></span></button></td></tr>`).join("") + "</tbody></table>"
    : '<p class="muted">Nog geen servers. Voeg hieronder je eerste Proxmox-server toe.</p>';
  const sel = $("#pve_server"), cur = sel.value;
  sel.innerHTML = '<option value="">— kies een server —</option>' + SERVERS.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
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
    if (confirm(`Server '${s.name}' verwijderen?`)) { await fetch("/api/servers/" + s.id, { method: "DELETE" }); if (EDIT === s.id) srvReset(); await serversLoad(); }
  } else if (t.dataset.se) {
    const s = SERVERS.find((x) => String(x.id) === t.dataset.se);
    EDIT = s.id; $("#srv_form_title").textContent = `Server bewerken: ${s.name}`;
    $("#srv_name").value = s.name; $("#srv_url").value = s.url; $("#srv_tid").value = s.token_id; $("#srv_secret").value = ""; $("#srv_secret").placeholder = "ongewijzigd laten";
    $("#srv_tls").checked = s.verify_tls; $("#srv_cancel").hidden = false; $("#srv_name").scrollIntoView({ block: "center" });
  } else if (t.dataset.st) {
    const out = $("#srv_t" + t.dataset.st); out.className = "hint"; out.textContent = "…";
    const r = await post(`/api/servers/${t.dataset.st}/test`, {}), j = await json(r);
    out.className = "hint " + (r.ok ? (j.warning ? "warn" : "ok") : "error"); out.textContent = r.ok ? `OK · PVE ${j.version}` + (j.warning ? " ⚠" : "") : "Fout: " + j.detail;
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
  out.className = "hint " + (r.ok ? (j.warning ? "warn" : "ok") : "error"); out.textContent = r.ok ? `OK — Proxmox VE ${j.version}` + (j.warning ? " · ⚠ " + j.warning : "") : "Fout: " + j.detail;
};

// ---- init -------------------------------------------------------------------
(async () => {
  const a = await (await fetch("/api/about")).json();
  STEPS = a.steps;
  $("#steps").innerHTML = a.steps.map((s) => `<li><b>${esc(s.title)}</b><span>${esc(s.desc)}</span></li>`).join("");
  $("#orch").textContent = a.orchestrator_present ? "Orchestrator-templates gevonden: " + a.orchestrator : "⚠ Orchestrator-templates niet gevonden op " + a.orchestrator;
  T = await (await fetch("/api/defaults")).json();
  $("#r_multi").addEventListener("change", () => { T = collect(); if (T.region.multi_vrf && !T.region.vrfs.length) T.region.vrfs = [{ id: 11, lan_summary: "10.0.0.0/14", dia: true }, { id: 12, lan_summary: "10.0.0.0/14", dia: false }]; draw(); markIpFields(); });
  draw(); markIpFields(); scopeInit(); stepsDraw(); await imgLoad(); await serversLoad(true); route();
})();
