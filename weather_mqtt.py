#!/usr/bin/env python3
"""
weather_mqtt.py -- Monitor precipitation from the National Weather Service
(api.weather.gov) and publish MQTT messages so irrigation PLCs know when NOT
to water.

Primary job:
  - Pull measured rainfall over a rolling window (default 24h) and whether it
    is precipitating right now from the nearest NWS observation station.
  - Evaluate rules from config.yaml. The default rule says "if it is raining
    OR it has rained >= X inches in the last 24h, tell the PLCs to inhibit
    watering" by publishing a retained MQTT message.
  - Publish only when a rule's state changes (so the bus isn't spammed),
    with retain=True so a PLC that connects later immediately gets the
    current directive.

No API key is required. The NWS API is free and US-only.

Run:   python weather_mqtt.py --config config.yaml
Test:  python weather_mqtt.py --config config.yaml --once --dry-run --verbose
"""

import argparse
import ast
import contextlib
import json
import logging
import logging.handlers
import math
import os
import re
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
import paho.mqtt.client as mqtt

LOG = logging.getLogger("weather_mqtt")
NWS_API = "https://api.weather.gov"
CACHE_FILE = Path("nws_location_cache.json")

# Set by the signal handler so blocking HTTP backoff sleeps bail out promptly on
# shutdown instead of making SIGTERM wait out a full retry cycle.
_SHUTDOWN = threading.Event()

# One pooled HTTP session for the NWS calls (keep-alive across the ~4-6 requests
# per cycle to the same host, instead of a fresh TCP+TLS handshake each time).
_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
    return _SESSION


def _atomic_write(path, text, fsync=True):
    """Write `text` to `path` atomically: temp file + os.replace so a reader never
    sees a partial file. With fsync (the default for durable state) the data is
    flushed to disk before the rename, so a power loss can't leave a zero-length
    or stale file behind the successful rename. Raises on failure.

    Preserves the destination's existing permissions across the replace. Without
    this, os.replace would give `path` the temp file's umask-default mode
    (typically 0644) -- silently widening a config.yaml the installer locked to
    0600, exposing stored passwords/tokens to any local user."""
    path = Path(path)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        if fsync:
            os.fsync(f.fileno())
    try:
        mode = os.stat(path).st_mode  # existing file: keep its permissions
    except OSError:
        mode = None
    if mode is not None:
        try:
            os.chmod(tmp, mode & 0o7777)
        except OSError:
            pass
    tmp.replace(path)

# Words in NWS present-weather / textDescription that mean "it's precipitating".
# Note "freezing" is intentionally NOT here: alone it matches "Freezing Fog"
# (not precipitation). "Freezing Rain"/"Freezing Drizzle" still match via
# "rain"/"drizzle".
PRECIP_WORDS = (
    "rain", "drizzle", "shower", "thunderstorm", "sleet",
    "snow", "wintry", "ice pellets", "hail",
)
# Phrases that mean the precip is NOT falling at the station, so they must not
# trip is_raining (which would wrongly hold irrigation closed).
NOT_HERE_WORDS = ("vicinity", "in the area")

# Canonical metric catalogue: value type + the operators each accepts. This is
# the single source of truth shared by config validation here and the web UI's
# rule builder (which imports it), so the two can never drift apart.
NUMERIC_COMPARE = ("<", "<=", ">", ">=", "==", "!=")
# Set-style operators (ROADMAP Phase 1 engine): `between` takes an inclusive
# [low, high] pair; `in` takes a list of allowed values.
SET_OPS = ("between", "in")
NUMBER_OPS = NUMERIC_COMPARE + SET_OPS
TEXT_OPS = ("contains", "equals", "in", "regex")
METRIC_SPECS = {
    "is_raining":                {"type": "bool",   "ops": ("==", "!=")},
    "precip_accum_in":           {"type": "number", "ops": NUMBER_OPS},
    "precipitation_probability": {"type": "number", "ops": NUMBER_OPS},
    "temperature":               {"type": "number", "ops": NUMBER_OPS},
    "wind_speed_mph":            {"type": "number", "ops": NUMBER_OPS},
    "humidity":                  {"type": "number", "ops": NUMBER_OPS},
    "short_forecast":            {"type": "text",   "ops": TEXT_OPS},
    "active_alert":              {"type": "alert",  "ops": ("any", "contains", "equals", "regex")},
    # --- schedule / clock inputs (ROADMAP Phase 3, no external calls) ---
    "time_hour":                 {"type": "number", "ops": NUMBER_OPS},   # 0..23 local
    "time_minute":               {"type": "number", "ops": NUMBER_OPS},   # 0..59
    "time_weekday":              {"type": "text",   "ops": TEXT_OPS},     # mon..sun
    "time_is_weekend":           {"type": "bool",   "ops": ("==", "!=")},
    "time_is_daytime":           {"type": "bool",   "ops": ("==", "!=")},  # sun up at site
}


MAX_WHEN_DEPTH = 25   # guard against pathological deeply-nested configs


def _validate_condition(cond, rule_name, specs=METRIC_SPECS):
    """Validate one rule condition's metric/operator/value. Raises ValueError.
    `specs` is the active metric catalogue (built-ins plus declared variables)."""
    if not isinstance(cond, dict) or "metric" not in cond:
        raise ValueError(f"rule '{rule_name}': each condition needs a 'metric'")
    metric = cond["metric"]
    spec = specs.get(metric)
    if spec is None:
        raise ValueError(f"rule '{rule_name}': unknown metric '{metric}' "
                         f"(valid: {', '.join(sorted(specs))})")
    op = cond.get("operator")
    # `for:` is an optional sustain modifier on any condition (must be a duration).
    if cond.get("for") is not None and parse_duration(cond["for"], None) is None:
        raise ValueError(f"rule '{rule_name}': '{metric}' for: {cond['for']!r} must "
                         "be a duration like '10m', '30s', '2h', or minutes")
    # `changed` works on any metric and needs no value.
    if op == "changed":
        return
    if metric == "active_alert" and op in (None, "any"):
        return  # the "any active alert" form needs no value
    if op not in spec["ops"]:
        raise ValueError(f"rule '{rule_name}': operator '{op}' is not valid for "
                         f"metric '{metric}' (valid: {', '.join(spec['ops'])})")
    # Compare against another metric's live value instead of a constant.
    if cond.get("value_metric"):
        other = cond["value_metric"]
        if op not in NUMERIC_COMPARE:
            raise ValueError(f"rule '{rule_name}': value_metric only works with "
                             f"{', '.join(NUMERIC_COMPARE)} (not '{op}')")
        if spec["type"] not in ("number", "bool"):
            raise ValueError(f"rule '{rule_name}': value_metric needs a number/bool "
                             f"metric on the left (not '{metric}')")
        ospec = specs.get(other)
        if ospec is None:
            raise ValueError(f"rule '{rule_name}': value_metric '{other}' is not a "
                             f"known metric (valid: {', '.join(sorted(specs))})")
        if ospec["type"] not in ("number", "bool"):
            raise ValueError(f"rule '{rule_name}': value_metric '{other}' must be a "
                             "number/bool metric")
        return
    # `regex` (text/alert): the value must be a compilable pattern.
    if op == "regex":
        if "value" not in cond or cond["value"] is None:
            raise ValueError(f"rule '{rule_name}': '{metric}' regex needs a pattern value")
        try:
            re.compile(str(cond["value"]))
        except re.error as e:
            raise ValueError(f"rule '{rule_name}': '{metric}' regex {cond['value']!r} "
                             f"is invalid ({e})")
        return
    if "value" not in cond or cond["value"] is None:
        raise ValueError(f"rule '{rule_name}': condition on '{metric}' needs a value")
    value = cond["value"]
    if op == "between":
        _validate_between_value(value, metric, rule_name)
        # Store coerced numbers so a quoted YAML bound (e.g. "40") never reaches
        # the comparison as a string (which would TypeError and freeze the rule).
        cond["value"] = [_as_number(value[0], None, "between low"),
                         _as_number(value[1], None, "between high")]
    elif op == "in":
        _validate_in_value(value, spec["type"], metric, rule_name)
        if spec["type"] == "number":
            cond["value"] = [_as_number(v, None, "in item") for v in value]
    elif spec["type"] == "number":
        num = _as_number(value, None, f"{metric} value")
        if num is None:
            raise ValueError(f"rule '{rule_name}': '{metric}' value "
                             f"{value!r} must be a number")
        # Normalize to a real number; the engine then never compares against a
        # string (e.g. value: "5" in YAML), which would raise and hold the rule.
        cond["value"] = num
    elif spec["type"] == "bool" and not isinstance(value, bool):
        # Normalize a quoted YAML bool (value: "true") to a real bool; the
        # string would otherwise compare unequal to True/False forever without
        # any error -- a rule that silently never fires.
        low = str(value).strip().lower()
        if low in ("true", "1", "yes", "on"):
            cond["value"] = True
        elif low in ("false", "0", "no", "off"):
            cond["value"] = False
        else:
            raise ValueError(f"rule '{rule_name}': '{metric}' value {value!r} "
                             "must be true or false")


def _validate_between_value(value, metric, rule_name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"rule '{rule_name}': '{metric}' between needs a "
                         "[low, high] pair")
    lo = _as_number(value[0], None, "between low")
    hi = _as_number(value[1], None, "between high")
    if lo is None or hi is None:
        raise ValueError(f"rule '{rule_name}': '{metric}' between bounds must be numbers")
    if lo > hi:
        raise ValueError(f"rule '{rule_name}': '{metric}' between low must be <= high")


def _validate_in_value(value, vtype, metric, rule_name):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"rule '{rule_name}': '{metric}' in needs a non-empty list")
    if vtype == "number":
        for v in value:
            if _as_number(v, None, "in item") is None:
                raise ValueError(f"rule '{rule_name}': '{metric}' in list must be "
                                 "all numbers")


def _validate_rule_when(when, rule_name, specs=METRIC_SPECS, _depth=0):
    """Validate a rule's `when`: a single condition, or a nested group built from
    `any` (OR), `all` (AND), and `not` (negation), to arbitrary depth."""
    if _depth > MAX_WHEN_DEPTH:
        raise ValueError(f"rule '{rule_name}': condition nesting is too deep "
                         f"(max {MAX_WHEN_DEPTH})")
    if isinstance(when, dict) and ("any" in when or "all" in when):
        if len(when) != 1:
            raise ValueError(f"rule '{rule_name}': an any/all group must have exactly "
                             "one of 'any' or 'all' as its only key")
        mode = "any" if "any" in when else "all"
        group = when[mode]
        if not isinstance(group, list) or not group:
            raise ValueError(f"rule '{rule_name}': '{mode}' must be a non-empty list")
        for c in group:
            _validate_rule_when(c, rule_name, specs, _depth + 1)
    elif isinstance(when, dict) and "not" in when:
        if len(when) != 1:
            raise ValueError(f"rule '{rule_name}': 'not' must be the only key in its group")
        _validate_rule_when(when["not"], rule_name, specs, _depth + 1)
    else:
        _validate_condition(when, rule_name, specs)


# ---------------------------------------------------------------------------
# Manual variables (operator-set inputs) -> dynamic metrics (ROADMAP Phase 3)
# ---------------------------------------------------------------------------
# A `variables:` section declares operator-set flags/setpoints. Each becomes a
# metric named `var_<name>` that rules can reference, toggled from the dashboard.
VAR_PREFIX = "var_"
VARIABLE_TYPES = ("bool", "number")


def variable_specs(variables):
    """Build metric specs for declared variables: {var_<name>: {type, ops}}."""
    out = {}
    for name, spec in (variables or {}).items():
        vtype = (spec or {}).get("type", "bool")
        if vtype == "number":
            ops = NUMBER_OPS + ("changed",)
        else:
            ops = ("==", "!=", "changed")
        out[VAR_PREFIX + str(name)] = {"type": vtype, "ops": ops}
    return out


def metric_catalogue(cfg):
    """The full metric catalogue: built-ins + variables + mqtt_in + http_poll +
    computed (derived) metrics."""
    return {**METRIC_SPECS,
            **variable_specs(cfg.get("variables", {})),
            **mqtt_input_specs(cfg.get("mqtt_inputs", [])),
            **http_input_specs(cfg.get("http_inputs", [])),
            **computed_specs(cfg.get("computed", {}))}


# ---------------------------------------------------------------------------
# Computed (derived) metrics: a tiny, safe arithmetic over other metrics.
# A `computed:` section maps a new metric name to {expr: "<arithmetic>"} using
# other metric names, numbers, + - * / // % ** and parentheses. Each becomes a
# number-typed metric usable in rules and discovered by the builder. Evaluation
# is fail-safe: a missing input (or a divide-by-zero) yields None, so dependent
# rules hold their last state exactly like any other unavailable metric.
# ---------------------------------------------------------------------------
_EXPR_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_EXPR_UNARY = {ast.UAdd: lambda a: +a, ast.USub: lambda a: -a}


def compile_expr(expr):
    """Parse an arithmetic expression into an AST, rejecting anything unsafe
    (calls, attributes, names that aren't bare identifiers, etc). Raises
    ValueError on a syntax/safety problem; returns (tree, referenced_names)."""
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"could not parse expression {expr!r}: {e}")
    names = set()

    def check(node):
        if isinstance(node, ast.Expression):
            return check(node.body)
        if isinstance(node, ast.BinOp) and type(node.op) in _EXPR_BINOPS:
            check(node.left); check(node.right); return
        if isinstance(node, ast.UnaryOp) and type(node.op) in _EXPR_UNARY:
            check(node.operand); return
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return
        if isinstance(node, ast.Name):
            names.add(node.id); return
        raise ValueError(f"expression {expr!r} uses an unsupported element "
                         f"({type(node).__name__}); only numbers, metric names and "
                         "+ - * / // % ** ( ) are allowed")
    check(tree)
    return tree, names


def _eval_expr(node, metrics):
    """Evaluate a compiled expression against `metrics`. Returns a number, or
    None if any referenced metric is missing/None or a math error occurs."""
    if isinstance(node, ast.Expression):
        return _eval_expr(node.body, metrics)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        v = metrics.get(node.id)
        if isinstance(v, bool):
            return int(v)
        return v if isinstance(v, (int, float)) else None
    if isinstance(node, ast.UnaryOp):
        a = _eval_expr(node.operand, metrics)
        return None if a is None else _EXPR_UNARY[type(node.op)](a)
    if isinstance(node, ast.BinOp):
        a = _eval_expr(node.left, metrics)
        b = _eval_expr(node.right, metrics)
        if a is None or b is None:
            return None
        try:
            if isinstance(node.op, ast.Pow):
                # Python ints are arbitrary-precision, so `9**9**9` never raises
                # OverflowError -- it just computes a galaxy-sized integer and
                # hangs the (single-threaded) control loop. Bound the operands and
                # evaluate in float so a runaway exponent overflows cleanly to
                # None instead of wedging the monitor.
                if abs(a) > 1e9 or abs(b) > 308:
                    return None
                r = float(a) ** float(b)
                # A negative base with a fractional exponent yields a complex
                # number; downstream numeric comparisons would TypeError and
                # freeze the rule, so treat it as unavailable.
                return r if isinstance(r, float) else None
            return _EXPR_BINOPS[type(node.op)](a, b)
        except (ZeroDivisionError, ValueError, OverflowError):
            return None
    return None


