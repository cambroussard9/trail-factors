"""Open-Meteo weather ingest (SPEC.md §8, milestone 3).

PRIVACY. The standing constraint is that a per-coordinate query stream to an
external service is a route trace (§5a.1). This is not that: it is
ONE coordinate per run, and it is deliberately COARSENED before it leaves the
machine -- rounded to COORD_PRECISION decimal places, ~1.1 km. That is finer
than any model Open-Meteo serves (ERA5 archive is ~11 km; the best forecast
grid is ~1-2 km), so the rounding costs nothing meteorologically while keeping
an exact start point out of a third party's logs. NEVER pass an unrounded
coordinate.

UTC THROUGHOUT. Open-Meteo's archive is indexed by UTC. With a local offset of
several hours, a morning run would otherwise fetch afternoon conditions, and
dew point moves substantially across that gap -- corrupting the single variable §8 exists to capture.

The raw response is cached under data/raw/weather/ for the same reason the API
listing is (§4.2): the archive is external and revises, so a re-run must be
able to reproduce what was actually used without re-querying.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trail_factors.paths import RAW_DIR

CACHE_DIR = RAW_DIR / "weather"
REQUEST_DELAY_S = 1.0
TIMEOUT_S = 30

# ~1.1 km. Finer than any Open-Meteo model; coarse enough not to be an address.
COORD_PRECISION = 2

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARS = ("temperature_2m", "dew_point_2m", "relative_humidity_2m",
               "wind_speed_10m", "wind_direction_10m", "precipitation",
               "cloud_cover", "shortwave_radiation")

# A PRECEDING-HOUR MEAN IS NOT AN INSTANT. Open-Meteo's shortwave_radiation
# stamped 10:00 is the mean over 09:00-10:00 (W/m2, global horizontal,
# OPEN SKY -- the model knows cloud, not canopy). Temperature stamped 10:00 is
# the value AT 10:00. So a record takes the value of the hour it falls INSIDE,
# never a linear interpolation between two means, and duration weighting uses
# each value's own hour.
PRECEDING_HOUR_MEAN = ("shortwave_radiation",)

# §8: a GPS-less treadmill run has no station to query, but NULL would be
# indistinguishable from a fetch failure and would silently drop the run from
# any temperature-aware model. An assumed value that is LABELLED is usable; an
# unlabelled one is a lie.
INDOOR_TEMP_C = 22.8            # ~73 F
INDOOR_DEW_POINT_C = 10.0       # ~50 F, typical conditioned air
INDOOR_HUMIDITY_PCT = 45.0

# weather_source, one per row, the same pattern as records.altitude_source.
# Provenance travels WITH the value so a query can tell a reanalysis reading
# from a provisional forecast without joining anything.
SOURCE_FORECAST = "forecast"        # provisional; replaced when ERA5 catches up
SOURCE_ERA5 = "era5"                # reanalysis archive, final
ASSUMED_INDOOR = "assumed_indoor"   # labelled assumption, never a measurement

# The archive/reanalysis endpoint lags several days (§8). A run OLDER than
# this is fetched from the archive, and a cached provisional (forecast) row
# for it is re-fetched by every `weather` pass -- which `sync` runs -- and
# flips to is_final on success. Was 6 with an integer `.days > 6`, i.e.
# "at least 7 whole days"; now stated as 7 and compared as a duration, so the
# rule reads the way it is said.
ARCHIVE_LAG_DAYS = 7


class WeatherFetchError(RuntimeError):
    """Weather should essentially ALWAYS be available for an outdoor run (§8).

    It needs only a coordinate and a timestamp, both of which every positioned
    run has. A failure here is worth flagging, not an expected gap.
    """


def round_coord(value: float) -> float:
    """Coarsen a coordinate before it leaves the machine. See the module docstring."""
    return round(value, COORD_PRECISION)


def _get(url: str, params: dict) -> dict:
    time.sleep(REQUEST_DELAY_S)
    query = urllib.parse.urlencode(params, doseq=True)
    with urllib.request.urlopen(f"{url}?{query}", timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def use_archive(start_utc: datetime, today: datetime | None = None) -> bool:
    """Is the run older than the archive lag? (strictly: older than 7 days)"""
    today = (today if today is not None
             else datetime.now(timezone.utc).replace(tzinfo=None))
    return today - start_utc > timedelta(days=ARCHIVE_LAG_DAYS)


def fetch(lat: float, lon: float, start_utc: datetime, end_utc: datetime,
          today: datetime | None = None) -> dict:
    """Hourly weather covering a run, plus the 24 h before it for the wetness proxy.

    Returns the raw Open-Meteo payload with `_source` and `_is_final` attached.
    """
    lat, lon = round_coord(lat), round_coord(lon)
    # 24 h of lead-in for precip_prev_24h, and an hour of tail so the last
    # record has a bracketing observation to interpolate against.
    first = (start_utc - timedelta(hours=25)).date()
    last = (end_utc + timedelta(hours=1)).date()
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "UTC",          # §8: pass UTC, never local
        "wind_speed_unit": "ms",    # SI internally (CLAUDE.md)
    }
    if use_archive(start_utc, today):
        params |= {"start_date": first.isoformat(), "end_date": last.isoformat()}
        payload = _get(ARCHIVE_URL, params)
        payload["_source"], payload["_is_final"] = SOURCE_ERA5, True
    else:
        # The archive has not caught up yet; the forecast endpoint serves
        # recent past hours via past_days. Marked NOT final and backfilled
        # later (§8).
        span = (datetime.now(timezone.utc).replace(tzinfo=None).date()
                - first).days
        params |= {"past_days": min(max(span, 1), 92), "forecast_days": 2}
        payload = _get(FORECAST_URL, params)
        payload["_source"], payload["_is_final"] = SOURCE_FORECAST, False
    # Record what LEFT THE MACHINE, not just what came back. Open-Meteo echoes
    # its own grid-cell centre, so the response alone cannot show that the
    # coordinate was coarsened -- and that is the claim worth auditing.
    payload["_requested_lat"], payload["_requested_lon"] = lat, lon
    if "hourly" not in payload or not payload["hourly"].get("time"):
        raise WeatherFetchError(
            f"Open-Meteo returned no hourly series for {lat},{lon} "
            f"{start_utc:%Y-%m-%d}")
    return payload


def source_of(payload: dict) -> str:
    """The weather_source label `fetch` attached to a cached payload."""
    return payload.get("_source", SOURCE_FORECAST)


def needs_backfill(payload: dict, start_utc: datetime,
                   today: datetime | None = None) -> bool:
    """Is this a provisional forecast row that ERA5 can now replace? (§8)

    Archive rows REPLACE forecast rows when they arrive -- weather_build
    deletes and re-inserts per activity, so the replacement is atomic and no
    row is left carrying a stale provenance.
    """
    return (not payload.get("_is_final", False)
            and use_archive(start_utc, today))


def cache_path(activity_id: str) -> Path:
    return CACHE_DIR / f"{activity_id}.json"


def save_cache(activity_id: str, payload: dict) -> Path:
    """ATOMIC (temp + os.replace): a backfill REWRITES existing caches, and a
    half-written one would lose the record of what was actually used."""
    import os
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = cache_path(activity_id)
    tmp = out.with_name(out.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, out)
    return out


def load_cache(activity_id: str) -> dict | None:
    path = cache_path(activity_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def to_rows(payload: dict) -> list[dict]:
    """Flatten the hourly block into rows with naive-UTC timestamps."""
    h = payload["hourly"]
    rows = []
    for i, stamp in enumerate(h["time"]):
        def val(name):
            series = h.get(name)
            return series[i] if series is not None and i < len(series) else None
        rows.append({
            "timestamp": datetime.fromisoformat(stamp).replace(tzinfo=None),
            "temp_c": val("temperature_2m"),
            "dew_point_c": val("dew_point_2m"),
            "humidity_pct": val("relative_humidity_2m"),
            "wind_mps": val("wind_speed_10m"),
            "wind_dir_deg": val("wind_direction_10m"),
            "precip_mm": val("precipitation"),
            "cloud_cover_pct": val("cloud_cover"),
            "shortwave_wm2": val("shortwave_radiation"),
        })
    return rows


def hour_of(stamp: datetime) -> datetime:
    """The stamp of the preceding-hour mean that COVERS `stamp`:
    [T-1h, T) is covered by the value stamped T."""
    return stamp.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def duration_weighted(rows: list[dict], start: datetime, end: datetime,
                      key: str, preceding_hour_mean: bool = True) -> dict:
    """One run-level value, weighted by how long the run spent in each hour.

    A preceding-hour mean stamped T covers [T-1h, T); an instantaneous value
    stamped T stands for [T-30min, T+30min). Each value's weight is the
    seconds of [start, end] inside its interval. A missing value contributes
    no weight, and `coverage` says how much of the run had one -- a mean
    over half a run is a different claim from a mean over all of it.
    """
    run_s = (end - start).total_seconds()
    num = den = 0.0
    n = 0
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        t = r["timestamp"]
        lo, hi = ((t - timedelta(hours=1), t) if preceding_hour_mean
                  else (t - timedelta(minutes=30), t + timedelta(minutes=30)))
        w = (min(end, hi) - max(start, lo)).total_seconds()
        if w <= 0:
            continue
        num += v * w
        den += w
        n += 1
    return {"value": num / den if den > 0 else None,
            "seconds_weighted": den, "run_seconds": run_s,
            "coverage": den / run_s if run_s > 0 else None,
            "n_hours": n,
            "convention": ("preceding-hour mean: value stamped T covers "
                           "[T-1h, T)" if preceding_hour_mean else
                           "instantaneous: value stamped T stands for "
                           "[T-30min, T+30min)")}


def precip_prev_24h(rows: list[dict], start_utc: datetime) -> float:
    """Rain in the 24 h before the run -- §8's automated trail-wetness proxy."""
    window = start_utc - timedelta(hours=24)
    return sum(r["precip_mm"] for r in rows
               if r["precip_mm"] is not None
               and window <= r["timestamp"] < start_utc)


