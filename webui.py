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
# Rules — structured form builder + raw YAML editor
# ---------------------------------------------------------------------------
# Single source of truth for which metrics exist, their value type, and the
# operators each accepts. Shared with the browser (serialized into the page) so
# the builder and the server validate against exactly the same rules.
RULE_METRICS = {
    "is_raining":                {"type": "bool",   "ops": ["==", "!="]},
    "precip_accum_in":           {"type": "number", "ops": ["<", "<=", ">", ">=", "==", "!="]},
    "precipitation_probability": {"type": "number", "ops": ["<", "<=", ">", ">=", "==", "!="]},
    "temperature":               {"type": "number", "ops": ["<", "<=", ">", ">=", "==", "!="]},
    "wind_speed_mph":            {"type": "number", "ops": ["<", "<=", ">", ">=", "==", "!="]},
    "humidity":                  {"type": "number", "ops": ["<", "<=", ">", ">=", "==", "!="]},
    "short_forecast":            {"type": "text",   "ops": ["contains", "equals"]},
    "active_alert":              {"type": "alert",  "ops": ["any", "contains", "equals"]},
}


def _coerce_cond_value(metric, operator, raw):
    """Validate a metric/operator pair and coerce the value to the right type.

    Returns the typed value, or None when the operator needs no value
    (active_alert + any). Raises ValueError with a human message on anything
    the builder shouldn't have allowed through."""
    meta = RULE_METRICS.get(metric)
    if meta is None:
        raise ValueError(f"unknown metric '{metric}'")
    if operator not in meta["ops"]:
        raise ValueError(f"operator '{operator}' is not valid for metric '{metric}'")
    if meta["type"] == "alert" and operator == "any":
        return None
    if meta["type"] == "bool":
        return str(raw).strip().lower() in ("true", "1", "yes", "on")
    if meta["type"] == "number":
        try:
            return _num(str(raw))
        except ValueError:
            raise ValueError(f"metric '{metric}' needs a numeric value")
    s = "" if raw is None else str(raw)
    if s == "":
        raise ValueError(f"metric '{metric}' needs a value")
    return s


def _rules_from_structured(items):
    """Build a validated rules list from the builder's JSON payload."""
    if not isinstance(items, list) or not items:
        raise ValueError("add at least one rule")
    out = []
    for idx, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise ValueError(f"rule #{idx} is malformed")
        name = str(it.get("name", "")).strip()
        topic = str(it.get("topic", "")).strip()
        on_match = it.get("on_match", "")
        on_match = "" if on_match is None else str(on_match)
        if not name:
            raise ValueError(f"rule #{idx}: a name is required")
        if not topic:
            raise ValueError(f"rule '{name}': a topic is required")
        if on_match == "":
            raise ValueError(f"rule '{name}': the on_match payload is required")

        conds = []
        for c in (it.get("conditions") or []):
            if not isinstance(c, dict):
                continue
            metric = str(c.get("metric", "")).strip()
            if not metric:
                continue
            operator = str(c.get("operator", "")).strip()
            try:
                val = _coerce_cond_value(metric, operator, c.get("value"))
            except ValueError as e:
                raise ValueError(f"rule '{name}': {e}")
            cond = {"metric": metric, "operator": _qstr(operator)}
            if val is not None:
                cond["value"] = _qstr(val) if isinstance(val, str) else val
            conds.append(cond)
        if not conds:
            raise ValueError(f"rule '{name}': add at least one condition")

        combine = str(it.get("combine", "")).strip().lower()
        if len(conds) == 1:
            when = conds[0]
        else:
            if combine not in ("any", "all"):
                combine = "any"
            when = {combine: conds}

        rule = {"name": _qstr(name)}
        desc = str(it.get("description", "")).strip()
        if desc:
            rule["description"] = _qstr(desc)
        rule["when"] = when
        rule["topic"] = _qstr(topic)
        rule["on_match"] = _qstr(on_match)
        on_clear = it.get("on_clear")
        if on_clear not in (None, ""):
            rule["on_clear"] = _qstr(str(on_clear))
        out.append(rule)
    return out


