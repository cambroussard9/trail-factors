"""Per-block HR for a stationary ride, and the longitudinal cut (§18.1).

ONE JOIN, NOT A NEW ENGINE. The HR is already binned by `session_series`
(§17.1b); this walks those bins, assigns each to the block whose
[start_min, end_min) contains it, and summarises. No new binning, no new
weighting, no second implementation of anything.

THE POINT OF THE BUILD IS THE LONGITUDINAL CUT: for blocks matched on
resistance -- and on RPM range where both carry one -- heart rate over
CALENDAR TIME. Same resistance, same cadence, lower HR months later is a
strength-endurance signal.

EFFECTIVE n IS THE NUMBER OF BLOCKS, NEVER THE NUMBER OF HR RECORDS. A block
is one exposure to one resistance; its several hundred HR samples are one
observation of it, not several hundred. This project has made the opposite
mistake twice already -- §6.9's drift onset, where one bin's pairs were drawn
from only two passages, and §2.6's run-level weather fit, where the per-record
standard error was more than an order of magnitude too small -- and both are
recorded in CLAUDE.md as the same disease. Every figure here
reports n_blocks and n_sessions.

A BLOCK CONTAINING AN OFF-BIKE EVENT IS FLAGGED, NOT SILENTLY AVERAGED. If
the clock keeps running through a short off-bike break and resistance is not
changed for it, the break lands INSIDE a block and its HR is recovery, not
work at that resistance. The mean
is still reported -- excluding it would discard a real session -- but
`contains_event` travels with it and the longitudinal cut can drop it.
"""

from __future__ import annotations

from trail_factors.analysis.series import binned_series
from trail_factors.ingest.ride_blocks import load_ride_blocks

CALC_VERSION = 1


def _summarise(bins: list) -> dict:
    hrs = [b["avg_hr"] for b in bins if b["avg_hr"] is not None]
    if not hrs:
        return {
            "mean_hr": None,
            "min_hr": None,
            "max_hr": None,
            "hr_end_minus_start": None,
            "n_bins_with_hr": 0,
        }
    return {
        # The mean of BIN means, which are themselves time-weighted within the
        # bin. Bins are equal-width, so this is the time-weighted mean over the
        # block except at its partly-covered edges.
        "mean_hr": sum(hrs) / len(hrs),
        "min_hr": min(hrs),
        "max_hr": max(hrs),
        # END MINUS START, the quantity asked for: drift across the block.
        # NOT a fitted slope -- two points over a handful of bins is a
        # difference, and calling it a rate would invite reading it as one.
        "hr_end_minus_start": hrs[-1] - hrs[0],
        "n_bins_with_hr": len(hrs),
    }


