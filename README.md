# The Castle Fun Center · MQTT Command Center

[![tests](https://github.com/SethMorrowSoftware/mqtt-dev/actions/workflows/tests.yml/badge.svg)](https://github.com/SethMorrowSoftware/mqtt-dev/actions/workflows/tests.yml)

The on-site automation controller behind **The Castle Fun Center's** MQTT command
center — a web-configurable **conditions → actions** engine. Weather-driven
irrigation is the default preset; the same engine drives any on/off device.

Watches rainfall from the **National Weather Service** (`api.weather.gov`) and
publishes **MQTT** messages so irrigation PLCs know when **not** to water.

The core decision: **if it is raining right now, *or* it has rained at least
*X* inches over the last *N* hours, publish a "hold watering" directive.**
PLCs (or anything else) subscribe to the topic and keep their valves closed
until conditions clear.

No API key is required. The NWS API is free and US-only.

This repo is the **server side only** — monitoring and MQTT publishing. The PLC
logic that consumes the messages is out of scope.

### More than weather

Irrigation is the **default preset**, but the controller has grown into a
general **conditions → actions** engine. A rule combines any of these inputs and
publishes on/off MQTT directives, all editable from the web UI:

- **Inputs:** NWS weather, a [schedule / clock](#available-metrics) (incl.
  sunrise/sunset), [operator variables](#operator-variables-optional) you toggle
  in the UI, [MQTT sensor topics](#mqtt-sensor-inputs-optional),
  [HTTP JSON endpoints](#http-json-inputs-optional), and **computed metrics**
  derived from any of these with a formula.
- **Rules:** nested `any`/`all`/`not`, comparison + `between`/`in`/`changed`
  operators, `regex` on text, **metric-to-metric** comparison, a `for:` sustain,
  per-rule `enable`, **time windows**, and **hysteresis** (anti-short-cycle).
- **Actions:** the built-in MQTT publish, plus optional [extra actions](#extra-actions-beyond-the-built-in-publish)
  per rule — multiple/templated publishes, **webhooks**, and **Slack notify** on
  a transition.
- **Control:** opt-in, audited [manual Auto/On/Off](#manual-control-opt-in) of
  any device from the dashboard; an outbound-only
  [remote status page](#remote-status-page-read-only).

Everything below applies whether you use it for irrigation or anything else
on/off.

## How it works

1. On first run it resolves your latitude/longitude to an NWS forecast grid and
   the nearest observation station (cached to `nws_location_cache.json`).
2. Every `poll_interval_minutes` it fetches:
   - **measured rainfall** over the rolling `lookback_hours` window
     (`precip_accum_in`), summed from the station's hourly observations and
     de-duplicated so it never double-counts; if the station doesn't report the
     hourly group it falls back to the coarser 3-/6-hour synoptic totals, and if
     it is visibly raining but the station reports no gauge value at all the
     metric reads *unknown* rather than a false `0.0`;
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
git clone https://github.com/SethMorrowSoftware/mqtt-dev.git
cd mqtt-dev
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
- **Inputs** — manage the **sources** your rules draw on, without hand-editing
  `config.yaml`: declare **operator variables** (`var_<name>` flags/setpoints you
  toggle from the dashboard), subscribe to **MQTT sensor inputs** (another
  device's topic → a metric), poll **HTTP JSON inputs** (an endpoint with
  dotted-path field mappings → metrics), and define **computed metrics** (a
  formula over other metrics). Add/remove rows inline; everything is validated
  and name-collision-checked before saving, and each new source immediately
  appears as a metric in the Rules builder.
- **MQTT** — a live **MQTT console**. The web UI keeps its own broker
  subscription and shows a **live message feed** (filter by topic prefix) and a
  **Topics** view (the latest retained value per topic). A **publish console**
  sends arbitrary messages (topic, payload, QoS, retain) to the broker — handy
  for testing devices and rules. Publishing is **off by default** and
  fail-closed: enable `web.allow_mqtt_publish` *and* set a web login (it's
  LAN-only and audited). The subscription is configurable
  (`web.mqtt_console_enabled` / `mqtt_console_topics` / `mqtt_console_buffer`).
- **Activity** — a read-only audit log of every device state change (automatic
  or manual), operator action, manual MQTT publish, and **extra action that
  fired** (webhook / notify / extra publish, with an ok/failed indicator),
  newest first, in plain language.
- **History** — **trend sparklines** for every numeric metric, over a selectable
  window (6h / 24h / 3d / 7d / 30d), with one-click **CSV export** of the window.
  The monitor records each cycle's numeric metrics to a small SQLite file
  (`history.db`) and prunes to the retention window; turn it off or set retention
  under **Settings → Metric history**.
- **System** — at-a-glance health (monitor running/stale, MQTT connected,
  config valid, time since last poll), a configuration summary (rule/metric/
  input counts and the files in use), and a **live runtime log viewer** that
  tails the monitor's log with level filtering and auto-refresh. The monitor
  mirrors its log to `log_file` (default `monitor.log`, a rolling ~1 MB file
  with 3 backups) so the web UI — a separate process — can read it; set
  `log_file:` to `""` in `config.yaml` to turn it off.

Extra endpoints are available:

- `GET /api/state` — the JSON snapshot the dashboard polls (503 until the
  monitor has written its first cycle).
- `GET /api/system` — health + config summary the System page polls.
- `GET /api/logs?limit=N` — the tailed runtime log (newest first).
- `GET /api/mqtt` — the live console feed (recent messages, optionally
  `?since=<seq>` / `?topic=<prefix>` / `?topics=1` for the per-topic summary).
- `POST /api/mqtt/publish` — publish a message (fail-closed; requires
  `web.allow_mqtt_publish` + a login).
- `GET /api/history?hours=N` — metric time series for the History page.
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
password field blank to keep the stored value. State-changing requests are
protected against cross-site request forgery: a POST whose `Origin` header
names another site is rejected outright, so a malicious page can't ride the
browser's remembered login to flip devices or publish MQTT.

On a **trusted, isolated LAN** you can skip the login entirely and still use the
privileged controls: set `web.allow_anonymous_control: true` (Settings → Web
interface → Anonymous control) to let **manual control** and **MQTT publishing**
work with no username/password — the same posture as an anonymous broker. It's
off by default (fail-closed), and the cross-site guard still applies, so a remote
page still can't drive it. With it on, **anyone who can reach the page can drive
MQTT**, so only use it where the network itself is the security boundary.

## Manual control (opt-in)

By default the dashboard is **display-only** — it shows state but issues no
commands. You can optionally let an authenticated operator force any device
**On**/**Off** from the dashboard (handy for maintenance or overriding the
weather):

- Enable **Settings → Web interface → Manual device control** (or set
  `web.allow_manual_control: true`). It is **fail-closed**: it only takes effect
  when a web **login is set**, and the control endpoint always requires that
  login — if no username/password is configured, manual control stays off.
- Each device then shows **Auto / On / Off** buttons. **On**/**Off** force the
  state (overriding the rules and bypassing hysteresis — the intent is
  explicit); **Auto** hands control back to the rules.
- Overrides are **persisted** to `overrides.json`, so they survive a restart and
  apply on the next poll (no restart needed). They're an overlay on top of
  `config.yaml`, so editing rules never wipes an override.
- Every manual change and every automatic state change is appended to an
  **audit log** (`audit.log`) with a timestamp and the acting user, and is shown
  in the web UI's **Activity** page in plain language (newest first). The log
  rotates at ~5 MB (one `.1` backup) so it can't grow without bound.
- The remote status page stays **strictly read-only** — it can never issue a
  command; it only shows a "manual" indicator when a device is overridden.

## Operator variables (optional)

Declare virtual flags/setpoints you can toggle from the dashboard and reference
from rules — handy for things like a "maintenance mode" that pauses everything,
a seasonal flag, or an adjustable threshold:

```yaml
variables:
  maintenance_mode: { type: bool,   default: false }
  temp_setpoint:    { type: number, default: 70 }
```

Each becomes a metric named `var_<name>` (e.g. `var_maintenance_mode`,
`var_temp_setpoint`) that the rule builder discovers automatically. When
**manual control** is enabled, the dashboard shows a **Variables** card with a
toggle per `bool` and an input per `number`; values **persist** to
`variables.json` and apply on the next poll. Example rule:

```yaml
- name: pause_all_for_maintenance
  when: { metric: var_maintenance_mode, operator: "==", value: true }
  topic: "irrigation/rain_inhibit"
  on_match: "INHIBIT"
  on_clear: "ALLOW"
```

## MQTT sensor inputs (optional)

Subscribe to other devices' MQTT topics and use their values as rule metrics —
e.g. a tank level, a door switch, or a power meter. The controller subscribes on
the **same broker connection** it already uses to publish:

```yaml
mqtt_inputs:
  - { topic: "sensors/tank/level", metric: tank_level, parse: number }
  - { topic: "sensors/door/open",  metric: door_open,  parse: bool }
```

Each `metric` becomes a rule metric of its own (discovered by the builder
automatically), typed by `parse` (`number`, `bool`, or `string`). The latest
received value is used each poll cycle; until a value arrives the metric is
unavailable, so the rule **holds its last state** (the usual fail-safe).

```yaml
- name: low_tank_hold
  when: { metric: tank_level, operator: "<", value: 20 }
  topic: "pumps/refill"
  on_match: "ON"
  on_clear: "OFF"
```

Subscriptions are established at startup; adding/removing a `mqtt_inputs` entry
takes effect after a service restart (like the broker connection settings).

**Event-driven re-evaluation.** By default (`event_driven: true`) the controller
re-runs the rules the **instant** an `mqtt_in` value changes, instead of waiting
for the next poll — so a tank crossing a threshold or a door opening reacts in
well under a second rather than up to `poll_interval_minutes`. The slow NWS
weather fetch still happens on the poll interval (its cached values are reused
for the in-between re-evaluations), a burst of messages is debounced into one
pass, and the outbound remote-status push stays at poll cadence. Set
`event_driven: false` for strict poll-cadence behavior. (Toggle it under
**Settings → Location & polling**; it takes effect on the next restart.)

## HTTP JSON inputs (optional)

Poll a JSON HTTP endpoint on an interval and map fields into rule metrics — e.g.
a local power meter, inverter, or any device with a small JSON API:

```yaml
http_inputs:
  - url: "https://meter.local/api"
    interval_minutes: 5            # effective granularity is the poll interval
    map:
      - { metric: power_kw, path: "current_kw",     type: number }
      - { metric: grid_up,  path: "status.online",  type: bool }
```

`path` is a dotted path into the JSON (a subset of JSONPath — a leading `$.` is
optional and numeric segments index arrays, e.g. `phases.0.volts`). Each
`metric` becomes a rule metric (discovered by the builder), typed by `type`
(`number`/`bool`/`string`). Fetches are best-effort: a failed request or a
missing field leaves the metric at its last value, so rules **hold state**.

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
cd mqtt-dev && git pull
sudo ./install.sh        # idempotent: refreshes code + deps, keeps your config,
                         # and restarts both services on the new code
```

A re-run never touches your `config.yaml` or runtime state (overrides,
variables, history, audit trail), and only restarts Mosquitto if it had to
write the broker config in the first place — so connected PLCs aren't bounced
by a routine update.

If you installed by hand, re-copy the `.py` files to your install dir, run
`./venv/bin/pip install -r requirements.txt`, then
`sudo systemctl restart weather-mqtt weather-webui`.

## Customizing rules

Each rule publishes `on_match`/`on_clear` to a `topic`. A rule's `when` is
either one condition, or several combined with `any` (OR) / `all` (AND) — and
those groups can be **nested** and negated with `not` to any depth:

```yaml
- name: irrigation_rain_inhibit
  when:
    any:
      - { metric: is_raining,      operator: "==", value: true }
      - { metric: precip_accum_in, operator: ">=", value: 0.25 }   # inches
  topic: "irrigation/rain_inhibit"
  on_match: "INHIBIT"        # published when wet
  on_clear: "ALLOW"          # published when dry again

# Nested example: hold only when it's NOT freezing AND (raining OR wind is high)
- name: complex_hold
  enabled: true              # set false to leave a rule idle (no publishing)
  when:
    all:
      - not: { metric: temperature, operator: "<", value: 32 }
      - any:
          - { metric: is_raining,     operator: "==",      value: true }
          - { metric: wind_speed_mph, operator: "between", value: [20, 60] }
  topic: "facility/complex_hold"
  on_match: "1"
  on_clear: "0"
```

Available metrics:

| Metric | Meaning | Operators |
|---|---|---|
| `is_raining` | precipitating right now (true/false) | `== !=` |
| `precip_accum_in` | **measured** rainfall over `lookback_hours`, inches | `< <= > >= == != between in` |
| `precipitation_probability` | **forecast** chance of precip, 0–100% | `< <= > >= == != between in` |
| `temperature` | current air temp, °F (measured if a station is nearby) | `< <= > >= == != between in` |
| `wind_speed_mph` | current wind speed, mph | `< <= > >= == != between in` |
| `humidity` | relative humidity, 0–100% | `< <= > >= == != between in` |
| `short_forecast` | text like "Light Rain" | `contains`, `equals`, `in`, `regex` |
| `active_alert` | NWS watches/warnings | `any`, `contains`, `equals`, `regex` |
| `time_hour` | local hour, 0–23 | `< <= > >= == != between in` |
| `time_minute` | local minute, 0–59 | `< <= > >= == != between in` |
| `time_weekday` | `mon`…`sun` (local) | `equals`, `in`, `contains` |
| `time_is_weekend` | Sat/Sun (true/false) | `== !=` |
| `time_is_daytime` | sun up at your lat/long (true/false) | `== !=` |

The `time_*` metrics are computed locally each cycle (no external calls), so
rules can combine weather with the time of day — e.g. only hold irrigation
during daytime hours, or skip a rule on weekends:

```yaml
- name: daytime_weekday_hold
  when:
    all:
      - { metric: is_raining,      operator: "==",      value: true }
      - { metric: time_hour,       operator: "between", value: [6, 20] }
      - { metric: time_is_weekend, operator: "==",      value: false }
  topic: "irrigation/rain_inhibit"
  on_match: "INHIBIT"
  on_clear: "ALLOW"
```

`between` takes an inclusive `[low, high]` pair; `in` takes a list of allowed
values (e.g. `value: [30, 50, 70]`, or `["Sunny", "Clear"]` for text).

**Richer conditionals** for building a fully custom controller:

- **Compare two metrics** — use `value_metric` instead of `value` to compare a
  metric against another metric's live value (works with `< <= > >= == !=` on
  number/bool metrics). Great with operator variables as setpoints:
  ```yaml
  - { metric: tank_level, operator: "<", value_metric: var_tank_setpoint }
  ```
  If either metric is unavailable that cycle, the rule holds its last state.
- **Regex on text** — `operator: regex` matches a text metric (or any NWS alert)
  against a case-insensitive pattern:
  `{ metric: short_forecast, operator: regex, value: "^(light|heavy) rain" }`.
- **Computed (derived) metrics** — a top-level `computed:` section defines new
  number metrics from a small formula (`+ - * / // % **` and parentheses) over
  other metrics. Each becomes a first-class metric rules can use and the builder
  discovers automatically:
  ```yaml
  computed:
    net_power:  { expr: "power_kw - solar_kw" }       # references mqtt/http inputs
    temp_delta: { expr: "temperature - var_temp_setpoint" }
  ```
  References must resolve to a metric defined **before** it (built-ins,
  variables, mqtt/http inputs, or an earlier computed), which makes reference
  cycles impossible. A missing input — or a divide-by-zero — yields no value, so
  dependent rules hold their last state (fail-safe). Edit these on the **Inputs**
  page, alongside variables and sensor inputs.

Two history-aware constructs are also available on any condition:

- **`operator: changed`** (no `value`) — true on the cycle a metric's value
  differs from the previous one (e.g. `{ metric: active_alert, operator: changed }`).
- **`for: <duration>`** — the condition must hold **continuously** for that long
  before it counts as true, e.g.
  `{ metric: temperature, operator: ">", value: 85, for: "10m" }`. If the
  condition drops (or its metric becomes unavailable) the timer resets.

A rule may set `enabled: false` to leave it idle — it is not evaluated and
publishes nothing, so the broker's last retained value stands. The web UI's
**form builder** edits single-level rules (one condition or one `any`/`all`
group), including the `enabled` toggle, `between`/`in`, the `changed` operator,
and a per-condition `for:`. Rules using nested/`not` conditions, a time window,
or hysteresis are edited in the **YAML (advanced)** tab, which the Rules page
opens automatically when it detects them.

### Extra actions (beyond the built-in publish)

Besides the built-in `on_match`/`on_clear` publish to `topic`, a rule can fire
**extra actions** on a transition via an `actions:` list — drive several devices,
hit a webhook, or send a Slack message from one rule. Each action has a
`trigger` (`match` / `clear` / `both`) and is one of three kinds. Payloads,
URLs, bodies, and text support **`{{metric}}` templating** with the cycle's live
values:

```yaml
- name: vent_fan
  when: { metric: temperature, operator: ">", value: 85 }
  topic: "facility/vent_fan"
  on_match: "ON"
  on_clear: "OFF"
  actions:
    - { trigger: match, mqtt: { topic: "facility/fan2", payload: "RUN {{temperature}}", retain: true } }
    - { trigger: both,  webhook: { url: "https://hooks.example.com/vent", method: POST, body: '{"temp": {{temperature}}}' } }
    - { trigger: clear, notify: { text: "Vent fan cleared at {{temperature}}°F" } }
```

- **`mqtt`** — an extra publish (`topic`, `payload`, optional `qos`/`retain`).
- **`webhook`** — an HTTP request (`url`, `method` GET/POST/PUT, optional `body`,
  `headers`). Outbound and best-effort.
- **`notify`** — a Slack message (`text`) via the configured Slack bot.

Use **`trigger`**, not `on` (`on` is a YAML boolean and would be misread). All
actions are **best-effort** — a failed action is logged and never blocks the
cycle or changes the committed state. Edit them in the form builder's **Extra
actions** section per rule (including per-publish **QoS/retain**), or in the YAML
tab (where webhook `headers` also live).

### Time windows & hysteresis (anti-short-cycle)

Two optional per-rule layers turn a rule's evaluated *desired* state into the
*committed* state that's actually published — useful when a rule drives a real
load (pump, valve, compressor) rather than just an advisory directive:

```yaml
- name: vent_fan
  when: { metric: temperature, operator: ">", value: 85 }
  window:                      # only active 06:00–20:00, weekdays
    from: "06:00"
    to:   "20:00"              # `to` is exclusive; from > to wraps past midnight
    days: [mon, tue, wed, thu, fri]
  hysteresis:                  # don't short-cycle the fan
    min_on:  10m               # once ON, stay ON ≥ 10 min
    min_off: 5m                # once OFF, stay OFF ≥ 5 min
    cooldown: 0m               # min gap between any two switches
  topic: "facility/vent_fan"
  on_match: "ON"
  on_clear: "OFF"
```

- **`window`** — outside its hours/days the desired state is forced **OFF**.
  `from`/`to` default to the whole day; `days` defaults to every day.
- **`hysteresis`** — `min_on` / `min_off` hold the current state for at least
  that long before the opposite transition; `cooldown` is a floor between any
  two switches. Durations accept `30s`, `10m`, `2h`, or a bare number (minutes).
  Unknown inputs still hold the last state (the fail-safe is preserved).

`on_match` / `on_clear` payloads are sent literally, so they can be anything
your PLCs expect — `INHIBIT`, `1`, `STOP`, or even a JSON string.

## Project layout

| Path | What it is |
|---|---|
| `weather_mqtt.py` | The monitor: gathers inputs (weather, schedule, variables, mqtt_in, http_poll), evaluates rules, publishes MQTT, writes `weather_state.json`. |
| `webui.py` | Flask dashboard + config editor (Dashboard / Settings / Rules / Inputs / MQTT / Activity / History / System), `/api/state`, `/api/control`, `/api/variable`, `/api/audit`, `/api/system`, `/api/logs`, `/api/mqtt`, `/api/mqtt/publish`, `/api/history`, `/healthz`. |
| `setup_wizard.py` | Interactive first-run config generator (`weather-mqtt-setup`). |
| `install.sh` | One-command Debian/Ubuntu installer (Mosquitto + venv + services). |
| `config.yaml` | Example/active configuration (the installer writes a real one from the wizard). |
| `test_weather_mqtt.py` | Offline test suite (no network needed). |
| `weather-mqtt.service`, `weather-webui.service` | systemd unit templates. |
| `pyproject.toml` | Packaging + the `weather-mqtt` / `weather-webui` / `weather-mqtt-setup` commands. |
| `demo/` | Standalone static copy of the UI for cPanel/static hosting (see `demo/README.md`). |
| `cloud-status/` | Outbound read-only status mirror for PHP/cPanel hosting. |

Runtime files the monitor/UI create next to the install (git-ignored): the
`weather_state.json` snapshot, `nws_location_cache.json`, `overrides.json`
(manual device overrides), `variables.json` (operator variables), `audit.log`
(manual + automatic state-change trail), `engine_state.json` (persisted
hysteresis/`for:`/`changed` history), and `history.db` (metric trends).

## Development & tests

```bash
python test_weather_mqtt.py          # offline test suite, no network required
```

CI (GitHub Actions, `.github/workflows/tests.yml`) runs the suite on Python
3.9–3.12 on every push/PR, plus an **`install-smoke`** job that runs the real
`install.sh` on a clean Ubuntu VM and verifies Mosquitto and both services come
up, the dashboard responds, and the broker round-trips a message — then
**re-runs `install.sh`** over the live install to prove it's idempotent (config
byte-for-byte unchanged, services still healthy).

## License

MIT — see [`LICENSE`](LICENSE).

## Notes & tips

- **Fail-safe behavior:** if a metric is unavailable a cycle (station gap,
  network blip), the affected rule's state is left **unchanged** — the last
  retained directive stands rather than flipping to a wrong value.
- **Pick a station that reports precip.** Some ASOS/AWOS stations report no
  precipitation at all. The monitor falls back from the hourly group to the
  3-/6-hour totals, but if `precip_accum_in` still reads `None` (unknown) during
  rain in `--dry-run`, set `location.station_id` to a nearby station with a gauge.
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
- **`precip_accum_in` reads 0 while it's pouring** → the nearest station reports
  no gauge value (neither the hourly nor the 3-/6-hour totals). Run
  `python check_rain.py <lat> <lon>` to see the raw METARs, then pin a
  `station_id` that reports precipitation. When it's raining and the station has
  no gauge, the metric now reads *unknown* (holds last state) rather than a
  false `0.0` that would let irrigation run.
- **Web UI shows "No status yet"** → start `weather_mqtt.py`; it writes
  `weather_state.json` on its first poll.
- **Metric `... unavailable this cycle`** → a feed returned a gap; the rule's
  state is left unchanged until data returns.
