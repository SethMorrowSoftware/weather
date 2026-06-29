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
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, url_for, render_template_string, Response, jsonify

import weather_mqtt as core

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover - the console degrades gracefully
    mqtt = None

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
# Cap request bodies so an oversized POST (e.g. a giant MQTT payload or rules
# blob) can't balloon memory and OOM the dashboard on a small box.
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MiB
CONFIG_PATH = "config.yaml"

# Serialize config writes so two concurrent saves can't interleave the backup +
# atomic-replace. (Atomic replace already prevents a torn file; this prevents a
# racy .bak.)
_SAVE_LOCK = threading.Lock()


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
    with _SAVE_LOCK:
        if p.exists():
            Path(str(p) + ".bak").write_text(p.read_text())
        # Atomic + fsync so a crash/power-loss can't leave a half-written config
        # the monitor would fail to load on its next cycle.
        core._atomic_write(p, text)


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
# Live MQTT console (subscribe + buffer + publish)
# ---------------------------------------------------------------------------
def _decode_payload(raw, limit=4096):
    """Bytes -> (text, is_binary, truncated). Best-effort UTF-8; non-text
    payloads are hex-previewed so the feed never breaks on binary."""
    if isinstance(raw, str):
        data = raw.encode("utf-8", "replace")
    else:
        data = bytes(raw or b"")
    try:
        text = data.decode("utf-8")
        binary = False
    except Exception:
        text = data[:64].hex(" ")
        binary = True
    truncated = len(text) > limit
    return (text[:limit], binary, truncated)


class MqttConsole:
    """The web UI's own MQTT client: subscribes to wildcard topics, keeps a
    capped ring buffer of recent messages plus a per-topic 'latest value' map,
    and can publish on demand. All buffer/query logic is broker-independent so
    it is unit-testable; the network client is created only by start()."""

    MAX_TOPICS = 2000  # cap the latest-value map so a chatty broker can't grow it forever

    def __init__(self, buffer_size=500):
        self._lock = threading.Lock()
        self._buf = deque(maxlen=buffer_size)
        self._latest = {}            # topic -> {payload, ts, qos, retain, count, binary}
        self._seq = 0
        self._received = 0
        self._client = None
        self._connected = False
        self._topics = ["#"]
        self._started = False
        self._err = None

    # ---- ingest / query (no network) ----
    def record(self, topic, payload, qos=0, retain=False, ts=None):
        text, binary, truncated = _decode_payload(payload)
        ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._seq += 1
            self._received += 1
            entry = {"seq": self._seq, "ts": ts, "topic": str(topic),
                     "payload": text, "qos": int(qos), "retain": bool(retain),
                     "binary": binary, "truncated": truncated}
            self._buf.append(entry)
            cur = self._latest.get(topic)
            if cur is None and len(self._latest) >= self.MAX_TOPICS:
                return entry  # at cap: still buffered in the feed, just not tracked as a distinct topic
            self._latest[topic] = {"payload": text, "ts": ts, "qos": int(qos),
                                   "retain": bool(retain), "binary": binary,
                                   "count": (cur["count"] + 1 if cur else 1)}
            return entry

    def messages(self, since=0, topic=None, limit=300):
        pre = (topic or "").strip()
        with self._lock:
            items = [e for e in self._buf if e["seq"] > since]
        if pre:
            items = [e for e in items if e["topic"].startswith(pre)]
        return items[-limit:]

    def topics(self):
        with self._lock:
            return [dict(topic=t, **v) for t, v in sorted(self._latest.items())]

    def stats(self):
        with self._lock:
            return {"connected": self._connected, "buffered": len(self._buf),
                    "received": self._received, "topics": len(self._latest),
                    "seq": self._seq, "subscribed": list(self._topics),
                    "started": self._started, "error": self._err}

    # ---- publish ----
    def publish(self, topic, payload, qos=0, retain=False):
        topic = (topic or "").strip()
        if not topic:
            return (False, "topic is required")
        if any(c in topic for c in "#+") or "\x00" in topic:
            return (False, "topic must not contain wildcards (# +) or nulls")
        if self._client is None or not self._connected:
            return (False, "MQTT console is not connected to the broker")
        try:
            info = self._client.publish(topic, payload if payload is not None else "",
                                        qos=int(qos), retain=bool(retain))
            rc = getattr(info, "rc", 0)
            if rc != 0:
                return (False, f"publish failed (rc={rc})")
            return (True, None)
        except Exception as e:
            return (False, f"publish error: {e}")

    # ---- lifecycle (network) ----
    def start(self, cfg):
        """Connect + subscribe per config. Idempotent and best-effort: a broker
        that is down just means an empty feed until it reconnects."""
        if self._started or mqtt is None:
            return
        web = cfg.get("web", {}) or {}
        if not web.get("mqtt_console_enabled", True):
            return
        mq = cfg.get("mqtt", {}) or {}
        self._topics = list(web.get("mqtt_console_topics", ["#"])) or ["#"]
        self._buf = deque(self._buf, maxlen=int(web.get("mqtt_console_buffer", 500)))
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=str(mq.get("client_id", "weather-mqtt")) + "-webui")
            if mq.get("username"):
                client.username_pw_set(mq["username"], mq.get("password", ""))

            def on_connect(c, u, flags, reason_code, props):
                self._connected = not reason_code.is_failure
                if self._connected:
                    for t in self._topics:
                        c.subscribe(t)

            def on_disconnect(c, u, flags, reason_code, props):
                self._connected = False

            def on_message(c, u, msg):
                self.record(msg.topic, msg.payload, msg.qos, msg.retain)

            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client.on_message = on_message
            client.reconnect_delay_set(min_delay=1, max_delay=60)
            client.connect_async(mq.get("host", "localhost"),
                                 int(mq.get("port", 1883)), keepalive=60)
            client.loop_start()
            self._client = client
            self._started = True
        except Exception as e:
            self._err = str(e)


console = MqttConsole()


def _config_error_page(page_name, err):
    """Friendly error instead of a 500 when config.yaml can't be read/parsed."""
    body = render_template_string(
        '<div class="card"><h3>Configuration problem</h3>'
        '<div class="msg err">Could not read config.yaml: {{ err }}</div>'
        '<p class="muted">Fix the file on disk (check YAML syntax) and reload '
        'this page. The monitor keeps running on its last good config.</p></div>',
        err=str(err), favicon=FAVICON)
    return page(body, page=page_name, title="Config error · The Castle Fun Center")


# ---------------------------------------------------------------------------
# Optional basic auth
# ---------------------------------------------------------------------------
def _auth_ok():
    """True when the request satisfies basic auth (or auth is disabled).

    Fails CLOSED: if the config can't be read we deny rather than silently
    serving the config editor unauthenticated. A username with an empty password
    is treated as misconfigured and also denied (the Settings form refuses to
    save that combination, but a hand-edit shouldn't open a hole)."""
    try:
        web = (load_raw().get("web", {}) or {})
    except Exception:
        return False  # config unreadable -> deny, never fail open
    user = str(web.get("username", "") or "")
    pw = str(web.get("password", "") or "")
    if not user and not pw:
        return True  # no credentials configured -> auth disabled (trusted LAN)
    if not user or not pw:
        return False  # half-configured credentials -> deny, don't accept blanks
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
           "%3Ctext y='.9em' font-size='90'%3E%F0%9F%8F%B0%3C/text%3E%3C/svg%3E")

