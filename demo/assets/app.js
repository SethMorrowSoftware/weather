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
  let lastChange = { irrigation_rain_inhibit: isoNow(), freeze_protection: null, any_nws_alert: isoNow() };
  let prevActive = {};

  function round(n, d) { const p = Math.pow(10, d); return Math.round(n * p) / p; }

  // Re-derive rule state the same way the monitor does, for the demo rules.
  function deriveRules(m) {
    const rules = [
      { name: "irrigation_rain_inhibit", description: "Hold irrigation when raining or >= 0.25 in / 24h",
        topic: "irrigation/rain_inhibit", on_match: "INHIBIT", on_clear: "ALLOW",
        active: m.is_raining || m.precip_accum_in >= 0.25 },
      { name: "freeze_protection", description: "Energize heat trace below freezing",
        topic: "facility/weather/freeze_protection", on_match: "ON", on_clear: "OFF",
        active: m.temperature <= 35 },
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
    if (irr && irr.active !== null && irr.active !== undefined) {
      d.className = "big " + (irr.active ? "inhibit" : "allow");
      setText("directive", irr.current_payload + (irr.active ? " — do NOT water" : " — watering allowed"));
      setText("directive-sub", "topic " + irr.topic + (irr.last_change ? " · changed " + irr.last_change : ""));
    } else {
      d.className = "big unknown"; setText("directive", "UNKNOWN");
    }

    const m = s.metrics;
    setText("updated", "updated " + s.updated);
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
        "<td>" + esc(r.current_payload) + '</td><td class="muted">' + esc(r.last_change || "—") + "</td>";
      tb.appendChild(tr);
    }
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
    const errBox = el.parentElement.querySelector(".field-err");
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
   RULES  ·  lightweight YAML-shape validator (the live server uses full YAML)
   ========================================================================= */
(function rules() {
  const form = document.getElementById("rules-form");
  if (!form) return;
  const ta = document.getElementById("rules_yaml");
  const errBox = document.getElementById("rules-err");
  const METRICS = ["is_raining", "precip_accum_in", "precipitation_probability", "temperature",
                   "wind_speed_mph", "humidity", "short_forecast", "active_alert"];

  // Heuristic structural check: split top-level "- name:" blocks, require the
  // four mandatory keys and a recognised metric. Good enough for demo feedback;
  // the real app parses the YAML properly server-side.
  function validate(text) {
    if (!text.trim()) return "Rules list is empty.";
    const lines = text.replace(/\t/g, "  ").split("\n");
    const items = [];
    let cur = null, startLine = 0;
    lines.forEach((ln, i) => {
      if (/^- /.test(ln)) {
        if (cur !== null) items.push({ text: cur, line: startLine });
        cur = ln + "\n"; startLine = i + 1;
      } else if (cur !== null) {
        cur += ln + "\n";
      } else if (ln.trim() && !ln.startsWith("#")) {
        // content before the first "- " => not a list
      }
    });
    if (cur !== null) items.push({ text: cur, line: startLine });
    if (!items.length) return "Expected a YAML list (each rule starts with '- ').";

    for (let k = 0; k < items.length; k++) {
      // normalise the leading "- " into indentation so the first key (which
      // appears as "- name:") is checked the same way as the rest.
      const blk = items[k].text.replace(/^- /, "  ");
      const ln = items[k].line, label = "rule #" + (k + 1) + " (line " + ln + ")";
      for (const key of ["name", "when", "topic", "on_match"]) {
        if (!new RegExp("(^|\\n)\\s*" + key + ":").test(blk)) return label + ": missing '" + key + "'.";
      }
      const metricMatches = blk.match(/metric:\s*([A-Za-z_]+)/g) || [];
      if (!metricMatches.length) return label + ": needs at least one 'metric:' under 'when'.";
      for (const mm of metricMatches) {
        const name = mm.split(":")[1].trim();
        if (!METRICS.includes(name)) return label + ": unknown metric '" + name + "'.";
      }
      const payloads = blk.match(/(on_match|on_clear):\s*([^\n#]*)/g) || [];
      for (const p of payloads) {
        const val = p.split(":").slice(1).join(":").trim();
        if (/^(on|off|yes|no|true|false)$/i.test(val))
          return label + ": quote the payload \"" + val + "\" (unquoted it becomes a boolean).";
      }
    }
    return "";
  }

  function check(showOk) {
    const err = validate(ta.value);
    errBox.textContent = err;
    errBox.style.color = err ? "#fda4a4" : "#86efac";
    if (!err && showOk) errBox.textContent = "Looks valid ✓";
    return !err;
  }

  const EXAMPLE = "\n- name: high_wind_hold\n  description: \"Pause watering in high wind\"\n  when:\n    metric: wind_speed_mph\n    operator: \">=\"\n    value: 25\n  topic: \"facility/weather/high_wind\"\n  on_match: \"1\"\n  on_clear: \"0\"\n";

  document.getElementById("add-example").addEventListener("click", () => {
    ta.value = ta.value.replace(/\s*$/, "") + "\n" + EXAMPLE;
    ta.focus(); check(true);
  });
  document.getElementById("check").addEventListener("click", () => check(true));
  ta.addEventListener("input", () => { errBox.textContent = ""; });

  form.addEventListener("submit", e => {
    e.preventDefault();
    if (check(false)) toast("Rules saved (demo — nothing was written).");
    else toast("Could not save: " + errBox.textContent, true);
  });
})();
