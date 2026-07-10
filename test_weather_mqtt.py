#!/usr/bin/env python3
"""Offline unit tests for weather_mqtt -- no network required.

Run:  python test_weather_mqtt.py
"""
from datetime import datetime, timedelta as _td, timezone

import weather_mqtt as w


class _Skip(Exception):
    """Raised by a test to mark itself skipped (e.g. an optional dependency
    like Flask is missing). The runner counts these separately from passes so
    the summary never claims coverage that didn't run."""


_OPTIONAL_DEPS = ("flask", "ruamel", "ruamel.yaml", "yaml")


def _skip_if_optional(e):
    """Turn an import failure for an OPTIONAL dependency into a _Skip; re-raise
    anything else so a real bug in webui/setup_wizard surfaces as an error
    instead of being silently masked as a skipped test."""
    name = getattr(e, "name", "") or ""
    if isinstance(e, ImportError) and (
            name in _OPTIONAL_DEPS or any(d in str(e) for d in _OPTIONAL_DEPS)):
        return _Skip(e)
    raise e


def _obs(ts, value_mm, unit="wmoUnit:mm"):
    return {"properties": {"timestamp": ts,
                           "precipitationLastHour": {"value": value_mm,
                                                     "unitCode": unit}}}


def _rainy_null_obs(now, hours=24):
    """A KMGJ-style feed: obs every 30 min showing rain, but precipitationLastHour
    is null throughout except two stray 0.0 readings (a dead/absent gauge)."""
    feats = []
    for i in range(hours * 2):
        t = now - _td(minutes=30 * i)
        val = 0.0 if i in (2, 26) else None      # two stray non-null readings
        feats.append({"properties": {
            "timestamp": t.isoformat(),
            "textDescription": "Heavy Rain",
            "precipitationLastHour": {"value": val, "unitCode": "wmoUnit:mm"}}})
    return feats


def _hourly_gauge_obs(now, hours=24, wet_hours=3, wet_mm=2.54):
    """A working-gauge feed: a precipitationLastHour value every hour (0.0 when
    dry), covering the whole window. `wet_hours` of `wet_mm` set the total."""
    feats = []
    for k in range(hours):
        t = now - _td(hours=k)
        val = wet_mm if k < wet_hours else 0.0
        feats.append({"properties": {
            "timestamp": t.isoformat(),
            "textDescription": "Rain" if val else "Clear",
            "precipitationLastHour": {"value": val, "unitCode": "wmoUnit:mm"}}})
    return feats


def _realistic_gauge_obs(now, hours=24, wet_hours=0, wet_mm=2.54, start_hour=6):
    """How real NWS stations actually report: a precipitationLastHour value ONLY
    during precip hours, and *null* on dry hours (not 0.0). `wet_hours` of
    `wet_mm` in a block starting `start_hour` back set the total; the rest of the
    window is dry-null. This is the shape that made coverage collapse below 50%
    and wrongly reject good stations in production."""
    feats = []
    for k in range(hours):
        t = now - _td(hours=k)
        wet = start_hour <= k < start_hour + wet_hours
        feats.append({"properties": {
            "timestamp": t.isoformat(),
            "textDescription": "Rain" if wet else "Fair",
            "precipitationLastHour": {"value": (wet_mm if wet else None),
                                      "unitCode": "wmoUnit:mm"}}})
    return feats


def _fake_nws(station_data):
    """Stand-in for w.nws_get that dispatches on the station id in the URL."""
    def _get(url, ua, **kw):
        for sid, data in station_data.items():
            if f"/stations/{sid}/observations" in url:
                return data
        return {"features": []}
    return _get


def test_unit_conversion():
    assert w.to_mm(5, "wmoUnit:mm") == 5.0
    assert w.to_mm(0.005, "wmoUnit:m") == 5.0          # meters -> mm
    assert w.to_mm(0.5, "wmoUnit:cm") == 5.0           # cm -> mm
    assert w.to_mm(None, "wmoUnit:mm") is None
    assert w.mm_to_in(25.4) == 1.0


def test_accumulation_sums_hourly_buckets():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    data = {"features": [
        _obs("2026-06-27T11:53:00+00:00", 6.35),   # 0.25 in
        _obs("2026-06-27T10:53:00+00:00", 12.7),   # 0.50 in
        _obs("2026-06-27T09:53:00+00:00", 0.0),
    ]}
    # 6.35 + 12.7 = 19.05 mm = 0.75 in
    assert w._accumulate_precip(data, 24, now) == 0.75


def test_accumulation_dedups_within_an_hour():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    # Two obs in the same clock-hour must not double count; take the max.
    data = {"features": [
        _obs("2026-06-27T11:53:00+00:00", 6.35),
        _obs("2026-06-27T11:20:00+00:00", 5.0),
    ]}
    assert w._accumulate_precip(data, 24, now) == 0.25


def test_accumulation_ignores_outside_window_and_meters():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    data = {"features": [
        _obs("2026-06-27T11:53:00+00:00", 0.00635, "wmoUnit:m"),  # 6.35mm in
        _obs("2026-06-25T11:53:00+00:00", 100.0),                 # 2 days old
    ]}
    assert w._accumulate_precip(data, 24, now) == 0.25


def test_accumulation_dry_vs_no_data():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    # Station IS reporting in-window, but precipitationLastHour is null -- that's
    # how most ASOS stations report a dry hour, so it means 0.0 (dry), not
    # unknown. This lets a rain-inhibit rule resolve to ALLOW instead of UNKNOWN.
    reporting_dry = {"features": [
        {"properties": {"timestamp": "2026-06-27T11:53:00+00:00",
                        "precipitationLastHour": {"value": None}}},
        {"properties": {"timestamp": "2026-06-27T10:53:00+00:00",
                        "precipitationLastHour": {"value": None}}},
    ]}
    assert w._accumulate_precip(reporting_dry, 24, now) == 0.0
    # No observations at all in the window -> genuinely unavailable -> None.
    assert w._accumulate_precip({"features": []}, 24, now) is None
    # Observations exist but all are older than the lookback window -> None too.
    stale = {"features": [
        {"properties": {"timestamp": "2026-06-20T11:53:00+00:00",
                        "precipitationLastHour": {"value": 5.0}}},
    ]}
    assert w._accumulate_precip(stale, 24, now) is None


def _obs6(ts, mm, unit="wmoUnit:mm"):
    """Observation with a null hourly group but a 6-hour synoptic total."""
    return {"properties": {"timestamp": ts,
                           "precipitationLastHour": {"value": None},
                           "precipitationLast6Hours": {"value": mm,
                                                       "unitCode": unit}}}


def test_accumulation_falls_back_to_coarse_totals():
    # A station that never emits the hourly group (null every hour) but does
    # report the 6-hour synoptic total must NOT read a flat 0.0 -- that was the
    # bug that showed 0 in over 24h during a downpour. Sum the coarse totals.
    now = datetime(2026, 6, 27, 18, 10, tzinfo=timezone.utc)
    data = {"features": [
        _obs6("2026-06-27T18:05:00+00:00", 20.0),   # covers 12:05-18:05
        _obs6("2026-06-27T12:05:00+00:00", 5.0),    # covers 06:05-12:05
        _obs6("2026-06-27T06:05:00+00:00", 0.0),    # covers 00:05-06:05
    ]}
    # 25.0 mm across the tiled 6h windows = 0.98 in, despite every hourly null.
    assert w._accumulate_precip(data, 24, now) == round(25.0 / 25.4, 2)


def test_accumulation_coarse_totals_do_not_double_count():
    # At one obs the station reports BOTH a 6h and a 3h total; the 3h span is a
    # subset of the 6h span, so only the 6h total counts (not their sum).
    now = datetime(2026, 6, 27, 12, 10, tzinfo=timezone.utc)
    data = {"features": [{"properties": {
        "timestamp": "2026-06-27T12:05:00+00:00",
        "precipitationLastHour": {"value": None},
        "precipitationLast6Hours": {"value": 10.0, "unitCode": "wmoUnit:mm"},
        "precipitationLast3Hours": {"value": 7.0, "unitCode": "wmoUnit:mm"},
    }}]}
    assert w._accumulate_precip(data, 24, now) == round(10.0 / 25.4, 2)


def test_accumulation_prefers_measurement_over_present_weather():
    # Present weather says rain, but the station also reports a real coarse
    # total -> use the measurement, not the None "unknown" path.
    now = datetime(2026, 6, 27, 12, 10, tzinfo=timezone.utc)
    data = {"features": [{"properties": {
        "timestamp": "2026-06-27T12:05:00+00:00",
        "textDescription": "Rain",
        "precipitationLastHour": {"value": None},
        "precipitationLast6Hours": {"value": 12.7, "unitCode": "wmoUnit:mm"},
    }}]}
    assert w._accumulate_precip(data, 24, now) == 0.5


def test_accumulation_raining_but_no_gauge_reads_unknown():
    # The reported failure: it is visibly pouring, the station is reporting
    # observations, but every precip field is null (no usable gauge). A flat 0.0
    # would be a dangerous false-dry, so the metric must read unknown (None) and
    # let the rain-inhibit rule hold its last state instead of allowing water.
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    data = {"features": [
        {"properties": {"timestamp": "2026-06-27T11:53:00+00:00",
                        "textDescription": "Heavy Rain",
                        "precipitationLastHour": {"value": None}}},
        {"properties": {"timestamp": "2026-06-27T10:53:00+00:00",
                        "textDescription": "Rain",
                        "precipitationLastHour": {"value": None}}},
    ]}
    assert w._accumulate_precip(data, 24, now) is None


def test_precip_stats_coverage_distinguishes_dead_gauge():
    # The real KMGJ case: rain in every ob, but precip reported on almost none of
    # them -> inches happens to be 0.0 (two stray readings) but coverage is tiny,
    # which is how the fallback knows the gauge is unusable.
    now = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    dead = w._precip_stats({"features": _rainy_null_obs(now, 24)}, 24, now)
    assert dead["raining"] is True
    assert dead["inches"] == 0.0
    assert dead["coverage"] < w.MIN_PRECIP_COVERAGE
    # A working gauge covers the whole window and totals its hourly values.
    good = w._precip_stats({"features": _hourly_gauge_obs(now, 24)}, 24, now)
    assert good["coverage"] >= 0.9
    assert good["inches"] == 0.30


def test_precip_fallback_uses_next_station_when_gauge_dead():
    now = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    data = {"KMGJ": {"features": _rainy_null_obs(now, 24)},      # dead gauge
            "KSWF": {"features": _hourly_gauge_obs(now, 24)}}     # good, 0.30 in
    real = w.nws_get
    w.nws_get = _fake_nws(data)
    try:
        inches, used = w.fetch_precip_accum_best(["KMGJ", "KSWF"], "ua", 24, now)
    finally:
        w.nws_get = real
    assert used == "KSWF"
    assert inches == 0.30


def test_precip_fallback_prefers_primary_when_usable():
    now = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    data = {"KMGJ": {"features": _hourly_gauge_obs(now, 24)},
            "KSWF": {"features": _hourly_gauge_obs(now, 24, wet_hours=6)}}
    real = w.nws_get
    w.nws_get = _fake_nws(data)
    try:
        inches, used = w.fetch_precip_accum_best(["KMGJ", "KSWF"], "ua", 24, now)
    finally:
        w.nws_get = real
    assert used == "KMGJ"          # nearest is usable -> no fallback, no KSWF fetch
    assert inches == 0.30


def test_precip_fallback_all_dead_returns_none():
    now = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    data = {"KMGJ": {"features": _rainy_null_obs(now, 24)},
            "KSWF": {"features": _rainy_null_obs(now, 24)}}
    real = w.nws_get
    w.nws_get = _fake_nws(data)
    try:
        inches, used = w.fetch_precip_accum_best(["KMGJ", "KSWF"], "ua", 24, now)
    finally:
        w.nws_get = real
    assert used is None            # nobody usable -> hold last state
    assert inches is None


def test_precip_real_gauge_reporting_null_when_dry_is_usable():
    # Regression: a healthy station that reports precip only during rain and null
    # on dry hours (how real NWS stations report) spans well under 50% of the
    # window. The old coverage gate rejected it, so precip_accum_in was stuck at
    # None forever. It must now be trusted and return the measured total.
    now = datetime(2026, 7, 10, 19, 20, tzinfo=timezone.utc)
    feats = _realistic_gauge_obs(now, 24, wet_hours=4, wet_mm=2.54, start_hour=8)
    st = w._precip_stats({"features": feats}, 24, now)
    assert st["coverage"] < w.MIN_PRECIP_COVERAGE      # would fail the old gate
    inches, usable = w._resolve_station_precip(st)
    assert usable is True
    assert inches == round(4 * 2.54 / 25.4, 2)         # 0.40 in, measured
    data = {"KMGJ": {"features": feats}}
    real = w.nws_get
    w.nws_get = _fake_nws(data)
    try:
        got, used = w.fetch_precip_accum_best(["KMGJ"], "ua", 24, now)
    finally:
        w.nws_get = real
    assert used == "KMGJ"
    assert got == round(4 * 2.54 / 25.4, 2)


def test_precip_genuinely_dry_reads_zero_not_unknown():
    # Regression (the stuck-"INHIBIT" report): when it is simply dry -- stations
    # reporting, precip null every hour, no present weather -- accumulation must
    # resolve to 0.0 so the rain-inhibit rule can re-evaluate and clear, not stay
    # None and hold a stale INHIBIT indefinitely.
    now = datetime(2026, 7, 10, 19, 20, tzinfo=timezone.utc)
    dry = _realistic_gauge_obs(now, 24, wet_hours=0)   # all dry-null, "Fair"
    st = w._precip_stats({"features": dry}, 24, now)
    assert st["raining"] is False
    assert st["inches"] is None                        # no non-null precip value
    data = {"KMGJ": {"features": dry}}
    real = w.nws_get
    w.nws_get = _fake_nws(data)
    try:
        inches, used = w.fetch_precip_accum_best(["KMGJ"], "ua", 24, now)
    finally:
        w.nws_get = real
    assert used == "KMGJ"
    assert inches == 0.0


def test_resolve_location_builds_nearest_first_station_list():
    import tempfile, pathlib
    points = {"properties": {"forecastHourly": "https://api/hourly",
                             "observationStations": "https://api/stations",
                             "gridId": "OKX"}}
    stations = {"features": [{"properties": {"stationIdentifier": "KMGJ"}},
                             {"properties": {"stationIdentifier": "KSWF"}},
                             {"properties": {"stationIdentifier": "KPOU"}}]}

    def fake(url, ua, **kw):
        if "/points/" in url:
            return points
        if url == "https://api/stations":
            return stations
        return {"features": []}

    real_get, real_cache = w.nws_get, w.CACHE_FILE
    w.nws_get = fake
    w.CACHE_FILE = pathlib.Path(tempfile.mkdtemp()) / "cache.json"
    try:
        loc = w.resolve_location(40.0, -74.0, "ua", None, max_stations=5)
        assert loc["station_ids"] == ["KMGJ", "KSWF", "KPOU"]
        assert loc["station_id"] == "KMGJ"
        # A pinned override is tried first, with the rest kept as fallbacks.
        loc2 = w.resolve_location(40.0, -74.0, "ua", "KSWF", max_stations=5)
        assert loc2["station_ids"][0] == "KSWF"
        assert set(loc2["station_ids"]) == {"KSWF", "KMGJ", "KPOU"}
    finally:
        w.nws_get, w.CACHE_FILE = real_get, real_cache


def test_detect_raining():
    assert w.detect_raining({"textDescription": "Light Rain"}) is True
    assert w.detect_raining({"presentWeather": [{"weather": "drizzle"}]}) is True
    assert w.detect_raining({"textDescription": "Clear"}) is False
    assert w.detect_raining({}) is None   # nothing reported -> unknown


def test_compound_any_rule():
    rule = {"name": "irr", "when": {"any": [
        {"metric": "is_raining", "operator": "==", "value": True},
        {"metric": "precip_accum_in", "operator": ">=", "value": 0.25},
    ]}}
    # raining now -> match even if accumulation low
    assert w.evaluate_rule(rule, {"is_raining": True, "precip_accum_in": 0.0}) is True
    # not raining but enough accumulation -> match
    assert w.evaluate_rule(rule, {"is_raining": False, "precip_accum_in": 0.3}) is True
    # dry both ways -> no match
    assert w.evaluate_rule(rule, {"is_raining": False, "precip_accum_in": 0.0}) is False


def test_compound_unknown_is_failsafe():
    rule = {"name": "irr", "when": {"any": [
        {"metric": "is_raining", "operator": "==", "value": True},
        {"metric": "precip_accum_in", "operator": ">=", "value": 0.25},
    ]}}
    # accumulation known-false but rain unknown -> None (leave state unchanged)
    assert w.evaluate_rule(rule, {"is_raining": None, "precip_accum_in": 0.0}) is None
    # one branch true is enough regardless of the unknown
    assert w.evaluate_rule(rule, {"is_raining": None, "precip_accum_in": 0.5}) is True


def test_nested_any_all_not():
    # not(temperature < 35)  AND  (raining OR accum >= 0.25)
    rule = {"name": "r", "when": {"all": [
        {"not": {"metric": "temperature", "operator": "<", "value": 35}},
        {"any": [
            {"metric": "is_raining", "operator": "==", "value": True},
            {"metric": "precip_accum_in", "operator": ">=", "value": 0.25},
        ]},
    ]}}
    # warm (not<35 -> true) and raining -> true
    assert w.evaluate_rule(rule, {"temperature": 50, "is_raining": True,
                                  "precip_accum_in": 0.0}) is True
    # cold (not<35 -> false) kills the ALL regardless of the rain branch
    assert w.evaluate_rule(rule, {"temperature": 20, "is_raining": True,
                                  "precip_accum_in": 1.0}) is False
    # warm, dry both ways -> false
    assert w.evaluate_rule(rule, {"temperature": 50, "is_raining": False,
                                  "precip_accum_in": 0.0}) is False


def test_not_propagates_unknown():
    rule = {"name": "r", "when": {"not": {"metric": "is_raining",
                                          "operator": "==", "value": True}}}
    assert w.evaluate_rule(rule, {"is_raining": True}) is False
    assert w.evaluate_rule(rule, {"is_raining": False}) is True
    assert w.evaluate_rule(rule, {"is_raining": None}) is None   # unknown stays unknown


