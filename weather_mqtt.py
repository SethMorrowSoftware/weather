#!/usr/bin/env python3
"""
weather_mqtt.py -- Monitor precipitation from the National Weather Service
(api.weather.gov) and publish MQTT messages so irrigation PLCs know when NOT
to water.

Primary job:
  - Pull measured rainfall over a rolling window (default 24h) and whether it
    is precipitating right now from the nearest NWS observation station.
  - Evaluate rules from config.yaml. The default rule says "if it is raining
    OR it has rained >= X inches in the last 24h, tell the PLCs to inhibit
    watering" by publishing a retained MQTT message.
  - Publish only when a rule's state changes (so the bus isn't spammed),
    with retain=True so a PLC that connects later immediately gets the
    current directive.

No API key is required. The NWS API is free and US-only.

Run:   python weather_mqtt.py --config config.yaml
Test:  python weather_mqtt.py --config config.yaml --once --dry-run --verbose
"""

import argparse
import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
import paho.mqtt.client as mqtt

LOG = logging.getLogger("weather_mqtt")
NWS_API = "https://api.weather.gov"
CACHE_FILE = Path("nws_location_cache.json")

# Words in NWS present-weather / textDescription that mean "it's precipitating".
# Note "freezing" is intentionally NOT here: alone it matches "Freezing Fog"
# (not precipitation). "Freezing Rain"/"Freezing Drizzle" still match via
# "rain"/"drizzle".
PRECIP_WORDS = (
    "rain", "drizzle", "shower", "thunderstorm", "sleet",
    "snow", "wintry", "ice pellets", "hail",
)
# Phrases that mean the precip is NOT falling at the station, so they must not
# trip is_raining (which would wrongly hold irrigation closed).
NOT_HERE_WORDS = ("vicinity", "in the area")

# Canonical metric catalogue: value type + the operators each accepts. This is
# the single source of truth shared by config validation here and the web UI's
# rule builder (which imports it), so the two can never drift apart.
NUMERIC_COMPARE = ("<", "<=", ">", ">=", "==", "!=")
METRIC_SPECS = {
    "is_raining":                {"type": "bool",   "ops": ("==", "!=")},
    "precip_accum_in":           {"type": "number", "ops": NUMERIC_COMPARE},
    "precipitation_probability": {"type": "number", "ops": NUMERIC_COMPARE},
    "temperature":               {"type": "number", "ops": NUMERIC_COMPARE},
    "wind_speed_mph":            {"type": "number", "ops": NUMERIC_COMPARE},
    "humidity":                  {"type": "number", "ops": NUMERIC_COMPARE},
    "short_forecast":            {"type": "text",   "ops": ("contains", "equals")},
    "active_alert":              {"type": "alert",  "ops": ("any", "contains", "equals")},
}


def _validate_condition(cond, rule_name):
    """Validate one rule condition's metric/operator/value. Raises ValueError."""
    if not isinstance(cond, dict) or "metric" not in cond:
        raise ValueError(f"rule '{rule_name}': each condition needs a 'metric'")
    metric = cond["metric"]
    spec = METRIC_SPECS.get(metric)
    if spec is None:
        raise ValueError(f"rule '{rule_name}': unknown metric '{metric}' "
                         f"(valid: {', '.join(sorted(METRIC_SPECS))})")
    op = cond.get("operator")
    if metric == "active_alert" and op in (None, "any"):
        return  # the "any active alert" form needs no value
    if op not in spec["ops"]:
        raise ValueError(f"rule '{rule_name}': operator '{op}' is not valid for "
                         f"metric '{metric}' (valid: {', '.join(spec['ops'])})")
    if "value" not in cond or cond["value"] is None:
        raise ValueError(f"rule '{rule_name}': condition on '{metric}' needs a value")
    if spec["type"] == "number" and _as_number(cond["value"], None, f"{metric} value") is None:
        raise ValueError(f"rule '{rule_name}': '{metric}' value "
                         f"{cond['value']!r} must be a number")