def computed_specs(computed):
    """Metric specs for computed metrics: each is a number with the full numeric
    operator set (so rules can compare/derive freely)."""
    out = {}
    for name in (computed or {}):
        out[str(name)] = {"type": "number", "ops": NUMBER_OPS + ("changed",)}
    return out


def _validate_computed(computed, taken_names):
    """Validate/normalize the `computed:` section. References must resolve to a
    metric already known at that point (built-ins/vars/mqtt/http or an *earlier*
    computed metric), which makes reference cycles structurally impossible.
    Returns an ordered dict {name: {expr}}."""
    if not isinstance(computed, dict):
        raise ValueError("config.computed must be a mapping of name -> { expr }")
    available = set(taken_names)
    out = {}
    for name, spec in computed.items():
        nm = str(name).strip()
        if not nm.isidentifier():
            raise ValueError(f"computed metric '{name}' is not a valid name "
                             "(letters, digits, underscore; not starting with a digit)")
        if nm in available:
            raise ValueError(f"computed metric '{nm}' collides with an existing metric")
        if not isinstance(spec, dict) or "expr" not in spec:
            raise ValueError(f"computed metric '{nm}' needs an 'expr'")
        _tree, refs = compile_expr(spec["expr"])
        unknown = sorted(r for r in refs if r not in available)
        if unknown:
            raise ValueError(f"computed metric '{nm}' references unknown metric(s): "
                             f"{', '.join(unknown)} (define them, or an earlier "
                             "computed metric, first)")
        out[nm] = {"expr": str(spec["expr"])}
        available.add(nm)
    return out


def compute_metrics(computed, metrics):
    """Evaluate computed metrics in declared order against `metrics`, returning a
    dict of the new values. Later expressions can use earlier computed metrics."""
    out = {}
    work = dict(metrics)
    for name, spec in (computed or {}).items():
        try:
            tree, _ = compile_expr(spec["expr"])
            val = _eval_expr(tree, work)
        except Exception:
            val = None
        out[name] = val
        work[name] = val
    return out


def _ops_for_type(mtype):
    """The operator set offered for a dynamic metric of the given type."""
    if mtype == "number":
        return NUMBER_OPS + ("changed",)
    if mtype == "bool":
        return ("==", "!=", "changed")
    return TEXT_OPS + ("changed",)


# ---------------------------------------------------------------------------
# mqtt_in sensors: subscribe to a topic, expose its payload as a metric (Phase 3)
# ---------------------------------------------------------------------------
MQTT_IN_PARSE = {"number": "number", "bool": "bool", "string": "text"}


def mqtt_input_specs(inputs):
    """Metric specs for mqtt_in sensors: {metric: {type, ops}} keyed by the
    configured metric name (used as-is, not prefixed)."""
    out = {}
    for it in (inputs or []):
        metric = str((it or {}).get("metric", "")).strip()
        if not metric:
            continue
        mtype = MQTT_IN_PARSE.get(str(it.get("parse", "number")).strip().lower(), "number")
        if mtype == "number":
            ops = NUMBER_OPS + ("changed",)
        elif mtype == "bool":
            ops = ("==", "!=", "changed")
        else:
            ops = TEXT_OPS + ("changed",)
        out[metric] = {"type": mtype, "ops": ops}
    return out


def _validate_mqtt_inputs(inputs, taken_names):
    """Validate/normalize the `mqtt_inputs:` list. `taken_names` are metric names
    already in use (built-ins + variables) that an input must not shadow."""
    if not isinstance(inputs, list):
        raise ValueError("'mqtt_inputs' must be a list")
    clean, seen = [], set()
    for it in inputs:
        if not isinstance(it, dict):
            raise ValueError("each mqtt_in entry must be a mapping")
        topic = str(it.get("topic", "")).strip()
        metric = str(it.get("metric", "")).strip()
        parse = str(it.get("parse", "number")).strip().lower()
        if not topic:
            raise ValueError("mqtt_in entry needs a 'topic'")
        if not metric or not all(c.isalnum() or c == "_" for c in metric):
            raise ValueError(f"mqtt_in metric '{metric}' must be alphanumeric/underscore")
        if metric in taken_names:
            raise ValueError(f"mqtt_in metric '{metric}' collides with an existing metric")
        if metric in seen:
            raise ValueError(f"duplicate mqtt_in metric '{metric}'")
        if parse not in MQTT_IN_PARSE:
            raise ValueError(f"mqtt_in '{metric}': parse must be one of "
                             f"{tuple(MQTT_IN_PARSE)}")
        seen.add(metric)
        clean.append({"topic": topic, "metric": metric, "parse": parse})
    return clean


def coerce_payload(payload, parse):
    """Coerce a raw MQTT payload to the input's type. Returns None when a
    'number' payload isn't numeric (so the metric holds its last value)."""
    try:
        s = payload.decode() if isinstance(payload, (bytes, bytearray)) else str(payload)
    except Exception:
        return None
    s = s.strip()
    if parse == "number":
        return _as_number(s, None, "mqtt_in payload")
    if parse == "bool":
        low = s.lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        # Unrecognized payload: hold the last value (handle_mqtt_input skips None)
        # instead of fabricating a real "off".
        return None
    return s


def handle_mqtt_input(in_store, topic_map, topic, payload):
    """Route an incoming message to its metric and store the coerced value.
    Pure (no network) so it's unit-testable. A None coercion is ignored so the
    last good value persists. Returns True when a known input's value actually
    changed (so an event-driven loop knows a re-evaluation is worthwhile)."""
    it = topic_map.get(topic)
    if not it:
        return False
    val = coerce_payload(payload, it["parse"])
    if val is None:
        return False
    metric = it["metric"]
    changed = in_store.get(metric) != val
    in_store[metric] = val
    return changed


# ---------------------------------------------------------------------------
# http_poll: GET a JSON endpoint on an interval, map fields to metrics (Phase 3)
# ---------------------------------------------------------------------------
def http_input_specs(inputs):
    """Metric specs for http_poll mappings: {metric: {type, ops}}."""
    out = {}
    for src in (inputs or []):
        for mp in (src or {}).get("map", []):
            metric = str((mp or {}).get("metric", "")).strip()
            if not metric:
                continue
            mtype = MQTT_IN_PARSE.get(str(mp.get("type", "number")).strip().lower(), "number")
            out[metric] = {"type": mtype, "ops": _ops_for_type(mtype)}
    return out


def _validate_http_inputs(inputs, taken_names):
    """Validate/normalize the `http_inputs:` list. `taken_names` are metric names
    already in use that a mapping must not shadow."""
    if not isinstance(inputs, list):
        raise ValueError("'http_inputs' must be a list")
    clean, seen = [], set(taken_names)
    for src in inputs:
        if not isinstance(src, dict):
            raise ValueError("each http_inputs entry must be a mapping")
        url = str(src.get("url", "")).strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("http_inputs entry needs a url starting http:// or https://")
        interval = max(1, int(_as_number(src.get("interval_minutes", 5), 5,
                                         "http_inputs.interval_minutes")))
        timeout = max(1, int(_as_number(src.get("timeout", 10), 10, "http_inputs.timeout")))
        mapping = src.get("map")
        if not isinstance(mapping, list) or not mapping:
            raise ValueError(f"http_inputs '{url}': 'map' must be a non-empty list")
        clean_map = []
        for mp in mapping:
            if not isinstance(mp, dict):
                raise ValueError(f"http_inputs '{url}': each map entry must be a mapping")
            metric = str(mp.get("metric", "")).strip()
            path = str(mp.get("path", "")).strip()
            mtype = str(mp.get("type", "number")).strip().lower()
            if not metric or not all(c.isalnum() or c == "_" for c in metric):
                raise ValueError(f"http_inputs metric '{metric}' must be alphanumeric/underscore")
            if metric in seen:
                raise ValueError(f"http_inputs metric '{metric}' collides with an existing metric")
            if not path:
                raise ValueError(f"http_inputs '{metric}': needs a 'path'")
            if mtype not in MQTT_IN_PARSE:
                raise ValueError(f"http_inputs '{metric}': type must be one of "
                                 f"{tuple(MQTT_IN_PARSE)}")
            seen.add(metric)
            clean_map.append({"metric": metric, "path": path, "type": mtype})
        clean.append({"url": url, "interval_minutes": interval, "timeout": timeout,
                      "map": clean_map})
    return clean


def extract_path(obj, path):
    """Resolve a dotted path (a subset of JSONPath) into a nested JSON value.
    Leading '$.'/'$' is ignored; numeric segments index lists. Returns None if
    any segment is missing."""
    cur = obj
    for part in path.lstrip("$").lstrip(".").split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def coerce_value(value, mtype):
    """Coerce an extracted JSON value to a metric type. Returns None when a
    'number' value isn't numeric (so the metric holds its last value)."""
    if value is None:
        return None
    if mtype == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        return _as_number(value, None, "http value")
    if mtype == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        # Unrecognized value (an unexpected string/object): hold the last value
        # instead of fabricating False, which could read as a real "off".
        return None
    return str(value)


def apply_http_map(data, mapping, store):
    """Extract + coerce each mapped field into `store`. Pure; a None result is
    skipped so the last good value persists."""
    for mp in mapping:
        val = coerce_value(extract_path(data, mp["path"]), mp["type"])
        if val is not None:
            store[mp["metric"]] = val


