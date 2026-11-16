const $=s=>document.querySelector(s);let T=null;
const WAN=[["isp1_intf","ISP1 poort"],["isp2_intf","ISP2 poort"],["mpls_intf","MPLS poort"]];
function dev(kind,d,i){const hub=kind=="hubs";let h=`<div class="dev" data-k="${kind}" data-i="${i}"><b>${d.name}</b><div class="grid">`;
 const f=(k,l,v)=>`<label>${l}<input data-f="${k}" value="${v??""}"></label>`;
 h+=f("name","Naam",d.name)+f("serial","Serienummer",d.serial)+f("loopback","Loopback",d.loopback);
 WAN.forEach(([k,l])=>h+=f(k,l,d[k]));
 if(hub)["isp1","isp2","mpls"].forEach(k=>h+=f(k+"_ip",k.toUpperCase()+" hub-IP",d[k+"_ip"]));
 (d.lans||[]).forEach((l,j)=>{h+=f("lan"+j+"_intf","LAN"+(j+1)+" poort",l.intf)+f("lan"+j+"_ip","LAN"+(j+1)+" IP/prefix",l.ip)});
 return h+"</div></div>"}
function draw(){$("#adom").value=T.adom;$("#platform").value=T.platform;$("#psk").value=T.psk;$("#hub2hub").checked=!!T.hub2hub;$("#r_name").value=T.region.name;$("#r_as").value=T.region.bgp_as;$("#r_lo").value=T.region.lo_summary;
 $("#vrfs").innerHTML="<tr><td>VRF-id</td><td>LAN-summary</td><td>DIA</td></tr>"+T.region.vrfs.map((v,i)=>`<tr data-v="${i}"><td><input data-f="id" type="number" value="${v.id}"></td><td><input data-f="lan_summary" value="${v.lan_summary}"></td><td><input data-f="dia" type="checkbox" ${v.dia?"checked":""}></td></tr>`).join("");
 ["hubs","edges"].forEach(k=>$("#"+k).innerHTML=T[k].map((d,i)=>dev(k,d,i)).join(""))}
function collect(){const t=JSON.parse(JSON.stringify(T));t.adom=$("#adom").value.trim();t.platform=$("#platform").value.trim();t.psk=$("#psk").value;t.hub2hub=$("#hub2hub").checked;
 t.region.name=$("#r_name").value.trim();t.region.bgp_as=+$("#r_as").value;t.region.lo_summary=$("#r_lo").value.trim();
 t.region.vrfs=[...document.querySelectorAll("[data-v]")].map(r=>({id:+r.querySelector('[data-f=id]').value,lan_summary:r.querySelector('[data-f=lan_summary]').value.trim(),dia:r.querySelector('[data-f=dia]').checked}));
 document.querySelectorAll(".dev").forEach(el=>{const d=t[el.dataset.k][el.dataset.i];const lans=[];
  el.querySelectorAll("[data-f]").forEach(inp=>{const k=inp.dataset.f,v=inp.value.trim();const m=k.match(/^lan(\d+)_(intf|ip)$/);
   if(m){(lans[+m[1]]??={})[m[2]]=v}else d[k]=v});d.lans=lans});return t}
const post=(u,b)=>fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
const fmg=()=>({host:$("#fmg_host").value.trim(),username:$("#fmg_user").value,password:$("#fmg_pass").value,api_token:$("#fmg_token").value||null});
function showErr(j){const e=j.detail||j.errors||[];$("#errors").innerHTML=(Array.isArray(e)?e:[e]).map(x=>"✕ "+(x.msg||x)).join("<br>")}
$("#btn_test").onclick=async()=>{$("#test_res").textContent="…";const r=await post("/api/fmg/test",fmg());const j=await r.json();$("#test_res").textContent=r.ok?`OK — ${j.hostname} ${j.version}`:"Fout: "+j.detail};
$("#btn_render").onclick=async()=>{$("#errors").innerHTML="";const r=await post("/api/render",{topology:collect()});const j=await r.json();if(!r.ok){showErr(j);return}
 $("#preview").hidden=false;const names=Object.keys(j.configs);$("#tabs").innerHTML=names.map(n=>`<button data-n="${n}">${n} (${j.lines[n]})</button>`).join("");
 const sel=n=>{$("#cfg").textContent=j.configs[n];document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("on",b.dataset.n==n))};
 $("#tabs").onclick=e=>e.target.dataset.n&&sel(e.target.dataset.n);sel(names[0])};
$("#btn_deploy").onclick=async()=>{$("#errors").innerHTML="";const dry=$("#dry").checked;
 if(!dry&&!confirm("Dit schrijft naar FortiManager (ADOM + devices + scripts). Doorgaan?"))return;
 const r=await post("/api/deploy",{topology:collect(),fmg:fmg(),dry_run:dry});const j=await r.json();if(!r.ok){showErr(j);return}
 $("#progress").hidden=false;$("#log").textContent="";
 const es=new EventSource(`/api/jobs/${j.job}/stream`);
 es.addEventListener("log",e=>{const x=JSON.parse(e.data);const s=document.createElement("div");s.className="e-"+x.status;s.textContent=`${x.t} ${x.step?"["+x.step+"] ":""}${x.msg}`;$("#log").append(s);$("#log").scrollTop=1e9});
 es.addEventListener("state",e=>{const s=JSON.parse(e.data);document.querySelectorAll("#pl li").forEach(li=>li.className=s.steps[li.dataset.id]);if(s.state!="running"){es.close();const d=document.createElement("div");d.textContent=s.state=="done"?"✔ Klaar":"✕ Mislukt";$("#log").append(d)}})};
(async()=>{const a=await (await fetch("/api/about")).json();
 $("#steps").innerHTML=a.steps.map(s=>`<li><b>${s.title}</b> — ${s.desc}</li>`).join("");$("#pl").innerHTML=a.steps.map(s=>`<li data-id="${s.id}">${s.title}</li>`).join("");
 $("#orch").textContent=a.orchestrator_present?"Orchestrator-templates gevonden: "+a.orchestrator:"⚠ Orchestrator-templates niet gevonden op "+a.orchestrator;
 T=await (await fetch("/api/defaults")).json();draw()})();
