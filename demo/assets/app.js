/* ===========================================================================
   Automation (Conditions → Actions) · demo behaviour
   All client-side, no backend. Every page shares this file; each block runs
   only if the elements it needs are present on the page. Mirrors the live
   webui.py UI (dashboard with variables + manual control, the rule builder,
   settings, the inputs editor, the activity log, the System health/log page,
   the MQTT console, and the History trends page) but everything is mock data —
   nothing is published or saved.
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
   DASHBOARD  ·  conditions + device states + variables + manual control
   ========================================================================= */
(function dashboard() {
  if (!document.getElementById("devicegrid")) return;

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
  const MANUAL_CONTROL = true;             // demo: controls are enabled
  let current = "wet";
  // operator-set variables (toggled below) and per-device manual overrides
  let variables = [
    { name: "maintenance_mode", type: "bool",   value: false },
    { name: "temp_setpoint",    type: "number", value: 70 },
  ];
  let manual = { irrigation_rain_inhibit: "auto", maintenance_hold: "auto", any_nws_alert: "auto" };
  let lastChange = {};
  let prevActive = {};

  function round(n, d) { const p = Math.pow(10, d); return Math.round(n * p) / p; }
  function varVal(name) { const v = variables.find(x => x.name === name); return v ? v.value : undefined; }

  // Re-derive device state the way the monitor does: rules -> desired, then a
  // manual override (on/off) wins.
  function deriveRules(m) {
    const rules = [
      { name: "irrigation_rain_inhibit", description: "Hold irrigation when raining or ≥ 0.25 in / 24h",
        topic: "irrigation/rain_inhibit", on_match: "INHIBIT", on_clear: "ALLOW", enabled: true,
        desired: m.is_raining || m.precip_accum_in >= 0.25 },
      { name: "maintenance_hold", description: "Pause everything while in maintenance mode",
        topic: "facility/maintenance", on_match: "ON", on_clear: "OFF", enabled: true,
        desired: !!varVal("maintenance_mode") },
      { name: "any_nws_alert", description: "Flag whenever any NWS alert is active",
        topic: "facility/weather/nws_alert", on_match: "1", on_clear: "0", enabled: true,
        desired: m.active_alerts.length > 0 },
    ];
    for (const r of rules) {
      const man = manual[r.name] || "auto";
      r.manual = man;
      r.active = !r.enabled ? null : (man === "on" ? true : man === "off" ? false : r.desired);
      if (prevActive[r.name] !== undefined && prevActive[r.name] !== r.active) lastChange[r.name] = isoNow();
      if (lastChange[r.name] === undefined) lastChange[r.name] = isoNow();
      prevActive[r.name] = r.active;
      r.current_payload = r.active == null ? null : (r.active ? r.on_match : r.on_clear);
      r.last_change = lastChange[r.name];
    }
    return rules;
  }

  function buildState() {
    const s = SCENARIOS[current];
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
             manual_control: MANUAL_CONTROL, metrics: m, rules: deriveRules(m),
             variables: variables.map(v => ({ ...v })) };
  }

  function ctlButtons(r) {
    const cur = r.manual || "auto";
    const mk = (st, lbl) => '<button type="button" class="mini ' + (cur === st ? "" : "secondary") +
      '" data-state="' + st + '" style="margin:0;padding:5px 11px;font-size:12px">' + lbl + '</button>';
    return '<div class="ctl" data-device="' + esc(r.name) +
      '" style="display:flex;gap:5px;margin-top:8px">' + mk("auto", "Auto") + mk("on", "On") + mk("off", "Off") + '</div>';
  }

  function renderVars(vars, manualControl) {
    const card = document.getElementById("vars-card");
    const box = document.getElementById("vars-body");
    if (!card || !box) return;
    if (!vars.length) { card.style.display = "none"; return; }
    card.style.display = "";
    box.innerHTML = "";
    for (const v of vars) {
      let ctrl;
      if (!manualControl) {
        ctrl = '<span class="v">' + esc(fmt(v.value)) + "</span>";
      } else if (v.type === "bool") {
        const on = v.value === true;
        ctrl = '<button type="button" class="mini ' + (on ? "" : "secondary") + '" data-var="' + esc(v.name) +
          '" data-next="' + (on ? "false" : "true") + '" style="margin:0">' + (on ? "ON" : "OFF") + "</button>";
      } else {
        ctrl = '<input class="var-num" data-var="' + esc(v.name) + '" type="number" step="any" value="' +
          (v.value != null ? esc(v.value) : "") + '" style="width:120px;margin:0">';
      }
      const cell = document.createElement("div"); cell.className = "metric";
      cell.style.cssText = "display:flex;justify-content:space-between;align-items:center;gap:10px";
      cell.innerHTML = '<div class="k">' + esc(v.name) + "</div><div>" + ctrl + "</div>";
      box.appendChild(cell);
    }
  }

  function render(s) {
    const conn = document.getElementById("connstate");
    const up = !!s.mqtt_connected;
    conn.innerHTML = '<span class="dot ' + (up ? "up" : "down") + '"></span>MQTT ' + (up ? "connected" : "offline");

    // Headline device: prefer the irrigation rule (back-compat), else first
    // enabled rule with a known state, else the first rule.
    const rules = s.rules;
    const irr = rules.find(r => r.enabled !== false && /irrigation|rain_inhibit/.test(r.name || ""))
             || rules.find(r => r.enabled !== false && r.active !== null && r.active !== undefined)
             || rules[0];
    const dEl = document.getElementById("directive");
    const card = document.getElementById("directive-card");
    let st = "unknown";
    if (irr && irr.active !== null && irr.active !== undefined) {
      const isIrr = /irrigation|rain_inhibit/.test(irr.name || "");
      st = irr.active ? "inhibit" : "allow";
      dEl.className = "big " + st;
      const suffix = isIrr ? (irr.active ? " — do NOT water" : " — watering allowed")
                           : (irr.active ? " — active" : " — clear");
      setText("directive", irr.current_payload + suffix);
      setText("directive-sub", "topic " + irr.topic + (irr.last_change ? " · changed " + agoText(irr.last_change) : ""));
    } else {
      dEl.className = "big unknown"; setText("directive", "UNKNOWN");
      setText("directive-sub", irr ? "Waiting on data…" : "No rules configured.");
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

    renderVars(s.variables || [], !!s.manual_control);

    const grid = document.getElementById("devicegrid");
    grid.innerHTML = "";
    for (const r of rules) {
      let pill;
      if (r.enabled === false) pill = '<span class="pill na">disabled</span>';
      else if (r.active === null || r.active === undefined) pill = '<span class="pill na">n/a</span>';
      else if (r.active) pill = '<span class="pill on">active</span>';
      else pill = '<span class="pill off">clear</span>';
      if (r.manual && r.manual !== "auto") pill += ' <span class="pill na">manual ' + esc(r.manual) + "</span>";
      const cell = document.createElement("div");
      cell.className = "metric"; cell.style.cssText = "display:flex;flex-direction:column;gap:6px";
      let html = '<div class="toprow" style="align-items:center"><strong>' + esc(r.name) + "</strong><span>" + pill + "</span></div>";
      if (r.description) html += '<div class="muted" style="font-size:12px">' + esc(r.description) + "</div>";
      html += '<div class="muted" style="font-size:12px">topic <code>' + esc(r.topic) + "</code></div>";
      html += '<div class="muted" style="font-size:12px">payload ' + (r.current_payload != null ? esc(r.current_payload) : "—") +
        " · changed " + esc(agoText(r.last_change)) + "</div>";
      if (s.manual_control && r.enabled !== false) html += ctlButtons(r);
      cell.innerHTML = html;
      grid.appendChild(cell);
    }
    const dash = document.getElementById("dash");
    if (dash) dash.classList.remove("loading");
  }

  function tick() { render(buildState()); }

  // Demo control wiring: manual device buttons + variable toggles mutate the
  // mock state in place (no network) and re-render.
  document.getElementById("devicegrid").addEventListener("click", e => {
    const b = e.target.closest("button[data-state]"); if (!b) return;
    const wrap = b.closest(".ctl"); if (!wrap) return;
    manual[wrap.getAttribute("data-device")] = b.getAttribute("data-state");
    tick(); toast("Manual: " + wrap.getAttribute("data-device") + " → " + b.getAttribute("data-state"));
  });
  const vb = document.getElementById("vars-body");
  if (vb) {
    vb.addEventListener("click", e => {
      const b = e.target.closest("button[data-var]"); if (!b) return;
      const v = variables.find(x => x.name === b.getAttribute("data-var"));
      if (v) { v.value = b.getAttribute("data-next") === "true"; tick(); toast(v.name + " = " + v.value); }
    });
    vb.addEventListener("change", e => {
      const i = e.target.closest("input.var-num[data-var]"); if (!i) return;
      const v = variables.find(x => x.name === i.getAttribute("data-var"));
      if (v) { v.value = Number(i.value); toast(v.name + " = " + v.value); }
    });
  }

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
    const errBox = el.parentElement.querySelector(":scope > .field-err");
    const raw = (el.value || "").trim();
    const type = el.dataset.type;          // "num" | "int" | undefined
    let err = "";
    if (raw === "") {
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

  // Manual control / MQTT publishing require a login — mirror the server's
  // fail-closed refusal. Returns "" when ok, else the field label that needs one.
  function loginGuard() {
    const u = (form.querySelector("[name=web_username]") || {}).value || "";
    const p = (form.querySelector("[name=web_password]") || {}).value || "";
    const haveLogin = !!(u.trim() && p.trim());
    const amc = form.querySelector("[name=web_allow_manual_control]");
    if (amc && amc.value === "true" && !haveLogin) return "Manual device control";
    const amp = form.querySelector("[name=web_allow_mqtt_publish]");
    if (amp && amp.value === "true" && !haveLogin) return "MQTT publishing";
    return "";
  }

  form.addEventListener("submit", e => {
    e.preventDefault();
    let ok = true;
    form.querySelectorAll("input[data-type],input[data-required]").forEach(el => {
      if (!validateField(el)) ok = false;
    });
    if (!ok) { toast("Could not save: fix the highlighted fields.", true); return; }
    const needsLogin = loginGuard();
    if (needsLogin) { toast(needsLogin + " needs a web login (set a username and password).", true); return; }
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

  const NUM = ["<", "<=", ">", ">=", "==", "!=", "between", "in", "changed"];
  const NUMCMP = ["<", "<=", ">", ">=", "==", "!="];
  const BOOLO = ["==", "!=", "changed"];
  const TXT = ["contains", "equals", "in", "regex", "changed"];
  // Built-ins + schedule metrics + a few "discovered" dynamic metrics (as if a
  // config declared variables / mqtt_in / http_poll), so the dropdowns show the
  // same dynamic discovery the live builder does.
  const METRICS = {
    is_raining:                { type: "bool",   ops: ["==", "!=", "changed"] },
    precip_accum_in:           { type: "number", ops: NUM },
    precipitation_probability: { type: "number", ops: NUM },
    temperature:               { type: "number", ops: NUM },
    wind_speed_mph:            { type: "number", ops: NUM },
    humidity:                  { type: "number", ops: NUM },
    short_forecast:            { type: "text",   ops: TXT },
    active_alert:              { type: "alert",  ops: ["any", "contains", "equals", "regex"] },
    time_hour:                 { type: "number", ops: NUM },
    time_minute:               { type: "number", ops: NUM },
    time_weekday:              { type: "text",   ops: TXT },
    time_is_weekend:           { type: "bool",   ops: BOOLO },
    time_is_daytime:           { type: "bool",   ops: BOOLO },
    var_maintenance_mode:      { type: "bool",   ops: BOOLO },
    var_temp_setpoint:         { type: "number", ops: NUM },
    tank_level:                { type: "number", ops: NUM },
    power_kw:                  { type: "number", ops: NUM },
    solar_kw:                  { type: "number", ops: NUM },
    net_power:                 { type: "number", ops: NUM },   // computed: power_kw - solar_kw
  };
  const METRIC_NAMES = Object.keys(METRICS);
  const builder = document.getElementById("builder");

  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function opt(v, label, sel) { const o = document.createElement("option"); o.value = v; o.textContent = label || v; if (sel) o.selected = true; return o; }

  function valueControl(metric, operator, value) {
    const meta = METRICS[metric] || { type: "text" };
    let c;
    if (operator === "between") {
      c = document.createElement("input"); c.className = "c-val"; c.type = "text";
      c.value = value != null ? value : ""; c.placeholder = "low, high";
    } else if (operator === "in") {
      c = document.createElement("input"); c.className = "c-val"; c.type = "text";
      c.value = value != null ? value : ""; c.placeholder = meta.type === "number" ? "e.g. 30, 50, 70" : "a, b, c";
    } else if (meta.type === "bool") {
      c = document.createElement("select"); c.className = "c-val";
      c.appendChild(opt("true", "true", String(value) === "true"));
      c.appendChild(opt("false", "false", String(value) !== "true"));
    } else if (meta.type === "number") {
      c = document.createElement("input"); c.className = "c-val"; c.type = "number"; c.step = "any";
      c.value = value != null ? value : ""; c.placeholder = "number";
    } else {
      c = document.createElement("input"); c.className = "c-val"; c.type = "text";
      c.value = value != null ? value : ""; c.placeholder = "text";
    }
    c.setAttribute("aria-label", "condition value");
    return c;
  }
  function fillOps(sel, metric, chosen) {
    sel.innerHTML = "";
    const ops = (METRICS[metric] || { ops: [] }).ops;
    ops.forEach(o => sel.appendChild(opt(o, o, o === chosen)));
    if (!ops.includes(chosen) && ops.length) sel.value = ops[0];
  }
  function cmpMetricNames() { return METRIC_NAMES.filter(n => { const t = (METRICS[n] || {}).type; return t === "number" || t === "bool"; }); }
  function metricPicker(selected) {
    const s = document.createElement("select"); s.className = "c-vmetric"; s.setAttribute("aria-label", "comparison metric");
    cmpMetricNames().forEach(n => s.appendChild(opt(n, n, n === selected)));
    return s;
  }
  function condRow(cond) {
    cond = cond || { metric: METRIC_NAMES[0], operator: "", value: "", value_metric: "", for: "" };
    const row = el("div", "cond row");
    const metricWrap = el("div"); const m = document.createElement("select"); m.className = "c-metric";
    m.setAttribute("aria-label", "metric");
    METRIC_NAMES.forEach(n => m.appendChild(opt(n, n, n === cond.metric)));
    if (!METRICS[cond.metric]) m.value = METRIC_NAMES[0];
    metricWrap.appendChild(m);
    const opWrap = el("div"); const o = document.createElement("select"); o.className = "c-op";
    o.setAttribute("aria-label", "operator");
    fillOps(o, m.value, cond.operator); opWrap.appendChild(o);
    const modeWrap = el("div", "c-vmode-wrap"); const mode = document.createElement("select"); mode.className = "c-vmode";
    mode.setAttribute("aria-label", "compare to");
    mode.appendChild(opt("value", "a value", !cond.value_metric));
    mode.appendChild(opt("metric", "a metric", !!cond.value_metric));
    modeWrap.appendChild(mode);
    const valWrap = el("div", "c-val-wrap");
    const forWrap = el("div", "c-for-wrap"); const f = document.createElement("input"); f.className = "c-for"; f.type = "text";
    f.placeholder = "for (e.g. 10m)"; f.value = cond.for || ""; f.setAttribute("aria-label", "sustain duration");
    f.title = "optional: the condition must hold continuously this long (e.g. 30s, 10m, 2h)";
    forWrap.appendChild(f);
    const rmWrap = el("div", "rm"); const rm = el("button", "secondary danger mini", "×"); rm.type = "button"; rmWrap.appendChild(rm);
    function noValue() { const meta = METRICS[m.value] || {}; return o.value === "changed" || (meta.type === "alert" && o.value === "any"); }
    function cmpEligible() { const meta = METRICS[m.value] || {}; return (meta.type === "number" || meta.type === "bool") && NUMCMP.includes(o.value); }
    function syncMode() { modeWrap.style.display = (cmpEligible() && !noValue()) ? "" : "none"; if (!cmpEligible()) mode.value = "value"; }
    function buildVal(keep) {
      valWrap.innerHTML = "";
      if (mode.value === "metric" && cmpEligible()) valWrap.appendChild(metricPicker(cond.value_metric));
      else valWrap.appendChild(valueControl(m.value, o.value, keep));
      valWrap.style.display = noValue() ? "none" : "";
    }
    m.addEventListener("change", () => { fillOps(o, m.value, o.value); syncMode(); buildVal(null); });
    o.addEventListener("change", () => { syncMode(); buildVal(null); });
    mode.addEventListener("change", () => buildVal(null));
    rm.addEventListener("click", () => { const card = row.closest(".rule-card"); row.remove(); refreshCombine(card); });
    syncMode(); buildVal(cond.value);
    row.appendChild(metricWrap); row.appendChild(opWrap); row.appendChild(modeWrap); row.appendChild(valWrap); row.appendChild(forWrap); row.appendChild(rmWrap);
    return row;
  }
  function refreshCombine(card) {
    if (!card) return;
    card.querySelector(".combine-wrap").style.display = card.querySelectorAll(".cond").length > 1 ? "" : "none";
  }
  function actionFields(kind, a) {
    a = a || {};
    const wrap = el("div", "a-fields"); wrap.style.cssText = "display:flex;gap:8px;flex-wrap:wrap;flex:1;min-width:240px";
    if (kind === "webhook") {
      wrap.innerHTML = '<input class="a-url" placeholder="https://host/hook" style="flex:2;min-width:160px">' +
        '<select class="a-method" style="flex:0 0 84px"></select>' +
        '<input class="a-body" placeholder="body (supports {{metric}})" style="flex:2;min-width:160px">';
      ["POST", "GET", "PUT"].forEach(x => wrap.querySelector(".a-method").appendChild(opt(x, x, x === (a.method || "POST"))));
      wrap.querySelector(".a-url").value = a.url || ""; wrap.querySelector(".a-body").value = a.body || "";
    } else if (kind === "notify") {
      wrap.innerHTML = '<input class="a-text" placeholder="Slack message (supports {{metric}})" style="flex:1;min-width:200px">';
      wrap.querySelector(".a-text").value = a.text || "";
    } else {
      wrap.innerHTML = '<input class="a-topic" placeholder="topic e.g. facility/relay1" style="flex:1;min-width:140px">' +
        '<input class="a-payload" placeholder="payload (supports {{metric}})" style="flex:1;min-width:140px">' +
        '<select class="a-qos" title="QoS" style="flex:0 0 70px"></select>' +
        '<label class="muted" style="margin:0;display:flex;align-items:center;gap:5px;font-weight:500;white-space:nowrap">' +
        '<input type="checkbox" class="a-retain" style="width:auto;margin:0"> retain</label>';
      wrap.querySelector(".a-topic").value = a.topic || ""; wrap.querySelector(".a-payload").value = a.payload || "";
      ["", "0", "1", "2"].forEach(x => wrap.querySelector(".a-qos").appendChild(opt(x, x === "" ? "qos —" : "qos " + x, String(a.qos == null ? "" : a.qos) === x)));
      wrap.querySelector(".a-retain").checked = a.retain === true;
    }
    return wrap;
  }
  function actionRow(a) {
    a = a || { kind: "mqtt", on: "match" };
    const row = el("div", "action-row row"); row.style.alignItems = "center";
    const onW = el("div"); onW.style.flex = "0 0 96px"; const on = document.createElement("select"); on.className = "a-on";
    [["match", "on match"], ["clear", "on clear"], ["both", "on both"]].forEach(x => on.appendChild(opt(x[0], x[1], x[0] === (a.on || "both")))); onW.appendChild(on);
    const kW = el("div"); kW.style.flex = "0 0 108px"; const k = document.createElement("select"); k.className = "a-kind";
    [["mqtt", "MQTT"], ["webhook", "Webhook"], ["notify", "Notify"]].forEach(x => k.appendChild(opt(x[0], x[1], x[0] === (a.kind || "mqtt")))); kW.appendChild(k);
    let fields = actionFields(k.value, a);
    const rmW = el("div"); rmW.style.flex = "0 0 auto"; const rm = el("button", "secondary danger mini", "×"); rm.type = "button"; rmW.appendChild(rm);
    k.addEventListener("change", () => { const nf = actionFields(k.value, {}); row.replaceChild(nf, fields); fields = nf; });
    rm.addEventListener("click", () => row.remove());
    row.appendChild(onW); row.appendChild(kW); row.appendChild(fields); row.appendChild(rmW);
    return row;
  }
  function ruleCard(rule) {
    rule = rule || { name: "", description: "", topic: "", on_match: "", on_clear: "", enabled: true, combine: "any", conditions: [], actions: [] };
    const card = el("div", "rule-card");
    card.innerHTML =
      '<div class="rhead"><span class="idx"></span>' +
      '<label class="enabled-lbl" style="display:flex;align-items:center;gap:7px;margin:0;font-weight:600" ' +
      'title="Disabled rules are not evaluated and publish nothing">' +
      '<input type="checkbox" class="f-enabled" style="width:auto;margin:0"> enabled</label></div>' +
      '<div class="row"><div><label>Name <input class="f-name"></label></div>' +
      '<div><label>Topic <input class="f-topic"></label></div></div>' +
      '<label>Description <span class="hint">(optional)</span> <input class="f-desc"></label>' +
      '<div class="row"><div><label>Payload when matched <span class="hint">(on_match)</span> <input class="f-onmatch"></label></div>' +
      '<div><label>Payload when cleared <span class="hint">(on_clear, optional)</span> <input class="f-onclear"></label></div></div>' +
      '<div class="combine-wrap"><label>When there are multiple conditions, match' +
      ' <select class="f-combine"></select></label></div>' +
      '<label style="margin-top:14px">Conditions</label><div class="conds"></div>' +
      '<div class="btnrow"><button type="button" class="secondary mini add-cond">+ Add condition</button></div>' +
      '<details class="actions-wrap" style="margin-top:6px"><summary class="muted" style="cursor:pointer">' +
      'Extra actions <span class="hint">(optional — extra publishes, webhooks, Slack on a transition)</span></summary>' +
      '<div class="actions" style="margin-top:8px;display:flex;flex-direction:column;gap:8px"></div>' +
      '<div class="btnrow"><button type="button" class="secondary mini add-action">+ Add action</button></div></details>' +
      '<div class="btnrow"><button type="button" class="danger mini remove-rule">Remove rule</button></div>';
    card.querySelector(".f-name").value = rule.name || "";
    card.querySelector(".f-topic").value = rule.topic || "";
    card.querySelector(".f-desc").value = rule.description || "";
    card.querySelector(".f-onmatch").value = rule.on_match || "";
    card.querySelector(".f-onclear").value = rule.on_clear || "";
    card.querySelector(".f-enabled").checked = rule.enabled !== false;
    const comb = card.querySelector(".f-combine");
    comb.appendChild(opt("any", "ANY is true (OR)", rule.combine !== "all"));
    comb.appendChild(opt("all", "ALL are true (AND)", rule.combine === "all"));
    const conds = card.querySelector(".conds");
    (rule.conditions && rule.conditions.length ? rule.conditions : [null]).forEach(c => conds.appendChild(condRow(c)));
    card.querySelector(".add-cond").addEventListener("click", () => { conds.appendChild(condRow()); refreshCombine(card); });
    const actionsBox = card.querySelector(".actions");
    (rule.actions || []).forEach(a => actionsBox.appendChild(actionRow(a)));
    if ((rule.actions || []).length) card.querySelector(".actions-wrap").open = true;
    card.querySelector(".add-action").addEventListener("click", () => actionsBox.appendChild(actionRow()));
    card.querySelector(".remove-rule").addEventListener("click", () => { card.remove(); reindex(); });
    refreshCombine(card);
    return card;
  }
  function reindex() {
    [...builder.querySelectorAll(".rule-card")].forEach((c, i) => {
      c.querySelector(".idx").textContent = "Rule " + (i + 1) + (i === 0 ? " · headline" : "");
    });
  }
  function collect() {
    return [...builder.querySelectorAll(".rule-card")].map(card => {
      const conds = [...card.querySelectorAll(".cond")].map(row => {
        const metric = row.querySelector(".c-metric").value;
        const operator = row.querySelector(".c-op").value;
        const meta = METRICS[metric] || {};
        const noVal = operator === "changed" || (meta.type === "alert" && operator === "any");
        const forv = (row.querySelector(".c-for").value || "").trim();
        const modeSel = row.querySelector(".c-vmode");
        const vm = row.querySelector(".c-vmetric");
        if (modeSel && modeSel.value === "metric" && vm) {
          return { metric, operator, value_metric: vm.value, for: forv };
        }
        let value = "";
        if (!noVal) { const ctrl = row.querySelector(".c-val"); value = ctrl ? ctrl.value : ""; }
        return { metric, operator, value, for: forv };
      });
      const actions = [...card.querySelectorAll(".action-row")].map(row => {
        const kind = row.querySelector(".a-kind").value;
        const on = row.querySelector(".a-on").value;
        if (kind === "webhook") return { kind, on, url: (row.querySelector(".a-url").value || "").trim(), method: row.querySelector(".a-method").value, body: row.querySelector(".a-body").value };
        if (kind === "notify") return { kind, on, text: (row.querySelector(".a-text").value || "").trim() };
        const qsel = row.querySelector(".a-qos");
        return { kind, on, topic: (row.querySelector(".a-topic").value || "").trim(), payload: row.querySelector(".a-payload").value,
          qos: (qsel && qsel.value !== "") ? Number(qsel.value) : null, retain: row.querySelector(".a-retain").checked };
      });
      return {
        name: card.querySelector(".f-name").value.trim(),
        description: card.querySelector(".f-desc").value.trim(),
        topic: card.querySelector(".f-topic").value.trim(),
        on_match: card.querySelector(".f-onmatch").value,
        on_clear: card.querySelector(".f-onclear").value,
        enabled: card.querySelector(".f-enabled").checked,
        combine: card.querySelector(".f-combine").value,
        conditions: conds,
        actions: actions,
      };
    });
  }
  function validateForm(data) {
    if (!data.length) return "Add at least one rule.";
    const durRe = /^\d+(\.\d+)?\s*[smh]?$/;
    for (let i = 0; i < data.length; i++) {
      const r = data[i], label = "Rule " + (i + 1);
      if (!r.name) return label + ": name is required.";
      if (!r.topic) return "Rule '" + r.name + "': topic is required.";
      if (r.on_match === "") return "Rule '" + r.name + "': the on_match payload is required.";
      if (!r.conditions.length) return "Rule '" + r.name + "': add at least one condition.";
      for (const c of r.conditions) {
        const meta = METRICS[c.metric] || {};
        if (c.for && !durRe.test(c.for.trim())) return "Rule '" + r.name + "': '" + c.metric + "' for must be a duration like 10m, 30s, 2h.";
        if (c.value_metric) {
          if (!NUMCMP.includes(c.operator)) return "Rule '" + r.name + "': comparing to a metric needs < <= > >= == != (not " + c.operator + ").";
          if (!METRICS[c.value_metric]) return "Rule '" + r.name + "': unknown comparison metric '" + c.value_metric + "'.";
          continue;
        }
        if (c.operator === "changed") continue;
        if (meta.type === "alert" && c.operator === "any") continue;
        if (c.operator === "between") {
          const ps = c.value.split(",").map(s => s.trim()).filter(s => s !== "");
          if (ps.length !== 2 || ps.some(p => isNaN(Number(p)))) return "Rule '" + r.name + "': " + c.metric + " between needs two numbers 'low, high'.";
          continue;
        }
        if (c.operator === "in") {
          const ps = c.value.split(",").map(s => s.trim()).filter(s => s !== "");
          if (!ps.length) return "Rule '" + r.name + "': " + c.metric + " in needs at least one value.";
          if (meta.type === "number" && ps.some(p => isNaN(Number(p)))) return "Rule '" + r.name + "': " + c.metric + " in needs numeric values.";
          continue;
        }
        if (c.value === "") return "Rule '" + r.name + "': the " + c.metric + " condition needs a value.";
        if (meta.type === "number" && isNaN(Number(c.value))) return "Rule '" + r.name + "': " + c.metric + " needs a numeric value.";
      }
      for (const a of (r.actions || [])) {
        if (a.kind === "mqtt" && !a.topic) return "Rule '" + r.name + "': an MQTT action needs a topic.";
        if (a.kind === "webhook" && !a.url) return "Rule '" + r.name + "': a webhook action needs a URL.";
        if (a.kind === "notify" && !a.text) return "Rule '" + r.name + "': a notify action needs a message.";
      }
    }
    return "";
  }

  document.getElementById("add-rule").addEventListener("click", () => { builder.appendChild(ruleCard()); reindex(); });
  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById("tab-form").style.display = t.dataset.tab === "form" ? "" : "none";
    document.getElementById("tab-yaml").style.display = t.dataset.tab === "yaml" ? "" : "none";
  }));

  /* ---- YAML tab (heuristic shape check) --------------------------------- */
  const ta = document.getElementById("rules_yaml");
  const errBox = document.getElementById("rules-err");
  function validateYaml(text) {
    if (!text.trim()) return "Rules list is empty.";
    const lines = text.replace(/\t/g, "  ").split("\n");
    const items = []; let cur = null, startLine = 0;
    lines.forEach((ln, i) => {
      if (/^- /.test(ln)) { if (cur !== null) items.push({ text: cur, line: startLine }); cur = ln + "\n"; startLine = i + 1; }
      else if (cur !== null) { cur += ln + "\n"; }
    });
    if (cur !== null) items.push({ text: cur, line: startLine });
    if (!items.length) return "Expected a YAML list (each rule starts with '- ').";
    for (let k = 0; k < items.length; k++) {
      const blk = items[k].text.replace(/^- /, "  ");
      const label = "rule #" + (k + 1) + " (line " + items[k].line + ")";
      for (const key of ["name", "when", "topic", "on_match"])
        if (!new RegExp("(^|\\n)\\s*" + key + ":").test(blk)) return label + ": missing '" + key + "'.";
      const mm = blk.match(/metric:\s*([A-Za-z_0-9]+)/g) || [];
      if (!mm.length) return label + ": needs at least one 'metric:' under 'when'.";
      for (const x of mm) { const n = x.split(":")[1].trim(); if (!METRIC_NAMES.includes(n)) return label + ": unknown metric '" + n + "'."; }
      const pl = blk.match(/(on_match|on_clear):\s*([^\n#]*)/g) || [];
      for (const p of pl) {
        const v = p.split(":").slice(1).join(":").trim();
        if (/^(on|off|yes|no|true|false)$/i.test(v)) return label + ": quote the payload \"" + v + "\" (unquoted it becomes a boolean).";
      }
    }
    return "";
  }
  function checkYaml(showOk) {
    const err = validateYaml(ta.value); errBox.textContent = err; errBox.style.color = err ? "#fda4a4" : "#86efac";
    if (!err && showOk) errBox.textContent = "Looks valid ✓"; return !err;
  }
  const EXAMPLE = "\n- name: vent_fan\n  enabled: true\n  when:\n    all:\n      - { metric: temperature, operator: \">\", value: 85, for: \"10m\" }\n      - { metric: time_is_daytime, operator: \"==\", value: true }\n  topic: \"facility/vent_fan\"\n  on_match: \"ON\"\n  on_clear: \"OFF\"\n";
  document.getElementById("add-example").addEventListener("click", () => { ta.value = ta.value.replace(/\s*$/, "") + "\n" + EXAMPLE; ta.focus(); checkYaml(true); });
  document.getElementById("check").addEventListener("click", () => checkYaml(true));
  ta.addEventListener("input", () => { errBox.textContent = ""; });

  /* ---- submit (mode depends on which Save was clicked) ------------------ */
  let mode = "form";
  document.getElementById("save-form").addEventListener("click", () => mode = "form");
  document.getElementById("save-yaml").addEventListener("click", () => mode = "yaml");
  form.addEventListener("submit", e => {
    e.preventDefault();
    if (mode === "form") {
      const data = collect(); const err = validateForm(data);
      const box = document.getElementById("form-err");
      box.textContent = err; box.style.color = "#fda4a4";
      if (err) { toast("Could not save: " + err, true); return; }
      toast("Rules saved (demo — nothing was written).");
    } else {
      if (checkYaml(false)) toast("Rules saved (demo — nothing was written).");
      else toast("Could not save: " + errBox.textContent, true);
    }
  });

  /* ---- initial render --------------------------------------------------- */
  const INITIAL = window.DEMO_RULES || [];
  (INITIAL.length ? INITIAL : [null]).forEach(r => builder.appendChild(ruleCard(r)));
  reindex();
  document.querySelector('.tab[data-tab="form"]').click();
})();

/* =========================================================================
   ACTIVITY  ·  read-only audit-log viewer (sample data in the demo)
   ========================================================================= */
(function activity() {
  const tb = document.getElementById("actbody");
  if (!tb) return;
  const ago = m => new Date(Date.now() - m * 60000).toISOString().replace(/\.\d+Z$/, "Z");
  // Mock events mirroring the two shapes the live app records (monitor + UI).
  const events = [
    { ts: ago(1),   device: "maintenance_hold", action: "manual_set", state: "on", by: "admin" },
    { ts: ago(2),   device: "vent_fan", action: "action_fired", kind: "notify", target: "slack", trigger: "match", ok: true, by: "monitor" },
    { ts: ago(2),   device: "vent_fan", action: "action_fired", kind: "webhook", target: "https://hooks.example.com/vent", trigger: "match", ok: true, by: "monitor" },
    { ts: ago(3),   device: "irrigation_rain_inhibit", source: "auto", state: "on", by: "monitor" },
    { ts: ago(9),   topic: "facility/cmd/relay1", action: "mqtt_publish", qos: 1, retain: false, by: "admin" },
    { ts: ago(18),  variable: "maintenance_mode", action: "variable_set", value: true, by: "admin" },
    { ts: ago(46),  device: "vent_fan", source: "auto", state: "off", by: "monitor" },
    { ts: ago(47),  device: "vent_fan", action: "action_fired", kind: "webhook", target: "https://hooks.example.com/vent", trigger: "clear", ok: false, by: "monitor" },
    { ts: ago(95),  device: "irrigation_rain_inhibit", source: "auto", state: "off", by: "monitor" },
    { ts: ago(140), device: "vent_fan", source: "manual", state: "on", by: "admin" },
  ];
  function describe(e) {
    if (e.action === "manual_set")   return { what: e.device, action: "manual override", detail: String(e.state).toUpperCase() };
    if (e.action === "variable_set") return { what: e.variable, action: "variable set", detail: String(e.value) };
    if (e.action === "mqtt_publish") return { what: e.topic, action: "manual publish", detail: "qos " + e.qos + (e.retain ? " · retain" : "") };
    if (e.action === "action_fired") {
      const tgt = e.kind === "notify" ? "Slack" : (e.target || "");
      return { what: e.device, action: e.kind + " action" + (e.ok === false ? " (failed)" : ""),
               detail: "on " + (e.trigger || "") + (tgt ? " → " + tgt : "") };
    }
    const src = e.source === "manual" ? "manual" : "automatic";
    return { what: e.device, action: src + " state change", detail: String(e.state).toUpperCase() };
  }
  function pillFor(d) {
    if (d.action === "manual override" || d.action === "manual publish" || /^manual/.test(d.action)) return '<span class="pill on">' + esc(d.action) + "</span>";
    if (d.action === "variable set") return '<span class="pill na">' + esc(d.action) + "</span>";
    if (/\(failed\)/.test(d.action)) return '<span class="pill na">' + esc(d.action) + "</span>";
    if (/ action$/.test(d.action)) return '<span class="pill on">' + esc(d.action) + "</span>";
    return '<span class="pill off">' + esc(d.action) + "</span>";
  }
  document.getElementById("act-count").textContent = events.length + " recent";
  tb.innerHTML = "";
  for (const e of events) {
    const d = describe(e); const tr = document.createElement("tr");
    tr.innerHTML = '<td class="muted" title="' + esc(e.ts || "") + '">' + esc(agoText(e.ts)) + "</td>" +
      "<td>" + esc(d.what || "—") + "</td><td>" + pillFor(d) + "</td>" +
      "<td>" + esc(d.detail || "—") + "</td><td class=\"muted\">" + esc(e.by || "—") + "</td>";
    tb.appendChild(tr);
  }
})();

/* =========================================================================
   SYSTEM  ·  health + config summary + runtime log (sample data in the demo)
   Mirrors the live webui.py System page; mock data, no backend.
   ========================================================================= */
(function system() {
  const conn = document.getElementById("sys-conn");
  if (!conn || !document.getElementById("logbox")) return;

  // Mock health + summary (what /api/system returns in the live app).
  const SYS = {
    monitor: "ok", mqtt_connected: true, config_ok: true,
    last_update: isoNow(),
    summary: { rules_total: 3, rules_enabled: 3, metrics: 18, variables: 2, mqtt_inputs: 1, http_inputs: 1 },
    files: { config: "config.yaml", state: "weather_state.json", audit: "audit.log", log: "monitor.log" },
  };
  // Sample runtime log lines (newest first), as the live /api/logs returns.
  const mins = m => new Date(Date.now() - m * 60000).toISOString().replace("T", " ").replace(/\.\d+Z$/, "");
  const LINES = [
    { ts: mins(0),  level: "INFO",    msg: "facility/vent_fan -> ON (temperature sustained > 85 for 10m)" },
    { ts: mins(2),  level: "INFO",    msg: "irrigation/rain_inhibit unchanged (INHIBIT)" },
    { ts: mins(6),  level: "WARNING", msg: "http_poll meter.local timed out; holding last value (fail-safe)" },
    { ts: mins(9),  level: "INFO",    msg: "Poll cycle complete: 3 rules evaluated, 1 published" },
    { ts: mins(14), level: "ERROR",   msg: "Rule 'broken_demo' failed this cycle, skipping: unknown metric 'foo'" },
    { ts: mins(15), level: "INFO",    msg: "MQTT connected to localhost:1883" },
    { ts: mins(15), level: "INFO",    msg: "Runtime log mirrored to monitor.log" },
  ];
  const LEVEL_RANK = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };

  function renderHealth() {
    SYS.last_update = isoNow();          // demo: monitor stays fresh between ticks
    const mon = SYS.monitor;
    const dot = mon === "ok" ? "up" : (mon === "stale" || mon === "no_data" ? "down" : "idle");
    conn.innerHTML = '<span class="dot ' + dot + '"></span>' + (mon === "ok" ? "healthy" : mon === "stale" ? "monitor stale" : mon === "no_data" ? "no data yet" : "unknown");
    setText("h_monitor", { ok: "running", stale: "stale", no_data: "no data", unknown: "unknown" }[mon] || mon);
    document.getElementById("h_monitor").className = "v " + (mon === "ok" ? "allow" : mon === "unknown" ? "unknown" : "inhibit");
    const mq = SYS.mqtt_connected;
    setText("h_mqtt", mq == null ? "—" : (mq ? "connected" : "offline"));
    document.getElementById("h_mqtt").className = "v " + (mq ? "allow" : mq === false ? "inhibit" : "unknown");
    setText("h_config", SYS.config_ok ? "valid" : "invalid");
    document.getElementById("h_config").className = "v " + (SYS.config_ok ? "allow" : "inhibit");
    const u = document.getElementById("h_update");
    setText("h_update", agoText(SYS.last_update)); u.title = SYS.last_update || "";

    const s = SYS.summary;
    setText("s_rules", s.rules_enabled + " / " + s.rules_total);
    setText("s_metrics", s.metrics);
    setText("s_vars", s.variables);
    setText("s_mqtt", s.mqtt_inputs);
    setText("s_http", s.http_inputs);
    const f = SYS.files;
    setText("files", "config " + f.config + " · state " + f.state + " · audit " + f.audit + " · log " + f.log);
  }

  function renderLog() {
    const min = LEVEL_RANK[document.getElementById("lvl").value];
    const box = document.getElementById("logbox");
    const rows = (min == null) ? LINES : LINES.filter(l => LEVEL_RANK[l.level] == null || LEVEL_RANK[l.level] >= min);
    if (!rows.length) { box.textContent = "No lines at this level."; return; }
    box.innerHTML = rows.map(l => {
      const lv = l.level || "";
      const color = lv === "ERROR" || lv === "CRITICAL" ? "var(--bad)" : lv === "WARNING" ? "var(--warn)" : lv === "DEBUG" ? "var(--muted2)" : "var(--good)";
      const tag = lv ? '<span style="color:' + color + ';font-weight:700">' + esc(lv.padEnd(7)) + "</span> " : "";
      const ts = l.ts ? '<span style="color:var(--muted2)">' + esc(l.ts) + "</span> " : "";
      return ts + tag + esc(l.msg || "");
    }).join("\n");
  }
  document.getElementById("lvl").addEventListener("change", renderLog);

  // "last poll" drifts so agoText stays lively, like the auto-refresh in the live page.
  renderHealth(); renderLog();
  setInterval(renderHealth, 4000);
})();

/* =========================================================================
   INPUTS  ·  sources editor: operator variables, MQTT + HTTP inputs, computed
   Mirrors the live webui.py Inputs page; client-side only (no persistence).
   ========================================================================= */
(function inputs() {
  const form = document.getElementById("inputs-form");
  if (!form) return;

  const SRC = window.DEMO_SOURCES || {};
  const PARSE = ["number", "bool", "string"];
  function el(t, c, h) { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; }
  function opt(v, l, s) { const o = document.createElement("option"); o.value = v; o.textContent = l || v; if (s) o.selected = true; return o; }
  function rmBtn() { const b = el("button", "secondary danger mini", "×"); b.type = "button"; b.style.margin = "0"; return b; }

  // ---- variables ----
  const vars = document.getElementById("vars");
  function varDefault(type, val) {
    let c;
    if (type === "number") { c = document.createElement("input"); c.type = "number"; c.step = "any"; c.className = "v-def"; c.value = val != null ? val : ""; c.placeholder = "default"; }
    else { c = document.createElement("select"); c.className = "v-def"; c.appendChild(opt("true", "true", String(val) === "true")); c.appendChild(opt("false", "false", String(val) !== "true")); }
    return c;
  }
  function varRow(v) {
    v = v || { name: "", type: "bool", default: "false" };
    const row = el("div", "row"); row.style.alignItems = "flex-end";
    const nw = el("div"); nw.innerHTML = '<label style="margin-top:0">Name</label>'; const n = el("input", "v-name"); n.value = v.name || ""; n.placeholder = "maintenance_mode"; nw.appendChild(n);
    const tw = el("div"); tw.innerHTML = '<label style="margin-top:0">Type</label>'; const t = document.createElement("select"); t.className = "v-type"; ["bool", "number"].forEach(x => t.appendChild(opt(x, x, x === v.type))); tw.appendChild(t);
    const dw = el("div"); dw.innerHTML = '<label style="margin-top:0">Default</label>'; const dwrap = el("div", "v-defwrap"); dwrap.appendChild(varDefault(v.type, v.default)); dw.appendChild(dwrap);
    const rw = el("div"); rw.style.flex = "0 0 auto"; const rm = rmBtn(); rw.appendChild(rm);
    t.addEventListener("change", () => { dwrap.innerHTML = ""; dwrap.appendChild(varDefault(t.value, null)); });
    rm.addEventListener("click", () => row.remove());
    row.appendChild(nw); row.appendChild(tw); row.appendChild(dw); row.appendChild(rw);
    return row;
  }
  document.getElementById("add-var").addEventListener("click", () => vars.appendChild(varRow()));

  // ---- mqtt ----
  const mqtts = document.getElementById("mqtts");
  function mqttRow(m) {
    m = m || { topic: "", metric: "", parse: "number" };
    const row = el("div", "row"); row.style.alignItems = "flex-end";
    const tw = el("div"); tw.innerHTML = '<label style="margin-top:0">Topic</label>'; const t = el("input", "m-topic"); t.value = m.topic || ""; t.placeholder = "sensors/tank/level"; tw.appendChild(t);
    const me = el("div"); me.innerHTML = '<label style="margin-top:0">Metric name</label>'; const mm = el("input", "m-metric"); mm.value = m.metric || ""; mm.placeholder = "tank_level"; me.appendChild(mm);
    const pw = el("div"); pw.innerHTML = '<label style="margin-top:0">Parse</label>'; const p = document.createElement("select"); p.className = "m-parse"; PARSE.forEach(x => p.appendChild(opt(x, x, x === m.parse))); pw.appendChild(p);
    const rw = el("div"); rw.style.flex = "0 0 auto"; const rm = rmBtn(); rw.appendChild(rm); rm.addEventListener("click", () => row.remove());
    row.appendChild(tw); row.appendChild(me); row.appendChild(pw); row.appendChild(rw);
    return row;
  }
  document.getElementById("add-mqtt").addEventListener("click", () => mqtts.appendChild(mqttRow()));

  // ---- http ----
  const https = document.getElementById("https");
  function httpMapRow(mp) {
    mp = mp || { metric: "", path: "", type: "number" };
    const row = el("div", "row"); row.style.alignItems = "flex-end";
    const me = el("div"); me.innerHTML = '<label style="margin-top:0">Metric</label>'; const m = el("input", "h-metric"); m.value = mp.metric || ""; m.placeholder = "power_kw"; me.appendChild(m);
    const pe = el("div"); pe.innerHTML = '<label style="margin-top:0">JSON path</label>'; const p = el("input", "h-path"); p.value = mp.path || ""; p.placeholder = "data.current_kw"; pe.appendChild(p);
    const tw = el("div"); tw.innerHTML = '<label style="margin-top:0">Type</label>'; const t = document.createElement("select"); t.className = "h-type"; PARSE.forEach(x => t.appendChild(opt(x, x, x === mp.type))); tw.appendChild(t);
    const rw = el("div"); rw.style.flex = "0 0 auto"; const rm = rmBtn(); rw.appendChild(rm); rm.addEventListener("click", () => row.remove());
    row.appendChild(me); row.appendChild(pe); row.appendChild(tw); row.appendChild(rw);
    return row;
  }
  function httpCard(h) {
    h = h || { url: "", interval_minutes: 5, timeout: 10, map: [] };
    const card = el("div", "rule-card");
    card.innerHTML = '<div class="row"><div><label style="margin-top:0">URL</label><input class="h-url"></div>' +
      '<div style="flex:0 0 130px"><label style="margin-top:0">Every (min)</label><input class="h-iv" type="number" min="1"></div>' +
      '<div style="flex:0 0 120px"><label style="margin-top:0">Timeout (s)</label><input class="h-to" type="number" min="1"></div></div>' +
      '<label style="margin-top:10px">Field mappings</label><div class="h-map"></div>' +
      '<div class="btnrow"><button type="button" class="secondary mini add-map">+ Add mapping</button>' +
      '<button type="button" class="danger mini rm-http">Remove input</button></div>';
    card.querySelector(".h-url").value = h.url || ""; card.querySelector(".h-url").placeholder = "https://meter.local/api";
    card.querySelector(".h-iv").value = h.interval_minutes != null ? h.interval_minutes : 5;
    card.querySelector(".h-to").value = h.timeout != null ? h.timeout : 10;
    const mapWrap = card.querySelector(".h-map");
    (h.map && h.map.length ? h.map : [null]).forEach(mp => mapWrap.appendChild(httpMapRow(mp)));
    card.querySelector(".add-map").addEventListener("click", () => mapWrap.appendChild(httpMapRow()));
    card.querySelector(".rm-http").addEventListener("click", () => card.remove());
    return card;
  }
  document.getElementById("add-http").addEventListener("click", () => https.appendChild(httpCard()));

  // ---- computed ----
  const comps = document.getElementById("comps");
  function compRow(c) {
    c = c || { name: "", expr: "" };
    const row = el("div", "row"); row.style.alignItems = "flex-end";
    const nw = el("div"); nw.innerHTML = '<label style="margin-top:0">Metric name</label>'; const n = el("input", "co-name"); n.value = c.name || ""; n.placeholder = "net_power"; nw.appendChild(n);
    const ew = el("div"); ew.style.flex = "2"; ew.innerHTML = '<label style="margin-top:0">Formula</label>'; const ex = el("input", "co-expr"); ex.value = c.expr || ""; ex.placeholder = "power_kw - solar_kw"; ew.appendChild(ex);
    const rw = el("div"); rw.style.flex = "0 0 auto"; const rm = rmBtn(); rw.appendChild(rm); rm.addEventListener("click", () => row.remove());
    row.appendChild(nw); row.appendChild(ew); row.appendChild(rw);
    return row;
  }
  document.getElementById("add-comp").addEventListener("click", () => comps.appendChild(compRow()));

  function collect() {
    const variables = [...vars.querySelectorAll(".row")].map(r => ({
      name: r.querySelector(".v-name").value.trim(), type: r.querySelector(".v-type").value,
      default: (r.querySelector(".v-def") || {}).value || "",
    })).filter(v => v.name);
    const mqtt_inputs = [...mqtts.querySelectorAll(".row")].map(r => ({
      topic: r.querySelector(".m-topic").value.trim(), metric: r.querySelector(".m-metric").value.trim(),
      parse: r.querySelector(".m-parse").value,
    })).filter(m => m.topic || m.metric);
    const http_inputs = [...https.querySelectorAll(".rule-card")].map(c => ({
      url: c.querySelector(".h-url").value.trim(),
      map: [...c.querySelectorAll(".h-map .row")].map(r => ({ metric: r.querySelector(".h-metric").value.trim() })).filter(m => m.metric),
    })).filter(h => h.url || h.map.length);
    const computed = [...comps.querySelectorAll(".row")].map(r => ({
      name: r.querySelector(".co-name").value.trim(), expr: r.querySelector(".co-expr").value.trim(),
    })).filter(c => c.name || c.expr);
    return { variables, mqtt_inputs, http_inputs, computed };
  }

  const RESERVED = ["is_raining", "precip_accum_in", "precipitation_probability", "temperature",
    "wind_speed_mph", "humidity", "short_forecast", "active_alert", "time_hour", "time_minute",
    "time_weekday", "time_is_weekend", "time_is_daytime"];
  function validate(src) {
    const seen = {};
    function claim(name) {
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return "'" + name + "' is not a valid metric name.";
      if (RESERVED.includes(name)) return "'" + name + "' collides with a built-in metric.";
      if (seen[name]) return "'" + name + "' is defined twice (collides).";
      seen[name] = 1; return "";
    }
    for (const v of src.variables) { const e = claim("var_" + v.name); if (e) return e; }
    for (const m of src.mqtt_inputs) { if (!m.metric) return "Each MQTT input needs a metric name."; const e = claim(m.metric); if (e) return e; }
    for (const h of src.http_inputs) for (const mp of h.map) { const e = claim(mp.metric); if (e) return e; }
    for (const c of src.computed) {
      if (!c.expr) return "Computed metric '" + c.name + "' needs a formula.";
      const e = claim(c.name); if (e) return e;
      const refs = (c.expr.match(/[A-Za-z_][A-Za-z0-9_]*/g) || []);
      for (const r of refs) if (!seen[r] && !RESERVED.includes(r)) return "Computed '" + c.name + "' references unknown metric '" + r + "' (define it above).";
    }
    return "";
  }

  const errBox = document.getElementById("inputs-err");
  form.addEventListener("submit", e => {
    e.preventDefault();
    const src = collect();
    const err = validate(src);
    errBox.textContent = err; errBox.style.color = "#fda4a4";
    if (err) { toast("Could not save: " + err, true); return; }
    toast("Inputs saved (demo — nothing was written).");
  });

  (SRC.variables || []).forEach(v => vars.appendChild(varRow(v)));
  (SRC.mqtt_inputs || []).forEach(m => mqtts.appendChild(mqttRow(m)));
  (SRC.http_inputs || []).forEach(h => https.appendChild(httpCard(h)));
  (SRC.computed || []).forEach(c => comps.appendChild(compRow(c)));
})();

/* =========================================================================
   MQTT  ·  live console: simulated topic feed + topics view + publish (toast)
   Mirrors the live webui.py MQTT page; mock data, no broker.
   ========================================================================= */
(function mqtt() {
  if (!document.getElementById("feedbody") || !document.getElementById("p-send")) return;

  // A small set of topics the demo "broker" emits on, with payload generators.
  const SOURCES = [
    { topic: "sensors/tank/level",   gen: () => (40 + Math.random() * 20).toFixed(1), retain: true },
    { topic: "sensors/power/kw",     gen: () => (1 + Math.random() * 4).toFixed(2),  retain: false },
    { topic: "facility/door/north",  gen: () => (Math.random() < 0.5 ? "OPEN" : "CLOSED"), retain: true },
    { topic: "irrigation/rain_inhibit", gen: () => (Math.random() < 0.5 ? "INHIBIT" : "ALLOW"), retain: true },
    { topic: "weather/status",       gen: () => JSON.stringify({ t: Math.round(55 + Math.random() * 20), rh: Math.round(40 + Math.random() * 40) }), retain: true },
    { topic: "facility/vent_fan",    gen: () => (Math.random() < 0.5 ? "ON" : "OFF"), retain: true },
  ];
  let seq = 0, received = 0;
  const ROWS = [];           // recent feed entries (mock ring buffer)
  const LATEST = {};         // topic -> {payload, ts, count, retain, qos}
  const MAXROWS = 400;

  function emit() {
    const s = SOURCES[Math.floor(Math.random() * SOURCES.length)];
    const qos = Math.random() < 0.3 ? 1 : 0;
    const e = { seq: ++seq, ts: isoNow(), topic: s.topic, payload: String(s.gen()), qos, retain: s.retain };
    ROWS.push(e); received++;
    if (ROWS.length > MAXROWS) ROWS.shift();
    const cur = LATEST[s.topic];
    LATEST[s.topic] = { payload: e.payload, ts: e.ts, qos, retain: s.retain, count: cur ? cur.count + 1 : 1 };
  }
  // Seed some history so the page isn't empty on load.
  for (let i = 0; i < 12; i++) emit();

  const ago = iso => { const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000)); return s < 5 ? "now" : s < 60 ? s + "s" : Math.round(s / 60) + "m"; };
  function flags(m) { let f = []; if (m.retain) f.push('<span class="pill na" style="padding:1px 6px">R</span>'); if (m.qos) f.push('<span class="pill off" style="padding:1px 6px">q' + m.qos + "</span>"); return f.join(" "); }

  function filtered() {
    const pre = document.getElementById("filter").value.trim();
    return pre ? ROWS.filter(m => m.topic.startsWith(pre)) : ROWS;
  }
  function renderFeed() {
    const tb = document.getElementById("feedbody");
    const rows = filtered();
    document.getElementById("feednote").textContent = "Live · " + Object.keys(LATEST).length + " topics · " + received + " received";
    document.getElementById("mq-conn").innerHTML = '<span class="dot up"></span>connected · ' + received + " msgs";
    if (!rows.length) { tb.innerHTML = '<tr><td colspan="4" class="muted">No messages on this filter yet.</td></tr>'; return; }
    tb.innerHTML = "";
    for (const m of rows.slice(-120).reverse()) {
      const tr = document.createElement("tr"); tr.style.cursor = "pointer";
      tr.innerHTML = '<td class="muted" title="' + esc(m.ts) + '">' + esc(ago(m.ts)) + "</td>" +
        "<td><code>" + esc(m.topic) + "</code></td><td>" + flags(m) + "</td>" +
        '<td style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px">' + esc(m.payload) + "</td>";
      tr.addEventListener("click", () => { document.getElementById("p-topic").value = m.topic; });
      tb.appendChild(tr);
    }
  }
  function renderTopics() {
    const tb = document.getElementById("topicsbody");
    const list = Object.keys(LATEST).sort().map(t => ({ topic: t, ...LATEST[t] }));
    if (!list.length) { tb.innerHTML = '<tr><td colspan="4" class="muted">No topics seen yet…</td></tr>'; return; }
    tb.innerHTML = "";
    for (const t of list) {
      const tr = document.createElement("tr"); tr.style.cursor = "pointer";
      const pl = t.payload.length > 120 ? t.payload.slice(0, 120) + "…" : t.payload;
      tr.innerHTML = "<td><code>" + esc(t.topic) + "</code></td><td class=\"muted\">" + t.count + "</td>" +
        '<td class="muted" title="' + esc(t.ts) + '">' + esc(ago(t.ts)) + "</td>" +
        '<td style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px">' + esc(pl) + "</td>";
      tr.addEventListener("click", () => { document.getElementById("p-topic").value = t.topic; document.getElementById("filter").value = t.topic; });
      tb.appendChild(tr);
    }
  }

  document.getElementById("p-send").addEventListener("click", () => {
    const topic = document.getElementById("p-topic").value.trim();
    const err = document.getElementById("p-err"); err.textContent = "";
    if (!topic) { err.textContent = "Topic is required."; return; }
    if (/[#+]/.test(topic)) { err.textContent = "Wildcards (# +) are not allowed in a publish topic."; toast("Could not publish: wildcards not allowed", true); return; }
    // Echo it into the feed like a real publish would appear on a subscribed broker.
    const payload = document.getElementById("p-payload").value;
    const qos = Number(document.getElementById("p-qos").value);
    const retain = document.getElementById("p-retain").checked;
    const e = { seq: ++seq, ts: isoNow(), topic, payload: String(payload), qos, retain };
    ROWS.push(e); received++;
    const cur = LATEST[topic]; LATEST[topic] = { payload: String(payload), ts: e.ts, qos, retain, count: cur ? cur.count + 1 : 1 };
    renderFeed();
    toast("Published to " + topic + " (demo — nothing was sent).");
  });

  document.getElementById("clear").addEventListener("click", () => { ROWS.length = 0; renderFeed(); });
  document.getElementById("filter").addEventListener("input", renderFeed);
  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active")); t.classList.add("active");
    const tab = t.dataset.tab;
    document.getElementById("tab-feed").style.display = tab === "feed" ? "" : "none";
    document.getElementById("tab-topics").style.display = tab === "topics" ? "" : "none";
    if (tab === "topics") renderTopics();
  }));

  document.querySelector('.tab[data-tab="feed"]').click();
  renderFeed();
  setInterval(() => {
    if (document.getElementById("follow").checked) { emit(); if (Math.random() < 0.5) emit(); renderFeed(); }
    if (document.getElementById("tab-topics").style.display !== "none") renderTopics();
  }, 1500);
})();