def test_between_operator():
    rule = {"name": "r", "when": {"metric": "temperature",
                                  "operator": "between", "value": [40, 80]}}
    assert w.evaluate_rule(rule, {"temperature": 60}) is True
    assert w.evaluate_rule(rule, {"temperature": 40}) is True    # inclusive
    assert w.evaluate_rule(rule, {"temperature": 80}) is True    # inclusive
    assert w.evaluate_rule(rule, {"temperature": 81}) is False
    assert w.evaluate_rule(rule, {"temperature": None}) is None  # fail-safe


def test_in_operator_numeric_and_text():
    rnum = {"name": "r", "when": {"metric": "humidity",
                                  "operator": "in", "value": [30, 50, 70]}}
    assert w.evaluate_rule(rnum, {"humidity": 50}) is True
    assert w.evaluate_rule(rnum, {"humidity": 55}) is False
    rtext = {"name": "r", "when": {"metric": "short_forecast",
                                   "operator": "in", "value": ["Sunny", "Clear"]}}
    assert w.evaluate_rule(rtext, {"short_forecast": "clear"}) is True   # case-insensitive
    assert w.evaluate_rule(rtext, {"short_forecast": "Rain"}) is False


def test_validate_accepts_nested_and_new_ops():
    w.validate_config(_min_cfg(rules=[{
        "name": "complex", "topic": "t", "on_match": "1",
        "when": {"all": [
            {"not": {"metric": "is_raining", "operator": "==", "value": True}},
            {"metric": "temperature", "operator": "between", "value": [40, 90]},
            {"metric": "humidity", "operator": "in", "value": [10, 20, 30]},
        ]}}]))


def test_validate_rejects_bad_between_and_in():
    for when, needle in [
        ({"metric": "temperature", "operator": "between", "value": 5}, "between needs"),
        ({"metric": "temperature", "operator": "between", "value": [90, 10]}, "low must be <= high"),
        ({"metric": "temperature", "operator": "between", "value": ["a", "b"]}, "must be numbers"),
        ({"metric": "humidity", "operator": "in", "value": []}, "non-empty list"),
        ({"metric": "humidity", "operator": "in", "value": ["x"]}, "all numbers"),
        ({"not": {"metric": "bogus", "operator": "<", "value": 1}}, "unknown metric"),
        ({"any": []}, "non-empty list"),
    ]:
        try:
            w.validate_config(_min_cfg(rules=[{
                "name": "r", "topic": "t", "on_match": "1", "when": when}]))
            raise AssertionError(f"expected ValueError containing {needle!r}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"


def test_validate_enabled_default_and_coercion():
    cfg = w.validate_config(_min_cfg())
    assert cfg["rules"][0]["enabled"] is True            # default on
    cfg2 = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1", "enabled": False,
        "when": {"metric": "temperature", "operator": "<", "value": 5}}]))
    assert cfg2["rules"][0]["enabled"] is False
    cfg3 = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1", "enabled": "false",
        "when": {"metric": "temperature", "operator": "<", "value": 5}}]))
    assert cfg3["rules"][0]["enabled"] is False          # string coerced


def test_parse_duration():
    assert w.parse_duration("30s") == 30
    assert w.parse_duration("10m") == 600
    assert w.parse_duration("2h") == 7200
    assert w.parse_duration(5) == 300          # bare number == minutes
    assert w.parse_duration("0m") == 0
    assert w.parse_duration(None, 0) == 0
    assert w.parse_duration("garbage", 99) == 99


def test_in_window_hours_days_and_wrap():
    from datetime import datetime
    mon_10 = datetime(2026, 6, 29, 10, 0)   # Monday 10:00
    mon_05 = datetime(2026, 6, 29, 5, 0)    # Monday 05:00
    sun_10 = datetime(2026, 6, 28, 10, 0)   # Sunday 10:00
    assert w.in_window(None, mon_10) is True                                  # no window -> always
    assert w.in_window({"from": "06:00", "to": "20:00"}, mon_10) is True
    assert w.in_window({"from": "06:00", "to": "20:00"}, mon_05) is False     # before open
    assert w.in_window({"from": "06:00", "to": "20:00",
                        "days": ["mon", "tue"]}, sun_10) is False             # wrong day
    # overnight window 22:00 -> 06:00 wraps past midnight
    assert w.in_window({"from": "22:00", "to": "06:00"}, mon_05) is True
    assert w.in_window({"from": "22:00", "to": "06:00"}, mon_10) is False
    # `to` is exclusive
    assert w.in_window({"from": "06:00", "to": "10:00"}, mon_10) is False


def test_apply_hysteresis_min_on_off_cooldown():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    # first commit: no prior state -> desired wins regardless of timers
    assert w.apply_hysteresis({"min_off": "10m"}, None, True, None, t0) is True
    # currently ON, want OFF, min_on=10m not yet elapsed -> hold ON
    assert w.apply_hysteresis({"min_on": "10m"}, True, False, t0, t0 + timedelta(minutes=5)) is True
    # ...once min_on elapses, allow OFF
    assert w.apply_hysteresis({"min_on": "10m"}, True, False, t0, t0 + timedelta(minutes=11)) is False
    # currently OFF, want ON, min_off holds it OFF until elapsed
    assert w.apply_hysteresis({"min_off": "10m"}, False, True, t0, t0 + timedelta(minutes=5)) is False
    assert w.apply_hysteresis({"min_off": "10m"}, False, True, t0, t0 + timedelta(minutes=11)) is True
    # cooldown blocks any transition regardless of direction
    assert w.apply_hysteresis({"cooldown": "30m"}, True, False, t0, t0 + timedelta(minutes=20)) is True
    # no hysteresis config -> desired passes straight through
    assert w.apply_hysteresis(None, True, False, t0, t0 + timedelta(minutes=1)) is False


def test_resolve_desired_window_gate():
    from datetime import datetime
    rule = {"name": "r", "window": {"from": "06:00", "to": "20:00"},
            "when": {"metric": "temperature", "operator": ">", "value": 50}}
    inside = datetime(2026, 6, 29, 10, 0)
    outside = datetime(2026, 6, 29, 22, 0)
    assert w.resolve_desired(rule, {"temperature": 60}, inside) is True
    assert w.resolve_desired(rule, {"temperature": 40}, inside) is False
    assert w.resolve_desired(rule, {"temperature": 60}, outside) is False    # window forces off
    # no window -> just the evaluation
    assert w.resolve_desired({"name": "r", "when": rule["when"]},
                             {"temperature": 60}, outside) is True


def test_validate_window_and_hysteresis():
    w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1",
        "window": {"from": "06:00", "to": "20:00", "days": ["mon", "fri"]},
        "hysteresis": {"min_on": "10m", "min_off": "5m", "cooldown": "0m"},
        "when": {"metric": "temperature", "operator": ">", "value": 50}}]))
    for win, needle in [
        ({"from": "25:00", "to": "26:00"}, "out of range"),
        ({"from": "6am", "to": "20:00"}, "HH:MM"),
        ({"days": ["funday"]}, "invalid day"),
        ({"days": []}, "non-empty list"),
    ]:
        try:
            w.validate_config(_min_cfg(rules=[{
                "name": "r", "topic": "t", "on_match": "1", "window": win,
                "when": {"metric": "temperature", "operator": ">", "value": 50}}]))
            raise AssertionError(f"expected ValueError for window {win}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"
    try:
        w.validate_config(_min_cfg(rules=[{
            "name": "r", "topic": "t", "on_match": "1",
            "hysteresis": {"min_on": "ten minutes"},
            "when": {"metric": "temperature", "operator": ">", "value": 50}}]))
        raise AssertionError("expected ValueError for bad hysteresis duration")
    except ValueError as e:
        assert "duration" in str(e)


def test_changed_operator():
    st = w.EngineState()
    rule = {"name": "r", "when": {"metric": "temperature", "operator": "changed"}}
    # first cycle: nothing to compare to yet -> False, then remember 70
    assert w.evaluate_rule(rule, {"temperature": 70}, st) is False
    st.observe({"temperature": 70})
    # same value -> not changed
    assert w.evaluate_rule(rule, {"temperature": 70}, st) is False
    st.observe({"temperature": 70})
    # different value -> changed
    assert w.evaluate_rule(rule, {"temperature": 72}, st) is True
    st.observe({"temperature": 72})
    # metric unavailable -> unknown (fail-safe)
    assert w.evaluate_rule(rule, {"temperature": None}, st) is None
    # without engine state the construct can't evaluate -> None
    assert w.evaluate_rule(rule, {"temperature": 80}) is None


def test_for_sustain_modifier():
    from datetime import datetime, timedelta, timezone
    st = w.EngineState()
    t0 = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    rule = {"name": "r", "when": {"metric": "temperature", "operator": ">",
                                  "value": 85, "for": "10m"}}
    # condition true but not yet sustained 10 min -> False
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0) is False
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=5)) is False
    # sustained past 10 min -> True
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=11)) is True
    # drops below -> resets the timer (False), and must re-accumulate
    assert w.evaluate_rule(rule, {"temperature": 80}, st, t0 + timedelta(minutes=12)) is False
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=13)) is False
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=24)) is True
    # unknown breaks continuity -> None (fail-safe), and resets timer
    assert w.evaluate_rule(rule, {"temperature": None}, st, t0 + timedelta(minutes=25)) is None
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=26)) is False


def test_validate_changed_and_for():
    w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1",
        "when": {"all": [
            {"metric": "temperature", "operator": "changed"},
            {"metric": "humidity", "operator": ">", "value": 80, "for": "15m"},
        ]}}]))
    # bad `for` duration is rejected
    try:
        w.validate_config(_min_cfg(rules=[{
            "name": "r", "topic": "t", "on_match": "1",
            "when": {"metric": "temperature", "operator": ">", "value": 5,
                     "for": "soon"}}]))
        raise AssertionError("expected ValueError for bad for: duration")
    except ValueError as e:
        assert "must" in str(e) and "duration" in str(e)


def test_override_store_roundtrip():
    import tempfile, os, json
    p = tempfile.mktemp(suffix=".json")
    try:
        assert w.load_overrides(p) == {}            # missing -> empty
        w.set_override(p, "pump", "on")
        assert w.load_overrides(p) == {"pump": "on"}
        w.set_override(p, "fan", "off")
        assert w.load_overrides(p) == {"pump": "on", "fan": "off"}
        w.set_override(p, "pump", "auto")           # auto clears the key
        assert w.load_overrides(p) == {"fan": "off"}
        # corrupt / bad values are ignored
        open(p, "w").write("{ not json")
        assert w.load_overrides(p) == {}
        json.dump({"x": "on", "y": "bogus"}, open(p, "w"))
        assert w.load_overrides(p) == {"x": "on"}
        try:
            w.set_override(p, "z", "weird")
            raise AssertionError("expected ValueError for bad state")
        except ValueError:
            pass
    finally:
        for s in ("", ".tmp"):
            try: os.unlink(p + s)
            except OSError: pass


def test_effective_manual():
    rule = {"name": "pump", "manual": "off"}
    assert w.effective_manual(rule, {}) == "off"                 # config default
    assert w.effective_manual(rule, {"pump": "on"}) == "on"      # override wins
    assert w.effective_manual({"name": "x"}, {}) == "auto"       # default


def test_audit_appends_jsonl():
    import tempfile, os, json
    p = tempfile.mktemp(suffix=".log")
    try:
        w.audit(p, device="pump", action="manual_set", state="on", by="admin")
        w.audit(p, device="pump", state="off", source="auto", by="monitor")
        lines = [json.loads(l) for l in open(p) if l.strip()]
        assert len(lines) == 2
        assert lines[0]["device"] == "pump" and lines[0]["by"] == "admin"
        assert "ts" in lines[0] and lines[1]["source"] == "auto"
    finally:
        try: os.unlink(p)
        except OSError: pass


def test_validate_manual_control_gating_and_manual_field():
    # allow_manual_control without a login is forced off (fail closed)
    cfg = w.validate_config(_min_cfg(web={"allow_manual_control": True}))
    assert cfg["web"]["allow_manual_control"] is False
    # with a login it stays on
    cfg2 = w.validate_config(_min_cfg(
        web={"allow_manual_control": True, "username": "a", "password": "b"}))
    assert cfg2["web"]["allow_manual_control"] is True
    # allow_anonymous_control keeps it on WITHOUT a login (trusted-LAN opt-in)
    cfg_anon = w.validate_config(_min_cfg(
        web={"allow_manual_control": True, "allow_anonymous_control": True}))
    assert cfg_anon["web"]["allow_manual_control"] is True
    assert cfg_anon["web"]["allow_anonymous_control"] is True
    # default stays off/fail-closed
    assert cfg["web"]["allow_anonymous_control"] is False
    # per-rule manual coerces/validates; defaults to auto
    assert cfg["rules"][0]["manual"] == "auto"
    cfg3 = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1", "manual": "ON",
        "when": {"metric": "temperature", "operator": "<", "value": 5}}]))
    assert cfg3["rules"][0]["manual"] == "on"
    cfg4 = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1", "manual": "bogus",
        "when": {"metric": "temperature", "operator": "<", "value": 5}}]))
    assert cfg4["rules"][0]["manual"] == "auto"
    # new file defaults exist
    assert cfg["overrides_file"] == "overrides.json"
    assert cfg["audit_file"] == "audit.log"


def test_webui_manual_control_endpoint():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml, json, base64

    def _client(allow, login=True):
        cfg = {
            "version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
            "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
            "precipitation": {"lookback_hours": 24},
            "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
            "web": {"enabled": True, "host": "0.0.0.0", "port": 8080,
                    "username": "admin" if login else "", "password": "pw" if login else "",
                    "allow_manual_control": allow},
            "overrides_file": ovr, "audit_file": aud,
            "rules": [{"name": "pump", "topic": "t", "on_match": "ON", "on_clear": "OFF",
                       "when": {"metric": "is_raining", "operator": "==", "value": True}}],
        }
        open(p, "w").write(yaml.safe_dump(cfg))
        webui.CONFIG_PATH = p
        webui.app.config["TESTING"] = True
        return webui.app.test_client()

    p = tempfile.mktemp(suffix=".yaml")
    ovr = tempfile.mktemp(suffix=".json")
    aud = tempfile.mktemp(suffix=".log")
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    try:
        # disabled -> 403, nothing written
        c = _client(allow=False)
        r = c.post("/api/control", json={"device": "pump", "state": "on"}, headers=hdr)
        assert r.status_code == 403
        assert w.load_overrides(ovr) == {}

        # enabled + authed -> 200, persisted + audited
        c = _client(allow=True)
        r = c.post("/api/control", json={"device": "pump", "state": "on"}, headers=hdr)
        assert r.status_code == 200 and r.get_json()["manual"] == "on"
        assert w.load_overrides(ovr) == {"pump": "on"}
        events = [json.loads(l) for l in open(aud) if l.strip()]
        assert events[-1] == {**events[-1], "device": "pump", "action": "manual_set",
                              "state": "on", "by": "admin"}
        # auto clears it
        r = c.post("/api/control", json={"device": "pump", "state": "auto"}, headers=hdr)
        assert r.status_code == 200 and w.load_overrides(ovr) == {}
        # unknown device / bad state
        assert c.post("/api/control", json={"device": "nope", "state": "on"}, headers=hdr).status_code == 404
        assert c.post("/api/control", json={"device": "pump", "state": "x"}, headers=hdr).status_code == 400
        # wrong credentials are rejected by auth
        bad = {"Authorization": "Basic " + base64.b64encode(b"admin:WRONG").decode()}
        assert c.post("/api/control", json={"device": "pump", "state": "on"}, headers=bad).status_code == 401
    finally:
        for f in (p, ovr, aud):
            for s in ("", ".tmp", ".bak"):
                try: os.unlink(f + s)
                except OSError: pass


def test_schedule_metrics():
    from datetime import datetime
    sat = w.schedule_metrics(datetime(2026, 6, 27, 9, 30))   # Saturday 09:30
    assert sat == {"time_hour": 9, "time_minute": 30,
                   "time_weekday": "sat", "time_is_weekend": True}
    mon = w.schedule_metrics(datetime(2026, 6, 29, 14, 5))   # Monday 14:05
    assert mon["time_weekday"] == "mon" and mon["time_is_weekend"] is False
    assert mon["time_hour"] == 14


def test_schedule_rules_validate_and_evaluate():
    # a rule combining weather + time validates and evaluates against the merged context
    cfg = w.validate_config(_min_cfg(rules=[{
        "name": "daytime_weekday_hold", "topic": "t", "on_match": "1",
        "when": {"all": [
            {"metric": "is_raining", "operator": "==", "value": True},
            {"metric": "time_hour", "operator": "between", "value": [6, 20]},
            {"metric": "time_is_weekend", "operator": "==", "value": False},
            {"metric": "time_weekday", "operator": "in", "value": ["mon", "fri"]},
        ]}}]))
    rule = cfg["rules"][0]
    ctx = dict({"is_raining": True}, **w.schedule_metrics(__import__("datetime").datetime(2026, 6, 29, 10, 0)))
    assert w.evaluate_rule(rule, ctx) is True            # Mon 10:00, raining
    ctx2 = dict({"is_raining": True}, **w.schedule_metrics(__import__("datetime").datetime(2026, 6, 27, 10, 0)))
    assert w.evaluate_rule(rule, ctx2) is False          # Saturday -> weekend, not in [mon,fri]


def test_variables_validate_catalogue_and_rules():
    cfg = w.validate_config(_min_cfg(
        variables={"maintenance_mode": {"type": "bool", "default": True},
                   "temp_setpoint": {"type": "number", "default": 70}},
        rules=[{"name": "r", "topic": "t", "on_match": "1",
                "when": {"any": [
                    {"metric": "var_maintenance_mode", "operator": "==", "value": True},
                    {"metric": "var_temp_setpoint", "operator": ">", "value": 65},
                ]}}]))
    assert cfg["variables"]["maintenance_mode"] == {"type": "bool", "default": True}
    cat = w.metric_catalogue(cfg)
    assert "var_maintenance_mode" in cat and cat["var_temp_setpoint"]["type"] == "number"
    # a rule referencing an undeclared variable is rejected
    try:
        w.validate_config(_min_cfg(rules=[{"name": "r", "topic": "t", "on_match": "1",
            "when": {"metric": "var_unknown", "operator": "==", "value": True}}]))
        raise AssertionError("expected ValueError for unknown var metric")
    except ValueError as e:
        assert "unknown metric" in str(e)
    # bad variable type rejected
    try:
        w.validate_config(_min_cfg(variables={"x": {"type": "text"}}))
        raise AssertionError("expected ValueError for bad var type")
    except ValueError as e:
        assert "type must be one of" in str(e)


