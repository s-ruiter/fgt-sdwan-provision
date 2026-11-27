// ---- Demo dashboard: the running deployment as a picture; click a device to connect, a WAN link to change it ------
// Pure part (no DOM at load time, tested in node by tests/test_dashboard.py): dashModel -> positions, dashSvg -> SVG.
const dEsc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const DASH_CLOUDS = { hub: ["isp-hub", "ISP-HUB"], a: ["isp-a", "ISP-A"], b: ["isp-b", "ISP-B"] };

// what is set on a link, short: "" (neutral), "down", or e.g. "80 ms · ±5 ms · 2 % · 2000 kbit/s"
function dashLabel(s) {
  if (!s) return "";
  if (s.down) return "down";
  const p = [];
  if (+s.delay_ms) p.push(`${+s.delay_ms} ms`);
  if (+s.jitter_ms) p.push(`±${+s.jitter_ms} ms`);
  if (+s.loss_pct) p.push(`${+s.loss_pct} %`);
  if (+s.rate_kbit) p.push(`${+s.rate_kbit} kbit/s`);
  return p.join(" · ");
}
const dashState = (s) => (s && s.down ? "down" : dashLabel(s) ? "changed" : "neutral");

// links = /api/wanemu/links .links; access = /api/access (or null); traffic = /api/traffic/status (or null)
function dashModel(links, access, traffic) {
  links = links || [];
  const fgts = [], byName = {};
  for (const l of links) {                                    // FortiGates in the agent's order; hub = has an ISP-HUB link
    let d = byName[l.name];
    if (!d) { d = byName[l.name] = { name: l.name, links: [], role: "spoke" }; fgts.push(d); }
    d.links.push(l);
    if (l.seg === "hub") d.role = "hub";
  }
  const hubs = fgts.filter((d) => d.role === "hub"), spokes = fgts.filter((d) => d.role !== "hub");
  const fmgs = (access && access.fmgs) || [], clients = (access && access.clients) || [];
  const w = Math.max(960, 160 * spokes.length + 200), h = 560, left = 100, right = w - 220;
  const spread = (n, i) => (n <= 1 ? (left + right) / 2 : left + ((right - left) * i) / (n - 1));
  const hubX = (n, i) => left + ((right - left) * (i + 1)) / (n + 1);
  const acc = {};
  for (const f of (access && access.fgts) || []) acc[f.name] = f;
  const nodes = [], edges = [];
  hubs.forEach((d, i) => nodes.push({ id: `fgt-h${i}`, kind: "fgt", role: "hub", name: d.name, x: hubX(hubs.length, i), y: 0.14 * h, access: acc[d.name] || null, links: d.links }));
  spokes.forEach((d, i) => nodes.push({ id: `fgt-s${i}`, kind: "fgt", role: "spoke", name: d.name, x: spread(spokes.length, i), y: 0.72 * h, access: acc[d.name] || null, links: d.links }));
  const segs = new Set(links.map((l) => l.seg));
  const cloudPos = { hub: [(left + right) / 2, 0.37 * h], a: [left + (right - left) * 0.25, 0.5 * h], b: [left + (right - left) * 0.75, 0.5 * h] };
  for (const s of ["hub", "a", "b"]) if (segs.has(s)) nodes.push({ id: DASH_CLOUDS[s][0], kind: "cloud", name: DASH_CLOUDS[s][1], x: cloudPos[s][0], y: cloudPos[s][1] });
  for (const n of nodes.filter((x) => x.kind === "fgt")) {
    for (const l of n.links) {
      if (!DASH_CLOUDS[l.seg]) continue;
      edges.push({ id: l.id, from: n.id, to: DASH_CLOUDS[l.seg][0], kind: "wan", link: l, state: dashState(l.state), label: dashLabel(l.state) });
    }
  }
  fmgs.forEach((f, i) => nodes.push({ id: `fmg-${i}`, kind: "fmg", name: f.name, x: w - 90, y: 0.1 * h + i * 72, access: f }));
  if (links.length) nodes.push({ id: "wanemu", kind: "wanemu", name: "wan-emu", x: w - 90, y: 0.1 * h + Math.max(1, fmgs.length) * 72 + 20 });
  clients.forEach((c, i) => {
    const sp = nodes.find((n) => n.kind === "fgt" && n.name === c.spoke);
    if (!sp) return;
    nodes.push({ id: `cl-${i}`, kind: "client", name: c.name, x: sp.x, y: 0.9 * h, access: c });
    edges.push({ id: `lan-${i}`, from: sp.id, to: `cl-${i}`, kind: "lan", state: "neutral", label: "" });
  });
  if (traffic && traffic.deployed) {
    const first = nodes.find((n) => n.role === "spoke");
    const x = first ? Math.min(first.x + 80, w - 30) : left;
    nodes.push({ id: "tgen", kind: "tgen", name: "Traffic-Gen", x, y: 0.9 * h, access: { gui: traffic.url || "" } });
    if (first) edges.push({ id: "lan-tgen", from: first.id, to: "tgen", kind: "lan", state: "neutral", label: "" });
  }
  return { w, h, nodes, edges };
}