def _http_fetch_json(url, timeout, user_agent):
    """GET a JSON endpoint. Best-effort: returns the decoded object or None."""
    try:
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
        if r.status_code // 100 != 2:
            LOG.warning("http_poll %s returned HTTP %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:
        LOG.warning("http_poll %s failed: %s", url, e)
        return None


def poll_http_inputs(inputs, store, last_fetch, now, user_agent, fetch=_http_fetch_json):
    """Fetch any http_inputs whose interval has elapsed and merge mapped values
    into `store`. `last_fetch` (url -> datetime) tracks due times across cycles."""
    for src in (inputs or []):
        url = src["url"]
        last = last_fetch.get(url)
        if last is not None and (now - last).total_seconds() < src["interval_minutes"] * 60:
            continue
        last_fetch[url] = now
        data = fetch(url, src.get("timeout", 10), user_agent)
        if data is not None:
            apply_http_map(data, src["map"], store)


def _validate_variables(variables):
    """Validate + normalize the `variables:` section. Returns the cleaned dict."""
    if not isinstance(variables, dict):
        raise ValueError("'variables' must be a mapping of name -> {type, default}")
    clean = {}
    for name, spec in variables.items():
        nm = str(name).strip()
        if not nm or not all(c.isalnum() or c == "_" for c in nm):
            raise ValueError(f"variable name '{name}' must be alphanumeric/underscore")
        spec = dict(spec or {})
        vtype = str(spec.get("type", "bool")).strip().lower()
        if vtype not in VARIABLE_TYPES:
            raise ValueError(f"variable '{nm}': type must be one of {VARIABLE_TYPES}")
        if vtype == "number":
            default = _as_number(spec.get("default", 0), 0, f"variable '{nm}' default")
        else:
            d = spec.get("default", False)
            default = d if isinstance(d, bool) else str(d).strip().lower() in ("true", "1", "yes", "on")
        clean[nm] = {"type": vtype, "default": default}
    return clean


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Sensible floors/limits so a typo in config.yaml (or the web UI) can never put
# the monitor into a tight loop hammering the free NWS API, or hand paho an
# illegal QoS. These are clamped (with a warning) rather than fatal so the
# monitor keeps running on the last-known-good behavior.
MIN_POLL_MINUTES = 1
MIN_LOOKBACK_HOURS = 1
MAX_LOOKBACK_HOURS = 720      # 30 days; NWS observation history is limited anyway

# Diagnostic-only signal (see check_rain.py): the fraction of the lookback
# window a station's non-null precip reports span. It reads high only for a
# station that reports a value EVERY hour incl. 0.0 -- which most real NWS
# stations do NOT do (they report null on dry hours), so coverage tracks "how
# wet was the window", not gauge health, and must NOT gate trust. A station's
# gauge is judged dead by the rain-vs-measured mismatch instead (see
# _resolve_station_precip): present weather shows precipitation, yet the gauge
# measured nothing.
MIN_PRECIP_COVERAGE = 0.5
# How many nearest-first stations to try before giving up (keeps a storm from
# fanning out into dozens of requests against the free API).
MAX_FALLBACK_STATIONS = 5

# Config schema version. A missing `version:` is treated as 1 so every existing
# install keeps loading unchanged. The v2 "Conditions -> Actions" schema (see
# ROADMAP.md) is not implemented yet; the gate exists now so a v2 file is
# rejected with a clear message instead of being silently mis-parsed as v1.
CURRENT_SCHEMA_VERSION = 1


def _as_number(value, default, name):
    """Coerce a YAML scalar to int/float, falling back to default with a warn.
    NaN/Infinity are rejected too: a sensor payload of "nan" must read as
    unavailable (hold last state), not poison every comparison downstream."""
    if isinstance(value, bool):  # bool is a subclass of int; reject it explicitly
        LOG.warning("%s=%r is not a number; using %r", name, value, default)
        return default
    if isinstance(value, float) and not math.isfinite(value):
        LOG.warning("%s=%r is not a finite number; using %r", name, value, default)
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        s = str(value).strip()
        num = int(s) if s.lstrip("-").isdigit() else float(s)
        if isinstance(num, float) and not math.isfinite(num):
            LOG.warning("%s=%r is not a finite number; using %r", name, value, default)
            return default
        return num
    except (TypeError, ValueError):
        LOG.warning("%s=%r is not a number; using %r", name, value, default)
        return default


def validate_config(cfg):
    """Validate structure and sanitize/clamp numeric fields in place.

    Raises ValueError for problems that make the config unusable (missing
    sections, no coordinates, empty rules, malformed rules). Out-of-range
    numbers are clamped with a warning so a small mistake never takes the
    monitor down. Returns the same (mutated) cfg for convenience.
    """
    if not isinstance(cfg, dict):
        raise ValueError("config root must be a mapping")

    # Schema-version gate. Absent == 1 (every current install). Anything other
    # than 1 is rejected clearly rather than mis-read against the v1 structure.
    version = cfg.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"config 'version' must be an integer (got {version!r})")
    if version != CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"config version {version} is not supported by this release "
            f"(expected {CURRENT_SCHEMA_VERSION}). The v2 schema in ROADMAP.md "
            "is not implemented yet.")
    cfg["version"] = version

    for key in ("location", "user_agent", "mqtt", "rules"):
        if key not in cfg:
            raise ValueError(f"config is missing required section: '{key}'")

    loc = cfg["location"]
    if not isinstance(loc, dict) or "latitude" not in loc or "longitude" not in loc:
        raise ValueError("config.location needs 'latitude' and 'longitude'")
    lat = _as_number(loc["latitude"], None, "location.latitude")
    lon = _as_number(loc["longitude"], None, "location.longitude")
    if lat is None or lon is None:
        raise ValueError("location.latitude/longitude must be numbers")
    if not (-90 <= lat <= 90):
        raise ValueError(f"location.latitude {lat} out of range (-90..90)")
    if not (-180 <= lon <= 180):
        raise ValueError(f"location.longitude {lon} out of range (-180..180)")
    loc["latitude"], loc["longitude"] = lat, lon

    if not cfg.get("user_agent") or not str(cfg["user_agent"]).strip():
        raise ValueError("user_agent must be set (NWS requires a real contact)")

    # Operator-set variables -> dynamic metrics (var_<name>), validated first so
    # rules may reference them.
    if "variables" in cfg and cfg["variables"] is not None:
        cfg["variables"] = _validate_variables(cfg["variables"])
    else:
        cfg.setdefault("variables", {})
    # mqtt_in sensors -> dynamic metrics (the configured metric name).
    if "mqtt_inputs" in cfg and cfg["mqtt_inputs"] is not None:
        taken = set(METRIC_SPECS) | set(variable_specs(cfg["variables"]))
        cfg["mqtt_inputs"] = _validate_mqtt_inputs(cfg["mqtt_inputs"], taken)
    else:
        cfg.setdefault("mqtt_inputs", [])
    # http_poll inputs -> dynamic metrics, must not shadow earlier metrics.
    if "http_inputs" in cfg and cfg["http_inputs"] is not None:
        taken = (set(METRIC_SPECS) | set(variable_specs(cfg["variables"]))
                 | set(mqtt_input_specs(cfg["mqtt_inputs"])))
        cfg["http_inputs"] = _validate_http_inputs(cfg["http_inputs"], taken)
    else:
        cfg.setdefault("http_inputs", [])
    # computed (derived) metrics -> reference only metrics defined before them.
    if "computed" in cfg and cfg["computed"] is not None:
        taken = (set(METRIC_SPECS) | set(variable_specs(cfg["variables"]))
                 | set(mqtt_input_specs(cfg["mqtt_inputs"]))
                 | set(http_input_specs(cfg["http_inputs"])))
        cfg["computed"] = _validate_computed(cfg["computed"], taken)
    else:
        cfg.setdefault("computed", {})
    specs = metric_catalogue(cfg)

    if not isinstance(cfg["rules"], list) or not cfg["rules"]:
        raise ValueError("'rules' must be a non-empty list")
    seen_names = set()
    for r in cfg["rules"]:
        if not isinstance(r, dict):
            raise ValueError("each rule must be a mapping")
        for req in ("name", "when", "topic", "on_match"):
            if req not in r:
                raise ValueError(f"rule '{r.get('name', '?')}' is missing '{req}'")
        name = r["name"]
        if name in seen_names:
            raise ValueError(f"duplicate rule name '{name}' (names must be unique)")
        seen_names.add(name)
        # Validate the condition(s) so one malformed rule is caught here rather
        # than blowing up mid-cycle in the monitor.
        _validate_rule_when(r["when"], name, specs)
        # Per-rule on/off switch (default on). A disabled rule is left idle:
        # it's evaluated against nothing and publishes no actions this cycle.
        en = r.get("enabled", True)
        if isinstance(en, str):
            en = en.strip().lower() not in ("false", "0", "no", "off", "")
        r["enabled"] = bool(en)
        # Optional time window + hysteresis (anti-short-cycle) per rule.
        if r.get("window") is not None:
            _validate_window(r["window"], name)
        if r.get("hysteresis") is not None:
            _validate_hysteresis(r["hysteresis"], name)
        # Optional extra actions (mqtt/webhook/notify) on transitions.
        if r.get("actions") is not None:
            r["actions"] = _validate_actions(r["actions"], name)
        # Config-declared manual state (auto|on|off). The web UI sets runtime
        # overrides in overrides.json instead; this is just the fallback/default.
        man = str(r.get("manual", "auto")).strip().lower()
        if man not in ("auto", "on", "off"):
            LOG.warning("Rule '%s': manual=%r invalid; using 'auto'", name, r.get("manual"))
            man = "auto"
        r["manual"] = man

    # --- defaults + clamping for the forgiving numeric knobs ---
    poll = _as_number(cfg.get("poll_interval_minutes", 15), 15, "poll_interval_minutes")
    if poll < MIN_POLL_MINUTES:
        LOG.warning("poll_interval_minutes=%s is below the %d-minute floor; "
                    "clamping (be a good citizen of the free NWS API)",
                    poll, MIN_POLL_MINUTES)
        poll = MIN_POLL_MINUTES
    cfg["poll_interval_minutes"] = poll

    cfg.setdefault("always_publish", False)
    cfg["always_publish"] = bool(cfg["always_publish"])
    # Event-driven re-evaluation: re-run the rules promptly when an mqtt_in
    # sensor message arrives, instead of only once per poll cycle. The slow NWS
    # fetch still runs on poll_interval_minutes; only rule evaluation is woken.
    cfg.setdefault("event_driven", True)
    cfg["event_driven"] = bool(cfg["event_driven"])
    cfg.setdefault("state_file", "weather_state.json")
    # Persisted engine decision history (last_state / last_change / for: timers /
    # changed baseline) so hysteresis and actions survive a monitor restart.
    cfg.setdefault("engine_state_file", "engine_state.json")
    # Where runtime manual overrides + the audit trail live (Phase 2).
    cfg.setdefault("overrides_file", "overrides.json")
    cfg.setdefault("audit_file", "audit.log")
    cfg.setdefault("variables_file", "variables.json")   # operator-set variables (Phase 3)
    # Rolling runtime log the monitor mirrors its logging to, so the web UI's
    # System page can tail it (the two run as separate processes). Blank = off.
    cfg.setdefault("log_file", "monitor.log")
    # Metric history (Phase 4): the monitor appends each cycle's numeric metrics
    # to a small SQLite db so the web UI can chart trends. Best-effort + pruned.
    hist = cfg.setdefault("history", {})
    hist.setdefault("enabled", True)
    hist["enabled"] = bool(hist["enabled"])
    hist.setdefault("file", "history.db")
    hist["retention_days"] = max(1, min(3650, int(
        _as_number(hist.get("retention_days", 14), 14, "history.retention_days"))))

    precip = cfg.setdefault("precipitation", {})
    lb = _as_number(precip.get("lookback_hours", 24), 24, "precipitation.lookback_hours")
    lb = max(MIN_LOOKBACK_HOURS, min(MAX_LOOKBACK_HOURS, int(lb)))
    precip["lookback_hours"] = lb
    # When the nearest station has no working precip gauge, fall through to the
    # next-nearest stations for the accumulation metric (on by default).
    precip.setdefault("station_fallback", True)
    precip["station_fallback"] = bool(precip["station_fallback"])
    mfs = _as_number(precip.get("max_fallback_stations", MAX_FALLBACK_STATIONS),
                     MAX_FALLBACK_STATIONS, "precipitation.max_fallback_stations")
    precip["max_fallback_stations"] = max(1, min(10, int(mfs)))

    web = cfg.setdefault("web", {})
    web.setdefault("enabled", True)
    web.setdefault("host", "0.0.0.0")
    web["port"] = _clamp_port(_as_number(web.get("port", 8080), 8080, "web.port"))
    web.setdefault("username", "")     # blank = no auth (use only on trusted LAN)
    web.setdefault("password", "")
    # Opt-in escape hatch: allow the privileged control surfaces (manual control +
    # MQTT publish) WITHOUT a web login. Off by default (fail closed). Intended for
    # a trusted/isolated LAN -- with it on, anyone who can reach the page can drive
    # MQTT, exactly like an anonymous broker (mosquitto's allow_anonymous).
    web.setdefault("allow_anonymous_control", False)
    web["allow_anonymous_control"] = bool(web["allow_anonymous_control"])
    _has_login = bool(str(web.get("username") or "") and str(web.get("password") or ""))
    _may_control = _has_login or web["allow_anonymous_control"]
    # Manual on/off control of devices from the dashboard. Default off so the UI
    # stays display-only exactly like today. Fail closed: enabling it requires a
    # web login (username AND password) OR allow_anonymous_control, else it is
    # forced back off with a warning.
    amc = bool(web.get("allow_manual_control", False))
    if amc and not _may_control:
        LOG.warning("web.allow_manual_control requires a web login (username + "
                    "password) or web.allow_anonymous_control: true; disabling "
                    "manual control until one is set")
        amc = False
    web["allow_manual_control"] = amc
    # Arbitrary MQTT publishing from the web UI's console. Same posture as manual
    # control: off by default, and enabling it requires a web login OR
    # allow_anonymous_control.
    amp = bool(web.get("allow_mqtt_publish", False))
    if amp and not _may_control:
        LOG.warning("web.allow_mqtt_publish requires a web login (username + "
                    "password) or web.allow_anonymous_control: true; disabling "
                    "MQTT publishing until one is set")
        amp = False
    web["allow_mqtt_publish"] = amp
    # Web UI's live MQTT console (subscribe + buffer). Display-only; independent
    # of publishing. Topics default to everything ("#"); buffer is capped.
    web.setdefault("mqtt_console_enabled", True)
    web["mqtt_console_enabled"] = bool(web["mqtt_console_enabled"])
    topics = web.get("mqtt_console_topics", ["#"])
    if isinstance(topics, str):
        topics = [topics]
    web["mqtt_console_topics"] = [str(t) for t in (topics or ["#"]) if str(t).strip()] or ["#"]
    web["mqtt_console_buffer"] = max(50, min(5000, int(
        _as_number(web.get("mqtt_console_buffer", 500), 500, "web.mqtt_console_buffer"))))

    mq = cfg["mqtt"]
    if not isinstance(mq, dict):
        raise ValueError("config.mqtt must be a mapping")
    mq.setdefault("host", "localhost")
    mq["port"] = _clamp_port(_as_number(mq.get("port", 1883), 1883, "mqtt.port"))
    mq.setdefault("username", "")
    mq.setdefault("password", "")
    mq.setdefault("client_id", "weather-mqtt-controller")
    qos = int(_as_number(mq.get("qos", 1), 1, "mqtt.qos"))
    if qos not in (0, 1, 2):
        LOG.warning("mqtt.qos=%s invalid; using 1", qos)
        qos = 1
    mq["qos"] = qos
    mq.setdefault("retain", True)
    mq["retain"] = bool(mq["retain"])
    mq.setdefault("status_topic", "")   # optional: JSON snapshot of conditions
    # Retained online/offline availability topic + MQTT Last Will. On by default
    # so a dead controller is detectable; set to "" to disable.
    mq.setdefault("availability_topic", "weather-mqtt/status")
    mq["availability_topic"] = str(mq.get("availability_topic") or "")
    # Optional TLS for the broker connection. Absent/disabled == plaintext (the
    # historical behavior). enabled+ca_certs verifies the broker; insecure skips
    # verification (testing only).
    tls = mq.get("tls")
    if tls is not None:
        if not isinstance(tls, dict):
            raise ValueError("config.mqtt.tls must be a mapping")
        tls.setdefault("enabled", False)
        tls["enabled"] = bool(tls["enabled"])
        for k in ("ca_certs", "certfile", "keyfile"):
            if tls.get(k) is not None:
                tls[k] = str(tls[k])
        tls.setdefault("insecure", False)
        tls["insecure"] = bool(tls["insecure"])
        mq["tls"] = tls

    # --- Slack alerts (optional) ---
    slack = cfg.setdefault("slack", {})
    slack.setdefault("enabled", False)
    slack["enabled"] = bool(slack["enabled"])
    slack.setdefault("bot_token", "")      # or set SLACK_BOT_TOKEN in the env
    slack.setdefault("channel", "")        # channel name (#alerts) or ID (C0…)
    mins = _as_number(slack.get("broker_unreachable_minutes", 60), 60,
                      "slack.broker_unreachable_minutes")
    slack["broker_unreachable_minutes"] = max(1, int(mins))
    # Alert when NWS weather has been unusable this long (0 = off).
    swm = _as_number(slack.get("stale_weather_minutes", 0), 0,
                     "slack.stale_weather_minutes")
    slack["stale_weather_minutes"] = max(0, int(swm))

    # --- Remote status push (optional, read-only/outbound) ---
    sp = cfg.setdefault("status_push", {})
    sp.setdefault("enabled", False)
    sp["enabled"] = bool(sp["enabled"])
    sp.setdefault("url", "")          # https endpoint that receives the snapshot
    sp.setdefault("token", "")        # shared secret sent in X-Status-Token

    # Payloads must be strings. Unquoted ON/OFF/YES/NO in YAML parse as
    # booleans -- coerce and warn so a PLC never gets "True" by surprise.
    for r in cfg["rules"]:
        for k in ("on_match", "on_clear"):
            if k in r and not isinstance(r[k], str):
                if isinstance(r[k], bool):
                    LOG.warning("Rule '%s': %s=%r looks like an unquoted YAML "
                                "boolean (ON/OFF/YES/NO). Quote it in config.yaml "
                                "to publish the literal text.", r.get("name"), k, r[k])
                r[k] = str(r[k])
    return cfg


def _clamp_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return 8080
    return port if 1 <= port <= 65535 else 8080


def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    return validate_config(cfg)


def reload_config_or_keep(path, previous):
    """Reload config for a hot cycle, returning `previous` unchanged if the file
    is now unreadable/invalid (bad YAML or failing validation). This is the
    documented fail-safe: a mid-run edit that breaks config.yaml must not take
    the monitor down -- it keeps running on the last-good config. Returns
    (cfg, ok)."""
    try:
        return load_config(path), True
    except Exception as e:
        LOG.error("Config reload failed, keeping previous: %s", e)
        return previous, False


# ---------------------------------------------------------------------------
# NWS / weather.gov client
# ---------------------------------------------------------------------------
def nws_get(url, user_agent, retries=3, timeout=20):
    """GET a weather.gov endpoint with the required User-Agent + retries.

    Retries transient failures (network errors, 5xx, 429) with exponential
    backoff. A non-retryable client error (e.g. 400/403/404) fails fast --
    retrying a rejected User-Agent or a bad station id only wastes time and
    pesters a free API.
    """
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    delay = 2
    for attempt in range(1, retries + 1):
        wait = delay
        try:
            r = _session().get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError as e:
                    raise RuntimeError(f"NWS returned non-JSON for {url}: {e}")
            retryable = r.status_code == 429 or r.status_code >= 500
            LOG.warning("NWS %s returned HTTP %s (attempt %d/%d)%s",
                        url, r.status_code, attempt, retries,
                        "" if retryable else " -- not retrying")
            if not retryable:
                raise RuntimeError(
                    f"NWS request rejected with HTTP {r.status_code}: {url}")
            # Honor Retry-After (seconds form) when the API asks us to back off.
            wait = _retry_after_seconds(r, delay)
        except requests.RequestException as e:
            LOG.warning("NWS request error for %s: %s (attempt %d/%d)",
                        url, e, attempt, retries)
        if attempt < retries:
            # Interruptible backoff: return early if we're shutting down so
            # SIGTERM isn't stuck waiting out the sleep.
            if _SHUTDOWN.wait(wait):
                break
            delay = min(delay * 2, 60)
    raise RuntimeError(f"NWS request failed after {retries} attempts: {url}")


