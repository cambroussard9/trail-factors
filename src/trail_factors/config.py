"""Athlete configuration: `config/athlete.yaml`, validated on load.

Every value that belongs to ONE athlete -- where they train, which run orders
the segment library, which projection their ground sits in -- lives in this
file rather than in the code. The file is gitignored; `athlete.example.yaml`
is the committed template, with one comment per field.

THE FILE IS OPTIONAL. With no file every field takes its default, and a
default is always the NEUTRAL choice: no clusters, no reference run, no
consent. A feature that cannot work without a value (terrain work without a
projection) asks for it by name when it is reached, rather than guessing one
here.

VALIDATION IS STRICT, AND EVERY ERROR NAMES THE FIELD AND THE LINE. An unknown
key is refused rather than ignored, because a misspelt key that is ignored is
a setting silently not applied -- the commonest way for a config file to lie.
Wrong types and out-of-range values are refused the same way. The file is
parsed with `yaml.safe_load` only; `yaml.compose` (also safe: it builds nodes,
never objects) is used solely to find line numbers for error messages.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from trail_factors import paths


class ConfigError(ValueError):
    """The athlete configuration is invalid, or a required field is unset."""


@dataclass(frozen=True)
class Cluster:
    name: str
    lat: float
    lon: float
    radius_km: float


@dataclass(frozen=True)
class AthleteConfig:
    clusters: tuple[Cluster, ...] = ()
    projection_crs: str | None = None
    reference_run_garmin_id: str | None = None
    unresolved_peak_run: str | None = None
    hr_above_default_bpm: float = 180.0
    time_above_ladder_bpm: tuple[float, ...] = (170.0, 175.0, 180.0, 185.0)
    route_fingerprint_accepted: bool = False
    dem_tile_suffix_guess: str | None = None
    goal_event: str | None = None
    # Where the values came from: a display path, or "defaults".
    source: str = field(default="defaults (no config file)", compare=False)

    def require(self, name: str) -> object:
        """The value of `name`, or a ConfigError saying how to set it."""
        value = getattr(self, name)
        if value is None or value == ():
            raise ConfigError(
                f"{name} is not set, and this operation needs it. Set it in "
                f"{paths.display_path(paths.CONFIG_FILE)} (copy "
                f"config/athlete.example.yaml if you have no config yet).")
        return value


# Names a cluster may not take: report.cluster_of returns them itself.
RESERVED_CLUSTER_NAMES = frozenset({"NO_GPS", "OTHER"})
CLUSTER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}Z$")
GARMIN_ID_RE = re.compile(r"^\d{6,15}$")
CRS_RE = re.compile(r"^EPSG:\d{4,5}$")
TILE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
HR_RANGE = (40.0, 250.0)
LAT_RANGE = (-90.0, 90.0)
LON_RANGE = (-180.0, 180.0)
RADIUS_RANGE_KM = (0.1, 500.0)
GOAL_EVENT_MAX_CHARS = 80

TOP_LEVEL_KEYS = ("clusters", "projection_crs", "reference_run_garmin_id",
                  "unresolved_peak_run", "hr_above_default_bpm",
                  "time_above_ladder_bpm", "route_fingerprint_accepted",
                  "dem_tile_suffix_guess", "goal_event")
CLUSTER_KEYS = ("name", "lat", "lon", "radius_km")


# --- locating a value in the file ------------------------------------------

def _line_index(node, path=(), out=None) -> dict:
    """Map every path in the node tree to its 1-based line number."""
    out = {} if out is None else out
    out[path] = node.start_mark.line + 1
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            key = k.value if isinstance(k, yaml.ScalarNode) else str(k)
            out[path + (key,)] = k.start_mark.line + 1
            _line_index(v, path + (key,), out)
    elif isinstance(node, yaml.SequenceNode):
        for i, v in enumerate(node.value):
            _line_index(v, path + (i,), out)
    return out


def _duplicate_keys(node, path=()) -> list[tuple[tuple, int]]:
    """safe_load silently keeps the LAST of two equal keys. Find them first."""
    found = []
    if isinstance(node, yaml.MappingNode):
        seen = set()
        for k, v in node.value:
            key = k.value if isinstance(k, yaml.ScalarNode) else str(k)
            if key in seen:
                found.append((path + (key,), k.start_mark.line + 1))
            seen.add(key)
            found += _duplicate_keys(v, path + (key,))
    elif isinstance(node, yaml.SequenceNode):
        for i, v in enumerate(node.value):
            found += _duplicate_keys(v, path + (i,))
    return found


def _dotted(path: tuple) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return out or "<top level>"


class _Checker:
    def __init__(self, where: str, lines: dict):
        self.where, self.lines = where, lines

    def fail(self, path: tuple, problem: str):
        line = self.lines.get(path)
        if line is None and path:
            line = self.lines.get(path[:-1])
        at = f" line {line}" if line is not None else ""
        raise ConfigError(f"{self.where}{at}: {_dotted(path)}: {problem}")

    def number(self, path, v, lo, hi) -> float:
        # bool is a subclass of int; `true` is not a heart rate.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.fail(path, f"must be a number, got {type(v).__name__} {v!r}")
        if not math.isfinite(v) or not lo <= v <= hi:
            self.fail(path, f"{v!r} is out of range ({lo:g} to {hi:g})")
        return float(v)

    def string(self, path, v, pattern: re.Pattern, what: str) -> str:
        if not isinstance(v, str):
            self.fail(path, f"must be a string ({what}), got "
                            f"{type(v).__name__} {v!r}")
        if not pattern.match(v):
            self.fail(path, f"{v!r} is not {what}")
        return v

    def keys(self, path, mapping: dict, allowed: tuple, required=()):
        for k in mapping:
            if k not in allowed:
                self.fail(path + (k,), f"unknown key (allowed: "
                                       f"{', '.join(allowed)})")
        for k in required:
            if k not in mapping:
                self.fail(path, f"missing required key {k!r}")


# --- the fields -------------------------------------------------------------

def _clusters(c: _Checker, v) -> tuple[Cluster, ...]:
    path = ("clusters",)
    if v is None:
        return ()
    if not isinstance(v, list):
        c.fail(path, f"must be a list of clusters, got {type(v).__name__}")
    out, names = [], set()
    for i, item in enumerate(v):
        p = path + (i,)
        if not isinstance(item, dict):
            c.fail(p, f"must be a mapping with keys {', '.join(CLUSTER_KEYS)}")
        c.keys(p, item, CLUSTER_KEYS, required=CLUSTER_KEYS)
        name = c.string(p + ("name",), item["name"], CLUSTER_NAME_RE,
                        "a name of letters, digits, '_' or '-' (max 32), "
                        "starting with a letter")
        if name.upper() in RESERVED_CLUSTER_NAMES:
            c.fail(p + ("name",), f"{name!r} is reserved (the code uses "
                                  f"{', '.join(sorted(RESERVED_CLUSTER_NAMES))})")
        if name in names:
            c.fail(p + ("name",), f"duplicate cluster name {name!r}")
        names.add(name)
        out.append(Cluster(
            name=name,
            lat=c.number(p + ("lat",), item["lat"], *LAT_RANGE),
            lon=c.number(p + ("lon",), item["lon"], *LON_RANGE),
            radius_km=c.number(p + ("radius_km",), item["radius_km"],
                               *RADIUS_RANGE_KM)))
    return tuple(out)


def _projection(c: _Checker, v) -> str | None:
    if v is None:
        return None
    crs = c.string(("projection_crs",), v, CRS_RE, "an 'EPSG:<code>' string")
    from pyproj import CRS
    try:
        parsed = CRS.from_user_input(crs)
    except Exception as exc:  # pyproj raises its own CRSError
        c.fail(("projection_crs",), f"{crs!r} is not a known CRS ({exc})")
    axis_units = {a.unit_name for a in parsed.axis_info}
    if not parsed.is_projected or axis_units != {"metre"}:
        c.fail(("projection_crs",),
               f"{crs!r} must be a PROJECTED CRS in metres (a UTM zone such "
               f"as EPSG:326NN north / EPSG:327NN south); every distance and "
               f"matching radius is computed in it")
    return crs


def _ladder(c: _Checker, v) -> tuple[float, ...]:
    path = ("time_above_ladder_bpm",)
    if not isinstance(v, list) or not v:
        c.fail(path, "must be a non-empty list of heart rates")
    rungs = tuple(c.number(path + (i,), x, *HR_RANGE) for i, x in enumerate(v))
    if any(b <= a for a, b in zip(rungs, rungs[1:])):
        c.fail(path, f"must be strictly increasing, got {list(rungs)}")
    return rungs


def _from_mapping(data: dict, where: str, lines: dict) -> AthleteConfig:
    c = _Checker(where, lines)
    c.keys((), data, TOP_LEVEL_KEYS)
    kw = {}
    if "clusters" in data:
        kw["clusters"] = _clusters(c, data["clusters"])
    if "projection_crs" in data:
        kw["projection_crs"] = _projection(c, data["projection_crs"])
    if data.get("reference_run_garmin_id") is not None:
        v = data["reference_run_garmin_id"]
        # A Garmin id is digits; YAML reads an unquoted one as an int. Both
        # spellings are the same id, so both are accepted -- a bool is not.
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        kw["reference_run_garmin_id"] = c.string(
            ("reference_run_garmin_id",), v, GARMIN_ID_RE,
            "a Garmin activity id (6-15 digits)")
    if data.get("unresolved_peak_run") is not None:
        kw["unresolved_peak_run"] = c.string(
            ("unresolved_peak_run",), data["unresolved_peak_run"], RUN_ID_RE,
            "a run_id like 2025-01-31T1405Z")
    if "hr_above_default_bpm" in data:
        kw["hr_above_default_bpm"] = c.number(
            ("hr_above_default_bpm",), data["hr_above_default_bpm"], *HR_RANGE)
    if "time_above_ladder_bpm" in data:
        kw["time_above_ladder_bpm"] = _ladder(c, data["time_above_ladder_bpm"])
    if "route_fingerprint_accepted" in data:
        v = data["route_fingerprint_accepted"]
        if not isinstance(v, bool):
            c.fail(("route_fingerprint_accepted",),
                   f"must be true or false, got {type(v).__name__} {v!r}")
        kw["route_fingerprint_accepted"] = v
    if data.get("dem_tile_suffix_guess") is not None:
        kw["dem_tile_suffix_guess"] = c.string(
            ("dem_tile_suffix_guess",), data["dem_tile_suffix_guess"],
            TILE_SUFFIX_RE, "a USGS project suffix of letters, digits and '_'")
    if data.get("goal_event") is not None:
        v = data["goal_event"]
        if not isinstance(v, str) or not v.strip():
            c.fail(("goal_event",), f"must be a non-empty string, got {v!r}")
        if len(v) > GOAL_EVENT_MAX_CHARS:
            c.fail(("goal_event",), f"is {len(v)} characters; the limit is "
                                    f"{GOAL_EVENT_MAX_CHARS}")
        kw["goal_event"] = v.strip()
    return AthleteConfig(source=where, **kw)


def parse(text: str, where: str = "<config>") -> AthleteConfig:
    """Validate YAML text. `where` names the file in every error message."""
    try:
        data = yaml.safe_load(text)
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        at = f" line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"{where}{at}: not valid YAML: "
                          f"{getattr(exc, 'problem', None) or exc}") from None
    if data is None:
        return AthleteConfig(source=where)
    if not isinstance(data, dict):
        raise ConfigError(f"{where} line 1: <top level>: must be a mapping of "
                          f"field: value, got {type(data).__name__}")
    dupes = _duplicate_keys(node)
    if dupes:
        path, line = dupes[0]
        raise ConfigError(f"{where} line {line}: {_dotted(path)}: duplicate "
                          f"key (YAML would silently keep only the last)")
    return _from_mapping(data, where, _line_index(node))


def load(path: Path | None = None) -> AthleteConfig:
    """Read and validate the config file; defaults if it does not exist."""
    path = paths.CONFIG_FILE if path is None else Path(path)
    if not path.exists():
        return AthleteConfig()
    return parse(path.read_text(encoding="utf-8"), paths.display_path(path))


@lru_cache(maxsize=1)
def get() -> AthleteConfig:
    """The process-wide configuration, read once. `get.cache_clear()` resets."""
    return load()