// icons as symbols: FortiGate = red brick wall with a white circle and a flame (drawn here, in that style)
const DASH_DEFS = `<defs>
<symbol id="ic-fgt" viewBox="0 0 64 54"><rect x="1" y="1" width="62" height="52" rx="8" fill="#d52b1e" stroke="#4a4a4a" stroke-width="2"/>
<path d="M1 18H63M1 36H63M16 1V18M48 1V18M32 18V36M16 36V53M48 36V53" stroke="#4a4a4a" stroke-width="2" fill="none"/>
<circle cx="32" cy="27" r="15" fill="#fff" stroke="#4a4a4a" stroke-width="2"/>
<path d="M32 15c5 6 9 9 9 15a9 9 0 0 1-18 0c0-4 2-6 4-8 0 3 1 5 3 5-1-4 0-8 2-12z" fill="#d52b1e" stroke="#4a4a4a" stroke-width="1.5"/>
<path d="M32 26c2 3 4 4 4 7a4 4 0 0 1-8 0c0-2 2-4 4-7z" fill="#fff"/></symbol>
<symbol id="ic-fmg" viewBox="0 0 64 54"><rect x="4" y="4" width="56" height="46" rx="6" fill="var(--card)" stroke="#4a4a4a" stroke-width="2"/>
<rect x="12" y="13" width="40" height="7" rx="2" fill="#d52b1e"/><rect x="12" y="24" width="40" height="7" rx="2" fill="#4a4a4a"/><rect x="12" y="35" width="40" height="7" rx="2" fill="#4a4a4a"/></symbol>
<symbol id="ic-srv" viewBox="0 0 64 54"><rect x="14" y="2" width="36" height="50" rx="5" fill="var(--card)" stroke="#4a4a4a" stroke-width="2"/>
<path d="M20 12H44M20 20H44M20 28H44" stroke="#4a4a4a" stroke-width="2"/><circle cx="32" cy="42" r="3" fill="#4a4a4a"/></symbol>
<symbol id="ic-pc" viewBox="0 0 64 54"><rect x="8" y="4" width="48" height="32" rx="4" fill="var(--card)" stroke="#4a4a4a" stroke-width="2"/>
<path d="M24 46H40M32 36V46" stroke="#4a4a4a" stroke-width="3"/></symbol>
</defs>`;
const DASH_IC = { fgt: ["ic-fgt", 64, 54], fmg: ["ic-fmg", 52, 44], wanemu: ["ic-srv", 44, 38], client: ["ic-pc", 40, 34], tgen: ["ic-pc", 40, 34] };

function dashSvg(m) {
  const at = {};
  m.nodes.forEach((n) => (at[n.id] = n));
  const lines = m.edges.map((e) => {
    const a = at[e.from], b = at[e.to];
    if (!a || !b) return "";
    const ln = `x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"`;
    if (e.kind !== "wan") return `<line class="dl-lan" ${ln}/>`;
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, lw = e.label.length * 7 + 14;
    const lab = e.label ? `<rect class="dl-tag" x="${mx - lw / 2}" y="${my - 11}" width="${lw}" height="20" rx="10"/><text class="dl-txt" x="${mx}" y="${my + 4}">${dEsc(e.label)}</text>` : "";
    return `<g class="de st-${e.state}" data-edge="${dEsc(e.id)}"><title>${dEsc(e.link.name)} ${dEsc(e.link.ip)}</title><line class="dl" ${ln}/><line class="dl-hit" ${ln}/>${lab}</g>`;
  }).join("");
  const nodes = m.nodes.map((n) => {
    if (n.kind === "cloud") {
      const c = `M${n.x - 62} ${n.y + 18}a22 22 0 0 1 8-40a30 30 0 0 1 54-8a26 26 0 0 1 44 12a20 20 0 0 1 2 36z`;
      return `<g class="dn dn-cloud"><path d="${c}"/><text class="dn-txt dn-ctxt" x="${n.x}" y="${n.y + 6}">${dEsc(n.name)}</text></g>`;
    }
    const [ic, iw, ih] = DASH_IC[n.kind] || DASH_IC.client;
    const sub = n.kind === "fgt" ? (n.role === "hub" ? "hub" : "spoke") : n.kind === "fmg" ? "FortiManager" : "";
    return `<g class="dn dn-${n.kind}" data-node="${dEsc(n.id)}" tabindex="0"><title>${dEsc(n.name)}</title>` +
      `<use href="#${ic}" x="${n.x - iw / 2}" y="${n.y - ih / 2}" width="${iw}" height="${ih}"/>` +
      `<text class="dn-txt" x="${n.x}" y="${n.y + ih / 2 + 16}">${dEsc(n.name)}</text>` +
      (sub ? `<text class="dn-sub" x="${n.x}" y="${n.y + ih / 2 + 30}">${sub}</text>` : "") + `</g>`;
  }).join("");
  return `<svg class="dash-svg" viewBox="0 0 ${m.w} ${m.h}" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Topology">${DASH_DEFS}${lines}${nodes}</svg>`;
}

