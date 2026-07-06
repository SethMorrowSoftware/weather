#!/usr/bin/env python3
"""Diagnose precip_accum_in: show what the NWS station reports vs. what the
monitor computes, so you can verify the measured-rainfall metric is accurate.

It reuses weather_mqtt's own NWS client and accumulation logic, so the
"precip_accum_in" it prints is exactly what the monitor would publish.

Usage:
    python check_rain.py                       # use config.yaml
    python check_rain.py --hours 12
    python check_rain.py --lat 41.24 --lon -74.27 --station KMGJ
"""
import argparse
import sys
from datetime import datetime, timezone, timedelta

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
    }


def _mm(group):
    """Coax an NWS precip group {value, unitCode} to millimeters, or None."""
    group = group or {}
    return w.to_mm(group.get("value"), group.get("unitCode"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--station", help="pin a station id (else auto-pick nearest)")
    ap.add_argument("--hours", type=int, help="lookback window (default: config)")
    args = ap.parse_args()

    d = _load_defaults(args.config)
    lat = args.lat if args.lat is not None else d.get("lat")
    lon = args.lon if args.lon is not None else d.get("lon")
    station = args.station or d.get("station")
    hours = args.hours if args.hours is not None else int(d.get("hours") or 24)
    ua = d.get("user_agent")

    if lat is None or lon is None:
        sys.exit("No location: set it in config.yaml or pass --lat/--lon.")
    if not ua or not str(ua).strip():
        sys.exit("No user_agent in config.yaml (NWS requires one). Set it first.")

    print(f"location: {lat},{lon}   window: last {hours}h   user_agent: {ua}\n")

    # Resolve the station the same way the monitor does (respects an override).
    loc = w.resolve_location(lat, lon, ua, station_override=station)
    stn = loc.get("station_id")
    if not stn:
        sys.exit("Could not resolve an observation station for this location.")
    print(f"station: {stn}{'  (pinned)' if station else '  (nearest, auto)'}\n")

    # Same request the monitor makes for measured accumulation.
    from urllib.parse import quote
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    url = f"{w.NWS_API}/stations/{stn}/observations?start={quote(start)}"
    data = w.nws_get(url, ua)
    feats = data.get("features", [])

    print(f"{len(feats)} observations in window\n")
    print(f"{'timestamp':21} {'1h':>6} {'3h':>6} {'6h':>6}  rain?  raw METAR")
    print("-" * 92)
    any_value = False
    for f in feats:
        p = f.get("properties", {})
        p1, p3, p6 = (_mm(p.get("precipitationLastHour")),
                      _mm(p.get("precipitationLast3Hours")),
                      _mm(p.get("precipitationLast6Hours")))
        if any(v is not None for v in (p1, p3, p6)):
            any_value = True
        raining = w.detect_raining(p)
        flag = {True: "RAIN", False: "dry", None: "?"}[raining]
        cells = " ".join(f"{('-' if v is None else round(v, 1))!s:>6}"
                         for v in (p1, p3, p6))
        raw = (p.get("rawMessage") or "").strip()
        print(f"{p.get('timestamp',''):21} {cells}  {flag:>5}  {raw[:44]}")

    print("-" * 92)
    result = w._accumulate_precip(data, hours, datetime.now(timezone.utc))
    print(f"\nprecip_accum_in (what the monitor publishes): {result} "
          f"{'in' if result is not None else '(unknown -> rule holds last state)'}")

    if not any_value and feats:
        print("\n>>> This station reports NO precip value (no 1h/3h/6h group) in the\n"
              "    window. If the 'rain?' column shows RAIN above, it has no usable\n"
              "    gauge -- pin a --station that reports precipitation in config.yaml.")


if __name__ == "__main__":
    main()