def _retry_after_seconds(resp, default):
    """Parse a Retry-After header (seconds form). Falls back to `default` for the
    HTTP-date form or a missing/garbage header. Capped so a hostile value can't
    park the monitor for hours."""
    ra = resp.headers.get("Retry-After")
    if not ra:
        return default
    try:
        return max(0, min(300, int(float(ra))))
    except (TypeError, ValueError):
        return default


def resolve_location(lat, lon, user_agent, station_override=None,
                     max_stations=MAX_FALLBACK_STATIONS):
    """Resolve lat/lon -> forecast grid + nearest observation stations. Cached.

    Stores `station_ids`, a nearest-first list of up to `max_stations`
    candidates (the override, if any, pinned to the front). Precipitation
    accumulation walks this list until a station with a working gauge is found;
    `station_id` (the first entry) still drives the point metrics.
    """
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            # Trust the cache only if it matches this location AND still has the
            # URLs + station list we need -- a truncated/partial or pre-fallback
            # cache must re-resolve, not feed dead URLs into every cycle.
            if (isinstance(cached, dict)
                    and cached.get("lat") == lat and cached.get("lon") == lon
                    and cached.get("station_override") == station_override
                    and cached.get("forecast_hourly") and cached.get("stations_url")
                    and cached.get("station_ids")):
                LOG.info("Using cached NWS location data")
                return cached
        except Exception:
            pass  # fall through and re-resolve

    LOG.info("Resolving NWS grid point for %s,%s ...", lat, lon)
    points = nws_get(f"{NWS_API}/points/{lat},{lon}", user_agent)
    props = (points or {}).get("properties") or {}
    forecast_hourly = props.get("forecastHourly")
    stations_url = props.get("observationStations")
    if not forecast_hourly or not stations_url:
        raise RuntimeError(
            f"NWS /points response missing forecast/station URLs for {lat},{lon} "
            "(is the location inside US coverage?)")

    # NWS returns observation stations ordered by distance from the point, so
    # this list is already nearest-first.
    nearest = []
    try:
        stations = nws_get(stations_url, user_agent)
        for feat in stations.get("features", []):
            sid = (feat.get("properties") or {}).get("stationIdentifier")
            if sid and sid not in nearest:
                nearest.append(sid)
    except Exception as e:
        LOG.warning("Could not resolve observation stations: %s", e)

    if station_override:
        # Pin the operator's choice first, keep the rest as fallbacks.
        station_ids = [station_override] + [s for s in nearest if s != station_override]
    else:
        station_ids = nearest
    if max_stations and max_stations > 0:
        station_ids = station_ids[:max_stations]

    info = {
        "lat": lat,
        "lon": lon,
        "station_override": station_override,
        "forecast_hourly": forecast_hourly,
        "stations_url": stations_url,
        "grid_id": props.get("gridId"),
        "station_ids": station_ids,
        "station_id": station_ids[0] if station_ids else station_override,
    }
    _atomic_write(CACHE_FILE, json.dumps(info))
    LOG.info("Resolved grid %s; stations (nearest-first): %s",
             info.get("grid_id"), ", ".join(station_ids) or "none")
    return info


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------
def c_to_f(c):
    return None if c is None else round((c * 9 / 5) + 32, 1)


def to_mm(value, unit_code):
    """Normalize an NWS length value (m / mm / cm) to millimeters."""
    if value is None:
        return None
    unit = (unit_code or "").split(":")[-1].lower()
    if unit in ("m", "meter", "meters"):
        return value * 1000.0
    if unit in ("cm", "centimeter", "centimeters"):
        return value * 10.0
    if unit in ("in", "inch", "inches", "[in_i]"):
        return value * 25.4
    # "mm", "millimeter", or unknown -> assume millimeters
    return float(value)


def mm_to_in(mm):
    return None if mm is None else round(mm / 25.4, 2)


# ---------------------------------------------------------------------------
# Precipitation
# ---------------------------------------------------------------------------
def _says_precip(text):
    """True if `text` names precipitation falling at the station (not nearby)."""
    t = (text or "").lower()
    if not t:
        return False
    if any(w in t for w in NOT_HERE_WORDS):
        return False  # e.g. "Showers in Vicinity" -- not at the station
    return any(word in t for word in PRECIP_WORDS)


def detect_raining(obs_props):
    """True if precipitating now, False if clearly not, None if unknown."""
    seen = False
    for w in (obs_props.get("presentWeather") or []):
        seen = True
        if w.get("inVicinity"):
            continue  # phenomenon is near, not at, the station
        if _says_precip((w.get("weather") or "") + " " + (w.get("rawString") or "")):
            return True
    text = (obs_props.get("textDescription") or "").strip()
    if text:
        seen = True
        if _says_precip(text):
            return True
    return False if seen else None


# Coarser accumulation groups NWS reports at synoptic times, longest first. A
# station that omits the hourly `precipitationLastHour` group often still
# reports these, so they are the fallback when no hourly value is available.
_COARSE_PRECIP_FIELDS = (
    ("precipitationLast6Hours", timedelta(hours=6)),
    ("precipitationLast3Hours", timedelta(hours=3)),
)


def _tile_coarse_precip(intervals, now):
    """Sum non-overlapping precip intervals, walking back from `now` and
    preferring the longest interval at each step so overlapping 3h/6h totals
    tile without double-counting (a 3h total is a subset of the 6h total that
    contains it). Each interval is (start, end, mm). Returns total mm, or None
    if there was nothing to tile."""
    if not intervals:
        return None
    # end descending; for a shared end, longest span (earliest start) first so
    # the greedy walk takes the 6h total over the 3h total nested inside it.
    ordered = sorted(intervals, key=lambda iv: iv[0])          # start asc
    ordered.sort(key=lambda iv: iv[1], reverse=True)           # end desc (stable)
    total = 0.0
    cursor = now
    used = False
    for start, end, mm in ordered:
        if end > cursor:
            continue          # overlaps a span we've already counted -> skip
        total += mm
        cursor = start
        used = True
    return total if used else None


def _merged_coverage_seconds(spans, lo, hi):
    """Total seconds covered by the union of (start, end) spans, each clamped to
    [lo, hi]. Overlaps count once."""
    clamped = []
    for s, e in spans:
        s = max(s, lo)
        e = min(e, hi)
        if e > s:
            clamped.append((s, e))
    if not clamped:
        return 0.0
    clamped.sort()
    covered = 0.0
    cur_s, cur_e = clamped[0]
    for s, e in clamped[1:]:
        if s > cur_e:                       # gap -> bank the run, start a new one
            covered += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    covered += (cur_e - cur_s).total_seconds()
    return covered


def _precip_stats(data, hours, now):
    """Parse one station's observations feed into precip stats (no network).

    Returns a dict:
      inches:   measured accumulation over the window, or None if the station
                reported no precip value at all;
      coverage: fraction (0.0-1.0) of the window actually spanned by the
                station's precip reports -- the trust signal for whether it has
                a working gauge (see MIN_PRECIP_COVERAGE);
      saw_obs:  the station reported at least one observation in the window;
      raining:  present weather indicated precipitation at the station.

    Accumulation precedence: hourly `precipitationLastHour` (bucketed by
    clock-hour, max within an hour) when present, else the coarser 3-/6-hour
    synoptic totals tiled without overlap.
    """
    cutoff = now - timedelta(hours=hours)
    window_secs = max((now - cutoff).total_seconds(), 1.0)
    buckets = {}             # "YYYY-MM-DDTHH" -> max mm reported in that hour
    coarse = []              # (start, end, mm) 3h/6h totals, fallback only
    spans = []               # (start, end) of every non-null precip report
    saw_obs = False
    raining = False

    for feat in data.get("features", []):
        p = feat.get("properties", {})
        ts = p.get("timestamp")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue
        # Parse to UTC so the same instant written with different offsets can't
        # land in two buckets and double-count.
        when = when.astimezone(timezone.utc)
        saw_obs = True
        if detect_raining(p) is True:
            raining = True

        plh = p.get("precipitationLastHour") or {}
        mm = to_mm(plh.get("value"), plh.get("unitCode"))
        if mm is not None:
            buckets[when.strftime("%Y-%m-%dT%H")] = max(
                buckets.get(when.strftime("%Y-%m-%dT%H"), 0.0), mm)
            spans.append((when - timedelta(hours=1), when))

        for field, span in _COARSE_PRECIP_FIELDS:
            cmm = to_mm((p.get(field) or {}).get("value"),
                        (p.get(field) or {}).get("unitCode"))
            if cmm is not None:
                coarse.append((when - span, when, cmm))
                spans.append((when - span, when))

    if buckets:
        inches = mm_to_in(sum(buckets.values()))
    else:
        total_mm = _tile_coarse_precip(coarse, now)
        inches = None if total_mm is None else mm_to_in(total_mm)

    coverage = _merged_coverage_seconds(spans, cutoff, now) / window_secs
    return {"inches": inches, "coverage": coverage,
            "saw_obs": saw_obs, "raining": raining}


def _resolve_station_precip(st):
    """Turn one station's `_precip_stats` into (inches, usable).

    `inches` is the accumulation to trust for this station (may be 0.0), or None
    when precip is unknown; `usable` says whether to trust it and stop the
    nearest-first station walk.

    A station's gauge is judged dead by a rain-vs-measured mismatch -- present
    weather shows precipitation, yet the gauge measured nothing -- NOT by how
    much of the window its reports span. Real NWS stations report a value only
    during precip and null when dry, so a coverage threshold rejected every
    normal station and pinned precip_accum_in at "unknown" forever.
    """
    inches = st["inches"]
    if inches is not None:
        # A gauge that measured nothing while present weather shows precip is
        # dead/absent -- its 0.0 is a false-dry, so distrust it (try the next
        # station). Any real measurement (>= 0.01 in after rounding) is trusted.
        if st["raining"] and inches <= 0.0:
            return None, False
        return inches, True
    # No precip value at all in the window.
    if not st["saw_obs"]:
        return None, False                # no observations -> true data gap
    if st["raining"]:
        return None, False                # raining, but the station has no gauge
    return 0.0, True                      # station reporting, all dry -> trust 0.0


def _accumulate_precip(data, hours, now):
    """Single-station accumulation in inches, or None when precip is unknown.

    Pure helper (no network) so it can be unit-tested with a fixture. Returns
    the measured accumulation when the station has a trustworthy gauge reading;
    0.0 when it was reporting but quiet (dry); and None -- so a rule holds its
    last state instead of wrongly reading "dry" -- when there were no
    observations at all, or it is visibly precipitating but the station reports
    no usable gauge value.
    """
    return _resolve_station_precip(_precip_stats(data, hours, now))[0]