def _validate_rule_when(when, rule_name):
    """Validate a rule's `when` (single condition or any/all group)."""
    if isinstance(when, dict) and ("any" in when or "all" in when):
        mode = "any" if "any" in when else "all"
        group = when[mode]
        if not isinstance(group, list) or not group:
            raise ValueError(f"rule '{rule_name}': '{mode}' must be a non-empty list")
        for c in group:
            _validate_condition(c, rule_name)
    else:
        _validate_condition(when, rule_name)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Sensible floors/limits so a typo in config.yaml (or the web UI) can never put
# the monitor into a tight loop hammering the free NWS API, or hand paho an
# illegal QoS. These are clamped (with a warning) rather than fatal so the
# monitor keeps running on the last-known-good behavior.
MIN_POLL_MINUTES = 1
MIN_LOOKBACK_HOURS = 1
MAX_LOOKBACK_HOURS = 720      # 30 days; NWS observation history is limited anyway


def _as_number(value, default, name):
    """Coerce a YAML scalar to int/float, falling back to default with a warn."""
    if isinstance(value, bool):  # bool is a subclass of int; reject it explicitly
        LOG.warning("%s=%r is not a number; using %r", name, value, default)
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        s = str(value).strip()
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except (TypeError, ValueError):
        LOG.warning("%s=%r is not a number; using %r", name, value, default)
        return default