def test_variable_store_and_metrics():
    import tempfile, os
    declared = {"maintenance_mode": {"type": "bool", "default": False},
                "temp_setpoint": {"type": "number", "default": 70}}
    p = tempfile.mktemp(suffix=".json")
    try:
        # defaults when nothing stored
        assert w.load_variables(p, declared) == {"maintenance_mode": False, "temp_setpoint": 70}
        w.set_variable(p, "maintenance_mode", "true", declared)
        w.set_variable(p, "temp_setpoint", "68.5", declared)
        vals = w.load_variables(p, declared)
        assert vals == {"maintenance_mode": True, "temp_setpoint": 68.5}
        assert w.variable_metrics(vals) == {"var_maintenance_mode": True, "var_temp_setpoint": 68.5}
        try:
            w.set_variable(p, "nope", 1, declared)
            raise AssertionError("expected ValueError for undeclared variable")
        except ValueError:
            pass
    finally:
        for s in ("", ".tmp"):
            try: os.unlink(p + s)
            except OSError: pass


def test_webui_variable_endpoint_and_builder_metrics():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml, base64

    p = tempfile.mktemp(suffix=".yaml")
    varf = tempfile.mktemp(suffix=".json")
    aud = tempfile.mktemp(suffix=".log")
    cfg = {
        "version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
        "precipitation": {"lookback_hours": 24},
        "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
        "web": {"enabled": True, "host": "0.0.0.0", "port": 8080,
                "username": "admin", "password": "pw", "allow_manual_control": True},
        "variables": {"maintenance_mode": {"type": "bool", "default": False}},
        "variables_file": varf, "audit_file": aud,
        "rules": [{"name": "hold", "topic": "t", "on_match": "1",
                   "when": {"metric": "var_maintenance_mode", "operator": "==", "value": True}}],
    }
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    try:
        # builder discovers the variable metric
        bm = webui.builder_metrics(yaml.safe_load(open(p)))
        assert "var_maintenance_mode" in bm and bm["var_maintenance_mode"]["type"] == "bool"
        # set the variable via the endpoint
        r = c.post("/api/variable", json={"name": "maintenance_mode", "value": "true"}, headers=hdr)
        assert r.status_code == 200 and r.get_json()["value"] is True
        assert w.load_variables(varf, {"maintenance_mode": {"type": "bool", "default": False}}) == {"maintenance_mode": True}
        # unknown variable -> 404
        assert c.post("/api/variable", json={"name": "nope", "value": "1"}, headers=hdr).status_code == 404
    finally:
        for f in (p, varf, aud):
            for s in ("", ".tmp", ".bak"):
                try: os.unlink(f + s)
                except OSError: pass


def test_mqtt_in_coerce_and_routing():
    assert w.coerce_payload(b"3.5", "number") == 3.5
    assert w.coerce_payload(b"  on ", "bool") is True
    assert w.coerce_payload(b"off", "bool") is False
    assert w.coerce_payload(b"hello", "string") == "hello"
    assert w.coerce_payload(b"not-a-number", "number") is None     # junk -> None
    # routing: store updates only for known topics; None coercion keeps last value
    store, tmap = {}, {"sensors/tank": {"topic": "sensors/tank", "metric": "tank_level",
                                        "parse": "number"}}
    assert w.handle_mqtt_input(store, tmap, "sensors/tank", b"42") is True   # new value
    assert store == {"tank_level": 42}
    assert w.handle_mqtt_input(store, tmap, "sensors/tank", b"42") is False  # unchanged
    assert w.handle_mqtt_input(store, tmap, "sensors/tank", b"43") is True   # changed
    assert store == {"tank_level": 43}
    assert w.handle_mqtt_input(store, tmap, "sensors/tank", b"bad") is False  # junk ignored
    assert store == {"tank_level": 43}
    assert w.handle_mqtt_input(store, tmap, "other/topic", b"9") is False    # unknown topic
    assert store == {"tank_level": 43}


def test_event_driven_wake_hook():
    # The mqtt client wakes the loop only when a known input actually changes.
    try:
        import weather_mqtt as _w
        mq = {"client_id": "test-evt", "host": "localhost", "port": 1883}
        woke = []
        client = _w.make_mqtt_client(
            mq, [{"topic": "s/tank", "metric": "tank_level", "parse": "number"}],
            {}, on_input=lambda: woke.append(1))
    except Exception as e:
        raise _skip_if_optional(e)

    class _Msg:
        def __init__(self, t, p): self.topic, self.payload, self.qos, self.retain = t, p, 0, False
    client.on_message(client, None, _Msg("s/tank", b"10"))   # new -> wake
    client.on_message(client, None, _Msg("s/tank", b"10"))   # same -> no wake
    client.on_message(client, None, _Msg("s/tank", b"11"))   # changed -> wake
    client.on_message(client, None, _Msg("other", b"1"))     # unknown -> no wake
    assert len(woke) == 2
    # event_driven defaults on, and is a clean bool
    import copy
    cfg = w.validate_config(copy.deepcopy({
        "location": {"latitude": 1, "longitude": 2}, "user_agent": "x (a@b.com)",
        "mqtt": {}, "rules": [{"name": "r", "topic": "t", "on_match": "1",
                               "when": {"metric": "is_raining", "operator": "==", "value": True}}]}))
    assert cfg["event_driven"] is True


def test_main_once_cycle_offline():
    # Run one full poll cycle through main() with the network mocked, exercising
    # the refactored weather-cache / publish / state / history path end-to-end.
    import tempfile, os, sys, json, yaml
    p = tempfile.mktemp(suffix=".yaml")
    state = tempfile.mktemp(suffix=".json"); db = tempfile.mktemp(suffix=".db")
    aud = tempfile.mktemp(suffix=".log"); logf = tempfile.mktemp(suffix=".log")
    esf = tempfile.mktemp(suffix=".json")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
           "precipitation": {"lookback_hours": 24},
           "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
           "state_file": state, "audit_file": aud, "log_file": logf,
           "engine_state_file": esf,
           "history": {"enabled": True, "file": db, "retention_days": 14},
           "rules": [{"name": "rainflag", "topic": "facility/rain", "on_match": "ON",
                      "on_clear": "OFF",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))

    orig = (w.resolve_location, w.fetch_conditions, w.make_mqtt_client, sys.argv)
    calls = {"fetch": 0, "pub": []}

    class _Info:  rc = 0
    class _Fake:
        def publish(self, topic, payload, **k): calls["pub"].append((topic, payload)); return _Info()
        def is_connected(self): return True
        def connect_async(self, *a, **k): pass
        def loop_start(self): pass
        def loop_stop(self): pass
        def disconnect(self): pass
    try:
        w.resolve_location = lambda *a, **k: {"office": "x"}
        def _fetch(loc, ua, lookback):
            calls["fetch"] += 1
            return {"temperature": 60.0, "humidity": 80.0, "wind_speed_mph": 5.0,
                    "is_raining": True, "precip_accum_in": 0.3,
                    "precipitation_probability": 90.0, "short_forecast": "Rain",
                    "active_alerts": []}
        w.fetch_conditions = _fetch
        w.make_mqtt_client = lambda *a, **k: _Fake()
        sys.argv = ["weather_mqtt", "--config", p, "--once"]
        w.main()
        assert calls["fetch"] == 1                                   # one weather fetch
        assert ("facility/rain", "ON") in calls["pub"]              # rule published ON
        st = json.loads(open(state).read())
        assert st["metrics"]["is_raining"] is True and st["rules"]
        assert "temperature" in w.read_history(db, hours=24)        # history recorded
    finally:
        w.resolve_location, w.fetch_conditions, w.make_mqtt_client, sys.argv = orig
        for f in (p, state, db, aud, logf, esf):
            for s in ("", ".bak", ".tmp"):
                try: os.unlink(f + s)
                except OSError: pass


def test_mqtt_in_validation_catalogue_and_rules():
    cfg = w.validate_config(_min_cfg(
        mqtt_inputs=[{"topic": "sensors/tank/level", "metric": "tank_level", "parse": "number"},
                     {"topic": "sensors/door", "metric": "door_open", "parse": "bool"}],
        rules=[{"name": "r", "topic": "t", "on_match": "1",
                "when": {"all": [
                    {"metric": "tank_level", "operator": "<", "value": 20},
                    {"metric": "door_open", "operator": "==", "value": True},
                ]}}]))
    cat = w.metric_catalogue(cfg)
    assert cat["tank_level"]["type"] == "number" and cat["door_open"]["type"] == "bool"
    # the rule evaluates against merged mqtt_in values
    rule = cfg["rules"][0]
    assert w.evaluate_rule(rule, {"tank_level": 10, "door_open": True}) is True
    assert w.evaluate_rule(rule, {"tank_level": 30, "door_open": True}) is False
    # rejections: missing topic, bad parse, collision with a built-in metric
    for inputs, needle in [
        ([{"metric": "x", "parse": "number"}], "needs a 'topic'"),
        ([{"topic": "t", "metric": "x", "parse": "weird"}], "parse must be one of"),
        ([{"topic": "t", "metric": "temperature", "parse": "number"}], "collides"),
        ([{"topic": "t", "metric": "bad name", "parse": "number"}], "alphanumeric"),
    ]:
        try:
            w.validate_config(_min_cfg(mqtt_inputs=inputs))
            raise AssertionError(f"expected ValueError for {inputs}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"


def test_http_extract_coerce_and_map():
    data = {"current_kw": 3.5, "online": True, "phases": [{"volts": 240}, {"volts": 241}],
            "name": "meter1"}
    assert w.extract_path(data, "current_kw") == 3.5
    assert w.extract_path(data, "$.phases.1.volts") == 241      # leading $., list index
    assert w.extract_path(data, "phases.9.volts") is None       # out of range -> None
    assert w.extract_path(data, "missing.key") is None
    assert w.coerce_value("12.5", "number") == 12.5
    assert w.coerce_value(True, "number") is None               # bool isn't a number
    assert w.coerce_value("yes", "bool") is True
    assert w.coerce_value(240, "string") == "240"
    store = {}
    w.apply_http_map(data, [
        {"metric": "power_kw", "path": "current_kw", "type": "number"},
        {"metric": "grid_up", "path": "online", "type": "bool"},
        {"metric": "v1", "path": "phases.0.volts", "type": "number"},
        {"metric": "absent", "path": "nope", "type": "number"},   # missing -> skipped
    ], store)
    assert store == {"power_kw": 3.5, "grid_up": True, "v1": 240}


def test_poll_http_inputs_due_logic_and_failsafe():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    calls = []

    def fake_fetch(url, timeout, ua):
        calls.append(url)
        return {"v": 7} if url.endswith("ok") else None

    inputs = [{"url": "http://x/ok", "interval_minutes": 5, "timeout": 10,
               "map": [{"metric": "kw", "path": "v", "type": "number"}]}]
    store, last = {}, {}
    w.poll_http_inputs(inputs, store, last, t0, "ua", fetch=fake_fetch)
    assert store == {"kw": 7} and len(calls) == 1
    # not due yet (2 min < 5 min) -> no fetch
    w.poll_http_inputs(inputs, store, last, t0 + timedelta(minutes=2), "ua", fetch=fake_fetch)
    assert len(calls) == 1
    # due again
    w.poll_http_inputs(inputs, store, last, t0 + timedelta(minutes=6), "ua", fetch=fake_fetch)
    assert len(calls) == 2
    # a failed fetch keeps the last good value
    inputs[0]["url"] = "http://x/bad"
    last.clear()
    w.poll_http_inputs(inputs, store, last, t0 + timedelta(minutes=12), "ua", fetch=fake_fetch)
    assert store == {"kw": 7}


def test_http_inputs_validation_and_catalogue():
    cfg = w.validate_config(_min_cfg(
        http_inputs=[{"url": "https://meter.local/api", "interval_minutes": 5,
                      "map": [{"metric": "power_kw", "path": "current_kw", "type": "number"}]}],
        rules=[{"name": "r", "topic": "t", "on_match": "1",
                "when": {"metric": "power_kw", "operator": ">", "value": 5}}]))
    assert w.metric_catalogue(cfg)["power_kw"]["type"] == "number"
    assert cfg["http_inputs"][0]["interval_minutes"] == 5
    for inputs, needle in [
        ([{"url": "ftp://x", "map": [{"metric": "a", "path": "b"}]}], "http:// or https://"),
        ([{"url": "https://x", "map": []}], "non-empty list"),
        ([{"url": "https://x", "map": [{"metric": "temperature", "path": "b"}]}], "collides"),
        ([{"url": "https://x", "map": [{"metric": "a", "path": ""}]}], "needs a 'path'"),
    ]:
        try:
            w.validate_config(_min_cfg(http_inputs=inputs))
            raise AssertionError(f"expected ValueError for {inputs}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"


def test_is_daytime_and_schedule_inclusion():
    from datetime import datetime, timezone
    lat, lon = 41.25, -74.27        # New York area (EDT, UTC-4 in June)
    noon = datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc)    # ~12:00 EDT
    midnight = datetime(2026, 6, 28, 4, 0, tzinfo=timezone.utc)  # ~00:00 EDT
    assert w.is_daytime(lat, lon, noon) is True
    assert w.is_daytime(lat, lon, midnight) is False
    # polar: high north latitude in June = midnight sun; in December = polar night
    jun = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    dec = datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc)
    assert w.is_daytime(80.0, 0.0, jun) is True
    assert w.is_daytime(80.0, 0.0, dec) is False
    # schedule_metrics includes the flag only when lat/lon are supplied
    assert "time_is_daytime" not in w.schedule_metrics(noon)
    assert w.schedule_metrics(noon, lat, lon)["time_is_daytime"] is True


def test_read_audit_newest_first_and_robust():
    import tempfile, os
    p = tempfile.mktemp(suffix=".log")
    try:
        assert w.read_audit(p) == []                       # missing file
        w.audit(p, device="pump", state="on", source="auto", by="monitor")
        w.audit(p, device="pump", action="manual_set", state="off", by="admin")
        open(p, "a").write("{ not json\n\n")               # junk lines ignored
        ev = w.read_audit(p, 10)
        assert len(ev) == 2 and ev[0]["state"] == "off"    # newest first
        assert ev[1]["device"] == "pump"
        for i in range(5):
            w.audit(p, device="d%d" % i, state="on", source="auto", by="monitor")
        assert len(w.read_audit(p, 3)) == 3                # limit keeps newest
    finally:
        try: os.unlink(p)
        except OSError: pass


def test_webui_inputs_editor():
    """The Inputs page saves variables / mqtt_inputs / http_inputs to config and
    the new metrics become available to rules; collisions are rejected."""
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml, json
    p = tempfile.mktemp(suffix=".yaml")
    cfg = {
        "version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
        "precipitation": {"lookback_hours": 24},
        "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
        "web": {"enabled": True, "host": "0.0.0.0", "port": 8080, "username": "", "password": ""},
        "rules": [{"name": "r", "topic": "t", "on_match": "1",
                   "when": {"metric": "is_raining", "operator": "==", "value": True}}],
    }
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    try:
        assert b'href="/inputs"' in c.get("/").data        # nav link
        assert c.get("/inputs").status_code == 200
        payload = {
            "variables": [{"name": "maintenance_mode", "type": "bool", "default": "true"},
                          {"name": "setpoint", "type": "number", "default": "72"}],
            "mqtt_inputs": [{"topic": "sensors/tank", "metric": "tank_level", "parse": "number"}],
            "http_inputs": [{"url": "https://meter.local/api", "interval_minutes": "5",
                             "timeout": "10", "map": [{"metric": "power_kw", "path": "current_kw",
                                                       "type": "number"}]}],
        }
        r = c.post("/inputs", data={"inputs_json": json.dumps(payload)})
        assert b"Inputs saved" in r.data
        saved = yaml.safe_load(open(p))
        assert saved["variables"] == {"maintenance_mode": {"type": "bool", "default": True},
                                      "setpoint": {"type": "number", "default": 72}}
        assert saved["mqtt_inputs"][0]["metric"] == "tank_level"
        assert saved["http_inputs"][0]["map"][0] == {"metric": "power_kw", "path": "current_kw",
                                                     "type": "number"}
        # the monitor accepts what the UI wrote, and the new metrics are in the catalogue
        w.validate_config(yaml.safe_load(open(p)))
        assert "var_maintenance_mode" in w.metric_catalogue(saved)
        assert "tank_level" in w.metric_catalogue(saved)
        # a metric-name collision with a built-in is rejected
        bad = {"mqtt_inputs": [{"topic": "t", "metric": "temperature", "parse": "number"}]}
        assert b"collides" in c.post("/inputs", data={"inputs_json": json.dumps(bad)}).data
    finally:
        for s in ("", ".bak", ".tmp"):
            try: os.unlink(p + s)
            except OSError: pass


def test_value_metric_comparison():
    # Compare a metric to another metric's live value (number and bool).
    cond = {"metric": "tank_level", "operator": "<", "value_metric": "tank_setpoint"}
    assert w._eval_condition(cond, {"tank_level": 40, "tank_setpoint": 50}, "r") is True
    assert w._eval_condition(cond, {"tank_level": 60, "tank_setpoint": 50}, "r") is False
    # rhs unavailable -> None (fail-safe hold), not a crash
    assert w._eval_condition(cond, {"tank_level": 60}, "r") is None
    # validation: only numeric-compare operators, both sides number/bool
    specs = {**w.METRIC_SPECS,
             "tank_level": {"type": "number", "ops": w.NUMBER_OPS},
             "tank_setpoint": {"type": "number", "ops": w.NUMBER_OPS}}
    w._validate_condition(cond, "r", specs)                      # ok
    # comparing to a text metric is rejected (it has no numeric-compare op)
    specs_txt = {**specs, "label": {"type": "text", "ops": w.TEXT_OPS}}
    for bad, needle in [
        ({"metric": "tank_level", "operator": "between", "value_metric": "tank_setpoint"}, "only works with"),
        ({"metric": "tank_level", "operator": "<", "value_metric": "nope"}, "not a known metric"),
        ({"metric": "tank_level", "operator": "<", "value_metric": "label"}, "must be a number/bool"),
    ]:
        try:
            w._validate_condition(bad, "r", specs_txt)
            assert False, f"expected rejection: {needle}"
        except ValueError as e:
            assert needle in str(e), f"{needle!r} not in {e}"