/* =========================================================================
   HISTORY  ·  metric trend sparklines (mock time series in the demo)
   Mirrors the live webui.py History page; client-side only, no backend.
   ========================================================================= */
(function history() {
  const charts = document.getElementById("charts");
  const win = document.getElementById("win");
  if (!charts || !win) return;

  // Mock metric generators: a base value the series drifts around (with a daily
  // wobble), so the sparklines look like plausible sensor history.
  const METRICS = [
    { name: "temperature",     base: 68, amp: 14, jit: 1.2, dec: 1 },
    { name: "humidity",        base: 55, amp: 25, jit: 3,   dec: 0 },
    { name: "precip_accum_in", base: 0.1, amp: 0.18, jit: 0.04, dec: 2, floor: 0 },
    { name: "wind_speed_mph",  base: 9,  amp: 7,  jit: 2,   dec: 0, floor: 0 },
    { name: "tank_level",      base: 60, amp: 22, jit: 1.5, dec: 1 },
    { name: "net_power",       base: 2.4, amp: 3.5, jit: 0.5, dec: 2 },
  ];
  // A deterministic-ish pseudo-random so re-renders of the same window look stable.
  function series(m, hours) {
    const n = Math.min(180, Math.max(24, Math.round(hours * (hours <= 24 ? 4 : 1))));
    const now = Date.now(), span = hours * 3600 * 1000;
    const out = [];
    for (let i = 0; i < n; i++) {
      const frac = i / (n - 1);
      const ts = new Date(now - span + frac * span).toISOString().replace(/\.\d+Z$/, "Z");
      const daily = Math.sin(frac * Math.PI * 2 * Math.max(1, hours / 24));
      let v = m.base + daily * m.amp + (Math.random() - 0.5) * m.jit * 2;
      if (m.floor != null) v = Math.max(m.floor, v);
      const p = Math.pow(10, m.dec); v = Math.round(v * p) / p;
      out.push([ts, v]);
    }
    return out;
  }

  function fmtNum(x) { if (x == null) return "—"; const r = Math.round(x * 100) / 100; return (r === Math.round(r)) ? String(r) : r.toFixed(2); }
  function fmtTime(iso) { const t = Date.parse(iso); if (isNaN(t)) return ""; return new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
  function sparkline(points) {
    const W = 260, H = 64, pad = 4;
    const vals = points.map(p => p[1]); let lo = Math.min(...vals), hi = Math.max(...vals);
    if (lo === hi) { lo -= 1; hi += 1; }
    const n = points.length;
    const x = i => pad + (n === 1 ? 0 : (i / (n - 1)) * (W - 2 * pad));
    const y = v => pad + (1 - (v - lo) / (hi - lo)) * (H - 2 * pad);
    let d = ""; points.forEach((p, i) => { d += (i ? " L" : "M") + x(i).toFixed(1) + " " + y(p[1]).toFixed(1); });
    const area = d + " L" + x(n - 1).toFixed(1) + " " + (H - pad) + " L" + x(0).toFixed(1) + " " + (H - pad) + " Z";
    const lx = x(n - 1).toFixed(1), ly = y(points[n - 1][1]).toFixed(1);
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="' + H + '" preserveAspectRatio="none" style="display:block">' +
      '<path d="' + area + '" fill="var(--accentglow)" stroke="none"/>' +
      '<path d="' + d + '" fill="none" stroke="var(--accent)" stroke-width="1.6"/>' +
      '<circle cx="' + lx + '" cy="' + ly + '" r="2.6" fill="var(--accent)"/></svg>';
  }

  function render() {
    const hours = Number(win.value);
    charts.innerHTML = "";
    LAST = {};
    for (const m of METRICS) {
      const pts = series(m, hours); const vals = pts.map(p => p[1]);
      LAST[m.name] = pts;
      const last = vals[vals.length - 1], lo = Math.min(...vals), hi = Math.max(...vals);
      const card = document.createElement("div"); card.className = "card"; card.style.margin = "0";
      card.innerHTML = '<div class="toprow" style="align-items:baseline">' +
        '<div class="eyebrow">' + esc(m.name) + "</div>" +
        '<div class="big" style="font-size:22px;margin:0">' + fmtNum(last) + "</div></div>" +
        '<div style="margin:10px 0 6px">' + sparkline(pts) + "</div>" +
        '<div class="muted" style="display:flex;justify-content:space-between;font-size:11.5px">' +
        "<span>min " + fmtNum(lo) + " · max " + fmtNum(hi) + "</span><span>" + pts.length + " pts</span></div>" +
        '<div class="muted" style="font-size:11px;margin-top:2px">' + fmtTime(pts[0][0]) + " → " + fmtTime(pts[pts.length - 1][0]) + "</div>";
      charts.appendChild(card);
    }
  }
  let LAST = {};
  function exportCsv() {
    const names = Object.keys(LAST).sort();
    if (!names.length) return;
    const rows = {};
    for (const name of names) { for (const [ts, v] of LAST[name]) { (rows[ts] = rows[ts] || {})[name] = v; } }
    const tss = Object.keys(rows).sort();
    const e2 = s => { s = String(s); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    let csv = "ts," + names.map(e2).join(",") + "\n";
    for (const ts of tss) { csv += e2(ts) + "," + names.map(n => rows[ts][n] == null ? "" : rows[ts][n]).join(",") + "\n"; }
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "history-" + win.value + "h.csv"; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    toast("Exported " + tss.length + " rows (demo data).");
  }
  document.getElementById("export").addEventListener("click", exportCsv);
  win.addEventListener("change", render);
  render();
  setInterval(() => { if (document.getElementById("follow").checked) render(); }, 5000);
})();
