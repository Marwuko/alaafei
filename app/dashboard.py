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

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

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
            }
        )
    return stats, rows


@router.get("/dashboard/data")
async def dashboard_data():
    async with SessionLocal() as session:
        stats, rows = await compute_stats(session)
    return JSONResponse({"stats": stats.__dict__, "rows": rows})


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alaafei. Referral watch</title>
<style>
:root{
  --ink:#1F2A24; --paper:#FAF8F3; --leaf:#1B7A55; --harmattan:#C75B39;
  --sand:#E7E0D2; --khaki:#6B7263;
}
*{box-sizing:border-box;margin:0}
body{background:var(--paper);color:var(--ink);
  font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:0 0 4rem}
header{display:flex;justify-content:space-between;align-items:baseline;
  padding:1.4rem 2rem;border-bottom:2px solid var(--ink)}
header h1{font-size:1.15rem;font-weight:800;letter-spacing:.01em}
header h1 span{color:var(--leaf)}
.stamp{font-size:.78rem;color:var(--khaki);font-variant-numeric:tabular-nums}
main{max-width:960px;margin:0 auto;padding:0 2rem}
.lead{padding:2.2rem 0 1.6rem;border-bottom:1px solid var(--sand)}
.lead p{font-size:1.7rem;font-weight:650;line-height:1.25;max-width:34ch}
.lead p strong{color:var(--leaf);font-variant-numeric:tabular-nums}
.lead p .bad{color:var(--harmattan)}
.tiles{display:flex;gap:2.5rem;padding:1.2rem 0 1.6rem;flex-wrap:wrap}
.tile .n{font-size:1.5rem;font-weight:750;font-variant-numeric:tabular-nums}
.tile .l{font-size:.72rem;text-transform:uppercase;letter-spacing:.12em;color:var(--khaki)}
table{width:100%;border-collapse:collapse;margin-top:.4rem}
th{font-size:.7rem;text-transform:uppercase;letter-spacing:.12em;color:var(--khaki);
  text-align:left;padding:.55rem .6rem .45rem;border-bottom:1px solid var(--ink)}
td{padding:.7rem .6rem;border-bottom:1px solid var(--sand);font-size:.92rem;vertical-align:top}
td.num{font-variant-numeric:tabular-nums;color:var(--khaki)}
.sign{color:var(--khaki);font-size:.82rem}
.loop{display:flex;gap:.35rem;align-items:center;margin-top:.15rem}
.dot{width:.55rem;height:.55rem;border-radius:50%;background:var(--sand)}
.dot.on{background:var(--leaf)}
.dot.alert{background:var(--harmattan)}
.seg{flex:0 0 1.1rem;height:2px;background:var(--sand)}
.seg.on{background:var(--leaf)}
.seg.alert{background:var(--harmattan)}
.state{font-size:.78rem;font-weight:650}
.state.arrived,.state.closed{color:var(--leaf)}
.state.escalated{color:var(--harmattan)}
.state.registered,.state.notified{color:var(--khaki)}
.empty{padding:3rem 0;color:var(--khaki);font-size:1.05rem}
@media (max-width:640px){main,header{padding-left:1rem;padding-right:1rem}
  .lead p{font-size:1.3rem}.hide-sm{display:none}}
</style>
</head>
<body>
<header>
  <h1>Alaafei <span>·</span> Savelugu district referral watch</h1>
  <div class="stamp" id="stamp">loading</div>
</header>
<main>
  <div class="lead"><p id="lead">Loading referrals…</p></div>
  <div class="tiles" id="tiles"></div>
  <div id="tablewrap"></div>
</main>
<script>
const STATES = ["registered","notified","arrived"];
function loopTrack(status){
  if(status==="closed") status="arrived";
  let html = '<div class="loop">';
  const idx = STATES.indexOf(status);
  const alert = status==="escalated";
  STATES.forEach((s,i)=>{
    const reached = alert ? i<2 : i<=idx;
    const cls = alert && i===2 ? "dot alert" : (reached ? "dot on" : "dot");
    if(i>0){
      const segReached = alert ? i<=2 : i<=idx;
      html += '<div class="'+(alert && i===2 ? "seg alert" : (segReached ? "seg on" : "seg"))+'"></div>';
    }
    html += '<div class="'+cls+'"></div>';
  });
  return html + '</div>';
}
function stateLabel(s){
  const words = {registered:"registered", notified:"family reminded",
    arrived:"arrived", closed:"closed", escalated:"needs follow up"};
  return '<span class="state '+s+'">'+(words[s]||s)+'</span>';
}
async function refresh(){
  try{
    const res = await fetch("/dashboard/data");
    const {stats, rows} = await res.json();
    const lead = document.getElementById("lead");
    if(stats.total===0){
      lead.innerHTML = "No referrals yet. The first one a nurse registers will appear here.";
    } else {
      let s = "<strong>"+stats.arrived+" of "+stats.total+"</strong> referred families confirmed arrived";
      if(stats.escalated>0) s += ", and <span class='bad'>"+stats.escalated+
        (stats.escalated===1?" referral needs":" referrals need")+" follow up now</span>";
      lead.innerHTML = s + ".";
    }
    const t=[["Completion","%s%%".replace("%s",stats.completion_pct)],
      ["Open now",stats.open_count],
      ["Needs follow up",stats.escalated],
      ["Median hours to arrival",stats.median_hours ?? "no data yet"]];
    document.getElementById("tiles").innerHTML =
      t.map(([l,n])=>'<div class="tile"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>').join("");
    if(rows.length){
      document.getElementById("tablewrap").innerHTML =
        '<table><tr><th>Referral</th><th>Patient</th><th class="hide-sm">Community</th>'+
        '<th class="hide-sm">Facility</th><th>Journey</th><th class="hide-sm">Registered</th></tr>'+
        rows.map(r=>'<tr><td class="num">'+r.id+'</td>'+
          '<td>'+r.patient+'<div class="sign">'+r.danger_sign+'</div></td>'+
          '<td class="hide-sm">'+r.community+'</td>'+
          '<td class="hide-sm">'+r.facility+'</td>'+
          '<td>'+stateLabel(r.status)+loopTrack(r.status)+'</td>'+
          '<td class="num hide-sm">'+r.registered+'</td></tr>').join("")+'</table>';
    } else {
      document.getElementById("tablewrap").innerHTML =
        '<div class="empty">Nothing to show yet.</div>';
    }
    document.getElementById("stamp").textContent =
      "live · updated "+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("stamp").textContent = "connection lost, retrying";
  }
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


@router.get("/dashboard")
async def dashboard():
    return HTMLResponse(PAGE)
