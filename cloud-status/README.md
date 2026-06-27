# Remote status page (read‑only)

A tiny, **display‑only** mirror of the controller's status that lives on ordinary
PHP/cPanel hosting. The on‑site controller **POSTs** its status snapshot here each
cycle (outbound only); this page just shows the latest one. There is **no control
capability and no path back into your network** — even if someone reached this
page, the worst they could do is read a status.

```
on‑site controller ──HTTPS POST (X‑Status‑Token)──▶ ingest.php ──writes──▶ status.json
visitor's browser ──HTTPS GET──▶ index.html ──fetches──▶ status.json (renders)
```

## Files
```
cloud-status/
├── ingest.php          receives the POST, checks the token, writes status.json
├── secret.sample.php   copy to secret.php and set your shared token
├── index.html          the read‑only dashboard
└── assets/
    ├── style.css       theme (matches the live UI)
    └── app.js          fetches status.json and renders it
```
`status.json` is created at runtime by `ingest.php`.

## Deploy (cPanel / any PHP host)
1. Upload the **contents** of this folder to a directory on your dashboard site,
   e.g. `public_html/weather/` (so it's reachable at
   `https://your-dashboard-domain/weather/`).
2. Create the token file:
   ```bash
   cp secret.sample.php secret.php
   # edit secret.php and set a long random token, e.g. from:  openssl rand -hex 32
   ```
   Requesting `secret.php` directly returns a blank page (PHP executes it), so the
   token is never served as text. Keep `secret.php` out of git.
3. Make sure the directory is writable by PHP so `ingest.php` can create
   `status.json` (on most cPanel setups it already is).

## Point the controller at it
On the Ubuntu box, in **Settings → Remote status page** (or `config.yaml`):
```yaml
status_push:
  enabled: true
  url: "https://your-dashboard-domain/weather/ingest.php"
  token: "the-same-token-you-put-in-secret.php"
```
The token is sent in the `X-Status-Token` header and must match `secret.php`.

## Verify
- Wait one poll cycle, then open `https://your-dashboard-domain/weather/` — you
  should see the current directive and conditions, with "updated … ago".
- Manual test from anywhere:
  ```bash
  curl -i -X POST https://your-dashboard-domain/weather/ingest.php \
    -H "X-Status-Token: <your-token>" -H "Content-Type: application/json" \
    -d '{"updated":"2026-06-27T12:00:00+00:00","mqtt_connected":true,"lookback_hours":24,
         "metrics":{"is_raining":true,"precip_accum_in":0.3},
         "rules":[{"name":"irrigation_rain_inhibit","topic":"irrigation/rain_inhibit",
                   "active":true,"current_payload":"INHIBIT"}]}'
  ```
  Expect `{"ok":true}` (and `401` if the token is wrong).

## Notes
- If the page shows a "status is stale" banner, the controller hasn't pushed
  recently (offline, or `status_push` disabled). The staleness threshold is
  `STALE_SECONDS` in `assets/app.js` (default 30 min).
- `status.json` is public (it's what the page fetches) — it contains only the
  same non‑sensitive status shown on screen. Don't put secrets in rule payloads
  if you'd rather they not appear here.
