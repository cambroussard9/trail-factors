"""Stationary-bike block annotations — athlete-supplied structure (§18).

WHAT THE WATCH GIVES US ON A STATIONARY RIDE, MEASURED ON THE CORPUS. The
`cycling`/`indoor_cycling` activities recorded by a wrist watch with no bike
sensors paired carry `heart_rate` on every record. NONE carries cadence,
speed, position or power, and `distance_m` is present-but-constant
0.0 rather than null -- a fake zero, not a measurement. There are no `rwd_*`
splits.

So the watch knows WHEN the heart did what, and nothing about what the legs
were doing. The resistance, the RPM and the block boundaries exist only in
the athlete's memory, and that is what this file stores.

TWO RELIABILITIES, BOTH ATHLETE-TYPED, NEITHER SENSOR-GRADE, AND THE SCHEMA
SAYS WHICH IS WHICH. `machine_totals` is the console's end-of-session
readout -- machine-reported, and the machine is the ONLY source for it.
`blocks` and `events` are athlete-recalled. A downstream reader must never
mistake a recalled RPM range for a measurement, so `provenance` is a stored
field rather than a convention someone has to remember.

RAW TEXT IS THE RECORD; THE BLOCKS ARE AN INTERPRETATION OF IT. Same reason
bronze keeps the raw FIT file. If the parse was wrong, the text is what makes
that recoverable.

PARSE AMBIGUITY IS REFUSED, NEVER RESOLVED. This tool takes ALREADY-STRUCTURED
blocks. Shorthand like "0-20 R8 @ 80-85, 20-? R9" -- where one boundary is
missing and a later entry restates an earlier minute -- cannot be parsed
consistently, and guessing produces a confident number in
the one directory that cannot be rebuilt. Parsing is the coaching chat's job,
at save time, with the athlete present to resolve it.

WHAT IS NOT STORED HERE, DELIBERATELY:
  - Athlete-recalled average HR. The watch's record-grain HR is authoritative
    and already in the corpus; a typed average HR survives in `raw_text` only.
  - Perceived effort. Garmin's own post-activity self-evaluation
    (garmin_rpe / garmin_feel, read from the file at ingest) is the one
    perceived-effort channel; a second place to record it is two values that
    can disagree.
  - Anything derived: power, speed or distance from resistance. Not a
    primitive, and explicitly out of scope.
"""

from __future__ import annotations

import datetime
import json

from trail_factors.ingest.athlete_state import atomic_write, load_json, revise
from trail_factors.paths import ATHLETE_DIR, display_path

RIDE_BLOCKS = ATHLETE_DIR / "ride_blocks.json"
BIKE_CONFIG = ATHLETE_DIR / "bike_config.json"
SCHEMA_VERSION = 1
SOURCE = "athlete_chat"

# THE KNOB IS A 360-DEGREE SWIVEL READ BY EYE, not a detent scale: the athlete
# records turns from closed as a decimal.
#
# Quarter turns are the finest resolution a logged value is expected to show
# -- but it is NOT enforced. The knob is eyeballed, an 8.3 is a real
# observation, and refusing it would be hostile to the way the log is kept.
#
# THE SET OF VALUES ACTUALLY LOGGED IS NOT A CONSTANT. It belongs to the
# stored blocks and, if anything ever needs it, is derived from them at read
# time. A constant listing "observed" values goes stale with the next ride and
# then cites old data as measurement.
RESISTANCE_STEP_OBSERVED = 0.25

# THE CEILING IS DECLARED, NOT MEASURED, AND SAYS SO. Where the knob actually
# stops is something the athlete measures, not something this code knows.
# This is a logging-error detector -- it catches 85 typed for 8.5 -- and a
# declared editorial bound, said to be declared.
# WHERE THIS NUMBER LIVES, stated because the answer was wrong once: it is
# THIS CONSTANT, in this file. `bike_config.json` is an OPTIONAL override that
# does not exist yet and is not created by the code.
#
# It is deliberately not created on first save. `data/athlete/` means
# athlete-SUPPLIED state that cannot be regenerated, and a declared default is
# neither supplied nor irreplaceable. Writing 20.0 in there would file a guess
# as athlete input, and every later reader would treat it as though the
# athlete had said it.
#
# Do not quietly promote this number to a measurement.
RESISTANCE_MAX_DECLARED = 20.0
RESISTANCE_MAX_DEFINED_IN = (
    "src/trail_factors/ingest/ride_blocks.py RESISTANCE_MAX_DECLARED"
)

