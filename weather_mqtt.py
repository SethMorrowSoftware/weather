#!/usr/bin/env python3
"""
weather_mqtt.py -- Monitor NWS (weather.gov) conditions and publish MQTT
messages so devices around a facility can react to the weather.

  - Pulls current conditions + active alerts from api.weather.gov (no API key).
  - Evaluates a set of user-defined rules from config.yaml.
  - Publishes MQTT messages when a rule's state changes (on/off, open/close...).

Run:   python weather_mqtt.py --config config.yaml
Test:  python weather_mqtt.py --config config.yaml --once --dry-run
"""

import argparse
import json
import logging
import signal
import time
from pathlib import Path

import requests
import yaml
import paho.mqtt.client as mqtt

LOG = logging.getLogger("weather_mqtt")
NWS_API = "https://api.weather.gov"
CACHE_FILE = Path("nws_location_cache.json")


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

    mq = cfg["mqtt"]
    mq.setdefault("host", "localhost")
    mq.setdefault("port", 1883)
    mq.setdefault("username", "")
    mq.setdefault("password", "")
    mq.setdefault("client_id", "weather-mqtt-controller")
    mq.setdefault("qos", 1)
    mq.setdefault("retain", True)
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


def resolve_location(lat, lon, user_agent):
    """Resolve lat/lon -> forecast grid + nearest station. Cached to disk."""
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            if cached.get("lat") == lat and cached.get("lon") == lon:
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
        "forecast_hourly": props["forecastHourly"],
        "stations_url": props["observationStations"],
        "grid_id": props.get("gridId"),
        "station_id": None,
    }
    try:
        stations = nws_get(info["stations_url"], user_agent)
        feats = stations.get("features", [])
        if feats:
            info["station_id"] = feats[0]["properties"]["stationIdentifier"]
    except Exception as e:
        LOG.warning("Could not resolve observation station: %s", e)

    CACHE_FILE.write_text(json.dumps(info))
    LOG.info("Resolved grid %s; nearest station %s",
             info.get("grid_id"), info.get("station_id"))
    return info


def c_to_f(c):
    return None if c is None else round((c * 9 / 5) + 32, 1)


def fetch_conditions(loc, user_agent):
    """Return a dict of current weather metrics + active alerts."""
    metrics = {
        "temperature": None,                 # degF
        "wind_speed_mph": None,
        "precipitation_probability": None,   # %
        "humidity": None,                    # %
        "short_forecast": "",
        "is_daytime": None,
        "active_alerts": [],                 # list of NWS event names
    }

    # --- Hourly forecast: reliable US units, includes precip probability ---
    try:
        hourly = nws_get(loc["forecast_hourly"], user_agent)
        period = hourly["properties"]["periods"][0]
        metrics["temperature"] = float(period["temperature"])  # degF
        metrics["short_forecast"] = period.get("shortForecast", "")
        metrics["is_daytime"] = period.get("isDaytime")

        pop = period.get("probabilityOfPrecipitation", {}).get("value")
        metrics["precipitation_probability"] = float(pop) if pop is not None else 0.0

        ws = period.get("windSpeed", "") or ""           # e.g. "10 to 15 mph"
        nums = [int(s) for s in ws.replace("to", " ").split() if s.isdigit()]
        metrics["wind_speed_mph"] = float(max(nums)) if nums else 0.0
    except Exception as e:
        LOG.warning("Hourly forecast unavailable: %s", e)

    # --- Latest measured observation overrides forecast temp/humidity ---
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
        except Exception as e:
            LOG.warning("Latest observation unavailable: %s", e)

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


def evaluate_rule(rule, metrics):
    """True if condition met, False if not, None if the metric is unavailable."""
    cond = rule["when"]
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
        LOG.warning("Rule '%s': unknown alert operator '%s'", rule["name"], op)
        return False

    # Text metric: short forecast
    if metric == "short_forecast":
        text = metrics.get("short_forecast", "") or ""
        if op == "contains":
            return str(value).lower() in text.lower()
        if op == "equals":
            return text.lower() == str(value).lower()
        LOG.warning("Rule '%s': unknown text operator '%s'", rule["name"], op)
        return False

    # Numeric metrics
    current = metrics.get(metric)
    if current is None:
        LOG.warning("Rule '%s': metric '%s' unavailable this cycle",
                    rule["name"], metric)
        return None
    fn = NUMERIC_OPS.get(op)
    if fn is None:
        LOG.warning("Rule '%s': unknown operator '%s'", rule["name"], op)
        return None
    return fn(current, value)


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
# Main loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Weather-driven MQTT controller (NWS / weather.gov)")
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
    interval = cfg["poll_interval_minutes"] * 60
    mq = cfg["mqtt"]
    rules = cfg["rules"]

    loc = resolve_location(lat, lon, ua)

    client = None
    if not args.dry_run:
        client = make_mqtt_client(mq)
        client.connect_async(mq["host"], int(mq["port"]), keepalive=60)
        client.loop_start()

    last_state = {}            # rule name -> bool
    stop = {"flag": False}

    def handle_sig(signum, frame):
        LOG.info("Signal %s received, shutting down ...", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    while not stop["flag"]:
        try:
            m = fetch_conditions(loc, ua)
            LOG.info("Conditions: temp=%s F  humidity=%s%%  wind=%s mph  "
                     "precip=%s%%  '%s'  alerts=%s",
                     m["temperature"], m["humidity"], m["wind_speed_mph"],
                     m["precipitation_probability"], m["short_forecast"],
                     m["active_alerts"] or "none")

            for rule in rules:
                result = evaluate_rule(rule, m)
                if result is None:
                    continue  # metric missing this cycle; leave state unchanged

                prev = last_state.get(rule["name"])
                changed = (prev is None) or (prev != result) or cfg["always_publish"]
                if changed:
                    payload = rule["on_match"] if result else rule.get("on_clear", "")
                    if payload == "" and not result:
                        last_state[rule["name"]] = result  # no clear payload set
                        continue
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
                last_state[rule["name"]] = result

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
