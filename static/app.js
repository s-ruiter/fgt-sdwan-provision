"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
let T = null, IMGS = [], STEPS = [], ES = null;

// ---- helpers ----------------------------------------------------------------
const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
async function json(r) { try { return await r.json(); } catch (_) { return {}; } }
function errList(j) { const e = j.detail || j.errors || []; return (Array.isArray(e) ? e : [e]).map((x) => x.msg || String(x)); }
function showErr(list, target = "#errors") {
  $(target).innerHTML = list.length ? `<div class="error">${list.map((x) => "✕ " + esc(x)).join("<br>")}</div>` : "";
  if (list.length) window.scrollTo({ top: 0, behavior: "smooth" });
}
const fld = (k, l, v, extra = "") => `<div class="f"><label>${l}</label><input data-f="${k}" value="${esc(v)}" ${extra}></div>`;
const fmtBytes = (n) => (n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB");

// ---- routing ----------------------------------------------------------------
function route() {
  const r = (location.hash.replace(/^#\//, "") || "deploy").split("?")[0];
  const view = ["deploy", "progress", "images", "about"].includes(r) ? r : "deploy";
  for (const v of ["deploy", "progress", "images", "about"]) $("#v_" + v).hidden = v !== view;
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === (view === "progress" ? "deploy" : view)));
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// ---- topology form ----------------------------------------------------------
const WAN = [["isp1_intf", "wan1-poort (ZTP)"], ["isp2_intf", "ISP2-poort"], ["mpls_intf", "MPLS-poort"]];
function dev(kind, d, i) {
  const hub = kind === "hubs";
  let h = `<div class="dev" data-k="${kind}" data-i="${i}"><b>${esc(d.name)}</b><div class="fields">`;
  h += fld("name", "Naam", d.name) + fld("serial", "Serienummer", d.serial) + fld("loopback", "Loopback", d.loopback);
  WAN.forEach(([k, l]) => (h += fld(k, l, d[k])));
  h += `<div class="f"><label>wan1-adressering</label><select data-f="wan1_mode"><option value="static"${d.wan1_mode === "static" ? " selected" : ""}>Statisch</option><option value="dhcp"${d.wan1_mode === "dhcp" ? " selected" : ""}>Dynamisch (DHCP)</option></select></div>`;
  h += fld("wan1_ip", "wan1 IP/prefix (statisch)", d.wan1_ip) + fld("wan1_gw", "wan1 gateway (statisch)", d.wan1_gw);
  h += fld("initial_ip", "Initieel IP (alleen SSH)", d.initial_ip);
  h += `<div class="f"><label>FortiFlex-token</label><input data-tok="${esc(d.name)}" type="password" autocomplete="off"></div>`;
  if (hub) ["isp2", "mpls"].forEach((k) => (h += fld(k + "_ip", k.toUpperCase() + " hub-IP", d[k + "_ip"])));
  (d.lans || []).forEach((l, j) => { h += fld("lan" + j + "_intf", "LAN" + (j + 1) + "-poort", l.intf) + fld("lan" + j + "_ip", "LAN" + (j + 1) + " IP/prefix", l.ip); });
  return h + "</div></div>";
}
function draw() {
  const p = T.pve;
  $("#adom").value = T.adom; $("#platform").value = T.platform; $("#psk").value = T.psk; $("#hub2hub").checked = !!T.hub2hub;
  $("#dns1").value = T.dns_primary; $("#dns2").value = T.dns_secondary; $("#wan1_access").value = T.wan1_access;
  $("#fm_name").value = T.fmgr.name; $("#fm_ip").value = T.fmgr.ip; $("#fm_gw").value = T.fmgr.gateway; $("#fm_port").value = T.fmgr.port; $("#fm_init").value = T.fmgr.initial_ip;
  $("#r_name").value = T.region.name; $("#r_as").value = T.region.bgp_as; $("#r_lo").value = T.region.lo_summary;
  $("#pve_url").value = p.url; $("#pve_tid").value = p.token_id; $("#pve_tls").checked = !!p.verify_tls; $("#pve_node").value = p.node;
  $("#pve_dstore").value = p.disk_storage; $("#pve_istore").value = p.import_storage; $("#pve_isostore").value = p.iso_storage;
  $("#pve_br").value = p.default_bridge; $("#pve_cpu").value = p.cpu_type; $("#pve_lic").value = p.license_mode;
  $("#fgt_cores").value = p.fgt.cores; $("#fgt_mem").value = p.fgt.memory_mb;
  $("#fmg_cores").value = p.fmg.cores; $("#fmg_mem").value = p.fmg.memory_mb; $("#fmg_disk").value = p.fmg.data_disk_gb; $("#fmg_br").value = p.fmg.bridge; $("#fmg_vlan").value = p.fmg.vlan ?? "";
  $("#ports").innerHTML = "<thead><tr><th>Poort</th><th>Bridge</th><th>VLAN</th></tr></thead><tbody>" +
    Object.entries(p.ports).map(([k, v]) => `<tr data-p="${k}"><td class="mono">${k}</td><td><input data-f="bridge" list="dl_br" value="${esc(v.bridge)}"></td><td><input data-f="vlan" type="number" value="${v.vlan ?? ""}"></td></tr>`).join("") + "</tbody>";
  $("#vrfs").innerHTML = "<thead><tr><th>VRF-id</th><th>LAN-summary</th><th>DIA</th></tr></thead><tbody>" +
    T.region.vrfs.map((v, i) => `<tr data-v="${i}"><td><input data-f="id" type="number" value="${v.id}"></td><td><input data-f="lan_summary" value="${esc(v.lan_summary)}"></td><td><input data-f="dia" type="checkbox" ${v.dia ? "checked" : ""}></td></tr>`).join("") + "</tbody>";
  ["hubs", "edges"].forEach((k) => ($("#" + k).innerHTML = T[k].map((d, i) => dev(k, d, i)).join("")));
  $("#d0_dev").innerHTML = ["fmg", ...T.hubs.map((d) => d.name), ...T.edges.map((d) => d.name)].map((n) => `<option>${esc(n)}</option>`).join("");
}
function collect() {
  const t = JSON.parse(JSON.stringify(T)), n = (v) => (v === "" ? null : +v);
  t.adom = $("#adom").value.trim(); t.platform = $("#platform").value.trim(); t.psk = $("#psk").value; t.hub2hub = $("#hub2hub").checked;
  t.dns_primary = $("#dns1").value.trim(); t.dns_secondary = $("#dns2").value.trim(); t.wan1_access = $("#wan1_access").value.trim();
  t.fmgr = { name: $("#fm_name").value.trim(), ip: $("#fm_ip").value.trim(), gateway: $("#fm_gw").value.trim(), port: $("#fm_port").value.trim() || "port1", initial_ip: $("#fm_init").value.trim() };
  t.pve = {
    url: $("#pve_url").value.trim(), token_id: $("#pve_tid").value.trim(), verify_tls: $("#pve_tls").checked, node: $("#pve_node").value.trim(),
    disk_storage: $("#pve_dstore").value.trim(), import_storage: $("#pve_istore").value.trim(), iso_storage: $("#pve_isostore").value.trim(),
    default_bridge: $("#pve_br").value.trim(), cpu_type: $("#pve_cpu").value.trim() || "host", license_mode: $("#pve_lic").value,
    fgt: { cores: +$("#fgt_cores").value, memory_mb: +$("#fgt_mem").value },
    fmg: { cores: +$("#fmg_cores").value, memory_mb: +$("#fmg_mem").value, data_disk_gb: +$("#fmg_disk").value, bridge: $("#fmg_br").value.trim(), vlan: n($("#fmg_vlan").value) },
    ports: Object.fromEntries([...document.querySelectorAll("[data-p]")].map((r) => [r.dataset.p, { bridge: r.querySelector("[data-f=bridge]").value.trim(), vlan: n(r.querySelector("[data-f=vlan]").value) }])),
  };
  t.region.name = $("#r_name").value.trim(); t.region.bgp_as = +$("#r_as").value; t.region.lo_summary = $("#r_lo").value.trim();
  t.region.vrfs = [...document.querySelectorAll("[data-v]")].map((r) => ({ id: +r.querySelector("[data-f=id]").value, lan_summary: r.querySelector("[data-f=lan_summary]").value.trim(), dia: r.querySelector("[data-f=dia]").checked }));
  document.querySelectorAll(".dev").forEach((el) => {
    const d = t[el.dataset.k][el.dataset.i], lans = [];
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
  return { fmg_token: $("#fm_token").value.trim() || null, fgt_tokens: tok, fmg_initial_pw: $("#fmg_initpw").value, fgt_initial_pw: $("#fgt_initpw").value, fgt_new_pw: $("#fgt_newpw").value, admin_user: $("#fmg_user").value || "admin", pve_token_secret: $("#pve_secret").value };
}
const fmgCfg = () => ({ host: $("#fmg_host").value.trim() || null, username: $("#fmg_user").value, password: $("#fmg_pass").value });

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
$("#btn_test").onclick = async () => {
  $("#test_res").className = "hint"; $("#test_res").textContent = "…";
  const c = fmgCfg(); c.host = c.host || $("#fm_ip").value.split("/")[0];
  const r = await post("/api/fmg/test", c), j = await json(r);
  $("#test_res").className = "hint " + (r.ok ? "ok" : "error"); $("#test_res").textContent = r.ok ? `OK — ${j.hostname} ${j.version}` : "Fout: " + j.detail;
};
$("#btn_pve").onclick = async () => {
  $("#pve_res").className = "hint"; $("#pve_res").textContent = "…";
  const r = await post("/api/pve/test", { url: $("#pve_url").value.trim(), token_id: $("#pve_tid").value.trim(), token_secret: $("#pve_secret").value, verify_tls: $("#pve_tls").checked, node: $("#pve_node").value.trim() || null });
  const j = await json(r);
  if (!r.ok) { $("#pve_res").className = "hint error"; $("#pve_res").textContent = "Fout: " + j.detail; return; }
  const fill = (id, a) => ($(id).innerHTML = (a || []).map((x) => `<option value="${x}">`).join(""));
  fill("#dl_nodes", j.nodes); fill("#dl_images", j.image_storages); fill("#dl_import", j.import_storages); fill("#dl_iso", j.iso_storages); fill("#dl_br", j.bridges);
  if (!$("#pve_node").value && j.nodes.length === 1) $("#pve_node").value = j.nodes[0];
  if (!$("#pve_dstore").value && j.image_storages.length) $("#pve_dstore").value = j.image_storages[0];
  if (!$("#pve_istore").value && j.import_storages.length) $("#pve_istore").value = j.import_storages[0];
  if (!$("#pve_isostore").value && j.iso_storages.length) $("#pve_isostore").value = j.iso_storages[0];
  $("#pve_res").className = "hint " + (j.warning ? "warn" : "ok"); $("#pve_res").textContent = `OK — Proxmox VE ${j.version}` + (j.warning ? " · ⚠ " + j.warning : "");
};
$("#btn_render").onclick = async () => {
  showErr([]);
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
  const r = await post("/api/cloudinit/preview", { topology: collect(), device: $("#d0_dev").value }), j = await json(r);
  if (!r.ok) return showErr(errList(j));
  $("#d0_out").hidden = false;
  $("#d0_out").textContent = `# ${j.layout} · licentiemodus: ${j.mode} · token/wachtwoord gemaskeerd\n` + j.user_data;
};

// ---- deploy + progress ------------------------------------------------------
function stepsDraw(states = {}) {
  $("#pl").innerHTML = STEPS.map((s) => { const st = states[s.id] || "pending"; return `<li data-id="${s.id}" class="st-${st}"><span class="ic"><span class="dot st-${st}"></span></span><span class="t">${esc(s.title)}</span><span class="msg"></span></li>`; }).join("");
}
$("#btn_deploy").onclick = async () => {
  showErr([]);
  const dry = $("#dry").checked, mf = $("#m_fmg").value, mg = $("#m_fgt").value;
  if (!dry && !confirm("Dit schrijft naar FortiManager" + (mf !== "none" || mg !== "none" ? ", maakt/configureert en licenseert de apparaten (die herstarten)" : "") + ". Doorgaan?")) return;
  const r = await post("/api/deploy", { topology: collect(), fmg: fmgCfg(), dry_run: dry, bootstrap: { fmg: mf, fgt: mg }, secrets: secrets(), images: { fgt: $("#img_fgt").value || null, fmg: $("#img_fmg").value || null } });
  const j = await json(r);
  if (!r.ok) return showErr(errList(j));
  if (ES) ES.close();
  $("#p_title").textContent = `Uitrol van ${$("#adom").value.trim()}` + (dry ? " (dry-run)" : "");
  $("#p_sum").className = "summary"; $("#p_sum").textContent = "Bezig…";
  $("#log").textContent = ""; stepsDraw(); location.hash = "#/progress";
  ES = new EventSource(`/api/jobs/${j.job}/stream`);
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
      ES.close();
      $("#p_sum").className = "summary " + (s.state === "done" ? "ok" : "error");
      $("#p_sum").textContent = s.state === "done" ? "✔ Klaar" : "✕ Mislukt — zie de log";
      $("#p_sum").style.padding = "12px 16px";
    }
  });
};

// ---- init -------------------------------------------------------------------
(async () => {
  const a = await (await fetch("/api/about")).json();
  STEPS = a.steps;
  $("#steps").innerHTML = a.steps.map((s) => `<li><b>${esc(s.title)}</b><span>${esc(s.desc)}</span></li>`).join("");
  $("#orch").textContent = a.orchestrator_present ? "Orchestrator-templates gevonden: " + a.orchestrator : "⚠ Orchestrator-templates niet gevonden op " + a.orchestrator;
  T = await (await fetch("/api/defaults")).json();
  draw(); stepsDraw(); await imgLoad(); route();
})();
