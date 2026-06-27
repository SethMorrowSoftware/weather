# Precipitation → MQTT Controller

Watches rainfall from the **National Weather Service** (`api.weather.gov`) and
publishes **MQTT** messages so irrigation PLCs know when **not** to water.

The core decision: **if it is raining right now, *or* it has rained at least
*X* inches over the last *N* hours, publish a "hold watering" directive.**
PLCs (or anything else) subscribe to the topic and keep their valves closed
until conditions clear.

No API key is required. The NWS API is free and US-only.

This repo is the **server side only** — weather monitoring and MQTT publishing.
The PLC logic that consumes the messages is out of scope.

## How it works

1. On first run it resolves your latitude/longitude to an NWS forecast grid and
   the nearest observation station (cached to `nws_location_cache.json`).
2. Every `poll_interval_minutes` it fetches:
   - **measured rainfall** over the rolling `lookback_hours` window
     (`precip_accum_in`), summed from the station's hourly observations and
     de-duplicated so it never double-counts;
   - whether it is **precipitating right now** (`is_raining`), from the
     station's current present-weather;
   - plus temperature, humidity, wind, forecast precip probability, and active
     NWS alerts.
3. It evaluates the rules in `config.yaml`. The default `irrigation_rain_inhibit`
   rule fires when `is_raining` **OR** `precip_accum_in >= threshold`, and
   publishes `INHIBIT` (else `ALLOW`) to `irrigation/rain_inhibit`.
4. Messages publish **only on a state change** (so the bus isn't spammed) and
   are **retained**, so a PLC that connects later immediately gets the current
   directive.

### Why measured rainfall, not "chance of rain"

`precipitation_probability` from the forecast is only a *chance* and says
nothing about what already fell. For "has it rained X in the last 24h" you need
**measured** accumulation, which is what `precip_accum_in` provides from real
station observations. The probability metric is still available if you want it.

## Requirements

- Ubuntu/Linux server with Python 3.9+
- An MQTT broker reachable on the network (e.g. Mosquitto). To install one
  locally: `sudo apt install mosquitto mosquitto-clients`

## Install

```bash
sudo mkdir -p /opt/weather-mqtt
sudo cp weather_mqtt.py webui.py config.yaml requirements.txt /opt/weather-mqtt/
cd /opt/weather-mqtt

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Configure

Edit `config.yaml` (or use the web UI below):

- **`location`** — set your facility's `latitude` / `longitude`. Optionally pin
  `station_id` if the nearest station doesn't report precipitation (list
  candidates at `https://api.weather.gov/points/<lat>,<lon>/stations`).
- **`user_agent`** — put a real contact (email or phone). NWS requires this and
  may block requests without a valid identifier.
- **`precipitation.lookback_hours`** — the rolling window for measured rainfall.
- **`mqtt`** — point `host`/`port` at your broker; set `username`/`password` if
  needed; optional `status_topic` publishes a JSON snapshot each cycle.
- **`rules`** — the first rule is the irrigation inhibit; tune its threshold.

## Test before deploying

Run a single cycle without touching the broker — it prints what it *would*
publish, including the measured rainfall it read:

```bash
./venv/bin/python weather_mqtt.py --config config.yaml --once --dry-run --verbose
```

Watch real messages land from another terminal:

```bash
mosquitto_sub -h localhost -t 'irrigation/#' -v
```

Run the offline logic tests (no network needed):

```bash
./venv/bin/python test_weather_mqtt.py
```

## Web interface

`webui.py` is an optional Flask dashboard + config editor.

```bash
./venv/bin/python webui.py --config config.yaml
# then open http://<server>:8080
```

- **Dashboard** — live conditions, the current irrigation directive
  (INHIBIT / ALLOW), every rule's state, and MQTT connection status. It reads
  the snapshot the monitor writes to `weather_state.json` each cycle.
- **Settings** — friendly form for location, station, user-agent, poll interval,
  rain lookback window, and MQTT broker.
- **Rules** — YAML editor for the rule list, validated before saving.

Edits to thresholds, the lookback window, the poll interval, and rules take
effect on the **next poll cycle** with no restart (the monitor reloads
`config.yaml` each cycle). Changing **location** or **MQTT connection** settings
requires restarting the monitor service.

Configure it under the `web:` section of `config.yaml` (`enabled`, `host`,
`port`, and optional `username`/`password` for basic auth). It uses Flask's
development server — fine for a trusted control network. To expose it more
broadly, put it behind nginx/Caddy and enable auth.

## Run as services

```bash
sudo useradd --system --no-create-home weather   # service account
sudo chown -R weather:weather /opt/weather-mqtt   # allow writing cache/state

# Monitor
sudo cp weather-mqtt.service /etc/systemd/system/
# Web UI (optional)
sudo cp weather-webui.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now weather-mqtt
sudo systemctl enable --now weather-webui          # optional
journalctl -u weather-mqtt -f                       # live logs
```

## Customizing rules

Each rule publishes `on_match`/`on_clear` to a `topic`. A rule's `when` is
either one condition, or several combined with `any` (OR) / `all` (AND):

```yaml
- name: irrigation_rain_inhibit
  when:
    any:
      - { metric: is_raining,      operator: "==", value: true }
      - { metric: precip_accum_in, operator: ">=", value: 0.25 }   # inches
  topic: "irrigation/rain_inhibit"
  on_match: "INHIBIT"        # published when wet
  on_clear: "ALLOW"          # published when dry again
```

Available metrics:

| Metric | Meaning | Operators |
|---|---|---|
| `is_raining` | precipitating right now (true/false) | `== !=` |
| `precip_accum_in` | **measured** rainfall over `lookback_hours`, inches | `< <= > >= == !=` |
| `precipitation_probability` | **forecast** chance of precip, 0–100% | `< <= > >= == !=` |
| `temperature` | current air temp, °F (measured if a station is nearby) | `< <= > >= == !=` |
| `wind_speed_mph` | current wind speed, mph | `< <= > >= == !=` |
| `humidity` | relative humidity, 0–100% | `< <= > >= == !=` |
| `short_forecast` | text like "Light Rain" | `contains`, `equals` |
| `active_alert` | NWS watches/warnings | `any`, `contains`, `equals` |

`on_match` / `on_clear` payloads are sent literally, so they can be anything
your PLCs expect — `INHIBIT`, `1`, `STOP`, or even a JSON string.

## Notes & tips

- **Fail-safe behavior:** if a metric is unavailable a cycle (station gap,
  network blip), the affected rule's state is left **unchanged** — the last
  retained directive stands rather than flipping to a wrong value.
- **Pick a station that reports precip.** Some ASOS/AWOS stations don't report
  hourly precipitation. If `precip_accum_in` is always `None` in `--dry-run`,
  set `location.station_id` to a nearby station that does.
- **Retained messages:** `retain: true` means the broker holds each topic's last
  value, so a PLC that connects later immediately gets the current state.
- **Broker restarts:** set `always_publish: true` to re-send every rule's state
  each cycle as a heartbeat if your broker may drop retained values.
- **Polling frequency:** 15 minutes is plenty — NWS observations update roughly
  hourly. Be a good citizen of a free API.

## Troubleshooting

- **403 from weather.gov** → your `user_agent` is missing or rejected; set a real
  contact string.
- **`precip_accum_in` is always null** → the station doesn't report hourly
  precipitation; pin a different `station_id`.
- **Web UI shows "No status yet"** → start `weather_mqtt.py`; it writes
  `weather_state.json` on its first poll.
- **Metric `... unavailable this cycle`** → a feed returned a gap; the rule's
  state is left unchanged until data returns.
