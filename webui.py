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
import json
from pathlib import Path

from flask import Flask, request, redirect, url_for, render_template_string, Response

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
    """Validate then atomically write config.yaml, keeping a .bak."""
    text = dump_raw(data)
    # Validate by round-tripping through the monitor's own loader.
    import tempfile
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
    """Reuse the monitor's validation rules without touching the filesystem."""
    for key in ("location", "user_agent", "mqtt", "rules"):
        if key not in parsed:
            raise ValueError(f"config is missing required section: '{key}'")
    loc = parsed["location"]
    if "latitude" not in loc or "longitude" not in loc:
        raise ValueError("location needs 'latitude' and 'longitude'")
    if not isinstance(parsed["rules"], list) or not parsed["rules"]:
        raise ValueError("'rules' must be a non-empty list")
    for r in parsed["rules"]:
        for req in ("name", "when", "topic", "on_match"):
            if req not in r:
                raise ValueError(f"rule {r.get('name', '?')} missing '{req}'")


def load_state(cfg):
    path = cfg.get("state_file", "weather_state.json")
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Optional basic auth
# ---------------------------------------------------------------------------
def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        cfg = load_raw()
        web = cfg.get("web", {}) or {}
        user, pw = web.get("username", ""), web.get("password", "")
        if user:
            auth = request.authorization
            if not auth or auth.username != user or auth.password != pw:
                return Response(
                    "Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="weather-mqtt"'})
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
BASE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Precipitation → MQTT</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
 header{background:#1e293b;padding:14px 22px;display:flex;gap:20px;align-items:center}
 header h1{font-size:17px;margin:0;color:#fff}
 nav a{color:#93c5fd;text-decoration:none;margin-right:16px;font-size:14px}
 nav a.active{color:#fff;font-weight:600}
 main{max-width:920px;margin:24px auto;padding:0 18px}
 .card{background:#1e293b;border-radius:10px;padding:18px 20px;margin-bottom:18px}
 .big{font-size:30px;font-weight:700;margin:6px 0}
 .inhibit{color:#f87171}.allow{color:#4ade80}.unknown{color:#fbbf24}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #334155}
 th{color:#94a3b8;font-weight:600}
 .pill{padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
 .on{background:#7f1d1d;color:#fecaca}.off{background:#14532d;color:#bbf7d0}
 .na{background:#374151;color:#d1d5db}
 label{display:block;font-size:13px;color:#94a3b8;margin:12px 0 4px}
 input,textarea,select{width:100%;box-sizing:border-box;background:#0f172a;color:#e2e8f0;
   border:1px solid #334155;border-radius:6px;padding:8px;font-size:14px;font-family:inherit}
 textarea{min-height:320px;font-family:ui-monospace,Menlo,Consolas,monospace}
 .row{display:flex;gap:14px}.row>div{flex:1}
 button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:10px 18px;
   font-size:14px;font-weight:600;cursor:pointer;margin-top:16px}
 .msg{padding:10px 14px;border-radius:6px;margin-bottom:14px}
 .ok{background:#14532d;color:#bbf7d0}.err{background:#7f1d1d;color:#fecaca}
 .muted{color:#64748b;font-size:12px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
 .metric{background:#0f172a;border-radius:8px;padding:12px}
 .metric .v{font-size:20px;font-weight:600}.metric .k{color:#94a3b8;font-size:12px}
</style></head><body>
<header>
 <h1>🌧 Precipitation → MQTT</h1>
 <nav>
  <a href="{{ url_for('dashboard') }}" class="{{ 'active' if page=='dash' }}">Dashboard</a>
  <a href="{{ url_for('settings') }}" class="{{ 'active' if page=='settings' }}">Settings</a>
  <a href="{{ url_for('rules') }}" class="{{ 'active' if page=='rules' }}">Rules</a>
 </nav>
</header>
<main>
 {% if msg %}<div class="msg {{ msgclass }}">{{ msg }}</div>{% endif %}
 {{ body|safe }}
</main></body></html>
"""


def page(body, **kw):
    return render_template_string(BASE, body=body, **kw)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASH = """
{% if not state %}
  <div class="card"><p>No status yet. The monitor writes a snapshot each poll
  cycle to <code>{{ state_file }}</code>. Start <code>weather_mqtt.py</code> and
  refresh.</p></div>
{% else %}
  {% set irr = irr_rule %}
  <div class="card">
    <div class="muted">Irrigation directive</div>
    {% if irr and irr.active is not none %}
      <div class="big {{ 'inhibit' if irr.active else 'allow' }}">
        {{ irr.current_payload }} — {{ 'do NOT water' if irr.active else 'watering allowed' }}
      </div>
      <div class="muted">topic <code>{{ irr.topic }}</code>
        {% if irr.last_change %}· changed {{ irr.last_change }}{% endif %}</div>
    {% else %}
      <div class="big unknown">UNKNOWN</div>
      <div class="muted">No irrigation rule data yet (waiting on weather data).</div>
    {% endif %}
  </div>

  <div class="card">
    <div class="muted">Conditions · updated {{ state.updated }} ·
      MQTT {{ 'connected' if state.mqtt_connected else 'NOT connected' }}</div>
    <div class="grid" style="margin-top:10px">
      <div class="metric"><div class="v">{{ fmt(m.is_raining) }}</div><div class="k">raining now</div></div>
      <div class="metric"><div class="v">{{ fmt(m.precip_accum_in) }} in</div><div class="k">rain last {{ state.lookback_hours }}h</div></div>
      <div class="metric"><div class="v">{{ fmt(m.precipitation_probability) }}%</div><div class="k">forecast chance</div></div>
      <div class="metric"><div class="v">{{ fmt(m.temperature) }}°F</div><div class="k">temperature</div></div>
      <div class="metric"><div class="v">{{ fmt(m.humidity) }}%</div><div class="k">humidity</div></div>
      <div class="metric"><div class="v">{{ fmt(m.wind_speed_mph) }}</div><div class="k">wind mph</div></div>
    </div>
    <p class="muted" style="margin-top:12px">{{ m.short_forecast }} ·
      alerts: {{ m.active_alerts|join(', ') if m.active_alerts else 'none' }}</p>
  </div>

  <div class="card">
    <table><tr><th>Rule</th><th>Topic</th><th>State</th><th>Payload</th><th>Last change</th></tr>
    {% for r in state.rules %}
      <tr>
        <td>{{ r.name }}<div class="muted">{{ r.description }}</div></td>
        <td><code>{{ r.topic }}</code></td>
        <td>{% if r.active is none %}<span class="pill na">n/a</span>
            {% elif r.active %}<span class="pill on">active</span>
            {% else %}<span class="pill off">clear</span>{% endif %}</td>
        <td>{{ r.current_payload if r.current_payload is not none else '—' }}</td>
        <td class="muted">{{ r.last_change or '—' }}</td>
      </tr>
    {% endfor %}
    </table>
  </div>
{% endif %}
<p class="muted">Auto-refreshes every 30s.</p>
<script>setTimeout(()=>location.reload(),30000)</script>
"""


def _fmt(v):
    return "—" if v is None else ("yes" if v is True else ("no" if v is False else v))


@app.route("/")
@require_auth
def dashboard():
    cfg = load_raw()
    state = load_state(cfg)
    m = (state or {}).get("metrics", {}) or {}
    irr = None
    for r in (state or {}).get("rules", []):
        if "irrigation" in r["name"] or "rain_inhibit" in r["name"]:
            irr = r
            break
    body = render_template_string(
        DASH, state=state, m=m, fmt=_fmt, irr_rule=irr,
        state_file=cfg.get("state_file", "weather_state.json"))
    return page(body, page="dash")


# ---------------------------------------------------------------------------
# Settings (friendly form for scalar config)
# ---------------------------------------------------------------------------
SETTINGS = """
<form method="post"><div class="card">
  <h3 style="margin-top:0">Location & polling</h3>
  <div class="row">
    <div><label>Latitude</label><input name="latitude" value="{{ c.location.latitude }}"></div>
    <div><label>Longitude</label><input name="longitude" value="{{ c.location.longitude }}"></div>
  </div>
  <div class="row">
    <div><label>Station ID (optional, blank = nearest)</label>
      <input name="station_id" value="{{ c.location.station_id or '' }}"></div>
    <div><label>Poll interval (minutes)</label>
      <input name="poll_interval_minutes" value="{{ c.poll_interval_minutes }}"></div>
  </div>
  <label>User-Agent (NWS requires a real contact)</label>
  <input name="user_agent" value="{{ c.user_agent }}">
  <div class="row">
    <div><label>Rain lookback window (hours)</label>
      <input name="lookback_hours" value="{{ c.precipitation.lookback_hours }}"></div>
    <div><label>Always publish (heartbeat)</label>
      <select name="always_publish">
        <option value="false" {{ 'selected' if not c.always_publish }}>false</option>
        <option value="true" {{ 'selected' if c.always_publish }}>true</option>
      </select></div>
  </div>
</div>
<div class="card">
  <h3 style="margin-top:0">MQTT broker</h3>
  <div class="row">
    <div><label>Host</label><input name="mqtt_host" value="{{ c.mqtt.host }}"></div>
    <div><label>Port</label><input name="mqtt_port" value="{{ c.mqtt.port }}"></div>
  </div>
  <div class="row">
    <div><label>Username</label><input name="mqtt_username" value="{{ c.mqtt.username }}"></div>
    <div><label>Password</label><input name="mqtt_password" type="password" value="{{ c.mqtt.password }}"></div>
  </div>
  <div class="row">
    <div><label>QoS</label><input name="mqtt_qos" value="{{ c.mqtt.qos }}"></div>
    <div><label>Status topic (JSON snapshot, blank = off)</label>
      <input name="status_topic" value="{{ c.mqtt.status_topic or '' }}"></div>
  </div>
  <p class="muted">Changing location or MQTT connection needs a monitor restart.
   Thresholds, interval and rules apply on the next poll automatically.</p>
  <button type="submit">Save settings</button>
</div></form>
"""


def _num(s):
    s = (s or "").strip()
    try:
        return int(s)
    except ValueError:
        return float(s)


@app.route("/settings", methods=["GET", "POST"])
@require_auth
def settings():
    cfg = load_raw()
    msg = msgclass = None
    if request.method == "POST":
        f = request.form
        try:
            cfg["location"]["latitude"] = _num(f["latitude"])
            cfg["location"]["longitude"] = _num(f["longitude"])
            st = f.get("station_id", "").strip()
            if st:
                cfg["location"]["station_id"] = _qstr(st)
            elif "station_id" in cfg["location"]:
                del cfg["location"]["station_id"]
            cfg["user_agent"] = _qstr(f["user_agent"].strip())
            cfg["poll_interval_minutes"] = _num(f["poll_interval_minutes"])
            cfg["always_publish"] = f.get("always_publish") == "true"
            cfg.setdefault("precipitation", {})["lookback_hours"] = _num(f["lookback_hours"])
            mq = cfg["mqtt"]
            mq["host"] = _qstr(f["mqtt_host"].strip())
            mq["port"] = _num(f["mqtt_port"])
            mq["username"] = _qstr(f["mqtt_username"])
            mq["password"] = _qstr(f["mqtt_password"])
            mq["qos"] = _num(f["mqtt_qos"])
            mq["status_topic"] = _qstr(f.get("status_topic", "").strip())
            save_config(cfg)
            msg, msgclass = "Settings saved. Changes apply on the next poll cycle.", "ok"
            cfg = load_raw()
        except Exception as e:
            msg, msgclass = f"Could not save: {e}", "err"

    # normalize for template access
    cfg.setdefault("precipitation", {}).setdefault("lookback_hours", 24)
    cfg["location"].setdefault("station_id", None)
    cfg["mqtt"].setdefault("status_topic", "")
    body = render_template_string(SETTINGS, c=cfg)
    return page(body, page="settings", msg=msg, msgclass=msgclass)


# ---------------------------------------------------------------------------
# Rules (raw YAML editor for the rules list)
# ---------------------------------------------------------------------------
RULES = """
<form method="post"><div class="card">
  <h3 style="margin-top:0">Rules</h3>
  <p class="muted">YAML for the <code>rules:</code> list. The first rule
   controls irrigation. A rule's <code>when</code> can be a single condition or
   an <code>any</code>/<code>all</code> group. Validated before saving.</p>
  <label>rules:</label>
  <textarea name="rules_yaml">{{ rules_yaml }}</textarea>
  <button type="submit">Save rules</button>
</div></form>
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
    return page(body, page="rules", msg=msg, msgclass=msgclass)


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
