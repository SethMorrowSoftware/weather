# The Castle Fun Center · MQTT Command Center — UI demo

A **static, self-contained** preview of the controller's web interface
(`webui.py`). It uses sample data and runs entirely in the browser — nothing is
published to MQTT and nothing is saved. It exists to show off the UI on any
ordinary web host (e.g. cPanel) without installing Python, Flask, or MQTT.

## What's inside

```
demo/
├── index.html      Dashboard  — conditions, device states, a Variables card,
│                                and manual Auto/On/Off controls
├── settings.html   Settings   — full config form with in-browser validation
│                                (incl. Slack, remote status, manual control)
├── rules.html      Rules      — form rule builder + YAML editor (both validated)
├── inputs.html     Inputs     — sources editor: operator variables, MQTT sensor
│                                inputs, HTTP JSON inputs, and computed metrics
├── mqtt.html       MQTT       — live console: simulated topic feed, a Topics view,
│                                and a publish box (toast only in the demo)
├── activity.html   Activity   — read-only audit log of state changes (sample data)
├── history.html    History    — trend sparklines per numeric metric (sample data)
├── system.html     System     — monitor/MQTT health, config summary, and a
│                                runtime log viewer with level filtering (sample data)
└── assets/
    ├── style.css   Shared theme (mirrors the live app)
    └── app.js      Mock data + interactivity (no backend, no dependencies)
```

It mirrors the current `webui.py`: the rule builder offers the full operator set
(`between`/`in`/`changed`/`regex`, comparing to **another metric**, a per-condition
`for`, the `enabled` toggle), supports per-rule **extra actions** (extra publishes,
webhooks, Slack notify with `{{metric}}` templating), and discovers schedule,
variable, input, and **computed** metrics; the Inputs page edits all of those
sources; the **MQTT** page is a live console (topic feed, per-topic view, and a
publish box); the **History** page shows trend sparklines per metric; the
dashboard has a Variables card and per-device manual controls; the **System**
page shows health, a config summary, and a runtime log viewer. Everything is
mock — clicks update local state and show a toast; nothing is published or saved.

No build step, no external CDNs, no cookies — just static files.

## Try it locally

Open `index.html` directly in a browser, or serve the folder:

```bash
cd demo
python3 -m http.server 8000   # then visit http://localhost:8000
```

On the **Dashboard**, use the *Demo controls* to switch scenarios (raining,
rained earlier, freezing, broker offline) and watch the `INHIBIT`/`ALLOW`
directive and rule states react. Values also drift slightly every few seconds
to mimic a live feed.

## Deploy on cPanel

1. In **File Manager**, open the folder you want to serve from — usually
   `public_html` (or a subfolder like `public_html/weather-demo`).
2. Upload the **contents** of this `demo/` folder (the `.html` pages and the
   `assets/` folder), keeping the structure intact.
3. Visit `https://yourdomain.com/` (or `/weather-demo/` if you used a
   subfolder). `index.html` is served automatically as the landing page.

That's it — because everything is static, no Node, Python, or database is
required. To set the demo as a site's home page, just ensure `index.html` sits
at the web root.

## Differences from the live app

This demo is intentionally read-only:

- **No persistence** — "Save" buttons validate input and show a confirmation
  toast, but never write a config file.
- **Simplified validation** — the Rules **form builder** validates the same way
  as the live app (it shares the metric/operator rules), but the **YAML** tab
  does only a structural/shape check in JavaScript. The live app parses the YAML
  fully on the server and round-trips everything through the monitor's own
  validator before saving.
- **Mock weather** — the Dashboard generates plausible sample data instead of
  polling api.weather.gov.

For the real, functional interface, run `webui.py` from the project root (see
the top-level `README.md`).