def fetch_precip_accum_in(station_id, user_agent, hours, now=None):
    """Measured precip over the last `hours` for a single station, in inches.
    See `_accumulate_precip` for the value semantics."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{NWS_API}/stations/{station_id}/observations?start={quote(start)}"
    data = nws_get(url, user_agent)
    return _accumulate_precip(data, hours, now)


def fetch_precip_accum_best(station_ids, user_agent, hours, now=None,
                            max_stations=MAX_FALLBACK_STATIONS):
    """Measured precip over the last `hours`, walking `station_ids` nearest-first
    until one has a trustworthy gauge reading.

    Returns (inches, station_id). A station whose present weather shows rain but
    whose gauge measured nothing -- a dead/absent gauge, e.g. some ASOS sites --
    is skipped for the next nearest station rather than publishing its bogus 0.0
    (see _resolve_station_precip). A station that is simply reporting a dry
    window is trusted at 0.0. If no station in range reports usable data,
    returns (None, None) so the rule holds its last state.
    """
    now = now or datetime.now(timezone.utc)
    candidates = [s for s in (station_ids or []) if s][:max_stations]
    primary = candidates[0] if candidates else None
    tried = []
    for sid in candidates:
        start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{NWS_API}/stations/{sid}/observations?start={quote(start)}"
        try:
            data = nws_get(url, user_agent)
        except Exception as e:
            LOG.warning("Precip: station %s unreachable (%s); trying next", sid, e)
            continue
        tried.append(sid)
        st = _precip_stats(data, hours, now)
        inches, usable = _resolve_station_precip(st)
        if usable:
            if sid != primary:
                LOG.info("Precip: %s lacked a usable gauge; using nearby %s "
                         "(coverage %.0f%%) -> %.2f in",
                         primary, sid, st["coverage"] * 100, inches)
            return inches, sid
        LOG.info("Precip: station %s not usable for accumulation "
                 "(coverage %.0f%%, raining=%s, measured=%s); trying next",
                 sid, st["coverage"] * 100, st["raining"], st["inches"])
    LOG.warning("Precip: no station within range reported usable accumulation "
                "(tried %s); holding last state", ", ".join(tried) or "none")
    return None, None


def fetch_conditions(loc, user_agent, lookback_hours):
    """Return a dict of current weather metrics for rule evaluation."""
    metrics = {
        "temperature": None,                 # degF
        "wind_speed_mph": None,
        "precipitation_probability": None,   # % (forecast, NOT measured)
        "precip_accum_in": None,             # measured rainfall over lookback
        "is_raining": None,                  # bool: precipitating right now
        "humidity": None,                    # %
        "short_forecast": "",
        "active_alerts": None,               # list of NWS event names; None = fetch failed (hold)
    }

    # --- Hourly forecast: US units, includes forecast precip probability ---
    try:
        hourly = nws_get(loc["forecast_hourly"], user_agent)
        period = hourly["properties"]["periods"][0]
        metrics["temperature"] = float(period["temperature"])  # degF
        metrics["short_forecast"] = period.get("shortForecast", "")

        pop = period.get("probabilityOfPrecipitation", {}).get("value")
        metrics["precipitation_probability"] = float(pop) if pop is not None else 0.0

        ws = period.get("windSpeed", "") or ""           # e.g. "10 to 15 mph"
        nums = [int(s) for s in ws.replace("to", " ").split() if s.isdigit()]
        metrics["wind_speed_mph"] = float(max(nums)) if nums else 0.0
    except Exception as e:
        LOG.warning("Hourly forecast unavailable: %s", e)

    # --- Latest measured observation: temp/humidity + is_raining now ---
    if loc.get("station_id"):
        try:
            obs = nws_get(
                f"{NWS_API}/stations/{loc['station_id']}/observations/latest",
                user_agent,
            )
            op = obs["properties"]
            t = op.get("temperature", {}).get("value")      # degC
            if t is not None:
                metrics["temperature"] = c_to_f(t)
            h = op.get("relativeHumidity", {}).get("value")  # %
            if h is not None:
                metrics["humidity"] = round(h, 1)
            metrics["is_raining"] = detect_raining(op)
        except Exception as e:
            LOG.warning("Latest observation unavailable: %s", e)

        # --- Measured precip accumulation over the lookback window ---
        # Walk nearest-first through the resolved stations so a local site with
        # a dead/absent precip gauge (reports rain but no accumulation) doesn't
        # peg the metric at a bogus 0.0 -- fall through to the next station.
        try:
            station_ids = loc.get("station_ids") or [loc["station_id"]]
            metrics["precip_accum_in"], _ = fetch_precip_accum_best(
                station_ids, user_agent, lookback_hours)
        except Exception as e:
            LOG.warning("Precip accumulation unavailable: %s", e)
    else:
        LOG.warning("No observation station resolved; precipitation metrics "
                    "(precip_accum_in, is_raining) will be unavailable")

    # --- Active NWS alerts for this point ---
    try:
        alerts = nws_get(
            f"{NWS_API}/alerts/active?point={loc['lat']},{loc['lon']}",
            user_agent,
        )
        events = []
        for feat in alerts.get("features", []):
            ev = feat.get("properties", {}).get("event")
            if ev:
                events.append(ev)
        metrics["active_alerts"] = events
    except Exception as e:
        LOG.warning("Alerts unavailable: %s", e)

    return metrics


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------
NUMERIC_OPS = {
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _regex_search(pattern, text):
    """Case-insensitive regex search used by the `regex` operator. A bad pattern
    never raises mid-cycle (validation already rejects it); it just misses."""
    try:
        return re.search(str(pattern), str(text), re.IGNORECASE) is not None
    except re.error:
        return False


def _eval_condition(cond, metrics, rule_name, state=None, now=None, specs=None):
    """One condition: True/False, or None if its metric is unavailable.

    Two history-dependent constructs need the per-cycle `state`/`now`:
      - operator `changed` -> True when the metric differs from last cycle;
      - a `for: <duration>` modifier -> the base condition must hold continuously
        for that long before it counts as True.
    """
    base = _eval_base(cond, metrics, rule_name, state, specs)
    dur = cond.get("for")
    if dur is not None and state is not None and now is not None:
        base = _apply_for(base, _cond_key(rule_name, cond),
                          parse_duration(dur, 0), state, now)
    return base


def _eval_base(cond, metrics, rule_name, state, specs=None):
    """The condition's value before any `for:` sustain gate is applied.

    `specs` is the active metric catalogue (metric_catalogue(cfg)); without it
    only the built-in METRIC_SPECS are known, so dynamic text metrics (mqtt_in
    parse: string / http type: string) would fall through to the numeric path
    and never evaluate."""
    metric = cond["metric"]
    op = cond.get("operator")
    value = cond.get("value")

    # `changed`: did this metric's value move since the previous cycle?
    if op == "changed":
        if state is None:
            return None
        # active_alert's value lives under the plural key in the metric dict.
        key = "active_alerts" if metric == "active_alert" else metric
        cur = metrics.get(key)
        if cur is None:
            return None
        prev = state.prev_metrics.get(key, _UNSET)
        if prev is _UNSET:
            return False          # first observation -> nothing to compare to yet
        return cur != prev

    # Special metric: active NWS alerts
    if metric == "active_alert":
        alerts = metrics.get("active_alerts")
        if alerts is None:
            # Alerts fetch failed this cycle -> unavailable, hold last state
            # (don't read "no alerts" during an outage and clear a warning rule).
            return None
        if op in (None, "any"):
            return len(alerts) > 0
        if op == "contains":
            return any(str(value).lower() in a.lower() for a in alerts)
        if op == "equals":
            # case-insensitive like every other text/alert comparison
            return any(a.lower() == str(value).lower() for a in alerts)
        if op == "regex":
            return any(_regex_search(value, a) for a in alerts)
        LOG.warning("Rule '%s': unknown alert operator '%s'", rule_name, op)
        return False

    # Text metrics (short_forecast, time_weekday, dynamic string inputs, ...):
    # case-insensitive ops.
    spec = (specs or METRIC_SPECS).get(metric) or METRIC_SPECS.get(metric)
    if spec and spec["type"] == "text":
        raw = metrics.get(metric)
        if raw is None and metric not in METRIC_SPECS:
            # A dynamic string input with no reading yet is unavailable ->
            # hold last state (built-in text metrics always carry "" at least).
            LOG.warning("Rule '%s': metric '%s' unavailable this cycle",
                        rule_name, metric)
            return None
        text = str(raw or "")
        if op == "contains":
            return str(value).lower() in text.lower()
        if op == "equals":
            return text.lower() == str(value).lower()
        if op == "in":
            return any(text.lower() == str(v).lower() for v in (value or []))
        if op == "regex":
            return _regex_search(value, text)
        LOG.warning("Rule '%s': unknown text operator '%s'", rule_name, op)
        return False

    # Numeric / boolean metrics
    current = metrics.get(metric)
    if current is None:
        LOG.warning("Rule '%s': metric '%s' unavailable this cycle",
                    rule_name, metric)
        return None
    # Compare against another metric's live value instead of a constant.
    if cond.get("value_metric"):
        rhs = metrics.get(cond["value_metric"])
        if rhs is None:
            LOG.warning("Rule '%s': value_metric '%s' unavailable this cycle",
                        rule_name, cond["value_metric"])
            return None
        fn = NUMERIC_OPS.get(op)
        return None if fn is None else fn(current, rhs)
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        lo = _as_number(value[0], None, "between low")
        hi = _as_number(value[1], None, "between high")
        if lo is None or hi is None:
            return None
        return lo <= current <= hi
    if op == "in":
        items = value or []
        if isinstance(current, bool):
            return current in items
        return any(n is not None and current == n
                   for n in (_as_number(v, None, "in item") for v in items))
    fn = NUMERIC_OPS.get(op)
    if fn is None:
        LOG.warning("Rule '%s': unknown operator '%s'", rule_name, op)
        return None
    return fn(current, value)


def _eval_node(node, metrics, rule_name, state=None, now=None, _depth=0,
               specs=None):
    """Recursively evaluate a `when` node with three-valued logic.

    Returns True, False, or None (a referenced metric was unavailable -> the
    caller leaves the rule's state unchanged). Groups: `any` (OR), `all` (AND),
    and `not` (negation), nestable to arbitrary depth; a leaf is a single
    {metric, operator, value} condition. Unknown (None) propagates so the
    fail-safe "hold last state" behaviour is preserved through nesting.
    """
    if _depth > MAX_WHEN_DEPTH + 5:   # defensive; validation already bounds depth
        return None
    if isinstance(node, dict) and ("any" in node or "all" in node):
        mode = "any" if "any" in node else "all"
        results = [_eval_node(c, metrics, rule_name, state, now, _depth + 1, specs)
                   for c in node[mode]]
        if mode == "any":
            if any(r is True for r in results):
                return True
            if any(r is None for r in results):
                return None      # could still be true once missing data returns
            return False
        if any(r is False for r in results):   # all
            return False
        if any(r is None for r in results):
            return None
        return True
    if isinstance(node, dict) and "not" in node:
        inner = _eval_node(node["not"], metrics, rule_name, state, now,
                           _depth + 1, specs)
        return None if inner is None else (not inner)
    return _eval_condition(node, metrics, rule_name, state, now, specs)


def evaluate_rule(rule, metrics, state=None, now=None, specs=None):
    """Evaluate a rule's `when` (single condition, or a nested any/all/not
    group). Returns True, False, or None (metric(s) unavailable).

    `state`/`now` are needed only by the history-dependent constructs
    (`changed` operator and `for:` sustain); without them those evaluate to
    None/unsustained, so plain rules need no engine state. `specs` is the
    active metric catalogue (metric_catalogue(cfg)); without it dynamic text
    metrics can't be typed and their conditions read as unavailable."""
    return _eval_node(rule["when"], metrics, rule["name"], state, now, 0, specs)


# Sentinel distinguishing "metric never observed" from "observed value None".
_UNSET = object()


class EngineState:
    """Per-monitor history that the `changed` operator and `for:` sustain gate
    need across cycles. Created once and threaded into evaluate_rule each cycle;
    `observe()` is called at the end of a cycle to remember this cycle's metrics
    for the next one's `changed` comparison."""

    def __init__(self):
        self.prev_metrics = {}      # metric name -> value seen last cycle
        self.cond_since = {}        # condition key -> datetime it first held true

    def observe(self, metrics):
        self.prev_metrics = dict(metrics)


def _cond_key(rule_name, cond):
    """Stable identity for a leaf condition's `for:` timer, tied to the rule and
    the condition's content (so an edit re-arms the timer rather than reusing a
    stale one)."""
    return "|".join(str(x) for x in (
        rule_name, cond.get("metric"), cond.get("operator"),
        cond.get("value"), cond.get("value_metric"), cond.get("for")))


def _apply_for(base, key, dur, state, now):
    """Gate a condition's `base` result behind a sustain duration: only True
    once `base` has held True continuously for `dur` seconds. False/None reset
    the timer (and propagate, preserving the unknown->hold fail-safe)."""
    if base is None:
        state.cond_since.pop(key, None)
        return None
    if not base:
        state.cond_since.pop(key, None)
        return False
    since = state.cond_since.get(key)
    if since is None:
        since = state.cond_since[key] = now
    return (now - since).total_seconds() >= dur


# How long a restart gap may be before we stop trusting persisted `for:` sustain
# timers. A quick restart/deploy (seconds) keeps them; a longer outage re-accrues
# them, since we can't prove the condition held *continuously* across a gap we
# have no observations for. last_state/last_change are wall-clock and always kept.
_ENGINE_STATE_SUSTAIN_GAP_S = 600


def save_engine_state(path, last_state, last_change, engine_state, now=None):
    """Persist the engine's decision history (atomic, best-effort) so hysteresis
    timers, `for:` sustain and the `changed` baseline survive a restart. Without
    this, every restart resets `last_change` (so `apply_hysteresis` sees
    elapsed=inf and a load can short-cycle) and clears `last_state` (so actions
    re-fire and directives re-publish spuriously). A `saved_at` stamp lets the
    loader decide whether the restart gap was short enough to trust sustain
    timers."""
    if not path:
        return
    try:
        now = now or datetime.now(timezone.utc)
        data = {
            "saved_at": now.isoformat(timespec="seconds"),
            "last_state": {k: v for k, v in last_state.items() if v is not None},
            "last_change": dict(last_change),
            "cond_since": {k: dt.isoformat() for k, dt in
                           engine_state.cond_since.items()},
        }
        # prev_metrics powers the `changed` operator; keep it only if it's
        # cleanly serializable so a weird value never blocks the whole save.
        try:
            data["prev_metrics"] = json.loads(json.dumps(engine_state.prev_metrics))
        except (TypeError, ValueError):
            data["prev_metrics"] = {}
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except Exception as e:
        LOG.warning("Could not save engine state %s: %s", path, e)


def load_engine_state(path, now=None):
    """Reload persisted decision history written by save_engine_state. Returns
    (last_state, last_change, EngineState). A missing or corrupt file yields a
    clean cold-start state (never raises).

    `last_state`/`last_change` (and the `changed` baseline) are always restored.
    `for:` sustain timers are restored only when the restart gap was short
    (<= _ENGINE_STATE_SUSTAIN_GAP_S); after a longer outage they re-accrue from
    `now`, because a sustain gate asserts the condition held *continuously* and we
    have no observations across the gap to back that up."""
    es = EngineState()
    last_state, last_change = {}, {}
    if not path:
        return last_state, last_change, es
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return last_state, last_change, es
    if not isinstance(data, dict):
        return last_state, last_change, es
    if isinstance(data.get("last_state"), dict):
        last_state = {str(k): bool(v) for k, v in data["last_state"].items()}
    if isinstance(data.get("last_change"), dict):
        last_change = {str(k): str(v) for k, v in data["last_change"].items() if v}
    now = now or datetime.now(timezone.utc)
    saved_at = _parse_iso(data.get("saved_at"))
    short_gap = (saved_at is not None
                 and (now - saved_at).total_seconds() <= _ENGINE_STATE_SUSTAIN_GAP_S)
    cond_since = data.get("cond_since")
    if isinstance(cond_since, dict) and cond_since:
        if short_gap:
            for k, v in cond_since.items():
                dt = _parse_iso(v)
                if dt is not None:
                    es.cond_since[str(k)] = dt
        else:
            LOG.info("Engine-state restart gap too large; re-accruing `for:` "
                     "sustain timers from now")
    if isinstance(data.get("prev_metrics"), dict):
        es.prev_metrics = dict(data["prev_metrics"])
    return last_state, last_change, es


