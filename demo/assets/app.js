/* ===========================================================================
   Precipitation -> MQTT  ·  demo behaviour
   All client-side, no backend. Three pages share this file; each block runs
   only if the elements it needs are present on the page.
   =========================================================================== */
"use strict";

/* ---- shared helpers ----------------------------------------------------- */
function toast(text, isErr) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = text;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = isErr ? "err" : ""; }, 3200);
}
function setText(id, v) { const e = document.getElementById(id); if (e) e.textContent = v; }
function esc(s) { const d = document.createElement("div"); d.textContent = String(s); return d.innerHTML; }
const fmt = v => (v === null || v === undefined) ? "—"
  : (v === true ? "yes" : (v === false ? "no" : v));
function isoNow() { return new Date().toISOString().replace(/\.\d+Z$/, "Z"); }
function agoText(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso); if (isNaN(t)) return iso;
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

/* =========================================================================
   DASHBOARD
   ========================================================================= */
(function dashboard() {
  if (!document.getElementById("rulebody")) return;

  // Base "weather" the demo nudges around; scenarios override pieces of it.
  const SCENARIOS = {
    dry:     { is_raining: false, precip_accum_in: 0.00, precipitation_probability: 10,
               temperature: 72, humidity: 45, wind_speed_mph: 6,
               short_forecast: "Sunny", active_alerts: [], connected: true },
    wet:     { is_raining: true,  precip_accum_in: 0.31, precipitation_probability: 90,
               temperature: 58, humidity: 88, wind_speed_mph: 9,
               short_forecast: "Light Rain", active_alerts: ["Flood Watch"], connected: true },
    accum:   { is_raining: false, precip_accum_in: 0.42, precipitation_probability: 30,
               temperature: 63, humidity: 70, wind_speed_mph: 7,
               short_forecast: "Mostly Cloudy", active_alerts: [], connected: true },
    freeze:  { is_raining: false, precip_accum_in: 0.00, precipitation_probability: 20,
               temperature: 29, humidity: 60, wind_speed_mph: 12,
               short_forecast: "Clear and Cold", active_alerts: ["Freeze Warning"], connected: true },
    offline: { is_raining: false, precip_accum_in: 0.05, precipitation_probability: 15,
               temperature: 66, humidity: 52, wind_speed_mph: 8,
               short_forecast: "Partly Cloudy", active_alerts: [], connected: false },
  };
  const LOOKBACK = 24;
  let current = "wet";
  let lastChange = { irrigation_rain_inhibit: isoNow(), any_nws_alert: isoNow() };
  let prevActive = {};

  function round(n, d) { const p = Math.pow(10, d); return Math.round(n * p) / p; }

  // Re-derive rule state the same way the monitor does, for the demo rules.
  function deriveRules(m) {
    const rules = [
      { name: "irrigation_rain_inhibit", description: "Hold irrigation when raining or >= 0.25 in / 24h",
        topic: "irrigation/rain_inhibit", on_match: "INHIBIT", on_clear: "ALLOW",
        active: m.is_raining || m.precip_accum_in >= 0.25 },
      { name: "any_nws_alert", description: "Flag whenever any NWS alert is active",
        topic: "facility/weather/nws_alert", on_match: "1", on_clear: "0",
        active: m.active_alerts.length > 0 },
    ];
    for (const r of rules) {
      if (prevActive[r.name] !== undefined && prevActive[r.name] !== r.active) lastChange[r.name] = isoNow();
      prevActive[r.name] = r.active;
      r.current_payload = r.active ? r.on_match : r.on_clear;
      r.last_change = lastChange[r.name];
    }
    return rules;
  }

  function buildState() {
    const s = SCENARIOS[current];
    // gentle drift so it feels live without changing rule outcomes
    const d = (base, amp, dec) => round(base + (Math.random() - 0.5) * amp, dec);
    const m = {
      is_raining: s.is_raining,
      precip_accum_in: round(s.precip_accum_in, 2),
      precipitation_probability: Math.max(0, Math.min(100, Math.round(d(s.precipitation_probability, 6, 0)))),
      temperature: d(s.temperature, 1.5, 1),
      humidity: Math.max(0, Math.min(100, Math.round(d(s.humidity, 4, 0)))),
      wind_speed_mph: Math.max(0, Math.round(d(s.wind_speed_mph, 3, 0))),
      short_forecast: s.short_forecast,
      active_alerts: s.active_alerts,
    };
    return { updated: isoNow(), lookback_hours: LOOKBACK, mqtt_connected: s.connected,
             metrics: m, rules: deriveRules(m) };
  }

  function render(s) {
    const conn = document.getElementById("connstate");
    const up = !!s.mqtt_connected;
    conn.innerHTML = '<span class="dot ' + (up ? "up" : "down") + '"></span>MQTT ' + (up ? "connected" : "offline");

    const irr = s.rules.find(r => /irrigation|rain_inhibit/.test(r.name));
    const d = document.getElementById("directive");
    const card = document.getElementById("directive-card");
    let st = "unknown";
    if (irr && irr.active !== null && irr.active !== undefined) {
      st = irr.active ? "inhibit" : "allow";
      d.className = "big " + st;
      setText("directive", irr.current_payload + (irr.active ? " — do NOT water" : " — watering allowed"));
      setText("directive-sub", "topic " + irr.topic + (irr.last_change ? " · changed " + agoText(irr.last_change) : ""));
    } else {
      d.className = "big unknown"; setText("directive", "UNKNOWN");
      setText("directive-sub", "Waiting on weather data.");
    }
    if (card) card.className = "card state-" + st;

    const m = s.metrics;
    const up2 = document.getElementById("updated");
    if (up2) { up2.textContent = "updated " + agoText(s.updated); up2.title = s.updated; }
    setText("m_rain", fmt(m.is_raining));
    setText("m_accum", fmt(m.precip_accum_in) + " in");
    setText("m_accum_k", "rain last " + s.lookback_hours + "h");
    setText("m_prob", fmt(m.precipitation_probability) + "%");
    setText("m_temp", fmt(m.temperature) + "°F");
    setText("m_hum", fmt(m.humidity) + "%");
    setText("m_wind", fmt(m.wind_speed_mph));
    const alerts = m.active_alerts.length ? m.active_alerts.join(", ") : "none";
    setText("forecast", m.short_forecast + " · alerts: " + alerts);

    const tb = document.getElementById("rulebody");
    tb.innerHTML = "";
    for (const r of s.rules) {
      const pill = r.active ? '<span class="pill on">active</span>' : '<span class="pill off">clear</span>';
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>" + esc(r.name) + '<div class="muted">' + esc(r.description) + "</div></td>" +
        "<td><code>" + esc(r.topic) + "</code></td><td>" + pill + "</td>" +
        "<td>" + esc(r.current_payload) + '</td><td class="muted">' + esc(agoText(r.last_change)) + "</td>";
      tb.appendChild(tr);
    }
    const dash = document.getElementById("dash");
    if (dash) dash.classList.remove("loading");
  }

  function tick() { render(buildState()); }

  document.querySelectorAll(".chip[data-scn]").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chip[data-scn]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      current = btn.dataset.scn;
      tick();
      toast("Scenario: " + btn.textContent.trim());
    });
  });

  tick();
  setInterval(tick, 4000);
})();