MINUTE_MAX = 600.0  # 10 hours; a bound on typos, not on training


class RideBlockError(ValueError):
    """A malformed or contradictory ride log. Never coerced, never guessed."""


def resistance_max() -> tuple[float, str]:
    """(ceiling, source). Source is the honest half."""
    cfg = load_json(BIKE_CONFIG)
    v = cfg.get("resistance_max_turns")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v), "measured_by_athlete"
    return RESISTANCE_MAX_DECLARED, "declared_editorial_not_measured"


def resistance_max_defined_in() -> str:
    """WHERE the live ceiling comes from, as a path a reader can open."""
    return (
        display_path(BIKE_CONFIG) if BIKE_CONFIG.exists() else RESISTANCE_MAX_DEFINED_IN
    )


def load_ride_blocks() -> dict:
    return load_json(RIDE_BLOCKS)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def normalise_rpm(v, where: str) -> list | None:
    """A single target or a range, stored as [low, high]. Absent is absent.

    STORED AS THE RANGE STATED, NEVER COLLAPSED TO A MIDPOINT. "80-85" is
    what the athlete remembers, and a midpoint would invent a precision the
    recollection does not have. A single value becomes [v, v] so every reader
    handles one shape.
    """
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        if len(v) != 2 or _num(v[0]) is None or _num(v[1]) is None:
            raise RideBlockError(
                f"{where}: rpm range must be [low, high] numbers, got {v!r}"
            )
        lo, hi = float(v[0]), float(v[1])
        if lo > hi:
            raise RideBlockError(f"{where}: rpm range [{lo}, {hi}] is backwards")
        return [lo, hi]
    n = _num(v)
    if n is None:
        raise RideBlockError(f"{where}: rpm must be a number or [low, high], got {v!r}")
    return [float(n), float(n)]


def derive_blocks(entries: list) -> list:
    """END-ANCHORED IN, EXPLICIT [start_min, end_min) OUT.

    The athlete's log is a sequence of (resistance, rpm?, end_min): the knob is
    turned while still pedalling, so each block starts exactly where the last
    ended and the first starts at 0. There are no rests between blocks -- an
    off-bike break (water bottle, restroom) happens with the clock running and
    is logged as an EVENT, not as a gap, because the athlete does not change
    resistance for it.

    `start_min` IS DERIVED AND REJECTED IF SUPPLIED. One way to state a
    boundary means no way for two statements to disagree.
    """
    if not isinstance(entries, list) or not entries:
        raise RideBlockError(
            "blocks must be a non-empty list of {resistance, end_min, rpm?} entries"
        )
    out, prev_end = [], 0.0
    ceiling, ceiling_source = resistance_max()
    for i, e in enumerate(entries):
        where = f"block {i} (entry {i + 1} of {len(entries)})"
        if not isinstance(e, dict):
            raise RideBlockError(f"{where}: must be an object, got {e!r}")
        if "start_min" in e:
            raise RideBlockError(
                f"{where}: start_min is DERIVED from the previous entry's "
                f"end_min and must not be supplied. Blocks are end-anchored: "
                f"give (resistance, end_min, rpm?) and the start follows."
            )
        end = _num(e.get("end_min"))
        if end is None:
            raise RideBlockError(f"{where}: end_min is required and must be a number")
        if end <= prev_end:
            raise RideBlockError(
                f"{where}: end_min {end} does not advance past the previous "
                f"block's end ({prev_end}). Two entries claim the same "
                f"minutes; blocks cannot overlap and cannot be zero length."
            )
        if end > MINUTE_MAX:
            raise RideBlockError(
                f"{where}: end_min {end} exceeds {MINUTE_MAX:.0f} minutes"
            )
        r = _num(e.get("resistance"))
        if r is None:
            raise RideBlockError(
                f"{where}: resistance is required (knob turns from closed, decimal)"
            )
        if not 0.0 <= r <= ceiling:
            raise RideBlockError(
                f"{where}: resistance {r} is outside [0, {ceiling}]. That "
                f"ceiling is {ceiling_source.replace('_', ' ')} and is "
                f"defined in {resistance_max_defined_in()}. If the knob really "
                f"goes past it, create {display_path(BIKE_CONFIG)} with "
                f'{{"resistance_max_turns": <measured>}} -- that file does not '
                f"exist yet -- rather than widening the declared default."
            )
        unknown = set(e) - {"resistance", "end_min", "rpm"}
        if unknown:
            raise RideBlockError(
                f"{where}: unknown field(s): {', '.join(sorted(unknown))}"
            )
        out.append(
            {
                "start_min": prev_end,
                "end_min": float(end),
                "resistance": float(r),
                "rpm": normalise_rpm(e.get("rpm"), where),
                "rpm_stated_as": (
                    None if e.get("rpm") is None else json.dumps(e["rpm"])
                ),
            }
        )
        prev_end = float(end)
    return out