def test_regex_operator():
    # Text metric regex (case-insensitive) and alert regex.
    fc = {"metric": "short_forecast", "operator": "regex", "value": "^(light|heavy) rain"}
    assert w._eval_condition(fc, {"short_forecast": "Light Rain"}, "r") is True
    assert w._eval_condition(fc, {"short_forecast": "Sunny"}, "r") is False
    al = {"metric": "active_alert", "operator": "regex", "value": "flood|tornado"}
    assert w._eval_condition(al, {"active_alerts": ["Flood Warning"]}, "r") is True
    assert w._eval_condition(al, {"active_alerts": ["Heat Advisory"]}, "r") is False
    # regex is offered for text metrics and validated as a pattern
    assert "regex" in w.METRIC_SPECS["short_forecast"]["ops"]
    w._validate_condition(fc, "r")                               # ok
    try:
        w._validate_condition({"metric": "short_forecast", "operator": "regex",
                               "value": "([unclosed"}, "r")
        assert False, "expected invalid-regex rejection"
    except ValueError as e:
        assert "invalid" in str(e)


def test_computed_metrics_eval_and_validate():
    # Safe arithmetic: ordered eval, dependency on earlier computed, fail-safe None.
    computed = {"net_power": {"expr": "power_kw - solar_kw"},
                "headroom": {"expr": "(net_power) * 2 + 1"}}
    out = w.compute_metrics(computed, {"power_kw": 5, "solar_kw": 3})
    assert out == {"net_power": 2, "headroom": 5}
    assert w.compute_metrics(computed, {"power_kw": 5})["headroom"] is None   # missing input
    assert w.compute_metrics({"r": {"expr": "a / b"}}, {"a": 1, "b": 0})["r"] is None  # div by zero
    # bools coerce to 0/1 so flags can drive arithmetic
    assert w.compute_metrics({"x": {"expr": "is_raining * 10"}}, {"is_raining": True}) == {"x": 10}
    # unsafe expressions are rejected at compile time
    for bad in ("__import__('os')", "a.b", "foo(1)", "a if b else c"):
        try:
            w.compile_expr(bad); assert False, f"expected rejection of {bad!r}"
        except ValueError:
            pass
    # validation: collision, unknown ref, missing expr, cycle-by-forward-ref
    taken = set(w.METRIC_SPECS) | {"power_kw", "solar_kw"}
    w._validate_computed({"net_power": {"expr": "power_kw - solar_kw"}}, taken)
    for bad, needle in [
        ({"temperature": {"expr": "1+1"}}, "collides"),
        ({"x": {"expr": "missing + 1"}}, "unknown metric"),
        ({"x": {"expr": "power_kw"}, "y": {"expr": "z + 1"}}, "unknown metric"),  # forward ref to later 'z'
        ({"x": {}}, "needs an 'expr'"),
    ]:
        try:
            w._validate_computed(bad, taken); assert False, f"expected: {needle}"
        except ValueError as e:
            assert needle in str(e), f"{needle!r} not in {e}"


def test_rule_actions_template_validate_and_fire():
    # {{metric}} templating (bools render true/false; unknown renders empty)
    assert w.render_template("lvl={{tank}} on={{flag}} x={{nope}}",
                             {"tank": 7, "flag": True}) == "lvl=7 on=true x="
    # validation: trigger + exactly-one-type + per-type required fields
    ok = [{"trigger": "match", "mqtt": {"topic": "a/cmd", "payload": "ON", "qos": 1}},
          {"webhook": {"url": "https://h/", "method": "POST", "body": "{{tank}}"}},
          {"trigger": "clear", "notify": {"text": "cleared"}}]
    assert len(w._validate_actions(ok, "r")) == 3
    for bad, needle in [
        ([{"trigger": "match"}], "exactly one"),
        ([{"mqtt": {}}], "needs a 'topic'"),
        ([{"webhook": {"method": "POST"}}], "needs a 'url'"),
        ([{"webhook": {"url": "h", "method": "DELETE"}}], "method must be"),
        ([{"notify": {"text": ""}}], "needs 'text'"),
        ([{"trigger": "never", "notify": {"text": "x"}}], "'trigger' must be"),
        ([{"mqtt": {"topic": "t"}, "notify": {"text": "x"}}], "exactly one"),
    ]:
        try:
            w._validate_actions(bad, "r"); assert False, f"expected: {needle}"
        except ValueError as e:
            assert needle in str(e), f"{needle!r} not in {e}"

    # fire_actions: only the matching trigger fires; templating applied; dry-run safe
    class _Info:  rc = 0
    class _Fake:
        def __init__(self): self.pub = []
        def publish(self, *a, **k): self.pub.append((a, k)); return _Info()
    fc = _Fake()
    rule = {"name": "r", "actions": [
        {"trigger": "match", "mqtt": {"topic": "a/cmd", "payload": "v={{tank}}", "qos": 2, "retain": False}},
        {"trigger": "clear", "mqtt": {"topic": "a/cmd", "payload": "OFF"}},
        {"trigger": "both", "mqtt": {"topic": "log", "payload": "x"}}]}
    w.fire_actions(rule, True, {"tank": 9}, fc, 1, True, {})     # match + both
    assert [p[0] for p in fc.pub] == [("a/cmd", "v=9"), ("log", "x")]
    assert fc.pub[0][1] == {"qos": 2, "retain": False}           # action overrides defaults
    fc.pub.clear()
    w.fire_actions(rule, False, {"tank": 9}, fc, 1, True, {})    # clear + both
    assert [p[0][0] for p in fc.pub] == ["a/cmd", "log"]
    # dry-run (client None) never raises and never publishes
    w.fire_actions(rule, True, {"tank": 9}, None, 1, True, {})


def test_full_config_with_actions_validates():
    import copy
    cfg = {"version": 1, "location": {"latitude": 41, "longitude": -74},
           "user_agent": "x (a@b.com)", "mqtt": {},
           "rules": [{"name": "r", "topic": "t", "on_match": "ON", "on_clear": "OFF",
                      "when": {"metric": "is_raining", "operator": "==", "value": True},
                      "actions": [{"trigger": "match", "webhook": {"url": "https://h/x"}},
                                  {"notify": {"text": "rain={{is_raining}}"}}]}]}
    out = w.validate_config(copy.deepcopy(cfg))
    assert len(out["rules"][0]["actions"]) == 2
    bad = copy.deepcopy(cfg); bad["rules"][0]["actions"] = [{"webhook": {"method": "POST"}}]
    try:
        w.validate_config(bad); assert False
    except ValueError as e:
        assert "needs a 'url'" in str(e)


def test_fire_actions_audits():
    # Each fired action is recorded to the audit log (so Activity can show it),
    # with the kind, target, trigger, and an ok flag.
    import tempfile, os
    aud = tempfile.mktemp(suffix=".log")
    class _Info:  rc = 0
    class _Fake:
        def publish(self, *a, **k): return _Info()
    rule = {"name": "vent", "actions": [
        {"trigger": "match", "mqtt": {"topic": "facility/fan", "payload": "v={{t}}"}},
        {"trigger": "clear", "notify": {"text": "off"}}]}
    try:
        w.fire_actions(rule, True, {"t": 9}, _Fake(), 1, True, {}, aud)   # match -> mqtt only
        events = w.read_audit(aud, 50)
        assert len(events) == 1
        e = events[0]
        assert e["action"] == "action_fired" and e["device"] == "vent"
        assert e["kind"] == "mqtt" and e["target"] == "facility/fan"
        assert e["trigger"] == "match" and e["ok"] is True
        # dry-run (client None) records nothing
        w.fire_actions(rule, True, {"t": 9}, None, 1, True, {}, aud)
        assert len(w.read_audit(aud, 50)) == 1
    finally:
        try: os.unlink(aud)
        except OSError: pass