BASE = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>{{ title or "The Castle Fun Center · MQTT Command Center" }}</title>
<link rel="icon" href="{{ favicon }}">
<style>
 :root{
   --bg:#0a111f;--bg2:#0c1424;--panel:#111d33;--panel2:#0e1828;
   --line:#243349;--line2:#1a2742;--ink:#e8eef8;--muted:#a3b6d2;--muted2:#7e94b5;
   --accent:#4b8bf5;--accent2:#2f6fe0;--accentglow:rgba(75,139,245,.35);
   --good:#34d399;--good2:#22c55e;--bad:#f87171;--bad2:#ef4444;--warn:#fbbf24;
   --r:10px;--r-lg:16px;--r-pill:999px;--shadow:0 18px 40px -24px rgba(0,0,0,.85);
   --ring:0 0 0 3px var(--accentglow);--t:.18s cubic-bezier(.4,0,.2,1);
 }
 *{box-sizing:border-box}
 body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:0;
   color:var(--ink);min-height:100vh;line-height:1.5;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
   background:radial-gradient(900px 500px at 88% -8%,#1a2c4b 0,transparent 60%),radial-gradient(700px 500px at 0% 0%,#13213a 0,transparent 55%),var(--bg);background-attachment:fixed}
 a{color:#86b6ff;text-decoration:none}a:hover{text-decoration:underline}
 ::selection{background:var(--accentglow)}
 header{position:sticky;top:0;z-index:20;display:flex;gap:22px;align-items:center;padding:12px 22px;
   background:rgba(9,15,28,.78);backdrop-filter:saturate(140%) blur(10px);-webkit-backdrop-filter:saturate(140%) blur(10px);border-bottom:1px solid var(--line2)}
 header h1{font-size:15.5px;margin:0;color:#fff;display:flex;gap:10px;align-items:center;font-weight:700;letter-spacing:.2px}
 header h1 .logo{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:9px;background:linear-gradient(160deg,#3b82f6,#1e40af);box-shadow:0 4px 12px -4px var(--accentglow);font-size:15px}
 nav{display:flex;gap:4px;flex-wrap:wrap}
 nav a{color:var(--muted);font-size:14px;padding:7px 13px;border-radius:9px;font-weight:500;transition:color var(--t),background var(--t)}
 nav a:hover{color:#fff;background:#17243c;text-decoration:none}
 nav a.active{color:#fff;background:#1c2e4e;font-weight:600;box-shadow:inset 0 0 0 1px #2c4470,0 1px 0 rgba(255,255,255,.04)}
 .spacer{flex:1}
 .conn{font-size:12.5px;color:var(--muted);display:flex;align-items:center;white-space:nowrap;font-weight:500}
 .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:8px;position:relative}
 .dot.up{background:var(--good)}.dot.down{background:var(--bad)}.dot.idle{background:var(--muted2)}
 .dot.up::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:2px solid var(--good);opacity:.5;animation:ping 1.8s cubic-bezier(0,0,.2,1) infinite}
 @keyframes ping{0%{transform:scale(.6);opacity:.7}80%,100%{transform:scale(1.7);opacity:0}}
 main{max-width:980px;margin:26px auto;padding:0 18px 72px;animation:rise .35s ease both}
 @keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
 .card{position:relative;background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line2);border-radius:var(--r-lg);padding:20px 22px;margin-bottom:18px;box-shadow:var(--shadow)}
 .card::before{content:"";position:absolute;inset:0 0 auto 0;height:1px;border-radius:var(--r-lg) var(--r-lg) 0 0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.08),transparent)}
 .card h3{margin:0 0 4px;font-size:15px;letter-spacing:.2px}
 .eyebrow{text-transform:uppercase;letter-spacing:.13em;font-size:11px;color:var(--muted2);font-weight:700}
 .toprow{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 hr{border:0;border-top:1px solid var(--line2);margin:18px 0}
 #directive-card{overflow:hidden}
 #directive-card::after{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--muted2);transition:background var(--t)}
 #directive-card.state-inhibit::after{background:linear-gradient(var(--bad),var(--bad2))}
 #directive-card.state-allow::after{background:linear-gradient(var(--good),var(--good2))}
 #directive-card.state-unknown::after{background:var(--warn)}
 #directive-card.state-inhibit{background:linear-gradient(180deg,rgba(248,113,113,.07),var(--panel2))}
 #directive-card.state-allow{background:linear-gradient(180deg,rgba(52,211,153,.06),var(--panel2))}
 .big{font-size:34px;font-weight:800;margin:8px 0;letter-spacing:-.6px;line-height:1.1}
 .inhibit{color:var(--bad)}.allow{color:var(--good)}.unknown{color:var(--warn)}
 .table-wrap{overflow-x:auto;border-radius:var(--r);margin:0 -4px}
 table{width:100%;border-collapse:collapse;font-size:14px;min-width:520px}
 th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line)}
 tbody tr{transition:background var(--t)}tbody tr:hover{background:rgba(255,255,255,.022)}
 tbody tr:last-child td{border-bottom:0}
 th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.07em}
 td:nth-child(4){font-variant-numeric:tabular-nums}
 code{background:#0a1322;border:1px solid var(--line);border-radius:6px;padding:1.5px 6px;font-size:12.5px;font-family:ui-monospace,Menlo,Consolas,monospace}
 .pill{display:inline-flex;align-items:center;gap:6px;padding:3.5px 11px;border-radius:var(--r-pill);font-size:12px;font-weight:700;white-space:nowrap}
 .pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.9}
 .on{background:#3a1115;color:#fda4af;box-shadow:0 0 0 1px #7f1d1d inset}
 .off{background:#0f2e1c;color:#86efac;box-shadow:0 0 0 1px #14532d inset}
 .na{background:#1c2740;color:#cdd9ec;box-shadow:0 0 0 1px #2c3c5a inset}
 label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px;font-weight:600}
 .hint{font-weight:400;color:var(--muted2)}
 input,textarea,select{width:100%;background:#0a1322;color:var(--ink);border:1px solid var(--line);border-radius:var(--r);padding:10px 12px;font-size:14px;font-family:inherit;transition:border-color var(--t),box-shadow var(--t),background var(--t)}
 input::placeholder,textarea::placeholder{color:#5a6f90}
 input:hover,textarea:hover,select:hover{border-color:#33486a}
 input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:var(--ring)}
 select{appearance:none;-webkit-appearance:none;cursor:pointer;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);background-position:calc(100% - 18px) 17px,calc(100% - 13px) 17px;background-size:5px 5px,5px 5px;background-repeat:no-repeat;padding-right:34px}
 input.invalid{border-color:var(--bad);box-shadow:0 0 0 3px rgba(248,113,113,.16)}
 .field-err{color:#fda4af;font-size:12px;margin-top:5px;min-height:14px}
 textarea{min-height:340px;font-family:ui-monospace,Menlo,Consolas,monospace;line-height:1.55}
 .row{display:flex;gap:16px;flex-wrap:wrap}.row>div{flex:1;min-width:165px}
 button{background:linear-gradient(180deg,var(--accent),var(--accent2));color:#fff;border:0;border-radius:var(--r);padding:11px 20px;font-size:14px;font-weight:700;cursor:pointer;margin-top:18px;box-shadow:0 8px 18px -10px var(--accentglow);transition:filter var(--t),transform var(--t),box-shadow var(--t)}
 button:hover{filter:brightness(1.08)}button:active{transform:translateY(1px)}button:focus-visible{outline:none;box-shadow:var(--ring)}
 button.secondary{background:#1c2e4e;box-shadow:none;color:#dbe6f7}button.secondary:hover{background:#26395e;filter:none}
 button.danger{background:#3a1115;color:#fecaca;box-shadow:0 0 0 1px #7f1d1d inset}button.danger:hover{background:#511a20;filter:none}
 button.mini{padding:8px 13px;margin-top:0;font-size:13px}
 .btnrow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
 .btnrow button{margin-top:0}
 .msg{padding:12px 15px;border-radius:var(--r);margin-bottom:16px;font-size:14px;font-weight:600;display:flex;align-items:center;gap:9px}
 .ok{background:#0f2e1c;color:#bbf7d0;box-shadow:0 0 0 1px #14532d inset}
 .err{background:#3a1115;color:#fecaca;box-shadow:0 0 0 1px #7f1d1d inset}
 .muted{color:var(--muted2);font-size:12.5px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
 .metric{background:#0a1322;border:1px solid var(--line);border-radius:12px;padding:14px 15px;transition:transform var(--t),border-color var(--t)}
 .metric:hover{transform:translateY(-2px);border-color:#314865}
 .metric .v{font-size:23px;font-weight:700;letter-spacing:-.3px;font-variant-numeric:tabular-nums}
 .metric .k{color:var(--muted);font-size:12px;margin-top:4px}
 #dash.loading .metric .v,#dash.loading #directive{color:transparent;border-radius:8px;background:linear-gradient(90deg,#0e1a2d 25%,#192842 37%,#0e1a2d 63%);background-size:400% 100%;animation:shimmer 1.4s ease infinite}
 @keyframes shimmer{0%{background-position:100% 0}100%{background-position:-100% 0}}
 .tabs{display:inline-flex;gap:4px;margin:6px 0 16px;padding:4px;background:#0a1322;border:1px solid var(--line);border-radius:12px}
 .tab{margin:0;background:transparent;color:var(--muted);border:0;border-radius:9px;padding:8px 16px;font-weight:600;font-size:14px;cursor:pointer;box-shadow:none}
 .tab:hover{color:#fff;filter:none}
 .tab.active{background:#1c2e4e;color:#fff;box-shadow:inset 0 0 0 1px #2c4470}
 .rule-card{background:#0b1525;border:1px solid var(--line);border-radius:13px;padding:16px 18px;margin-bottom:14px}
 .rule-card .rhead{display:flex;justify-content:space-between;align-items:center;gap:10px}
 .rule-card .rhead .idx{font-size:11px;color:var(--muted2);font-weight:700;text-transform:uppercase;letter-spacing:.09em}
 .cond{align-items:flex-end}.cond .rm{flex:0 0 auto;min-width:0}.combine-wrap{margin-top:6px}
 footer{max-width:980px;margin:0 auto;padding:8px 18px 44px;color:var(--muted2);font-size:12.5px;text-align:center}
 @media (max-width:560px){header{gap:12px;padding:10px 14px}.conn{display:none}main{margin-top:16px}.big{font-size:28px}}
 @media (prefers-reduced-motion:reduce){*{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important;scroll-behavior:auto!important}}
</style></head><body>
<header>
 <h1><span class="logo">🏰</span> The Castle Fun Center <span style="font-weight:500;color:var(--muted2);font-size:12px;letter-spacing:.02em">· MQTT Command Center</span></h1>
 <nav>
  <a href="{{ url_for('dashboard') }}" class="{{ 'active' if page=='dash' }}">Dashboard</a>
  <a href="{{ url_for('settings') }}" class="{{ 'active' if page=='settings' }}">Settings</a>
  <a href="{{ url_for('rules') }}" class="{{ 'active' if page=='rules' }}">Rules</a>
  <a href="{{ url_for('inputs') }}" class="{{ 'active' if page=='inputs' }}">Inputs</a>
  <a href="{{ url_for('mqtt_page') }}" class="{{ 'active' if page=='mqtt' }}">MQTT</a>
  <a href="{{ url_for('activity') }}" class="{{ 'active' if page=='activity' }}">Activity</a>
  <a href="{{ url_for('history') }}" class="{{ 'active' if page=='history' }}">History</a>
  <a href="{{ url_for('system') }}" class="{{ 'active' if page=='system' }}">System</a>
 </nav>
 <span class="spacer"></span>
 <span class="conn" id="connstate"><span class="dot idle"></span>weather-mqtt</span>
</header>
<main>
 {% if msg %}<div class="msg {{ msgclass }}">{{ msg }}</div>{% endif %}
 {{ body|safe }}
</main>
<footer>The Castle Fun Center · MQTT Command Center · data source: National Weather Service (api.weather.gov)
 · <a href="https://github.com/SethMorrowSoftware/mqtt-dev" target="_blank" rel="noopener">Docs ↗</a></footer>
</body></html>
"""


def page(body, **kw):
    kw.setdefault("favicon", FAVICON)
    return render_template_string(BASE, body=body, **kw)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASH = """
<div id="dash" class="loading" data-state-file="{{ state_file }}">
  <div class="card" id="getstarted" style="display:none">
    <h3 style="margin:0 0 6px">Getting started</h3>
    <p class="muted" style="margin:0 0 8px">No data yet — the monitor hasn't written a status snapshot.
     Three steps to go live:</p>
    <ol class="muted" style="margin:0;padding-left:20px;line-height:1.7">
      <li>Set your location &amp; NWS contact on the <a href="{{ url_for('settings') }}">Settings</a> page.</li>
      <li>Tune your devices on the <a href="{{ url_for('rules') }}">Rules</a> page.</li>
      <li>Start the monitor service — it writes a snapshot each poll cycle and this page fills in.</li>
    </ol>
  </div>

  <div class="card" id="directive-card">
    <div class="eyebrow">Headline device</div>
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

  <div class="card" id="vars-card" style="display:none">
    <div class="eyebrow" style="margin-bottom:10px">Variables</div>
    <div class="grid" id="vars-body"></div>
  </div>

  <div class="card">
    <div class="toprow" style="align-items:center">
      <div class="eyebrow">Devices</div>
      <div class="muted" style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
        <span class="pill on">active</span><span class="pill off">clear</span>
        <span class="pill na">disabled / n/a</span>
      </div>
    </div>
    <p class="muted" id="manual-hint" style="display:none;margin:8px 0 0">💡 Manual control is off — enable it
     under <a href="{{ url_for('settings') }}">Settings → Web interface</a> (a login is required) to add
     <b>Auto / On / Off</b> buttons to each device.</p>
    <div class="grid" id="devicegrid" style="grid-template-columns:repeat(auto-fill,minmax(230px,1fr));margin-top:12px">
      <div class="muted">Loading…</div>
    </div>
  </div>
  <p class="muted">Live — updates every {{ refresh }}s. <span id="staleness"></span></p>
</div>

<script>
const REFRESH = {{ refresh }} * 1000;
const fmt = v => v === null || v === undefined ? "—" : (v === true ? "yes" : (v === false ? "no" : v));
function setText(id, t){ const e=document.getElementById(id); if(e) e.textContent=t; }
function agoText(iso){
  if(!iso) return "—";
  const t=Date.parse(iso); if(isNaN(t)) return iso;
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<5) return "just now"; if(s<60) return s+"s ago";
  if(s<3600) return Math.round(s/60)+"m ago"; return Math.round(s/3600)+"h ago";
}

function render(s){
  const conn = document.getElementById("connstate");
  const card = document.getElementById("directive-card");
  const gs = document.getElementById("getstarted");
  if(!s){
    document.getElementById("directive").className = "big unknown";
    if(card) card.className = "card state-unknown";
    setText("directive","NO DATA");
    setText("directive-sub","No snapshot yet — see Getting started above.");
    conn.innerHTML = '<span class="dot idle"></span>no monitor data';
    if(gs) gs.style.display = "";
    const grid = document.getElementById("devicegrid");
    if(grid) grid.innerHTML = '<div class="muted">Waiting on the monitor…</div>';
    document.getElementById("dash").classList.remove("loading");
    return;
  }
  if(gs) gs.style.display = "none";
  // connection badge
  const up = !!s.mqtt_connected;
  conn.innerHTML = '<span class="dot '+(up?'up':'down')+'"></span>MQTT '+(up?'connected':'offline');

  // Headline device: prefer the irrigation rule (back-compat), else the first
  // rule with a known state, else just the first rule. This way a renamed first
  // rule still drives the hero instead of leaving it stuck on UNKNOWN.
  const rules = s.rules || [];
  const irr = rules.find(r => r.enabled !== false && /irrigation|rain_inhibit/.test(r.name || ""))
           || rules.find(r => r.enabled !== false && r.active !== null && r.active !== undefined)
           || rules[0];
  const d = document.getElementById("directive");
  let st = "unknown";
  if(irr && irr.active !== null && irr.active !== undefined){
    const isIrr = /irrigation|rain_inhibit/.test(irr.name || "");
    st = irr.active ? "inhibit" : "allow";
    d.className = "big " + st;
    const suffix = isIrr ? (irr.active ? " — do NOT water" : " — watering allowed")
                         : (irr.active ? " — active" : " — clear");
    setText("directive", (irr.current_payload ?? "?") + suffix);
    setText("directive-sub", "topic " + irr.topic + (irr.last_change ? " · changed " + agoText(irr.last_change) : ""));
  } else {
    d.className = "big unknown";
    setText("directive","UNKNOWN");
    setText("directive-sub", irr ? "Waiting on data…" : "No rules configured.");
  }
  if(card) card.className = "card state-" + st;

  const m = s.metrics || {};
  const up2=document.getElementById("updated");
  if(up2){ up2.textContent = "updated " + agoText(s.updated); up2.title = s.updated || ""; }
  setText("m_rain", fmt(m.is_raining));
  setText("m_accum", fmt(m.precip_accum_in) + " in");
  setText("m_accum_k", "rain last " + (s.lookback_hours ?? "?") + "h");
  setText("m_prob", fmt(m.precipitation_probability) + "%");
  setText("m_temp", fmt(m.temperature) + "°F");
  setText("m_hum", fmt(m.humidity) + "%");
  setText("m_wind", fmt(m.wind_speed_mph));
  const alerts = (m.active_alerts && m.active_alerts.length) ? m.active_alerts.join(", ") : "none";
  setText("forecast", (m.short_forecast || "—") + " · alerts: " + alerts);

  const manualControl = !!s.manual_control;
  const hint = document.getElementById("manual-hint");
  if(hint) hint.style.display = manualControl ? "none" : "";
  const grid = document.getElementById("devicegrid");
  grid.innerHTML = "";
  for(const r of rules){
    let pill;
    if(r.enabled === false) pill = '<span class="pill na">disabled</span>';
    else if(r.active === null || r.active === undefined) pill = '<span class="pill na">n/a</span>';
    else if(r.active) pill = '<span class="pill on">active</span>';
    else pill = '<span class="pill off">clear</span>';
    if(r.manual && r.manual !== "auto") pill += ' <span class="pill na">manual '+esc(r.manual)+'</span>';
    const cell = document.createElement("div");
    cell.className = "metric"; cell.style.cssText = "display:flex;flex-direction:column;gap:6px";
    let html = '<div class="toprow" style="align-items:center"><strong>'+esc(r.name)+'</strong><span>'+pill+'</span></div>';
    if(r.description) html += '<div class="muted" style="font-size:12px">'+esc(r.description)+'</div>';
    html += '<div class="muted" style="font-size:12px">topic <code>'+esc(r.topic)+'</code></div>';
    html += '<div class="muted" style="font-size:12px">payload '+(r.current_payload!=null?esc(r.current_payload):"—")+
            ' · changed '+esc(agoText(r.last_change))+'</div>';
    if(manualControl && r.enabled !== false) html += ctlButtons(r);
    cell.innerHTML = html;
    grid.appendChild(cell);
  }
  if(!rules.length) grid.innerHTML = '<div class="muted">No devices configured yet — add rules on the Rules page.</div>';
  renderVars(s.variables || [], manualControl);
  document.getElementById("dash").classList.remove("loading");
}
function esc(s){ const d=document.createElement("div"); d.textContent=String(s); return d.innerHTML; }
function renderVars(vars, manualControl){
  const card = document.getElementById("vars-card");
  const box = document.getElementById("vars-body");
  if(!card || !box) return;
  if(!vars.length){ card.style.display = "none"; return; }
  card.style.display = "";
  box.innerHTML = "";
  for(const v of vars){
    let ctrl;
    if(!manualControl){
      ctrl = '<span class="v">'+esc(fmt(v.value))+'</span>';
    } else if(v.type === "bool"){
      const on = v.value === true;
      ctrl = '<button type="button" class="mini '+(on?"":"secondary")+'" data-var="'+esc(v.name)+
        '" data-next="'+(on?"false":"true")+'" style="margin:0">'+(on?"ON":"OFF")+'</button>';
    } else {
      ctrl = '<input class="var-num" data-var="'+esc(v.name)+'" type="number" step="any" value="'+
        (v.value!=null?esc(v.value):"")+'" style="width:120px;margin:0">';
    }
    const cell = document.createElement("div"); cell.className = "metric";
    cell.style.cssText = "display:flex;justify-content:space-between;align-items:center;gap:10px";
    cell.innerHTML = '<div class="k">'+esc(v.name)+'</div><div>'+ctrl+'</div>';
    box.appendChild(cell);
  }
}
function setVar(name, value){
  fetch("api/variable", {method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name, value})})
    .then(async r=>{ if(!r.ok){ const j=await r.json().catch(()=>({})); alert(j.error||("variable failed ("+r.status+")")); } tick(); })
    .catch(()=>{ alert("variable request failed"); });
}
document.getElementById("vars-body").addEventListener("click", e=>{
  const b = e.target.closest("button[data-var]"); if(!b) return;
  b.disabled = true; setVar(b.getAttribute("data-var"), b.getAttribute("data-next"));
});
document.getElementById("vars-body").addEventListener("change", e=>{
  const i = e.target.closest("input.var-num[data-var]"); if(!i) return;
  setVar(i.getAttribute("data-var"), i.value);
});
function ctlButtons(r){
  const cur = r.manual || "auto";
  const mk = (st,lbl)=> '<button type="button" class="mini '+(cur===st?"":"secondary")+
    '" data-state="'+st+'" style="margin:0;padding:5px 11px;font-size:12px">'+lbl+'</button>';
  return '<div class="ctl" data-device="'+esc(r.name)+
    '" style="display:flex;gap:5px;margin-top:8px">'+mk("auto","Auto")+mk("on","On")+mk("off","Off")+'</div>';
}
async function setManual(device, state){
  try{
    const r = await fetch("api/control", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({device, state})});
    if(!r.ok){ const j=await r.json().catch(()=>({})); alert(j.error || ("control failed ("+r.status+")")); }
  }catch(e){ alert("control request failed"); }
  tick();
}
document.getElementById("devicegrid").addEventListener("click", e=>{
  const b = e.target.closest("button[data-state]"); if(!b) return;
  const wrap = b.closest(".ctl"); if(!wrap) return;
  b.disabled = true;
  setManual(wrap.getAttribute("data-device"), b.getAttribute("data-state"));
});

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
    try:
        cfg = load_raw()
    except Exception as e:
        return _config_error_page("dash", e)
    body = render_template_string(
        DASH, refresh=DASH_REFRESH_SECONDS,
        state_file=cfg.get("state_file", "weather_state.json"))
    return page(body, page="dash", title="Dashboard · The Castle Fun Center")


@app.route("/api/state")
@require_auth
def api_state():
    """JSON snapshot the dashboard polls. 503 (not 500) when no data yet so the
    client can show a friendly 'waiting on monitor' state."""
    try:
        state = load_state(load_raw())
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    if state is None:
        return jsonify({"error": "no state yet"}), 503
    return jsonify(state)


@app.route("/api/control", methods=["POST"])
@require_auth
def api_control():
    """Set a device's manual override (auto|on|off). Fails closed: only works
    when web.allow_manual_control is on AND a web login is configured (so this
    control surface is always authenticated). Persists to overrides.json and
    appends to the audit log with the authenticated user."""
    try:
        cfg = load_raw()
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    web = cfg.get("web", {}) or {}
    user = str(web.get("username") or "")
    pw = str(web.get("password") or "")
    if not (bool(web.get("allow_manual_control", False)) and user and pw):
        return jsonify({"error": "manual control is disabled "
                        "(enable it and set a web login in Settings)"}), 403

    data = request.get_json(silent=True) or request.form
    device = str(data.get("device", "")).strip()
    state = str(data.get("state", "")).strip().lower()
    names = {str(r.get("name")) for r in (cfg.get("rules") or []) if isinstance(r, dict)}
    if device not in names:
        return jsonify({"error": f"unknown device '{device}'"}), 404
    if state not in core.MANUAL_STATES:
        return jsonify({"error": f"state must be one of {core.MANUAL_STATES}"}), 400

    try:
        core.set_override(cfg.get("overrides_file", "overrides.json"), device, state)
    except Exception as e:
        return jsonify({"error": f"could not save override: {e}"}), 500
    auth = request.authorization
    core.audit(cfg.get("audit_file", "audit.log"), device=device,
               action="manual_set", state=state,
               by=(auth.username if auth and auth.username else user or "local"))
    return jsonify({"ok": True, "device": device, "manual": state})


@app.route("/api/variable", methods=["POST"])
@require_auth
def api_variable():
    """Set an operator variable's value. Same fail-closed gating as /api/control:
    manual control must be enabled and a web login configured. Persists to
    variables.json and audits with the authenticated user."""
    try:
        cfg = load_raw()
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    web = cfg.get("web", {}) or {}
    user = str(web.get("username") or "")
    pw = str(web.get("password") or "")
    if not (bool(web.get("allow_manual_control", False)) and user and pw):
        return jsonify({"error": "manual control is disabled "
                        "(enable it and set a web login in Settings)"}), 403
    try:
        declared = core._validate_variables(cfg.get("variables") or {})
    except Exception:
        declared = {}
    data = request.get_json(silent=True) or request.form
    name = str(data.get("name", "")).strip()
    if name not in declared:
        return jsonify({"error": f"unknown variable '{name}'"}), 404
    try:
        coerced = core.set_variable(cfg.get("variables_file", "variables.json"),
                                    name, data.get("value"), declared)
    except Exception as e:
        return jsonify({"error": f"could not save variable: {e}"}), 500
    auth = request.authorization
    core.audit(cfg.get("audit_file", "audit.log"), variable=name,
               action="variable_set", value=coerced,
               by=(auth.username if auth and auth.username else user or "local"))
    return jsonify({"ok": True, "variable": name, "value": coerced})


@app.route("/api/audit")
@require_auth
def api_audit():
    """Recent audit events (newest first) for the Activity page."""
    try:
        cfg = load_raw()
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    return jsonify({"events": core.read_audit(cfg.get("audit_file", "audit.log"), 200)})


def _system_snapshot():
    """Assemble the System page's health + config summary. Never raises -- on a
    bad config it reports config_ok=False with a generic message."""
    from datetime import datetime, timezone
    out = {"monitor": "unknown", "config_ok": False, "mqtt_connected": None,
           "last_update": None, "age_seconds": None}
    try:
        cfg = load_raw()
    except Exception:
        out["error"] = "config invalid or unreadable"
        return out
    try:
        core_check(cfg)
        out["config_ok"] = True
    except Exception:
        out["error"] = "config invalid"

    poll = 15
    try:
        poll = max(1, int(cfg.get("poll_interval_minutes", 15)))
    except Exception:
        pass
    # Two missed cycles plus a small grace before we call the monitor stale.
    stale_after = poll * 60 * 2 + 90
    out["poll_interval_minutes"] = poll
    out["stale_after_seconds"] = stale_after

    state = load_state(cfg)
    if state and state.get("updated"):
        out["last_update"] = state["updated"]
        out["mqtt_connected"] = bool(state.get("mqtt_connected"))
        out["manual_control"] = bool(state.get("manual_control"))
        try:
            upd = datetime.fromisoformat(state["updated"])
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - upd).total_seconds()
            out["age_seconds"] = int(age)
            out["monitor"] = "ok" if age <= stale_after else "stale"
        except Exception:
            out["monitor"] = "ok"
    else:
        out["monitor"] = "no_data"

    rules = [r for r in (cfg.get("rules") or []) if isinstance(r, dict)]
    try:
        metrics = len(core.metric_catalogue(cfg))
    except Exception:
        metrics = None
    out["summary"] = {
        "rules_total": len(rules),
        "rules_enabled": sum(1 for r in rules if r.get("enabled", True)),
        "variables": len(cfg.get("variables") or {}),
        "mqtt_inputs": len(cfg.get("mqtt_inputs") or []),
        "http_inputs": len(cfg.get("http_inputs") or []),
        "metrics": metrics,
    }
    log_file = cfg.get("log_file") or ""
    out["files"] = {
        "config": CONFIG_PATH,
        "state": cfg.get("state_file", "weather_state.json"),
        "audit": cfg.get("audit_file", "audit.log"),
        "log": log_file or None,
    }
    out["log_enabled"] = bool(log_file)
    out["log_present"] = bool(log_file) and Path(log_file).exists()
    return out


@app.route("/api/system")
@require_auth
def api_system():
    """Health + config summary for the System page (auto-refreshed)."""
    return jsonify(_system_snapshot())


@app.route("/api/logs")
@require_auth
def api_logs():
    """Tail the monitor's runtime log (newest first). Empty when no log_file is
    configured or the file doesn't exist yet."""
    try:
        cfg = load_raw()
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    log_file = cfg.get("log_file") or ""
    try:
        limit = max(1, min(1000, int(request.args.get("limit", 300))))
    except Exception:
        limit = 300
    return jsonify({"enabled": bool(log_file),
                    "lines": core.read_log(log_file, limit) if log_file else []})


@app.route("/api/history")
@require_auth
def api_history():
    """Metric history (time series) over the last N hours for the History page."""
    try:
        cfg = load_raw()
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    hist = cfg.get("history", {}) or {}
    enabled = bool(hist.get("enabled", True))
    db = hist.get("file", "history.db")
    try:
        hours = max(1, min(24 * 90, int(request.args.get("hours", 24))))
    except Exception:
        hours = 24
    names = [n for n in (request.args.get("metrics", "").split(",")) if n.strip()]
    available = core.history_metrics(db) if enabled else []
    series = core.read_history(db, hours=hours, names=names or None) if enabled else {}
    return jsonify({"enabled": enabled, "hours": hours,
                    "available": available, "series": series})


@app.route("/api/mqtt")
@require_auth
def api_mqtt():
    """Live MQTT console feed: recent messages (optionally since a seq / filtered
    by topic prefix), a per-topic latest-value summary, and connection stats."""
    try:
        cfg = load_raw()
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    web = cfg.get("web", {}) or {}
    try:
        since = int(request.args.get("since", 0))
    except Exception:
        since = 0
    try:
        limit = max(1, min(1000, int(request.args.get("limit", 300))))
    except Exception:
        limit = 300
    topic = request.args.get("topic", "")
    want_topics = request.args.get("topics") == "1"
    out = {"enabled": bool(web.get("mqtt_console_enabled", True)),
           "can_publish": bool(web.get("allow_mqtt_publish", False)
                               and web.get("username") and web.get("password")),
           "stats": console.stats(),
           "messages": console.messages(since=since, topic=topic, limit=limit)}
    if want_topics:
        out["topic_list"] = console.topics()
    return jsonify(out)


@app.route("/api/mqtt/publish", methods=["POST"])
@require_auth
def api_mqtt_publish():
    """Publish an arbitrary message. Fail-closed like /api/control: requires
    web.allow_mqtt_publish AND a configured web login. Audited."""
    try:
        cfg = load_raw()
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    web = cfg.get("web", {}) or {}
    user = str(web.get("username") or "")
    pw = str(web.get("password") or "")
    if not (bool(web.get("allow_mqtt_publish", False)) and user and pw):
        return jsonify({"error": "MQTT publishing is disabled "
                        "(enable it and set a web login in Settings)"}), 403
    data = request.get_json(silent=True) or request.form
    topic = str(data.get("topic", "")).strip()
    payload = data.get("payload", "")
    if payload is None:
        payload = ""
    try:
        qos = int(data.get("qos", 0))
    except Exception:
        qos = 0
    if qos not in (0, 1, 2):
        qos = 0
    retain = str(data.get("retain", "")).strip().lower() in ("1", "true", "yes", "on") \
        or data.get("retain") is True
    ok, err = console.publish(topic, payload, qos=qos, retain=retain)
    if not ok:
        return jsonify({"error": err or "publish failed"}), 400
    auth = request.authorization
    core.audit(cfg.get("audit_file", "audit.log"), action="mqtt_publish",
               topic=topic, qos=qos, retain=retain,
               by=(auth.username if auth and auth.username else user or "local"))
    return jsonify({"ok": True, "topic": topic, "qos": qos, "retain": retain})


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
    except Exception:
        # Unauthenticated endpoint: report the failure without leaking config
        # values/paths in the message.
        out["error"] = "config invalid or unreadable"
    code = 200 if out["config_ok"] else 500
    return jsonify(out), code


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


# ---------------------------------------------------------------------------
# Activity (audit log viewer)
# ---------------------------------------------------------------------------
ACTIVITY = """
<div class="card">
  <div class="toprow">
    <div><h3 style="margin:0">Activity</h3>
      <p class="muted" style="margin:4px 0 0">Every device state change (automatic or manual) and operator
       action, newest first. Read-only.</p></div>
    <div class="muted" id="act-count">—</div>
  </div>
  <div class="table-wrap" style="margin-top:12px">
    <table>
      <thead><tr><th>When</th><th>What</th><th>Action</th><th>Detail</th><th>By</th></tr></thead>
      <tbody id="actbody"><tr><td colspan="5" class="muted">Loading…</td></tr></tbody>
    </table>
  </div>
  <p class="muted" style="margin-top:12px">Refreshes every {{ refresh }}s · source: <code>audit.log</code>.
   Activity is recorded only when a device's committed state changes or an operator acts.</p>
</div>
<script>
const REFRESH = {{ refresh }} * 1000;
function esc(s){ const d=document.createElement("div"); d.textContent=String(s); return d.innerHTML; }
function agoText(iso){
  if(!iso) return "—"; const t=Date.parse(iso); if(isNaN(t)) return iso;
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<5) return "just now"; if(s<60) return s+"s ago";
  if(s<3600) return Math.round(s/60)+"m ago"; return Math.round(s/3600)+"h ago";
}
function describe(e){
  // Normalize the monitor's and the web UI's event shapes into one readable row.
  if(e.action==="manual_set")   return {what:e.device, action:"manual override", detail:String(e.state).toUpperCase()};
  if(e.action==="variable_set") return {what:e.variable, action:"variable set", detail:String(e.value)};
  if(e.action==="mqtt_publish") return {what:e.topic, action:"manual publish", detail:"qos "+e.qos+(e.retain?" · retain":"")};
  if(e.action==="action_fired"){
    const tgt = e.kind==="notify" ? "Slack" : (e.target||"");
    return {what:e.device, action:e.kind+" action"+(e.ok===false?" (failed)":""),
            detail:"on "+(e.trigger||"")+(tgt?" → "+tgt:"")};
  }
  const src = e.source==="manual" ? "manual" : "automatic";
  return {what:e.device, action:src+" state change", detail:String(e.state).toUpperCase()};
}
function pillFor(d){
  if(d.action==="manual override") return '<span class="pill on">'+esc(d.action)+'</span>';
  if(d.action==="variable set")    return '<span class="pill na">'+esc(d.action)+'</span>';
  if(d.action==="manual publish")  return '<span class="pill on">'+esc(d.action)+'</span>';
  if(/\(failed\)/.test(d.action))  return '<span class="pill na">'+esc(d.action)+'</span>';
  if(/ action$/.test(d.action))    return '<span class="pill on">'+esc(d.action)+'</span>';
  if(/^manual/.test(d.action))     return '<span class="pill on">'+esc(d.action)+'</span>';
  return '<span class="pill off">'+esc(d.action)+'</span>';
}
async function tick(){
  let events=[];
  try{ const r=await fetch("api/audit",{cache:"no-store"}); if(r.ok) events=(await r.json()).events||[]; }
  catch(e){ /* keep last render */ return; }
  const tb=document.getElementById("actbody");
  document.getElementById("act-count").textContent = events.length ? (events.length+" recent") : "";
  if(!events.length){ tb.innerHTML='<tr><td colspan="5" class="muted">No activity yet — changes appear here as devices switch or an operator acts.</td></tr>'; return; }
  tb.innerHTML="";
  for(const e of events){
    const d=describe(e); const tr=document.createElement("tr");
    tr.innerHTML='<td class="muted" title="'+esc(e.ts||"")+'">'+esc(agoText(e.ts))+'</td>'+
      '<td>'+esc(d.what||"—")+'</td><td>'+pillFor(d)+'</td>'+
      '<td>'+esc(d.detail||"—")+'</td><td class="muted">'+esc(e.by||"—")+'</td>';
    tb.appendChild(tr);
  }
}
tick(); setInterval(tick, REFRESH);
</script>
"""


@app.route("/activity")
@require_auth
def activity():
    return page(render_template_string(ACTIVITY, refresh=DASH_REFRESH_SECONDS),
                page="activity", title="Activity · The Castle Fun Center")


# ---------------------------------------------------------------------------
# System (health + live runtime log)
# ---------------------------------------------------------------------------
SYSTEM = """
<div class="card">
  <div class="toprow" style="align-items:center">
    <div><h3 style="margin:0">System</h3>
      <p class="muted" style="margin:4px 0 0">Live health of the monitor and web UI, a snapshot of your
       configuration, and the controller's runtime log.</p></div>
    <span class="conn" id="sys-conn"><span class="dot idle"></span>checking…</span>
  </div>
  <div class="grid" id="health" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr));margin-top:14px">
    <div class="metric"><div class="v" id="h_monitor">—</div><div class="k">monitor</div></div>
    <div class="metric"><div class="v" id="h_mqtt">—</div><div class="k">MQTT broker</div></div>
    <div class="metric"><div class="v" id="h_config">—</div><div class="k">config</div></div>
    <div class="metric"><div class="v" id="h_update">—</div><div class="k">last poll</div></div>
  </div>
</div>

<div class="card">
  <div class="eyebrow" style="margin-bottom:10px">Configuration summary</div>
  <div class="grid" id="summary" style="grid-template-columns:repeat(auto-fill,minmax(150px,1fr))">
    <div class="metric"><div class="v" id="s_rules">—</div><div class="k">rules (enabled)</div></div>
    <div class="metric"><div class="v" id="s_metrics">—</div><div class="k">metrics available</div></div>
    <div class="metric"><div class="v" id="s_vars">—</div><div class="k">variables</div></div>
    <div class="metric"><div class="v" id="s_mqtt">—</div><div class="k">MQTT inputs</div></div>
    <div class="metric"><div class="v" id="s_http">—</div><div class="k">HTTP inputs</div></div>
  </div>
  <p class="muted" id="files" style="margin-top:14px">—</p>
</div>

<div class="card">
  <div class="toprow" style="align-items:center">
    <div><h3 style="margin:0">Runtime log</h3>
      <p class="muted" style="margin:4px 0 0">The monitor's recent log, newest first.</p></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <select id="lvl" style="margin:0;width:auto">
        <option value="">all levels</option>
        <option value="INFO">info & up</option>
        <option value="WARNING">warnings & up</option>
        <option value="ERROR">errors only</option>
      </select>
      <label class="muted" style="margin:0;display:flex;align-items:center;gap:6px;font-weight:500">
        <input type="checkbox" id="follow" checked style="width:auto;margin:0"> auto-refresh</label>
    </div>
  </div>
  <div id="lognote" class="muted" style="margin-top:10px;display:none"></div>
  <pre id="logbox" style="margin-top:12px;max-height:460px;overflow:auto;background:#0a1322;border:1px solid var(--line);
   border-radius:10px;padding:14px 16px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;
   line-height:1.6;white-space:pre-wrap;word-break:break-word">Loading…</pre>
  <p class="muted" style="margin-top:10px">Source: <code id="logpath">log_file</code> · rolls at ~1 MB (3 backups).
   Configure it as <code>log_file:</code> in <code>config.yaml</code>.</p>
</div>
<script>
const REFRESH = {{ refresh }} * 1000;
function esc(s){ const d=document.createElement("div"); d.textContent=String(s); return d.innerHTML; }
function setText(id,v){ const e=document.getElementById(id); if(e) e.textContent=v; }
function agoText(iso){
  if(!iso) return "never"; const t=Date.parse(iso); if(isNaN(t)) return iso;
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<5) return "just now"; if(s<60) return s+"s ago";
  if(s<3600) return Math.round(s/60)+"m ago"; return Math.round(s/3600)+"h ago";
}
const LEVEL_RANK = {DEBUG:0, INFO:1, WARNING:2, ERROR:3, CRITICAL:4};

async function tickHealth(){
  let d; try{ const r=await fetch("api/system",{cache:"no-store"}); if(!r.ok) return; d=await r.json(); }
  catch(e){ return; }
  const conn=document.getElementById("sys-conn");
  const mon=d.monitor;
  const dot = mon==="ok" ? "up" : (mon==="stale"||mon==="no_data" ? "down" : "idle");
  conn.innerHTML='<span class="dot '+dot+'"></span>'+(mon==="ok"?"healthy":mon==="stale"?"monitor stale":mon==="no_data"?"no data yet":"unknown");
  const monLabel = {ok:"running", stale:"stale", no_data:"no data", unknown:"unknown"}[mon]||mon;
  setText("h_monitor", monLabel);
  document.getElementById("h_monitor").className = "v " + (mon==="ok"?"allow":mon==="unknown"?"unknown":"inhibit");
  const mq = d.mqtt_connected;
  setText("h_mqtt", mq===null||mq===undefined ? "—" : (mq?"connected":"offline"));
  document.getElementById("h_mqtt").className = "v " + (mq?"allow":mq===false?"inhibit":"unknown");
  setText("h_config", d.config_ok ? "valid" : "invalid");
  document.getElementById("h_config").className = "v " + (d.config_ok?"allow":"inhibit");
  setText("h_update", agoText(d.last_update));
  document.getElementById("h_update").title = d.last_update || "";

  const s=d.summary||{};
  setText("s_rules", (s.rules_enabled!=null?s.rules_enabled:"—")+" / "+(s.rules_total!=null?s.rules_total:"—"));
  setText("s_metrics", s.metrics!=null?s.metrics:"—");
  setText("s_vars", s.variables!=null?s.variables:"—");
  setText("s_mqtt", s.mqtt_inputs!=null?s.mqtt_inputs:"—");
  setText("s_http", s.http_inputs!=null?s.http_inputs:"—");
  const f=d.files||{};
  setText("files", "config "+(f.config||"—")+" · state "+(f.state||"—")+" · audit "+(f.audit||"—")+" · log "+(f.log||"(off)"));
  setText("logpath", f.log || "log_file (not configured)");
}

let LINES=[];
function renderLog(){
  const min = LEVEL_RANK[document.getElementById("lvl").value];
  const box=document.getElementById("logbox");
  const rows = (min==null) ? LINES : LINES.filter(l => (LEVEL_RANK[l.level]==null) || LEVEL_RANK[l.level]>=min);
  if(!rows.length){ box.textContent = LINES.length ? "No lines at this level." : "No log lines yet."; return; }
  box.innerHTML = rows.map(l=>{
    const lv = l.level || "";
    const color = lv==="ERROR"||lv==="CRITICAL" ? "var(--bad)" : lv==="WARNING" ? "var(--warn)" : lv==="DEBUG" ? "var(--muted2)" : "var(--good)";
    const tag = lv ? '<span style="color:'+color+';font-weight:700">'+esc(lv.padEnd(7))+'</span> ' : '';
    const ts = l.ts ? '<span style="color:var(--muted2)">'+esc(l.ts)+'</span> ' : '';
    return ts+tag+esc(l.msg||l.raw||"");
  }).join("\\n");
}
async function tickLog(){
  let d; try{ const r=await fetch("api/logs?limit=400",{cache:"no-store"}); if(!r.ok) return; d=await r.json(); }
  catch(e){ return; }
  const note=document.getElementById("lognote");
  if(!d.enabled){
    note.style.display=""; note.innerHTML='Runtime log is off. Set <code>log_file: monitor.log</code> in <code>config.yaml</code> and restart the monitor to capture it here.';
    LINES=[]; renderLog(); return;
  }
  note.style.display="none";
  LINES = d.lines || [];
  if(!LINES.length){ document.getElementById("logbox").textContent = "No log lines yet — the monitor writes here as it polls."; return; }
  renderLog();
}
document.getElementById("lvl").addEventListener("change", renderLog);

function tick(){ tickHealth(); if(document.getElementById("follow").checked) tickLog(); }
tickHealth(); tickLog();
setInterval(tick, REFRESH);
</script>
"""


@app.route("/system")
@require_auth
def system():
    return page(render_template_string(SYSTEM, refresh=DASH_REFRESH_SECONDS),
                page="system", title="System · The Castle Fun Center")


# ---------------------------------------------------------------------------
# History (metric trend sparklines from the SQLite log)
# ---------------------------------------------------------------------------
HISTORY = """
<div class="card">
  <div class="toprow" style="align-items:center">
    <div><h3 style="margin:0">History &amp; trends</h3>
      <p class="muted" style="margin:4px 0 0">How each numeric metric has moved over time, sampled every poll
       cycle. Use it to tune thresholds and spot drift.</p></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <select id="win" style="margin:0;width:auto">
        <option value="6">last 6 hours</option>
        <option value="24" selected>last 24 hours</option>
        <option value="72">last 3 days</option>
        <option value="168">last 7 days</option>
        <option value="720">last 30 days</option>
      </select>
      <label class="muted" style="margin:0;display:flex;align-items:center;gap:6px;font-weight:500">
        <input type="checkbox" id="follow" checked style="width:auto;margin:0"> auto-refresh</label>
      <button type="button" class="secondary mini" id="export" style="margin:0">Export CSV</button>
    </div>
  </div>
  <div id="hist-note" class="muted" style="margin-top:10px;display:none"></div>
</div>

<div class="grid" id="charts" style="grid-template-columns:repeat(auto-fill,minmax(280px,1fr))">
  <div class="muted">Loading…</div>
</div>
<script>
const REFRESH = {{ refresh }} * 1000;
function esc(s){ const d=document.createElement("div"); d.textContent=String(s); return d.innerHTML; }
function fmtNum(n){ if(n===null||n===undefined) return "—"; const r=Math.round(n*100)/100; return (r===Math.round(r))?String(r):r.toFixed(2); }
function fmtTime(iso){ const t=Date.parse(iso); if(isNaN(t)) return ""; const d=new Date(t);
  return d.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"}); }

// Build an inline SVG sparkline from [[ts,value],...].
function sparkline(points){
  const W=260, H=64, pad=4;
  if(!points.length) return '<div class="muted" style="height:'+H+'px;display:flex;align-items:center">no data</div>';
  const vals=points.map(p=>p[1]); let lo=Math.min(...vals), hi=Math.max(...vals);
  if(lo===hi){ lo-=1; hi+=1; }
  const n=points.length;
  const x=i=> pad + (n===1?0:(i/(n-1))*(W-2*pad));
  const y=v=> pad + (1-(v-lo)/(hi-lo))*(H-2*pad);
  let d=""; points.forEach((p,i)=>{ d += (i?" L":"M")+x(i).toFixed(1)+" "+y(p[1]).toFixed(1); });
  const area = d + " L"+x(n-1).toFixed(1)+" "+(H-pad)+" L"+x(0).toFixed(1)+" "+(H-pad)+" Z";
  const lastY=y(points[n-1][1]).toFixed(1), lastX=x(n-1).toFixed(1);
  return '<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="'+H+'" preserveAspectRatio="none" style="display:block">'+
    '<path d="'+area+'" fill="var(--accentglow)" stroke="none"/>'+
    '<path d="'+d+'" fill="none" stroke="var(--accent)" stroke-width="1.6"/>'+
    '<circle cx="'+lastX+'" cy="'+lastY+'" r="2.6" fill="var(--accent)"/></svg>';
}

let LAST = {series:{}, hours:24};
async function tick(){
  const hours=document.getElementById("win").value;
  let d; try{ const r=await fetch("api/history?hours="+hours,{cache:"no-store"}); if(!r.ok) return; d=await r.json(); }
  catch(e){ return; }
  LAST = {series:d.series||{}, hours:hours};
  const note=document.getElementById("hist-note");
  const charts=document.getElementById("charts");
  if(!d.enabled){
    note.style.display=""; note.innerHTML='History is off. Set <code>history.enabled: true</code> in <code>config.yaml</code> (or Settings) and let the monitor run a few cycles.';
    charts.innerHTML=""; return;
  }
  const names=Object.keys(d.series||{}).sort();
  if(!names.length){
    note.style.display=""; note.textContent="No samples yet — the monitor writes a point each poll cycle; check back after a few cycles.";
    charts.innerHTML=""; return;
  }
  note.style.display="none";
  charts.innerHTML="";
  for(const name of names){
    const pts=d.series[name]; const vals=pts.map(p=>p[1]);
    const last=vals[vals.length-1], lo=Math.min(...vals), hi=Math.max(...vals);
    const card=document.createElement("div"); card.className="card"; card.style.margin="0";
    card.innerHTML='<div class="toprow" style="align-items:baseline">'+
      '<div class="eyebrow">'+esc(name)+'</div>'+
      '<div class="big" style="font-size:22px;margin:0">'+fmtNum(last)+'</div></div>'+
      '<div style="margin:10px 0 6px">'+sparkline(pts)+'</div>'+
      '<div class="muted" style="display:flex;justify-content:space-between;font-size:11.5px">'+
      '<span>min '+fmtNum(lo)+' · max '+fmtNum(hi)+'</span><span>'+pts.length+' pts</span></div>'+
      '<div class="muted" style="font-size:11px;margin-top:2px">'+fmtTime(pts[0][0])+' → '+fmtTime(pts[pts.length-1][0])+'</div>';
    charts.appendChild(card);
  }
}
// Export the currently-loaded window as CSV (one column per metric, rows by
// timestamp). All metrics share the cycle timestamps, so they line up.
function exportCsv(){
  const names=Object.keys(LAST.series).sort();
  if(!names.length){ return; }
  const rows={};   // ts -> {metric: value}
  for(const name of names){ for(const [ts,v] of LAST.series[name]){ (rows[ts]=rows[ts]||{})[name]=v; } }
  const tss=Object.keys(rows).sort();
  const esc2=s=>{ s=String(s); return /[",\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s; };
  let csv="ts,"+names.map(esc2).join(",")+"\n";
  for(const ts of tss){ csv+=esc2(ts)+","+names.map(n=> rows[ts][n]==null?"":rows[ts][n]).join(",")+"\n"; }
  const blob=new Blob([csv],{type:"text/csv"});
  const a=document.createElement("a"); a.href=URL.createObjectURL(blob);
  a.download="history-"+LAST.hours+"h.csv"; document.body.appendChild(a); a.click();
  setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 0);
}
document.getElementById("export").addEventListener("click", exportCsv);
document.getElementById("win").addEventListener("change", tick);
tick();
setInterval(()=>{ if(document.getElementById("follow").checked) tick(); }, REFRESH);
</script>
"""


@app.route("/history")
@require_auth
def history():
    return page(render_template_string(HISTORY, refresh=DASH_REFRESH_SECONDS),
                page="history", title="History · The Castle Fun Center")


# ---------------------------------------------------------------------------
# MQTT console (live topic feed + manual publish)
# ---------------------------------------------------------------------------
MQTT_PAGE = """
<div class="card">
  <div class="toprow" style="align-items:center">
    <div><h3 style="margin:0">MQTT console</h3>
      <p class="muted" style="margin:4px 0 0">Live view of broker traffic and a manual publish console.
       The web UI keeps its own subscription so you can watch any topic and send test messages.</p></div>
    <span class="conn" id="mq-conn"><span class="dot idle"></span>connecting…</span>
  </div>
</div>

<div class="card">
  <div class="tabs">
    <button type="button" class="tab" data-tab="feed">Live feed</button>
    <button type="button" class="tab" data-tab="topics">Topics</button>
  </div>

  <div id="tab-feed" class="tabpane">
    <div class="row" style="align-items:flex-end">
      <div style="flex:1"><label style="margin-top:0">Topic filter <span class="hint">(prefix, e.g. sensors/)</span></label>
        <input id="filter" placeholder="(all topics)"></div>
      <div style="flex:0 0 auto"><label class="muted" style="margin:0 0 8px;display:flex;align-items:center;gap:6px;font-weight:500">
        <input type="checkbox" id="follow" checked style="width:auto;margin:0"> auto-refresh</label></div>
      <div style="flex:0 0 auto"><button type="button" class="secondary mini" id="clear" style="margin:0 0 6px">Clear view</button></div>
    </div>
    <div class="table-wrap" style="margin-top:10px">
      <table>
        <thead><tr><th style="width:90px">When</th><th>Topic</th><th style="width:70px">Flags</th><th>Payload</th></tr></thead>
        <tbody id="feedbody"><tr><td colspan="4" class="muted">Waiting for messages…</td></tr></tbody>
      </table>
    </div>
    <p class="muted" id="feednote" style="margin-top:10px">—</p>
  </div>

  <div id="tab-topics" class="tabpane" style="display:none">
    <div class="table-wrap">
      <table>
        <thead><tr><th>Topic</th><th style="width:70px">Msgs</th><th style="width:90px">Updated</th><th>Latest payload</th></tr></thead>
        <tbody id="topicsbody"><tr><td colspan="4" class="muted">No topics seen yet…</td></tr></tbody>
      </table>
    </div>
    <p class="muted" style="margin-top:10px">The most recent value retained per topic (newest broker state first seen by the UI).</p>
  </div>
</div>

<div class="card" id="pub-card">
  <h3 style="margin:0 0 4px">Publish a message</h3>
  <div id="pub-disabled" class="msg" style="display:none;background:#1a2742;border:1px solid var(--line);color:var(--muted)">
    Publishing is <b>off</b>. Enable <b>Allow MQTT publishing</b> under
    <a href="{{ url_for('settings') }}">Settings → Web interface</a> (a web login is required) to send messages.</div>
  <div id="pub-form">
    <div class="row">
      <div style="flex:2"><label>Topic</label><input id="p-topic" placeholder="facility/cmd/relay1"></div>
      <div style="flex:0 0 110px"><label>QoS</label>
        <select id="p-qos"><option>0</option><option>1</option><option>2</option></select></div>
      <div style="flex:0 0 auto"><label class="muted" style="display:flex;align-items:center;gap:6px;margin:14px 0 6px;font-weight:500">
        <input type="checkbox" id="p-retain" style="width:auto;margin:0"> retain</label></div>
    </div>
    <label>Payload</label>
    <textarea id="p-payload" style="min-height:90px" placeholder="ON  (or any string / JSON)"></textarea>
    <div class="field-err" id="p-err"></div>
    <button type="button" id="p-send">Publish</button>
    <p class="muted" style="margin-top:10px">⚠ Messages go straight to the broker (LAN-only, authenticated, audited).
     Wildcards (<code>#</code> <code>+</code>) are not allowed in a publish topic.</p>
  </div>
</div>
<div id="toast"></div>
<script>
const REFRESH = {{ refresh }} * 1000;
function esc(s){ const d=document.createElement("div"); d.textContent=String(s); return d.innerHTML; }
function toast(t,e){ const x=document.getElementById("toast"); if(!x) return; x.textContent=t; x.className="show"+(e?" err":""); clearTimeout(toast._t); toast._t=setTimeout(()=>x.className=e?"err":"",3200); }
function agoText(iso){ if(!iso) return "—"; const t=Date.parse(iso); if(isNaN(t)) return iso;
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<5) return "now"; if(s<60) return s+"s"; if(s<3600) return Math.round(s/60)+"m"; return Math.round(s/3600)+"h"; }

let SINCE=0, CAN_PUBLISH=false, ROWS=[];
const MAXROWS=400;
function flags(m){ let f=[]; if(m.retain) f.push('<span class="pill na" style="padding:1px 6px">R</span>'); if(m.qos) f.push('<span class="pill off" style="padding:1px 6px">q'+m.qos+'</span>'); return f.join(" "); }

function renderFeed(){
  const tb=document.getElementById("feedbody");
  if(!ROWS.length){ tb.innerHTML='<tr><td colspan="4" class="muted">No messages yet on this filter.</td></tr>'; return; }
  tb.innerHTML="";
  for(const m of ROWS.slice().reverse()){
    const tr=document.createElement("tr");
    const pl = m.binary ? '<span class="muted">[binary] </span>'+esc(m.payload) : esc(m.payload) + (m.truncated?' <span class="muted">…(truncated)</span>':'');
    tr.innerHTML='<td class="muted" title="'+esc(m.ts)+'">'+esc(agoText(m.ts))+'</td>'+
      '<td><code>'+esc(m.topic)+'</code></td><td>'+flags(m)+'</td>'+
      '<td style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px">'+pl+'</td>';
    tr.style.cursor="pointer";
    tr.addEventListener("click",()=>{ document.getElementById("p-topic").value=m.topic; });
    tb.appendChild(tr);
  }
}
async function tickFeed(){
  const topic=encodeURIComponent(document.getElementById("filter").value.trim());
  let d; try{ const r=await fetch("api/mqtt?since="+SINCE+"&topic="+topic,{cache:"no-store"}); if(!r.ok) return; d=await r.json(); }
  catch(e){ return; }
  const st=d.stats||{}; const conn=document.getElementById("mq-conn");
  if(!d.enabled){ conn.innerHTML='<span class="dot idle"></span>console disabled'; }
  else conn.innerHTML='<span class="dot '+(st.connected?"up":"down")+'"></span>'+(st.connected?"connected":"broker offline")+' · '+(st.received||0)+' msgs';
  CAN_PUBLISH=!!d.can_publish; applyPublishState();
  for(const m of (d.messages||[])){ ROWS.push(m); SINCE=Math.max(SINCE,m.seq); }
  if(ROWS.length>MAXROWS) ROWS=ROWS.slice(-MAXROWS);
  document.getElementById("feednote").textContent =
    (st.connected? "Live · ":"Offline · ")+(st.topics||0)+" topics · "+(st.buffered||0)+" buffered"+
    (d.enabled? "":" · enable the console in config (web.mqtt_console_enabled)");
  if(d.messages && d.messages.length) renderFeed();
}
async function tickTopics(){
  let d; try{ const r=await fetch("api/mqtt?topics=1&limit=1",{cache:"no-store"}); if(!r.ok) return; d=await r.json(); }
  catch(e){ return; }
  const tb=document.getElementById("topicsbody"); const list=d.topic_list||[];
  if(!list.length){ tb.innerHTML='<tr><td colspan="4" class="muted">No topics seen yet…</td></tr>'; return; }
  tb.innerHTML="";
  for(const t of list){
    const tr=document.createElement("tr"); tr.style.cursor="pointer";
    const pl = t.binary ? '[binary]' : (t.payload.length>120? t.payload.slice(0,120)+"…" : t.payload);
    tr.innerHTML='<td><code>'+esc(t.topic)+'</code></td><td class="muted">'+t.count+'</td>'+
      '<td class="muted" title="'+esc(t.ts)+'">'+esc(agoText(t.ts))+'</td>'+
      '<td style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px">'+esc(pl)+'</td>';
    tr.addEventListener("click",()=>{ document.getElementById("p-topic").value=t.topic; document.getElementById("filter").value=t.topic; });
    tb.appendChild(tr);
  }
}

function applyPublishState(){
  document.getElementById("pub-disabled").style.display = CAN_PUBLISH? "none":"";
  document.getElementById("pub-form").style.display = CAN_PUBLISH? "":"none";
}
document.getElementById("p-send").addEventListener("click", async ()=>{
  const topic=document.getElementById("p-topic").value.trim();
  const payload=document.getElementById("p-payload").value;
  const qos=Number(document.getElementById("p-qos").value);
  const retain=document.getElementById("p-retain").checked;
  const err=document.getElementById("p-err"); err.textContent="";
  if(!topic){ err.textContent="Topic is required."; return; }
  try{
    const r=await fetch("api/mqtt/publish",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({topic,payload,qos,retain})});
    const j=await r.json();
    if(!r.ok){ err.textContent=j.error||"Publish failed."; toast(j.error||"Publish failed",true); return; }
    toast("Published to "+topic);
  }catch(e){ err.textContent="Network error."; toast("Network error",true); }
});

document.getElementById("clear").addEventListener("click",()=>{ ROWS=[]; renderFeed(); });
document.getElementById("filter").addEventListener("input",()=>{ ROWS=[]; SINCE=0; renderFeed(); });
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active")); t.classList.add("active");
  const tab=t.dataset.tab;
  document.getElementById("tab-feed").style.display = tab==="feed"?"":"none";
  document.getElementById("tab-topics").style.display = tab==="topics"?"":"none";
  if(tab==="topics") tickTopics();
}));

function tick(){ if(document.getElementById("follow").checked) tickFeed();
  if(document.getElementById("tab-topics").style.display!=="none") tickTopics(); }
document.querySelector('.tab[data-tab="feed"]').click();
tickFeed();
setInterval(tick, 2500);
</script>
"""


@app.route("/mqtt")
@require_auth
def mqtt_page():
    return page(render_template_string(MQTT_PAGE, refresh=DASH_REFRESH_SECONDS),
                page="mqtt", title="MQTT · The Castle Fun Center")


# ---------------------------------------------------------------------------
# Settings (friendly form for scalar config)
# ---------------------------------------------------------------------------
SETTINGS = """
<div class="card" style="background:linear-gradient(180deg,#0e1a2e,var(--panel2))">
  <h3 style="margin:0 0 4px">Settings</h3>
  <p class="muted" style="margin:0">Configure the controller. Everything is <b>validated before saving</b> —
   out-of-range values are rejected and nothing is written. Thresholds, the lookback window, the poll
   interval, MQTT publish options and rules apply on the <b>next poll</b>; changing <b>location</b>, the
   <b>MQTT connection</b>, or any <b>web</b> setting needs a service restart. Passwords/tokens are never
   shown back — leave a field blank to keep the stored value.</p>
</div>
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
  <div class="row">
    <div><label>Event-driven re-evaluation <span class="hint">(re-run rules the instant an MQTT input changes)</span></label>
      <select name="event_driven">
        <option value="true" {{ 'selected' if c.event_driven }}>on (react immediately; needs a restart)</option>
        <option value="false" {{ 'selected' if not c.event_driven }}>off (only re-evaluate each poll cycle)</option>
      </select></div>
    <div></div>
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
    <div><label>Password</label><input name="mqtt_password" type="password" value="" autocomplete="new-password"
      placeholder="{{ '•••••• — leave blank to keep' if c.mqtt.password else 'none set' }}"></div>
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
  <h3>Slack alerts</h3>
  <p class="muted">Get a Slack message if the MQTT broker stays unreachable. Needs a
   Slack <b>bot token</b> (<code>xoxb-…</code>) with <code>chat:write</code>, invited to the channel.</p>
  <div class="row">
    <div><label>Enabled</label>
      <select name="slack_enabled">
        <option value="false" {{ 'selected' if not c.slack.enabled }}>false</option>
        <option value="true" {{ 'selected' if c.slack.enabled }}>true</option>
      </select></div>
    <div><label>Alert after broker down for <span class="hint">(minutes)</span></label>
      <input name="slack_minutes" value="{{ c.slack.broker_unreachable_minutes }}"></div>
  </div>
  <div class="row">
    <div><label>Channel <span class="hint">(#name or ID)</span></label>
      <input name="slack_channel" value="{{ c.slack.channel or '' }}" placeholder="#alerts"></div>
    <div><label>Bot token <span class="hint">(or set SLACK_BOT_TOKEN in the env)</span></label>
      <input name="slack_token" type="password" value="" autocomplete="new-password"
        placeholder="{{ '•••••• — leave blank to keep' if c.slack.bot_token else 'xoxb-… (env takes precedence)' }}"></div>
  </div>
</div>

<div class="card">
  <h3>Remote status page</h3>
  <p class="muted">Push a read‑only copy of the status to an external dashboard each cycle
   (see <code>cloud-status/</code>). <b>Outbound only</b> — nothing can reach back in, no remote control.</p>
  <div class="row">
    <div><label>Enabled</label>
      <select name="status_push_enabled">
        <option value="false" {{ 'selected' if not c.status_push.enabled }}>false</option>
        <option value="true" {{ 'selected' if c.status_push.enabled }}>true</option>
      </select></div>
    <div><label>Shared token <span class="hint">(sent as X‑Status‑Token; matches ingest.php)</span></label>
      <input name="status_push_token" type="password" value="" autocomplete="new-password"
        placeholder="{{ '•••••• — leave blank to keep' if c.status_push.token else 'a long random secret' }}"></div>
  </div>
  <label>Endpoint URL <span class="hint">(your dashboard's ingest.php)</span></label>
  <input name="status_push_url" value="{{ c.status_push.url or '' }}" placeholder="https://dashboards.example.com/weather/ingest.php">
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
      <input name="web_password" type="password" value="" autocomplete="new-password"
        placeholder="{{ '•••••• — leave blank to keep' if c.web.password else 'set to enable login' }}"></div>
  </div>
  <div class="row">
    <div><label>Manual device control <span class="hint">(Auto/On/Off buttons on the dashboard)</span></label>
      <select name="web_allow_manual_control">
        <option value="false" {{ 'selected' if not c.web.allow_manual_control }}>off (display only)</option>
        <option value="true" {{ 'selected' if c.web.allow_manual_control }}>on (requires a login)</option>
      </select></div>
    <div><label>Allow MQTT publishing <span class="hint">(send messages from the MQTT console)</span></label>
      <select name="web_allow_mqtt_publish">
        <option value="false" {{ 'selected' if not c.web.allow_mqtt_publish }}>off (feed is read-only)</option>
        <option value="true" {{ 'selected' if c.web.allow_mqtt_publish }}>on (requires a login)</option>
      </select></div>
  </div>
  <p class="muted">Manual control lets an authenticated operator force a device ON/OFF from the
   dashboard (LAN-only, audited). <b>Allow MQTT publishing</b> lets the MQTT console send arbitrary
   messages to the broker. Both require a login to be set; the remote status page stays read-only.</p>
  <p class="muted">⚠ Changing <b>location</b>, the <b>MQTT connection</b> (host/port/credentials/client id),
   or any <b>web interface</b> setting needs a restart of the corresponding service.
   Thresholds, lookback, poll interval, QoS, retain, status topic and rules apply on the next poll automatically.</p>
</div>

<div class="card">
  <h3>Metric history</h3>
  <p class="muted">Record each cycle's numeric metrics to a small local database so the
   <b>History</b> page can chart trends. Applies on the next poll.</p>
  <div class="row">
    <div><label>Enabled</label>
      <select name="history_enabled">
        <option value="true" {{ 'selected' if c.history.enabled }}>true</option>
        <option value="false" {{ 'selected' if not c.history.enabled }}>false</option>
      </select></div>
    <div><label>Retention <span class="hint">(days, 1…3650)</span></label>
      <input name="history_retention_days" value="{{ c.history.retention_days }}"></div>
  </div>
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
    try:
        cfg = load_raw()
    except Exception as e:
        return _config_error_page("settings", e)
    msg = msgclass = None
    if request.method == "POST":
        f = request.form
        try:
            ua = f.get("user_agent", "").strip()
            if not ua:
                raise ValueError("User-Agent is required (NWS rejects requests without one)")

            loc = cfg.setdefault("location", {})
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
            if f.get("event_driven") is not None:
                cfg["event_driven"] = f.get("event_driven") == "true"
            cfg.setdefault("precipitation", {})["lookback_hours"] = _ranged(
                "Lookback window", f.get("lookback_hours"), 1, 720, integer=True)

            mq = cfg.setdefault("mqtt", {})
            mq["host"] = _qstr(f.get("mqtt_host", "").strip() or "localhost")
            mq["port"] = _ranged("MQTT port", f.get("mqtt_port"), 1, 65535, integer=True)
            mq["username"] = _qstr(f.get("mqtt_username", ""))
            # Password fields are never echoed back, so a blank submission means
            # "keep the stored password" rather than "wipe it".
            if f.get("mqtt_password", ""):
                mq["password"] = _qstr(f.get("mqtt_password"))
            mq.setdefault("password", "")
            mq["client_id"] = _qstr(f.get("mqtt_client_id", "").strip() or "weather-mqtt-controller")
            mq["qos"] = _ranged("QoS", f.get("mqtt_qos"), 0, 2, integer=True)
            mq["retain"] = f.get("mqtt_retain", "true") == "true"
            mq["status_topic"] = _qstr(f.get("status_topic", "").strip())

            slack = cfg.setdefault("slack", {})
            slack["enabled"] = f.get("slack_enabled") == "true"
            slack["channel"] = _qstr(f.get("slack_channel", "").strip())
            mins_raw = (f.get("slack_minutes") or "").strip()
            slack["broker_unreachable_minutes"] = (
                _ranged("Slack alert delay", mins_raw, 1, 10080, integer=True)
                if mins_raw else int(slack.get("broker_unreachable_minutes", 60)))
            if f.get("slack_token", ""):              # blank = keep stored token
                slack["bot_token"] = _qstr(f.get("slack_token"))
            slack.setdefault("bot_token", "")
            if slack["enabled"] and not slack["channel"]:
                raise ValueError("Slack alerts need a channel (e.g. #alerts)")
            if (slack["enabled"] and not str(slack.get("bot_token") or "")
                    and not os.environ.get("SLACK_BOT_TOKEN")):
                raise ValueError("Slack alerts need a bot token (set it here or as "
                                 "the SLACK_BOT_TOKEN env var)")

            sp = cfg.setdefault("status_push", {})
            sp["enabled"] = f.get("status_push_enabled") == "true"
            sp["url"] = _qstr(f.get("status_push_url", "").strip())
            if f.get("status_push_token", ""):        # blank = keep stored token
                sp["token"] = _qstr(f.get("status_push_token"))
            sp.setdefault("token", "")
            if sp["enabled"]:
                if not sp["url"]:
                    raise ValueError("Remote status page needs an endpoint URL")
                if not str(sp["url"]).lower().startswith(("http://", "https://")):
                    raise ValueError("Remote status URL must start with http:// or https://")

            web = cfg.setdefault("web", {})
            web["host"] = _qstr(f.get("web_host", "").strip() or "0.0.0.0")
            web["port"] = _ranged("Web port", f.get("web_port"), 1, 65535, integer=True)
            web["username"] = _qstr(f.get("web_username", "").strip())
            if f.get("web_password", ""):
                web["password"] = _qstr(f.get("web_password"))
            web.setdefault("password", "")
            # Refuse a username with no password: it looks like auth is on but
            # accepts a blank password. Require both, or neither.
            if web["username"] and not str(web.get("password") or ""):
                raise ValueError("set a login password too (or clear the username "
                                 "to disable the login)")
            amc = f.get("web_allow_manual_control") == "true"
            if amc and not (str(web.get("username") or "") and str(web.get("password") or "")):
                raise ValueError("manual device control needs a web login "
                                 "(set a username and password first)")
            web["allow_manual_control"] = amc
            amp = f.get("web_allow_mqtt_publish") == "true"
            if amp and not (str(web.get("username") or "") and str(web.get("password") or "")):
                raise ValueError("MQTT publishing needs a web login "
                                 "(set a username and password first)")
            web["allow_mqtt_publish"] = amp

            histd = cfg.setdefault("history", {})
            if f.get("history_enabled") is not None:
                histd["enabled"] = f.get("history_enabled") == "true"
            if str(f.get("history_retention_days", "")).strip():
                histd["retention_days"] = _ranged("History retention",
                                                  f.get("history_retention_days"), 1, 3650, integer=True)

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
    cfg.setdefault("location", {}).setdefault("station_id", None)
    cfg.setdefault("event_driven", True)
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
    webd.setdefault("allow_manual_control", False)
    webd.setdefault("allow_mqtt_publish", False)
    sld = cfg.setdefault("slack", {})
    sld.setdefault("enabled", False)
    sld.setdefault("channel", "")
    sld.setdefault("bot_token", "")
    sld.setdefault("broker_unreachable_minutes", 60)
    spd = cfg.setdefault("status_push", {})
    spd.setdefault("enabled", False)
    spd.setdefault("url", "")
    spd.setdefault("token", "")
    histd = cfg.setdefault("history", {})
    histd.setdefault("enabled", True)
    histd.setdefault("retention_days", 14)
    body = render_template_string(SETTINGS, c=cfg)
    return page(body, page="settings", msg=msg, msgclass=msgclass,
                title="Settings · The Castle Fun Center")


# ---------------------------------------------------------------------------
# Rules — structured form builder + raw YAML editor
# ---------------------------------------------------------------------------
# Derived from the monitor's canonical METRIC_SPECS so the builder, this server,
# and the monitor's own validator can never disagree about valid metrics/ops.
# Serialized into the page for the browser-side builder.
def _builder_ops(name, spec):
    """Operators the form builder offers for a metric. `changed` (value-less) is
    universally valid, but only meaningful for metrics whose value is tracked
    cycle-to-cycle -- the NWS alert set isn't, so it's omitted there."""
    ops = list(spec["ops"])
    if spec["type"] != "alert":
        ops.append("changed")
    return ops


RULE_METRICS = {
    name: {"type": spec["type"], "ops": _builder_ops(name, spec)}
    for name, spec in core.METRIC_SPECS.items()
}


def builder_metrics(cfg):
    """The builder's metric catalogue for this config: built-ins plus the
    config-declared variables (var_<name>), mqtt_in sensors, and http_poll
    metrics, so dropdowns discover them live."""
    out = dict(RULE_METRICS)
    extra = {**core.variable_specs(cfg.get("variables", {})),
             **core.mqtt_input_specs(cfg.get("mqtt_inputs", [])),
             **core.http_input_specs(cfg.get("http_inputs", [])),
             **core.computed_specs(cfg.get("computed", {}))}
    for name, spec in extra.items():
        out[name] = {"type": spec["type"], "ops": list(spec["ops"])}
    return out


def _coerce_cond_value(metric, operator, raw, metrics=RULE_METRICS):
    """Validate a metric/operator pair and coerce the value to the right type.

    Returns the typed value, or None when the operator needs no value
    (active_alert + any). Raises ValueError with a human message on anything
    the builder shouldn't have allowed through."""
    meta = metrics.get(metric)
    if meta is None:
        raise ValueError(f"unknown metric '{metric}'")
    if operator not in meta["ops"]:
        raise ValueError(f"operator '{operator}' is not valid for metric '{metric}'")
    if operator == "changed":
        return None                       # value-less: true when the metric moves
    if meta["type"] == "alert" and operator == "any":
        return None
    if operator == "between":             # value is a "low, high" pair of numbers
        parts = [p for p in (x.strip() for x in str(raw).split(",")) if p]
        if len(parts) != 2:
            raise ValueError(f"metric '{metric}' between needs two numbers 'low, high'")
        try:
            return [_num(parts[0]), _num(parts[1])]
        except ValueError:
            raise ValueError(f"metric '{metric}' between needs numeric bounds")
    if operator == "in":                  # value is a comma-separated list
        parts = [p for p in (x.strip() for x in str(raw).split(",")) if p]
        if not parts:
            raise ValueError(f"metric '{metric}' in needs at least one value")
        if meta["type"] == "number":
            try:
                return [_num(p) for p in parts]
            except ValueError:
                raise ValueError(f"metric '{metric}' in needs numeric values")
        return parts
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


def _rules_from_structured(items, metrics=RULE_METRICS):
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
            cond = {"metric": metric, "operator": _qstr(operator)}
            # Compare against another metric instead of a constant value.
            vmetric = str(c.get("value_metric", "") or "").strip()
            if vmetric:
                if metrics.get(metric) is None:
                    raise ValueError(f"rule '{name}': unknown metric '{metric}'")
                if operator not in core.NUMERIC_COMPARE:
                    raise ValueError(f"rule '{name}': comparing to a metric needs one of "
                                     f"{', '.join(core.NUMERIC_COMPARE)} (not '{operator}')")
                if metrics.get(vmetric) is None:
                    raise ValueError(f"rule '{name}': unknown comparison metric '{vmetric}'")
                cond["value_metric"] = _qstr(vmetric)
            else:
                try:
                    val = _coerce_cond_value(metric, operator, c.get("value"), metrics)
                except ValueError as e:
                    raise ValueError(f"rule '{name}': {e}")
                if val is not None:
                    if isinstance(val, list):  # between/in -> protect any string items
                        cond["value"] = [_qstr(x) if isinstance(x, str) else x for x in val]
                    else:
                        cond["value"] = _qstr(val) if isinstance(val, str) else val
            forr = str(c.get("for", "") or "").strip()
            if forr:
                if core.parse_duration(forr, None) is None:
                    raise ValueError(f"rule '{name}': condition '{metric}' for: "
                                     f"'{forr}' is not a valid duration")
                cond["for"] = _qstr(forr)
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
        # Only write enabled when off, so normal rules stay uncluttered (the
        # monitor defaults a missing `enabled` to true).
        if it.get("enabled") is False:
            rule["enabled"] = False
        actions = _actions_from_structured(it.get("actions"), name)
        if actions:
            rule["actions"] = actions
        out.append(rule)
    return out


def _actions_from_structured(items, rule_name):
    """Build a rule's `actions:` list from the builder's JSON (or YAML round-trip).
    Empty/blank rows are skipped; the monitor's validator does the final check."""
    out = []
    for a in (items or []):
        if not isinstance(a, dict):
            continue
        kind = str(a.get("kind", "")).strip().lower()
        trig = str(a.get("on", "both")).strip().lower()       # builder field is `on`
        if trig not in ("match", "clear", "both"):
            trig = "both"
        if kind == "mqtt":
            topic = str(a.get("topic", "")).strip()
            if not topic:
                continue
            spec = {"topic": _qstr(topic), "payload": _qstr(str(a.get("payload", "")))}
            if a.get("qos") not in (None, ""):
                try:
                    q = int(a["qos"])
                    if q in (0, 1, 2):
                        spec["qos"] = q
                except (TypeError, ValueError):
                    pass
            if a.get("retain") is True:
                spec["retain"] = True
            out.append({"trigger": _qstr(trig), "mqtt": spec})
        elif kind == "webhook":
            url = str(a.get("url", "")).strip()
            if not url:
                continue
            method = str(a.get("method", "POST")).strip().upper()
            spec = {"url": _qstr(url), "method": _qstr(method if method in ("GET", "POST", "PUT") else "POST")}
            body = str(a.get("body", ""))
            if body:
                spec["body"] = _qstr(body)
            out.append({"trigger": _qstr(trig), "webhook": spec})
        elif kind == "notify":
            text = str(a.get("text", "")).strip()
            if not text:
                continue
            out.append({"trigger": _qstr(trig), "notify": {"text": _qstr(text)}})
    return out


def _actions_to_structured(actions):
    """Flatten a rule's `actions:` into the builder's editable rows."""
    out = []
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        on = str(a.get("trigger", "both"))
        if "mqtt" in a and isinstance(a["mqtt"], dict):
            mq = a["mqtt"]
            out.append({"kind": "mqtt", "on": on, "topic": str(mq.get("topic", "")),
                        "payload": _value_to_str(mq.get("payload")),
                        "qos": mq.get("qos"), "retain": mq.get("retain") is True})
        elif "webhook" in a and isinstance(a["webhook"], dict):
            out.append({"kind": "webhook", "on": on, "url": str(a["webhook"].get("url", "")),
                        "method": str(a["webhook"].get("method", "POST")),
                        "body": _value_to_str(a["webhook"].get("body"))})
        elif "notify" in a and isinstance(a["notify"], dict):
            out.append({"kind": "notify", "on": on, "text": _value_to_str(a["notify"].get("text"))})
    return out


def _value_to_str(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):       # between/in -> "low, high" / "a, b, c"
        return ", ".join(_value_to_str(x) for x in v)
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
            "value_metric": str(c.get("value_metric", "") or ""),
            "for": str(c.get("for", "") or ""),
        })
    return {
        "name": str(rule.get("name", "")),
        "description": str(rule.get("description", "")),
        "topic": str(rule.get("topic", "")),
        "on_match": _value_to_str(rule.get("on_match")),
        "on_clear": _value_to_str(rule.get("on_clear")),
        "enabled": rule.get("enabled", True) is not False,
        "combine": combine,
        "conditions": conds,
        "actions": _actions_to_structured(rule.get("actions")),
    }


def _structured_list(cfg):
    return [s for s in (_rule_to_structured(r) for r in _to_plain(cfg.get("rules", [])))
            if s is not None]


RULES = """
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
          <tr><td><code>is_raining</code></td><td>precipitating right now</td><td><code>== != changed</code></td></tr>
          <tr><td><code>precip_accum_in</code></td><td>measured rain over lookback (in)</td><td><code>&lt; &lt;= &gt; &gt;= == != between in changed</code></td></tr>
          <tr><td><code>precipitation_probability</code></td><td>forecast chance (%)</td><td><code>&lt; &lt;= &gt; &gt;= == != between in changed</code></td></tr>
          <tr><td><code>temperature</code></td><td>air temp (°F)</td><td><code>&lt; &lt;= &gt; &gt;= == != between in changed</code></td></tr>
          <tr><td><code>wind_speed_mph</code></td><td>wind speed (mph)</td><td><code>&lt; &lt;= &gt; &gt;= == != between in changed</code></td></tr>
          <tr><td><code>humidity</code></td><td>relative humidity (%)</td><td><code>&lt; &lt;= &gt; &gt;= == != between in changed</code></td></tr>
          <tr><td><code>short_forecast</code></td><td>text e.g. "Light Rain"</td><td><code>contains equals in changed</code></td></tr>
          <tr><td><code>active_alert</code></td><td>NWS watches/warnings</td><td><code>any contains equals</code></td></tr>
          <tr><td><code>time_hour</code></td><td>local hour 0–23</td><td><code>&lt; &lt;= &gt; &gt;= == != between in changed</code></td></tr>
          <tr><td><code>time_minute</code></td><td>local minute 0–59</td><td><code>&lt; &lt;= &gt; &gt;= == != between in changed</code></td></tr>
          <tr><td><code>time_weekday</code></td><td>mon…sun (local)</td><td><code>equals in contains changed</code></td></tr>
          <tr><td><code>time_is_weekend</code></td><td>Sat/Sun (true/false)</td><td><code>== != changed</code></td></tr>
          <tr><td><code>time_is_daytime</code></td><td>sun up at your lat/long</td><td><code>== != changed</code></td></tr>
        </tbody>
      </table>
      <p class="muted" style="margin-top:8px"><code>between</code> takes a
       <code>low, high</code> pair; <code>in</code> a comma-separated list;
       <code>changed</code> fires when the value moves (no value). The optional
       <b>for</b> box requires the condition to hold that long (e.g. <code>10m</code>).
       Nested groups, <code>not</code>, time windows and hysteresis are edited in
       the YAML tab.</p>
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

function valueControl(metric, operator, value){
  const meta = METRICS[metric] || {type:"text"};
  let c;
  if(operator==="between"){
    c=document.createElement("input"); c.className="c-val"; c.type="text";
    c.value = value!=null ? value : ""; c.placeholder="low, high";
  } else if(operator==="in"){
    c=document.createElement("input"); c.className="c-val"; c.type="text";
    c.value = value!=null ? value : ""; c.placeholder = meta.type==="number" ? "e.g. 30, 50, 70" : "a, b, c";
  } else if(meta.type==="bool"){
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

const NUMCMP = ["<","<=",">",">=","==","!="];
function cmpMetricNames(){ return METRIC_NAMES.filter(n=>{ const t=(METRICS[n]||{}).type; return t==="number"||t==="bool"; }); }
function metricPicker(selected){
  const s=document.createElement("select"); s.className="c-vmetric"; s.setAttribute("aria-label","comparison metric");
  cmpMetricNames().forEach(n=> s.appendChild(opt(n,n, n===selected)));
  return s;
}
function condRow(cond){
  cond = cond || {metric:METRIC_NAMES[0], operator:"", value:"", value_metric:"", for:""};
  const row = el("div","cond row");
  const metricWrap = el("div"); const m = document.createElement("select"); m.className="c-metric";
  m.setAttribute("aria-label","metric");
  METRIC_NAMES.forEach(n=> m.appendChild(opt(n,n, n===cond.metric)));
  if(!METRICS[cond.metric]) m.value=METRIC_NAMES[0];
  metricWrap.appendChild(m);
  const opWrap = el("div"); const o=document.createElement("select"); o.className="c-op";
  o.setAttribute("aria-label","operator");
  fillOps(o, m.value, cond.operator); opWrap.appendChild(o);
  // "compare to" mode: a constant value, or another metric's live value.
  const modeWrap = el("div","c-vmode-wrap"); const mode=document.createElement("select"); mode.className="c-vmode";
  mode.setAttribute("aria-label","compare to");
  mode.appendChild(opt("value","a value", !cond.value_metric));
  mode.appendChild(opt("metric","a metric", !!cond.value_metric));
  modeWrap.appendChild(mode);
  const valWrap = el("div","c-val-wrap");
  const forWrap = el("div","c-for-wrap"); const f=document.createElement("input"); f.className="c-for"; f.type="text";
  f.placeholder="for (e.g. 10m)"; f.value=cond.for||""; f.setAttribute("aria-label","sustain duration");
  f.title="optional: the condition must hold continuously this long (e.g. 30s, 10m, 2h)";
  forWrap.appendChild(f);
  const rmWrap = el("div","rm"); const rm=el("button","secondary danger mini","×"); rm.type="button";
  rmWrap.appendChild(rm);

  function noValue(){ const meta=METRICS[m.value]||{}; return o.value==="changed" || (meta.type==="alert" && o.value==="any"); }
  function cmpEligible(){ const meta=METRICS[m.value]||{}; return (meta.type==="number"||meta.type==="bool") && NUMCMP.includes(o.value); }
  function syncMode(){ modeWrap.style.display = (cmpEligible() && !noValue()) ? "" : "none"; if(!cmpEligible()) mode.value="value"; }
  function buildVal(keep){
    valWrap.innerHTML="";
    if(mode.value==="metric" && cmpEligible()) valWrap.appendChild(metricPicker(cond.value_metric));
    else valWrap.appendChild(valueControl(m.value, o.value, keep));
    valWrap.style.display = noValue() ? "none" : "";
  }
  m.addEventListener("change", ()=>{ fillOps(o, m.value, o.value); syncMode(); buildVal(null); });
  o.addEventListener("change", ()=>{ syncMode(); buildVal(null); });
  mode.addEventListener("change", ()=> buildVal(null));
  rm.addEventListener("click", ()=>{ const card=row.closest(".rule-card"); row.remove(); refreshCombine(card); });
  syncMode(); buildVal(cond.value);
  row.appendChild(metricWrap); row.appendChild(opWrap); row.appendChild(modeWrap); row.appendChild(valWrap); row.appendChild(forWrap); row.appendChild(rmWrap);
  return row;
}

function refreshCombine(card){
  const conds = card.querySelectorAll(".cond").length;
  card.querySelector(".combine-wrap").style.display = conds>1 ? "" : "none";
}

function actionFields(kind, a){
  a = a || {};
  const wrap = el("div","a-fields"); wrap.style.cssText="display:flex;gap:8px;flex-wrap:wrap;flex:1;min-width:240px";
  if(kind==="webhook"){
    wrap.innerHTML='<input class="a-url" placeholder="https://host/hook" style="flex:2;min-width:160px">'+
      '<select class="a-method" style="flex:0 0 84px"></select>'+
      '<input class="a-body" placeholder="body (supports {{metric}})" style="flex:2;min-width:160px">';
    ["POST","GET","PUT"].forEach(x=> wrap.querySelector(".a-method").appendChild(opt(x,x, x===(a.method||"POST"))));
    wrap.querySelector(".a-url").value=a.url||""; wrap.querySelector(".a-body").value=a.body||"";
  } else if(kind==="notify"){
    wrap.innerHTML='<input class="a-text" placeholder="Slack message (supports {{metric}})" style="flex:1;min-width:200px">';
    wrap.querySelector(".a-text").value=a.text||"";
  } else {
    wrap.innerHTML='<input class="a-topic" placeholder="topic e.g. facility/relay1" style="flex:1;min-width:140px">'+
      '<input class="a-payload" placeholder="payload (supports {{metric}})" style="flex:1;min-width:140px">'+
      '<select class="a-qos" title="QoS" style="flex:0 0 70px"></select>'+
      '<label class="muted" style="margin:0;display:flex;align-items:center;gap:5px;font-weight:500;white-space:nowrap">'+
      '<input type="checkbox" class="a-retain" style="width:auto;margin:0"> retain</label>';
    wrap.querySelector(".a-topic").value=a.topic||""; wrap.querySelector(".a-payload").value=a.payload||"";
    ["","0","1","2"].forEach(x=> wrap.querySelector(".a-qos").appendChild(opt(x, x===""?"qos —":"qos "+x, String(a.qos==null?"":a.qos)===x)));
    wrap.querySelector(".a-retain").checked = a.retain===true;
  }
  return wrap;
}
function actionRow(a){
  a = a || {kind:"mqtt", on:"match"};
  const row = el("div","action-row row"); row.style.alignItems="center";
  const onW=el("div"); onW.style.flex="0 0 96px"; const on=document.createElement("select"); on.className="a-on";
  [["match","on match"],["clear","on clear"],["both","on both"]].forEach(x=> on.appendChild(opt(x[0],x[1], x[0]===(a.on||"both")))); onW.appendChild(on);
  const kW=el("div"); kW.style.flex="0 0 108px"; const k=document.createElement("select"); k.className="a-kind";
  [["mqtt","MQTT"],["webhook","Webhook"],["notify","Notify"]].forEach(x=> k.appendChild(opt(x[0],x[1], x[0]===(a.kind||"mqtt")))); kW.appendChild(k);
  let fields=actionFields(k.value, a);
  const rmW=el("div"); rmW.style.flex="0 0 auto"; const rm=el("button","secondary danger mini","×"); rm.type="button"; rmW.appendChild(rm);
  k.addEventListener("change", ()=>{ const nf=actionFields(k.value,{}); row.replaceChild(nf, fields); fields=nf; });
  rm.addEventListener("click", ()=> row.remove());
  row.appendChild(onW); row.appendChild(kW); row.appendChild(fields); row.appendChild(rmW);
  return row;
}
function ruleCard(rule){
  rule = rule || {name:"",description:"",topic:"",on_match:"",on_clear:"",enabled:true,combine:"any",conditions:[],actions:[]};
  const card = el("div","rule-card");
  card.innerHTML =
    '<div class="rhead"><span class="idx"></span>'+
    '<label class="enabled-lbl" style="display:flex;align-items:center;gap:7px;margin:0;font-weight:600" '+
    'title="Disabled rules are not evaluated and publish nothing">'+
    '<input type="checkbox" class="f-enabled" style="width:auto;margin:0"> enabled</label></div>'+
    '<div class="row"><div><label>Name <input class="f-name"></label></div>'+
    '<div><label>Topic <input class="f-topic"></label></div></div>'+
    '<label>Description <span class="hint">(optional)</span> <input class="f-desc"></label>'+
    '<div class="row"><div><label>Payload when matched <span class="hint">(on_match)</span> <input class="f-onmatch"></label></div>'+
    '<div><label>Payload when cleared <span class="hint">(on_clear, optional)</span> <input class="f-onclear"></label></div></div>'+
    '<div class="combine-wrap"><label>When there are multiple conditions, match'+
    ' <select class="f-combine"></select></label></div>'+
    '<label style="margin-top:14px">Conditions</label><div class="conds"></div>'+
    '<div class="btnrow"><button type="button" class="secondary mini add-cond">+ Add condition</button></div>'+
    '<details class="actions-wrap" style="margin-top:6px"><summary class="muted" style="cursor:pointer">'+
    'Extra actions <span class="hint">(optional — extra publishes, webhooks, Slack on a transition)</span></summary>'+
    '<div class="actions" style="margin-top:8px;display:flex;flex-direction:column;gap:8px"></div>'+
    '<div class="btnrow"><button type="button" class="secondary mini add-action">+ Add action</button></div></details>'+
    '<div class="btnrow"><button type="button" class="danger mini remove-rule">Remove rule</button></div>';
  card.querySelector(".f-name").value = rule.name||"";
  card.querySelector(".f-topic").value = rule.topic||"";
  card.querySelector(".f-desc").value = rule.description||"";
  card.querySelector(".f-onmatch").value = rule.on_match||"";
  card.querySelector(".f-onclear").value = rule.on_clear||"";
  card.querySelector(".f-enabled").checked = rule.enabled !== false;
  const comb = card.querySelector(".f-combine");
  comb.appendChild(opt("any","ANY is true (OR)", rule.combine!=="all"));
  comb.appendChild(opt("all","ALL are true (AND)", rule.combine==="all"));
  const conds = card.querySelector(".conds");
  (rule.conditions && rule.conditions.length ? rule.conditions : [null]).forEach(c=> conds.appendChild(condRow(c)));
  card.querySelector(".add-cond").addEventListener("click", ()=>{ conds.appendChild(condRow()); refreshCombine(card); });
  const actionsBox = card.querySelector(".actions");
  (rule.actions || []).forEach(a=> actionsBox.appendChild(actionRow(a)));
  if((rule.actions || []).length) card.querySelector(".actions-wrap").open = true;
  card.querySelector(".add-action").addEventListener("click", ()=> actionsBox.appendChild(actionRow()));
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
      const noVal = operator==="changed" || (meta.type==="alert" && operator==="any");
      const forv=(row.querySelector(".c-for").value||"").trim();
      const modeSel=row.querySelector(".c-vmode");
      const vm=row.querySelector(".c-vmetric");
      if(modeSel && modeSel.value==="metric" && vm){
        return {metric, operator, value_metric:vm.value, for:forv};
      }
      let value="";
      if(!noVal){ const ctrl=row.querySelector(".c-val"); value=ctrl?ctrl.value:""; }
      return {metric, operator, value, for:forv};
    });
    const actions = [...card.querySelectorAll(".action-row")].map(row=>{
      const kind=row.querySelector(".a-kind").value;
      const on=row.querySelector(".a-on").value;
      if(kind==="webhook") return {kind, on, url:(row.querySelector(".a-url").value||"").trim(),
        method:row.querySelector(".a-method").value, body:row.querySelector(".a-body").value};
      if(kind==="notify") return {kind, on, text:(row.querySelector(".a-text").value||"").trim()};
      const qsel=row.querySelector(".a-qos");
      return {kind, on, topic:(row.querySelector(".a-topic").value||"").trim(), payload:row.querySelector(".a-payload").value,
        qos:(qsel&&qsel.value!=="")?Number(qsel.value):null, retain:row.querySelector(".a-retain").checked};
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

function validate(data){
  if(!data.length) return "Add at least one rule.";
  const durRe=/^\d+(\.\d+)?\s*[smh]?$/;
  for(let i=0;i<data.length;i++){
    const r=data[i], label="Rule "+(i+1);
    if(!r.name) return label+": name is required.";
    if(!r.topic) return "Rule '"+r.name+"': topic is required.";
    if(r.on_match==="") return "Rule '"+r.name+"': the on_match payload is required.";
    if(!r.conditions.length) return "Rule '"+r.name+"': add at least one condition.";
    for(const c of r.conditions){
      const meta=METRICS[c.metric]||{};
      if(c.for && !durRe.test(c.for.trim())) return "Rule '"+r.name+"': '"+c.metric+"' for must be a duration like 10m, 30s, 2h.";
      if(c.operator==="changed") continue;
      if(meta.type==="alert" && c.operator==="any") continue;
      if(c.operator==="between"){
        const ps=c.value.split(",").map(s=>s.trim()).filter(s=>s!=="");
        if(ps.length!==2 || ps.some(p=>isNaN(Number(p)))) return "Rule '"+r.name+"': "+c.metric+" between needs two numbers 'low, high'.";
        continue;
      }
      if(c.operator==="in"){
        const ps=c.value.split(",").map(s=>s.trim()).filter(s=>s!=="");
        if(!ps.length) return "Rule '"+r.name+"': "+c.metric+" in needs at least one value.";
        if(meta.type==="number" && ps.some(p=>isNaN(Number(p)))) return "Rule '"+r.name+"': "+c.metric+" in needs numeric values.";
        continue;
      }
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
    try:
        cfg = load_raw()
    except Exception as e:
        return _config_error_page("rules", e)
    msg = msgclass = None
    mode = request.form.get("mode", "form") if request.method == "POST" else "form"
    rules_yaml_override = None
    structured_override = None

    if request.method == "POST":
        try:
            if mode == "form":
                items = json.loads(request.form.get("rules_json", "[]"))
                cfg["rules"] = _rules_from_structured(items, builder_metrics(cfg))
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
    # The form builder only represents flat rules (a single condition, or one
    # any/all group of conditions). If the config uses the engine's nested
    # any/all/not, default to the YAML editor so a Save in the form tab can't
    # silently flatten/drop the advanced structure.
    active_tab = mode
    if request.method == "GET" and not all(
            _rule_is_flat(r) for r in _to_plain(cfg.get("rules", []) or [])):
        active_tab = "yaml"
    body = render_template_string(
        RULES, rules_yaml=rules_yaml, structured=structured,
        metrics=builder_metrics(cfg), active_tab=active_tab)
    return page(body, page="rules", msg=msg, msgclass=msgclass,
                title="Rules · The Castle Fun Center")


# ---------------------------------------------------------------------------
# Inputs (sources): operator variables + mqtt_in sensors + http_poll, editable
# ---------------------------------------------------------------------------
def _num_or(value, default):
    try:
        return _num(str(value))
    except Exception:
        return default


def _apply_sources(cfg, payload):
    """Replace cfg's variables / mqtt_inputs / http_inputs from the builder's
    JSON. Values are quoted for a lossless YAML round-trip; the monitor's
    validate_config (via save_config) does the real validation."""
    if not isinstance(payload, dict):
        raise ValueError("malformed inputs payload")

    variables = {}
    for it in (payload.get("variables") or []):
        name = str((it or {}).get("name", "")).strip()
        if not name:
            continue
        vtype = str(it.get("type", "bool")).strip().lower()
        if vtype == "number":
            default = _num_or(it.get("default"), 0)
        else:
            d = it.get("default")
            default = d if isinstance(d, bool) else str(d).strip().lower() in ("true", "1", "yes", "on")
        variables[_qstr(name)] = {"type": _qstr(vtype), "default": default}
    cfg["variables"] = variables

    mlist = []
    for it in (payload.get("mqtt_inputs") or []):
        topic = str((it or {}).get("topic", "")).strip()
        metric = str(it.get("metric", "")).strip()
        if not topic and not metric:
            continue
        mlist.append({"topic": _qstr(topic), "metric": _qstr(metric),
                      "parse": _qstr(str(it.get("parse", "number")).strip().lower())})
    cfg["mqtt_inputs"] = mlist

    hlist = []
    for it in (payload.get("http_inputs") or []):
        url = str((it or {}).get("url", "")).strip()
        mp = []
        for m in (it.get("map") or []):
            metric = str((m or {}).get("metric", "")).strip()
            if not metric:
                continue
            mp.append({"metric": _qstr(metric), "path": _qstr(str(m.get("path", "")).strip()),
                       "type": _qstr(str(m.get("type", "number")).strip().lower())})
        if not url and not mp:
            continue
        hlist.append({"url": _qstr(url),
                      "interval_minutes": int(_num_or(it.get("interval_minutes"), 5)),
                      "timeout": int(_num_or(it.get("timeout"), 10)),
                      "map": mp})
    cfg["http_inputs"] = hlist

    computed = {}
    for it in (payload.get("computed") or []):
        name = str((it or {}).get("name", "")).strip()
        expr = str(it.get("expr", "")).strip()
        if not name and not expr:
            continue
        computed[_qstr(name)] = {"expr": _qstr(expr)}
    cfg["computed"] = computed


def _sources_payload(cfg):
    """Current sources as the builder's editable JSON shape."""
    variables = [{"name": str(n), "type": str((s or {}).get("type", "bool")),
                  "default": _value_to_str((s or {}).get("default"))}
                 for n, s in (_to_plain(cfg.get("variables", {})) or {}).items()]
    mqtt_inputs = [{"topic": str((it or {}).get("topic", "")), "metric": str(it.get("metric", "")),
                    "parse": str(it.get("parse", "number"))}
                   for it in (_to_plain(cfg.get("mqtt_inputs", [])) or [])]
    http_inputs = [{"url": str((it or {}).get("url", "")),
                    "interval_minutes": (it.get("interval_minutes", 5)),
                    "timeout": (it.get("timeout", 10)),
                    "map": [{"metric": str((m or {}).get("metric", "")), "path": str(m.get("path", "")),
                             "type": str(m.get("type", "number"))} for m in (it.get("map") or [])]}
                   for it in (_to_plain(cfg.get("http_inputs", [])) or [])]
    computed = [{"name": str(n), "expr": str((s or {}).get("expr", ""))}
                for n, s in (_to_plain(cfg.get("computed", {})) or {}).items()]
    return {"variables": variables, "mqtt_inputs": mqtt_inputs,
            "http_inputs": http_inputs, "computed": computed}


INPUTS = """
<form method="post" id="inputs-form">
  <input type="hidden" name="inputs_json" id="inputs_json">
  <div class="card">
    <h3 style="margin:0 0 4px">Inputs (sources)</h3>
    <p class="muted" style="margin:0 0 6px">Add the signals your rules can use. Each one becomes a
     <b>metric</b> the rule builder discovers automatically. Saved to <code>config.yaml</code> and
     validated before writing; new metrics apply on the next poll (mqtt/http connections re-read at the
     next cycle, a new topic subscription needs a service restart).</p>
  </div>

  <div class="card">
    <div class="toprow"><h3 style="margin:0">Operator variables</h3>
      <button type="button" class="secondary mini" id="add-var" style="margin:0">+ Add variable</button></div>
    <p class="muted" style="margin:6px 0 0">Virtual flags/setpoints you toggle from the dashboard
     (metric <code>var_&lt;name&gt;</code>). Type <b>bool</b> or <b>number</b>.</p>
    <div id="vars" style="margin-top:10px"></div>
  </div>

  <div class="card">
    <div class="toprow"><h3 style="margin:0">MQTT sensor inputs</h3>
      <button type="button" class="secondary mini" id="add-mqtt" style="margin:0">+ Add MQTT input</button></div>
    <p class="muted" style="margin:6px 0 0">Subscribe to another device's topic and expose its payload as a
     metric (<b>number</b>/<b>bool</b>/<b>string</b>).</p>
    <div id="mqtts" style="margin-top:10px"></div>
  </div>

  <div class="card">
    <div class="toprow"><h3 style="margin:0">HTTP JSON inputs</h3>
      <button type="button" class="secondary mini" id="add-http" style="margin:0">+ Add HTTP input</button></div>
    <p class="muted" style="margin:6px 0 0">Poll a JSON endpoint and map fields (dotted path, arrays by
     index) to metrics.</p>
    <div id="https" style="margin-top:10px"></div>
  </div>

  <div class="card">
    <div class="toprow"><h3 style="margin:0">Computed metrics</h3>
      <button type="button" class="secondary mini" id="add-comp" style="margin:0">+ Add computed</button></div>
    <p class="muted" style="margin:6px 0 0">Derive a new <b>number</b> metric from others with a small
     formula: <code>+ - * / // % **</code> and parentheses over any metric defined <i>above</i> it (built-ins,
     variables, mqtt/http inputs, or an earlier computed). E.g. <code>power_kw - solar_kw</code> or
     <code>temperature - var_temp_setpoint</code>. A missing input yields no value (rules hold, fail-safe).</p>
    <div id="comps" style="margin-top:10px"></div>
    <div class="field-err" id="inputs-err" style="margin-top:10px"></div>
    <button type="submit" id="save-inputs">Save inputs</button>
  </div>
</form>
<script>
const SRC = {{ sources|tojson }};
const PARSE = ["number","bool","string"];
function el(t,c,h){const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e;}
function opt(v,l,s){const o=document.createElement("option");o.value=v;o.textContent=l||v;if(s)o.selected=true;return o;}
function rmBtn(){const b=el("button","secondary danger mini","×");b.type="button";b.style.margin="0";return b;}

// ---- variables ----
const vars=document.getElementById("vars");
function varDefault(type,val){
  let c;
  if(type==="number"){c=document.createElement("input");c.type="number";c.step="any";c.className="v-def";c.value=val!=null?val:"";c.placeholder="default";}
  else{c=document.createElement("select");c.className="v-def";c.appendChild(opt("true","true",String(val)==="true"));c.appendChild(opt("false","false",String(val)!=="true"));}
  return c;
}
function varRow(v){
  v=v||{name:"",type:"bool",default:"false"};
  const row=el("div","row"); row.style.alignItems="flex-end";
  const nw=el("div"); nw.innerHTML='<label style="margin-top:0">Name</label>'; const n=el("input","v-name");n.value=v.name||"";n.placeholder="maintenance_mode";nw.appendChild(n);
  const tw=el("div"); tw.innerHTML='<label style="margin-top:0">Type</label>'; const t=document.createElement("select");t.className="v-type";["bool","number"].forEach(x=>t.appendChild(opt(x,x,x===v.type)));tw.appendChild(t);
  const dw=el("div"); dw.innerHTML='<label style="margin-top:0">Default</label>'; const dwrap=el("div","v-defwrap");dwrap.appendChild(varDefault(v.type,v.default));dw.appendChild(dwrap);
  const rw=el("div"); rw.style.flex="0 0 auto"; const rm=rmBtn(); rw.appendChild(rm);
  t.addEventListener("change",()=>{dwrap.innerHTML="";dwrap.appendChild(varDefault(t.value,null));});
  rm.addEventListener("click",()=>row.remove());
  row.appendChild(nw);row.appendChild(tw);row.appendChild(dw);row.appendChild(rw);
  return row;
}
document.getElementById("add-var").addEventListener("click",()=>vars.appendChild(varRow()));

// ---- mqtt ----
const mqtts=document.getElementById("mqtts");
function mqttRow(m){
  m=m||{topic:"",metric:"",parse:"number"};
  const row=el("div","row"); row.style.alignItems="flex-end";
  const tw=el("div"); tw.innerHTML='<label style="margin-top:0">Topic</label>'; const t=el("input","m-topic");t.value=m.topic||"";t.placeholder="sensors/tank/level";tw.appendChild(t);
  const me=el("div"); me.innerHTML='<label style="margin-top:0">Metric name</label>'; const mm=el("input","m-metric");mm.value=m.metric||"";mm.placeholder="tank_level";me.appendChild(mm);
  const pw=el("div"); pw.innerHTML='<label style="margin-top:0">Parse</label>'; const p=document.createElement("select");p.className="m-parse";PARSE.forEach(x=>p.appendChild(opt(x,x,x===m.parse)));pw.appendChild(p);
  const rw=el("div"); rw.style.flex="0 0 auto"; const rm=rmBtn(); rw.appendChild(rm); rm.addEventListener("click",()=>row.remove());
  row.appendChild(tw);row.appendChild(me);row.appendChild(pw);row.appendChild(rw);
  return row;
}
document.getElementById("add-mqtt").addEventListener("click",()=>mqtts.appendChild(mqttRow()));

// ---- http ----
const https=document.getElementById("https");
function httpMapRow(mp){
  mp=mp||{metric:"",path:"",type:"number"};
  const row=el("div","row"); row.style.alignItems="flex-end";
  const me=el("div"); me.innerHTML='<label style="margin-top:0">Metric</label>'; const m=el("input","h-metric");m.value=mp.metric||"";m.placeholder="power_kw";me.appendChild(m);
  const pe=el("div"); pe.innerHTML='<label style="margin-top:0">JSON path</label>'; const p=el("input","h-path");p.value=mp.path||"";p.placeholder="data.current_kw";pe.appendChild(p);
  const tw=el("div"); tw.innerHTML='<label style="margin-top:0">Type</label>'; const t=document.createElement("select");t.className="h-type";PARSE.forEach(x=>t.appendChild(opt(x,x,x===mp.type)));tw.appendChild(t);
  const rw=el("div"); rw.style.flex="0 0 auto"; const rm=rmBtn(); rw.appendChild(rm); rm.addEventListener("click",()=>row.remove());
  row.appendChild(me);row.appendChild(pe);row.appendChild(tw);row.appendChild(rw);
  return row;
}
function httpCard(h){
  h=h||{url:"",interval_minutes:5,timeout:10,map:[]};
  const card=el("div","rule-card");
  card.innerHTML='<div class="row"><div><label style="margin-top:0">URL</label><input class="h-url"></div>'+
    '<div style="flex:0 0 130px"><label style="margin-top:0">Every (min)</label><input class="h-iv" type="number" min="1"></div>'+
    '<div style="flex:0 0 120px"><label style="margin-top:0">Timeout (s)</label><input class="h-to" type="number" min="1"></div></div>'+
    '<label style="margin-top:10px">Field mappings</label><div class="h-map"></div>'+
    '<div class="btnrow"><button type="button" class="secondary mini add-map">+ Add mapping</button>'+
    '<button type="button" class="danger mini rm-http">Remove input</button></div>';
  card.querySelector(".h-url").value=h.url||""; card.querySelector(".h-url").placeholder="https://meter.local/api";
  card.querySelector(".h-iv").value=h.interval_minutes!=null?h.interval_minutes:5;
  card.querySelector(".h-to").value=h.timeout!=null?h.timeout:10;
  const mapWrap=card.querySelector(".h-map");
  (h.map&&h.map.length?h.map:[null]).forEach(mp=>mapWrap.appendChild(httpMapRow(mp)));
  card.querySelector(".add-map").addEventListener("click",()=>mapWrap.appendChild(httpMapRow()));
  card.querySelector(".rm-http").addEventListener("click",()=>card.remove());
  return card;
}
document.getElementById("add-http").addEventListener("click",()=>https.appendChild(httpCard()));

// ---- computed ----
const comps=document.getElementById("comps");
function compRow(c){
  c=c||{name:"",expr:""};
  const row=el("div","row"); row.style.alignItems="flex-end";
  const nw=el("div"); nw.innerHTML='<label style="margin-top:0">Metric name</label>'; const n=el("input","co-name");n.value=c.name||"";n.placeholder="net_power";nw.appendChild(n);
  const ew=el("div"); ew.style.flex="2"; ew.innerHTML='<label style="margin-top:0">Formula</label>'; const ex=el("input","co-expr");ex.value=c.expr||"";ex.placeholder="power_kw - solar_kw";ew.appendChild(ex);
  const rw=el("div"); rw.style.flex="0 0 auto"; const rm=rmBtn(); rw.appendChild(rm); rm.addEventListener("click",()=>row.remove());
  row.appendChild(nw);row.appendChild(ew);row.appendChild(rw);
  return row;
}
document.getElementById("add-comp").addEventListener("click",()=>comps.appendChild(compRow()));

function collect(){
  const variables=[...vars.querySelectorAll(".row")].map(r=>({
    name:r.querySelector(".v-name").value.trim(),
    type:r.querySelector(".v-type").value,
    default:(r.querySelector(".v-def")||{}).value||""
  })).filter(v=>v.name);
  const mqtt_inputs=[...mqtts.querySelectorAll(".row")].map(r=>({
    topic:r.querySelector(".m-topic").value.trim(),
    metric:r.querySelector(".m-metric").value.trim(),
    parse:r.querySelector(".m-parse").value
  })).filter(m=>m.topic||m.metric);
  const http_inputs=[...https.querySelectorAll(".rule-card")].map(c=>({
    url:c.querySelector(".h-url").value.trim(),
    interval_minutes:c.querySelector(".h-iv").value,
    timeout:c.querySelector(".h-to").value,
    map:[...c.querySelectorAll(".h-map .row")].map(r=>({
      metric:r.querySelector(".h-metric").value.trim(),
      path:r.querySelector(".h-path").value.trim(),
      type:r.querySelector(".h-type").value
    })).filter(m=>m.metric)
  })).filter(h=>h.url||h.map.length);
  const computed=[...comps.querySelectorAll(".row")].map(r=>({
    name:r.querySelector(".co-name").value.trim(),
    expr:r.querySelector(".co-expr").value.trim()
  })).filter(c=>c.name||c.expr);
  return {variables,mqtt_inputs,http_inputs,computed};
}
document.getElementById("save-inputs").addEventListener("click",e=>{
  document.getElementById("inputs_json").value=JSON.stringify(collect());
});

(SRC.variables||[]).forEach(v=>vars.appendChild(varRow(v)));
(SRC.mqtt_inputs||[]).forEach(m=>mqtts.appendChild(mqttRow(m)));
(SRC.http_inputs||[]).forEach(h=>https.appendChild(httpCard(h)));
(SRC.computed||[]).forEach(c=>comps.appendChild(compRow(c)));
</script>
"""


@app.route("/inputs", methods=["GET", "POST"])
@require_auth
def inputs():
    try:
        cfg = load_raw()
    except Exception as e:
        return _config_error_page("inputs", e)
    msg = msgclass = None
    if request.method == "POST":
        try:
            payload = json.loads(request.form.get("inputs_json", "{}"))
            _apply_sources(cfg, payload)
            save_config(cfg)
            msg, msgclass = ("Inputs saved. New metrics are available to rules now; mqtt/http "
                             "values refresh on the next poll (a new MQTT subscription needs a "
                             "service restart).", "ok")
            cfg = load_raw()
        except Exception as e:
            msg, msgclass = f"Could not save: {e}", "err"
            cfg = load_raw()
    body = render_template_string(INPUTS, sources=_sources_payload(cfg))
    return page(body, page="inputs", msg=msg, msgclass=msgclass,
                title="Inputs · The Castle Fun Center")


def _rule_is_flat(rule):
    """True if the form builder can faithfully represent this rule (a single
    leaf condition, or one any/all group of leaf conditions). Nested groups,
    `not`, time windows, and hysteresis are YAML-editor only -- a rule using
    any of them opens the YAML tab so a form save can't silently drop it. The
    builder does handle enabled, between/in, changed, and per-condition for."""
    if not isinstance(rule, dict):
        return False
    if rule.get("window") is not None or rule.get("hysteresis") is not None:
        return False
    when = rule.get("when")
    if not isinstance(when, dict):
        return False
    if "not" in when:
        return False
    if "any" in when or "all" in when:
        group = when.get("any" if "any" in when else "all")
        return (isinstance(group, list) and bool(group)
                and all(_leaf_is_simple(c) for c in group))
    return _leaf_is_simple(when)


def _leaf_is_simple(c):
    """A leaf the flat form builder can edit: a plain metric condition (any
    operator, optional `for:`). A nested group dict is not metric-keyed, so it
    is not simple and routes the rule to the YAML editor."""
    return isinstance(c, dict) and "metric" in c


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
    # Loud warning if we're exposed on a non-loopback interface with no login:
    # anyone who can reach the port can read config/history and (if manual
    # control / publish are enabled) drive MQTT. Don't block — just be explicit.
    if host not in ("127.0.0.1", "localhost", "::1") and not (
            str(web.get("username") or "") and str(web.get("password") or "")):
        print(f"WARNING: web UI binding to {host} with NO authentication. "
              "Set web.username/web.password, or bind web.host to 127.0.0.1, "
              "before exposing this beyond a trusted host.")
    # Start the live MQTT console subscriber (best-effort; an unreachable broker
    # just means an empty feed until it reconnects).
    console.start(cfg)
    print(f"Web UI on http://{host}:{port}  (config: {CONFIG_PATH})")
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