# ---------------------------------------------------------------------------
# Engine: time windows + hysteresis (ROADMAP Phase 1)
# ---------------------------------------------------------------------------
# A rule's evaluated result is its *desired* state. Two optional layers turn
# that into the *committed* state the monitor actually publishes:
#   - `window`: outside its active hours/days the desired state is forced OFF;
#   - `hysteresis`: min_on / min_off / cooldown timers suppress rapid flapping
#     so a real load (pump, valve, compressor) isn't short-cycled.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_duration(value, default=0):
    """Parse a duration into whole seconds. Accepts '30s', '10m', '2h', or a
    bare number (minutes). Returns `default` for None/blank/garbage."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value * 60)            # bare number == minutes
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(value).lower())
    if not m:
        return default
    return int(float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 60}[m.group(2)])


def _parse_hhmm(s):
    """'HH:MM' -> minutes past midnight (0..1440). '24:00' is allowed (end of day)."""
    parts = str(s).strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"time '{s}' must be HH:MM")
    h, mi = int(parts[0]), int(parts[1])
    total = h * 60 + mi
    if not (0 <= mi <= 59) or not (0 <= total <= 1440):
        raise ValueError(f"time '{s}' is out of range")
    return total


def _validate_window(win, rule_name):
    if not isinstance(win, dict):
        raise ValueError(f"rule '{rule_name}': window must be a mapping")
    for k in ("from", "to"):
        if k in win and win[k] is not None:
            try:
                _parse_hhmm(win[k])
            except ValueError as e:
                raise ValueError(f"rule '{rule_name}': window.{k}: {e}")
    days = win.get("days")
    if days is not None:
        if not isinstance(days, list) or not days:
            raise ValueError(f"rule '{rule_name}': window.days must be a non-empty list")
        for d in days:
            if str(d).strip().lower()[:3] not in WEEKDAYS:
                raise ValueError(f"rule '{rule_name}': window.days has invalid day '{d}' "
                                 f"(use {', '.join(WEEKDAYS)})")


def in_window(win, now):
    """True if local civil time `now` (a datetime) is inside `win`.

    `from`/`to` default to the whole day; `to` is exclusive so adjacent windows
    don't overlap. A window whose `from` is later than its `to` wraps past
    midnight (e.g. 22:00->06:00). `days` (mon..sun) filters by weekday.
    """
    if not win:
        return True
    days = win.get("days")
    if days:
        allowed = {str(d).strip().lower()[:3] for d in days}
        if WEEKDAYS[now.weekday()] not in allowed:
            return False
    start = _parse_hhmm(win.get("from", "00:00"))
    end = _parse_hhmm(win.get("to", "24:00"))
    cur = now.hour * 60 + now.minute
    if start == end:
        return True                       # zero-length/degenerate -> treat as always on
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end       # wraps past midnight


def _validate_hysteresis(hyst, rule_name):
    if not isinstance(hyst, dict):
        raise ValueError(f"rule '{rule_name}': hysteresis must be a mapping")
    for k in ("min_on", "min_off", "cooldown"):
        if k in hyst and hyst[k] is not None and parse_duration(hyst[k], None) is None:
            raise ValueError(f"rule '{rule_name}': hysteresis.{k} must be a duration "
                             "like '10m', '30s', '2h', or a number of minutes")


def apply_hysteresis(hyst, prev, desired, last_change, now):
    """Smooth a `desired` bool into the committed bool using min_on/min_off/
    cooldown. `prev` is the current committed state (None if never set),
    `last_change` the datetime it last changed, `now` the current time.

    Returns the state to commit: `desired` when a transition is allowed, or
    `prev` when a timer is still holding the current state.
    """
    if prev is None or prev == desired or not hyst:
        return desired
    elapsed = (now - last_change).total_seconds() if last_change else float("inf")
    if elapsed < parse_duration(hyst.get("cooldown"), 0):
        return prev
    hold = parse_duration(hyst.get("min_on" if prev else "min_off"), 0)
    if elapsed < hold:
        return prev
    return desired


def resolve_desired(rule, metrics, now_local, state=None, now=None, specs=None):
    """The rule's desired state after the time-window gate: outside the window
    the desired state is OFF; inside it is the evaluated `when` (True/False/
    None, where None means hold)."""
    win = rule.get("window")
    if win and not in_window(win, now_local):
        return False
    return evaluate_rule(rule, metrics, state, now, specs)


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def is_daytime(lat, lon, now):
    """True if the sun is up at (lat, lon) for the instant `now` (any-tz
    datetime). Dependency-free sunrise equation; handles polar day/night.

    The solar transit is computed for the integer solar day nearest `now` (not
    the continuous timestamp), then `now` is tested against that day's sunrise
    and sunset.
    """
    jd = now.timestamp() / 86400.0 + 2440587.5       # Julian date (UTC instant)
    n = round(jd - 2451545.0 + lon / 360.0)          # solar-day cycle nearest now
    D = n - lon / 360.0                              # days since the 2000 epoch
    M = math.radians((357.5291 + 0.98560028 * D) % 360)
    C = 1.9148 * math.sin(M) + 0.0200 * math.sin(2 * M) + 0.0003 * math.sin(3 * M)
    lam = math.radians((math.degrees(M) + C + 180 + 102.9372) % 360)
    j_transit = 2451545.0 + D + 0.0053 * math.sin(M) - 0.0069 * math.sin(2 * lam)
    sin_dec = math.sin(lam) * math.sin(math.radians(23.44))
    dec = math.asin(sin_dec)
    lat_r = math.radians(lat)
    denom = math.cos(lat_r) * math.cos(dec)
    if denom == 0:
        return False
    cos_omega = (math.sin(math.radians(-0.833)) - math.sin(lat_r) * sin_dec) / denom
    if cos_omega > 1:
        return False        # polar night: sun never rises
    if cos_omega < -1:
        return True         # polar day: sun never sets
    omega = math.degrees(math.acos(cos_omega)) / 360.0
    return (j_transit - omega) <= jd <= (j_transit + omega)


def schedule_metrics(now, lat=None, lon=None):
    """Clock-derived metrics for time-of-day rules, from local civil time `now`.
    Pure (no clock read here) so it's unit-testable. When `lat`/`lon` are given,
    also includes `time_is_daytime` (sun up at the site). Merged into the metric
    context each cycle alongside the weather metrics."""
    wd = WEEKDAYS[now.weekday()]
    out = {
        "time_hour": now.hour,
        "time_minute": now.minute,
        "time_weekday": wd,
        "time_is_weekend": wd in ("sat", "sun"),
    }
    if lat is not None and lon is not None:
        out["time_is_daytime"] = is_daytime(lat, lon, now)
    return out


# ---------------------------------------------------------------------------
# Manual control: override store + audit log (ROADMAP Phase 2)
# ---------------------------------------------------------------------------
# Operators can override a device to forced ON/OFF from the dashboard. The
# override is persisted (overrides.json) so it survives restarts, and is applied
# as an overlay on top of the config so config edits don't wipe it. "auto" means
# "no override" -> let the rules decide; it is stored as the absence of a key.
MANUAL_STATES = ("auto", "on", "off")


def load_overrides(path):
    """Read the manual-override map {device_name: 'on'|'off'} from disk. Robust:
    a missing or corrupt file yields {} (no overrides)."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if v in ("on", "off")}


def set_override(path, name, state):
    """Persist one device's override. 'auto' clears it (removes the key).
    Atomic write. Returns the resulting overrides map. Raises ValueError on a
    bad state."""
    if state not in MANUAL_STATES:
        raise ValueError(f"manual state must be one of {MANUAL_STATES}")
    overrides = load_overrides(path)
    if state == "auto":
        overrides.pop(name, None)
    else:
        overrides[name] = state
    # fsync: a forced Auto/On/Off is a safety decision that must survive a power
    # loss, not silently revert.
    _atomic_write(path, json.dumps(overrides, indent=2))
    return overrides


def effective_manual(rule, overrides):
    """The manual state in force for a rule: a runtime override wins over the
    config-declared `manual`, otherwise 'auto'."""
    name = rule.get("name")
    if name in overrides:
        return overrides[name]
    return rule.get("manual", "auto")


def _coerce_var(value, vtype, default):
    """Coerce a stored/incoming variable value to its declared type."""
    if vtype == "number":
        return _as_number(value, default, "variable")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def load_variables(path, declared):
    """Effective variable values: declared defaults overlaid with any persisted
    operator settings (type-coerced). Only declared names are returned."""
    try:
        stored = json.loads(Path(path).read_text())
        if not isinstance(stored, dict):
            stored = {}
    except Exception:
        stored = {}
    out = {}
    for name, spec in (declared or {}).items():
        vtype, default = spec.get("type", "bool"), spec.get("default")
        out[name] = _coerce_var(stored[name], vtype, default) if name in stored else default
    return out


def set_variable(path, name, value, declared):
    """Persist one operator-set variable (must be declared). Atomic write."""
    if name not in (declared or {}):
        raise ValueError(f"unknown variable '{name}'")
    spec = declared[name]
    coerced = _coerce_var(value, spec.get("type", "bool"), spec.get("default"))
    try:
        stored = json.loads(Path(path).read_text())
        if not isinstance(stored, dict):
            stored = {}
    except Exception:
        stored = {}
    stored[name] = coerced
    _atomic_write(path, json.dumps(stored, indent=2))
    return coerced


def variable_metrics(values):
    """Map effective variable values to their metric names (var_<name>)."""
    return {VAR_PREFIX + str(k): v for k, v in (values or {}).items()}


_AUDIT_MAX_BYTES = 5_000_000   # rotate audit.log past ~5 MB (one .1 backup kept)


def audit(path, **event):
    """Append one JSON event to the audit log. Best-effort: never raises.
    Rotates the file to <path>.1 once it grows past _AUDIT_MAX_BYTES, so a
    chatty rule can't grow it without bound over a months-long runtime."""
    try:
        p = Path(path)
        try:
            if p.exists() and p.stat().st_size > _AUDIT_MAX_BYTES:
                p.replace(str(p) + ".1")
        except OSError:
            pass
        event.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with open(path, "a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
    except Exception as e:
        LOG.warning("Could not write audit log %s: %s", path, e)


def read_audit(path, limit=200):
    """Return the most recent audit events (newest first), up to `limit`.
    Robust to a missing file or unparseable lines. Reaches into the rotated
    backup (<path>.1) when the current file alone can't fill `limit`, so the
    Activity page doesn't go near-empty right after a rotation."""
    try:
        lines = Path(path).read_text().splitlines()
    except Exception:
        lines = []
    if len(lines) < limit:
        try:
            prev = Path(str(path) + ".1").read_text().splitlines()
            lines = prev[-(limit - len(lines)):] + lines
        except Exception:
            pass
    if not lines:
        return []
    out = []
    for ln in lines[-limit:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    out.reverse()
    return out


# ---------------------------------------------------------------------------
# Metric history (Phase 4): a small SQLite log of each cycle's numeric metrics,
# so the web UI can chart trends. Recording is best-effort and pruned to the
# retention window; reading tolerates a missing/locked db.
# ---------------------------------------------------------------------------
def _history_numeric(metrics):
    """The subset of a metrics dict worth charting: numbers and bools (as 0/1).
    Text/list metrics (short_forecast, active_alerts) are skipped."""
    out = {}
    for name, v in (metrics or {}).items():
        if isinstance(v, bool):
            out[name] = 1.0 if v else 0.0
        elif isinstance(v, (int, float)):
            out[name] = float(v)
    return out


def _history_connect(db_path):
    """Open the history db in WAL mode with a busy timeout so the monitor's
    writes and the web UI's reads (separate processes) don't lock each other
    out -- a plain rollback journal would surface 'database is locked'."""
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    return con


def record_history(db_path, metrics, ts=None, retention_days=14):
    """Append this cycle's numeric metrics to the SQLite history, then prune
    anything older than the retention window. Best-effort: never raises."""
    if not db_path:
        return
    ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = _history_numeric(metrics)
    if not rows:
        return
    try:
        con = _history_connect(db_path)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS samples "
                        "(ts TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_samples_name_ts "
                        "ON samples(name, ts)")
            con.executemany("INSERT INTO samples(ts, name, value) VALUES (?, ?, ?)",
                            [(ts, n, v) for n, v in rows.items()])
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=retention_days)).isoformat(timespec="seconds")
            con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        LOG.warning("Could not record history to %s: %s", db_path, e)


def read_history(db_path, hours=24, names=None, max_points=600):
    """Return {metric: [[ts, value], ...]} over the last `hours`. Optionally
    restrict to `names`. Robust to a missing db; returns {} on any problem."""
    if not db_path or not Path(db_path).exists():
        return {}
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
    try:
        con = _history_connect(db_path)
        try:
            q = "SELECT ts, name, value FROM samples WHERE ts >= ?"
            args = [cutoff]
            if names:
                q += " AND name IN (%s)" % ",".join("?" * len(names))
                args += list(names)
            q += " ORDER BY ts"
            cur = con.execute(q, args)
            series = {}
            for ts, name, value in cur.fetchall():
                series.setdefault(name, []).append([ts, value])
        finally:
            con.close()
    except Exception as e:
        LOG.warning("Could not read history from %s: %s", db_path, e)
        return {}
    # Down-sample very long series so the payload/redraw stays light.
    for name, pts in series.items():
        if len(pts) > max_points:
            step = len(pts) / max_points
            series[name] = [pts[int(i * step)] for i in range(max_points)]
    return series


def history_metrics(db_path):
    """Distinct metric names present in the history db (for the UI's picker)."""
    if not db_path or not Path(db_path).exists():
        return []
    try:
        con = _history_connect(db_path)
        try:
            return [r[0] for r in con.execute(
                "SELECT DISTINCT name FROM samples ORDER BY name").fetchall()]
        finally:
            con.close()
    except Exception:
        return []


# Lines like: "2026-06-28 16:40:01,234 INFO Published ..." -- pull the level out
# so the UI can colour-code without re-parsing on every render.
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[.,]?\d*\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<msg>.*)$")


def read_log(path, limit=300):
    """Tail the runtime log file. Returns the most recent lines (newest first),
    each a dict {ts, level, msg, raw}. Robust to a missing file; continuation
    lines (e.g. tracebacks) are attached to the preceding entry."""
    if not path:
        return []
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-(limit * 4):]:        # headroom for multi-line entries
        m = _LOG_LINE_RE.match(ln)
        if m:
            out.append({"ts": m.group("ts"), "level": m.group("level"),
                        "msg": m.group("msg"), "raw": ln})
        elif out and ln.strip():
            out[-1]["msg"] += "\n" + ln
            out[-1]["raw"] += "\n" + ln
        elif ln.strip():
            out.append({"ts": None, "level": None, "msg": ln, "raw": ln})
    out = out[-limit:]
    out.reverse()
    return out


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
def _apply_mqtt_tls(client, mq):
    """Enable TLS on a paho client when mqtt.tls.enabled is set. No-op otherwise,
    so plaintext deployments are unchanged. `insecure` skips cert verification
    (testing only)."""
    tls = mq.get("tls") or {}
    if not tls.get("enabled"):
        return
    import ssl
    client.tls_set(
        ca_certs=(tls.get("ca_certs") or None),
        certfile=(tls.get("certfile") or None),
        keyfile=(tls.get("keyfile") or None),
        cert_reqs=ssl.CERT_NONE if tls.get("insecure") else ssl.CERT_REQUIRED,
    )
    if tls.get("insecure"):
        client.tls_insecure_set(True)


def make_mqtt_client(mq, mqtt_inputs=None, in_store=None, on_input=None,
                     on_reconnect=None):
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=mq["client_id"],
    )
    if mq.get("username"):
        client.username_pw_set(mq["username"], mq.get("password", ""))
    _apply_mqtt_tls(client, mq)

    inputs = list(mqtt_inputs or [])
    topic_map = {i["topic"]: i for i in inputs}
    # mqtt_in messages are delivered on paho's network thread; the main loop reads
    # the same store. Guard both sides with this lock so a sensor burst can't race
    # the loop's snapshot (a "dictionary changed size during iteration" crash that
    # would silently drop a whole evaluation cycle).
    client.in_lock = threading.Lock()
    # Retained availability topic. The LWT marks us "offline" if we die
    # unexpectedly (crash/power-loss/network-drop); a birth message marks us
    # "online" on (re)connect. Subscribers can then distinguish "controller alive"
    # from "controller gone" instead of trusting a stale retained directive forever.
    avail = mq.get("availability_topic", "")
    if avail:
        client.will_set(avail, "offline", qos=1, retain=True)
    # Set on every (re)connect to tell the main loop to re-assert all retained
    # directives: a broker restart wipes retained messages, so we must re-publish
    # current state even though our in-memory last_state hasn't changed.
    client.republish_event = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("MQTT connect failed: %s", reason_code)
            return
        LOG.info("Connected to MQTT broker %s:%s", mq["host"], mq["port"])
        if avail:
            client.publish(avail, "online", qos=1, retain=True)
        # (Re)subscribe to mqtt_in sensor topics on every (re)connect.
        for i in inputs:
            client.subscribe(i["topic"])
            LOG.info("Subscribed to mqtt_in '%s' -> metric '%s' (%s)",
                     i["topic"], i["metric"], i["parse"])
        # Ask the loop to re-assert directives, and wake it so the re-publish
        # happens promptly rather than at the next poll.
        client.republish_event.set()
        if on_reconnect is not None:
            try:
                on_reconnect()
            except Exception as e:
                LOG.debug("on_reconnect hook failed: %s", e)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        LOG.warning("Disconnected from MQTT broker (%s); auto-reconnecting",
                    reason_code)

    def on_message(client, userdata, msg):
        if in_store is not None:
            with client.in_lock:
                changed = handle_mqtt_input(in_store, topic_map, msg.topic,
                                            msg.payload)
            # Wake the main loop for a prompt re-evaluation when a *known* input
            # actually changed (event-driven mode). Runs on the network thread, so
            # on_input must be cheap and thread-safe (an Event.set()). Called
            # outside the lock so the callback never blocks the network thread.
            if changed and on_input is not None:
                try:
                    on_input()
                except Exception as e:
                    LOG.debug("on_input hook failed: %s", e)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