def test_webui_rule_actions_roundtrip():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, json, yaml
    p = tempfile.mktemp(suffix=".yaml")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
           "precipitation": {"lookback_hours": 24},
           "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
           "web": {"enabled": True, "host": "0.0.0.0", "port": 8080, "username": "", "password": ""},
           "rules": [{"name": "r", "topic": "t", "on_match": "1",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    try:
        assert b"Extra actions" in c.get("/rules").data        # actions editor present
        rules = [{"name": "vent", "topic": "facility/vent", "on_match": "ON", "on_clear": "OFF",
                  "enabled": True, "combine": "any",
                  "conditions": [{"metric": "temperature", "operator": ">", "value": "85"}],
                  "actions": [
                      {"kind": "mqtt", "on": "match", "topic": "facility/fan", "payload": "RUN {{temperature}}"},
                      {"kind": "webhook", "on": "both", "url": "https://h/x", "method": "POST", "body": "t={{temperature}}"},
                      {"kind": "notify", "on": "clear", "text": "vent cleared"}]}]
        c.post("/rules", data={"mode": "form", "rules_json": json.dumps(rules)})
        saved = yaml.safe_load(open(p))["rules"][0]
        acts = saved["actions"]
        # stored with the collision-safe `trigger` key (NOT `on`), correct values
        assert [a["trigger"] for a in acts] == ["match", "both", "clear"]
        assert acts[0]["mqtt"]["payload"] == "RUN {{temperature}}"
        assert acts[1]["webhook"]["url"] == "https://h/x"
        # the monitor accepts it, and it round-trips back to the builder shape
        w.validate_config(yaml.safe_load(open(p)))
        st = webui._rule_to_structured(saved)
        assert [(a["kind"], a["on"]) for a in st["actions"]] == \
            [("mqtt", "match"), ("webhook", "both"), ("notify", "clear")]
        # a malformed action is rejected with a clear message
        bad = [{"name": "b", "topic": "t", "on_match": "1", "enabled": True, "combine": "any",
                "conditions": [{"metric": "is_raining", "operator": "==", "value": "true"}],
                "actions": [{"kind": "notify", "on": "match", "text": "hi"},
                            {"kind": "mqtt", "on": "match", "topic": ""}]}]  # empty topic -> skipped, ok
        r = c.post("/rules", data={"mode": "form", "rules_json": json.dumps(bad)})
        assert b"Rules saved" in r.data or b"saved" in r.data
        # per-action QoS/retain from the form builder persist (and omit when unset)
        q = [{"name": "q", "topic": "t", "on_match": "1", "enabled": True, "combine": "any",
              "conditions": [{"metric": "is_raining", "operator": "==", "value": "true"}],
              "actions": [{"kind": "mqtt", "on": "match", "topic": "a/cmd", "payload": "GO", "qos": 2, "retain": True},
                          {"kind": "mqtt", "on": "clear", "topic": "b/cmd", "payload": "OFF", "qos": None, "retain": False}]}]
        c.post("/rules", data={"mode": "form", "rules_json": json.dumps(q)})
        qa = yaml.safe_load(open(p))["rules"][0]["actions"]
        assert qa[0]["mqtt"]["qos"] == 2 and qa[0]["mqtt"]["retain"] is True
        assert "qos" not in qa[1]["mqtt"] and "retain" not in qa[1]["mqtt"]   # unset -> omitted
        w.validate_config(yaml.safe_load(open(p)))
        qs = webui._rule_to_structured(yaml.safe_load(open(p))["rules"][0])
        assert (qs["actions"][0]["qos"], qs["actions"][0]["retain"]) == (2, True)
    finally:
        for s in ("", ".bak", ".tmp"):
            try: os.unlink(p + s)
            except OSError: pass


def test_mqtt_console_buffer_and_publish():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    con = webui.MqttConsole(buffer_size=3)
    con.record("sensors/a", b"1", qos=0, retain=True)
    con.record("sensors/b", b"hello", qos=1, retain=False)
    con.record("other/c", b"\xff\xfe", qos=0, retain=False)  # binary payload
    # ring buffer caps at 3; a 4th drops the oldest
    con.record("sensors/a", b"2", qos=0, retain=True)
    msgs = con.messages()
    assert len(msgs) == 3 and msgs[0]["topic"] == "sensors/b"   # oldest ("sensors/a"/1) evicted
    assert msgs[-1]["payload"] == "2"
    # incremental fetch by seq, and topic-prefix filter
    last = msgs[-1]["seq"]
    assert con.messages(since=last) == []
    assert [m["topic"] for m in con.messages(topic="sensors/")] == ["sensors/b", "sensors/a"]
    # binary payloads are flagged, never crash (other/c survived the eviction)
    assert con.messages(topic="other/")[0]["binary"] is True
    # per-topic latest map tracks the newest value + a count
    tops = {t["topic"]: t for t in con.topics()}
    assert tops["sensors/a"]["payload"] == "2" and tops["sensors/a"]["count"] == 2
    # publish guards: wildcards/empty rejected; not-connected rejected
    assert con.publish("", "x")[0] is False
    assert con.publish("a/#", "x")[0] is False
    assert con.publish("a/b", "x")[0] is False           # no client/connection
    class _Info:  rc = 0
    class _Fake:
        def __init__(self): self.calls = []
        def publish(self, *a, **k): self.calls.append((a, k)); return _Info()
    con._client = _Fake(); con._connected = True
    ok, err = con.publish("facility/cmd", "ON", qos=1, retain=True)
    assert ok and err is None and con._client.calls[0][0][0] == "facility/cmd"


def test_mqtt_console_latest_lookup():
    # latest(topic) returns the live value a subscriber currently holds on an
    # exact topic (newest wins), a defensive copy, and None when nothing's seen.
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    con = webui.MqttConsole()
    assert con.latest("irrigation/rain_inhibit") is None       # nothing seen yet
    assert con.latest("") is None and con.latest(None) is None
    con.record("irrigation/rain_inhibit", b"INHIBIT", qos=1, retain=True)
    con.record("irrigation/rain_inhibit", b"ALLOW", qos=1, retain=False)   # newer wins
    v = con.latest("irrigation/rain_inhibit")
    assert v["payload"] == "ALLOW" and v["retain"] is False
    # a returned copy must not let a caller mutate the console's internal map
    v["payload"] = "TAMPERED"
    assert con.latest("irrigation/rain_inhibit")["payload"] == "ALLOW"


def test_webui_state_mirrors_bus_value():
    # /api/state annotates each rule with the live value its topic holds on the
    # broker (bus_payload/retain/ts) so a value published to the bus -- even by
    # hand -- is mirrored on the dashboard and any divergence from the
    # controller's decision is visible.
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, json, yaml
    from datetime import datetime, timezone
    p = tempfile.mktemp(suffix=".yaml")
    state = tempfile.mktemp(suffix=".json")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
           "precipitation": {"lookback_hours": 24},
           "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
           "web": {"enabled": True, "host": "0.0.0.0", "port": 8080,
                   "username": "", "password": ""},
           "state_file": state,
           "rules": [{"name": "irrigation_rain_inhibit",
                      "topic": "irrigation/rain_inhibit", "on_match": "INHIBIT",
                      "on_clear": "ALLOW",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    # Monitor snapshot: the controller DECIDED ALLOW (active False -> on_clear).
    open(state, "w").write(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mqtt_connected": True, "manual_control": False, "metrics": {}, "variables": [],
        "rules": [{"name": "irrigation_rain_inhibit",
                   "topic": "irrigation/rain_inhibit", "enabled": True,
                   "active": False, "current_payload": "ALLOW", "last_change": None}]}))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    saved_console = webui.console
    webui.console = webui.MqttConsole()
    c = webui.app.test_client()
    try:
        # Nothing on the bus yet -> bus_payload null, but the summary is present.
        j = c.get("/api/state").get_json()
        assert j["bus"]["enabled"] is True
        assert j["rules"][0]["bus_payload"] is None
        # Someone publishes INHIBIT to the bus (retained); the console sees it.
        webui.console.record("irrigation/rain_inhibit", b"INHIBIT", qos=1, retain=True)
        row = c.get("/api/state").get_json()["rules"][0]
        assert row["bus_payload"] == "INHIBIT" and row["bus_retain"] is True
        # ...while the controller's decision is still ALLOW -> divergence is visible.
        assert row["current_payload"] == "ALLOW"
        assert row["bus_payload"] != row["current_payload"]
    finally:
        webui.console = saved_console
        for f in (p, state):
            try: os.unlink(f)
            except OSError: pass


def test_webui_mqtt_console_api_and_publish_gating():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, json, base64, yaml
    p = tempfile.mktemp(suffix=".yaml")
    aud = tempfile.mktemp(suffix=".log")

    def write_cfg(allow_publish, login=True):
        web = {"enabled": True, "host": "0.0.0.0", "port": 8080,
               "allow_mqtt_publish": allow_publish}
        web["username"], web["password"] = ("admin", "pw") if login else ("", "")
        cfg = {
            "version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
            "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
            "precipitation": {"lookback_hours": 24},
            "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
            "web": web, "audit_file": aud,
            "rules": [{"name": "r", "topic": "t", "on_match": "1",
                       "when": {"metric": "is_raining", "operator": "==", "value": True}}],
        }
        open(p, "w").write(yaml.safe_dump(cfg))

    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    webui.console = webui.MqttConsole()        # fresh, isolated console
    c = webui.app.test_client()
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    try:
        write_cfg(allow_publish=True)
        # page + nav link
        assert c.get("/mqtt", headers=hdr).status_code == 200
        assert b'href="/mqtt"' in c.get("/", headers=hdr).data
        # feed reflects recorded messages and the publish gate
        webui.console.record("sensors/x", b"7", qos=0, retain=True)
        j = c.get("/api/mqtt?topics=1", headers=hdr).get_json()
        assert j["enabled"] is True and j["can_publish"] is True
        assert j["messages"][0]["topic"] == "sensors/x"
        assert j["topic_list"][0]["topic"] == "sensors/x"
        # publish succeeds with a fake broker client and is audited
        class _Info:  rc = 0
        class _Fake:
            def publish(self, *a, **k): return _Info()
        webui.console._client = _Fake(); webui.console._connected = True
        r = c.post("/api/mqtt/publish", headers=hdr,
                   json={"topic": "facility/cmd", "payload": "ON", "qos": 1, "retain": True})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        assert json.loads(open(aud).read().splitlines()[-1])["action"] == "mqtt_publish"
        # fail-closed: turn publishing off -> 403 even with a valid login
        write_cfg(allow_publish=False)
        r = c.post("/api/mqtt/publish", headers=hdr, json={"topic": "facility/cmd", "payload": "ON"})
        assert r.status_code == 403
        assert c.get("/api/mqtt", headers=hdr).get_json()["can_publish"] is False
    finally:
        for f in (p, aud):
            for s in ("", ".bak", ".tmp"):
                try: os.unlink(f + s)
                except OSError: pass


def test_allow_mqtt_publish_requires_login():
    # Fail-closed at the config layer: enabling publish without a login is forced off.
    cfg = {
        "location": {"latitude": 1, "longitude": 2}, "user_agent": "x (a@b.com)",
        "mqtt": {}, "web": {"allow_mqtt_publish": True},  # no username/password
        "rules": [{"name": "r", "topic": "t", "on_match": "1",
                   "when": {"metric": "temperature", "operator": "<", "value": 5}}],
    }
    out = w.validate_config(cfg)
    assert out["web"]["allow_mqtt_publish"] is False
    # with a login it sticks
    cfg2 = dict(cfg); cfg2["web"] = {"allow_mqtt_publish": True, "username": "a", "password": "b"}
    assert w.validate_config(cfg2)["web"]["allow_mqtt_publish"] is True
    # allow_anonymous_control unlocks it without a login (trusted-LAN opt-in)
    cfg3 = dict(cfg); cfg3["web"] = {"allow_mqtt_publish": True, "allow_anonymous_control": True}
    assert w.validate_config(cfg3)["web"]["allow_mqtt_publish"] is True


def test_webui_anonymous_control_no_login():
    # web.allow_anonymous_control lets manual control / publish work with NO
    # login (open UI on a trusted LAN). Publish succeeds with no auth header;
    # turning the flag off (still no login) returns to 403.
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml
    p = tempfile.mktemp(suffix=".yaml"); aud = tempfile.mktemp(suffix=".log")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
           "precipitation": {"lookback_hours": 24},
           "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
           "web": {"enabled": True, "host": "0.0.0.0", "port": 8080,
                   "username": "", "password": "",
                   "allow_manual_control": True, "allow_mqtt_publish": True,
                   "allow_anonymous_control": True},
           "audit_file": aud,
           "rules": [{"name": "pump", "topic": "t", "on_match": "1", "on_clear": "0",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    webui.console = webui.MqttConsole()
    c = webui.app.test_client()

    class _Info:
        rc = 0
    class _Fake:
        def publish(self, *a, **k): return _Info()
    try:
        # no auth header at all: UI open, publish + manual control allowed
        assert c.get("/mqtt").status_code == 200
        assert c.get("/api/mqtt").get_json()["can_publish"] is True
        webui.console._client = _Fake(); webui.console._connected = True
        r = c.post("/api/mqtt/publish",
                   json={"topic": "facility/cmd", "payload": "ON", "qos": 0})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        # manual control endpoint also works with no login
        r2 = c.post("/api/control", json={"device": "pump", "state": "on"})
        assert r2.status_code == 200 and w.load_overrides(
            cfg.get("overrides_file", "overrides.json")) == {"pump": "on"}
        # the CSRF guard still applies even in anonymous mode
        assert c.post("/api/mqtt/publish", json={"topic": "x", "payload": "y"},
                      headers={"Origin": "http://evil.example"}).status_code == 403
        # turn anonymous control OFF (still no login) -> back to 403 / can_publish false
        cfg["web"]["allow_anonymous_control"] = False
        open(p, "w").write(yaml.safe_dump(cfg))
        assert c.post("/api/mqtt/publish",
                      json={"topic": "facility/cmd", "payload": "ON"}).status_code == 403
        assert c.get("/api/mqtt").get_json()["can_publish"] is False
        assert c.post("/api/control", json={"device": "pump", "state": "off"}).status_code == 403
    finally:
        webui.console = webui.MqttConsole()
        for f in (p, aud, "overrides.json"):
            for s in ("", ".bak", ".tmp"):
                try: os.unlink(f + s)
                except OSError: pass


def test_webui_settings_saves_anonymous_control(_tmp=None):
    # The Settings form can enable anonymous control, and that relaxes the
    # login requirement on manual control / publish.
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml
    p = tempfile.mktemp(suffix=".yaml")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "mqtt": {"host": "localhost", "port": 1883},
           "web": {"enabled": True},
           "rules": [{"name": "r", "topic": "t", "on_match": "ON",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    form = {"latitude": "41", "longitude": "-74", "user_agent": "x (a@b.com)",
            "poll_interval_minutes": "15", "lookback_hours": "24",
            "mqtt_host": "localhost", "mqtt_port": "1883", "mqtt_qos": "1",
            "mqtt_retain": "true", "web_host": "0.0.0.0", "web_port": "8080",
            "web_username": "", "web_password": "",
            "web_allow_manual_control": "true", "web_allow_mqtt_publish": "true",
            "web_allow_anonymous_control": "true"}
    try:
        # anonymous control on -> manual/publish accepted with no login
        r = c.post("/settings", data=form, headers={"Origin": "http://localhost"})
        assert b"Settings saved" in r.data, r.data
        saved = yaml.safe_load(open(p))
        assert saved["web"]["allow_anonymous_control"] is True
        assert saved["web"]["allow_manual_control"] is True
        assert saved["web"]["allow_mqtt_publish"] is True
        # without anonymous control and no login -> the save is rejected
        form2 = dict(form, web_allow_anonymous_control="false")
        r = c.post("/settings", data=form2, headers={"Origin": "http://localhost"})
        assert b"needs a web login" in r.data
    finally:
        for s in ("", ".bak", ".tmp"):
            try: os.unlink(p + s)
            except OSError: pass


def test_webui_activity_page_and_audit_api():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml
    p = tempfile.mktemp(suffix=".yaml")
    aud = tempfile.mktemp(suffix=".log")
    cfg = {
        "version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
        "precipitation": {"lookback_hours": 24},
        "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
        "web": {"enabled": True, "host": "0.0.0.0", "port": 8080, "username": "", "password": ""},
        "audit_file": aud,
        "rules": [{"name": "r", "topic": "t", "on_match": "1",
                   "when": {"metric": "is_raining", "operator": "==", "value": True}}],
    }
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    try:
        w.audit(aud, device="pump", state="on", source="auto", by="monitor")
        r = c.get("/activity")
        assert r.status_code == 200 and b"Activity" in r.data and b"api/audit" in r.data
        assert b'href="/activity"' in c.get("/").data        # nav link present
        j = c.get("/api/audit").get_json()
        assert j["events"] and j["events"][0]["device"] == "pump"
    finally:
        for f in (p, aud):
            for s in ("", ".bak", ".tmp"):
                try: os.unlink(f + s)
                except OSError: pass


def test_read_log_parses_levels_and_continuations():
    import tempfile, os
    p = tempfile.mktemp(suffix=".log")
    open(p, "w").write(
        "2026-06-28 16:40:01,100 INFO Runtime log mirrored to monitor.log\n"
        "2026-06-28 16:40:02,200 WARNING Rule 'x' failed this cycle, skipping: boom\n"
        "Traceback (most recent call last):\n"
        "  File 'a.py', line 1, in <module>\n"
        "2026-06-28 16:40:03,300 ERROR Poll cycle failed: nope\n")
    try:
        lines = w.read_log(p, 100)
        assert lines[0]["level"] == "ERROR"            # newest first
        assert lines[1]["level"] == "WARNING"
        assert "Traceback" in lines[1]["msg"]          # continuation attached
        assert lines[2]["level"] == "INFO"
        assert w.read_log("", 100) == []               # disabled -> empty
        assert w.read_log(tempfile.mktemp(), 100) == []  # missing -> empty
    finally:
        os.unlink(p)


def test_history_record_read_and_prune():
    import tempfile, os
    from datetime import datetime, timezone, timedelta
    db = tempfile.mktemp(suffix=".db")
    now = datetime.now(timezone.utc)
    try:
        for i in range(6):
            ts = (now - timedelta(minutes=10 * (6 - i))).isoformat(timespec="seconds")
            w.record_history(db, {"temperature": 70 + i, "is_raining": i % 2 == 0,
                                  "short_forecast": "Sunny", "active_alerts": []},
                             ts=ts, retention_days=14)
        s = w.read_history(db, hours=24)
        # numeric + bool(as 0/1) recorded; text/list skipped
        assert set(s.keys()) == {"temperature", "is_raining"}
        assert [p[1] for p in s["temperature"]] == [70, 71, 72, 73, 74, 75]
        assert [p[1] for p in s["is_raining"]] == [1, 0, 1, 0, 1, 0]
        assert w.history_metrics(db) == ["is_raining", "temperature"]
        # name filter
        assert set(w.read_history(db, hours=24, names=["temperature"]).keys()) == {"temperature"}
        # retention prunes old rows
        w.record_history(db, {"temperature": 99},
                         ts=(now - timedelta(days=40)).isoformat(timespec="seconds"),
                         retention_days=14)
        assert len(w.read_history(db, hours=24 * 60)["temperature"]) == 6   # 40d-old pruned
        # a short window keeps only recent points (the 10/20/30 min-old ones)
        assert len(w.read_history(db, hours=1)["temperature"]) <= 6
        # missing db / disabled -> empty, never raises
        assert w.read_history("/nope/x.db") == {}
        assert w.history_metrics("") == []
        w.record_history("", {"temperature": 1})  # no-op, no raise
        # down-sampling caps the point count
        big = tempfile.mktemp(suffix=".db")
        for i in range(50):
            w.record_history(big, {"x": i}, ts=(now - timedelta(minutes=50 - i)).isoformat(timespec="seconds"))
        assert len(w.read_history(big, hours=24, max_points=10)["x"]) == 10
        os.unlink(big)
    finally:
        try: os.unlink(db)
        except OSError: pass


def test_webui_history_page_and_api():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml
    from datetime import datetime, timezone, timedelta
    p = tempfile.mktemp(suffix=".yaml")
    db = tempfile.mktemp(suffix=".db")
    now = datetime.now(timezone.utc)
    for i in range(5):
        w.record_history(db, {"temperature": 60 + i, "humidity": 40 + i},
                         ts=(now - timedelta(minutes=10 * (5 - i))).isoformat(timespec="seconds"))
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
           "precipitation": {"lookback_hours": 24},
           "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
           "web": {"enabled": True, "host": "0.0.0.0", "port": 8080, "username": "", "password": ""},
           "history": {"enabled": True, "file": db, "retention_days": 14},
           "rules": [{"name": "r", "topic": "t", "on_match": "1",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    try:
        r = c.get("/history")
        assert r.status_code == 200 and b"History" in r.data and b"api/history" in r.data
        assert b'href="/history"' in c.get("/").data           # nav link
        j = c.get("/api/history?hours=24").get_json()
        assert j["enabled"] is True
        assert set(j["available"]) == {"humidity", "temperature"}
        assert [pt[1] for pt in j["series"]["temperature"]] == [60, 61, 62, 63, 64]
        # Settings exposes the history toggle and round-trips a change
        assert b"history_enabled" in c.get("/settings").data
    finally:
        for f in (p, db):
            for s in ("", ".bak", ".tmp"):
                try: os.unlink(f + s)
                except OSError: pass


def test_webui_system_page_and_apis():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, json, yaml
    from datetime import datetime, timezone
    p = tempfile.mktemp(suffix=".yaml")
    state = tempfile.mktemp(suffix=".json")
    log = tempfile.mktemp(suffix=".log")
    cfg = {
        "version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
        "precipitation": {"lookback_hours": 24},
        "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
        "web": {"enabled": True, "host": "0.0.0.0", "port": 8080, "username": "", "password": ""},
        "state_file": state, "log_file": log,
        "variables": {"maintenance_mode": {"type": "bool", "default": False}},
        "mqtt_inputs": [{"topic": "s/level", "metric": "tank_level", "parse": "number"}],
        "rules": [
            {"name": "r1", "topic": "t1", "on_match": "1",
             "when": {"metric": "is_raining", "operator": "==", "value": True}},
            {"name": "r2", "enabled": False, "topic": "t2", "on_match": "1",
             "when": {"metric": "var_maintenance_mode", "operator": "==", "value": True}},
        ],
    }
    open(p, "w").write(yaml.safe_dump(cfg))
    # Fresh state snapshot so the monitor reads as "ok".
    open(state, "w").write(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mqtt_connected": True, "manual_control": False, "metrics": {}, "rules": [],
    }))
    open(log, "w").write(
        "2026-06-28 16:40:01,100 INFO Started.\n"
        "2026-06-28 16:40:02,200 ERROR Poll cycle failed: boom\n")
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    try:
        r = c.get("/system")
        assert r.status_code == 200 and b"System" in r.data and b"api/system" in r.data
        assert b'href="/system"' in c.get("/").data          # nav link present
        sysj = c.get("/api/system").get_json()
        assert sysj["config_ok"] is True and sysj["monitor"] == "ok"
        assert sysj["mqtt_connected"] is True
        s = sysj["summary"]
        assert s["rules_total"] == 2 and s["rules_enabled"] == 1
        assert s["variables"] == 1 and s["mqtt_inputs"] == 1
        assert "var_maintenance_mode" not in (sysj.get("error") or "")  # sanity
        assert s["metrics"] and s["metrics"] > 10                       # catalogue populated
        assert sysj["log_enabled"] is True and sysj["log_present"] is True
        logj = c.get("/api/logs").get_json()
        assert logj["enabled"] is True
        assert logj["lines"][0]["level"] == "ERROR"                     # newest first
    finally:
        for f in (p, state, log):
            for sfx in ("", ".bak", ".tmp"):
                try: os.unlink(f + sfx)
                except OSError: pass


def test_single_condition_rule():
    rule = {"name": "freeze", "when": {"metric": "temperature",
                                       "operator": "<=", "value": 35}}
    assert w.evaluate_rule(rule, {"temperature": 30}) is True
    assert w.evaluate_rule(rule, {"temperature": 40}) is False
    assert w.evaluate_rule(rule, {"temperature": None}) is None


def test_alert_and_text_metrics():
    assert w._eval_condition({"metric": "active_alert", "operator": "any"},
                             {"active_alerts": ["Flood Warning"]}, "r") is True
    assert w._eval_condition({"metric": "active_alert", "operator": "contains",
                              "value": "Winter"},
                             {"active_alerts": ["Flood Warning"]}, "r") is False
    assert w._eval_condition({"metric": "short_forecast", "operator": "contains",
                              "value": "rain"},
                             {"short_forecast": "Light Rain"}, "r") is True


def test_load_config_coerces_boolean_payloads(tmp=None):
    import tempfile, os, yaml
    cfg = {
        "location": {"latitude": 1, "longitude": 2},
        "user_agent": "x (a@b.com)",
        "mqtt": {},
        "rules": [{
            "name": "r", "topic": "t", "on_match": True, "on_clear": False,
            "when": {"metric": "temperature", "operator": "<", "value": 5},
        }],
    }
    p = tempfile.mktemp(suffix=".yaml")
    with open(p, "w") as f:
        yaml.safe_dump(cfg, f)
    try:
        loaded = w.load_config(p)
        r = loaded["rules"][0]
        assert r["on_match"] == "True" and isinstance(r["on_match"], str)
        assert r["on_clear"] == "False" and isinstance(r["on_clear"], str)
    finally:
        os.unlink(p)


def test_broker_watch_alert_and_recovery():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    bw = w.BrokerWatch(threshold_minutes=60)
    assert bw.update(True, t0) is None                                  # healthy
    assert bw.update(False, t0) is None                                 # just went down
    assert bw.update(False, t0 + timedelta(minutes=59)) is None         # not yet
    assert bw.update(False, t0 + timedelta(minutes=61)) == "down"       # threshold crossed
    assert bw.update(False, t0 + timedelta(minutes=120)) is None        # no re-alert
    assert bw.update(True, t0 + timedelta(minutes=121)) == "recovered"  # back up
    assert bw.update(True, t0 + timedelta(minutes=122)) is None         # steady


def test_broker_watch_brief_flap_no_alert():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    bw = w.BrokerWatch(threshold_minutes=60)
    bw.update(False, t0)
    # recovers before the threshold -> never alerted, so no "recovered" either
    assert bw.update(True, t0 + timedelta(minutes=5)) is None


def test_notify_slack_disabled_is_noop():
    # disabled, and missing token/channel: both must short-circuit without raising
    assert w.notify_slack({"enabled": False}, "x") is False
    assert w.notify_slack({"enabled": True, "channel": "", "bot_token": ""}, "x") is False


def test_slack_token_env_precedence():
    import os
    saved = os.environ.get("SLACK_BOT_TOKEN")
    try:
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-env"
        assert w.slack_token({"bot_token": "xoxb-config"}) == "xoxb-env"
        del os.environ["SLACK_BOT_TOKEN"]
        assert w.slack_token({"bot_token": "xoxb-config"}) == "xoxb-config"
    finally:
        if saved is not None:
            os.environ["SLACK_BOT_TOKEN"] = saved
        else:
            os.environ.pop("SLACK_BOT_TOKEN", None)


def test_validate_config_slack_defaults_and_clamp():
    cfg = w.validate_config(_min_cfg(slack={"enabled": True, "broker_unreachable_minutes": 0}))
    assert cfg["slack"]["enabled"] is True
    assert cfg["slack"]["broker_unreachable_minutes"] == 1   # clamped up from 0
    cfg2 = w.validate_config(_min_cfg())
    assert cfg2["slack"] == {"enabled": False, "bot_token": "", "channel": "",
                             "broker_unreachable_minutes": 60,
                             "stale_weather_minutes": 0}


def test_validate_config_status_push_defaults():
    cfg = w.validate_config(_min_cfg())
    assert cfg["status_push"] == {"enabled": False, "url": "", "token": ""}
    cfg2 = w.validate_config(_min_cfg(status_push={"enabled": True, "url": "https://x/i.php",
                                                   "token": "t"}))
    assert cfg2["status_push"]["enabled"] is True
    assert cfg2["status_push"]["url"] == "https://x/i.php"


def test_push_status_guards_and_payload():
    # disabled / missing url -> no-op, no network, returns False
    assert w.push_status({"enabled": False}, {"a": 1}) is False
    assert w.push_status({"enabled": True, "url": ""}, {"a": 1}) is False

    # enabled -> posts the snapshot with the token header (stub requests.post)
    captured = {}

    class _Resp:
        status_code = 200

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp()

    real = w.requests.post
    w.requests.post = _fake_post
    try:
        ok = w.push_status({"enabled": True, "url": "https://h/ingest.php", "token": "sek"},
                           {"updated": "t", "metrics": {}})
        assert ok is True
        assert captured["url"] == "https://h/ingest.php"
        assert captured["headers"]["X-Status-Token"] == "sek"
        assert captured["json"] == {"updated": "t", "metrics": {}}

        # non-2xx -> False
        class _Bad:
            status_code = 401
        w.requests.post = lambda *a, **k: _Bad()
        assert w.push_status({"enabled": True, "url": "https://h/i", "token": "x"}, {}) is False
    finally:
        w.requests.post = real


def test_setup_wizard_renders_valid_config():
    """Whatever the wizard collects, the file it would write must load + validate."""
    try:
        import setup_wizard
    except Exception as e:
        raise _skip_if_optional(e)
    import yaml
    answers = {
        "lat": 41.25, "lon": -74.27, "user_agent": "weather-mqtt-controller (a@b.com)",
        "threshold": 0.25, "lookback": 24, "poll": 15,
        "web_host": "0.0.0.0", "web_port": 8080, "web_user": "admin", "web_pass": "pw",
        "mqtt_host": "localhost", "mqtt_port": 1883, "mqtt_user": "", "mqtt_pass": "",
        "slack_enabled": "false", "slack_channel": "", "slack_token": "", "slack_minutes": 60,
    }
    parsed = yaml.safe_load(setup_wizard._render(answers))
    w.validate_config(parsed)
    assert parsed["web"]["username"] == "admin"
    assert parsed["location"]["latitude"] == 41.25
    # and with no web auth
    answers2 = dict(answers, web_user="", web_pass="")
    w.validate_config(yaml.safe_load(setup_wizard._render(answers2)))
    # answers containing quotes/backslashes must not break the YAML
    answers3 = dict(answers, web_pass='p"w\\d',
                    user_agent='weather ("quoted" contact)')
    parsed3 = yaml.safe_load(setup_wizard._render(answers3))
    w.validate_config(parsed3)
    assert parsed3["web"]["password"] == 'p"w\\d'
    assert parsed3["user_agent"] == 'weather ("quoted" contact)'


def test_detect_raining_ignores_vicinity_and_fog():
    # precip near but not at the station must NOT read as raining
    assert w.detect_raining({"textDescription": "Showers in Vicinity"}) is False
    assert w.detect_raining({"presentWeather": [{"weather": "rain", "inVicinity": True}]}) is False
    # freezing fog is not precipitation
    assert w.detect_raining({"textDescription": "Freezing Fog"}) is False
    # but real precip still detected, including freezing rain/drizzle
    assert w.detect_raining({"textDescription": "Freezing Rain"}) is True
    assert w.detect_raining({"textDescription": "Light Rain"}) is True


def test_to_mm_handles_inches():
    assert w.to_mm(1, "wmoUnit:[in_i]") == 25.4
    assert w.to_mm(0.5, "wmoUnit:in") == 12.7


def test_validate_rejects_bad_conditions():
    base = {"location": {"latitude": 1, "longitude": 2}, "user_agent": "x (a@b.com)",
            "mqtt": {}}
    cases = [
        ([{"name": "r", "topic": "t", "on_match": "X",
           "when": {"metric": "bogus", "operator": "<", "value": 1}}], "unknown metric"),
        ([{"name": "r", "topic": "t", "on_match": "X",
           "when": {"metric": "temperature", "operator": "contains", "value": 1}}], "not valid"),
        ([{"name": "r", "topic": "t", "on_match": "X",
           "when": {"metric": "temperature", "operator": "<", "value": "cold"}}], "must be a number"),
        ([{"name": "r", "topic": "t", "on_match": "X",
           "when": {"operator": "<", "value": 1}}], "needs a 'metric'"),
    ]
    for rules, needle in cases:
        try:
            w.validate_config(dict(base, rules=rules))
            raise AssertionError(f"expected ValueError containing {needle!r}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"
    # active_alert/any needs no value -> accepted
    w.validate_config(dict(base, rules=[{
        "name": "a", "topic": "t", "on_match": "1",
        "when": {"metric": "active_alert", "operator": "any"}}]))


def test_validate_rejects_duplicate_rule_names():
    cfg = _min_cfg(rules=[
        {"name": "dup", "topic": "t1", "on_match": "X",
         "when": {"metric": "temperature", "operator": "<", "value": 5}},
        {"name": "dup", "topic": "t2", "on_match": "Y",
         "when": {"metric": "humidity", "operator": ">", "value": 5}},
    ])
    try:
        w.validate_config(cfg)
        raise AssertionError("expected ValueError for duplicate rule name")
    except ValueError as e:
        assert "duplicate" in str(e)


def _min_cfg(**over):
    cfg = {
        "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)",
        "mqtt": {},
        "rules": [{
            "name": "r", "topic": "t", "on_match": "ON", "on_clear": "OFF",
            "when": {"metric": "temperature", "operator": "<", "value": 5},
        }],
    }
    cfg.update(over)
    return cfg


def test_validate_clamps_poll_and_lookback():
    cfg = w.validate_config(_min_cfg(poll_interval_minutes=0,
                                     precipitation={"lookback_hours": 100000}))
    assert cfg["poll_interval_minutes"] == w.MIN_POLL_MINUTES
    assert cfg["precipitation"]["lookback_hours"] == w.MAX_LOOKBACK_HOURS
    cfg = w.validate_config(_min_cfg(precipitation={"lookback_hours": -5}))
    assert cfg["precipitation"]["lookback_hours"] == w.MIN_LOOKBACK_HOURS


def test_validate_clamps_qos_and_port():
    cfg = w.validate_config(_min_cfg(mqtt={"qos": 9, "port": 99999}))
    assert cfg["mqtt"]["qos"] == 1
    assert cfg["mqtt"]["port"] == 8080  # clamp helper falls back on out-of-range


def test_validate_rejects_bad_coordinates():
    for bad in ({"latitude": 200, "longitude": 0}, {"latitude": 0, "longitude": 999}):
        try:
            w.validate_config(_min_cfg(location=bad))
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass


def test_validate_rejects_missing_user_agent():
    cfg = _min_cfg()
    cfg["user_agent"] = "   "
    try:
        w.validate_config(cfg)
        raise AssertionError("expected ValueError for blank user_agent")
    except ValueError:
        pass


def test_validate_version_default_and_accepts_one():
    # absent -> defaulted to the current schema version (back-compat)
    cfg = w.validate_config(_min_cfg())
    assert cfg["version"] == w.CURRENT_SCHEMA_VERSION == 1
    # explicit version: 1 is accepted unchanged
    cfg2 = w.validate_config(_min_cfg(version=1))
    assert cfg2["version"] == 1


def test_validate_rejects_unknown_version():
    # a future v2 file must be rejected clearly, not mis-parsed as v1
    for bad in (2, 0, "1", True):
        try:
            w.validate_config(_min_cfg(version=bad))
            raise AssertionError(f"expected ValueError for version={bad!r}")
        except ValueError:
            pass


def test_validate_rejects_empty_rules():
    try:
        w.validate_config(_min_cfg(rules=[]))
        raise AssertionError("expected ValueError for empty rules")
    except ValueError:
        pass


def test_validate_string_numbers_are_coerced():
    cfg = w.validate_config(_min_cfg(
        location={"latitude": "41.5", "longitude": "-74.2"},
        poll_interval_minutes="30"))
    assert cfg["location"]["latitude"] == 41.5
    assert cfg["poll_interval_minutes"] == 30


def test_webui_settings_roundtrip_and_validation():
    """Web UI save path: valid POST persists, invalid POST is rejected.

    Skipped automatically if Flask/ruamel (the optional web extras) aren't
    installed, so the monitor-only test run still passes.
    """
    try:
        import webui
    except Exception as e:  # Flask / ruamel not installed -> skip, don't fail
        raise _skip_if_optional(e)

    import tempfile, os, yaml
    base = {
        "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)",
        "poll_interval_minutes": 15,
        "precipitation": {"lookback_hours": 24},
        "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
        "rules": [{"name": "irrigation_rain_inhibit", "topic": "irrigation/rain_inhibit",
                   "on_match": "INHIBIT", "on_clear": "ALLOW",
                   "when": {"metric": "is_raining", "operator": "==", "value": True}}],
        "web": {"enabled": True, "host": "0.0.0.0", "port": 8080,
                "username": "", "password": ""},
    }
    p = tempfile.mktemp(suffix=".yaml")
    with open(p, "w") as f:
        yaml.safe_dump(base, f)
    webui.CONFIG_PATH = p
    app = webui.app
    app.config["TESTING"] = True
    c = app.test_client()
    try:
        good = {
            "latitude": "40.5", "longitude": "-75.5", "station_id": "KPHL",
            "poll_interval_minutes": "20", "user_agent": "y (c@d.com)",
            "lookback_hours": "12", "always_publish": "true",
            "mqtt_host": "broker", "mqtt_port": "8883", "mqtt_username": "u",
            "mqtt_password": "pw", "mqtt_client_id": "cid", "mqtt_qos": "2",
            "mqtt_retain": "false", "status_topic": "s/t",
            "web_host": "127.0.0.1", "web_port": "9090",
            "web_username": "", "web_password": "",
        }
        r = c.post("/settings", data=good)
        assert b"Settings saved" in r.data, "valid settings should save"
        saved = yaml.safe_load(open(p))
        assert saved["mqtt"]["qos"] == 2 and saved["mqtt"]["client_id"] == "cid"
        assert saved["web"]["port"] == 9090 and saved["poll_interval_minutes"] == 20
        # the monitor must still accept what the UI wrote
        w.validate_config(yaml.safe_load(open(p)))

        bad = dict(good, latitude="999")
        r = c.post("/settings", data=bad)
        assert b"Could not save" in r.data, "out-of-range latitude must be rejected"
        # file unchanged by the rejected save
        assert yaml.safe_load(open(p))["location"]["latitude"] == 40.5

        # healthz + api/state respond correctly (config here is valid, so a 500
        # would mean a broken endpoint, not a config problem).
        hz = c.get("/healthz")
        assert hz.status_code == 200 and hz.get_json().get("config_ok") is True
        st = c.get("/api/state")
        assert st.status_code in (200, 503)   # snapshot present or not -- never 500
    finally:
        for suffix in ("", ".bak", ".tmp"):
            try:
                os.unlink(p + suffix)
            except OSError:
                pass


def test_webui_structured_rule_builder():
    """The form builder's JSON -> rules conversion produces output the monitor
    accepts, with correctly typed values; bad input is rejected."""
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import yaml, copy

    items = [
        {"name": "irr", "description": "hold", "topic": "irrigation/x",
         "on_match": "INHIBIT", "on_clear": "ALLOW", "combine": "any",
         "conditions": [
             {"metric": "is_raining", "operator": "==", "value": "true"},
             {"metric": "precip_accum_in", "operator": ">=", "value": "0.25"}]},
        {"name": "freeze", "topic": "f/z", "on_match": "ON", "combine": "any",
         "conditions": [{"metric": "temperature", "operator": "<=", "value": "35"}]},
        {"name": "alert", "topic": "f/a", "on_match": "1", "combine": "any",
         "conditions": [{"metric": "active_alert", "operator": "any", "value": ""}]},
    ]
    built = webui._rules_from_structured(items)
    parsed = yaml.safe_load(webui.dump_raw({"rules": built}))["rules"]

    # value typing survived the YAML round-trip
    assert parsed[0]["when"]["any"][0]["value"] is True            # bool
    assert parsed[0]["when"]["any"][1]["value"] == 0.25            # float
    assert parsed[1]["when"]["value"] == 35                        # int, single condition flattened
    assert "value" not in parsed[2]["when"]                        # active_alert/any -> no value
    # the monitor accepts what the builder produced
    w.validate_config(copy.deepcopy({
        "location": {"latitude": 1, "longitude": 2}, "user_agent": "x (a@b)",
        "mqtt": {}, "rules": parsed}))

    # round-trips back to the editable shape
    struct = webui._rule_to_structured(parsed[0])
    assert struct["combine"] == "any" and len(struct["conditions"]) == 2
    assert struct["conditions"][0] == {"metric": "is_raining", "operator": "==",
                                       "value": "true", "value_metric": "", "for": ""}

    # rejections
    for bad, needle in [
        ([{"name": "", "topic": "t", "on_match": "x", "combine": "any",
           "conditions": [{"metric": "temperature", "operator": "<", "value": "1"}]}], "name is required"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any",
           "conditions": [{"metric": "bogus", "operator": "<", "value": "1"}]}], "unknown metric"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any",
           "conditions": [{"metric": "temperature", "operator": "contains", "value": "1"}]}], "not valid"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any",
           "conditions": [{"metric": "temperature", "operator": "<", "value": "abc"}]}], "numeric"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any", "conditions": []}], "at least one condition"),
        ([], "at least one rule"),
    ]:
        try:
            webui._rules_from_structured(bad)
            raise AssertionError(f"expected rejection containing {needle!r}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"


def test_webui_builder_advanced_constructs():
    """The form builder round-trips the constructs added in Phase 1: between/in,
    the value-less `changed` operator, a per-condition `for:` sustain, and a
    disabled rule -- producing YAML the monitor accepts."""
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import yaml, copy

    items = [
        {"name": "comfort", "topic": "f/comfort", "on_match": "ON", "on_clear": "OFF",
         "enabled": True, "combine": "all", "conditions": [
             {"metric": "temperature", "operator": "between", "value": "40, 80", "for": "10m"},
             {"metric": "humidity", "operator": "in", "value": "30, 50, 70", "for": ""},
         ]},
        {"name": "alert_pulse", "topic": "f/pulse", "on_match": "1", "enabled": True,
         "combine": "any", "conditions": [
             {"metric": "temperature", "operator": "changed", "value": "", "for": ""}]},
        {"name": "off_rule", "topic": "f/off", "on_match": "X", "enabled": False,
         "combine": "any", "conditions": [
             {"metric": "wind_speed_mph", "operator": ">", "value": "25", "for": ""}]},
    ]
    built = webui._rules_from_structured(items)
    parsed = yaml.safe_load(webui.dump_raw({"rules": built}))["rules"]

    assert parsed[0]["when"]["all"][0]["value"] == [40, 80]          # between -> list
    assert parsed[0]["when"]["all"][0]["for"] == "10m"               # for preserved
    assert parsed[0]["when"]["all"][1]["value"] == [30, 50, 70]      # in -> numeric list
    assert parsed[1]["when"]["operator"] == "changed"
    assert "value" not in parsed[1]["when"]                          # changed -> no value
    assert parsed[2]["enabled"] is False                             # disabled preserved
    assert "enabled" not in parsed[0]                                # default-on stays clean
    # the monitor accepts what the builder produced
    w.validate_config(copy.deepcopy({
        "location": {"latitude": 1, "longitude": 2}, "user_agent": "x (a@b)",
        "mqtt": {}, "rules": parsed}))

    # round-trips back to the editable shape
    s0 = webui._rule_to_structured(parsed[0])
    assert s0["conditions"][0] == {"metric": "temperature", "operator": "between",
                                   "value": "40, 80", "value_metric": "", "for": "10m"}
    assert webui._rule_to_structured(parsed[2])["enabled"] is False

    # rejections surface clearly
    for bad, needle in [
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any", "conditions": [
            {"metric": "temperature", "operator": "between", "value": "40", "for": ""}]}], "two numbers"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any", "conditions": [
            {"metric": "humidity", "operator": "in", "value": "", "for": ""}]}], "at least one"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any", "conditions": [
            {"metric": "temperature", "operator": ">", "value": "5", "for": "soon"}]}], "valid duration"),
    ]:
        try:
            webui._rules_from_structured(bad)
            raise AssertionError(f"expected rejection containing {needle!r}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"


def test_computed_pow_dos_guard():
    # `9**9**9` (all-integer) never raises OverflowError and would otherwise hang
    # the single-threaded control loop forever. It must return None ~instantly.
    import time
    t0 = time.monotonic()
    assert w.compute_metrics({"boom": {"expr": "9**9**9"}}, {}) == {"boom": None}
    assert time.monotonic() - t0 < 1.0
    assert w.compute_metrics({"p": {"expr": "2**10"}}, {})["p"] == 1024
    assert w.compute_metrics({"p": {"expr": "10**400"}}, {})["p"] is None   # overflow
    assert w.compute_metrics({"p": {"expr": "metric**2"}},
                             {"metric": 4})["p"] == 16
    # A negative base with a fractional exponent is complex, not a real number:
    # must be None so a downstream comparison can't TypeError and freeze the rule.
    assert w.compute_metrics({"p": {"expr": "(0-2)**0.5"}}, {})["p"] is None


def test_numeric_value_coerced_at_validation():
    # A quoted YAML number ("5") must be normalized to a real number so the engine
    # never does `current < "5"` (a TypeError that would freeze the rule).
    cfg = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "ON",
        "when": {"metric": "temperature", "operator": "<", "value": "5"}}]))
    cond = cfg["rules"][0]["when"]
    assert cond["value"] == 5 and not isinstance(cond["value"], str)
    assert w.evaluate_rule(cfg["rules"][0], {"temperature": 3.0}) is True
    assert w.evaluate_rule(cfg["rules"][0], {"temperature": 9.0}) is False
    # between bounds and `in` items coerce too
    cfg2 = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "ON",
        "when": {"metric": "temperature", "operator": "between",
                 "value": ["40", "80"]}}]))
    assert cfg2["rules"][0]["when"]["value"] == [40, 80]


