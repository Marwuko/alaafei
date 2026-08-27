"""District referral dashboard.

One page with one job: show whether referred mothers and children
arrived, and which referrals need action now. Server rendered, no
external assets, no build step. The page polls /dashboard/data every
ten seconds so the demo updates live while a judge watches.

No authentication yet: acceptable for a hackathon demo on seeded data,
and called out in the README. District login comes with the pilot.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from sqlalchemy import text as sql

from app.auth import require_user
from app.db import SessionLocal
from app.models import Facility, Household, Nurse, Referral, ReferralStatus

router = APIRouter()


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass
class Stats:
    total: int
    arrived: int
    escalated: int
    open_count: int
    completion_pct: int
    median_hours: float | None


async def compute_stats(session) -> tuple[Stats, list[dict]]:
    referrals = (
        (await session.execute(select(Referral).order_by(Referral.created_at.desc())))
        .scalars()
        .all()
    )
    arrived = [r for r in referrals if r.status in (ReferralStatus.ARRIVED, ReferralStatus.CLOSED)]
    escalated = [r for r in referrals if r.status == ReferralStatus.ESCALATED]
    open_refs = [
        r
        for r in referrals
        if r.status in (ReferralStatus.REGISTERED, ReferralStatus.CAREGIVER_NOTIFIED)
    ]

    durations = sorted(
        (_as_utc(r.arrived_at) - _as_utc(r.created_at)).total_seconds() / 3600
        for r in arrived
        if r.arrived_at is not None
    )
    median_hours = None
    if durations:
        mid = len(durations) // 2
        median_hours = (
            durations[mid]
            if len(durations) % 2
            else (durations[mid - 1] + durations[mid]) / 2
        )

    total = len(referrals)
    stats = Stats(
        total=total,
        arrived=len(arrived),
        escalated=len(escalated),
        open_count=len(open_refs),
        completion_pct=round(100 * len(arrived) / total) if total else 0,
        median_hours=round(median_hours, 1) if median_hours is not None else None,
    )

    rows = []
    for r in referrals[:50]:
        household = await session.get(Household, r.household_id)
        nurse = await session.get(Nurse, r.nurse_id)
        facility = await session.get(Facility, r.facility_id)
        rows.append(
            {
                "id": r.id,
                "patient": r.patient_name,
                "danger_sign": r.danger_sign,
                "community": household.community if household else "",
                "nurse": nurse.name if nurse else "",
                "facility": facility.name if facility else "",
                "status": r.status.value,
                "registered": _as_utc(r.created_at).strftime("%d %b %H:%M"),
                "registered_iso": _as_utc(r.created_at).isoformat(),
            }
        )
    return stats, rows


@router.get("/dashboard/data", dependencies=[Depends(require_user)])
async def dashboard_data():
    async with SessionLocal() as session:
        stats, rows = await compute_stats(session)
    return JSONResponse({"stats": stats.__dict__, "rows": rows})


@router.get("/dashboard/referral/{rid}", dependencies=[Depends(require_user)])
async def referral_thread(rid: int):
    """Every message that passed through Alaafei about one referral. This is
    what makes the audit trail real rather than claimed."""
    async with SessionLocal() as session:
        head = (
            await session.execute(
                sql(
                    "SELECT r.id, r.patient_name, r.danger_sign, r.status, "
                    "h.caregiver_name, h.community "
                    "FROM referrals r JOIN households h ON h.id = r.household_id "
                    "WHERE r.id = :r"
                ),
                {"r": rid},
            )
        ).first()
        msgs = (
            await session.execute(
                sql(
                    "SELECT direction, body, created_at, delivered "
                    "FROM referral_messages WHERE referral_id = :r "
                    "ORDER BY id ASC"
                ),
                {"r": rid},
            )
        ).fetchall()
    if head is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "id": head[0], "patient": head[1], "danger_sign": head[2],
        "status": str(head[3]), "contact": head[4], "community": head[5],
        "messages": [
            {"direction": m[0], "body": m[1],
             "at": str(m[2]), "delivered": bool(m[3])}
            for m in msgs
        ],
    })


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alaafei · Referral watch</title>
<style>
:root{
  --ink:#16211C; --paper:#F7F5EF; --card:#FFFFFF; --leaf:#126B49;
  --leaf-soft:#E4F0EA; --harmattan:#C04A28; --harm-soft:#FBE8E1;
  --sand:#E4DDCD; --khaki:#727A6B;
}
*{box-sizing:border-box;margin:0}
body{background:var(--paper);color:var(--ink);
  font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:0 0 4rem}
header{display:flex;justify-content:space-between;align-items:center;
  padding:1.1rem 2rem;background:var(--ink);color:var(--paper)}
header h1{font-size:1.05rem;font-weight:750;letter-spacing:.01em}
header h1 span{color:#7FD4AC}
.stamp{font-size:.75rem;opacity:.75;font-variant-numeric:tabular-nums;
  display:flex;align-items:center;gap:.45rem}
.pulse{width:.5rem;height:.5rem;border-radius:50%;background:#7FD4AC}
.pulse.flash{animation:ping .9s ease-out}
@keyframes ping{0%{transform:scale(1);opacity:1}
  50%{transform:scale(2.4);opacity:.35}100%{transform:scale(1);opacity:1}}
main{max-width:1000px;margin:0 auto;padding:0 2rem}
.hero{display:flex;align-items:center;gap:2.2rem;padding:2rem 0 1.4rem;flex-wrap:wrap}
.ring{position:relative;width:118px;height:118px;flex:0 0 auto}
.ring svg{transform:rotate(-90deg)}
.ring .val{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center}
.ring .val b{font-size:1.6rem;font-weight:780;font-variant-numeric:tabular-nums;line-height:1}
.ring .val i{font-style:normal;font-size:.62rem;letter-spacing:.11em;
  text-transform:uppercase;color:var(--khaki);margin-top:.15rem}
.hero-copy h2{font-size:1.45rem;font-weight:700;line-height:1.25;max-width:26ch}
.hero-copy .sub{color:var(--khaki);font-size:.92rem;margin-top:.4rem}
.hero-copy .alarm{color:var(--harmattan);font-weight:650}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:.8rem;padding:.4rem 0 1.6rem}
.tile{background:var(--card);border:1px solid var(--sand);border-radius:10px;padding:.85rem 1rem}
.tile .n{font-size:1.45rem;font-weight:750;font-variant-numeric:tabular-nums;line-height:1.1}
.tile .l{font-size:.66rem;text-transform:uppercase;letter-spacing:.11em;
  color:var(--khaki);margin-top:.3rem}
.tile.warn{background:var(--harm-soft);border-color:#EFC7B7}
.tile.warn .n{color:var(--harmattan)}
.sec{font-size:.68rem;text-transform:uppercase;letter-spacing:.13em;
  color:var(--khaki);margin:1.2rem 0 .6rem}
.card{background:var(--card);border:1px solid var(--sand);border-radius:10px;
  padding:.9rem 1.1rem;margin-bottom:.6rem;display:flex;gap:1.1rem;
  align-items:center;flex-wrap:wrap;transition:background .5s}
.card.new{background:var(--leaf-soft)}
.card.urgent{border-color:#EFC7B7;background:var(--harm-soft)}
.card .id{font-variant-numeric:tabular-nums;color:var(--khaki);font-size:.8rem;
  flex:0 0 2.2rem;font-weight:650}
.card .who{flex:1 1 190px;min-width:0}
.card .who b{font-size:1rem;font-weight:700}
.card .who div{color:var(--khaki);font-size:.83rem}
.card .where{flex:1 1 170px;font-size:.83rem;color:var(--khaki)}
.track{flex:1 1 260px;display:flex;align-items:flex-start;gap:0}
.step{flex:1;text-align:center;position:relative}
.step .bead{width:.7rem;height:.7rem;border-radius:50%;background:var(--sand);
  margin:0 auto .3rem;position:relative;z-index:1}
.step.done .bead{background:var(--leaf)}
.step.miss .bead{background:var(--harmattan)}
.step .cap{font-size:.62rem;letter-spacing:.04em;color:var(--khaki);line-height:1.25}
.step.done .cap{color:var(--leaf);font-weight:650}
.step.miss .cap{color:var(--harmattan);font-weight:650}
.step:not(:first-child):before{content:"";position:absolute;height:2px;
  background:var(--sand);top:.29rem;right:50%;left:-50%}
.step.done:not(:first-child):before{background:var(--leaf)}
.step.miss:not(:first-child):before{background:var(--harmattan)}
.waited{flex:0 0 auto;font-size:.75rem;color:var(--khaki);
  font-variant-numeric:tabular-nums;text-align:right;min-width:5.5rem}
.card.urgent .waited{color:var(--harmattan);font-weight:650}
.card{cursor:pointer}
.card:hover{border-color:var(--leaf)}
#veil{position:fixed;inset:0;background:rgba(22,33,28,.45);display:none;
  align-items:flex-end;justify-content:center;z-index:20}
#veil.on{display:flex}
#panel{background:var(--paper);width:100%;max-width:640px;max-height:82vh;
  overflow:auto;border-radius:14px 14px 0 0;padding:1.4rem 1.5rem 2rem}
#panel h2{font-size:1.05rem;margin-bottom:.15rem}
#panel .meta{color:var(--khaki);font-size:.83rem;margin-bottom:1.1rem}
#panel .close{float:right;cursor:pointer;color:var(--khaki);font-size:1.3rem;
  line-height:1;padding:0 .3rem}
.msg{margin-bottom:.7rem;max-width:82%;padding:.6rem .8rem;border-radius:10px;
  font-size:.9rem}
.msg .when{display:block;font-size:.7rem;color:var(--khaki);margin-top:.3rem;
  font-variant-numeric:tabular-nums}
.msg.in{background:var(--card);border:1px solid var(--sand)}
.msg.out{background:var(--leaf-soft);margin-left:auto}
.msg.held{opacity:.65;border-style:dashed}
.nothing{color:var(--khaki);font-size:.88rem}
.empty{padding:3rem 0;color:var(--khaki)}
@media (max-width:640px){main,header{padding-left:1rem;padding-right:1rem}
  .hero-copy h2{font-size:1.2rem}.where{display:none}}
</style>
</head>
<body>
<header>
  <h1>Alaafei <span>·</span> Savelugu district referral watch</h1>
  <div class="stamp"><span class="pulse" id="pulse"></span><span id="stamp">connecting</span><a href="/logout" style="color:inherit;opacity:.65;margin-left:14px;font-size:12px;text-decoration:none">Sign out</a></div>
</header>
<main>
  <div class="hero">
    <div class="ring">
      <svg width="118" height="118">
        <circle cx="59" cy="59" r="50" fill="none" stroke="#E4DDCD" stroke-width="10"/>
        <circle id="arc" cx="59" cy="59" r="50" fill="none" stroke="#126B49"
          stroke-width="10" stroke-linecap="round" stroke-dasharray="314"
          stroke-dashoffset="314" style="transition:stroke-dashoffset .8s ease"/>
      </svg>
      <div class="val"><b id="pct">0%</b><i>arrived</i></div>
    </div>
    <div class="hero-copy">
      <h2 id="lead">Loading referrals…</h2>
      <div class="sub" id="sub"></div>
    </div>
  </div>
  <div class="tiles" id="tiles"></div>
  <div class="sec" id="sechead"></div>
  <div id="list"></div>
</main>
<script>
const STEPS=[["registered","referred"],["notified","message sent"],
             ["arrived","arrived"],["closed","closed"]];
let seen={};
function track(status){
  const order=["registered","notified","arrived","closed"];
  const esc = status==="escalated";
  const idx = esc ? 1 : order.indexOf(status);
  return '<div class="track">'+STEPS.map(([k,cap],i)=>{
    let cls="step";
    if(esc && i===2) cls+=" miss";
    else if(i<=idx) cls+=" done";
    if(esc && i===2) cap="overdue";
    return '<div class="'+cls+'"><div class="bead"></div><div class="cap">'+cap+'</div></div>';
  }).join("")+'</div>';
}
function hrs(reg){
  const d=new Date(reg);
  if(isNaN(d)) return "";
  const h=(Date.now()-d.getTime())/36e5;
  if(h<1) return Math.round(h*60)+" min ago";
  if(h<48) return h.toFixed(1)+" h ago";
  return Math.round(h/24)+" d ago";
}
async function refresh(){
  try{
    const res=await fetch("/dashboard/data");
    const {stats,rows}=await res.json();
    const pct=stats.completion_pct||0;
    document.getElementById("pct").textContent=pct+"%";
    document.getElementById("arc").setAttribute("stroke-dashoffset",314-(314*pct/100));
    const lead=document.getElementById("lead"), sub=document.getElementById("sub");
    if(stats.total===0){
      lead.textContent="No referrals yet.";
      sub.textContent="The first one a nurse registers will appear here.";
    }else{
      lead.innerHTML=stats.arrived+" of "+stats.total+" referrals reached care.";
      sub.innerHTML = stats.escalated>0
        ? '<span class="alarm">'+stats.escalated+(stats.escalated===1?" referral needs":" referrals need")+
          ' follow up now.</span> Every one is being tracked until it closes.'
        : "Every open referral is being tracked until it closes.";
    }
    const t=[["Open now",stats.open_count,""],
             ["Needs follow up",stats.escalated,stats.escalated>0?"warn":""],
             ["Median hours to arrival",stats.median_hours ?? "—",""],
             ["Completion",pct+"%",""]];
    document.getElementById("tiles").innerHTML=t.map(([l,n,c])=>
      '<div class="tile '+c+'"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>').join("");
    const rank={escalated:0,registered:1,notified:2,arrived:3,closed:4};
    rows.sort((a,b)=>(rank[a.status]??9)-(rank[b.status]??9)||b.id-a.id);
    document.getElementById("sechead").textContent=rows.length?"Referral journeys":"";
    document.getElementById("list").innerHTML = rows.length ? rows.map(r=>{
      const fresh = seen[r.id] && seen[r.id]!==r.status;
      const cls="card"+(r.status==="escalated"?" urgent":"")+(fresh?" new":"");
      seen[r.id]=r.status;
      return '<div class="'+cls+'" onclick="openThread('+r.id+')">'+
        '<div class="id">#'+r.id+'</div>'+
        '<div class="who"><b>'+r.patient+'</b><div>'+r.danger_sign+'</div></div>'+
        '<div class="where">'+r.community+' → '+r.facility+'</div>'+
        track(r.status)+'<div class="waited">'+hrs(r.registered_iso)+'</div></div>';
    }).join("") : '<div class="empty">Nothing to show yet.</div>';
    const p=document.getElementById("pulse");
    p.classList.remove("flash"); void p.offsetWidth; p.classList.add("flash");
    document.getElementById("stamp").textContent="live · "+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("stamp").textContent="connection lost, retrying";
  }
}
async function openThread(id){
  const veil=document.getElementById("veil");
  const panel=document.getElementById("panel");
  panel.innerHTML='<div class="nothing">Loading...</div>';
  veil.classList.add("on");
  try{
    const res=await fetch("/dashboard/referral/"+id);
    const d=await res.json();
    let html='<span class="close" onclick="closeThread()">&times;</span>'+
      '<h2>'+d.patient+' · referral #'+d.id+'</h2>'+
      '<div class="meta">'+d.danger_sign+' · '+d.community+
      ' · contact: '+d.contact+'</div>';
    if(!d.messages.length){
      html+='<div class="nothing">No messages yet on this referral.</div>';
    }else{
      html+=d.messages.map(m=>{
        const out=m.direction==="to_family";
        const cls="msg "+(out?"out":"in")+(m.delivered?"":" held");
        const who=out?"Health worker":d.patient;
        const when=new Date(m.at.replace(" ","T")).toLocaleString();
        return '<div class="'+cls+'"><b>'+who+'</b><br>'+
          m.body.replace(/</g,"&lt;")+
          '<span class="when">'+when+
          (m.delivered?"":" · waiting to send")+'</span></div>';
      }).join("");
    }
    panel.innerHTML=html;
  }catch(e){
    panel.innerHTML='<span class="close" onclick="closeThread()">&times;</span>'+
      '<div class="nothing">Could not load that conversation.</div>';
  }
}
function closeThread(){document.getElementById("veil").classList.remove("on");}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeThread();});

refresh();
setInterval(refresh,5000);
</script>
<div id="veil" onclick="if(event.target===this)closeThread()">
  <div id="panel"></div>
</div>
</body>
</html>"""


@router.get("/dashboard", dependencies=[Depends(require_user)])
async def dashboard():
    return HTMLResponse(PAGE)