/* =========================================================================
   SETTINGS  ·  client-side validation mirroring the server's range checks
   ========================================================================= */
(function settings() {
  const form = document.getElementById("settings-form");
  if (!form) return;

  function validateField(el) {
    // scope to THIS field's own error box (direct child of its wrapper), not the
    // first .field-err in an ancestor card
    const errBox = el.parentElement.querySelector(":scope > .field-err");
    const raw = (el.value || "").trim();
    const type = el.dataset.type;          // "num" | "int" | undefined
    let err = "";
    if (raw === "") {
      // numeric fields and explicitly-required text fields can't be blank
      if (type || el.dataset.required) err = "Required.";
    } else if (type) {
      const n = Number(raw);
      if (isNaN(n)) err = "Must be a number.";
      else if (type === "int" && !Number.isInteger(n)) err = "Must be a whole number.";
      else if (el.dataset.min !== undefined && n < Number(el.dataset.min)) err = "Min " + el.dataset.min + ".";
      else if (el.dataset.max !== undefined && n > Number(el.dataset.max)) err = "Max " + el.dataset.max + ".";
    }
    if (errBox) errBox.textContent = err;
    el.classList.toggle("invalid", !!err);
    return !err;
  }

  form.querySelectorAll("input[data-type],input[data-required]").forEach(el => {
    el.addEventListener("input", () => validateField(el));
    el.addEventListener("blur", () => validateField(el));
  });

  form.addEventListener("submit", e => {
    e.preventDefault();
    let ok = true;
    form.querySelectorAll("input[data-type],input[data-required]").forEach(el => {
      if (!validateField(el)) ok = false;
    });
    if (!ok) { toast("Could not save: fix the highlighted fields.", true); return; }
    toast("Settings saved (demo — nothing was written).");
  });
})();

