#!/usr/bin/env python3
"""
webui.py -- Optional web interface for the precipitation -> MQTT controller.

Shows live conditions and the current irrigation directive, and lets you edit
config.yaml (location, thresholds, MQTT, and the rules) from a browser. The
monitor (weather_mqtt.py) reloads config.yaml every poll cycle, so most edits
take effect on the next cycle with no restart. Changing location or MQTT
connection settings requires restarting the monitor service.

Run:  python webui.py --config config.yaml
"""

import argparse
import functools
import hmac
import json
from pathlib import Path

from flask import Flask, request, url_for, render_template_string, Response, jsonify

import weather_mqtt as core

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString as _DQ
    _yaml = YAML()
    _yaml.preserve_quotes = True
    _yaml.indent(mapping=2, sequence=4, offset=2)
    _HAVE_RUAMEL = True
except Exception:  # pragma: no cover - fallback path
    import yaml as _pyyaml
    _HAVE_RUAMEL = False


def _qstr(s):
    """Quote a string when dumping so the monitor's PyYAML (1.1) loader never
    reinterprets it (e.g. ON/OFF/YES/NO -> bool, '0700' -> int)."""
    return _DQ(s) if _HAVE_RUAMEL else s


def _protect(obj):
    """Recursively force-quote any string whose bare YAML form would parse as a
    non-string (bool/int/float/null), so 1.2-writer / 1.1-reader stays lossless."""
    import yaml as _py
    if isinstance(obj, dict):
        return {k: _protect(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_protect(v) for v in obj]
    if isinstance(obj, str):
        try:
            reparsed = _py.safe_load(obj)
        except Exception:
            reparsed = obj
        if not isinstance(reparsed, str) or reparsed != obj:
            return _qstr(obj)
    return obj

app = Flask(__name__)
CONFIG_PATH = "config.yaml"


# ---------------------------------------------------------------------------
# Config + state IO
# ---------------------------------------------------------------------------
def load_raw():
    """Load config preserving comments/structure when ruamel is available."""
    text = Path(CONFIG_PATH).read_text()
    if _HAVE_RUAMEL:
        return _yaml.load(text)
    return _pyyaml.safe_load(text)


def dump_raw(data):
    """Serialize config back to a string."""
    import io
    if _HAVE_RUAMEL:
        buf = io.StringIO()
        _yaml.dump(data, buf)
        return buf.getvalue()
    return _pyyaml.safe_dump(data, sort_keys=False)


def save_config(data):
    """Validate then atomically write config.yaml, keeping a timestamp-free .bak.

    Validation round-trips the serialized text through the monitor's own
    loader/validator, so the UI can never persist a config the monitor would
    choke on. The previous file is copied to config.yaml.bak first.
    """
    text = dump_raw(data)
    import yaml as _y
    parsed = _y.safe_load(text)
    core_check(parsed)  # raises ValueError on a bad config
    p = Path(CONFIG_PATH)
    if p.exists():
        Path(str(p) + ".bak").write_text(p.read_text())
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(text)
    tmp.replace(p)


def core_check(parsed):
    """Reuse the monitor's full structural + range validation.

    validate_config mutates/clamps a *copy*, so the user's saved values are
    preserved verbatim; this call only confirms the config is loadable. Range
    problems the user should see (bad lat/lon, etc.) are reported by the form
    handlers before we ever get here, with friendlier messages.
    """
    import copy
    core.validate_config(copy.deepcopy(parsed))


def load_state(cfg):
    path = cfg.get("state_file", "weather_state.json")
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Optional basic auth
# ---------------------------------------------------------------------------
def _auth_ok():
    """True when the request satisfies basic auth (or auth is disabled)."""
    try:
        web = (load_raw().get("web", {}) or {})
    except Exception:
        web = {}
    user = str(web.get("username", "") or "")
    pw = str(web.get("password", "") or "")
    if not user:
        return True  # no username configured -> auth disabled (trusted LAN)
    auth = request.authorization
    if not auth or auth.username is None or auth.password is None:
        return False
    # constant-time comparison so the endpoint doesn't leak length/contents
    return (hmac.compare_digest(auth.username, user)
            and hmac.compare_digest(auth.password, pw))


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not _auth_ok():
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="weather-mqtt"'})
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
FAVICON = ("data:image/svg+xml,"
           "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
           "%3Ctext y='.9em' font-size='90'%3E%F0%9F%8C%A7%3C/text%3E%3C/svg%3E")

BASE = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>{{ title or 'Precipitation → MQTT' }}</title>
<link rel="icon" href="{{ favicon }}">
<style>
 :root{
   --bg:#0b1220;--panel:#111c30;--panel2:#0d1626;--line:#26344b;--line2:#1b2740;
   --ink:#e6edf6;--muted:#8aa0bd;--muted2:#5f7494;--accent:#3b82f6;--accent2:#2563eb;
   --good:#22c55e;--bad:#f87171;--warn:#fbbf24;
 }
 *{box-sizing:border-box}
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;
   background:radial-gradient(1200px 600px at 80% -10%,#15233c 0,var(--bg) 55%);
   color:var(--ink);min-height:100vh;-webkit-font-smoothing:antialiased}
 a{color:#8bbcff}
 header{position:sticky;top:0;z-index:10;background:rgba(13,22,38,.86);
   backdrop-filter:blur(8px);border-bottom:1px solid var(--line2);
   padding:12px 22px;display:flex;gap:22px;align-items:center}
 header h1{font-size:16px;margin:0;color:#fff;display:flex;gap:9px;align-items:center;font-weight:700}
 nav{display:flex;gap:6px;flex-wrap:wrap}
 nav a{color:var(--muted);text-decoration:none;font-size:14px;padding:6px 12px;border-radius:8px}
 nav a:hover{color:#fff;background:#16233b}
 nav a.active{color:#fff;background:#1d2c49;font-weight:600}
 .spacer{flex:1}
 .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px;
   box-shadow:0 0 0 3px rgba(255,255,255,.04)}
 .dot.up{background:var(--good)}.dot.down{background:var(--bad)}.dot.idle{background:var(--muted2)}
 .conn{font-size:12.5px;color:var(--muted);display:flex;align-items:center}
 main{max-width:960px;margin:26px auto;padding:0 18px 60px}
 .card{background:linear-gradient(180deg,var(--panel),var(--panel2));
   border:1px solid var(--line2);border-radius:14px;padding:20px 22px;margin-bottom:18px;
   box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 10px 30px -20px rgba(0,0,0,.8)}
 .card h3{margin:0 0 4px;font-size:15px;letter-spacing:.2px}
 .eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:11px;color:var(--muted2);font-weight:700}
 .big{font-size:34px;font-weight:800;margin:8px 0;letter-spacing:-.5px}
 .inhibit{color:var(--bad)}.allow{color:var(--good)}.unknown{color:var(--warn)}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{text-align:left;padding:10px 10px;border-bottom:1px solid var(--line)}
 tr:last-child td{border-bottom:0}
 th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
 code{background:#0a1322;border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-size:12.5px}
 .pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap}
 .on{background:#3a1115;color:#fecaca;box-shadow:0 0 0 1px #7f1d1d inset}
 .off{background:#0f2e1c;color:#bbf7d0;box-shadow:0 0 0 1px #14532d inset}
 .na{background:#1c2740;color:#cdd9ec;box-shadow:0 0 0 1px #2c3c5a inset}
 label{display:block;font-size:13px;color:var(--muted);margin:14px 0 5px;font-weight:600}
 .hint{font-weight:400;color:var(--muted2)}
 input,textarea,select{width:100%;background:#0a1322;color:var(--ink);
   border:1px solid var(--line);border-radius:8px;padding:9px 11px;font-size:14px;font-family:inherit}
 input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);
   box-shadow:0 0 0 3px rgba(59,130,246,.18)}
 textarea{min-height:340px;font-family:ui-monospace,Menlo,Consolas,monospace;line-height:1.5}
 .row{display:flex;gap:16px;flex-wrap:wrap}.row>div{flex:1;min-width:160px}
 button{background:var(--accent2);color:#fff;border:0;border-radius:9px;padding:11px 20px;
   font-size:14px;font-weight:700;cursor:pointer;margin-top:18px}
 button:hover{background:var(--accent)}
 .msg{padding:11px 15px;border-radius:9px;margin-bottom:16px;font-size:14px;font-weight:600}
 .ok{background:#0f2e1c;color:#bbf7d0;box-shadow:0 0 0 1px #14532d inset}
 .err{background:#3a1115;color:#fecaca;box-shadow:0 0 0 1px #7f1d1d inset}
 .muted{color:var(--muted2);font-size:12.5px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px}
 .metric{background:#0a1322;border:1px solid var(--line);border-radius:11px;padding:13px 14px}
 .metric .v{font-size:22px;font-weight:700}.metric .k{color:var(--muted);font-size:12px;margin-top:3px}
 .toprow{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 hr{border:0;border-top:1px solid var(--line2);margin:18px 0}
</style></head><body>
<header>
 <h1>🌧 Precipitation → MQTT</h1>
 <nav>
  <a href="{{ url_for('dashboard') }}" class="{{ 'active' if page=='dash' }}">Dashboard</a>
  <a href="{{ url_for('settings') }}" class="{{ 'active' if page=='settings' }}">Settings</a>
  <a href="{{ url_for('rules') }}" class="{{ 'active' if page=='rules' }}">Rules</a>
 </nav>
 <span class="spacer"></span>
 <span class="conn" id="connstate"><span class="dot idle"></span>weather-mqtt</span>
</header>
<main>
 {% if msg %}<div class="msg {{ msgclass }}">{{ msg }}</div>{% endif %}
 {{ body|safe }}
</main></body></html>
"""


def page(body, **kw):
    kw.setdefault("favicon", FAVICON)
    return render_template_string(BASE, body=body, **kw)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASH = """
<div id="dash" data-state-file="{{ state_file }}">
  <div class="card" id="directive-card">
    <div class="eyebrow">Irrigation directive</div>
    <div class="big unknown" id="directive">…</div>
    <div class="muted" id="directive-sub">Loading current conditions…</div>
  </div>

  <div class="card">
    <div class="toprow">
      <div class="eyebrow">Current conditions</div>
      <div class="muted" id="updated">—</div>
    </div>
    <div class="grid" style="margin-top:12px">
      <div class="metric"><div class="v" id="m_rain">—</div><div class="k">raining now</div></div>
      <div class="metric"><div class="v" id="m_accum">—</div><div class="k" id="m_accum_k">rain last window</div></div>
      <div class="metric"><div class="v" id="m_prob">—</div><div class="k">forecast chance</div></div>
      <div class="metric"><div class="v" id="m_temp">—</div><div class="k">temperature</div></div>
      <div class="metric"><div class="v" id="m_hum">—</div><div class="k">humidity</div></div>
      <div class="metric"><div class="v" id="m_wind">—</div><div class="k">wind mph</div></div>
    </div>
    <p class="muted" style="margin-top:14px" id="forecast">—</p>
  </div>

  <div class="card">
    <div class="eyebrow" style="margin-bottom:10px">Rules</div>
    <table>
      <thead><tr><th>Rule</th><th>Topic</th><th>State</th><th>Payload</th><th>Last change</th></tr></thead>
      <tbody id="rulebody">
        <tr><td colspan="5" class="muted">Loading…</td></tr>
      </tbody>
    </table>
  </div>
  <p class="muted">Live — updates every {{ refresh }}s. <span id="staleness"></span></p>
</div>

<script>
const REFRESH = {{ refresh }} * 1000;
const fmt = v => v === null || v === undefined ? "—" : (v === true ? "yes" : (v === false ? "no" : v));
function setText(id, t){ const e=document.getElementById(id); if(e) e.textContent=t; }

function render(s){
  const conn = document.getElementById("connstate");
  if(!s){
    document.getElementById("directive").className = "big unknown";
    setText("directive","NO DATA");
    setText("directive-sub","No snapshot yet. Start the monitor (weather_mqtt.py); it writes one each poll cycle.");
    conn.innerHTML = '<span class="dot idle"></span>no monitor data';
    return;
  }
  // connection badge
  const up = !!s.mqtt_connected;
  conn.innerHTML = '<span class="dot '+(up?'up':'down')+'"></span>MQTT '+(up?'connected':'offline');

  // directive (first irrigation/rain_inhibit rule)
  const rules = s.rules || [];
  const irr = rules.find(r => /irrigation|rain_inhibit/.test(r.name));
  const d = document.getElementById("directive");
  if(irr && irr.active !== null && irr.active !== undefined){
    d.className = "big " + (irr.active ? "inhibit" : "allow");
    setText("directive", (irr.current_payload ?? "?") + (irr.active ? " — do NOT water" : " — watering allowed"));
    setText("directive-sub", "topic " + irr.topic + (irr.last_change ? " · changed " + irr.last_change : ""));
  } else {
    d.className = "big unknown";
    setText("directive","UNKNOWN");
    setText("directive-sub","No irrigation rule data yet (waiting on weather data).");
  }

  const m = s.metrics || {};
  setText("updated", "updated " + (s.updated || "—"));
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
    const tr = document.createElement("tr");
    let pill;
    if(r.active === null || r.active === undefined) pill = '<span class="pill na">n/a</span>';
    else if(r.active) pill = '<span class="pill on">active</span>';
    else pill = '<span class="pill off">clear</span>';
    tr.innerHTML = '<td>'+esc(r.name)+'<div class="muted">'+esc(r.description||"")+'</div></td>'+
      '<td><code>'+esc(r.topic)+'</code></td><td>'+pill+'</td>'+
      '<td>'+(r.current_payload!=null?esc(r.current_payload):"—")+'</td>'+
      '<td class="muted">'+esc(r.last_change||"—")+'</td>';
    tb.appendChild(tr);
  }
  if(!rules.length) tb.innerHTML = '<tr><td colspan="5" class="muted">No rules.</td></tr>';
}
function esc(s){ const d=document.createElement("div"); d.textContent=String(s); return d.innerHTML; }

async function tick(){
  try{
    const r = await fetch("api/state", {cache:"no-store"});
    render(r.ok ? await r.json() : null);
    setText("staleness","");
  }catch(e){
    setText("staleness","(connection lost — retrying)");
  }
}
tick(); setInterval(tick, REFRESH);
</script>
"""

DASH_REFRESH_SECONDS = 20


@app.route("/")
@require_auth
def dashboard():
    cfg = load_raw()
    body = render_template_string(
        DASH, refresh=DASH_REFRESH_SECONDS,
        state_file=cfg.get("state_file", "weather_state.json"))
    return page(body, page="dash", title="Dashboard · Precipitation → MQTT")


@app.route("/api/state")
@require_auth
def api_state():
    """JSON snapshot the dashboard polls. 503 (not 500) when no data yet so the
    client can show a friendly 'waiting on monitor' state."""
    state = load_state(load_raw())
    if state is None:
        return jsonify({"error": "no state yet"}), 503
    return jsonify(state)


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness + freshness probe for systemd/monitoring."""
    out = {"web": "ok", "monitor": "unknown", "config_ok": False}
    try:
        cfg = load_raw()
        core_check(cfg)
        out["config_ok"] = True
        state = load_state(cfg)
        if state and state.get("updated"):
            out["monitor"] = "ok"
            out["last_update"] = state["updated"]
            out["mqtt_connected"] = bool(state.get("mqtt_connected"))
        else:
            out["monitor"] = "no_data"
    except Exception as e:
        out["error"] = str(e)
    code = 200 if out["config_ok"] else 500
    return jsonify(out), code


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


# ---------------------------------------------------------------------------
# Settings (friendly form for scalar config)
# ---------------------------------------------------------------------------
SETTINGS = """
<form method="post" autocomplete="off">
<div class="card">
  <h3>Location &amp; polling</h3>
  <div class="row">
    <div><label>Latitude <span class="hint">(−90…90)</span></label><input name="latitude" value="{{ c.location.latitude }}"></div>
    <div><label>Longitude <span class="hint">(−180…180)</span></label><input name="longitude" value="{{ c.location.longitude }}"></div>
  </div>
  <div class="row">
    <div><label>Station ID <span class="hint">(optional, blank = nearest)</span></label>
      <input name="station_id" value="{{ c.location.station_id or '' }}" placeholder="e.g. KMGJ"></div>
    <div><label>Poll interval <span class="hint">(minutes, ≥ 1)</span></label>
      <input name="poll_interval_minutes" value="{{ c.poll_interval_minutes }}"></div>
  </div>
  <label>User-Agent <span class="hint">(NWS requires a real contact email/phone)</span></label>
  <input name="user_agent" value="{{ c.user_agent }}">
  <div class="row">
    <div><label>Rain lookback window <span class="hint">(hours, 1…720)</span></label>
      <input name="lookback_hours" value="{{ c.precipitation.lookback_hours }}"></div>
    <div><label>Always publish <span class="hint">(re-send every cycle as a heartbeat)</span></label>
      <select name="always_publish">
        <option value="false" {{ 'selected' if not c.always_publish }}>false</option>
        <option value="true" {{ 'selected' if c.always_publish }}>true</option>
      </select></div>
  </div>
</div>

<div class="card">
  <h3>MQTT broker</h3>
  <div class="row">
    <div><label>Host</label><input name="mqtt_host" value="{{ c.mqtt.host }}"></div>
    <div><label>Port <span class="hint">(1…65535)</span></label><input name="mqtt_port" value="{{ c.mqtt.port }}"></div>
  </div>
  <div class="row">
    <div><label>Username <span class="hint">(blank = anonymous)</span></label><input name="mqtt_username" value="{{ c.mqtt.username }}" autocomplete="off"></div>
    <div><label>Password</label><input name="mqtt_password" type="password" value="{{ c.mqtt.password }}" autocomplete="new-password"></div>
  </div>
  <div class="row">
    <div><label>Client ID</label><input name="mqtt_client_id" value="{{ c.mqtt.client_id }}"></div>
    <div><label>QoS <span class="hint">(0, 1, 2)</span></label>
      <select name="mqtt_qos">
        {% for q in [0,1,2] %}<option value="{{ q }}" {{ 'selected' if c.mqtt.qos == q }}>{{ q }}</option>{% endfor %}
      </select></div>
  </div>
  <div class="row">
    <div><label>Retain <span class="hint">(broker keeps last value for new subscribers)</span></label>
      <select name="mqtt_retain">
        <option value="true" {{ 'selected' if c.mqtt.retain }}>true</option>
        <option value="false" {{ 'selected' if not c.mqtt.retain }}>false</option>
      </select></div>
    <div><label>Status topic <span class="hint">(JSON snapshot, blank = off)</span></label>
      <input name="status_topic" value="{{ c.mqtt.status_topic or '' }}"></div>
  </div>
</div>

<div class="card">
  <h3>Web interface</h3>
  <div class="row">
    <div><label>Bind host <span class="hint">(0.0.0.0 = all, 127.0.0.1 = local only)</span></label>
      <input name="web_host" value="{{ c.web.host }}"></div>
    <div><label>Port <span class="hint">(1…65535)</span></label><input name="web_port" value="{{ c.web.port }}"></div>
  </div>
  <div class="row">
    <div><label>Login username <span class="hint">(blank = no auth)</span></label>
      <input name="web_username" value="{{ c.web.username }}" autocomplete="off"></div>
    <div><label>Login password</label>
      <input name="web_password" type="password" value="{{ c.web.password }}" autocomplete="new-password"></div>
  </div>
  <p class="muted">⚠ Changing <b>location</b>, the <b>MQTT connection</b> (host/port/credentials/client id),
   or any <b>web interface</b> setting needs a restart of the corresponding service.
   Thresholds, lookback, poll interval, QoS, retain, status topic and rules apply on the next poll automatically.</p>
  <button type="submit">Save settings</button>
</div></form>
"""


def _num(s):
    """Parse a form field into int or float; raises ValueError on junk/blank."""
    s = (s or "").strip()
    if s == "":
        raise ValueError("value is required")
    try:
        return int(s)
    except ValueError:
        return float(s)


def _ranged(name, raw, lo, hi, integer=False):
    v = _num(raw)
    if integer:
        if isinstance(v, float) and not v.is_integer():
            raise ValueError(f"{name} must be a whole number")
        v = int(v)
    if not (lo <= v <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


@app.route("/settings", methods=["GET", "POST"])
@require_auth
def settings():
    cfg = load_raw()
    msg = msgclass = None
    if request.method == "POST":
        f = request.form
        try:
            ua = f.get("user_agent", "").strip()
            if not ua:
                raise ValueError("User-Agent is required (NWS rejects requests without one)")

            loc = cfg["location"]
            loc["latitude"] = _ranged("Latitude", f.get("latitude"), -90, 90)
            loc["longitude"] = _ranged("Longitude", f.get("longitude"), -180, 180)
            st = f.get("station_id", "").strip()
            if st:
                loc["station_id"] = _qstr(st)
            elif "station_id" in loc:
                del loc["station_id"]

            cfg["user_agent"] = _qstr(ua)
            cfg["poll_interval_minutes"] = _ranged(
                "Poll interval", f.get("poll_interval_minutes"), 1, 1440, integer=True)
            cfg["always_publish"] = f.get("always_publish") == "true"
            cfg.setdefault("precipitation", {})["lookback_hours"] = _ranged(
                "Lookback window", f.get("lookback_hours"), 1, 720, integer=True)

            mq = cfg["mqtt"]
            mq["host"] = _qstr(f.get("mqtt_host", "").strip() or "localhost")
            mq["port"] = _ranged("MQTT port", f.get("mqtt_port"), 1, 65535, integer=True)
            mq["username"] = _qstr(f.get("mqtt_username", ""))
            mq["password"] = _qstr(f.get("mqtt_password", ""))
            mq["client_id"] = _qstr(f.get("mqtt_client_id", "").strip() or "weather-mqtt-controller")
            mq["qos"] = _ranged("QoS", f.get("mqtt_qos"), 0, 2, integer=True)
            mq["retain"] = f.get("mqtt_retain", "true") == "true"
            mq["status_topic"] = _qstr(f.get("status_topic", "").strip())

            web = cfg.setdefault("web", {})
            web["host"] = _qstr(f.get("web_host", "").strip() or "0.0.0.0")
            web["port"] = _ranged("Web port", f.get("web_port"), 1, 65535, integer=True)
            web["username"] = _qstr(f.get("web_username", "").strip())
            web["password"] = _qstr(f.get("web_password", ""))

            save_config(cfg)
            msg, msgclass = ("Settings saved. Thresholds/MQTT-publish/rules apply on the "
                             "next poll; location, MQTT connection and web changes need a "
                             "service restart.", "ok")
            cfg = load_raw()
        except Exception as e:
            msg, msgclass = f"Could not save: {e}", "err"
            cfg = load_raw()  # discard partial in-memory edits; show what's on disk

    # normalize for template access (defaults if a key is absent)
    cfg.setdefault("precipitation", {}).setdefault("lookback_hours", 24)
    cfg["location"].setdefault("station_id", None)
    mqd = cfg.setdefault("mqtt", {})
    mqd.setdefault("status_topic", "")
    mqd.setdefault("client_id", "weather-mqtt-controller")
    mqd.setdefault("qos", 1)
    mqd.setdefault("retain", True)
    mqd.setdefault("username", "")
    mqd.setdefault("password", "")
    webd = cfg.setdefault("web", {})
    webd.setdefault("host", "0.0.0.0")
    webd.setdefault("port", 8080)
    webd.setdefault("username", "")
    webd.setdefault("password", "")
    body = render_template_string(SETTINGS, c=cfg)
    return page(body, page="settings", msg=msg, msgclass=msgclass,
                title="Settings · Precipitation → MQTT")


# ---------------------------------------------------------------------------
# Rules (raw YAML editor for the rules list)
# ---------------------------------------------------------------------------
RULES = """
<form method="post"><div class="card">
  <h3>Rules</h3>
  <p class="muted">YAML for the <code>rules:</code> list. The first rule controls
   irrigation. A rule's <code>when</code> can be a single condition or an
   <code>any</code> (OR) / <code>all</code> (AND) group. The whole list is
   validated before saving; on error nothing is written and your text is kept.</p>
  <label>rules:</label>
  <textarea name="rules_yaml" id="rules_yaml" spellcheck="false">{{ rules_yaml }}</textarea>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <button type="submit">Save rules</button>
    <button type="button" id="add-example" style="background:#1d2c49">Append example rule</button>
  </div>
  <details style="margin-top:16px">
    <summary class="muted" style="cursor:pointer">Available metrics &amp; operators</summary>
    <table style="margin-top:10px">
      <thead><tr><th>Metric</th><th>Meaning</th><th>Operators</th></tr></thead>
      <tbody>
        <tr><td><code>is_raining</code></td><td>precipitating right now</td><td><code>== !=</code></td></tr>
        <tr><td><code>precip_accum_in</code></td><td>measured rain over lookback (in)</td><td><code>&lt; &lt;= &gt; &gt;= == !=</code></td></tr>
        <tr><td><code>precipitation_probability</code></td><td>forecast chance (%)</td><td><code>&lt; &lt;= &gt; &gt;= == !=</code></td></tr>
        <tr><td><code>temperature</code></td><td>air temp (°F)</td><td><code>&lt; &lt;= &gt; &gt;= == !=</code></td></tr>
        <tr><td><code>wind_speed_mph</code></td><td>wind speed (mph)</td><td><code>&lt; &lt;= &gt; &gt;= == !=</code></td></tr>
        <tr><td><code>humidity</code></td><td>relative humidity (%)</td><td><code>&lt; &lt;= &gt; &gt;= == !=</code></td></tr>
        <tr><td><code>short_forecast</code></td><td>text e.g. "Light Rain"</td><td><code>contains equals</code></td></tr>
        <tr><td><code>active_alert</code></td><td>NWS watches/warnings</td><td><code>any contains equals</code></td></tr>
      </tbody>
    </table>
  </details>
</div></form>
<script>
const EXAMPLE = "\\n- name: high_wind_hold\\n  description: \\"Pause watering in high wind\\"\\n  when:\\n    metric: wind_speed_mph\\n    operator: \\">=\\"\\n    value: 25\\n  topic: \\"facility/weather/high_wind\\"\\n  on_match: \\"1\\"\\n  on_clear: \\"0\\"\\n";
document.getElementById("add-example").addEventListener("click", () => {
  const ta = document.getElementById("rules_yaml");
  ta.value = ta.value.replace(/\\s*$/, "") + "\\n" + EXAMPLE;
  ta.focus();
});
</script>
"""


@app.route("/rules", methods=["GET", "POST"])
@require_auth
def rules():
    cfg = load_raw()
    msg = msgclass = None
    if request.method == "POST":
        try:
            # Parse with ruamel (YAML 1.2): unlike PyYAML's 1.1 loader it does
            # NOT turn unquoted ON/OFF/YES/NO into booleans, so payloads survive.
            if _HAVE_RUAMEL:
                parsed = _to_plain(_yaml.load(request.form["rules_yaml"]))
            else:
                import yaml as _y
                parsed = _y.safe_load(request.form["rules_yaml"])
            if not isinstance(parsed, list) or not parsed:
                raise ValueError("rules must be a non-empty YAML list")
            cfg["rules"] = _protect(parsed)
            save_config(cfg)
            msg, msgclass = "Rules saved. They apply on the next poll cycle.", "ok"
            cfg = load_raw()
        except Exception as e:
            msg, msgclass = f"Could not save: {e}", "err"
            # keep the user's text on screen so edits aren't lost
            body = render_template_string(RULES, rules_yaml=request.form["rules_yaml"])
            return page(body, page="rules", msg=msg, msgclass=msgclass)

    import yaml as _y2
    rules_yaml = _y2.safe_dump(_to_plain(cfg["rules"]), sort_keys=False)
    body = render_template_string(RULES, rules_yaml=rules_yaml)
    return page(body, page="rules", msg=msg, msgclass=msgclass,
                title="Rules · Precipitation → MQTT")


def _to_plain(obj):
    """Convert ruamel structures (incl. its scalar subclasses) to plain
    Python types so PyYAML's safe_dump can represent them."""
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, bool):       # before int: bool is a subclass of int
        return bool(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    return obj


# ---------------------------------------------------------------------------
def main():
    global CONFIG_PATH
    ap = argparse.ArgumentParser(description="Web UI for weather_mqtt")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    CONFIG_PATH = args.config

    cfg = core.load_config(args.config)   # validate on startup
    web = cfg.get("web", {})
    if not web.get("enabled", True):
        print("web.enabled is false in config; refusing to start. "
              "Set web.enabled: true to use the UI.")
        raise SystemExit(1)
    host = args.host or web.get("host", "0.0.0.0")
    port = args.port or web.get("port", 8080)
    print(f"Web UI on http://{host}:{port}  (config: {CONFIG_PATH})")
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
