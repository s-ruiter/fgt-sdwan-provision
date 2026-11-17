const $=s=>document.querySelector(s);let T=null;
const WAN=[["isp1_intf","wan1 poort (ZTP)"],["isp2_intf","ISP2 poort"],["mpls_intf","MPLS poort"]];
const esc=v=>String(v??"").replace(/"/g,"&quot;");
function dev(kind,d,i){const hub=kind=="hubs";let h=`<div class="dev" data-k="${kind}" data-i="${i}"><b>${d.name}</b><div class="grid">`;
 const f=(k,l,v)=>`<label>${l}<input data-f="${k}" value="${esc(v)}"></label>`;
 h+=f("name","Naam",d.name)+f("serial","Serienummer",d.serial)+f("loopback","Loopback",d.loopback);
 WAN.forEach(([k,l])=>h+=f(k,l,d[k]));
 h+=`<label>wan1 adressering<select data-f="wan1_mode"><option value="static"${d.wan1_mode=="static"?" selected":""}>statisch</option><option value="dhcp"${d.wan1_mode=="dhcp"?" selected":""}>dynamisch (DHCP)</option></select></label>`;
 h+=f("wan1_ip","wan1 IP/prefix (statisch)",d.wan1_ip)+f("wan1_gw","wan1 gateway (statisch)",d.wan1_gw)+f("initial_ip","Initieel IP (nu bereikbaar)",d.initial_ip);
 h+=`<label>FortiFlex-token<input data-tok="${esc(d.name)}" type="password" autocomplete="off"></label>`;
 if(hub)["isp1","isp2","mpls"].forEach(k=>{if(k!="isp1")h+=f(k+"_ip",k.toUpperCase()+" hub-IP",d[k+"_ip"])});
 (d.lans||[]).forEach((l,j)=>{h+=f("lan"+j+"_intf","LAN"+(j+1)+" poort",l.intf)+f("lan"+j+"_ip","LAN"+(j+1)+" IP/prefix",l.ip)});
 return h+"</div></div>"}
let IMGS=[];
function pveDraw(){const p=T.pve;$("#pve_url").value=p.url;$("#pve_tid").value=p.token_id;$("#pve_tls").checked=!!p.verify_tls;$("#pve_node").value=p.node;
 $("#pve_dstore").value=p.disk_storage;$("#pve_istore").value=p.import_storage;$("#pve_isostore").value=p.iso_storage;$("#pve_br").value=p.default_bridge;$("#pve_cpu").value=p.cpu_type;$("#pve_lic").value=p.license_mode;
 $("#fgt_cores").value=p.fgt.cores;$("#fgt_mem").value=p.fgt.memory_mb;$("#fmg_cores").value=p.fmg.cores;$("#fmg_mem").value=p.fmg.memory_mb;$("#fmg_disk").value=p.fmg.data_disk_gb;$("#fmg_br").value=p.fmg.bridge;$("#fmg_vlan").value=p.fmg.vlan??"";
 $("#ports").innerHTML="<tr><td>poort</td><td>bridge</td><td>VLAN</td></tr>"+Object.entries(p.ports).map(([k,v])=>`<tr data-p="${k}"><td>${k}</td><td><input data-f="bridge" list="dl_br" value="${esc(v.bridge)}"></td><td><input data-f="vlan" type="number" value="${v.vlan??""}"></td></tr>`).join("")}
function pveCollect(){const n=v=>v===""?null:+v;return {url:$("#pve_url").value.trim(),token_id:$("#pve_tid").value.trim(),verify_tls:$("#pve_tls").checked,node:$("#pve_node").value.trim(),
 disk_storage:$("#pve_dstore").value.trim(),import_storage:$("#pve_istore").value.trim(),iso_storage:$("#pve_isostore").value.trim(),default_bridge:$("#pve_br").value.trim(),cpu_type:$("#pve_cpu").value.trim()||"host",license_mode:$("#pve_lic").value,
 fgt:{cores:+$("#fgt_cores").value,memory_mb:+$("#fgt_mem").value},fmg:{cores:+$("#fmg_cores").value,memory_mb:+$("#fmg_mem").value,data_disk_gb:+$("#fmg_disk").value,bridge:$("#fmg_br").value.trim(),vlan:n($("#fmg_vlan").value)},
 ports:Object.fromEntries([...document.querySelectorAll("[data-p]")].map(r=>[r.dataset.p,{bridge:r.querySelector("[data-f=bridge]").value.trim(),vlan:n(r.querySelector("[data-f=vlan]").value)}]))}}
function imgDraw(){for(const r of ["fgt","fmg"]){const s=$("#img_"+r),cur=s.value;const l=IMGS.filter(i=>i.role==r);s.innerHTML=l.length?l.map(i=>`<option value="${i.id}">${i.filename}${i.version?" (v"+i.version+")":""}</option>`).join(""):'<option value="">— nog geen image —</option>';if(cur&&l.some(i=>i.id==cur))s.value=cur}
 $("#img_list").innerHTML=IMGS.map(i=>`<div class="imgrow">${i.role.toUpperCase()}: ${i.filename} &mdash; ${i.virtual_gb} GB virtueel, sha256 <code>${i.sha256.slice(0,16)}…</code> (vergelijk met de portal)${i.warnings.map(w=>' <span class="e-warn">⚠ '+w+'</span>').join("")}<button data-del="${i.id}">verwijder</button></div>`).join("")}
async function imgLoad(){IMGS=await (await fetch("/api/images")).json();imgDraw()}
function upload(role){const f=$("#up_"+role).files[0];if(!f)return;const fd=new FormData();fd.append("role",role);fd.append("file",f);const x=new XMLHttpRequest();
 x.open("POST","/api/images");x.upload.onprogress=e=>{if(e.lengthComputable)$("#bar_"+role).style.width=(100*e.loaded/e.total)+"%"};
 x.onload=()=>{$("#bar_"+role).style.width="0";if(x.status==200){imgLoad()}else{let d;try{d=JSON.parse(x.responseText).detail}catch(_){d=x.responseText}$("#errors").innerHTML="✕ upload: "+d}$("#up_"+role).value=""};x.send(fd)}
function draw(){pveDraw();$("#adom").value=T.adom;$("#platform").value=T.platform;$("#psk").value=T.psk;$("#hub2hub").checked=!!T.hub2hub;
 $("#dns1").value=T.dns_primary;$("#dns2").value=T.dns_secondary;$("#wan1_access").value=T.wan1_access;
 $("#fm_name").value=T.fmgr.name;$("#fm_ip").value=T.fmgr.ip;$("#fm_gw").value=T.fmgr.gateway;$("#fm_port").value=T.fmgr.port;$("#fm_init").value=T.fmgr.initial_ip;
 $("#r_name").value=T.region.name;$("#r_as").value=T.region.bgp_as;$("#r_lo").value=T.region.lo_summary;
 $("#vrfs").innerHTML="<tr><td>VRF-id</td><td>LAN-summary</td><td>DIA</td></tr>"+T.region.vrfs.map((v,i)=>`<tr data-v="${i}"><td><input data-f="id" type="number" value="${v.id}"></td><td><input data-f="lan_summary" value="${v.lan_summary}"></td><td><input data-f="dia" type="checkbox" ${v.dia?"checked":""}></td></tr>`).join("");
 ["hubs","edges"].forEach(k=>$("#"+k).innerHTML=T[k].map((d,i)=>dev(k,d,i)).join(""))}
function collect(){const t=JSON.parse(JSON.stringify(T));t.adom=$("#adom").value.trim();t.platform=$("#platform").value.trim();t.psk=$("#psk").value;t.hub2hub=$("#hub2hub").checked;
 t.dns_primary=$("#dns1").value.trim();t.dns_secondary=$("#dns2").value.trim();t.wan1_access=$("#wan1_access").value.trim();
 t.fmgr={name:$("#fm_name").value.trim(),ip:$("#fm_ip").value.trim(),gateway:$("#fm_gw").value.trim(),port:$("#fm_port").value.trim()||"port1",initial_ip:$("#fm_init").value.trim()};
 t.pve=pveCollect();t.region.name=$("#r_name").value.trim();t.region.bgp_as=+$("#r_as").value;t.region.lo_summary=$("#r_lo").value.trim();
 t.region.vrfs=[...document.querySelectorAll("[data-v]")].map(r=>({id:+r.querySelector('[data-f=id]').value,lan_summary:r.querySelector('[data-f=lan_summary]').value.trim(),dia:r.querySelector('[data-f=dia]').checked}));
 document.querySelectorAll(".dev").forEach(el=>{const d=t[el.dataset.k][el.dataset.i];const lans=[];
  el.querySelectorAll("[data-f]").forEach(inp=>{const k=inp.dataset.f,v=inp.value.trim();const m=k.match(/^lan(\d+)_(intf|ip)$/);
   if(m){(lans[+m[1]]??={})[m[2]]=v}else d[k]=v});d.lans=lans});return t}
function secrets(){const tok={};document.querySelectorAll("[data-tok]").forEach(i=>{if(i.value.trim())tok[i.dataset.tok]=i.value.trim()});
 return {fmg_token:$("#fm_token").value.trim()||null,fgt_tokens:tok,fmg_initial_pw:$("#fmg_initpw").value,fgt_initial_pw:$("#fgt_initpw").value,fgt_new_pw:$("#fgt_newpw").value,admin_user:$("#fmg_user").value||"admin",pve_token_secret:$("#pve_secret").value}}
const post=(u,b)=>fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
const fmg=()=>({host:$("#fmg_host").value.trim()||null,username:$("#fmg_user").value,password:$("#fmg_pass").value})
function showErr(j){const e=j.detail||j.errors||[];$("#errors").innerHTML=(Array.isArray(e)?e:[e]).map(x=>"✕ "+(x.msg||x)).join("<br>")}
$("#btn_test").onclick=async()=>{$("#test_res").textContent="…";const c=fmg();c.host=c.host||$("#fm_ip").value.split("/")[0];const r=await post("/api/fmg/test",c);const j=await r.json();$("#test_res").textContent=r.ok?`OK — ${j.hostname} ${j.version}`:"Fout: "+j.detail};
$("#btn_render").onclick=async()=>{$("#errors").innerHTML="";const r=await post("/api/render",{topology:collect()});const j=await r.json();if(!r.ok){showErr(j);return}
 $("#preview").hidden=false;const names=Object.keys(j.configs);$("#tabs").innerHTML=names.map(n=>`<button data-n="${n}">${n} (${j.lines[n]})</button>`).join("");
 const sel=n=>{$("#cfg").textContent=j.configs[n];document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("on",b.dataset.n==n))};
 $("#tabs").onclick=e=>e.target.dataset.n&&sel(e.target.dataset.n);sel(names[0])};
