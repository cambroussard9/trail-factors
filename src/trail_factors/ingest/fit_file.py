"""Read a .fit file into plain Python structures. No database, no cleaning.

Bronze-layer rule (SPEC.md §3): parse exactly as recorded. The only
transformations applied here are ones §9 mandates as unambiguously correct —
semicircle -> degrees, and reading the timezone offset off the activity message.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from datetime import UTC
from pathlib import Path

import fitdecode

# SPEC.md §9: degrees = semicircles x 180 / 2^31
SEMICIRCLE_TO_DEGREES = 180.0 / 2**31

# A file whose median record gap is at or below this is Every Second recording.
# Above it, Smart Recording. Detected per file, never inferred from the date
# (CLAUDE.md conventions, SPEC.md §12 milestone 0).
ONE_HZ_MAX_MEDIAN_GAP_S = 1.5


def sha256(path: Path) -> str:
    """Content hash. This — not the filename — is the file's identity (§4)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# THE SECOND ALTITUDE CHANNEL, and the key it travels under.
#
# `gps_metadata` messages carry `enhanced_altitude` and `enhanced_speed` and
# NOTHING ELSE -- no timestamp, no position -- so they cannot be joined to
# `record` on any field. They can only be joined on FILE ORDER.
#
# THE PAIRING RULE, ESTABLISHED BY MEASUREMENT rather than assumed: each
# record takes the LAST gps_metadata frame written BEFORE it. On the FR245,
# where both channels are demonstrably the same series, that rule reproduces
# the record's own altitude EXACTLY in ~97% of records, against ~37% for the
# first frame written AFTER. The fix is written, then the record derived from
# it. Verified on a 1 Hz FR245 file.
#
# NO RECORD EVER INHERITS AN OLD FRAME. The pending
# frame is CLEARED once a record takes it, so a record carries a GNSS reading
# only if a gps_metadata frame was written SINCE THE PREVIOUS RECORD;
# otherwise it is NULL. A mid-run GNSS loss therefore leaves NULLs, never a
# stale value, and the
# provenance comparison (both channels required) already skips them. An
# earlier note claimed such records "take an older reading": that was wrong.
# THE ONE RESIDUAL CASE: a record after a long RECORDING gap takes the newest
# frame written during that gap, whose age is unknowable (frames carry no
# timestamp) but bounded by the gap. The PAIRING still stores it -- it is what
# the file says -- but altitude_provenance treats it as MISSING when the gap
# exceeds GNSS_UNKNOWN_AGE_AFTER_GAP_S (a declared choice, not a fit).
#
# WHAT IS DROPPED, and it is not silent: a gps_metadata frame with no record
# after it is unused (on the FR265, a few dozen trailing GPS epochs logged
# while the activity was being saved), and on Smart Recording files the several fixes
# written between two records collapse to the newest one, which is the same
# grid the record channel is already on.
#
# This key is DERIVED, not a FIT field. It is named apart from the FIT
# namespace so nobody reads it as something the watch wrote.
GNSS_ALTITUDE_KEY = "gnss_altitude_m"


def read_messages(path: Path) -> dict[str, list[dict]]:
    """Every data message in the file, grouped by message type.

    Record messages additionally carry GNSS_ALTITUDE_KEY, paired from
    `gps_metadata` by file order (see above). It is the INDEPENDENT altitude
    channel: on a barometric watch the record channel is pressure-derived and
    this one is not, which is what makes the §15.1 provenance question
    answerable at all.
    """
    with fitdecode.FitReader(path) as fit:
        frames = [
            (frame.name, {f.name: f.value for f in frame.fields})
            for frame in fit
            if frame.frame_type == fitdecode.FIT_FRAME_DATA
        ]
    return pair_frames(frames)


def pair_frames(frames) -> dict[str, list[dict]]:
    """Group (name, fields) data frames by type, pairing GNSS altitude onto
    records by FILE ORDER (see GNSS_ALTITUDE_KEY). Pure, so the pairing rule is
    testable without a FIT file."""
    out: dict[str, list[dict]] = defaultdict(list)
    pending_gnss = None
    for name, fields in frames:
        fields = dict(fields)
        if name == "gps_metadata":
            # The NEWEST fix seen since the previous record. Overwriting is
            # the Smart Recording collapse described above.
            if fields.get("enhanced_altitude") is not None:
                pending_gnss = fields["enhanced_altitude"]
        elif name == "record":
            fields[GNSS_ALTITUDE_KEY] = pending_gnss
            # CLEARED: the next record gets a reading only if a NEW frame
            # arrives first. This is what makes a stale pairing impossible.
            pending_gnss = None
        out[name].append(fields)
    return dict(out)