def _value_to_str(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _rule_to_structured(rule):
    """Flatten a stored rule into the builder's editable shape."""
    if not isinstance(rule, dict):
        return None
    when = rule.get("when")
    combine, raw = "any", []
    if isinstance(when, dict) and ("any" in when or "all" in when):
        combine = "any" if "any" in when else "all"
        raw = when.get(combine) or []
    elif isinstance(when, dict):
        raw = [when]
    conds = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        op = c.get("operator")
        conds.append({
            "metric": str(c.get("metric", "")),
            "operator": "" if op is None else str(op),
            "value": _value_to_str(c.get("value")),
        })
    return {
        "name": str(rule.get("name", "")),
        "description": str(rule.get("description", "")),
        "topic": str(rule.get("topic", "")),
        "on_match": _value_to_str(rule.get("on_match")),
        "on_clear": _value_to_str(rule.get("on_clear")),
        "combine": combine,
        "conditions": conds,
    }


def _structured_list(cfg):
    return [s for s in (_rule_to_structured(r) for r in _to_plain(cfg.get("rules", [])))
            if s is not None]


RULES = """
<style>
 .tabs{display:flex;gap:6px;margin:6px 0 16px}
 .tab{margin:0;background:#0a1322;color:var(--muted);border:1px solid var(--line);
   border-radius:9px;padding:8px 16px;font-weight:600;font-size:14px;cursor:pointer}
 .tab.active{background:#1d2c49;color:#fff;border-color:var(--accent)}
 .rule-card{background:#0a1322;border:1px solid var(--line);border-radius:12px;
   padding:16px 18px;margin-bottom:14px}
 .rule-card .rhead{display:flex;justify-content:space-between;align-items:center;gap:10px}
 .rule-card .rhead .idx{font-size:12px;color:var(--muted2);font-weight:700;text-transform:uppercase;letter-spacing:.08em}
 .cond{align-items:flex-end}
 .cond .rm{flex:0 0 auto;min-width:0}
 button.danger{background:#3a1115;color:#fecaca;box-shadow:0 0 0 1px #7f1d1d inset}
 button.danger:hover{background:#511a20}
 button.mini{padding:8px 12px;margin-top:0}
 .combine-wrap{margin-top:6px}
</style>
<form method="post" id="rules-form">
  <input type="hidden" name="mode" id="mode" value="form">
  <input type="hidden" name="rules_json" id="rules_json">
  <div class="card">
    <h3>Rules</h3>
    <p class="muted">The first rule controls irrigation. Each rule publishes its
     <code>on_match</code> payload to its topic when its condition becomes true,
     and <code>on_clear</code> when it clears. Build rules with the form, or edit
     the raw YAML directly — both are validated before saving.</p>

    <div class="tabs">
      <button type="button" class="tab" data-tab="form">Form builder</button>
      <button type="button" class="tab" data-tab="yaml">YAML (advanced)</button>
    </div>

    <div id="tab-form" class="tabpane">
      <div id="builder"></div>
      <div class="btnrow">
        <button type="button" class="secondary mini" id="add-rule">+ Add rule</button>
      </div>
      <div class="field-err" id="form-err"></div>
      <button type="submit" id="save-form">Save rules</button>
    </div>

    <div id="tab-yaml" class="tabpane" style="display:none">
      <label>rules:</label>
      <textarea name="rules_yaml" id="rules_yaml" spellcheck="false">{{ rules_yaml }}</textarea>
      <div class="btnrow">
        <button type="submit" id="save-yaml">Save rules</button>
        <button type="button" class="secondary mini" id="add-example">Append example rule</button>
      </div>
    </div>

    <details style="margin-top:18px">
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
  </div>
</form>
<script>
const METRICS = {{ metrics|tojson }};
const INITIAL = {{ structured|tojson }};
const ACTIVE_TAB = {{ active_tab|tojson }};
const METRIC_NAMES = Object.keys(METRICS);
const builder = document.getElementById("builder");

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
  const metricWrap = el("div"); const m = document.createElement("select"); m.className="c-metric";
  METRIC_NAMES.forEach(n=> m.appendChild(opt(n,n, n===cond.metric)));
  if(!METRICS[cond.metric]) m.value=METRIC_NAMES[0];
  metricWrap.appendChild(m);
  const opWrap = el("div"); const o=document.createElement("select"); o.className="c-op";
  fillOps(o, m.value, cond.operator); opWrap.appendChild(o);
  const valWrap = el("div","c-val-wrap"); valWrap.appendChild(valueControl(m.value, cond.value));
  const rmWrap = el("div","rm"); const rm=el("button","secondary danger mini","×"); rm.type="button";
  rmWrap.appendChild(rm);

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
  const conds = card.querySelectorAll(".cond").length;
  card.querySelector(".combine-wrap").style.display = conds>1 ? "" : "none";
}

function ruleCard(rule){
  rule = rule || {name:"",description:"",topic:"",on_match:"",on_clear:"",combine:"any",conditions:[]};
  const card = el("div","rule-card");
  card.innerHTML =
    '<div class="rhead"><span class="idx"></span></div>'+
    '<div class="row"><div><label>Name</label><input class="f-name"></div>'+
    '<div><label>Topic</label><input class="f-topic"></div></div>'+
    '<label>Description <span class="hint">(optional)</span></label><input class="f-desc">'+
    '<div class="row"><div><label>Payload when matched <span class="hint">(on_match)</span></label><input class="f-onmatch"></div>'+
    '<div><label>Payload when cleared <span class="hint">(on_clear, optional)</span></label><input class="f-onclear"></div></div>'+
    '<div class="combine-wrap"><label>When there are multiple conditions, match</label>'+
    '<select class="f-combine"></select></div>'+
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
      const valWrap=row.querySelector(".c-val-wrap");
      let value="";
      if(!(meta.type==="alert" && operator==="any")){
        const ctrl=valWrap.querySelector(".c-val"); value=ctrl?ctrl.value:"";
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

function validate(data){
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
  const which=t.dataset.tab;
  document.getElementById("tab-form").style.display = which==="form"?"":"none";
  document.getElementById("tab-yaml").style.display = which==="yaml"?"":"none";
}));

document.getElementById("save-form").addEventListener("click", e=>{
  document.getElementById("mode").value="form";
  const data=collect(); const err=validate(data);
  const box=document.getElementById("form-err");
  if(err){ e.preventDefault(); box.textContent=err; box.style.color="#fda4a4"; return; }
  box.textContent="";
  document.getElementById("rules_json").value=JSON.stringify(data);
});
document.getElementById("save-yaml").addEventListener("click", ()=>{ document.getElementById("mode").value="yaml"; });

const EXAMPLE = "\\n- name: high_wind_hold\\n  description: \\"Pause watering in high wind\\"\\n  when:\\n    metric: wind_speed_mph\\n    operator: \\">=\\"\\n    value: 25\\n  topic: \\"facility/weather/high_wind\\"\\n  on_match: \\"1\\"\\n  on_clear: \\"0\\"\\n";
document.getElementById("add-example").addEventListener("click", ()=>{
  const ta=document.getElementById("rules_yaml");
  ta.value=ta.value.replace(/\\s*$/,"")+"\\n"+EXAMPLE; ta.focus();
});

// initial render
(INITIAL.length ? INITIAL : [null]).forEach(r=> builder.appendChild(ruleCard(r)));
reindex();
document.querySelector('.tab[data-tab="'+(ACTIVE_TAB==="yaml"?"yaml":"form")+'"]').click();
</script>
"""


@app.route("/rules", methods=["GET", "POST"])
@require_auth
def rules():
    cfg = load_raw()
    msg = msgclass = None
    mode = request.form.get("mode", "form") if request.method == "POST" else "form"
    rules_yaml_override = None
    structured_override = None

    if request.method == "POST":
        try:
            if mode == "form":
                items = json.loads(request.form.get("rules_json", "[]"))
                cfg["rules"] = _rules_from_structured(items)
            else:
                # Parse with ruamel (YAML 1.2): unlike PyYAML's 1.1 loader it does
                # NOT turn unquoted ON/OFF/YES/NO into booleans, so payloads survive.
                rules_yaml_override = request.form.get("rules_yaml", "")
                if _HAVE_RUAMEL:
                    parsed = _to_plain(_yaml.load(rules_yaml_override))
                else:
                    import yaml as _y
                    parsed = _y.safe_load(rules_yaml_override)
                if not isinstance(parsed, list) or not parsed:
                    raise ValueError("rules must be a non-empty YAML list")
                cfg["rules"] = _protect(parsed)
            save_config(cfg)
            msg, msgclass = "Rules saved. They apply on the next poll cycle.", "ok"
            cfg = load_raw()
            rules_yaml_override = None  # show the freshly normalized YAML
        except Exception as e:
            msg, msgclass = f"Could not save: {e}", "err"
            cfg = load_raw()
            if mode == "form":
                try:  # preserve the user's in-progress builder edits
                    structured_override = json.loads(request.form.get("rules_json", "[]"))
                except Exception:
                    structured_override = None

    import yaml as _y2
    rules_yaml = (rules_yaml_override if rules_yaml_override is not None
                  else _y2.safe_dump(_to_plain(cfg["rules"]), sort_keys=False))
    structured = structured_override if structured_override is not None else _structured_list(cfg)
    body = render_template_string(
        RULES, rules_yaml=rules_yaml, structured=structured,
        metrics=RULE_METRICS, active_tab=mode)
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