/* =========================================================================
   RULES  ·  structured form builder + lightweight YAML-shape validator
   Mirrors the live webui.py Rules page; client-side only (no persistence).
   ========================================================================= */
(function rules() {
  const form = document.getElementById("rules-form");
  if (!form) return;

  const METRICS = {
    is_raining:                {type:"bool",   ops:["==","!="]},
    precip_accum_in:           {type:"number", ops:["<","<=",">",">=","==","!="]},
    precipitation_probability: {type:"number", ops:["<","<=",">",">=","==","!="]},
    temperature:               {type:"number", ops:["<","<=",">",">=","==","!="]},
    wind_speed_mph:            {type:"number", ops:["<","<=",">",">=","==","!="]},
    humidity:                  {type:"number", ops:["<","<=",">",">=","==","!="]},
    short_forecast:            {type:"text",   ops:["contains","equals"]},
    active_alert:              {type:"alert",  ops:["any","contains","equals"]},
  };
  const METRIC_NAMES = Object.keys(METRICS);
  const builder = document.getElementById("builder");

  /* ---- builder ---------------------------------------------------------- */
  function el(tag, cls, html){ const e=document.createElement(tag); if(cls)e.className=cls; if(html!=null)e.innerHTML=html; return e; }
  function opt(v, label, sel){ const o=document.createElement("option"); o.value=v; o.textContent=label||v; if(sel)o.selected=true; return o; }

  function valueControl(metric, value){
    const meta = METRICS[metric] || {type:"text"};
    let c;
    if(meta.type==="bool"){
      c=document.createElement("select"); c.className="c-val";
      c.appendChild(opt("true","true", String(value)==="true"));
      c.appendChild(opt("false","false", String(value)!=="true"));
    } else if(meta.type==="number"){
      c=document.createElement("input"); c.className="c-val"; c.type="number"; c.step="any";
      c.value = value!=null ? value : ""; c.placeholder="number";
    } else {
      c=document.createElement("input"); c.className="c-val"; c.type="text";
      c.value = value!=null ? value : ""; c.placeholder="text";
    }
    c.setAttribute("aria-label","condition value");
    return c;
  }
  function fillOps(sel, metric, chosen){
    sel.innerHTML="";
    const ops=(METRICS[metric]||{ops:[]}).ops;
    ops.forEach(o=> sel.appendChild(opt(o,o, o===chosen)));
    if(!ops.includes(chosen) && ops.length) sel.value=ops[0];
  }
  function condRow(cond){
    cond = cond || {metric:METRIC_NAMES[0], operator:"", value:""};
    const row = el("div","cond row");
    const metricWrap = el("div"); const m=document.createElement("select"); m.className="c-metric";
    m.setAttribute("aria-label","metric");
    METRIC_NAMES.forEach(n=> m.appendChild(opt(n,n, n===cond.metric)));
    if(!METRICS[cond.metric]) m.value=METRIC_NAMES[0];
    metricWrap.appendChild(m);
    const opWrap = el("div"); const o=document.createElement("select"); o.className="c-op";
    o.setAttribute("aria-label","operator");
    fillOps(o, m.value, cond.operator); opWrap.appendChild(o);
    const valWrap = el("div","c-val-wrap"); valWrap.appendChild(valueControl(m.value, cond.value));
    const rmWrap = el("div","rm"); const rm=el("button","secondary danger mini","×"); rm.type="button"; rmWrap.appendChild(rm);
    function syncValVisible(){
      const meta=METRICS[m.value]||{};
      valWrap.style.display = (meta.type==="alert" && o.value==="any") ? "none" : "";
    }
    m.addEventListener("change", ()=>{ fillOps(o, m.value, o.value);
      valWrap.innerHTML=""; valWrap.appendChild(valueControl(m.value, null)); syncValVisible(); });
    o.addEventListener("change", syncValVisible);
    rm.addEventListener("click", ()=>{ const card=row.closest(".rule-card"); row.remove(); refreshCombine(card); });
    syncValVisible();
    row.appendChild(metricWrap); row.appendChild(opWrap); row.appendChild(valWrap); row.appendChild(rmWrap);
    return row;
  }
  function refreshCombine(card){
    if(!card) return;
    card.querySelector(".combine-wrap").style.display = card.querySelectorAll(".cond").length>1 ? "" : "none";
  }
  function ruleCard(rule){
    rule = rule || {name:"",description:"",topic:"",on_match:"",on_clear:"",combine:"any",conditions:[]};
    const card = el("div","rule-card");
    card.innerHTML =
      '<div class="rhead"><span class="idx"></span></div>'+
      '<div class="row"><div><label>Name <input class="f-name"></label></div>'+
      '<div><label>Topic <input class="f-topic"></label></div></div>'+
      '<label>Description <span class="hint">(optional)</span> <input class="f-desc"></label>'+
      '<div class="row"><div><label>Payload when matched <span class="hint">(on_match)</span> <input class="f-onmatch"></label></div>'+
      '<div><label>Payload when cleared <span class="hint">(on_clear, optional)</span> <input class="f-onclear"></label></div></div>'+
      '<div class="combine-wrap"><label>When there are multiple conditions, match'+
      ' <select class="f-combine"></select></label></div>'+
      '<label style="margin-top:14px">Conditions</label><div class="conds"></div>'+
      '<div class="btnrow"><button type="button" class="secondary mini add-cond">+ Add condition</button>'+
      '<button type="button" class="danger mini remove-rule">Remove rule</button></div>';
    card.querySelector(".f-name").value = rule.name||"";
    card.querySelector(".f-topic").value = rule.topic||"";
    card.querySelector(".f-desc").value = rule.description||"";
    card.querySelector(".f-onmatch").value = rule.on_match||"";
    card.querySelector(".f-onclear").value = rule.on_clear||"";
    const comb = card.querySelector(".f-combine");
    comb.appendChild(opt("any","ANY is true (OR)", rule.combine!=="all"));
    comb.appendChild(opt("all","ALL are true (AND)", rule.combine==="all"));
    const conds = card.querySelector(".conds");
    (rule.conditions && rule.conditions.length ? rule.conditions : [null]).forEach(c=> conds.appendChild(condRow(c)));
    card.querySelector(".add-cond").addEventListener("click", ()=>{ conds.appendChild(condRow()); refreshCombine(card); });
    card.querySelector(".remove-rule").addEventListener("click", ()=>{ card.remove(); reindex(); });
    refreshCombine(card);
    return card;
  }
  function reindex(){
    [...builder.querySelectorAll(".rule-card")].forEach((c,i)=>{
      c.querySelector(".idx").textContent = "Rule "+(i+1)+(i===0?" · irrigation":"");
    });
  }
  function collect(){
    return [...builder.querySelectorAll(".rule-card")].map(card=>{
      const conds = [...card.querySelectorAll(".cond")].map(row=>{
        const metric=row.querySelector(".c-metric").value;
        const operator=row.querySelector(".c-op").value;
        const meta=METRICS[metric]||{};
        let value="";
        if(!(meta.type==="alert" && operator==="any")){
          const ctrl=row.querySelector(".c-val-wrap .c-val"); value=ctrl?ctrl.value:"";
        }
        return {metric, operator, value};
      });
      return {
        name: card.querySelector(".f-name").value.trim(),
        description: card.querySelector(".f-desc").value.trim(),
        topic: card.querySelector(".f-topic").value.trim(),
        on_match: card.querySelector(".f-onmatch").value,
        on_clear: card.querySelector(".f-onclear").value,
        combine: card.querySelector(".f-combine").value,
        conditions: conds,
      };
    });
  }
  function validateForm(data){
    if(!data.length) return "Add at least one rule.";
    for(let i=0;i<data.length;i++){
      const r=data[i], label="Rule "+(i+1);
      if(!r.name) return label+": name is required.";
      if(!r.topic) return "Rule '"+r.name+"': topic is required.";
      if(r.on_match==="") return "Rule '"+r.name+"': the on_match payload is required.";
      if(!r.conditions.length) return "Rule '"+r.name+"': add at least one condition.";
      for(const c of r.conditions){
        const meta=METRICS[c.metric]||{};
        if(meta.type==="alert" && c.operator==="any") continue;
        if(c.value==="") return "Rule '"+r.name+"': the "+c.metric+" condition needs a value.";
        if(meta.type==="number" && isNaN(Number(c.value))) return "Rule '"+r.name+"': "+c.metric+" needs a numeric value.";
      }
    }
    return "";
  }

  document.getElementById("add-rule").addEventListener("click", ()=>{ builder.appendChild(ruleCard()); reindex(); });
  document.querySelectorAll(".tab").forEach(t=> t.addEventListener("click", ()=>{
    document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById("tab-form").style.display = t.dataset.tab==="form"?"":"none";
    document.getElementById("tab-yaml").style.display = t.dataset.tab==="yaml"?"":"none";
  }));

  /* ---- YAML tab (heuristic shape check) --------------------------------- */
  const ta = document.getElementById("rules_yaml");
  const errBox = document.getElementById("rules-err");
  function validateYaml(text){
    if (!text.trim()) return "Rules list is empty.";
    const lines = text.replace(/\t/g, "  ").split("\n");
    const items = []; let cur=null, startLine=0;
    lines.forEach((ln,i)=>{
      if(/^- /.test(ln)){ if(cur!==null) items.push({text:cur,line:startLine}); cur=ln+"\n"; startLine=i+1; }
      else if(cur!==null){ cur+=ln+"\n"; }
    });
    if(cur!==null) items.push({text:cur,line:startLine});
    if(!items.length) return "Expected a YAML list (each rule starts with '- ').";
    for(let k=0;k<items.length;k++){
      const blk=items[k].text.replace(/^- /,"  ");
      const label="rule #"+(k+1)+" (line "+items[k].line+")";
      for(const key of ["name","when","topic","on_match"])
        if(!new RegExp("(^|\\n)\\s*"+key+":").test(blk)) return label+": missing '"+key+"'.";
      const mm=blk.match(/metric:\s*([A-Za-z_]+)/g)||[];
      if(!mm.length) return label+": needs at least one 'metric:' under 'when'.";
      for(const x of mm){ const n=x.split(":")[1].trim(); if(!METRIC_NAMES.includes(n)) return label+": unknown metric '"+n+"'."; }
      const pl=blk.match(/(on_match|on_clear):\s*([^\n#]*)/g)||[];
      for(const p of pl){ const v=p.split(":").slice(1).join(":").trim();
        if(/^(on|off|yes|no|true|false)$/i.test(v)) return label+": quote the payload \""+v+"\" (unquoted it becomes a boolean)."; }
    }
    return "";
  }
  function checkYaml(showOk){
    const err=validateYaml(ta.value); errBox.textContent=err; errBox.style.color=err?"#fda4a4":"#86efac";
    if(!err && showOk) errBox.textContent="Looks valid ✓"; return !err;
  }
  const EXAMPLE = "\n- name: high_wind_hold\n  description: \"Pause watering in high wind\"\n  when:\n    metric: wind_speed_mph\n    operator: \">=\"\n    value: 25\n  topic: \"facility/weather/high_wind\"\n  on_match: \"1\"\n  on_clear: \"0\"\n";
  document.getElementById("add-example").addEventListener("click", ()=>{ ta.value=ta.value.replace(/\s*$/,"")+"\n"+EXAMPLE; ta.focus(); checkYaml(true); });
  document.getElementById("check").addEventListener("click", ()=> checkYaml(true));
  ta.addEventListener("input", ()=>{ errBox.textContent=""; });

  /* ---- submit (mode depends on which Save was clicked) ------------------ */
  let mode = "form";
  document.getElementById("save-form").addEventListener("click", ()=> mode="form");
  document.getElementById("save-yaml").addEventListener("click", ()=> mode="yaml");
  form.addEventListener("submit", e=>{
    e.preventDefault();
    if(mode==="form"){
      const data=collect(); const err=validateForm(data);
      const box=document.getElementById("form-err");
      box.textContent=err; box.style.color="#fda4a4";
      if(err){ toast("Could not save: "+err, true); return; }
      toast("Rules saved (demo — nothing was written).");
    } else {
      if(checkYaml(false)) toast("Rules saved (demo — nothing was written).");
      else toast("Could not save: "+errBox.textContent, true);
    }
  });

  /* ---- initial render --------------------------------------------------- */
  const INITIAL = window.DEMO_RULES || [];
  (INITIAL.length ? INITIAL : [null]).forEach(r=> builder.appendChild(ruleCard(r)));
  reindex();
  document.querySelector('.tab[data-tab="form"]').click();
})();