def _validate_events(events) -> list:
    if events is None:
        return []
    if not isinstance(events, list):
        raise RideBlockError("events must be a list of {text, minute?}")
    out = []
    for i, e in enumerate(events):
        if not isinstance(e, dict) or not str(e.get("text", "")).strip():
            raise RideBlockError(f"event {i}: needs a non-empty `text`")
        unknown = set(e) - {"minute", "text", "duration_min"}
        if unknown:
            raise RideBlockError(
                f"event {i}: unknown field(s): {', '.join(sorted(unknown))}"
            )
        minute = _num(e.get("minute"))
        dur = _num(e.get("duration_min"))
        # NEVER PARSED INTO ANYTHING. It is stored and surfaced beside the
        # per-block HR, and that is the whole contract.
        out.append(
            {
                "minute": None if minute is None else float(minute),
                "duration_min": None if dur is None else float(dur),
                "text": str(e["text"]).strip(),
            }
        )
    return out


def _validate_machine_totals(mt) -> dict | None:
    if mt is None:
        return None
    if not isinstance(mt, dict):
        raise RideBlockError("machine_totals must be an object")
    unknown = set(mt) - {"distance", "distance_unit", "avg_rpm"}
    if unknown:
        raise RideBlockError(
            f"machine_totals: unknown field(s): {', '.join(sorted(unknown))}"
        )
    out = {}
    if "distance" in mt:
        d = _num(mt["distance"])
        if d is None or d < 0:
            raise RideBlockError(
                "machine_totals.distance must be a non-negative number"
            )
        # THE UNIT IS REQUIRED WITH THE NUMBER. A bare distance whose unit is
        # assumed is the decimal-minutes bug class: every value looks
        # plausible and nothing downstream can detect the error.
        unit = mt.get("distance_unit")
        if unit not in ("mi", "km"):
            raise RideBlockError(
                "machine_totals.distance needs "
                "distance_unit 'mi' or 'km'; a distance "
                "without its unit is not a measurement"
            )
        out["distance"] = float(d)
        out["distance_unit"] = unit
    if "avg_rpm" in mt:
        a = _num(mt["avg_rpm"])
        if a is None or not 0 <= a <= 250:
            raise RideBlockError("machine_totals.avg_rpm must be 0-250")
        out["avg_rpm"] = float(a)
    return out or None


PROVENANCE = {
    "raw_text": "athlete_verbatim",
    "blocks": "athlete_recalled",
    "events": "athlete_recalled",
    "machine_totals": "machine_reported",
    "heart_rate": "NOT STORED HERE -- records.heart_rate is authoritative and "
    "already in the corpus",
    "note": "blocks and events are RECALLED, not measured. machine_totals is "
    "the console readout. Neither is sensor-grade; they are different "
    "reliabilities and must not be pooled as one.",
}