def validate_config(cfg):
    """Validate structure and sanitize/clamp numeric fields in place.

    Raises ValueError for problems that make the config unusable (missing
    sections, no coordinates, empty rules, malformed rules). Out-of-range
    numbers are clamped with a warning so a small mistake never takes the
    monitor down. Returns the same (mutated) cfg for convenience.
    """
    if not isinstance(cfg, dict):
        raise ValueError("config root must be a mapping")

    for key in ("location", "user_agent", "mqtt", "rules"):
        if key not in cfg:
            raise ValueError(f"config is missing required section: '{key}'")

    loc = cfg["location"]
    if not isinstance(loc, dict) or "latitude" not in loc or "longitude" not in loc:
        raise ValueError("config.location needs 'latitude' and 'longitude'")
    lat = _as_number(loc["latitude"], None, "location.latitude")
    lon = _as_number(loc["longitude"], None, "location.longitude")
    if lat is None or lon is None:
        raise ValueError("location.latitude/longitude must be numbers")
    if not (-90 <= lat <= 90):
        raise ValueError(f"location.latitude {lat} out of range (-90..90)")
    if not (-180 <= lon <= 180):
        raise ValueError(f"location.longitude {lon} out of range (-180..180)")
    loc["latitude"], loc["longitude"] = lat, lon

    if not cfg.get("user_agent") or not str(cfg["user_agent"]).strip():
        raise ValueError("user_agent must be set (NWS requires a real contact)")

    if not isinstance(cfg["rules"], list) or not cfg["rules"]:
        raise ValueError("'rules' must be a non-empty list")
    seen_names = set()
    for r in cfg["rules"]:
        if not isinstance(r, dict):
            raise ValueError("each rule must be a mapping")
        for req in ("name", "when", "topic", "on_match"):
            if req not in r:
                raise ValueError(f"rule '{r.get('name', '?')}' is missing '{req}'")
        name = r["name"]
        if name in seen_names:
            raise ValueError(f"duplicate rule name '{name}' (names must be unique)")
        seen_names.add(name)
        # Validate the condition(s) so one malformed rule is caught here rather
        # than blowing up mid-cycle in the monitor.
        _validate_rule_when(r["when"], name)

    # --- defaults + clamping for the forgiving numeric knobs ---
    poll = _as_number(cfg.get("poll_interval_minutes", 15), 15, "poll_interval_minutes")
    if poll < MIN_POLL_MINUTES:
        LOG.warning("poll_interval_minutes=%s is below the %d-minute floor; "
                    "clamping (be a good citizen of the free NWS API)",
                    poll, MIN_POLL_MINUTES)
        poll = MIN_POLL_MINUTES
    cfg["poll_interval_minutes"] = poll

    cfg.setdefault("always_publish", False)
    cfg["always_publish"] = bool(cfg["always_publish"])
    cfg.setdefault("state_file", "weather_state.json")

    precip = cfg.setdefault("precipitation", {})
    lb = _as_number(precip.get("lookback_hours", 24), 24, "precipitation.lookback_hours")
    lb = max(MIN_LOOKBACK_HOURS, min(MAX_LOOKBACK_HOURS, int(lb)))
    precip["lookback_hours"] = lb

    web = cfg.setdefault("web", {})
    web.setdefault("enabled", True)
    web.setdefault("host", "0.0.0.0")
    web["port"] = _clamp_port(_as_number(web.get("port", 8080), 8080, "web.port"))
    web.setdefault("username", "")     # blank = no auth (use only on trusted LAN)
    web.setdefault("password", "")

    mq = cfg["mqtt"]
    if not isinstance(mq, dict):
        raise ValueError("config.mqtt must be a mapping")
    mq.setdefault("host", "localhost")
    mq["port"] = _clamp_port(_as_number(mq.get("port", 1883), 1883, "mqtt.port"))
    mq.setdefault("username", "")
    mq.setdefault("password", "")
    mq.setdefault("client_id", "weather-mqtt-controller")
    qos = int(_as_number(mq.get("qos", 1), 1, "mqtt.qos"))
    if qos not in (0, 1, 2):
        LOG.warning("mqtt.qos=%s invalid; using 1", qos)
        qos = 1
    mq["qos"] = qos
    mq.setdefault("retain", True)
    mq["retain"] = bool(mq["retain"])
    mq.setdefault("status_topic", "")   # optional: JSON snapshot of conditions

    # --- Slack alerts (optional) ---
    slack = cfg.setdefault("slack", {})
    slack.setdefault("enabled", False)
    slack["enabled"] = bool(slack["enabled"])
    slack.setdefault("bot_token", "")      # or set SLACK_BOT_TOKEN in the env
    slack.setdefault("channel", "")        # channel name (#alerts) or ID (C0…)
    mins = _as_number(slack.get("broker_unreachable_minutes", 60), 60,
                      "slack.broker_unreachable_minutes")
    slack["broker_unreachable_minutes"] = max(1, int(mins))

    # Payloads must be strings. Unquoted ON/OFF/YES/NO in YAML parse as
    # booleans -- coerce and warn so a PLC never gets "True" by surprise.
    for r in cfg["rules"]:
        for k in ("on_match", "on_clear"):
            if k in r and not isinstance(r[k], str):
                if isinstance(r[k], bool):
                    LOG.warning("Rule '%s': %s=%r looks like an unquoted YAML "
                                "boolean (ON/OFF/YES/NO). Quote it in config.yaml "
                                "to publish the literal text.", r.get("name"), k, r[k])
                r[k] = str(r[k])
    return cfg


def _clamp_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return 8080
    return port if 1 <= port <= 65535 else 8080


def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    return validate_config(cfg)


# ---------------------------------------------------------------------------
# NWS / weather.gov client
# ---------------------------------------------------------------------------
def nws_get(url, user_agent, retries=3, timeout=20):
    """GET a weather.gov endpoint with the required User-Agent + retries.

    Retries transient failures (network errors, 5xx, 429) with exponential
    backoff. A non-retryable client error (e.g. 400/403/404) fails fast --
    retrying a rejected User-Agent or a bad station id only wastes time and
    pesters a free API.
    """
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    delay = 2
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError as e:
                    raise RuntimeError(f"NWS returned non-JSON for {url}: {e}")
            retryable = r.status_code == 429 or r.status_code >= 500
            LOG.warning("NWS %s returned HTTP %s (attempt %d/%d)%s",
                        url, r.status_code, attempt, retries,
                        "" if retryable else " -- not retrying")
            if not retryable:
                raise RuntimeError(
                    f"NWS request rejected with HTTP {r.status_code}: {url}")
        except requests.RequestException as e:
            LOG.warning("NWS request error for %s: %s (attempt %d/%d)",
                        url, e, attempt, retries)
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"NWS request failed after {retries} attempts: {url}")


