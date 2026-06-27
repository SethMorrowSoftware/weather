# Precipitation → MQTT Controller

[![tests](https://github.com/SethMorrowSoftware/weather/actions/workflows/tests.yml/badge.svg)](https://github.com/SethMorrowSoftware/weather/actions/workflows/tests.yml)

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

## Quick start (one command)

On a fresh **Debian/Ubuntu** server, this installs Mosquitto and the controller,
runs a short setup wizard, and starts everything as systemd services:

```bash
git clone https://github.com/SethMorrowSoftware/weather.git
cd weather
sudo ./install.sh
```

The wizard asks for your latitude/longitude and an NWS contact, then writes
`config.yaml`. When it finishes, the dashboard is live at
`http://<server-ip>:8080`. That's it.

`install.sh` is idempotent (safe to re-run) and:

- installs `python3-venv`, `mosquitto`, and `mosquitto-clients`;
- creates a `weather` service account and a virtualenv in `/opt/weather-mqtt`;
- writes a local Mosquitto listener (`localhost:1883`) the controller uses;
- installs and starts the `weather-mqtt` and `weather-webui` services.

Override locations with env vars, e.g.
`sudo INSTALL_DIR=/srv/weather SERVICE_USER=weatherbot ./install.sh`, or skip the
broker setup (already have one?) with `sudo SETUP_MOSQUITTO=0 ./install.sh`.

## Requirements

- Ubuntu/Linux server with Python 3.9+
- An MQTT broker (e.g. Mosquitto). `install.sh` sets one up for you; otherwise
  install one with `sudo apt install mosquitto mosquitto-clients`.

## Manual / pip install

Prefer to do it by hand, or installing as a Python package?

```bash
# As a package (provides the weather-mqtt, weather-webui,
# and weather-mqtt-setup commands):
pip install .
weather-mqtt-setup            # interactive wizard -> writes config.yaml
weather-webui --config config.yaml
```

Or the classic layout:

```bash
sudo mkdir -p /opt/weather-mqtt
sudo cp weather_mqtt.py webui.py setup_wizard.py config.yaml requirements.txt /opt/weather-mqtt/
cd /opt/weather-mqtt
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python setup_wizard.py     # or edit config.yaml by hand
```

## Configure

Run the wizard (`weather-mqtt-setup` / `setup_wizard.py`) or edit `config.yaml`
directly (or use the web UI below):

- **`location`** — set your facility's `latitude` / `longitude`. Optionally pin
  `station_id` if the nearest station doesn't report precipitation (list
  candidates at `https://api.weather.gov/points/<lat>,<lon>/stations`).
- **`user_agent`** — put a real contact (email or phone). NWS requires this and
  may block requests without a valid identifier.
- **`precipitation.lookback_hours`** — the rolling window for measured rainfall.
- **`mqtt`** — point `host`/`port` at your broker; set `username`/`password` if
  needed; optional `status_topic` publishes a JSON snapshot each cycle.
- **`slack`** — optional broker-down alerts (see [Slack alerts](#slack-alerts)).
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
  (INHIBIT / ALLOW), every rule's state, and MQTT connection status. It polls
  `GET /api/state` and updates in place (no full-page reloads), reading the
  snapshot the monitor writes to `weather_state.json` each cycle.
- **Settings** — friendly, range-validated form for **location**, station,
  user-agent, poll interval, rain lookback window, the **MQTT broker** (host,
  port, credentials, client id, QoS, retain, status topic), and the **web
  interface itself** (bind host/port and the login username/password). Invalid
  values (e.g. a latitude of 999 or a QoS of 5) are rejected with a clear
  message and nothing is written.
- **Rules** — two ways to edit the rule list, both fully validated before
  saving (on error nothing is written):
  - a **form builder** (default) — add/remove rules and conditions, pick the
    metric/operator from menus with the value input adapting to the metric
    (number, true/false, or text), and choose ANY/ALL when a rule has multiple
    conditions;
  - a **YAML (advanced)** editor for power users, with a metrics/operators
    reference and an "append example rule" helper.

Two extra endpoints are available:

- `GET /api/state` — the JSON snapshot the dashboard polls (503 until the
  monitor has written its first cycle).
- `GET /healthz` — unauthenticated liveness/freshness probe for systemd or an
  uptime monitor; reports whether the config loads and how fresh the monitor's
  last update is.

Edits to thresholds, the lookback window, the poll interval, the rules, and the
**publish-time** MQTT options (QoS, retain, status topic) take effect on the
**next poll cycle** with no restart (the monitor reloads `config.yaml` each
cycle). Changing **location**, the **MQTT connection** (host/port/credentials/
client id), or any **web interface** setting requires restarting the relevant
service. Out-of-range numbers in `config.yaml` are clamped to safe values with a
warning rather than crashing the monitor (e.g. a sub-minute poll interval is
raised to the 1-minute floor so the free NWS API is never hammered).

Configure it under the `web:` section of `config.yaml` (`enabled`, `host`,
`port`, and optional `username`/`password` for basic auth). It uses Flask's
development server — fine for a trusted control network. To expose it more
broadly, put it behind nginx/Caddy and enable auth. To enable the login set
**both** `username` and `password` (the Settings form refuses a username with no
password); credentials are compared in constant time, and the UI fails **closed**
— if `config.yaml` can't be read it denies access rather than serving the editor
unauthenticated. Saved passwords are never echoed back into the page; leave a
password field blank to keep the stored value.

## Slack alerts

If the MQTT broker becomes unreachable and **stays** down past a threshold
(default 60 minutes), the monitor posts to Slack — and posts an all-clear when
the broker comes back. This catches the case where the controller is healthy but
can't deliver its directives to the PLCs.

You need a Slack **bot token** (`xoxb-…`) with the `chat:write` scope, and the
bot must be invited to the target channel.

```yaml
slack:
  enabled: true
  channel: "#irrigation-alerts"     # channel name or ID (e.g. C0123456)
  bot_token: ""                     # or, preferred, the SLACK_BOT_TOKEN env var
  broker_unreachable_minutes: 60
```

**Keep the token out of `config.yaml`** by putting it in the environment instead
(it takes precedence over the config value). For the systemd service:

```bash
sudo systemctl edit weather-mqtt
# add, under [Service]:
#   Environment="SLACK_BOT_TOKEN=xoxb-your-token"
sudo systemctl restart weather-mqtt
```

You can also enable/configure all of this from the web UI's **Settings → Slack
alerts** card (the token is never echoed back; leave it blank to keep the stored
one). Detection granularity is one poll cycle, so with the default 15-minute
poll an alert fires within ~15 minutes of crossing the threshold.

## Remote status page (read-only)

Want to see status over the internet **without exposing the broker or any
control**? Keep everything on the LAN and have the controller **push** a copy of
its status, each cycle, to a small read-only page on ordinary PHP/cPanel hosting.
It's **outbound only** — the controller POSTs to your URL, nothing reaches back
in, and the page has no controls.

Enable it in `config.yaml` or **Settings → Remote status page**:
```yaml
status_push:
  enabled: true
  url: "https://your-dashboard-domain/weather/ingest.php"
  token: "a-long-random-shared-secret"     # sent in the X-Status-Token header
```

The hosting side lives in [`cloud-status/`](cloud-status/): upload it, copy
`secret.sample.php` → `secret.php` and set the same token, and browse to it. See
[`cloud-status/README.md`](cloud-status/README.md) for the full deploy steps. The
page shows the current directive, conditions, and rule states, with a
"stale / controller offline" indicator if pushes stop.

### Static UI demo

`demo/` is a **standalone, static** copy of the interface (sample data, no
backend) you can drop onto any ordinary web host — including shared cPanel
hosting — to show off the UI without installing anything. Open `demo/index.html`
or see `demo/README.md` for deployment steps.

## Run as services (manual)

`sudo ./install.sh` already does all of this for you. To wire up systemd by hand
instead, run these from the cloned repo (the unit files assume
`/opt/weather-mqtt`):

```bash
sudo useradd --system --no-create-home weather   # service account
sudo chown -R weather:weather /opt/weather-mqtt   # allow writing cache/state

# Install the unit files (from the repo checkout)
sudo cp weather-mqtt.service /etc/systemd/system/    # monitor
sudo cp weather-webui.service /etc/systemd/system/   # web UI (optional)

sudo systemctl daemon-reload
sudo systemctl enable --now weather-mqtt
sudo systemctl enable --now weather-webui          # optional
journalctl -u weather-mqtt -f                       # live logs
```

## Updating

```bash
cd weather && git pull
sudo ./install.sh        # idempotent: refreshes code + deps, keeps your config
```

If you installed by hand, re-copy the `.py` files to your install dir, run
`./venv/bin/pip install -r requirements.txt`, then
`sudo systemctl restart weather-mqtt weather-webui`.

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

## Project layout

| Path | What it is |
|---|---|
| `weather_mqtt.py` | The monitor: polls NWS, evaluates rules, publishes MQTT, writes `weather_state.json`. |
| `webui.py` | Flask dashboard + config editor (Dashboard / Settings / Rules), `/api/state`, `/healthz`. |
| `setup_wizard.py` | Interactive first-run config generator (`weather-mqtt-setup`). |
| `install.sh` | One-command Debian/Ubuntu installer (Mosquitto + venv + services). |
| `config.yaml` | Example/active configuration (the installer writes a real one from the wizard). |
| `test_weather_mqtt.py` | Offline test suite (no network needed). |
| `weather-mqtt.service`, `weather-webui.service` | systemd unit templates. |
| `pyproject.toml` | Packaging + the `weather-mqtt` / `weather-webui` / `weather-mqtt-setup` commands. |
| `demo/` | Standalone static copy of the UI for cPanel/static hosting (see `demo/README.md`). |

## Development & tests

```bash
python test_weather_mqtt.py          # 24 offline tests, no network required
```

CI (GitHub Actions, `.github/workflows/tests.yml`) runs the suite on Python
3.9–3.12 on every push/PR, plus an **`install-smoke`** job that runs the real
`install.sh` on a clean Ubuntu VM and verifies Mosquitto and both services come
up, the dashboard responds, and the broker round-trips a message.

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
- **Validated config:** invalid configs (bad coordinates, unknown metric or
  operator, duplicate rule names, empty rules) are rejected with a clear message
  — the web UI won't save them, and on a hot reload the monitor keeps its
  last-good config. A single broken rule is isolated so it can't take down the
  rest of the cycle.
- **Robust startup:** if NWS is unreachable at boot the monitor retries with
  backoff instead of crash-looping; transient broker outages self-heal (a
  directive that failed to publish is retried next cycle).

## Troubleshooting

- **403 from weather.gov** → your `user_agent` is missing or rejected; set a real
  contact string. The monitor keeps retrying with backoff, so fix it in the UI
  (or `config.yaml`) and it recovers on the next cycle without a restart.
- **`precip_accum_in` is always null** → the station doesn't report hourly
  precipitation; pin a different `station_id`.
- **Web UI shows "No status yet"** → start `weather_mqtt.py`; it writes
  `weather_state.json` on its first poll.
- **Metric `... unavailable this cycle`** → a feed returned a gap; the rule's
  state is left unchanged until data returns.
