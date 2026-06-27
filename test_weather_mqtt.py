#!/usr/bin/env python3
"""Offline unit tests for weather_mqtt -- no network required.

Run:  python test_weather_mqtt.py
"""
from datetime import datetime, timezone

import weather_mqtt as w


def _obs(ts, value_mm, unit="wmoUnit:mm"):
    return {"properties": {"timestamp": ts,
                           "precipitationLastHour": {"value": value_mm,
                                                     "unitCode": unit}}}


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


def test_accumulation_none_when_no_data():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    data = {"features": [
        {"properties": {"timestamp": "2026-06-27T11:53:00+00:00",
                        "precipitationLastHour": {"value": None}}},
    ]}
    assert w._accumulate_precip(data, 24, now) is None


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


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run() else 0)