def resolve_location(lat, lon, user_agent, station_override=None):
    """Resolve lat/lon -> forecast grid + nearest station. Cached to disk."""
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            if (cached.get("lat") == lat and cached.get("lon") == lon
                    and cached.get("station_override") == station_override):
                LOG.info("Using cached NWS location data")
                return cached
        except Exception:
            pass  # fall through and re-resolve

    LOG.info("Resolving NWS grid point for %s,%s ...", lat, lon)
    points = nws_get(f"{NWS_API}/points/{lat},{lon}", user_agent)
    props = (points or {}).get("properties") or {}
    forecast_hourly = props.get("forecastHourly")
    stations_url = props.get("observationStations")
    if not forecast_hourly or not stations_url:
        raise RuntimeError(
            f"NWS /points response missing forecast/station URLs for {lat},{lon} "
            "(is the location inside US coverage?)")
    info = {
        "lat": lat,
        "lon": lon,
        "station_override": station_override,
        "forecast_hourly": forecast_hourly,
        "stations_url": stations_url,
        "grid_id": props.get("gridId"),
        "station_id": station_override,
    }
    if not station_override:
        try:
            stations = nws_get(info["stations_url"], user_agent)
            feats = stations.get("features", [])
            if feats:
                info["station_id"] = feats[0]["properties"]["stationIdentifier"]
        except Exception as e:
            LOG.warning("Could not resolve observation station: %s", e)

    CACHE_FILE.write_text(json.dumps(info))
    LOG.info("Resolved grid %s; observation station %s",
             info.get("grid_id"), info.get("station_id"))
    return info


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------
def c_to_f(c):
    return None if c is None else round((c * 9 / 5) + 32, 1)


def to_mm(value, unit_code):
    """Normalize an NWS length value (m / mm / cm) to millimeters."""
    if value is None:
        return None
    unit = (unit_code or "").split(":")[-1].lower()
    if unit in ("m", "meter", "meters"):
        return value * 1000.0
    if unit in ("cm", "centimeter", "centimeters"):
        return value * 10.0
    if unit in ("in", "inch", "inches", "[in_i]"):
        return value * 25.4
    # "mm", "millimeter", or unknown -> assume millimeters
    return float(value)


def mm_to_in(mm):
    return None if mm is None else round(mm / 25.4, 2)


# ---------------------------------------------------------------------------
# Precipitation
# ---------------------------------------------------------------------------
def _says_precip(text):
    """True if `text` names precipitation falling at the station (not nearby)."""
    t = (text or "").lower()
    if not t:
        return False
    if any(w in t for w in NOT_HERE_WORDS):
        return False  # e.g. "Showers in Vicinity" -- not at the station
    return any(word in t for word in PRECIP_WORDS)


def detect_raining(obs_props):
    """True if precipitating now, False if clearly not, None if unknown."""
    seen = False
    for w in (obs_props.get("presentWeather") or []):
        seen = True
        if w.get("inVicinity"):
            continue  # phenomenon is near, not at, the station
        if _says_precip((w.get("weather") or "") + " " + (w.get("rawString") or "")):
            return True
    text = (obs_props.get("textDescription") or "").strip()
    if text:
        seen = True
        if _says_precip(text):
            return True
    return False if seen else None


def fetch_precip_accum_in(station_id, user_agent, hours, now=None):
    """Measured precip over the last `hours`, in inches.

    Sums each hour's `precipitationLastHour`, de-duplicated into hourly
    buckets so more-frequent observations don't double-count. Returns None
    when the station reports no precipitation data at all (so a rule can
    leave its state unchanged rather than wrongly read "dry").
    """
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{NWS_API}/stations/{station_id}/observations?start={quote(start)}"
    data = nws_get(url, user_agent)
    return _accumulate_precip(data, hours, now)


