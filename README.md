# Weather → MQTT Controller

Monitors local weather from the **National Weather Service** (`api.weather.gov`)
and publishes **MQTT** messages so devices around your facility can switch on/off
based on conditions — freeze protection, cooling, securing equipment in wind,
closing vents before rain, reacting to official watches/warnings, and so on.

No API key is required. The NWS API is free and US-only.

## How it works

1. On first run it resolves your latitude/longitude to an NWS forecast grid and
   the nearest observation station (cached to `nws_location_cache.json`).
2. Every `poll_interval_minutes` it fetches current conditions and active alerts.
3. It evaluates the rules in `config.yaml`. When a rule's condition changes
   (e.g. temperature drops to/below 35°F), it publishes that rule's message to
   its MQTT topic. By default it only publishes on a *change*, so it won't spam
   the bus.

Whatever subscribes to those topics — relays, controllers, ESPHome/Tasmota
nodes, Home Assistant, PLC gateways — acts on the payloads you define.

## Requirements

- Ubuntu server with Python 3.9+
- An MQTT broker reachable on the network (e.g. Mosquitto). To install one
  locally: `sudo apt install mosquitto mosquitto-clients`

## Install

```bash
sudo mkdir -p /opt/weather-mqtt
sudo cp weather_mqtt.py config.yaml requirements.txt /opt/weather-mqtt/
cd /opt/weather-mqtt

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Configure

Edit `config.yaml`:

- **`location`** — set your facility's `latitude` / `longitude`.
- **`user_agent`** — put a real contact (email or phone). NWS requires this and
  may block requests without a valid identifier.
- **`mqtt`** — point `host`/`port` at your broker; set `username`/`password` if
  it requires auth.
- **`rules`** — define what conditions matter and what to publish (see below).

## Test before deploying

Run a single cycle without touching the broker — it just prints what it *would*
publish. Great for tuning thresholds:

```bash
./venv/bin/python weather_mqtt.py --config config.yaml --once --dry-run --verbose
```

You should see the current conditions and any rules that would fire. To watch
real messages land, subscribe in another terminal:

```bash
mosquitto_sub -h localhost -t 'facility/weather/#' -v
```

## Run as a service

```bash
sudo useradd --system --no-create-home weather   # service account
sudo chown -R weather:weather /opt/weather-mqtt   # allow writing the cache file

sudo cp weather-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weather-mqtt
journalctl -u weather-mqtt -f                      # live logs
```

## Customizing rules

Each rule watches one metric and maps a condition to an MQTT message:

```yaml
- name: freeze_protection
  when:
    metric: temperature      # which value to watch
    operator: "<="           # < <= > >= == !=
    value: 35
  topic: "facility/weather/freeze_protection"
  on_match: "ON"             # published when condition becomes true
  on_clear: "OFF"            # published when it becomes false (optional)
```

Available metrics:

| Metric | Meaning | Operators |
|---|---|---|
| `temperature` | current air temp, °F (measured if a station is nearby, else forecast) | `< <= > >= == !=` |
| `wind_speed_mph` | current wind speed, mph | `< <= > >= == !=` |
| `precipitation_probability` | chance of precip, 0–100% | `< <= > >= == !=` |
| `humidity` | relative humidity, 0–100% | `< <= > >= == !=` |
| `short_forecast` | text like "Light Snow" | `contains`, `equals` |
| `active_alert` | NWS watches/warnings | `any`, `contains`, `equals` |

`on_match` / `on_clear` payloads are sent literally, so they can be anything your
devices expect — `ON`, `1`, `CLOSE`, or even a JSON string.

## Notes & tips

- **Retained messages:** `retain: true` means the broker holds each topic's last
  value, so a device that connects later immediately gets the current state.
- **Broker restarts:** if your broker restarts and loses retained values while a
  condition is steady, set `always_publish: true` so every rule's state is
  re-sent each cycle as a heartbeat.
- **Polling frequency:** 15 minutes is plenty for NWS data (it updates roughly
  hourly). Avoid very short intervals — be a good citizen of a free API.
- **Custom logic:** rules are intentionally simple (one metric each). For
  compound conditions (e.g. cold *and* windy), use two rules and have the
  subscriber combine them, or extend `evaluate_rule()` in the script.

## Troubleshooting

- **403 from weather.gov** → your `user_agent` is missing or rejected; set a real
  contact string.
- **No alerts ever fire** → that's normal when none are active for your area; the
  `--dry-run --verbose` output shows `alerts=none`.
- **Metric `... unavailable this cycle`** → a station feed returned a gap; the
  rule's state is left unchanged until data returns.