def ride_blocks(con, activity_id: str) -> dict:
    """The stored blocks for one ride, each with its HR summary."""
    stored = load_ride_blocks()
    act = con.execute(
        """
        SELECT run_id, sport, sub_sport, timer_time_s
        FROM activities WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()
    if act is None:
        raise LookupError(f"no activity {activity_id}")
    run_id, sport, sub_sport, timer = act
    entry = stored.get(run_id) or stored.get(activity_id)
    if entry is None:
        # UNAVAILABLE WITH A REASON, never an empty list that reads as zero
        # blocks. Nothing has been logged is a different fact from a ride with
        # no structure in it.
        return {
            "status": "unavailable",
            "run_id": run_id,
            "activity_id": activity_id,
            "blocks": None,
            "reason": (
                f"no ride log has been saved for {run_id}. The "
                f"watch records HR only for these -- no cadence, no "
                f"resistance, and a constant-zero odometer -- so "
                f"the structure has to be supplied by the athlete "
                f"through save_ride_blocks. This is an absent "
                f"annotation, NOT a ride without blocks."
            ),
            "filters": _filters(sport, sub_sport, timer),
        }

    series = binned_series(con, activity_id)
    out = []
    for blk in entry["blocks"]:
        lo, hi = blk["start_min"] * 60.0, blk["end_min"] * 60.0
        mine = [
            b
            for b in series["bins"]
            if b["elapsed_start_s"] >= lo - 1e-9 and b["elapsed_start_s"] < hi - 1e-9
        ]
        inside = [
            e
            for e in entry.get("events", [])
            if e.get("minute") is not None
            and blk["start_min"] <= e["minute"] < blk["end_min"]
        ]
        out.append(
            {
                **blk,
                "duration_min": blk["end_min"] - blk["start_min"],
                **_summarise(mine),
                "n_bins": len(mine),
                # THE CONTAMINATION FLAG. See the module docstring.
                "contains_event": bool(inside),
                "events": inside,
                "hr_provenance": "records.heart_rate via session_series bins "
                "(measured); resistance and rpm are recalled",
            }
        )
    mt = entry.get("machine_totals") or {}
    rec = reconcile_rpm(entry["blocks"], mt.get("avg_rpm"))
    idx = load_index(entry["blocks"], rec)
    return {
        "status": "ok",
        "run_id": run_id,
        "activity_id": activity_id,
        "blocks": out,
        "n_blocks": len(out),
        # §18.2. Both are DERIVED and both say so; the matched-(resistance,
        # rpm) HR series above needs neither and stays the primary read.
        "rpm_reconciliation": rec,
        "load_index": idx,
        "raw_text": entry["raw_text"],
        "events": entry.get("events", []),
        "machine_totals": entry.get("machine_totals"),
        "provenance": entry["provenance"],
        "resistance_scale": entry.get("resistance_scale"),
        "revision": entry.get("revision", 0),
        "filters": _filters(sport, sub_sport, timer),
    }


def _rpm_key(rpm):
    return None if rpm is None else (rpm[0], rpm[1])


def resistance_history(
    con, resistance: float | None = None, match_rpm: bool = True
) -> dict:
    """HR at matched resistance over CALENDAR TIME — the longitudinal cut.

    Grouped by (resistance, rpm range) where both are present; blocks with no
    rpm group on resistance alone and are kept SEPARATE from blocks that have
    one, because "no stated cadence" is not a cadence that happens to match.
    """
    stored = load_ride_blocks()
    groups: dict = {}
    n_sessions = 0
    for _key, entry in sorted(stored.items()):
        aid = entry.get("activity_id")
        row = con.execute(
            "SELECT activity_id, start_time_utc FROM activities "
            "WHERE run_id = ? OR activity_id = ? LIMIT 1",
            [entry.get("run_id"), aid],
        ).fetchone()
        if row is None:
            continue
        n_sessions += 1
        detail = ride_blocks(con, row[0])
        if detail["status"] != "ok":
            continue
        for blk in detail["blocks"]:
            if resistance is not None and abs(blk["resistance"] - resistance) > 1e-9:
                continue
            gk = (blk["resistance"], _rpm_key(blk["rpm"]) if match_rpm else None)
            g = groups.setdefault(
                gk,
                {
                    "resistance": blk["resistance"],
                    "rpm": blk["rpm"] if match_rpm else None,
                    "blocks": [],
                },
            )
            g["blocks"].append(
                {
                    "run_id": detail["run_id"],
                    "date": str(row[1])[:10],
                    "start_min": blk["start_min"],
                    "end_min": blk["end_min"],
                    "duration_min": blk["duration_min"],
                    "mean_hr": blk["mean_hr"],
                    "hr_end_minus_start": blk["hr_end_minus_start"],
                    "contains_event": blk["contains_event"],
                }
            )

    out = []
    for _gk, g in sorted(groups.items(), key=lambda kv: (kv[1]["resistance"],)):
        pts = sorted(g["blocks"], key=lambda b: b["date"])
        n = len(pts)
        out.append(
            {
                "resistance": g["resistance"],
                "rpm": g["rpm"],
                "n_blocks": n,
                "n_sessions": len({p["run_id"] for p in pts}),
                "n_contaminated": sum(1 for p in pts if p["contains_event"]),
                "series": pts,
                # ONE BLOCK IS A READING, NOT A TREND, AND SAYS SO.
                "trend": None,
                "trend_status": (
                    "insufficient" if n < MIN_BLOCKS_FOR_TREND else "available"
                ),
                "trend_reason": (
                    f"{n} block(s) at this resistance. A trend needs at least "
                    f"{MIN_BLOCKS_FOR_TREND}, and the unit is the BLOCK, not the "
                    f"HR record -- a single block's several hundred samples are "
                    f"one observation of one exposure, not several hundred."
                    if n < MIN_BLOCKS_FOR_TREND
                    else f"{n} blocks across {len({p['run_id'] for p in pts})} "
                    f"sessions; read the series, the slope is not fitted here"
                ),
            }
        )
    return {
        "groups": out,
        "n_groups": len(out),
        "n_sessions": n_sessions,
        "matched_on": ["resistance", "rpm"] if match_rpm else ["resistance"],
        "filters": {
            "effective_n": "BLOCKS, never HR records",
            "effective_n_note": "a block is one exposure to one resistance; its HR "
            "samples "
            "are one observation of it. Quoting a per-record n here "
            "would be the pseudo-replication CLAUDE.md records twice "
            "(§6.9: one bin's pairs from two passages; §2.6: a per-record "
            "SE over an order of magnitude too small).",
            "rpm_matching": "exact stated range; a block with no rpm is "
            "grouped separately, never merged with one "
            "that has a range",
            "hr_source": "records.heart_rate via session_series bins",
            "resistance_source": "athlete-recalled, not measured",
            "contaminated_blocks_included": True,
            "contaminated_note": "blocks containing an off-bike event are "
            "INCLUDED and flagged; their HR carries "
            "recovery, not work at that resistance",
            "grain": "block",
        },
    }


MIN_BLOCKS_FOR_TREND = 3


def _filters(sport, sub_sport, timer) -> dict:
    return {
        "sport": sport,
        "sub_sport": sub_sport,
        "activity_timer_min": (timer if timer is not None else 0) / 60.0,
        "hr_source": "records.heart_rate via session_series 30 s bins",
        "watch_records": "HEART RATE ONLY. Measured over the indoor_cycling "
        "activities of the development corpus: no cadence, "
        "no speed, no position, no power, and distance_m is "
        "a constant 0.0 rather than null -- a fake zero, not "
        "a measurement.",
        "resistance_source": "athlete-recalled",
        "machine_totals_source": "machine-reported console readout",
        "derived_from_resistance": "NOTHING. Power, speed and distance are not "
        "estimated from resistance; that is out of "
        "scope by decision, not by omission.",
        "grain": "block",
    }


# --------------------------------------------------------------------------
# §18.2 TIME IN RESISTANCE, RPM RECONCILIATION, AND THE LOAD INDEX
#
# THE WORDS WATTS, POWER AND kJ DO NOT APPEAR IN ANY FIELD NAME OR ANY OUTPUT
# HERE, AND A TEST ENFORCES IT. Measured on the development rides, the
# console reports a constant distance per revolution -- it is a REV COUNTER
# with a fixed wheel constant, not a calibrated ergometer. There is no absolute
# calibration to
# recover, so a number in watts would be a fabrication wearing a unit. The
# index below is unitless and says so everywhere it appears.
# --------------------------------------------------------------------------

BAND_TURNS = 0.5  # the reporting band for time-in-resistance

# The load index's one assumption, stated wherever the index is returned.
LOAD_INDEX_ASSUMPTION = (
    "index = sum(resistance x reconciled_rpm x minutes). UNITLESS. It assumes "
    "resisting torque rises in proportion to knob turns, which is almost "
    "certainly wrong in detail -- a friction or magnetic brake is not linear "
    "in turns -- and is adequate for TREND on one machine whose settings are "
    "compared only against themselves. It is NOT a power estimate and cannot "
    "be converted into one: the console is a rev counter (a constant "
    "distance per revolution), so no absolute calibration exists."
)


def _band(resistance: float) -> float:
    """The band a resistance falls in, named by its LOWER edge.

    A BAND IS HALF-OPEN, [x, x + 0.5), and that is a deliberate change from
    rounding. `round()` is banker's rounding, so an exact half lands
    inconsistently -- 14.25 went DOWN to 14.0 while 14.75 went UP to 15.0 --
    and a quarter-turn value is an ordinary log entry. Half-open intervals are
    also what time-in-zone already does, and they read the way a rider
    describes a block: "N minutes between turn x and turn x-and-a-half".
    """
    import math

    return math.floor(resistance / BAND_TURNS) * BAND_TURNS


def reconcile_rpm(blocks: list, console_avg_rpm: float | None) -> dict:
    """Per-block rpm point estimates inside the stated ranges, summing to the
    console's whole-ride average.

    THE SOLUTION IS A ONE-PARAMETER FAMILY AND THE CHOICE IS DECLARED. Any set
    of per-block values inside their ranges that reproduces the total is an
    equally good answer, so this puts EVERY block at the SAME RELATIVE
    POSITION t within its own range: rpm_i = lo_i + t * (hi_i - lo_i). One
    unknown, one equation, one answer -- and no block is singled out to absorb
    the discrepancy.

    t ALSO MAKES THE LOAD INDEX INVARIANT TO BLOCK SPLITTING. Splitting a
    block into two with the same range leaves the weighted sums unchanged, so
    t is unchanged, so each piece gets the same rpm and the index is identical.
    A rule that shifted one block to absorb the residual would not have that
    property.

    NO SOLUTION IS REPORTED AS NO SOLUTION, NEVER CLAMPED. If the console
    total lies outside what the stated ranges can produce, the ranges and the
    total disagree and that is the finding -- clamping t into [0, 1] would
    manufacture agreement and hide a misremembered range or a misread console.
    """
    out = {"status": None, "t": None, "blocks": [], "console_avg_rpm": console_avg_rpm}
    if console_avg_rpm is None:
        out["status"] = "unavailable"
        out["reason"] = (
            "no console avg_rpm was logged for this ride; there "
            "is nothing to reconcile the stated ranges against"
        )
        return out
    usable = [b for b in blocks if b.get("rpm") is not None]
    if len(usable) != len(blocks):
        out["status"] = "unavailable"
        out["reason"] = (
            f"{len(blocks) - len(usable)} of {len(blocks)} blocks carry no rpm "
            f"range. The console average covers the WHOLE ride, so a block "
            f"with no stated range leaves the total unattributable -- any "
            f"value would satisfy it, which is not a reconciliation."
        )
        return out
    M = sum(b["end_min"] - b["start_min"] for b in usable)
    if M <= 0:
        out["status"] = "unavailable"
        out["reason"] = "the blocks span no time"
        return out
    L = sum(b["rpm"][0] * (b["end_min"] - b["start_min"]) for b in usable)
    D = sum(
        (b["rpm"][1] - b["rpm"][0]) * (b["end_min"] - b["start_min"]) for b in usable
    )
    lo_total, hi_total = L / M, (L + D) / M
    out["achievable_range"] = [lo_total, hi_total]
    if D == 0:
        if abs(lo_total - console_avg_rpm) > 1e-6:
            out["status"] = "no_solution"
            out["reason"] = (
                f"every block states a single rpm, so the ride average is "
                f"fixed at {lo_total:.2f}; the console reports "
                f"{console_avg_rpm:.2f}. These disagree and neither is "
                f"adjusted."
            )
            return out
        t = 0.0
    else:
        t = (console_avg_rpm * M - L) / D
    if not -1e-9 <= t <= 1 + 1e-9:
        out["status"] = "no_solution"
        out["reason"] = (
            f"the console average {console_avg_rpm:.2f} rpm lies outside what "
            f"the stated ranges can produce ({lo_total:.2f} to {hi_total:.2f} "
            f"rpm). NOT CLAMPED: a range was misremembered or the console was "
            f"misread, and forcing agreement would hide which."
        )
        return out
    t = min(1.0, max(0.0, t))
    out["status"] = "ok"
    out["t"] = t
    out["method"] = (
        "every block placed at the same relative position t within its own stated range"
    )
    for b in usable:
        lo, hi = b["rpm"]
        out["blocks"].append(
            {
                "start_min": b["start_min"],
                "end_min": b["end_min"],
                "resistance": b["resistance"],
                "rpm_range": [lo, hi],
                "reconciled_rpm": lo + t * (hi - lo),
                "rpm_provenance": "RECONCILED -- derived from the athlete's stated "
                "range and the console total, not observed",
            }
        )
    return out


def load_index(blocks: list, reconciliation: dict) -> dict:
    """Unitless load index per ride and per minute. NOT power. See the header."""
    if reconciliation.get("status") != "ok":
        return {
            "status": "unavailable",
            "index": None,
            "index_per_min": None,
            "reason": f"needs reconciled rpm: {reconciliation.get('reason')}",
            "assumption": LOAD_INDEX_ASSUMPTION,
        }
    total = 0.0
    minutes = 0.0
    per_block = []
    for b in reconciliation["blocks"]:
        m = b["end_min"] - b["start_min"]
        v = b["resistance"] * b["reconciled_rpm"] * m
        total += v
        minutes += m
        per_block.append(
            {
                "start_min": b["start_min"],
                "end_min": b["end_min"],
                "resistance": b["resistance"],
                "reconciled_rpm": b["reconciled_rpm"],
                "minutes": m,
                "index": v,
            }
        )
    return {
        "status": "ok",
        "index": total,
        "index_minutes": minutes,
        "index_per_min": (total / minutes) if minutes > 0 else None,
        "unit": "none -- this is an index, not a measurement",
        "per_block": per_block,
        "assumption": LOAD_INDEX_ASSUMPTION,
    }


def time_in_resistance(con, weekly: bool = True) -> dict:
    """Minutes per resistance value and per 0.5-turn band, per LOCAL ISO week.

    PURE AGGREGATION OF STORED BLOCKS -- the same shape as time-in-zones, and
    the only read here that needs no assumption at all beyond the athlete
    having logged the blocks correctly.

    LOCAL ISO WEEK, matching M7: a ride is grouped by the week it happened in
    where the athlete was standing, not by UTC.
    """
    import datetime as _dt
    from collections import defaultdict

    weeks: dict = defaultdict(
        lambda: {
            "by_resistance": defaultdict(float),
            "by_band": defaultdict(float),
            "rides": set(),
            "total_min": 0.0,
        }
    )
    overall_r: dict = defaultdict(float)
    overall_b: dict = defaultdict(float)
    for key, entry in sorted(load_ride_blocks().items()):
        row = con.execute(
            """
            SELECT activity_id, start_time_utc, local_tz FROM activities
            WHERE run_id = ? OR activity_id = ? LIMIT 1""",
            [entry.get("run_id"), entry.get("activity_id")],
        ).fetchone()
        if row is None:
            continue
        local = row[1] + _dt.timedelta(hours=row[2] if row[2] is not None else 0.0)
        iso = local.isocalendar()
        wk = f"{iso[0]}-W{iso[1]:02d}"
        w = weeks[wk]
        run_id = entry.get("run_id")
        w["rides"].add(run_id if run_id is not None else key)
        for b in entry["blocks"]:
            m = b["end_min"] - b["start_min"]
            w["by_resistance"][b["resistance"]] += m
            w["by_band"][_band(b["resistance"])] += m
            w["total_min"] += m
            overall_r[b["resistance"]] += m
            overall_b[_band(b["resistance"])] += m
    out = []
    for wk in sorted(weeks):
        w = weeks[wk]
        out.append(
            {
                "iso_week": wk,
                "n_rides": len(w["rides"]),
                "total_min": w["total_min"],
                "by_resistance": {
                    str(k): v for k, v in sorted(w["by_resistance"].items())
                },
                "by_band": {str(k): v for k, v in sorted(w["by_band"].items())},
            }
        )
    return {
        "weeks": out,
        "n_weeks": len(out),
        "by_resistance_all_time": {str(k): v for k, v in sorted(overall_r.items())},
        "by_band_all_time": {str(k): v for k, v in sorted(overall_b.items())},
        "band_turns": BAND_TURNS,
        "band_convention": "a band is named by its LOWER edge and is "
        "half-open: 14.0 means [14.0, 14.5)",
        "filters": {
            "source": "stored ride blocks (athlete-recalled), aggregated",
            "week": "LOCAL ISO week, matching M7's convention",
            "assumptions": "none beyond the blocks being logged correctly",
            "grain": "block",
        },
    }