def to_utc_naive(ts):
    """Normalise a FIT timestamp to naive UTC for storage.

    FIT records UTC already; dropping the tzinfo keeps DuckDB reads free of a
    timezone dependency. The local offset is stored separately (§9).
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


def semicircles_to_degrees(value: int | None) -> float | None:
    return None if value is None else value * SEMICIRCLE_TO_DEGREES


def local_tz_offset_hours(messages: dict[str, list[dict]]) -> float | None:
    """UTC offset from activity.timestamp vs activity.local_timestamp (§9).

    local_timestamp is the same instant written in local time but tagged UTC,
    so the difference between them is the offset.
    """
    for act in messages.get("activity", []):
        ts, local = act.get("timestamp"), act.get("local_timestamp")
        if ts is None or local is None:
            continue
        return (local.replace(tzinfo=ts.tzinfo) - ts).total_seconds() / 3600
    return None


def source_from_manufacturer(messages: dict[str, list[dict]]) -> str:
    """Derive source from file CONTENTS, never the folder path (§4).

    Folders under data/raw/ are human organization only.
    """
    file_ids = messages.get("file_id", [])
    manufacturer = file_ids[0].get("manufacturer") if file_ids else None
    if manufacturer == "garmin":
        return "garmin_watch"
    if manufacturer == "strava":
        return "strava_phone"
    raise ValueError(f"Unrecognised file_id.manufacturer: {manufacturer!r}")


def coalesce_records(records: list[dict]) -> list[dict]:
    """One row per timestamp, sorted by timestamp only (§9).

    Strava splits each sample across TWO record messages — one carrying
    `distance`, the other position/altitude/speed/gps_accuracy — and repeats
    some timestamps with distinct positions. Merging on timestamp roughly
    halves the message count into usable samples. First non-null value wins,
    so the duplicates deduplicate deterministically.

    Timestamp-only messages carry no measurement (a Strava file can end with
    one, the activity end marker) and are dropped.

    Garmin writes one message per sample, so this is a no-op there.
    """
    merged: dict = {}
    for r in records:
        ts = r.get("timestamp")
        if ts is None:
            continue
        current = merged.setdefault(ts, {})
        for key, value in r.items():
            if value is not None and key not in current:
                current[key] = value
    out = []
    for ts in sorted(merged):  # timestamp ONLY (§9)
        fields = merged[ts]
        if not any(k != "timestamp" for k in fields):
            continue
        out.append(dict(fields, timestamp=ts))
    return out


def record_gaps_s(records: list[dict]) -> list[float]:
    """Seconds between consecutive record timestamps.

    Sorted by timestamp ONLY — §9 warns that sorting by (timestamp, lat, lon)
    reorders duplicate-timestamp samples into a zigzag.
    """
    stamps = sorted(r["timestamp"] for r in records if r.get("timestamp"))
    return [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)]


def gps_coverage(records: list[dict], island_gap_s: float = 60.0) -> dict:
    """How much of a run actually has position, and whether it is one block.

    §9: a run can carry position on only part of its records, and neither
    `hasPolyline` nor `startLatitude` reveals it. Measured from the file.
    """
    stamps = sorted(r["timestamp"] for r in records if r.get("timestamp"))
    positioned = sorted(
        r["timestamp"] for r in records if r.get("position_lat") is not None
    )
    total = len(stamps)
    out = {
        "records": total,
        "positioned": len(positioned),
        "coverage_fraction": (len(positioned) / total) if total else None,
        "contiguous": None,
        "islands": 0,
        "gps_window_s": 0.0,
        "total_elapsed_s": (stamps[-1] - stamps[0]).total_seconds()
        if total > 1
        else 0.0,
        "first_fix_offset_s": None,
    }
    if not positioned:
        out["contiguous"] = False
        return out
    islands = 1
    for a, b in zip(positioned, positioned[1:], strict=False):
        if (b - a).total_seconds() > island_gap_s:
            islands += 1
    out["islands"] = islands
    out["contiguous"] = islands == 1
    out["gps_window_s"] = (positioned[-1] - positioned[0]).total_seconds()
    out["first_fix_offset_s"] = (positioned[0] - stamps[0]).total_seconds()
    return out


def first_positioned(records: list[dict]) -> tuple[float, float] | None:
    """True start coordinates: the first record that actually has a fix."""
    for r in sorted(
        (r for r in records if r.get("timestamp")), key=lambda r: r["timestamp"]
    ):
        if r.get("position_lat") is not None:
            return (
                semicircles_to_degrees(r["position_lat"]),
                semicircles_to_degrees(r["position_long"]),
            )
    return None


def detect_sampling_regime(records: list[dict]) -> tuple[str, float | None]:
    """(regime, median_gap_s) measured from the file itself (§12 milestone 0)."""
    gaps = record_gaps_s(records)
    if not gaps:
        return "unknown", None
    median_gap = statistics.median(gaps)
    regime = "1hz" if median_gap <= ONE_HZ_MAX_MEDIAN_GAP_S else "smart_recording"
    return regime, median_gap


# What SENSOR produced `enhanced_altitude`. The FIT field is identical on every
# device; only the device says what fed it. Provenance therefore cannot be
# recovered from a record, which is why it is stored beside the value (§14d).
#
# UNKNOWN is deliberate and must stay that way: a new device has to be
# classified explicitly rather than silently inheriting "gps". §15.1 makes
# provenance decide whether the elevation channel is INDEPENDENT of our DEM,
# and a wrong default there produces a confident comparison of a DEM to itself.
ALTITUDE_SOURCE_BY_DEVICE = {
    "fr245": "gps",
    "fr245_music": "gps",
    # `fr265_large` is what the watch actually reports in file_id. Its absence
    # is what made the map fail on the first FR265 file: every record would
    # have carried altitude_source='unknown' while the watch plainly has a
    # barometer. The map is now a CROSS-CHECK on a measured answer, not the
    # answer -- but it is still kept complete, because a disagreement between
    # measurement and expectation is the finding.
    "fr265_large": "barometric",
    # FR265 has a barometric altimeter. Garmin auto-calibrates it against GPS
    # and possibly a DEM server-side, so "barometric" records the SENSOR, not a
    # claim of independence -- that is what the §14 calibration test settles.
    "fr265": "barometric",
    "fr265_music": "barometric",
    "fr265s": "barometric",
    "fr265s_music": "barometric",
}


# HOW IDENTICAL THE TWO CHANNELS MUST BE TO COUNT AS ONE SERIES.
#
# A DECLARED EDITORIAL THRESHOLD, said to be declared -- but the gap it sits in
# is enormous and measured: on the FR245 the record channel reproduces the GNSS
# channel EXACTLY in 97.6% of paired records; on the FR265 it does so in 3.7%.
# Anything from 0.1 to 0.9 separates those two files. 0.90 is chosen to leave
# room for a watch that rounds one channel differently, not fitted to anything.
ALTITUDE_IDENTICAL_MIN = 0.90

# Below this many paired records the question is not asked at all: a handful of
# points can agree or disagree by luck.
ALTITUDE_MIN_PAIRS = 50

# A record that follows a RECORDING gap longer than this carries a GNSS reading
# of UNKNOWABLE AGE: gps_metadata frames have no timestamp, so the frame it was
# paired with may have been written anywhere inside the gap. The provenance
# comparison treats it as missing. 60 s is a declared choice, not a fit.
GNSS_UNKNOWN_AGE_AFTER_GAP_S = 60.0


def altitude_provenance(records: list[dict]) -> dict:
    """What FED `enhanced_altitude`, MEASURED from the two channels.

    THE DEVICE MAP WAS THE WRONG PRIMARY EVIDENCE. It failed on the first
    FR265 file -- the watch reports `fr265_large`, the map knew `fr265` --
    and it fails silently, for every future device, in the direction of
    "unknown". Provenance is a property of the DATA and can be measured:

      identical to the GNSS channel        -> the record channel IS GPS
      diverging, and SMOOTHER than GNSS    -> a barometer fed it
      diverging, and rougher               -> unknown; say so, do not guess

    Measured on the two reference files: FR245 identical_fraction 0.976,
    FR265 0.037 with sd 4.59 against the GNSS channel's 5.58.

    THE SMOOTHNESS TEST IS THE DIRECTION, not the magnitude. A barometric
    altimeter is a low-pass channel by construction; GNSS altitude is the
    noisiest thing either watch records. A file where the record channel is
    the ROUGHER one is not a barometer and is not GPS-as-usual either, so it
    returns `unknown` rather than being forced into a category.
    """
    import statistics as _st

    # A GNSS READING OF UNKNOWABLE AGE IS MISSING, not old-but-usable. The gap
    # is measured to the previous record; `records` arrive TIME-ORDERED
    # (coalesce_records sorts them) and the first has no predecessor. Counted
    # only where the reading would otherwise have paired.
    pairs, n_after_gap, prev_ts = [], 0, None
    for r in records:
        ts = r.get("timestamp")
        after_gap = (
            prev_ts is not None
            and ts is not None
            and (ts - prev_ts).total_seconds() > GNSS_UNKNOWN_AGE_AFTER_GAP_S
        )
        prev_ts = ts if ts is not None else prev_ts
        if r.get("enhanced_altitude") is None or r.get(GNSS_ALTITUDE_KEY) is None:
            continue
        if after_gap:
            n_after_gap += 1
            continue
        pairs.append((r["enhanced_altitude"], r[GNSS_ALTITUDE_KEY]))
    out = {
        "source": "unknown",
        "n_pairs": len(pairs),
        "n_excluded_after_recording_gap": n_after_gap,
        "recording_gap_threshold_s": GNSS_UNKNOWN_AGE_AFTER_GAP_S,
        "identical_fraction": None,
        "median_abs_diff_m": None,
        "sd_record_m": None,
        "sd_gnss_m": None,
        "reason": f"fewer than {ALTITUDE_MIN_PAIRS} records carry both channels",
    }
    if len(pairs) < ALTITUDE_MIN_PAIRS:
        return out
    rec = [a for a, _ in pairs]
    gnss = [b for _, b in pairs]
    diffs = [abs(a - b) for a, b in pairs]
    identical = sum(1 for d in diffs if d < 1e-9) / len(diffs)
    sd_rec, sd_gnss = _st.pstdev(rec), _st.pstdev(gnss)
    out.update(
        {
            "identical_fraction": identical,
            "median_abs_diff_m": _st.median(diffs),
            "sd_record_m": sd_rec,
            "sd_gnss_m": sd_gnss,
        }
    )
    if identical >= ALTITUDE_IDENTICAL_MIN:
        out.update(
            {
                "source": "gps",
                "reason": f"the record channel reproduces the GNSS channel "
                f"in {identical:.1%} of records: one series, not two",
            }
        )
    elif sd_rec < sd_gnss:
        out.update(
            {
                "source": "barometric",
                "reason": f"the two channels diverge (identical in "
                f"{identical:.1%}) and the record channel is "
                f"SMOOTHER (sd {sd_rec:.2f} m vs {sd_gnss:.2f} m), "
                f"which is what a pressure-derived channel looks "
                f"like against GNSS",
            }
        )
    else:
        out["reason"] = (
            f"the channels diverge (identical in {identical:.1%}) "
            f"but the record channel is not smoother "
            f"(sd {sd_rec:.2f} m vs {sd_gnss:.2f} m) -- neither "
            f"one series nor a barometer; classify it by hand"
        )
    return out


def altitude_source(device_model: str | None, source: str) -> str:
    """Provenance of `enhanced_altitude` for a whole file.

    Strava is NOT "gps". §5a-bis.1 measured its ascent/descent closing to 0.9%
    on a loop that physically closes to ~12 m; a per-second GPS altitude series
    does not balance that well by accident. §15.4 concludes it is most likely a
    DEM applied server-side. Recorded as `dem_suspected` -- evidence-based but
    unconfirmed -- so a query can EXCLUDE it from any comparison against our own
    DEM without re-deriving that argument (§15.1 circularity).
    """
    if source == "strava_phone":
        return "dem_suspected"
    if device_model is None:
        return "unknown"
    return ALTITUDE_SOURCE_BY_DEVICE.get(device_model.lower(), "unknown")


def session_of(messages: dict[str, list[dict]]) -> dict:
    sessions = messages.get("session", [])
    if not sessions:
        raise ValueError("file has no session message")
    return sessions[0]


def activity_start_utc(messages: dict[str, list[dict]]):
    """session.start_time — NOT file_id.time_created (§9).

    On Strava files time_created is the export time, ~10 min after the run.
    """
    return session_of(messages).get("start_time")


def device_identity(messages: dict[str, list[dict]]) -> tuple[str | None, str | None]:
    """(serial, model). Strava files have no serial; device_model is a model
    string, not a unique device id (§9)."""
    file_ids = messages.get("file_id", [])
    fid = file_ids[0] if file_ids else {}
    serial = fid.get("serial_number")
    model = fid.get("garmin_product")
    if model is None:
        for di in messages.get("device_info", []):
            if di.get("device_model"):
                model = di["device_model"]
                break
    return (
        str(serial) if serial is not None else None,
        str(model) if model is not None else None,
    )
