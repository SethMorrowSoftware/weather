/* ===========================================================================
   Read-only status dashboard. Fetches status.json (written by ingest.php from
   the on-site controller's pushes) and renders it. No controls, no MQTT, no
   path back into the network — display only.
   =========================================================================== */
"use strict";

// If the last update is older than this, the controller is probably offline /
// not pushing. Default 30 min = 2x a typical 15-min poll. Adjust to taste.
const STALE_SECONDS = 1800;
const REFRESH_MS = 30000;

function setText(id, v){ const e=document.getElementById(id); if(e) e.textContent=v; }
function esc(s){ const d=document.createElement("div"); d.textContent=String(s); return d.innerHTML; }
const fmt = v => (v===null||v===undefined) ? "—" : (v===true?"yes":(v===false?"no":v));
function agoText(iso){
  if(!iso) return "—";
  const t=Date.parse(iso); if(isNaN(t)) return iso;
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<5) return "just now"; if(s<60) return s+"s ago";
  if(s<3600) return Math.round(s/60)+"m ago"; return Math.round(s/3600)+"h ago";
}
function ageSeconds(iso){ const t=Date.parse(iso||""); return isNaN(t)?Infinity:(Date.now()-t)/1000; }

function render(s, ok){
  const conn = document.getElementById("connstate");
  const card = document.getElementById("directive-card");
  const stale = document.getElementById("stale");

  if(!ok || !s || !s.updated){
    conn.innerHTML = '<span class="dot down"></span>no status';
    document.getElementById("directive").className = "big unknown";
    card.className = "card state-unknown";
    setText("directive","NO DATA");
    setText("directive-sub","No status received yet from the controller.");
    document.getElementById("rulebody").innerHTML =
      '<tr><td colspan="5" class="muted">No data.</td></tr>';
    document.getElementById("dash").classList.remove("loading");
    return;
  }

  // staleness banner (controller may be offline / not pushing)
  const age = ageSeconds(s.updated);
  if(age > STALE_SECONDS){
    stale.style.display = "";
    stale.textContent = "⚠ Status is " + agoText(s.updated) +
      " — the controller may be offline or not pushing updates.";
  } else {
    stale.style.display = "none";
  }

  // connection (MQTT state reported by the controller) + freshness
  const up = !!s.mqtt_connected;
  const fresh = age <= STALE_SECONDS;
  conn.innerHTML = '<span class="dot '+(fresh ? (up?'up':'down') : 'idle')+'"></span>' +
    (fresh ? ('MQTT '+(up?'connected':'offline')) : 'stale');

  // directive (first irrigation / rain_inhibit rule)
  const rules = s.rules || [];
  const irr = rules.find(r => /irrigation|rain_inhibit/.test(r.name||""));
  const d = document.getElementById("directive");
  let st = "unknown";
  if(irr && irr.active !== null && irr.active !== undefined){
    st = irr.active ? "inhibit" : "allow";
    d.className = "big " + st;
    setText("directive", (irr.current_payload ?? "?") + (irr.active ? " — do NOT water" : " — watering allowed"));
    setText("directive-sub", "topic " + irr.topic + (irr.last_change ? " · changed " + agoText(irr.last_change) : ""));
  } else {
    d.className = "big unknown";
    setText("directive","UNKNOWN");
    setText("directive-sub","No irrigation rule data yet.");
  }
  card.className = "card state-" + st;

  const m = s.metrics || {};
  const u = document.getElementById("updated");
  u.textContent = "updated " + agoText(s.updated); u.title = s.updated;
  setText("m_rain", fmt(m.is_raining));
  setText("m_accum", fmt(m.precip_accum_in) + " in");
  setText("m_accum_k", "rain last " + (s.lookback_hours ?? "?") + "h");
  setText("m_prob", fmt(m.precipitation_probability) + "%");
  setText("m_temp", fmt(m.temperature) + "°F");
  setText("m_hum", fmt(m.humidity) + "%");
  setText("m_wind", fmt(m.wind_speed_mph));
  const alerts = (m.active_alerts && m.active_alerts.length) ? m.active_alerts.join(", ") : "none";
  setText("forecast", (m.short_forecast || "—") + " · alerts: " + alerts);

  const tb = document.getElementById("rulebody");
  tb.innerHTML = "";
  for(const r of rules){
    let pill;
    if(r.active===null||r.active===undefined) pill='<span class="pill na">n/a</span>';
    else if(r.active) pill='<span class="pill on">active</span>';
    else pill='<span class="pill off">clear</span>';
    const tr=document.createElement("tr");
    tr.innerHTML='<td>'+esc(r.name)+'<div class="muted">'+esc(r.description||"")+'</div></td>'+
      '<td><code>'+esc(r.topic)+'</code></td><td>'+pill+'</td>'+
      '<td>'+(r.current_payload!=null?esc(r.current_payload):"—")+'</td>'+
      '<td class="muted">'+esc(agoText(r.last_change))+'</td>';
    tb.appendChild(tr);
  }
  if(!rules.length) tb.innerHTML='<tr><td colspan="5" class="muted">No rules.</td></tr>';
  document.getElementById("dash").classList.remove("loading");
}

async function tick(){
  try{
    const r = await fetch("status.json", {cache:"no-store"});
    render(r.ok ? await r.json() : null, r.ok);
  }catch(e){
    render(null, false);
  }
}
tick();
setInterval(tick, REFRESH_MS);
