#!/usr/bin/env python3
"""Diagnose precip_accum_in: show what each nearby NWS station reports vs. what
the monitor computes, so you can verify the measured-rainfall metric.

It reuses weather_mqtt's own NWS client and accumulation logic, so the value it
prints is exactly what the monitor would publish -- including the nearest-first
station fallback (some ASOS sites report rain but no usable precip gauge).

Usage:
    python check_rain.py                       # use config.yaml
    python check_rain.py --hours 12
    python check_rain.py --lat 41.24 --lon -74.27 --station KMGJ
    python check_rain.py --raw                 # also dump every observation
"""
import argparse
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import weather_mqtt as w


def _load_defaults(path):
    """Read lat/lon/user_agent/station/lookback from config.yaml without the
    monitor's strict validation, so the tool still runs on a half-built config."""
    try:
        import yaml
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    loc = cfg.get("location") or {}
    precip = cfg.get("precipitation") or {}
    return {
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
        "station": loc.get("station_id"),
        "user_agent": cfg.get("user_agent"),
        "hours": precip.get("lookback_hours", 24),
        "max_stations": precip.get("max_fallback_stations",
                                   w.MAX_FALLBACK_STATIONS),
    }


def _mm(group):
    group = group or {}
    return w.to_mm(group.get("value"), group.get("unitCode"))


def _fetch_obs(station, ua, hours, now):
    start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{w.NWS_API}/stations/{station}/observations?start={quote(start)}"
    return w.nws_get(url, ua)


def _dump_raw(feats):
    print(f"\n{'timestamp':21} {'1h':>6} {'3h':>6} {'6h':>6}  rain?  raw METAR")
    print("-" * 92)
    for f in feats:
        p = f.get("properties", {})
        cells = " ".join(
            f"{('-' if v is None else round(v, 1))!s:>6}" for v in
            (_mm(p.get("precipitationLastHour")),
             _mm(p.get("precipitationLast3Hours")),
             _mm(p.get("precipitationLast6Hours"))))
        flag = {True: "RAIN", False: "dry", None: "?"}[w.detect_raining(p)]
        raw = (p.get("rawMessage") or "").strip()
        print(f"{p.get('timestamp',''):21} {cells}  {flag:>5}  {raw[:44]}")
    print("-" * 92)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--station", help="pin a station id (tried first)")
    ap.add_argument("--hours", type=int, help="lookback window (default: config)")
    ap.add_argument("--raw", action="store_true",
                    help="also dump every observation for the first station")
    args = ap.parse_args()

    d = _load_defaults(args.config)
    lat = args.lat if args.lat is not None else d.get("lat")
    lon = args.lon if args.lon is not None else d.get("lon")
    station = args.station or d.get("station")
    hours = args.hours if args.hours is not None else int(d.get("hours") or 24)
    max_stations = int(d.get("max_stations") or w.MAX_FALLBACK_STATIONS)
    ua = d.get("user_agent")

    if lat is None or lon is None:
        sys.exit("No location: set it in config.yaml or pass --lat/--lon.")
    if not ua or not str(ua).strip():
        sys.exit("No user_agent in config.yaml (NWS requires one). Set it first.")

    print(f"location: {lat},{lon}   window: last {hours}h   user_agent: {ua}\n")

    loc = w.resolve_location(lat, lon, ua, station_override=station,
                             max_stations=max_stations)
    candidates = loc.get("station_ids") or ([loc["station_id"]] if loc.get("station_id") else [])
    if not candidates:
        sys.exit("Could not resolve any observation station for this location.")
    print(f"candidate stations (nearest-first): {', '.join(candidates)}\n")

    now = datetime.now(timezone.utc)
    print(f"{'station':8} {'obs':>4} {'coverage':>9} {'precip_in':>10}  verdict")
    print("-" * 60)
    chosen = None
    first_feats = None
    for sid in candidates:
        try:
            data = _fetch_obs(sid, ua, hours, now)
        except Exception as e:
            print(f"{sid:8} {'-':>4} {'-':>9} {'-':>10}  fetch failed: {e}")
            continue
        feats = data.get("features", [])
        if first_feats is None:
            first_feats = feats
        st = w._precip_stats(data, hours, now)
        resolved, usable = w._resolve_station_precip(st)
        verdict = "USE (gauge OK)" if usable else "skip (no usable gauge)"
        inches = "-" if resolved is None else f"{resolved:.2f}"
        print(f"{sid:8} {len(feats):>4} {st['coverage']*100:>8.0f}% "
              f"{inches:>10}  {verdict}")
        if usable and chosen is None:
            chosen = sid
            break
    print("-" * 60)

    inches, used = w.fetch_precip_accum_best(candidates, ua, hours, now,
                                             max_stations=max_stations)
    if used:
        print(f"\nprecip_accum_in = {inches} in   (from station {used})")
    else:
        print("\nprecip_accum_in = None (unknown -> rule holds last state): "
              "no station in range reported a usable gauge.\n"
              "Widen precipitation.max_fallback_stations, or pin a known-good "
              "--station / location.station_id.")

    if args.raw and first_feats:
        _dump_raw(first_feats)


if __name__ == "__main__":
    main()