def validate(payload: dict) -> list[str]:
    """Every problem at once. Pure over the payload: the duration bound needs
    the corpus and is checked in save_ride_blocks, never here."""
    problems = []
    allowed = {
        "run_id",
        "activity_id",
        "raw_text",
        "blocks",
        "events",
        "machine_totals",
    }
    unknown = set(payload) - allowed
    if unknown:
        problems.append(f"unknown field(s): {', '.join(sorted(unknown))}")
    if not str(payload.get("raw_text", "")).strip():
        problems.append(
            "raw_text is REQUIRED and must be the athlete's log verbatim. The "
            "parsed blocks are an interpretation of it; the text is the record."
        )
    for f in ("blocks", "raw_text"):
        if f not in payload:
            problems.append(f"{f} is required")
    for check, arg in (
        (derive_blocks, payload.get("blocks")),
        (_validate_events, payload.get("events")),
        (_validate_machine_totals, payload.get("machine_totals")),
    ):
        if arg is None and check is not derive_blocks:
            continue
        try:
            check(arg)
        except RideBlockError as exc:
            problems.append(str(exc))
    return problems


def save_ride_blocks(
    payload: dict, duration_min: float | None = None, supersede: bool = False
) -> dict:
    """Validate, bound against the activity, then write ATOMICALLY.

    `duration_min` is the activity's own timer time and comes from the caller
    (the CLI or the MCP tool resolve it from the corpus), so this module stays
    a pure state writer with no database of its own.
    """
    problems = validate(payload)
    if problems:
        raise RideBlockError("payload rejected:\n  - " + "\n  - ".join(problems))
    blocks = derive_blocks(payload["blocks"])
    last = blocks[-1]["end_min"]
    if duration_min is not None and last > duration_min + 1e-9:
        raise RideBlockError(
            f"the last block ends at {last:g} min but the activity is only "
            f"{duration_min:.2f} min long (timer time). A block cannot "
            f"describe minutes the ride does not contain; check the end_min "
            f"values against the recorded duration."
        )
    events = _validate_events(payload.get("events"))
    for e in events:
        if (
            e["minute"] is not None
            and duration_min is not None
            and e["minute"] > duration_min + 1e-9
        ):
            raise RideBlockError(
                f"event at minute {e['minute']:g} falls outside the "
                f"activity's {duration_min:.2f} min"
            )

    key = (
        payload["run_id"]
        if payload.get("run_id") is not None
        else payload.get("activity_id")
    )
    if not key:
        raise RideBlockError("run_id or activity_id is required")
    stored = load_ride_blocks()
    previous = stored.get(key)
    if previous is not None and not supersede:
        raise RideBlockError(
            f"{key} already has a ride log (revision "
            f"{previous.get('revision', 0)}, recorded "
            f"{previous.get('recorded_at')}). Pass supersede=true AS AN "
            f"ARGUMENT BESIDE the payload, not as a field inside it; the "
            f"existing entry is kept in the record."
        )

    ceiling, ceiling_source = resistance_max()
    entry = {
        "run_id": payload.get("run_id"),
        "activity_id": payload.get("activity_id"),
        # VERBATIM, byte for byte, through every revision.
        "raw_text": payload["raw_text"],
        "blocks": blocks,
        "events": events,
        "machine_totals": _validate_machine_totals(payload.get("machine_totals")),
        "provenance": dict(PROVENANCE),
        "resistance_scale": {
            "unit": "knob turns from closed",
            "observed_resolution": RESISTANCE_STEP_OBSERVED,
            "resolution_enforced": False,
            "max": ceiling,
            "max_source": ceiling_source,
            "max_defined_in": resistance_max_defined_in(),
        },
        "source": SOURCE,
        "schema_version": SCHEMA_VERSION,
        "recorded_at": (
            datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat()
        ),
    }
    revise(previous, entry)
    stored[key] = entry
    atomic_write(RIDE_BLOCKS, stored)
    return entry