def test_engine_state_save_load_roundtrip():
    import tempfile, os
    from datetime import datetime, timezone
    esf = tempfile.mktemp(suffix=".json")
    es = w.EngineState()
    key = "rule|is_raining|==|True|10m"
    es.cond_since[key] = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    es.prev_metrics = {"temperature": 60.0, "is_raining": True, "active_alerts": []}
    try:
        w.save_engine_state(esf, {"r": True, "skip": None},
                            {"r": "2026-06-28T11:00:00+00:00"}, es)
        ls, lc, es2 = w.load_engine_state(esf)
        assert ls == {"r": True}                                  # None dropped
        assert lc == {"r": "2026-06-28T11:00:00+00:00"}
        assert es2.prev_metrics["temperature"] == 60.0
        assert es2.cond_since[key] == datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
        # corrupt + missing files both yield a clean cold start (never raise)
        open(esf, "w").write("{ broken")
        ls3, lc3, es3 = w.load_engine_state(esf)
        assert ls3 == {} and lc3 == {} and es3.cond_since == {}
        assert w.load_engine_state(tempfile.mktemp())[0] == {}
    finally:
        for s in ("", ".tmp"):
            try: os.unlink(esf + s)
            except OSError: pass


def test_engine_state_persists_and_republishes_across_restart():
    # End-to-end: state survives a restart (no spurious re-publish when unchanged),
    # and a (re)connect's republish flag re-asserts the retained directive.
    import tempfile, os, sys, threading, json, yaml
    p = tempfile.mktemp(suffix=".yaml")
    state = tempfile.mktemp(suffix=".json"); aud = tempfile.mktemp(suffix=".log")
    logf = tempfile.mktemp(suffix=".log"); esf = tempfile.mktemp(suffix=".json")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
           "precipitation": {"lookback_hours": 24},
           "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True,
                    "availability_topic": ""},
           "state_file": state, "audit_file": aud, "log_file": logf,
           "engine_state_file": esf, "history": {"enabled": False},
           "rules": [{"name": "rainflag", "topic": "facility/rain",
                      "on_match": "ON", "on_clear": "OFF",
                      "when": {"metric": "is_raining", "operator": "==",
                               "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    orig = (w.resolve_location, w.fetch_conditions, w.make_mqtt_client, sys.argv)
    pubs = []

    class _Info: rc = 0
    class _Fake:
        republish = False
        def __init__(self):
            self.in_lock = threading.Lock()
            self.republish_event = threading.Event()
            if _Fake.republish:
                self.republish_event.set()
        def publish(self, topic, payload, **k): pubs.append((topic, payload)); return _Info()
        def is_connected(self): return True
        def connect_async(self, *a, **k): pass
        def loop_start(self): pass
        def loop_stop(self): pass
        def disconnect(self): pass
    try:
        w.resolve_location = lambda *a, **k: {"office": "x"}
        w.fetch_conditions = lambda *a, **k: {
            "temperature": 60.0, "humidity": 80.0, "wind_speed_mph": 5.0,
            "is_raining": True, "precip_accum_in": 0.3,
            "precipitation_probability": 90.0, "short_forecast": "Rain",
            "active_alerts": []}
        w.make_mqtt_client = lambda *a, **k: _Fake()
        sys.argv = ["weather_mqtt", "--config", p, "--once"]
        w.main()                                          # cold start -> ON
        assert ("facility/rain", "ON") in pubs
        assert json.loads(open(esf).read())["last_state"]["rainflag"] is True
        pubs.clear(); w.main()                            # restart, unchanged -> silent
        assert ("facility/rain", "ON") not in pubs
        pubs.clear(); _Fake.republish = True; w.main()    # reconnect -> re-assert
        assert ("facility/rain", "ON") in pubs
    finally:
        w.resolve_location, w.fetch_conditions, w.make_mqtt_client, sys.argv = orig
        for f in (p, state, aud, logf, esf):
            for s in ("", ".bak", ".tmp"):
                try: os.unlink(f + s)
                except OSError: pass


def test_reassert_retained_status_helper():
    # After a broker reconnect the last status snapshot must be re-published
    # verbatim (a broker restart drops every retained message). It is a no-op
    # when there is nothing to assert, and reports failure on a non-success rc.
    class _Info:
        def __init__(self, rc=0): self.rc = rc

    class _C:
        def __init__(self, rc=0): self.pubs = []; self._rc = rc
        def publish(self, topic, payload, qos=0, retain=False):
            self.pubs.append((topic, payload, qos, retain)); return _Info(self._rc)

    c = _C()
    payload = '{"is_raining": true, "generated_at": "2026-07-10T00:00:00+00:00"}'
    assert w.reassert_retained_status(c, "irr/weather/status", payload, 1, True) is True
    assert c.pubs == [("irr/weather/status", payload, 1, True)]   # published verbatim
    # No-ops: no client, no configured topic, or nothing cached to re-assert yet.
    assert w.reassert_retained_status(None, "t", payload, 1, True) is False
    assert w.reassert_retained_status(c, "", payload, 1, True) is False
    assert w.reassert_retained_status(c, "t", None, 1, True) is False
    assert len(c.pubs) == 1                                       # none of those published
    # Broker dropped again mid-reassert -> non-success rc -> reported as failure.
    assert w.reassert_retained_status(_C(rc=4), "t", payload, 1, True) is False


def test_status_topic_carries_freshness_stamp():
    # Every status snapshot published to status_topic must carry a generated_at
    # timestamp (plus the live weather) so a pure-MQTT consumer can detect a
    # stale/hung controller instead of trusting a frozen retained snapshot.
    import tempfile, os, sys, threading, json, yaml
    p = tempfile.mktemp(suffix=".yaml")
    state = tempfile.mktemp(suffix=".json"); aud = tempfile.mktemp(suffix=".log")
    logf = tempfile.mktemp(suffix=".log"); esf = tempfile.mktemp(suffix=".json")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "poll_interval_minutes": 15,
           "precipitation": {"lookback_hours": 24},
           "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True,
                    "availability_topic": "", "status_topic": "irr/weather/status"},
           "state_file": state, "audit_file": aud, "log_file": logf,
           "engine_state_file": esf, "history": {"enabled": False},
           "rules": [{"name": "rainflag", "topic": "facility/rain",
                      "on_match": "ON", "on_clear": "OFF",
                      "when": {"metric": "is_raining", "operator": "==",
                               "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    orig = (w.resolve_location, w.fetch_conditions, w.make_mqtt_client, sys.argv)
    pubs = []

    class _Info: rc = 0
    class _Fake:
        def __init__(self):
            self.in_lock = threading.Lock()
            self.republish_event = threading.Event()
        def publish(self, topic, payload, **k): pubs.append((topic, payload)); return _Info()
        def is_connected(self): return True
        def connect_async(self, *a, **k): pass
        def loop_start(self): pass
        def loop_stop(self): pass
        def disconnect(self): pass
    try:
        w.resolve_location = lambda *a, **k: {"office": "x"}
        w.fetch_conditions = lambda *a, **k: {
            "temperature": 60.0, "humidity": 80.0, "wind_speed_mph": 5.0,
            "is_raining": True, "precip_accum_in": 0.3,
            "precipitation_probability": 90.0, "short_forecast": "Rain",
            "active_alerts": []}
        w.make_mqtt_client = lambda *a, **k: _Fake()
        sys.argv = ["weather_mqtt", "--config", p, "--once"]
        w.main()
        status = [pl for tp, pl in pubs if tp == "irr/weather/status"]
        assert status, "status snapshot must be published to status_topic"
        obj = json.loads(status[-1])
        assert obj.get("generated_at"), "status snapshot must carry generated_at"
        assert obj["is_raining"] is True and obj["precip_accum_in"] == 0.3
    finally:
        w.resolve_location, w.fetch_conditions, w.make_mqtt_client, sys.argv = orig
        for f in (p, state, aud, logf, esf):
            for s in ("", ".bak", ".tmp"):
                try: os.unlink(f + s)
                except OSError: pass


def test_webui_request_size_cap_configured():
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    # A request-body cap must be set so an oversized POST can't OOM the dashboard.
    assert webui.app.config.get("MAX_CONTENT_LENGTH") == 1024 * 1024
    # save_config routes through the core atomic+fsync writer.
    assert hasattr(webui.core, "_atomic_write")


def test_engine_state_resets_sustain_timers_after_long_gap():
    import tempfile, os, json
    from datetime import datetime, timezone, timedelta
    from pathlib import Path
    esf = tempfile.mktemp(suffix=".json")
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)
    key = "rule|is_raining|==|True|10m"
    base = {"last_state": {"r": True},
            "last_change": {"r": "2026-06-28T11:00:00+00:00"},
            "cond_since": {key: "2026-06-28T10:00:00+00:00"}, "prev_metrics": {}}
    try:
        # Saved an hour ago: beyond the grace window -> `for:` sustain timers
        # re-accrue (can't prove continuity across the gap), but last_state /
        # last_change ARE restored (those are genuine wall-clock).
        Path(esf).write_text(json.dumps(
            {**base, "saved_at": (now - timedelta(hours=1)).isoformat()}))
        ls, lc, es = w.load_engine_state(esf, now=now)
        assert es.cond_since == {}
        assert ls == {"r": True} and lc == {"r": "2026-06-28T11:00:00+00:00"}
        # Saved 30s ago: a quick restart keeps the sustain timers.
        Path(esf).write_text(json.dumps(
            {**base, "saved_at": (now - timedelta(seconds=30)).isoformat()}))
        _, _, es2 = w.load_engine_state(esf, now=now)
        assert key in es2.cond_since
        # Legacy file with no saved_at: conservatively don't trust sustain timers.
        Path(esf).write_text(json.dumps(base))
        _, _, es3 = w.load_engine_state(esf, now=now)
        assert es3.cond_since == {}
    finally:
        for s in ("", ".tmp"):
            try: os.unlink(esf + s)
            except OSError: pass


def test_coerce_payload_bool_holds_on_unknown():
    assert w.coerce_payload(b"on", "bool") is True
    assert w.coerce_payload(b"off", "bool") is False
    # An unrecognized payload holds (None) rather than fabricating a real "off".
    assert w.coerce_payload(b"maybe", "bool") is None
    store, tmap = {}, {"t": {"topic": "t", "metric": "m", "parse": "bool"}}
    assert w.handle_mqtt_input(store, tmap, "t", b"maybe") is False
    assert store == {}


class _Resp:
    def __init__(self, status, body=None, retry_after=None, bad_json=False):
        self.status_code = status
        self._body = {"ok": True} if body is None else body
        self._bad = bad_json
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after
    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


class _Sess:
    def __init__(self, seq): self.seq = list(seq); self.calls = 0
    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        r = self.seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_nws_get_retries_failfast_and_retry_after():
    real = w._SESSION
    try:
        # 503 (Retry-After: 0 -> no real sleep) then 200 -> returns after one retry
        s = _Sess([_Resp(503, retry_after="0"), _Resp(200, {"v": 1})])
        w._SESSION = s
        assert w.nws_get("http://x", "ua", retries=3) == {"v": 1}
        assert s.calls == 2
        # 404 is non-retryable: fail fast, exactly one call
        s2 = _Sess([_Resp(404)])
        w._SESSION = s2
        try:
            w.nws_get("http://x", "ua", retries=3); assert False, "should raise"
        except RuntimeError:
            pass
        assert s2.calls == 1
        # 200 but non-JSON body -> RuntimeError, no retry
        s3 = _Sess([_Resp(200, bad_json=True)])
        w._SESSION = s3
        try:
            w.nws_get("http://x", "ua", retries=3); assert False, "should raise"
        except RuntimeError:
            pass
        assert s3.calls == 1
    finally:
        w._SESSION = real


def test_retry_after_seconds_parsing():
    class _R:
        def __init__(self, h): self.headers = h
    assert w._retry_after_seconds(_R({"Retry-After": "5"}), 2) == 5
    assert w._retry_after_seconds(_R({}), 2) == 2
    assert w._retry_after_seconds(_R({"Retry-After": "99999"}), 2) == 300   # capped
    # HTTP-date form is unparsed here -> fall back to our own backoff default
    assert w._retry_after_seconds(
        _R({"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}), 2) == 2


def test_location_cache_validation_reresolves_on_partial():
    import tempfile, os, json
    from pathlib import Path
    real_cache, real_get = w.CACHE_FILE, w.nws_get
    cf = Path(tempfile.mktemp(suffix=".json"))
    calls = {"n": 0}

    def fake_get(url, ua, **k):
        calls["n"] += 1
        if "/points/" in url:
            return {"properties": {"forecastHourly": "https://f",
                                   "observationStations": "https://s", "gridId": "X"}}
        return {"features": [{"properties": {"stationIdentifier": "KXYZ"}}]}
    try:
        w.CACHE_FILE = cf
        w.nws_get = fake_get
        # A partial cache (matches location but missing the URLs) must NOT be
        # trusted -- re-resolve instead of feeding dead URLs to every cycle.
        cf.write_text(json.dumps({"lat": 1, "lon": 2, "station_override": None}))
        info = w.resolve_location(1, 2, "ua")
        assert info["station_id"] == "KXYZ" and calls["n"] >= 1
        # A complete cache (written atomically by resolve_location) is trusted.
        calls["n"] = 0
        info2 = w.resolve_location(1, 2, "ua")
        assert calls["n"] == 0 and info2["forecast_hourly"] == "https://f"
    finally:
        w.CACHE_FILE, w.nws_get = real_cache, real_get
        for s in ("", ".tmp"):
            try: os.unlink(str(cf) + s)
            except OSError: pass


def test_coerce_value_bool_holds_on_unknown():
    assert w.coerce_value("off", "bool") is False
    assert w.coerce_value("on", "bool") is True
    assert w.coerce_value(0, "bool") is False
    # an unexpected present value must hold (None), not fabricate a real "off"
    assert w.coerce_value("weird", "bool") is None
    assert w.coerce_value({"x": 1}, "bool") is None


def test_atomic_write_roundtrip():
    import tempfile, os
    from pathlib import Path
    p = Path(tempfile.mktemp(suffix=".txt"))
    try:
        w._atomic_write(p, "hello", fsync=True)
        assert p.read_text() == "hello"
        assert not Path(str(p) + ".tmp").exists()
        w._atomic_write(p, "world", fsync=False)       # overwrite
        assert p.read_text() == "world"
    finally:
        for s in ("", ".tmp"):
            try: os.unlink(str(p) + s)
            except OSError: pass


def test_history_uses_wal_mode():
    import tempfile, os
    db = tempfile.mktemp(suffix=".db")
    try:
        w.record_history(db, {"temperature": 60.0})
        con = w._history_connect(db)
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        con.close()
        assert mode.lower() == "wal"
    finally:
        for s in ("", "-wal", "-shm"):
            try: os.unlink(db + s)
            except OSError: pass


class _FakeInfo:
    def __init__(self, rc=0): self.rc = rc
    def wait_for_publish(self, timeout=None): return True


class _FakePahoClient:
    """Records will/tls/subscribe/publish so make_mqtt_client wiring is testable
    without a broker."""
    def __init__(self, **kwargs):
        self.subs, self.pubs = [], []
        self.will = None
        self.tls = None
        self.tls_insecure = None
        self.on_connect = self.on_disconnect = self.on_message = None
    def username_pw_set(self, *a, **k): pass
    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)
    def tls_set(self, **k): self.tls = k
    def tls_insecure_set(self, v): self.tls_insecure = v
    def subscribe(self, t): self.subs.append(t)
    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, payload, qos, retain)); return _FakeInfo()
    def reconnect_delay_set(self, **k): pass
    def connect_async(self, *a, **k): pass
    def loop_start(self): pass


class _FakeMqttModule:
    Client = _FakePahoClient
    MQTT_ERR_SUCCESS = 0
    class CallbackAPIVersion:
        VERSION2 = 2


def _with_fake_mqtt(fn):
    real = w.mqtt
    w.mqtt = _FakeMqttModule
    try:
        return fn()
    finally:
        w.mqtt = real


def test_mqtt_last_will_birth_and_republish():
    # An availability topic must arm an LWT ("offline", retained) and, on a
    # successful connect, publish the "online" birth message, (re)subscribe, set
    # the republish flag and wake the loop.
    def body():
        woke = []
        mq = {"client_id": "c", "host": "h", "port": 1883,
              "availability_topic": "av/status"}
        client = w.make_mqtt_client(
            mq, [{"topic": "s/tank", "metric": "tank", "parse": "number"}],
            {}, on_input=lambda: None, on_reconnect=lambda: woke.append(1))
        assert client.will == ("av/status", "offline", 1, True)
        assert hasattr(client, "in_lock")          # cross-thread lock present
        assert not client.republish_event.is_set()

        class _RC: is_failure = False
        client.on_connect(client, None, None, _RC(), None)
        assert ("av/status", "online", 1, True) in client.pubs   # birth message
        assert "s/tank" in client.subs                           # resubscribed
        assert client.republish_event.is_set()                   # re-assert asked
        assert woke == [1]                                        # loop woken
    _with_fake_mqtt(body)


def test_mqtt_no_availability_no_will():
    # With availability disabled (""), no LWT is armed and no birth/offline noise.
    def body():
        mq = {"client_id": "c", "host": "h", "port": 1883, "availability_topic": ""}
        client = w.make_mqtt_client(mq, [], {})
        assert client.will is None

        class _RC: is_failure = False
        client.on_connect(client, None, None, _RC(), None)
        assert client.pubs == []
    _with_fake_mqtt(body)


def test_mqtt_tls_applied_when_enabled():
    def body():
        mq = {"client_id": "c", "host": "h", "port": 8883,
              "availability_topic": "",
              "tls": {"enabled": True, "ca_certs": "/x/ca.pem", "insecure": True}}
        client = w.make_mqtt_client(mq, [], {})
        assert client.tls is not None                 # tls_set was called
        assert client.tls.get("ca_certs") == "/x/ca.pem"
        assert client.tls_insecure is True
        # ...and disabled/absent TLS leaves the socket plaintext.
        plain = w.make_mqtt_client({"client_id": "c2", "host": "h", "port": 1883,
                                    "availability_topic": ""}, [], {})
        assert plain.tls is None
    _with_fake_mqtt(body)


def test_dynamic_string_metric_rules_evaluate():
    # A rule on an mqtt_in `parse: string` metric must actually evaluate with
    # the text operators validation accepts for it (contains/equals/in/regex).
    cfg = w.validate_config(_min_cfg(
        mqtt_inputs=[{"topic": "t/door", "metric": "door_state", "parse": "string"}],
        rules=[{"name": "door",
                "when": {"metric": "door_state", "operator": "contains",
                         "value": "open"},
                "topic": "alarm/door", "on_match": "1", "on_clear": "0"}]))
    specs = w.metric_catalogue(cfg)
    rule = cfg["rules"][0]
    assert w.evaluate_rule(rule, {"door_state": "OPEN wide"}, specs=specs) is True
    assert w.evaluate_rule(rule, {"door_state": "closed"}, specs=specs) is False
    # No reading yet -> unavailable -> hold last state (None), never a
    # fabricated False.
    assert w.evaluate_rule(rule, {}, specs=specs) is None
    # The other text operators work on dynamic strings too.
    for op, val, text, expect in [
        ("equals", "open", "Open", True),
        ("equals", "open", "ajar", False),
        ("in", ["open", "ajar"], "AJAR", True),
        ("regex", "^op", "opening", True),
        ("regex", "^op", "closed", False),
    ]:
        r = {"name": "x", "when": {"metric": "door_state", "operator": op,
                                   "value": val},
             "topic": "t", "on_match": "1"}
        assert w.evaluate_rule(r, {"door_state": text}, specs=specs) is expect, \
            (op, val, text)
    # Built-in text metrics are unaffected: no specs arg still works.
    r2 = {"name": "y", "when": {"metric": "short_forecast",
                                "operator": "contains", "value": "rain"},
          "topic": "t", "on_match": "1"}
    assert w.evaluate_rule(r2, {"short_forecast": "Light Rain"}) is True


def test_mqtt_config_defaults_availability_and_tls():
    cfg = w.validate_config(_min_cfg())
    assert cfg["mqtt"]["availability_topic"] == "weather-mqtt/status"
    # tls is opt-in: absent unless configured, and normalized when present.
    assert "tls" not in cfg["mqtt"] or cfg["mqtt"]["tls"]["enabled"] is False
    cfg2 = w.validate_config(_min_cfg(mqtt={"tls": {"enabled": "yes"}}))
    assert cfg2["mqtt"]["tls"]["enabled"] is True


def test_reload_config_keeps_last_good_on_invalid():
    # Documented fail-safe: a mid-run edit that breaks config.yaml must not take
    # the monitor down -- it keeps the last-good config.
    import tempfile, os
    good = _min_cfg()
    prev = w.validate_config(dict(good, poll_interval_minutes=7,
                                  precipitation={"lookback_hours": 12}))
    p = tempfile.mktemp(suffix=".yaml")
    try:
        # syntactically broken YAML -> previous returned unchanged
        open(p, "w").write("rules: [::: not yaml")
        cfg, ok = w.reload_config_or_keep(p, prev)
        assert ok is False and cfg is prev
        # schema-invalid (validate_config raises) -> previous kept
        import yaml
        open(p, "w").write(yaml.safe_dump({"version": 1, "rules": []}))
        cfg, ok = w.reload_config_or_keep(p, prev)
        assert ok is False and cfg is prev
        # a valid file -> the new config is applied
        open(p, "w").write(yaml.safe_dump(_min_cfg(poll_interval_minutes=9)))
        cfg, ok = w.reload_config_or_keep(p, prev)
        assert ok is True and cfg["poll_interval_minutes"] == 9
    finally:
        try: os.unlink(p)
        except OSError: pass


def test_read_history_time_window_filters():
    # Unambiguous offsets well clear of the 1h boundary, so the window filter is
    # actually exercised (not a vacuous <= assertion).
    import tempfile, os
    from datetime import datetime, timezone, timedelta
    db = tempfile.mktemp(suffix=".db")
    now = datetime.now(timezone.utc)
    try:
        for mins, val in [(5, 10.0), (30, 20.0), (55, 30.0), (65, 40.0), (120, 50.0)]:
            ts = (now - timedelta(minutes=mins)).isoformat(timespec="seconds")
            w.record_history(db, {"p": val}, ts=ts, retention_days=30)
        got = [pt[1] for pt in w.read_history(db, hours=1)["p"]]
        assert got == [30.0, 20.0, 10.0], got   # only the 5/30/55-min points, in ts order
    finally:
        for s in ("", "-wal", "-shm"):
            try: os.unlink(db + s)
            except OSError: pass


def test_active_alerts_none_holds_state():
    # A failed alerts fetch leaves active_alerts None -> the rule holds, rather
    # than reading "no alerts" and clearing a warning directive.
    rule = {"name": "r", "topic": "t", "on_match": "1",
            "when": {"metric": "active_alert", "operator": "any"}}
    assert w.evaluate_rule(rule, {"active_alerts": None}) is None      # hold
    assert w.evaluate_rule(rule, {"active_alerts": []}) is False       # dry read
    assert w.evaluate_rule(rule, {"active_alerts": ["Flood Warning"]}) is True


def test_webhook_and_notify_failure_isolation():
    # A webhook/notify that raises must not stop the cycle or the other actions,
    # and each firing is audited with its ok flag.
    import tempfile, os
    aud = tempfile.mktemp(suffix=".log")

    class Info:
        rc = 0
    class FakeClient:
        def __init__(self): self.pubs = []
        def publish(self, topic, payload, qos=0, retain=False):
            self.pubs.append((topic, payload)); return Info()

    real_post, real_put = w.requests.post, w.requests.put
    def boom(*a, **k): raise ConnectionError("network down")
    w.requests.post = boom
    w.requests.put = boom
    try:
        rule = {"name": "vent", "topic": "t", "on_match": "ON", "on_clear": "OFF",
                "actions": [
                    {"trigger": "match", "webhook": {"url": "https://h/x", "method": "POST"}},
                    {"trigger": "match", "notify": {"text": "hi"}},
                    {"trigger": "match", "mqtt": {"topic": "x/y", "payload": "p"}},
                ]}
        client = FakeClient()
        slack = {"enabled": True, "bot_token": "xoxb-t", "channel": "#c"}
        # Must not raise despite webhook+notify failing.
        w.fire_actions(rule, True, {"temperature": 80}, client, 1, True, slack, aud)
        assert client.pubs == [("x/y", "p")]        # the mqtt action still fired
        events = w.read_audit(aud, 50)
        kinds = {e.get("kind"): e.get("ok") for e in events if e.get("action") == "action_fired"}
        assert kinds.get("webhook") is False and kinds.get("notify") is False
        assert kinds.get("mqtt") is True
    finally:
        w.requests.post, w.requests.put = real_post, real_put
        for s in ("", ".1"):
            try: os.unlink(aud + s)
            except OSError: pass


def test_manual_override_beats_window_and_hysteresis():
    # Precedence: a manual on/off override wins over the window gate and bypasses
    # hysteresis; clearing to auto hands control back to the rule.
    from datetime import datetime, timezone
    rule = {"name": "pump", "topic": "t", "on_match": "ON", "on_clear": "OFF",
            "when": {"metric": "temperature", "operator": ">", "value": 85},
            "window": {"from": "06:00", "to": "20:00"},
            "hysteresis": {"min_on": "10m"}}
    now = datetime(2026, 6, 29, 2, 0, tzinfo=timezone.utc)   # 02:00 -> outside window
    now_local = now.astimezone()
    # Outside the window the rule's desired state is forced OFF...
    assert w.resolve_desired(rule, {"temperature": 90}, now_local) is False
    # ...but a manual override is applied ahead of the window/rule in main()'s
    # resolution order, so effective_manual decides.
    assert w.effective_manual(rule, {"pump": "on"}) == "on"
    assert w.effective_manual(rule, {"pump": "off"}) == "off"
    assert w.effective_manual(rule, {}) == "auto"
    # min_on holds a running load ON until the timer elapses, even once the rule
    # wants OFF.
    lc = datetime(2026, 6, 29, 1, 55, tzinfo=timezone.utc)   # changed 5 min ago
    assert w.apply_hysteresis({"min_on": "10m"}, True, False, lc, now) is True   # held ON
    lc2 = datetime(2026, 6, 29, 1, 40, tzinfo=timezone.utc)  # 20 min ago
    assert w.apply_hysteresis({"min_on": "10m"}, True, False, lc2, now) is False  # released


def test_as_number_rejects_nan_and_inf():
    # A sensor payload of "nan"/"inf" must read as unavailable (hold last
    # state), not poison every downstream comparison/computed metric.
    assert w._as_number("nan", None, "x") is None
    assert w._as_number("inf", None, "x") is None
    assert w._as_number("-inf", None, "x") is None
    assert w._as_number(float("nan"), None, "x") is None
    assert w.coerce_payload(b"NaN", "number") is None
    assert w._as_number("5.5", None, "x") == 5.5      # real numbers still pass


def test_bool_condition_value_normalized():
    # A quoted YAML bool (value: "true") must normalize to a real bool instead
    # of silently never matching.
    cfg = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1",
        "when": {"metric": "is_raining", "operator": "==", "value": "true"}}]))
    assert cfg["rules"][0]["when"]["value"] is True
    assert w.evaluate_rule(cfg["rules"][0], {"is_raining": True}) is True
    try:
        w.validate_config(_min_cfg(rules=[{
            "name": "r", "topic": "t", "on_match": "1",
            "when": {"metric": "is_raining", "operator": "==", "value": "maybe"}}]))
        assert False, "garbage bool value should be rejected"
    except ValueError:
        pass


