# Precipitation → MQTT — UI demo

A **static, self-contained** preview of the controller's web interface
(`webui.py`). It uses sample data and runs entirely in the browser — nothing is
published to MQTT and nothing is saved. It exists to show off the UI on any
ordinary web host (e.g. cPanel) without installing Python, Flask, or MQTT.

## What's inside

```
demo/
├── index.html      Dashboard  — live-style conditions + irrigation directive
├── settings.html   Settings   — full config form with in-browser validation
├── rules.html      Rules      — YAML rule editor with shape validation
└── assets/
    ├── style.css   Shared theme (mirrors the live app)
    └── app.js      Mock data + interactivity (no backend, no dependencies)
```

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
2. Upload the **contents** of this `demo/` folder (`index.html`,
   `settings.html`, `rules.html`, and the `assets/` folder), keeping the
   structure intact.
3. Visit `https://yourdomain.com/` (or `/weather-demo/` if you used a
   subfolder). `index.html` is served automatically as the landing page.

That's it — because everything is static, no Node, Python, or database is
required. To set the demo as a site's home page, just ensure `index.html` sits
at the web root.

## Differences from the live app

This demo is intentionally read-only:

- **No persistence** — "Save" buttons validate input and show a confirmation
  toast, but never write a config file.
- **Simplified validation** — the Rules editor does a structural/YAML-shape
  check in JavaScript. The live app parses the YAML fully on the server and
  round-trips it through the monitor's own validator before saving.
- **Mock weather** — the Dashboard generates plausible sample data instead of
  polling api.weather.gov.

For the real, functional interface, run `webui.py` from the project root (see
the top-level `README.md`).
