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
PRECIP_WORDS = (
    "rain", "drizzle", "shower", "thunderstorm", "sleet",
    "snow", "wintry", "ice pellets", "hail", "freezing",
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    for key in ("location", "user_agent", "mqtt", "rules"):
        if key not in cfg:
            raise ValueError(f"config is missing required section: '{key}'")
    loc = cfg["location"]
    if "latitude" not in loc or "longitude" not in loc:
        raise ValueError("config.location needs 'latitude' and 'longitude'")

    cfg.setdefault("poll_interval_minutes", 15)
    cfg.setdefault("always_publish", False)
    cfg.setdefault("state_file", "weather_state.json")

    precip = cfg.setdefault("precipitation", {})
    precip.setdefault("lookback_hours", 24)

    web = cfg.setdefault("web", {})
    web.setdefault("enabled", True)
    web.setdefault("host", "0.0.0.0")
    web.setdefault("port", 8080)
    web.setdefault("username", "")     # blank = no auth (use only on trusted LAN)
    web.setdefault("password", "")

    mq = cfg["mqtt"]
    mq.setdefault("host", "localhost")
    mq.setdefault("port", 1883)
    mq.setdefault("username", "")
    mq.setdefault("password", "")
    mq.setdefault("client_id", "weather-mqtt-controller")
    mq.setdefault("qos", 1)
    mq.setdefault("retain", True)
    mq.setdefault("status_topic", "")   # optional: JSON snapshot of conditions

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


# ---------------------------------------------------------------------------
# NWS / weather.gov client
# ---------------------------------------------------------------------------
def nws_get(url, user_agent, retries=3, timeout=20):
    """GET a weather.gov endpoint with the required User-Agent + retries."""
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    delay = 2
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            LOG.warning("NWS %s returned HTTP %s (attempt %d/%d)",
                        url, r.status_code, attempt, retries)
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
    props = points["properties"]
    info = {
        "lat": lat,
        "lon": lon,
        "station_override": station_override,
        "forecast_hourly": props["forecastHourly"],
        "stations_url": props["observationStations"],
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
    # "mm", "millimeter", or unknown -> assume millimeters
    return float(value)


def mm_to_in(mm):
    return None if mm is None else round(mm / 25.4, 2)


# ---------------------------------------------------------------------------
# Precipitation
# ---------------------------------------------------------------------------
def detect_raining(obs_props):
    """True if precipitating now, False if clearly not, None if unknown."""
    seen = False
    for w in (obs_props.get("presentWeather") or []):
        seen = True
        weather = (w.get("weather") or "").lower()
        if any(word in weather for word in PRECIP_WORDS):
            return True
    text = (obs_props.get("textDescription") or "").strip()
    if text:
        seen = True
        if any(word in text.lower() for word in PRECIP_WORDS):
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
        key = ts[:13]  # hour bucket
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

    loc = resolve_location(lat, lon, ua, station_override)

    client = None
    if not args.dry_run:
        client = make_mqtt_client(mq)
        client.connect_async(mq["host"], int(mq["port"]), keepalive=60)
        client.loop_start()

    last_state = {}            # rule name -> bool
    last_change = {}           # rule name -> iso timestamp of last published change
    stop = {"flag": False}

    def handle_sig(signum, frame):
        LOG.info("Signal %s received, shutting down ...", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    while not stop["flag"]:
        # Reload config each cycle so web-UI edits to rules / thresholds /
        # interval take effect without a restart. Location & MQTT connection
        # are fixed at startup (changing those needs a restart).
        try:
            cfg = load_config(args.config)
        except Exception as e:
            LOG.error("Config reload failed, keeping previous: %s", e)
        lookback = cfg["precipitation"]["lookback_hours"]
        interval = cfg["poll_interval_minutes"] * 60
        rules = cfg["rules"]
        state_file = cfg["state_file"]

        try:
            m = fetch_conditions(loc, ua, lookback)
            LOG.info("Conditions: temp=%s F  humidity=%s%%  wind=%s mph  "
                     "raining=%s  precip_%dh=%s in  precip_prob=%s%%  '%s'  "
                     "alerts=%s",
                     m["temperature"], m["humidity"], m["wind_speed_mph"],
                     m["is_raining"], lookback, m["precip_accum_in"],
                     m["precipitation_probability"], m["short_forecast"],
                     m["active_alerts"] or "none")

            if client is not None and mq.get("status_topic"):
                client.publish(mq["status_topic"], json.dumps(m),
                               qos=mq["qos"], retain=mq["retain"])

            rule_rows = []
            for rule in rules:
                result = evaluate_rule(rule, m)
                payload = None
                if result is not None:
                    prev = last_state.get(rule["name"])
                    changed = (prev is None) or (prev != result) or cfg["always_publish"]
                    if changed:
                        payload = rule["on_match"] if result else rule.get("on_clear", "")
                        if payload == "" and not result:
                            payload = None  # no clear payload configured
                        else:
                            topic = rule["topic"]
                            if client is None:
                                LOG.info("[DRY-RUN] would publish '%s' -> %s "
                                         "(rule '%s', match=%s)",
                                         payload, topic, rule["name"], result)
                            else:
                                client.publish(topic, payload,
                                               qos=mq["qos"], retain=mq["retain"])
                                LOG.info("Published '%s' -> %s (rule '%s', match=%s)",
                                         payload, topic, rule["name"], result)
                            if prev != result:
                                last_change[rule["name"]] = datetime.now(
                                    timezone.utc).isoformat(timespec="seconds")
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

            connected = bool(client is not None and client.is_connected())
            write_state(state_file, m, rule_rows, lookback, connected)

        except Exception as e:
            LOG.error("Poll cycle failed: %s", e)

        if args.once:
            break

        slept = 0  # interruptible sleep so SIGTERM is handled promptly
        while slept < interval and not stop["flag"]:
            time.sleep(min(5, interval - slept))
            slept += 5

    if client is not None:
        client.loop_stop()
        client.disconnect()
    LOG.info("Stopped.")


if __name__ == "__main__":
    main()