def test_changed_operator_on_active_alert():
    # README documents { metric: active_alert, operator: changed }; the value
    # lives under the plural 'active_alerts' key in the metric context.
    st = w.EngineState()
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    rule = {"name": "r", "topic": "t", "on_match": "1",
            "when": {"metric": "active_alert", "operator": "changed"}}
    m1 = {"active_alerts": []}
    assert w.evaluate_rule(rule, m1, st, now) is False   # first observation
    st.observe(m1)
    m2 = {"active_alerts": ["Flood Warning"]}
    assert w.evaluate_rule(rule, m2, st, now) is True    # alert set changed
    st.observe(m2)
    assert w.evaluate_rule(rule, m2, st, now) is False   # stable again


def test_alert_equals_case_insensitive():
    rule = {"name": "r", "topic": "t", "on_match": "1",
            "when": {"metric": "active_alert", "operator": "equals",
                     "value": "flood warning"}}
    assert w.evaluate_rule(rule, {"active_alerts": ["Flood Warning"]}) is True
    assert w.evaluate_rule(rule, {"active_alerts": ["Heat Advisory"]}) is False


def test_cond_key_distinguishes_value_metric():
    a = {"metric": "m", "operator": ">", "value_metric": "x", "for": "10m"}
    b = {"metric": "m", "operator": ">", "value_metric": "y", "for": "10m"}
    assert w._cond_key("r", a) != w._cond_key("r", b)