# ---------------------------------------------------------------------------
# Slack alerting (broker-unreachable)
# ---------------------------------------------------------------------------
class BrokerWatch:
    """Tracks how long the MQTT broker has been unreachable and decides when to
    fire a Slack alert (once on threshold breach) and a recovery notice.

    Pure/​deterministic (takes `now` as an argument) so it's unit-testable
    without sleeping or a real clock. `update()` returns one of:
      "down"      -> broker has been down past the threshold; alert now
      "recovered" -> broker is back after we had alerted; send the all-clear
      None        -> nothing to announce
    """

    def __init__(self, threshold_minutes=60):
        self.threshold = timedelta(minutes=max(1, int(threshold_minutes)))
        self.down_since = None
        self.alerted = False

    def update(self, connected, now):
        if connected:
            recovered = self.alerted
            self.down_since = None
            self.alerted = False
            return "recovered" if recovered else None
        if self.down_since is None:
            self.down_since = now
        if not self.alerted and (now - self.down_since) >= self.threshold:
            self.alerted = True
            return "down"
        return None

    def downtime_minutes(self, now):
        if self.down_since is None:
            return 0
        return int((now - self.down_since).total_seconds() // 60)


def slack_token(slack):
    """Bot token from the env (preferred) or config. Env wins so the secret can
    stay out of config.yaml."""
    return os.environ.get("SLACK_BOT_TOKEN") or (slack.get("bot_token") or "")


def notify_slack(slack, text):
    """Post a message to Slack via chat.postMessage. Best-effort: never raises."""
    if not slack or not slack.get("enabled"):
        return False
    token = slack_token(slack)
    channel = slack.get("channel", "")
    if not token or not channel:
        LOG.warning("Slack alert wanted but bot token or channel is not set "
                    "(set slack.channel and SLACK_BOT_TOKEN or slack.bot_token)")
        return False
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            LOG.warning("Slack alert rejected: %s", data.get("error"))
            return False
        LOG.info("Slack alert sent to %s", channel)
        return True
    except Exception as e:
        LOG.warning("Slack alert failed to send: %s", e)
        return False


# ---------------------------------------------------------------------------
# State snapshot (consumed by the web UI + optional remote status page)
# ---------------------------------------------------------------------------
def build_snapshot(metrics, rule_rows, lookback, connected, manual_control=False,
                   variables=None):
    """The status object the dashboard(s) consume."""
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_hours": lookback,
        "mqtt_connected": connected,
        "manual_control": bool(manual_control),
        "metrics": metrics,
        "rules": rule_rows,
        "variables": variables or [],
    }


def write_state(path, snapshot):
    """Atomically write the snapshot JSON for the local web UI. Throwaway display
    state refreshed every cycle, so no fsync (don't pay a disk flush per re-eval)."""
    try:
        _atomic_write(path, json.dumps(snapshot, indent=2), fsync=False)
    except Exception as e:
        LOG.warning("Could not write state file %s: %s", path, e)


def push_status(cfg, snapshot):
    """POST the snapshot to an external read-only dashboard. Outbound-only and
    best-effort: never raises, never affects control. Auth via X-Status-Token."""
    if not cfg or not cfg.get("enabled"):
        return False
    url = cfg.get("url", "")
    if not url:
        LOG.warning("status_push enabled but no url is set")
        return False
    headers = {"Content-Type": "application/json"}
    token = cfg.get("token", "")
    if token:
        headers["X-Status-Token"] = token
    try:
        r = requests.post(url, json=snapshot, headers=headers, timeout=10)
        if r.status_code // 100 != 2:
            LOG.warning("status push to %s returned HTTP %s", url, r.status_code)
            return False
        return True
    except Exception as e:
        LOG.warning("status push failed: %s", e)
        return False


def reassert_retained_status(client, status_topic, payload, qos, retain):
    """Re-publish the last status snapshot to the broker after a reconnect.

    A broker restart drops every retained message; rule directives and the
    availability topic are already re-asserted on reconnect, but the retained
    status snapshot would otherwise stay gone until the next weather fetch (a
    whole poll interval). This puts it back immediately. Best-effort and
    idempotent (the payload is retained); a no-op when there's nothing to assert
    or no status topic configured. Returns True on a successful publish."""
    if client is None or not status_topic or payload is None:
        return False
    info = client.publish(status_topic, payload, qos=qos, retain=retain)
    if getattr(info, "rc", 0) != mqtt.MQTT_ERR_SUCCESS:
        LOG.warning("Status re-assert to %s returned rc=%s",
                    status_topic, getattr(info, "rc", "?"))
        return False
    return True


# ---------------------------------------------------------------------------
# Rule actions: extra things to do on a device's on/off transition.
# Beyond the built-in `topic`/`on_match`/`on_clear` publish, a rule may declare
# an `actions:` list. Each entry fires on the `match`, `clear`, or `both`
# transition and is one of: mqtt (extra publish), webhook (HTTP request), or
# notify (Slack). Payloads/URLs/bodies/text support {{metric}} templating with
# the cycle's live values. All firing is best-effort -- a failed action never
# stops the cycle or affects the committed state.
# ---------------------------------------------------------------------------
ACTION_TYPES = ("mqtt", "webhook", "notify")
ACTION_TRIGGERS = ("match", "clear", "both")
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_template(text, metrics):
    """Substitute {{metric}} placeholders with live values. Unknown metrics
    render empty. Non-string input is returned unchanged."""
    if not isinstance(text, str):
        return text

    def sub(m):
        v = metrics.get(m.group(1))
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)
    return _TEMPLATE_RE.sub(sub, text)


def _validate_actions(actions, rule_name):
    """Validate/normalize a rule's `actions:` list. Returns the cleaned list."""
    if not isinstance(actions, list):
        raise ValueError(f"rule '{rule_name}': actions must be a list")
    out = []
    for i, a in enumerate(actions, 1):
        if not isinstance(a, dict):
            raise ValueError(f"rule '{rule_name}': action #{i} must be a mapping")
        kinds = [k for k in ACTION_TYPES if k in a]
        if len(kinds) != 1:
            raise ValueError(f"rule '{rule_name}': action #{i} must have exactly one "
                             f"of {', '.join(ACTION_TYPES)}")
        kind = kinds[0]
        spec = a.get(kind)
        if not isinstance(spec, dict):
            raise ValueError(f"rule '{rule_name}': action #{i} '{kind}' must be a mapping")
        # `trigger` (not `on`): "on" is a YAML 1.1 boolean key, so it would load
        # as True and silently break. trigger: match | clear | both.
        trig = str(a.get("trigger", "both")).strip().lower()
        if trig not in ACTION_TRIGGERS:
            raise ValueError(f"rule '{rule_name}': action #{i} 'trigger' must be one of "
                             f"{', '.join(ACTION_TRIGGERS)}")
        if kind == "mqtt":
            if not str(spec.get("topic", "")).strip():
                raise ValueError(f"rule '{rule_name}': action #{i} mqtt needs a 'topic'")
            q = spec.get("qos")
            if q is not None and int(_as_number(q, 0, "action qos")) not in (0, 1, 2):
                raise ValueError(f"rule '{rule_name}': action #{i} mqtt qos must be 0, 1 or 2")
        elif kind == "webhook":
            if not str(spec.get("url", "")).strip():
                raise ValueError(f"rule '{rule_name}': action #{i} webhook needs a 'url'")
            method = str(spec.get("method", "POST")).strip().upper()
            if method not in ("GET", "POST", "PUT"):
                raise ValueError(f"rule '{rule_name}': action #{i} webhook method must be "
                                 "GET, POST or PUT")
            if spec.get("headers") is not None and not isinstance(spec["headers"], dict):
                raise ValueError(f"rule '{rule_name}': action #{i} webhook headers must be a mapping")
        elif kind == "notify":
            if not str(spec.get("text", "")).strip():
                raise ValueError(f"rule '{rule_name}': action #{i} notify needs 'text'")
        out.append(a)
    return out


def _do_webhook(spec, metrics):
    """Fire one webhook action (best-effort, never raises)."""
    url = render_template(spec.get("url", ""), metrics)
    method = str(spec.get("method", "POST")).strip().upper()
    body = render_template(spec.get("body", ""), metrics)
    headers = {k: render_template(str(v), metrics) for k, v in (spec.get("headers") or {}).items()}
    try:
        if method == "GET":
            requests.get(url, headers=headers, timeout=10)
        elif method == "PUT":
            requests.put(url, data=body, headers=headers, timeout=10)
        else:
            requests.post(url, data=body, headers=headers, timeout=10)
        return True
    except Exception as e:
        LOG.warning("webhook %s %s failed: %s", method, url, e)
        return False