SCALARS = ("temp_c", "dew_point_c", "humidity_pct", "wind_mps", "precip_mm",
           "cloud_cover_pct")


def interpolate(rows: list[dict], stamps: list[datetime]) -> list[dict]:
    """Weather at each record timestamp (§8: per record, never a run scalar).

    WIND DIRECTION IS CARRIED BY NEAREST OBSERVATION, NOT INTERPOLATED. It is
    circular: interpolating 350 deg to 10 deg passes through 180 -- the exact
    opposite -- and a calm observation coded 0 deg corrupts its neighbours.
    §8 measured this changing a result: a correlation against headwind moved
    materially between carrying direction by nearest and interpolating it.
    """
    ordered = sorted(rows, key=lambda r: r["timestamp"])
    if not ordered:
        return []
    by_hour = {r["timestamp"]: r.get("shortwave_wm2") for r in ordered}
    times = [r["timestamp"] for r in ordered]
    out = []
    for stamp in stamps:
        hi = 0
        while hi < len(times) and times[hi] < stamp:
            hi += 1
        if hi == 0:
            lo = hi = 0
        elif hi >= len(times):
            lo = hi = len(times) - 1
        else:
            lo = hi - 1
        a, b = ordered[lo], ordered[hi]
        span = (b["timestamp"] - a["timestamp"]).total_seconds()
        frac = ((stamp - a["timestamp"]).total_seconds() / span) if span else 0.0
        row = {"timestamp": stamp}
        for name in SCALARS:
            va, vb = a[name], b[name]
            if va is None or vb is None:
                # CLAUDE.md: never `va or vb` -- 0.0 mm of rain is a real value.
                row[name] = va if va is not None else vb
            else:
                row[name] = va + (vb - va) * frac
        nearest = a if frac < 0.5 else b
        row["wind_dir_deg"] = nearest["wind_dir_deg"]
        # PRECEDING-HOUR MEAN: the hour the record falls inside, not a blend.
        row["shortwave_wm2"] = by_hour.get(hour_of(stamp))
        out.append(row)
    return out