// ---- panels (pure: HTML strings) --------------------------------------------------------------------------------
const DASH_KIND = { fgt: "FortiGate", fmg: "FortiManager", client: "LAN client", tgen: "Traffic-Gen", wanemu: "WAN emulator" };
function dashDevicePanel(n) {
  const a = n.access, key = (a && a.key) || n.name;
  const head = `<div class="dash-ph"><div><b>${dEsc(n.name)}</b><span class="hint">${n.kind === "fgt" ? n.role : DASH_KIND[n.kind] || ""}</span></div><button class="icon" data-dclose="1" title="Close" aria-label="Close">✕</button></div>`;
  let body;
  if (n.kind === "tgen") {
    body = `<div class="btnrow">${a && a.gui ? `<a href="${dEsc(a.gui)}" target="_blank" rel="noopener"><button>GUI</button></a>` : ""}<a href="#/traffic"><button>Traffic tab</button></a></div>`;
  } else if (!a) {
    body = `<div class="hint">No connection data for this device in the tool (it keeps them for what it deployed itself). The WAN links below can still be changed.</div>`;
  } else {
    body = (a.ip ? `<button class="acc-copy mono" data-copy="${dEsc(a.ip)}" title="Copy ${dEsc(a.ip)} to the clipboard">${dEsc(a.ip)}</button>` : "") +
      `<div class="btnrow">` +
      (a.gui ? `<a href="${dEsc(a.gui)}" target="_blank" rel="noopener"><button>GUI</button></a>` : "") +
      (a.ssh ? `<button data-ssh="${dEsc(key)}" data-auto="${a.password ? 1 : ""}">SSH</button>` : "") +
      `<button data-console="${dEsc(key)}" title="Proxmox console (also when the network to the device is down)">Console</button>` +
      (a.password ? `<button data-cpw="${dEsc(key)}" title="Copy the password to the clipboard">Copy password</button>` : "") + `</div>` +
      (a.ssh ? `<button class="acc-copy acc-cmd mono" data-copy="${dEsc(a.ssh)}" title="Copy: ${dEsc(a.ssh)}">${dEsc(a.ssh)}</button>` : "");
  }
  const links = (n.links || []).map((l) => {
    const lab = dashLabel(l.state);
    return `<button class="dash-lk st-${dashState(l.state)}" data-openedge="${dEsc(l.id)}">${dEsc(DASH_CLOUDS[l.seg] ? DASH_CLOUDS[l.seg][1] : l.seg)} <span class="mono">${dEsc(l.ip)}</span>${lab ? ` · ${dEsc(lab)}` : ""}</button>`;
  }).join("");
  return head + body + (links ? `<h3>WAN links</h3><div class="dash-lks">${links}</div>` : "");
}
function dashLinkPanel(e) {
  const l = e.link, st = l.state, cloud = DASH_CLOUDS[l.seg] ? DASH_CLOUDS[l.seg][1] : l.seg;
  return `<div class="dash-link" data-dl="${dEsc(l.id)}"><div class="dash-ph"><div><b>${dEsc(l.name)}</b><span class="hint">${dEsc(cloud)} · <span class="mono">${dEsc(l.ip)}</span></span></div><button class="icon" data-dclose="1" title="Close" aria-label="Close">✕</button></div>` +
    WAN_F.map(([k, lbl, unit, lo, hi, step]) => `<div class="wan-f"><label>${lbl} <output>${dEsc(st[k])}</output> ${unit}</label><input type="range" data-df="${k}" min="${lo}" max="${hi}" step="${step}" value="${dEsc(st[k])}"></div>`).join("") +
    `<div class="wan-f"><label><input type="checkbox" data-df="down" ${st.down ? "checked" : ""}> link down</label></div>` +
    `<div class="btnrow"><button data-dneutral="1" title="No delay, jitter or loss, no bandwidth limit, link up">Neutral</button><span class="hint">RTT +${2 * (+st.delay_ms || 0)} ms</span></div></div>`;
}