$("#btn_deploy").onclick=async()=>{$("#errors").innerHTML="";const dry=$("#dry").checked;
 if(!dry&&!confirm("Dit schrijft naar FortiManager"+($("#m_fmg").value!="none"||$("#m_fgt").value!="none"?", maakt/configureert en licenseert de apparaten (die herstarten)":"")+". Doorgaan?"))return;
 const r=await post("/api/deploy",{topology:collect(),fmg:fmg(),dry_run:dry,bootstrap:{fmg:$("#m_fmg").value,fgt:$("#m_fgt").value},secrets:secrets(),images:{fgt:$("#img_fgt").value||null,fmg:$("#img_fmg").value||null}});const j=await r.json();if(!r.ok){showErr(j);return}
 $("#progress").hidden=false;$("#log").textContent="";
 const es=new EventSource(`/api/jobs/${j.job}/stream`);
 es.addEventListener("log",e=>{const x=JSON.parse(e.data);const s=document.createElement("div");s.className="e-"+x.status;s.textContent=`${x.t} ${x.step?"["+x.step+"] ":""}${x.msg}`;$("#log").append(s);$("#log").scrollTop=1e9});
 es.addEventListener("state",e=>{const s=JSON.parse(e.data);document.querySelectorAll("#pl li").forEach(li=>li.className=s.steps[li.dataset.id]);if(s.state!="running"){es.close();const d=document.createElement("div");d.textContent=s.state=="done"?"✔ Klaar":"✕ Mislukt";$("#log").append(d)}})};