def indoor_rows(stamps: list[datetime]) -> list[dict]:
    """§8's labelled indoor assumption for a treadmill run."""
    return [{"timestamp": s, "temp_c": INDOOR_TEMP_C,
             "dew_point_c": INDOOR_DEW_POINT_C,
             "humidity_pct": INDOOR_HUMIDITY_PCT, "wind_mps": 0.0,
             "wind_dir_deg": None, "precip_mm": 0.0, "cloud_cover_pct": None,
             "shortwave_wm2": None}
            for s in stamps]


# ESTIMATED WBGT -- DISPLAY AND STORAGE ONLY. HARD RULE: nothing new enters the §2 residual model until its own n
# threshold is met. This is a number to READ beside temperature, not a
# covariate.
#
# THE NAMED APPROXIMATION: the Australian Bureau of Meteorology's simplified
# WBGT, from air temperature and humidity alone:
#     WBGT = 0.567 * Ta + 0.393 * e + 3.94
#     e    = (RH / 100) * 6.105 * exp(17.27 * Ta / (237.7 + Ta))   [hPa]
# By the BoM's own description it does NOT take radiation or wind: it
# ASSUMES moderately high radiation and light wind. So it does not use the
# shortwave series at all -- that travels beside it as the evidence of how
# much sun there was -- and in deep shade, under canopy or in wind it
# overstates. A radiation-aware estimate (Liljegren) needs solar geometry
# and a direct/diffuse split; it is not built.
WBGT_APPROXIMATION = "Australian BoM simplified WBGT (Ta, RH only)"
WBGT_FORMULA = ("WBGT = 0.567*Ta + 0.393*e + 3.94, "
                "e = (RH/100) * 6.105 * exp(17.27*Ta / (237.7 + Ta)) hPa")


def estimated_wbgt_c(temp_c: float | None, humidity_pct: float | None):
    """The BoM simplified WBGT in deg C, or None when an input is missing."""
    if temp_c is None or humidity_pct is None:
        return None
    e = (humidity_pct / 100.0) * 6.105 * math.exp(
        17.27 * temp_c / (237.7 + temp_c))
    return 0.567 * temp_c + 0.393 * e + 3.94