def test_audit_log_rotates_and_reads_backup():
    import tempfile, os
    path = tempfile.mktemp(suffix=".log")
    old = w._AUDIT_MAX_BYTES
    try:
        w._AUDIT_MAX_BYTES = 200        # force rotation quickly
        for i in range(50):
            w.audit(path, device=f"d{i}", state="on", source="auto", by="t")
        assert os.path.exists(path + ".1")            # rotated
        assert os.path.getsize(path) < 1000           # current file stays small
        cur_lines = len(open(path).read().splitlines())
        events = w.read_audit(path, limit=50)
        assert len(events) > cur_lines                # backup fills the window
        assert events[0]["device"] == "d49"           # newest first, nothing lost
    finally:
        w._AUDIT_MAX_BYTES = old
        for s in ("", ".1"):
            try: os.unlink(path + s)
            except OSError: pass


def test_atomic_write_preserves_permissions():
    import tempfile, os, stat
    p = tempfile.mktemp(suffix=".yaml")
    try:
        w._atomic_write(p, "a: 1\n")
        os.chmod(p, 0o600)                     # installer locks config to 0600
        w._atomic_write(p, "a: 2\n")           # a web-UI save must not widen it
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    finally:
        for s in ("", ".tmp"):
            try: os.unlink(p + s)
            except OSError: pass


def test_webui_config_save_keeps_0600(_tmp=None):
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, stat, yaml
    p = tempfile.mktemp(suffix=".yaml")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "mqtt": {"host": "localhost", "port": 1883},
           "web": {"enabled": True},
           "rules": [{"name": "r", "topic": "t", "on_match": "ON",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    os.chmod(p, 0o600)
    webui.CONFIG_PATH = p
    try:
        webui.save_config(webui.load_raw())
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        # the backup copy must not be world-readable either
        assert stat.S_IMODE(os.stat(p + ".bak").st_mode) == 0o600
    finally:
        for s in ("", ".bak", ".tmp"):
            try: os.unlink(p + s)
            except OSError: pass


def test_webui_settings_clearing_username_clears_password(_tmp=None):
    # Clearing the username must also drop the stored password, or _auth_ok
    # would deny every request (username='' + password set) and lock the UI.
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, base64, yaml
    p = tempfile.mktemp(suffix=".yaml")
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "mqtt": {"host": "localhost", "port": 1883},
           "web": {"enabled": True, "username": "admin", "password": "secret"},
           "rules": [{"name": "r", "topic": "t", "on_match": "ON",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode(),
           "Origin": "http://localhost"}
    try:
        form = {"latitude": "41", "longitude": "-74", "user_agent": "x (a@b.com)",
                "poll_interval_minutes": "15", "lookback_hours": "24",
                "mqtt_host": "localhost", "mqtt_port": "1883", "mqtt_qos": "1",
                "mqtt_retain": "true", "web_host": "127.0.0.1", "web_port": "8080",
                "web_username": "", "web_password": ""}
        r = c.post("/settings", data=form, headers=hdr)
        assert b"Settings saved" in r.data
        saved = yaml.safe_load(open(p))
        assert saved["web"]["username"] == "" and saved["web"]["password"] == ""
        # auth is now disabled, so the UI is reachable with no credentials
        assert c.get("/api/system").status_code == 200
    finally:
        for s in ("", ".bak", ".tmp"):
            try: os.unlink(p + s)
            except OSError: pass


def test_webui_inputs_reject_duplicate_names(_tmp=None):
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    cfg = {"version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
           "user_agent": "x (a@b.com)", "mqtt": {},
           "rules": [{"name": "r", "topic": "t", "on_match": "ON",
                      "when": {"metric": "is_raining", "operator": "==", "value": True}}]}
    for payload, needle in [
        ({"variables": [{"name": "dup", "type": "bool", "default": "false"},
                        {"name": "dup", "type": "number", "default": "1"}]}, "duplicate variable"),
        ({"computed": [{"name": "c", "expr": "temperature"},
                       {"name": "c", "expr": "humidity"}]}, "duplicate computed"),
    ]:
        try:
            webui._apply_sources(dict(cfg), payload)
            assert False, "duplicate name should be rejected"
        except ValueError as e:
            assert needle in str(e), str(e)


def test_rule_is_flat_routes_advanced_rules_to_yaml(_tmp=None):
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    flat = {"name": "a", "topic": "t", "on_match": "1",
            "when": {"metric": "is_raining", "operator": "==", "value": True}}
    assert webui._rule_is_flat(flat) is True
    # a declared manual state can't be round-tripped by the builder
    assert webui._rule_is_flat(dict(flat, manual="on")) is False
    # webhook headers and explicit retain: false likewise
    assert webui._rule_is_flat(dict(flat, actions=[
        {"trigger": "match", "webhook": {"url": "u", "headers": {"X": "1"}}}])) is False
    assert webui._rule_is_flat(dict(flat, actions=[
        {"trigger": "match", "mqtt": {"topic": "x", "retain": False}}])) is False
    # a plain retain: true action is still flat
    assert webui._rule_is_flat(dict(flat, actions=[
        {"trigger": "match", "mqtt": {"topic": "x", "retain": True}}])) is True


def test_webui_cross_origin_posts_rejected():
    # Browsers attach Basic-auth credentials automatically, so a cross-site
    # POST must be refused even when it authenticates (CSRF defense).
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml, base64

    p = tempfile.mktemp(suffix=".yaml")
    ovr = tempfile.mktemp(suffix=".json")
    aud = tempfile.mktemp(suffix=".log")
    cfg = {
        "version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)",
        "mqtt": {"host": "localhost", "port": 1883},
        "web": {"enabled": True, "username": "admin", "password": "pw",
                "allow_manual_control": True},
        "overrides_file": ovr, "audit_file": aud,
        "rules": [{"name": "pump", "topic": "t", "on_match": "ON",
                   "when": {"metric": "is_raining", "operator": "==", "value": True}}],
    }
    open(p, "w").write(yaml.safe_dump(cfg))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    body = {"device": "pump", "state": "on"}
    try:
        # cross-site Origin -> rejected before any handler runs
        r = c.post("/api/control", json=body,
                   headers={**hdr, "Origin": "http://evil.example"})
        assert r.status_code == 403 and w.load_overrides(ovr) == {}
        # a sandboxed "null" Origin is cross-site too
        assert c.post("/api/control", json=body,
                      headers={**hdr, "Origin": "null"}).status_code == 403
        # same-origin fetch (browser sends our own host) -> allowed
        r = c.post("/api/control", json=body,
                   headers={**hdr, "Origin": "http://localhost"})
        assert r.status_code == 200 and w.load_overrides(ovr) == {"pump": "on"}
        # no Origin at all (curl / scripts) -> allowed
        assert c.post("/api/control", json={"device": "pump", "state": "auto"},
                      headers=hdr).status_code == 200
        # GETs are never blocked by the guard
        assert c.get("/api/state",
                     headers={**hdr, "Origin": "http://evil.example"}).status_code in (200, 503)
    finally:
        for f in (p, ovr, aud):
            for s in ("", ".tmp", ".bak"):
                try: os.unlink(f + s)
                except OSError: pass


def test_webui_auth_handles_non_ascii_credentials():
    # hmac.compare_digest raises on non-ASCII str; the login must compare bytes
    # so a non-ASCII password works and a wrong guess gets 401, not a 500.
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    import tempfile, os, yaml, base64

    p = tempfile.mktemp(suffix=".yaml")
    cfg = {
        "version": 1, "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)", "mqtt": {},
        "web": {"enabled": True, "username": "admin", "password": "pässwörd"},
        "rules": [{"name": "r", "topic": "t", "on_match": "ON",
                   "when": {"metric": "is_raining", "operator": "==", "value": True}}],
    }
    open(p, "w").write(yaml.safe_dump(cfg, allow_unicode=True))
    webui.CONFIG_PATH = p
    webui.app.config["TESTING"] = True
    c = webui.app.test_client()
    try:
        good = {"Authorization": "Basic " +
                base64.b64encode("admin:pässwörd".encode()).decode()}
        assert c.get("/api/system", headers=good).status_code == 200
        bad = {"Authorization": "Basic " +
               base64.b64encode("admin:wröng".encode()).decode()}
        assert c.get("/api/system", headers=bad).status_code == 401
    finally:
        try: os.unlink(p)
        except OSError: pass


def test_webui_console_applies_broker_tls():
    # The web UI's MQTT console must honor mqtt.tls like the monitor does,
    # or it can never connect to a TLS-only broker.
    try:
        import webui
    except Exception as e:
        raise _skip_if_optional(e)
    real = webui.mqtt
    webui.mqtt = _FakeMqttModule
    try:
        console = webui.MqttConsole()
        console.start({"web": {"mqtt_console_enabled": True},
                       "mqtt": {"host": "h", "port": 8883, "client_id": "c",
                                "tls": {"enabled": True, "ca_certs": "/x/ca.pem"}}})
        assert console._client is not None
        assert console._client.tls is not None
        assert console._client.tls.get("ca_certs") == "/x/ca.pem"
    finally:
        webui.mqtt = real


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = skipped = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except _Skip as e:
            skipped += 1
            print(f"  SKIP  {t.__name__} ({e})")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    summary = f"\n{len(tests) - failed - skipped}/{len(tests)} passed"
    if skipped:
        summary += f", {skipped} skipped"
    if failed:
        summary += f", {failed} FAILED"
    print(summary)
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run() else 0)