def _accumulate_precip(data, hours, now):
    """Pure helper (no network) so it can be unit-tested with a fixture."""
    cutoff = now - timedelta(hours=hours)
    buckets = {}  # "YYYY-MM-DDTHH" -> max mm reported in that hour
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        ts = p.get("timestamp")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue
        plh = p.get("precipitationLastHour") or {}
        mm = to_mm(plh.get("value"), plh.get("unitCode"))
        if mm is None:
            continue
        # Bucket by the parsed UTC hour, not the raw string, so the same instant
        # written with different timezone offsets can't land in two buckets and
        # double-count.
        key = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
        buckets[key] = max(buckets.get(key, 0.0), mm)

    if not buckets:
        return None  # station did not report any precip values this window
    return mm_to_in(sum(buckets.values()))


def fetch_conditions(loc, user_agent, lookback_hours):
    """Return a dict of current weather metrics for rule evaluation."""
    metrics = {
        "temperature": None,                 # degF
        "wind_speed_mph": None,
        "precipitation_probability": None,   # % (forecast, NOT measured)
        "precip_accum_in": None,             # measured rainfall over lookback
        "is_raining": None,                  # bool: precipitating right now
        "humidity": None,                    # %
        "short_forecast": "",
        "active_alerts": [],                 # list of NWS event names
    }

    # --- Hourly forecast: US units, includes forecast precip probability ---
    try:
        hourly = nws_get(loc["forecast_hourly"], user_agent)
        period = hourly["properties"]["periods"][0]
        metrics["temperature"] = float(period["temperature"])  # degF
        metrics["short_forecast"] = period.get("shortForecast", "")

        pop = period.get("probabilityOfPrecipitation", {}).get("value")
        metrics["precipitation_probability"] = float(pop) if pop is not None else 0.0

        ws = period.get("windSpeed", "") or ""           # e.g. "10 to 15 mph"
        nums = [int(s) for s in ws.replace("to", " ").split() if s.isdigit()]
        metrics["wind_speed_mph"] = float(max(nums)) if nums else 0.0
    except Exception as e:
        LOG.warning("Hourly forecast unavailable: %s", e)

    # --- Latest measured observation: temp/humidity + is_raining now ---
    if loc.get("station_id"):
        try:
            obs = nws_get(
                f"{NWS_API}/stations/{loc['station_id']}/observations/latest",
                user_agent,
            )
            op = obs["properties"]
            t = op.get("temperature", {}).get("value")      # degC
            if t is not None:
                metrics["temperature"] = c_to_f(t)
            h = op.get("relativeHumidity", {}).get("value")  # %
            if h is not None:
                metrics["humidity"] = round(h, 1)
            metrics["is_raining"] = detect_raining(op)
        except Exception as e:
            LOG.warning("Latest observation unavailable: %s", e)

        # --- Measured precip accumulation over the lookback window ---
        try:
            metrics["precip_accum_in"] = fetch_precip_accum_in(
                loc["station_id"], user_agent, lookback_hours)
        except Exception as e:
            LOG.warning("Precip accumulation unavailable: %s", e)
    else:
        LOG.warning("No observation station resolved; precipitation metrics "
                    "(precip_accum_in, is_raining) will be unavailable")

    # --- Active NWS alerts for this point ---
    try:
        alerts = nws_get(
            f"{NWS_API}/alerts/active?point={loc['lat']},{loc['lon']}",
            user_agent,
        )
        events = []
        for feat in alerts.get("features", []):
            ev = feat.get("properties", {}).get("event")
            if ev:
                events.append(ev)
        metrics["active_alerts"] = events
    except Exception as e:
        LOG.warning("Alerts unavailable: %s", e)

    return metrics


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------
NUMERIC_OPS = {
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _eval_condition(cond, metrics, rule_name):
    """One condition: True/False, or None if its metric is unavailable."""
    metric = cond["metric"]
    op = cond.get("operator")
    value = cond.get("value")

    # Special metric: active NWS alerts
    if metric == "active_alert":
        alerts = metrics.get("active_alerts", [])
        if op in (None, "any"):
            return len(alerts) > 0
        if op == "contains":
            return any(str(value).lower() in a.lower() for a in alerts)
        if op == "equals":
            return any(a == value for a in alerts)
        LOG.warning("Rule '%s': unknown alert operator '%s'", rule_name, op)
        return False

    # Text metric: short forecast
    if metric == "short_forecast":
        text = metrics.get("short_forecast", "") or ""
        if op == "contains":
            return str(value).lower() in text.lower()
        if op == "equals":
            return text.lower() == str(value).lower()
        LOG.warning("Rule '%s': unknown text operator '%s'", rule_name, op)
        return False

    # Numeric / boolean metrics
    current = metrics.get(metric)
    if current is None:
        LOG.warning("Rule '%s': metric '%s' unavailable this cycle",
                    rule_name, metric)
        return None
    fn = NUMERIC_OPS.get(op)
    if fn is None:
        LOG.warning("Rule '%s': unknown operator '%s'", rule_name, op)
        return None
    return fn(current, value)


def evaluate_rule(rule, metrics):
    """Evaluate a rule's `when`.

    `when` may be a single condition dict {metric, operator, value}, or a
    compound {any: [...]} / {all: [...]}. Returns True, False, or None
    (metric(s) unavailable -> caller leaves state unchanged).
    """
    when = rule["when"]
    name = rule["name"]

    if isinstance(when, dict) and ("any" in when or "all" in when):
        mode = "any" if "any" in when else "all"
        results = [_eval_condition(c, metrics, name) for c in when[mode]]
        if mode == "any":
            if any(r is True for r in results):
                return True
            if any(r is None for r in results):
                return None      # could still be true if missing data returns
            return False
        else:  # all
            if any(r is False for r in results):
                return False
            if any(r is None for r in results):
                return None
            return True

    return _eval_condition(when, metrics, name)


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
def make_mqtt_client(mq):
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=mq["client_id"],
    )
    if mq.get("username"):
        client.username_pw_set(mq["username"], mq.get("password", ""))

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("MQTT connect failed: %s", reason_code)
        else:
            LOG.info("Connected to MQTT broker %s:%s", mq["host"], mq["port"])

    def on_disconnect(client, userdata, flags, reason_code, properties):
        LOG.warning("Disconnected from MQTT broker (%s); auto-reconnecting",
                    reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


# ---------------------------------------------------------------------------
# Slack alerting (broker-unreachable)
# ---------------------------------------------------------------------------
class BrokerWatch:
    """Tracks how long the MQTT broker has been unreachable and decides when to
    fire a Slack alert (once on threshold breach) and a recovery notice.

    Pure/​deterministic (takes `now` as an argument) so it's unit-testable
    without sleeping or a real clock. `update()` returns one of:
      "down"      -> broker has been down past the threshold; alert now
      "recovered" -> broker is back after we had alerted; send the all-clear
      None        -> nothing to announce
    """

    def __init__(self, threshold_minutes=60):
        self.threshold = timedelta(minutes=max(1, int(threshold_minutes)))
        self.down_since = None
        self.alerted = False

    def update(self, connected, now):
        if connected:
            recovered = self.alerted
            self.down_since = None
            self.alerted = False
            return "recovered" if recovered else None
        if self.down_since is None:
            self.down_since = now
        if not self.alerted and (now - self.down_since) >= self.threshold:
            self.alerted = True
            return "down"
        return None

    def downtime_minutes(self, now):
        if self.down_since is None:
            return 0
        return int((now - self.down_since).total_seconds() // 60)


def slack_token(slack):
    """Bot token from the env (preferred) or config. Env wins so the secret can
    stay out of config.yaml."""
    return os.environ.get("SLACK_BOT_TOKEN") or (slack.get("bot_token") or "")


def notify_slack(slack, text):
    """Post a message to Slack via chat.postMessage. Best-effort: never raises."""
    if not slack or not slack.get("enabled"):
        return False
    token = slack_token(slack)
    channel = slack.get("channel", "")
    if not token or not channel:
        LOG.warning("Slack alert wanted but bot token or channel is not set "
                    "(set slack.channel and SLACK_BOT_TOKEN or slack.bot_token)")
        return False
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            LOG.warning("Slack alert rejected: %s", data.get("error"))
            return False
        LOG.info("Slack alert sent to %s", channel)
        return True
    except Exception as e:
        LOG.warning("Slack alert failed to send: %s", e)
        return False


# ---------------------------------------------------------------------------
# State snapshot (consumed by the web UI)
# ---------------------------------------------------------------------------
def write_state(path, metrics, rule_rows, lookback, connected):
    """Atomically write a JSON snapshot of the latest cycle for the web UI."""
    snapshot = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_hours": lookback,
        "mqtt_connected": connected,
        "metrics": metrics,
        "rules": rule_rows,
    }
    try:
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2))
        tmp.replace(path)
    except Exception as e:
        LOG.warning("Could not write state file %s: %s", path, e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Precipitation-driven MQTT controller (NWS / weather.gov)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true",
                        help="Run a single poll then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate rules and log, but don't publish MQTT")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    ua = cfg["user_agent"]
    lat = cfg["location"]["latitude"]
    lon = cfg["location"]["longitude"]
    station_override = cfg["location"].get("station_id")
    mq = cfg["mqtt"]

    stop = {"flag": False}

    def handle_sig(signum, frame):
        LOG.info("Signal %s received, shutting down ...", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    def interruptible_sleep(seconds):
        slept = 0
        while slept < seconds and not stop["flag"]:
            time.sleep(min(5, seconds - slept))
            slept += 5

    # Resolve location with backoff instead of crashing if NWS is unreachable at
    # boot -- otherwise systemd would restart us into a tight crash-loop during
    # an outage. Stays inside the process so SIGTERM still stops us promptly.
    loc = None
    delay = 5
    while not stop["flag"]:
        try:
            loc = resolve_location(lat, lon, ua, station_override)
            break
        except Exception as e:
            LOG.error("Location resolution failed (%s); retrying in %ds", e, delay)
            interruptible_sleep(delay)
            delay = min(delay * 2, 300)
    if loc is None:
        LOG.info("Stopped before location was resolved.")
        return

    client = None
    if not args.dry_run:
        client = make_mqtt_client(mq)
        client.connect_async(mq["host"], int(mq["port"]), keepalive=60)
        client.loop_start()

    last_state = {}            # rule name -> bool
    last_change = {}           # rule name -> iso timestamp of last published change
    broker_watch = BrokerWatch(cfg["slack"]["broker_unreachable_minutes"])

    while not stop["flag"]:
        # Reload config each cycle so web-UI edits to rules / thresholds /
        # interval take effect without a restart. Location & MQTT connection
        # are fixed at startup (changing those needs a restart).
        try:
            cfg = load_config(args.config)
        except Exception as e:
            LOG.error("Config reload failed, keeping previous: %s", e)
        lookback = cfg["precipitation"]["lookback_hours"]
        interval = max(MIN_POLL_MINUTES, cfg["poll_interval_minutes"]) * 60
        rules = cfg["rules"]
        state_file = cfg["state_file"]
        # Connection params (host/port/user/client_id) are fixed at startup, but
        # qos/retain/status_topic are publish-time options we can honor live so
        # web-UI edits to them take effect on the next cycle without a restart.
        mq_live = cfg["mqtt"]
        qos, retain = mq_live["qos"], mq_live["retain"]
        status_topic = mq_live.get("status_topic", "")

        try:
            m = fetch_conditions(loc, ua, lookback)
            LOG.info("Conditions: temp=%s F  humidity=%s%%  wind=%s mph  "
                     "raining=%s  precip_%dh=%s in  precip_prob=%s%%  '%s'  "
                     "alerts=%s",
                     m["temperature"], m["humidity"], m["wind_speed_mph"],
                     m["is_raining"], lookback, m["precip_accum_in"],
                     m["precipitation_probability"], m["short_forecast"],
                     m["active_alerts"] or "none")

            if client is not None and status_topic:
                client.publish(status_topic, json.dumps(m),
                               qos=qos, retain=retain)

            rule_rows = []
            for rule in rules:
              try:
                result = evaluate_rule(rule, m)
                if result is not None:
                    prev = last_state.get(rule["name"])
                    changed = (prev is None) or (prev != result) or cfg["always_publish"]
                    # Assume committed unless a real publish fails below. A failed
                    # publish leaves last_state unchanged so the next cycle retries
                    # the directive instead of silently dropping a state change.
                    commit = True
                    if changed:
                        payload = rule["on_match"] if result else rule.get("on_clear", "")
                        if payload == "" and not result:
                            pass  # no clear payload configured; nothing to publish
                        else:
                            topic = rule["topic"]
                            if client is None:
                                LOG.info("[DRY-RUN] would publish '%s' -> %s "
                                         "(rule '%s', match=%s)",
                                         payload, topic, rule["name"], result)
                            else:
                                info = client.publish(topic, payload,
                                                      qos=qos, retain=retain)
                                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                                    commit = False
                                    LOG.warning("Publish to %s returned rc=%s "
                                                "(broker offline? will retry next "
                                                "cycle)", topic, info.rc)
                                else:
                                    LOG.info("Published '%s' -> %s (rule '%s', "
                                             "match=%s)", payload, topic,
                                             rule["name"], result)
                            if commit and prev != result:
                                last_change[rule["name"]] = datetime.now(
                                    timezone.utc).isoformat(timespec="seconds")
                    if commit:
                        last_state[rule["name"]] = result

                rule_rows.append({
                    "name": rule["name"],
                    "description": rule.get("description", ""),
                    "topic": rule["topic"],
                    "active": last_state.get(rule["name"]),
                    "current_payload": (rule["on_match"]
                                        if last_state.get(rule["name"]) else
                                        rule.get("on_clear", ""))
                    if last_state.get(rule["name"]) is not None else None,
                    "last_change": last_change.get(rule["name"]),
                })
              except Exception as e:
                # One malformed/erroring rule must not take down the whole
                # cycle; log it and keep evaluating the rest.
                LOG.warning("Rule '%s' failed this cycle, skipping: %s",
                            rule.get("name", "?") if isinstance(rule, dict) else rule, e)

            connected = bool(client is not None and client.is_connected())
            write_state(state_file, m, rule_rows, lookback, connected)

        except Exception as e:
            LOG.error("Poll cycle failed: %s", e)

        # Broker-reachability watch runs every cycle, independent of the weather
        # fetch above, so a Slack alert fires even during an NWS outage.
        if client is not None:
            slack_cfg = cfg.get("slack", {})
            broker_watch.threshold = timedelta(
                minutes=cfg["slack"]["broker_unreachable_minutes"])
            now = datetime.now(timezone.utc)
            trigger = broker_watch.update(client.is_connected(), now)
            if trigger == "down":
                mins = broker_watch.downtime_minutes(now)
                notify_slack(slack_cfg,
                             f":red_circle: *weather-mqtt*: MQTT broker "
                             f"`{mq['host']}:{mq['port']}` has been unreachable for "
                             f"~{mins} min. Irrigation directives are not being "
                             f"published.")
            elif trigger == "recovered":
                notify_slack(slack_cfg,
                             f":large_green_circle: *weather-mqtt*: MQTT broker "
                             f"`{mq['host']}:{mq['port']}` is reachable again.")

        if args.once:
            break

        interruptible_sleep(interval)  # so SIGTERM is handled promptly

    if client is not None:
        client.loop_stop()
        client.disconnect()
    LOG.info("Stopped.")


if __name__ == "__main__":
    main()