// ---- wiring (browser only) -------------------------------------------------------------------------------------
let DASH = null, DASH_SEL = null;                         // last model; selection {type: "node"|"edge", id}
const dashErr = (msg) => ($("#dash-err").innerHTML = msg ? `<div class="error">✕ ${esc(msg)}</div>` : "");
async function dashLoad() {
  const get = (u) => fetch(u, { cache: "no-store" }).then(async (r) => ({ ok: r.ok, status: r.status, j: await r.json().catch(() => ({})) })).catch((e) => ({ ok: false, j: { detail: String(e) } }));
  const [lk, ac, tr] = await Promise.all([get("/api/wanemu/links"), get("/api/access"), get("/api/traffic/status")]);
  if (!lk.ok) { dashErr(lk.j.detail || `HTTP ${lk.status}`); DASH = null; $("#dash-pic").innerHTML = ""; $("#dash-panel").hidden = true; return; }
  if (!(lk.j.links || []).length) { dashErr("Nothing deployed yet: deploy the FortiManager and FortiGates first."); DASH = null; $("#dash-pic").innerHTML = ""; $("#dash-panel").hidden = true; return; }
  dashErr("");
  $("#dash-presets").innerHTML = Object.entries(lk.j.presets || {}).map(([k, l]) => `<button data-dp="${esc(k)}">${esc(l)}</button>`).join("") + `<button data-dreset="1">All neutral</button>`;
  DASH = dashModel(lk.j.links, ac.ok ? ac.j : null, tr.ok ? tr.j : null);
  $("#dash-pic").innerHTML = dashSvg(DASH);
  dashSelect(DASH_SEL, !$("#dash-panel .dirty"));
}
// show the selection: highlight it and (unless a slider is being changed) redraw its panel
function dashSelect(sel, redraw = true) {
  DASH_SEL = sel;
  document.querySelectorAll("#dash-pic .sel").forEach((g) => g.classList.remove("sel"));
  const panel = $("#dash-panel");
  const item = sel && DASH && (sel.type === "node" ? DASH.nodes.find((n) => n.id === sel.id) : DASH.edges.find((e) => e.id === sel.id));
  if (!item) { DASH_SEL = null; panel.hidden = true; panel.innerHTML = ""; return; }
  const g = document.querySelector(`#dash-pic [data-${sel.type}="${CSS.escape(sel.id)}"]`);
  if (g) g.classList.add("sel");
  if (redraw) panel.innerHTML = sel.type === "node" ? dashDevicePanel(item) : dashLinkPanel(item);
  panel.hidden = false;
}
$("#dash-pic").addEventListener("click", (e) => {
  const ed = e.target.closest("[data-edge]"), nd = e.target.closest("[data-node]");
  if (ed) return dashSelect({ type: "edge", id: ed.dataset.edge });
  if (nd && !nd.classList.contains("dn-wanemu")) return dashSelect({ type: "node", id: nd.dataset.node });
  dashSelect(null);
});
$("#dash-pic").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.closest("[data-node]")?.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
async function dashLinkApply(box) {
  const v = {};
  box.querySelectorAll("[data-df]").forEach((i) => (v[i.dataset.df] = i.type === "checkbox" ? i.checked : +i.value));
  const ok = await wanPut(box.dataset.dl, v, dashErr);
  box.classList.remove("dirty");
  if (ok) await dashLoad();
}
$("#dash-panel").addEventListener("input", (e) => {
  const i = e.target.closest("[data-df]"); if (!i) return;
  i.closest("[data-dl]").classList.add("dirty");
  if (i.type === "range") i.previousElementSibling.querySelector("output").textContent = i.value;
});
$("#dash-panel").addEventListener("change", (e) => {        // slider released / "link down" toggled: apply at once
  const i = e.target.closest("[data-df]");
  if (i) dashLinkApply(i.closest("[data-dl]"));
});
$("#dash-panel").addEventListener("click", async (e) => {
  if (e.target.closest("[data-dclose]")) return dashSelect(null);
  const oe = e.target.closest("[data-openedge]");
  if (oe) return dashSelect({ type: "edge", id: oe.dataset.openedge });
  const nb = e.target.closest("[data-dneutral]");
  if (nb) {
    const box = nb.closest("[data-dl]");
    if (await wanPut(box.dataset.dl, { delay_ms: 0, jitter_ms: 0, loss_pct: 0, rate_kbit: 0, down: false }, dashErr)) await dashLoad();
    return;
  }
  accessClick(e, dashErr);
});
$("#dash-presets").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.dp) { if (await wanPresetRun(b.dataset.dp, dashErr)) await dashLoad(); return; }
  if (b.dataset.dreset && confirm("Reset all links to neutral (no delay, no loss, everything up)?")) {
    if (await wanResetAll(dashErr)) await dashLoad();
  }
});