$("#btn_pve").onclick=async()=>{$("#pve_res").textContent="…";const r=await post("/api/pve/test",{url:$("#pve_url").value.trim(),token_id:$("#pve_tid").value.trim(),token_secret:$("#pve_secret").value,verify_tls:$("#pve_tls").checked,node:$("#pve_node").value.trim()||null});const j=await r.json();
 if(!r.ok){$("#pve_res").textContent="Fout: "+j.detail;return}
 const fill=(id,a)=>{$(id).innerHTML=(a||[]).map(x=>`<option value="${x}">`).join("")};fill("#dl_nodes",j.nodes);fill("#dl_images",j.image_storages);fill("#dl_import",j.import_storages);fill("#dl_iso",j.iso_storages);fill("#dl_br",j.bridges);
 if(!$("#pve_node").value&&j.nodes.length==1)$("#pve_node").value=j.nodes[0];if(!$("#pve_dstore").value&&j.image_storages.length)$("#pve_dstore").value=j.image_storages[0];if(!$("#pve_istore").value&&j.import_storages.length)$("#pve_istore").value=j.import_storages[0];if(!$("#pve_isostore").value&&j.iso_storages.length)$("#pve_isostore").value=j.iso_storages[0];
 $("#pve_res").textContent=`OK — PVE ${j.version}`+(j.warning?" ⚠ "+j.warning:"")};
$("#up_fgt").onchange=()=>upload("fgt");$("#up_fmg").onchange=()=>upload("fmg");
$("#img_list").onclick=async e=>{const id=e.target.dataset.del;if(id&&confirm("Image verwijderen?")){await fetch("/api/images/"+id,{method:"DELETE"});imgLoad()}};
$("#btn_d0").onclick=async()=>{$("#errors").innerHTML="";const r=await post("/api/cloudinit/preview",{topology:collect(),device:$("#d0_dev").value});const j=await r.json();if(!r.ok){showErr(j);return}
 $("#d0_out").hidden=false;$("#d0_out").textContent=`# ${j.layout} (licentiemodus: ${j.mode}; token/wachtwoord gemaskeerd)\n`+j.user_data};
(async()=>{const a=await (await fetch("/api/about")).json();
 $("#steps").innerHTML=a.steps.map(s=>`<li><b>${s.title}</b> — ${s.desc}</li>`).join("");$("#pl").innerHTML=a.steps.map(s=>`<li data-id="${s.id}">${s.title}</li>`).join("");
 $("#orch").textContent=a.orchestrator_present?"Orchestrator-templates gevonden: "+a.orchestrator:"⚠ Orchestrator-templates niet gevonden op "+a.orchestrator;
 T=await (await fetch("/api/defaults")).json();draw();await imgLoad();
 $("#d0_dev").innerHTML=["fmg",...T.hubs.map(d=>d.name),...T.edges.map(d=>d.name)].map(n=>`<option>${n}</option>`).join("")})();