def fire_actions(rule, result, metrics, client, qos, retain, slack_cfg, audit_file=None):
    """Fire a rule's extra actions for an on (result=True) / off (result=False)
    transition. Best-effort: each action is independent and never raises. Each
    real (non-dry-run) action is recorded to the audit log so the Activity page
    can show what fired."""
    want = "match" if result else "clear"

    def record(kind, target, ok):
        if audit_file:
            audit(audit_file, device=rule.get("name", "?"), action="action_fired",
                  kind=kind, target=target, trigger=want, ok=bool(ok), by="monitor")

    for a in (rule.get("actions") or []):
        trig = str(a.get("trigger", "both")).strip().lower()
        if trig not in (want, "both"):
            continue
        try:
            if "mqtt" in a:
                spec = a["mqtt"]
                topic = render_template(str(spec.get("topic", "")), metrics)
                payload = render_template(spec.get("payload", ""), metrics)
                aqos = int(spec.get("qos", qos))
                aretain = bool(spec.get("retain", retain))
                if client is None:
                    LOG.info("[DRY-RUN] would publish action '%s' -> %s (rule '%s')",
                             payload, topic, rule["name"])
                else:
                    info = client.publish(topic, payload, qos=aqos, retain=aretain)
                    ok = getattr(info, "rc", 0) == 0
                    if ok:
                        LOG.info("Action published '%s' -> %s (rule '%s')",
                                 payload, topic, rule["name"])
                    else:
                        LOG.warning("Action publish '%s' -> %s (rule '%s') "
                                    "returned rc=%s", payload, topic,
                                    rule["name"], getattr(info, "rc", "?"))
                    record("mqtt", topic, ok)
            elif "webhook" in a:
                if client is None:
                    LOG.info("[DRY-RUN] would call webhook for rule '%s'", rule["name"])
                else:
                    ok = _do_webhook(a["webhook"], metrics)
                    record("webhook", render_template(str(a["webhook"].get("url", "")), metrics), ok)
            elif "notify" in a:
                text = render_template(str(a["notify"].get("text", "")), metrics)
                if client is None:
                    LOG.info("[DRY-RUN] would notify: %s", text)
                else:
                    ok = notify_slack(slack_cfg, text)
                    record("notify", "slack", ok)
        except Exception as e:
            LOG.warning("Rule '%s' action failed: %s", rule.get("name", "?"), e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Precipitation-driven MQTT controller (NWS / weather.gov)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true",
                        help="Run a single poll then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate rules and log, but don't publish MQTT")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)

    # Mirror the log to a rolling file so the web UI's System page can tail it
    # (monitor and web UI are separate processes). Best-effort: a write failure
    # must never stop the controller -- it just means no in-UI log.
    log_file = cfg.get("log_file")
    if log_file:
        try:
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=3)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(fh)
            LOG.info("Runtime log mirrored to %s", log_file)
        except Exception as e:
            LOG.warning("Could not open log file %s: %s", log_file, e)
    ua = cfg["user_agent"]
    lat = cfg["location"]["latitude"]
    lon = cfg["location"]["longitude"]
    station_override = cfg["location"].get("station_id")
    # Precip station fallback breadth: 1 station when disabled, else the config
    # cap. resolve_location stores this many nearest-first candidates.
    max_stations = (cfg["precipitation"]["max_fallback_stations"]
                    if cfg["precipitation"]["station_fallback"] else 1)
    mq = cfg["mqtt"]

    stop = {"flag": False}
    _SHUTDOWN.clear()   # reset in case main() is re-entered (tests / embedding)

    # Set by an incoming mqtt_in message (event-driven re-eval) or a signal, to
    # break the between-cycle wait early.
    wake = threading.Event()

    def handle_sig(signum, frame):
        LOG.info("Signal %s received, shutting down ...", signum)
        stop["flag"] = True
        _SHUTDOWN.set()   # interrupt any in-flight HTTP backoff sleep
        wake.set()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    def interruptible_sleep(seconds):
        slept = 0
        while slept < seconds and not stop["flag"]:
            time.sleep(min(5, seconds - slept))
            slept += 5

    # Resolve location with backoff instead of crashing if NWS is unreachable at
    # boot -- otherwise systemd would restart us into a tight crash-loop during
    # an outage. Stays inside the process so SIGTERM still stops us promptly.
    loc = None
    delay = 5
    while not stop["flag"]:
        try:
            loc = resolve_location(lat, lon, ua, station_override, max_stations)
            break
        except Exception as e:
            LOG.error("Location resolution failed (%s); retrying in %ds", e, delay)
            interruptible_sleep(delay)
            delay = min(delay * 2, 300)
    if loc is None:
        LOG.info("Stopped before location was resolved.")
        return

    # Live values from mqtt_in sensors, written by the network thread's
    # on_message and read each cycle. Subscriptions are fixed at startup (a
    # topic change needs a restart, like other connection settings).
    mqtt_in_store = {}
    client = None
    event_driven = cfg.get("event_driven", True)
    if not args.dry_run:
        client = make_mqtt_client(mq, cfg.get("mqtt_inputs", []), mqtt_in_store,
                                  on_input=(wake.set if event_driven else None),
                                  on_reconnect=wake.set)
        client.connect_async(mq["host"], int(mq["port"]), keepalive=60)
        client.loop_start()

    # Reload persisted decision history so hysteresis timers, `for:` sustain and
    # the `changed` baseline carry across a restart instead of resetting (which
    # would re-fire actions and let a load short-cycle right after a restart).
    engine_state_file = cfg.get("engine_state_file", "engine_state.json")
    last_state, last_change, engine_state = load_engine_state(engine_state_file)
    http_store, http_last = {}, {}  # latest http_poll values + per-url last fetch
    broker_watch = BrokerWatch(cfg["slack"]["broker_unreachable_minutes"])
    # Track weather freshness so a silent NWS outage is visible (and optionally
    # alerted) instead of the engine acting on stale data with no signal.
    weather_watch = BrokerWatch(max(1, cfg["slack"].get("stale_weather_minutes", 0) or 1))
    last_weather_ok_iso = None
    # Cache the slow NWS weather between poll cycles so an input-triggered
    # re-evaluation reuses it instead of re-fetching (and hammering the API).
    weather_cache = None
    next_weather_at = 0.0      # monotonic deadline for the next weather fetch
    # Last status snapshot published to status_topic (JSON string), kept so it can
    # be re-asserted after a broker reconnect -- a restart drops the retained copy,
    # and it would otherwise stay gone until the next weather fetch (a whole poll
    # interval later) while the directives recover in seconds.
    last_status_payload = None
    EVENT_DEBOUNCE = 0.4       # s: coalesce a burst of messages into one re-eval

    while not stop["flag"]:
        # Reload config each cycle so web-UI edits to rules / thresholds /
        # interval take effect without a restart. Location & MQTT connection
        # are fixed at startup (changing those needs a restart).
        cfg, _cfg_ok = reload_config_or_keep(args.config, cfg)
        lookback = cfg["precipitation"]["lookback_hours"]
        interval = max(MIN_POLL_MINUTES, cfg["poll_interval_minutes"]) * 60
        rules = cfg["rules"]
        # Full metric catalogue (built-ins + variables + mqtt/http inputs +
        # computed) so evaluation can type dynamic metrics (esp. string ones).
        specs = metric_catalogue(cfg)
        state_file = cfg["state_file"]
        # Manual overrides are an overlay re-read each cycle (like config), so the
        # web UI's Auto/On/Off takes effect on the next poll without a restart.
        overrides = load_overrides(cfg["overrides_file"])
        audit_file = cfg["audit_file"]
        allow_manual = cfg["web"].get("allow_manual_control", False)
        declared_vars = cfg.get("variables", {})
        var_values = load_variables(cfg["variables_file"], declared_vars)
        # Connection params (host/port/user/client_id) are fixed at startup, but
        # qos/retain/status_topic are publish-time options we can honor live so
        # web-UI edits to them take effect on the next cycle without a restart.
        mq_live = cfg["mqtt"]
        qos, retain = mq_live["qos"], mq_live["retain"]
        status_topic = mq_live.get("status_topic", "")

        # Hoisted so the weather-freshness watch (below, outside this try) can run
        # even if a cycle raises before they're computed.
        fetched_this_cycle = False
        weather_ok = None
        try:
            # Fetch the slow NWS weather at most once per poll interval; an
            # input-triggered re-eval between fetches reuses the cached weather.
            do_fetch = weather_cache is None or time.monotonic() >= next_weather_at
            if do_fetch:
                m = fetch_conditions(loc, ua, lookback)
                weather_cache = dict(m)
                next_weather_at = time.monotonic() + interval
                fetched_this_cycle = True
                # A fetch "succeeded" if at least one safety-relevant weather
                # metric came back; an all-None result means NWS gave us nothing.
                weather_ok = any(m.get(k) is not None for k in
                                 ("temperature", "precip_accum_in", "is_raining",
                                  "humidity"))
                if weather_ok:
                    last_weather_ok_iso = datetime.now(timezone.utc).isoformat(
                        timespec="seconds")
                LOG.info("Conditions: temp=%s F  humidity=%s%%  wind=%s mph  "
                         "raining=%s  precip_%dh=%s in  precip_prob=%s%%  '%s'  "
                         "alerts=%s",
                         m["temperature"], m["humidity"], m["wind_speed_mph"],
                         m["is_raining"], lookback, m["precip_accum_in"],
                         m["precipitation_probability"], m["short_forecast"],
                         m["active_alerts"] or "none")
                if status_topic:
                    # Stamp the snapshot with the fetch time so a pure-MQTT
                    # consumer (SCADA/PLC) can tell live data from a stale/wedged
                    # controller: the LWT only fires on a dropped connection, not a
                    # hung main loop, so a frozen-but-connected monitor would
                    # otherwise leave a stale retained status looking current
                    # forever. Kept verbatim so a reconnect re-asserts the SAME
                    # timestamp (the data really is from then), not a
                    # misleadingly-fresh one.
                    status_obj = dict(m)
                    status_obj["generated_at"] = datetime.now(
                        timezone.utc).isoformat(timespec="seconds")
                    last_status_payload = json.dumps(status_obj)
                    if client is not None:
                        sinfo = client.publish(status_topic, last_status_payload,
                                               qos=qos, retain=retain)
                        if getattr(sinfo, "rc", 0) != mqtt.MQTT_ERR_SUCCESS:
                            LOG.warning("Status publish to %s returned rc=%s "
                                        "(broker offline?)", status_topic,
                                        getattr(sinfo, "rc", "?"))
            else:
                m = dict(weather_cache)

            now_utc = datetime.now(timezone.utc)
            now_local = now_utc.astimezone()    # system local civil time for windows
            m.update(schedule_metrics(now_local, lat, lon))   # time_* inputs
            m.update(variable_metrics(var_values))  # var_* operator-set inputs
            # Snapshot the mqtt_in store under the network thread's lock so a
            # concurrent sensor message can't change it mid-copy.
            in_lock = getattr(client, "in_lock", None)
            with (in_lock or contextlib.nullcontext()):
                m.update(dict(mqtt_in_store))        # latest mqtt_in sensor values
            poll_http_inputs(cfg.get("http_inputs", []), http_store, http_last,
                             now_utc, ua)            # GET due http_poll endpoints
            m.update(dict(http_store))               # latest http_poll values
            m.update(compute_metrics(cfg.get("computed", {}), m))  # derived metrics
            hist = cfg.get("history", {}) or {}
            if do_fetch and hist.get("enabled", True):
                record_history(hist.get("file", "history.db"), m,
                               ts=now_utc.isoformat(timespec="seconds"),
                               retention_days=int(hist.get("retention_days", 14)))
            # After a (re)connect the broker may have lost all retained messages,
            # so re-assert every rule's current directive once. This re-publishes
            # the retained value but is NOT a transition: hysteresis, last_change,
            # the audit trail and extra actions are all left untouched.
            republish = False
            if client is not None:
                ev = getattr(client, "republish_event", None)
                if ev is not None and ev.is_set():
                    ev.clear()
                    republish = True
                    LOG.info("Re-asserting retained directives after (re)connect")
                    # A broker restart also drops the retained status snapshot. The
                    # rule directives are re-asserted below and availability is
                    # re-published by on_connect; put the status snapshot back too so
                    # status subscribers recover as fast as the directives do. Skip
                    # when we already published a fresh one this cycle (do_fetch).
                    if not do_fetch:
                        reassert_retained_status(client, status_topic,
                                                 last_status_payload, qos, retain)

            rule_rows = []
            for rule in rules:
              try:
                enabled = rule.get("enabled", True)
                prev = last_state.get(rule["name"])
                manual = effective_manual(rule, overrides)
                # Resolution order: disabled -> idle; a manual on/off wins and
                # bypasses window+hysteresis (intent is explicit); otherwise the
                # window-gated, hysteresis-smoothed rule result. None == hold.
                if not enabled:
                    result = None
                elif manual in ("on", "off"):
                    result = (manual == "on")
                else:
                    desired = resolve_desired(rule, m, now_local, engine_state,
                                              now_utc, specs)
                    if desired is None:
                        result = None
                    else:
                        result = apply_hysteresis(
                            rule.get("hysteresis"), prev, desired,
                            _parse_iso(last_change.get(rule["name"])), now_utc)
                if enabled and result is not None:
                    # always_publish heartbeat only on a real poll tick, so an
                    # input-triggered re-eval doesn't re-broadcast every rule.
                    changed = ((prev is None) or (prev != result)
                               or (cfg["always_publish"] and do_fetch)
                               or republish)
                    # Assume committed unless a real publish fails below. A failed
                    # publish leaves last_state unchanged so the next cycle retries
                    # the directive instead of silently dropping a state change.
                    commit = True
                    if changed:
                        payload = rule["on_match"] if result else rule.get("on_clear", "")
                        if payload == "" and not result:
                            pass  # no clear payload configured; nothing to publish
                        else:
                            topic = rule["topic"]
                            if client is None:
                                LOG.info("[DRY-RUN] would publish '%s' -> %s "
                                         "(rule '%s', match=%s)",
                                         payload, topic, rule["name"], result)
                            else:
                                info = client.publish(topic, payload,
                                                      qos=qos, retain=retain)
                                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                                    commit = False
                                    LOG.warning("Publish to %s returned rc=%s "
                                                "(broker offline? will retry next "
                                                "cycle)", topic, info.rc)
                                else:
                                    LOG.info("Published '%s' -> %s (rule '%s', "
                                             "match=%s)", payload, topic,
                                             rule["name"], result)
                        # A committed transition updates last_change (so
                        # hysteresis timers measure from the real switch, even
                        # when there is no on_clear payload to publish), is
                        # audited, and fires the extra actions. A FAILED publish
                        # commits nothing -- the transition (and its actions)
                        # retries next cycle instead of firing on a directive
                        # the PLCs never received.
                        if commit and prev != result:
                            last_change[rule["name"]] = now_utc.isoformat(
                                timespec="seconds")
                            audit(audit_file, device=rule["name"],
                                  state="on" if result else "off",
                                  source="manual" if manual in ("on", "off")
                                  else "auto", by="monitor")
                            if rule.get("actions"):
                                fire_actions(rule, result, m, client, qos, retain,
                                             cfg.get("slack", {}), audit_file)
                    if commit:
                        last_state[rule["name"]] = result
                elif enabled and republish and prev is not None and client is not None:
                    # The rule is holding (metric gap) right after a (re)connect,
                    # but the broker may have lost its retained copy -- re-assert
                    # the last committed directive so a PLC that reconnects sees
                    # it. Not a transition: no last_change/audit/actions.
                    payload = rule["on_match"] if prev else rule.get("on_clear", "")
                    if not (payload == "" and not prev):
                        info = client.publish(rule["topic"], payload,
                                              qos=qos, retain=retain)
                        if getattr(info, "rc", 0) != mqtt.MQTT_ERR_SUCCESS:
                            LOG.warning("Re-assert publish to %s returned rc=%s",
                                        rule["topic"], getattr(info, "rc", "?"))
                        else:
                            LOG.info("Re-asserted '%s' -> %s (rule '%s', holding)",
                                     payload, rule["topic"], rule["name"])

                rule_rows.append({
                    "name": rule["name"],
                    "description": rule.get("description", ""),
                    "topic": rule["topic"],
                    "enabled": enabled,
                    "manual": manual,
                    "active": last_state.get(rule["name"]),
                    "current_payload": (rule["on_match"]
                                        if last_state.get(rule["name"]) else
                                        rule.get("on_clear", ""))
                    if last_state.get(rule["name"]) is not None else None,
                    "last_change": last_change.get(rule["name"]),
                })
              except Exception as e:
                # One malformed/erroring rule must not take down the whole
                # cycle; log it and keep evaluating the rest.
                LOG.warning("Rule '%s' failed this cycle, skipping: %s",
                            rule.get("name", "?") if isinstance(rule, dict) else rule, e)

            # Remember this cycle's metrics so next cycle's `changed` can compare,
            # and persist the decision history so it survives a restart.
            engine_state.observe(m)
            save_engine_state(engine_state_file, last_state, last_change,
                              engine_state)

            connected = bool(client is not None and client.is_connected())
            var_rows = [{"name": n, "type": declared_vars[n].get("type", "bool"),
                         "value": var_values.get(n)} for n in declared_vars]
            snapshot = build_snapshot(m, rule_rows, lookback, connected, allow_manual,
                                      var_rows)
            # Poll cadence so the dashboard can tell "live" from "stale" (a
            # frozen snapshot that stopped updating) rather than showing an old
            # state as current.
            snapshot["poll_interval_minutes"] = cfg["poll_interval_minutes"]
            # Surface weather freshness so the dashboard can show "data is N min
            # old" rather than always looking current (the snapshot's `updated`
            # is just when it was built, not when the weather was last fetched).
            snapshot["last_weather_fetch"] = last_weather_ok_iso
            snapshot["weather_ok"] = last_weather_ok_iso is not None
            if last_weather_ok_iso:
                ok_dt = _parse_iso(last_weather_ok_iso)
                if ok_dt is not None:
                    snapshot["weather_age_seconds"] = int(
                        (datetime.now(timezone.utc) - ok_dt).total_seconds())
            write_state(state_file, snapshot)        # local: refresh every re-eval
            if do_fetch:
                # Outbound remote push stays at poll cadence so a chatty sensor
                # in event-driven mode can't spam the external dashboard.
                push_status(cfg.get("status_push", {}), snapshot)

        except Exception as e:
            LOG.error("Poll cycle failed: %s", e)

        # Broker-reachability watch runs every cycle, independent of the weather
        # fetch above, so a Slack alert fires even during an NWS outage.
        if client is not None:
            slack_cfg = cfg.get("slack", {})
            broker_watch.threshold = timedelta(
                minutes=cfg["slack"]["broker_unreachable_minutes"])
            now = datetime.now(timezone.utc)
            trigger = broker_watch.update(client.is_connected(), now)
            if trigger == "down":
                mins = broker_watch.downtime_minutes(now)
                notify_slack(slack_cfg,
                             f":red_circle: *weather-mqtt*: MQTT broker "
                             f"`{mq['host']}:{mq['port']}` has been unreachable for "
                             f"~{mins} min. Irrigation directives are not being "
                             f"published.")
            elif trigger == "recovered":
                notify_slack(slack_cfg,
                             f":large_green_circle: *weather-mqtt*: MQTT broker "
                             f"`{mq['host']}:{mq['port']}` is reachable again.")

        # Weather-freshness watch (opt-in via slack.stale_weather_minutes > 0).
        # Only evaluated on cycles that actually attempted a fetch, so a chatty
        # sensor's between-fetch re-evals don't skew the timer.
        stale_min = cfg["slack"].get("stale_weather_minutes", 0)
        if stale_min and fetched_this_cycle:
            weather_watch.threshold = timedelta(minutes=max(1, int(stale_min)))
            now = datetime.now(timezone.utc)
            wtrig = weather_watch.update(bool(weather_ok), now)
            if wtrig == "down":
                mins = weather_watch.downtime_minutes(now)
                notify_slack(cfg.get("slack", {}),
                             f":red_circle: *weather-mqtt*: no usable NWS weather "
                             f"for ~{mins} min. Directives are running on the last "
                             f"known data.")
            elif wtrig == "recovered":
                notify_slack(cfg.get("slack", {}),
                             ":large_green_circle: *weather-mqtt*: NWS weather data "
                             "is flowing again.")

        if args.once:
            break

        # Wait until the next weather fetch is due, or until an mqtt_in message
        # wakes us early (event-driven). A burst of messages is coalesced into a
        # single re-evaluation via a short debounce. When event_driven is off,
        # nothing but a signal sets `wake`, so this is a plain timer sleep.
        timeout = max(0.0, next_weather_at - time.monotonic())
        woke = wake.wait(timeout)
        if woke and not stop["flag"] and event_driven:
            time.sleep(EVENT_DEBOUNCE)   # let a burst settle before re-evaluating
            LOG.debug("Input event woke the loop; re-evaluating with cached weather")
        wake.clear()

    if client is not None:
        # Mark ourselves offline on a *clean* shutdown (the LWT only fires on an
        # unexpected drop), and wait for it to flush so the retained state is
        # accurate before we disconnect. Use the startup `mq` so this matches the
        # topic the LWT was armed on (connection settings are startup-fixed).
        avail = mq.get("availability_topic", "")
        if avail:
            try:
                info = client.publish(avail, "offline", qos=1, retain=True)
                wfp = getattr(info, "wait_for_publish", None)
                if callable(wfp):
                    wfp(timeout=2)
            except Exception as e:
                LOG.debug("offline availability publish failed: %s", e)
        client.loop_stop()
        client.disconnect()
    LOG.info("Stopped.")


if __name__ == "__main__":
    main()
