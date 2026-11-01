"""File-level validation. Runs BEFORE anything is written (SPEC.md §9).

A file that fails a FATAL check is rejected outright: no rows, no partial
ingest. Corrupt timestamps are the motivating case — a file carrying a gap of
several weeks between two consecutive records would leave elapsed_s in the
millions and silently poison every time-window
analysis downstream (pairing, matched-terrain drift, decoupling).

Note that `sampling_regime` detection does NOT catch this: it uses the median
gap, which is robust to outliers by design. Correct behaviour, but it means
regime detection cannot be relied on to surface timestamp corruption. Hence
these checks.
"""

from __future__ import annotations

import statistics
from datetime import UTC

from trail_factors.units import pace_per_mile

from .fit_file import semicircles_to_degrees

# Record span may exceed the session's own elapsed time a little (the session
# message is written after the last record), but not by much.
SPAN_FACTOR = 1.5
SPAN_TOLERANCE_S = 600.0

# No single gap inside one activity should approach an hour.
MAX_GAP_S = 3600.0

# Informational: a gap far above this file's own median is worth surfacing
# even when it is not fatal.
WARN_GAP_FACTOR = 20.0
WARN_GAP_MIN_S = 120.0


class ValidationError(Exception):
    """Raised for a FATAL problem. The file must not be ingested.

    IT CARRIES ITS FINDINGS, not only their joined text. A rejection is acted
    on by whoever reads it -- confirm a window, re-export, or leave it -- and
    which of those applies depends on WHICH check fired. Parsing that back out
    of a message string would make the wording of every message load-bearing.
    """

    def __init__(self, message: str = "", findings=()):
        super().__init__(message)
        self.findings = list(findings)

    @property
    def checks(self) -> list[str]:
        return [f.check for f in self.findings]


def check_timestamps(records: list[dict], session: dict) -> tuple[list[str], list[str]]:
    """Return (fatal, warnings). Records must already be coalesced and sorted."""
    fatal: list[str] = []
    warn: list[str] = []

    stamps = sorted(r["timestamp"] for r in records if r.get("timestamp"))
    if len(stamps) < 2:
        return ["fewer than 2 timestamped records"], warn

    span_s = (stamps[-1] - stamps[0]).total_seconds()
    reported = session.get("total_elapsed_time")
    if reported:
        limit = reported * SPAN_FACTOR + SPAN_TOLERANCE_S
        if span_s > limit:
            fatal.append(
                f"record span {span_s:,.0f} s exceeds session.total_elapsed_time "
                f"{reported:,.0f} s by more than {SPAN_FACTOR}x "
                f"(+{SPAN_TOLERANCE_S:.0f} s "
                f"tolerance); timestamps are corrupt"
            )

    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)]
    median_gap = statistics.median(gaps)
    biggest = max(gaps)
    if biggest > MAX_GAP_S:
        i = gaps.index(biggest)
        fatal.append(
            f"gap of {biggest:,.0f} s between consecutive records "
            f"({stamps[i]} -> {stamps[i + 1]}) exceeds {MAX_GAP_S:,.0f} s; "
            f"the file's median gap is {median_gap:.1f} s"
        )
    elif biggest > max(WARN_GAP_MIN_S, median_gap * WARN_GAP_FACTOR):
        warn.append(
            f"largest gap {biggest:,.0f} s is {biggest / median_gap:.0f}x the "
            f"median gap of {median_gap:.1f} s"
        )
    return fatal, warn


# ---------------------------------------------------------------------------
# Check registry (SPEC.md §3, §15.4)
#
# Every check here exists because we hit the bug it catches. Adding a check is
# adding a function and a @check decorator; the CLI report enumerates them.
#
# Severities
#   FATAL   - physically impossible. Reject the file; write nothing.
#   WARNING - suspicious. Ingest it, and record a flag §15.4 can exclude on.
#   INFO    - notable but fine. Recorded only.
#
# Layers ask different questions
#   BRONZE - parse fidelity.        Does the parsed data match the file?
#   SILVER - physical plausibility. Are the derived values possible?
#   GOLD   - historical plausibility. Does this run resemble the others?
# ---------------------------------------------------------------------------

FATAL, WARNING, INFO = "fatal", "warning", "info"
BRONZE, SILVER, GOLD = "bronze", "silver", "gold"

# Pace bounds for RUNNING. Generic human limits, not fitted to anyone.
PACE_FATAL_MPS = 1609.344 / 240  # 4:00/mi
PACE_WARN_MPS = 1609.344 / 300  # 5:00/mi
# A SUSTAINED impossibility means a unit error and is fatal; a single sample is
# a GPS glitch: a single re-fix sample far above 4:00/mi can sit in an
# otherwise usable run, and rejecting that file would be wrong.
PACE_SUSTAINED_S = 30.0

HR_MIN, HR_MAX = 25, 220
LAT_MAX, LON_MAX = 90.0, 180.0
COVERAGE_WARN = 0.99


class Finding:
    """One validation result, ready to be persisted."""

    def __init__(self, check, severity, layer, message, observed=None, expected=None):
        self.check, self.severity, self.layer = check, severity, layer
        self.message, self.observed, self.expected = message, observed, expected

    def __repr__(self):
        return f"<{self.severity} {self.check}: {self.message}>"

    @property
    def is_fatal(self):
        return self.severity == FATAL


def bronze_checks(records: list[dict], session: dict, messages: dict) -> list[Finding]:
    """Parse fidelity: does the parsed data match the file? (§3 bronze)"""
    out: list[Finding] = []

    # --- timestamps (the original checks, now emitting Findings) ---
    fatal, warn = check_timestamps(records, session)
    for msg in fatal:
        out.append(Finding("timestamp_integrity", FATAL, BRONZE, msg))
    for msg in warn:
        # A large gap used to print and vanish; it is now a stored finding.
        out.append(Finding("timestamp_gap", WARNING, BRONZE, msg))

    # --- coordinates in range: catches a broken semicircle conversion (§9) ---
    bad = 0
    for r in records:
        lat = semicircles_to_degrees(r.get("position_lat"))
        lon = semicircles_to_degrees(r.get("position_long"))
        if lat is None:
            continue
        if abs(lat) > LAT_MAX or abs(lon) > LON_MAX:
            bad += 1
    if bad:
        out.append(
            Finding(
                "coordinate_range",
                FATAL,
                BRONZE,
                f"{bad} records outside plausible degree range; semicircle "
                f"conversion is wrong (degrees = semicircles x 180 / 2^31)",
                observed=float(bad),
                expected=0.0,
            )
        )

    # --- duplicate timestamps within one activity (§9 Strava dedup) ---
    stamps = [r["timestamp"] for r in records if r.get("timestamp")]
    dupes = len(stamps) - len(set(stamps))
    if dupes:
        out.append(
            Finding(
                "duplicate_timestamps",
                WARNING,
                BRONZE,
                f"{dupes} duplicate timestamps after coalescing",
                observed=float(dupes),
                expected=0.0,
            )
        )

    # --- distance must not go backwards ---
    prev, back = None, 0
    for r in sorted(
        (r for r in records if r.get("timestamp")), key=lambda r: r["timestamp"]
    ):
        d = r.get("distance")
        if d is None:
            continue
        if prev is not None and d < prev - 0.01:
            back += 1
        prev = d
    if back:
        out.append(
            Finding(
                "distance_monotonic",
                FATAL,
                BRONZE,
                f"distance decreases at {back} points",
                observed=float(back),
                expected=0.0,
            )
        )

    # --- a non-null but constant column: catches cycle_length16 (§9) ---
    candidates = (
        "cycle_length16",
        "cadence",
        "heart_rate",
        "enhanced_speed",
        "enhanced_altitude",
        "distance",
        "gps_accuracy",
    )
    for col in candidates:
        vals = [r[col] for r in records if r.get(col) is not None]
        if len(vals) >= 50 and len(set(vals)) == 1:
            out.append(
                Finding(
                    "constant_column",
                    WARNING,
                    BRONZE,
                    f"'{col}' is non-null in {len(vals)} records and constant at "
                    f"{vals[0]!r}; it carries no information and must not be treated "
                    f"as a measurement",
                    observed=float(len(vals)),
                    expected=None,
                )
            )

    # --- record count vs duration and sampling regime ---
    median_gap = None
    if len(stamps) > 2:
        import statistics

        g = sorted(
            (b - a).total_seconds()
            for a, b in zip(sorted(stamps), sorted(stamps)[1:], strict=False)
        )
        median_gap = statistics.median(g)
        elapsed = session.get("total_elapsed_time")
        if elapsed and median_gap:
            expected = elapsed / median_gap
            ratio = len(stamps) / expected if expected else 0
            if ratio > 1.5 or ratio < 0.5:
                out.append(
                    Finding(
                        "record_count_plausible",
                        WARNING,
                        BRONZE,
                        f"{len(stamps)} records against ~{expected:,.0f} expected from "
                        f"{elapsed:,.0f} s at a {median_gap:.1f} s median gap "
                        f"(ratio {ratio:.2f}); un-coalesced records look like this",
                        observed=float(len(stamps)),
                        expected=float(expected),
                    )
                )

    # --- internal cross-message consistency: the same quantity, stated twice ---
    #
    # THE SUM IS ONLY MEANINGFUL OVER A PARTITION, and on the FR265 the splits
    # stopped being one. That watch writes an `interval_active` split spanning
    # the WHOLE RUN on top of the rwd_run / rwd_walk / rwd_stand partition, so
    # a naive sum double-counts -- roughly twice the session distance, "100%
    # apart", on a file with nothing wrong with it. The check would fire on
    # every FR265 run.
    #
    # So the SPLIT sum is taken over the run/walk/stand partition only, and any
    # other split type is counted and named rather than added. Laps are
    # unchanged: they are a partition by construction on both devices.
    for name, key in (("lap", "total_distance"), ("split", "total_distance")):
        msgs = messages.get(name, [])
        if name == "split":
            partition = [
                m for m in msgs if str(m.get("split_type", "")).startswith("rwd_")
            ]
            other = [m for m in msgs if m not in partition]
            msgs = partition
        else:
            other = []
        vals = [m.get(key) for m in msgs if m.get(key) is not None]
        sess = session.get("total_distance")
        if vals and sess:
            total = sum(vals)
            rel = abs(total - sess) / sess
            if rel > 0.02:
                extra = (
                    f"; {len(other)} non-partition split(s) "
                    f"({', '.join(sorted({str(m.get('split_type')) for m in other}))}) "
                    f"were excluded from the sum"
                    if other
                    else ""
                )
                out.append(
                    Finding(
                        f"{name}_distance_consistency",
                        WARNING,
                        BRONZE,
                        f"{name}s sum to {total:,.1f} m against session {sess:,.1f} m "
                        f"({rel * 100:.1f}% apart){extra}",
                        observed=total,
                        expected=sess,
                    )
                )
    return out


# Per-sample time weight in the pace check: a Smart Recording gap can be long,
# and one sample must not stand for more than this many seconds.
PACE_SAMPLE_DT_CAP_S = 20.0


def impossible_speed_blocks(ordered: list[dict]) -> list[dict]:
    """Maximal runs of consecutive samples faster than PACE_FATAL_MPS.

    `ordered` is time-sorted. Each block is {start_idx, end_idx, span_s,
    n_records}, where span_s sums each sample's forward interval (capped at
    PACE_SAMPLE_DT_CAP_S) -- the accumulation silver_checks has always used.
    A sample with NO speed neither extends nor breaks a block: absence is not
    evidence the vehicle stopped.
    """
    blocks, cur = [], None
    for i, r in enumerate(ordered):
        v = r.get("enhanced_speed")
        if v is None:
            continue
        dt = (
            (ordered[i + 1]["timestamp"] - r["timestamp"]).total_seconds()
            if i + 1 < len(ordered)
            else 0.0
        )
        dt = min(dt, PACE_SAMPLE_DT_CAP_S)
        if v > PACE_FATAL_MPS:
            if cur is None:
                cur = {"start_idx": i, "end_idx": i, "span_s": 0.0, "n_records": 0}
            cur["end_idx"] = i
            cur["span_s"] += dt
            cur["n_records"] += 1
        elif cur is not None:
            blocks.append(cur)
            cur = None
    if cur is not None:
        blocks.append(cur)
    return blocks


# HOW MUCH ON-FOOT MOVEMENT MAY SIT BEYOND A VEHICLE BLOCK and still leave it
# "the end of the recording". A walk from a parked car to a door is on foot
# and is not the run; five minutes of it is generous. A DECLARED editorial
# bound, not a fit -- and it only shapes a SUGGESTION, which is never applied
# without the athlete's confirmation. It decides what is PROPOSED, never what
# is recoverable: see the cuts in suggest_vehicle_window.
VEHICLE_EDGE_FOOT_MAX_S = 300.0


def _on_foot(rec) -> bool:
    """Steps and a speed between standing and the running bound."""
    v, cad = rec.get("enhanced_speed"), rec.get("cadence")
    return (
        v is not None
        and STOP_SPEED_MPS < v <= PACE_FATAL_MPS
        and cad is not None
        and cad > 0
    )


def suggest_vehicle_window(records: list[dict], sport: str | None = "running") -> dict:
    """A window that removes a LEADING or TRAILING vehicle block.

    THE GAP THIS CLOSES. `detect_movement_ceased` suggests a window for a
    STOPPED edge -- frozen distance, zero cadence, zero speed. A car ride is
    none of those: it moves fast, so it never reads as a stop, and the file
    it ends was rejected by pace_sustained_impossible with nothing to confirm.

    THE RULE. A sustained-impossible block (the same blocks, the same
    PACE_SUSTAINED_S, as the fatal check) is TRAILING when less than
    VEHICLE_EDGE_FOOT_MAX_S of on-foot movement follows its start; the
    suggested end is then the LAST ON-FOOT RECORD BEFORE IT -- walking back
    over the no-step records (getting in, pulling away) that precede the
    impossible speed itself. LEADING is the mirror image. A block with more
    than that much on-foot movement on both sides is MID-RUN: no window is
    PROPOSED for it, and the result counts it in `midrun_blocks` rather than
    suggesting a window that would discard half a run.

    THE 300 s BOUND DECIDES WHAT IS PROPOSED, NOT WHAT IS RECOVERABLE. A
    trailing drive followed by six minutes of walking from the car counts as
    mid-run here, yet `--end` before the drive removes it at the cost of that
    walk. So every file with a sustained block also gets `cut_end` (the last
    on-foot record before the FIRST block) and `cut_start` (the first on-foot
    record after the LAST block), each with the on-foot seconds it discards,
    and `cut` is the cheaper of the two. Either one removes every block. A
    cut is never proposed as junk removal: its cost is the reader's to weigh.

    Never applied here. Returns {"suggested_valid_start", "suggested_valid_end",
    "trailing_s", "leading_s", "midrun_blocks", "n_blocks",
    "n_on_foot_records", "has_cadence", "cut_end", "cut_start", "cut"}; a
    suggestion is a timestamp or None, a cut is {"bound", "at",
    "discards_on_foot_s"} or None (no on-foot record anchors it), and
    `n_on_foot_records` / `has_cadence` are None where there was no block to
    examine.
    """
    rs = sorted(
        (r for r in records if r.get("timestamp")), key=lambda r: r["timestamp"]
    )
    out = {
        "suggested_valid_start": None,
        "suggested_valid_end": None,
        "trailing_s": 0.0,
        "leading_s": 0.0,
        "midrun_blocks": 0,
        "n_blocks": 0,
        "n_on_foot_records": None,
        "has_cadence": None,
        "cut_end": None,
        "cut_start": None,
        "cut": None,
    }
    blocks = sustained_impossible_blocks(rs, sport)
    if not blocks:
        return out
    out["n_blocks"] = len(blocks)
    # WHY a detector may find nothing on foot, measured rather than assumed:
    # a file with no cadence channel (a phone recording) has no record that
    # can read as on foot, which is a different fact from one that has steps
    # and never moved at a running speed.
    out["has_cadence"] = any(r.get("cadence") is not None for r in rs)
    out["n_on_foot_records"] = sum(1 for r in rs if _on_foot(r))
    foot_dt = []
    for i, r in enumerate(rs):
        dt = (
            (rs[i + 1]["timestamp"] - r["timestamp"]).total_seconds()
            if i + 1 < len(rs)
            else 0.0
        )
        foot_dt.append(min(dt, MAX_SAMPLE_DT_S) if _on_foot(r) else 0.0)
    after = [0.0] * (len(rs) + 1)  # on-foot seconds from i onward
    for i in range(len(rs) - 1, -1, -1):
        after[i] = after[i + 1] + foot_dt[i]
    before_end = [0.0] * len(rs)  # on-foot seconds up to i
    acc = 0.0
    for i in range(len(rs)):
        acc += foot_dt[i]
        before_end[i] = acc

    trailing = next(
        (b for b in blocks if after[b["start_idx"]] < VEHICLE_EDGE_FOOT_MAX_S), None
    )
    leading = next(
        (
            b
            for b in reversed(blocks)
            if before_end[b["end_idx"]] < VEHICLE_EDGE_FOOT_MAX_S
        ),
        None,
    )
    if trailing is not None:
        k = trailing["start_idx"] - 1
        while k >= 0 and not _on_foot(rs[k]):
            k -= 1
        if k >= 0:
            out["suggested_valid_end"] = rs[k]["timestamp"]
            out["trailing_s"] = (
                rs[-1]["timestamp"] - rs[k]["timestamp"]
            ).total_seconds()
    if leading is not None and leading is not trailing:
        k = leading["end_idx"] + 1
        while k < len(rs) and not _on_foot(rs[k]):
            k += 1
        if k < len(rs):
            out["suggested_valid_start"] = rs[k]["timestamp"]
            out["leading_s"] = (rs[k]["timestamp"] - rs[0]["timestamp"]).total_seconds()
    edge = {id(b) for b in (trailing, leading) if b is not None}
    lo = leading["end_idx"] if leading is not None else -1
    hi = trailing["start_idx"] if trailing is not None else len(rs)
    out["midrun_blocks"] = sum(
        1
        for b in blocks
        if id(b) not in edge and lo < b["start_idx"] and b["end_idx"] < hi
    )

    # THE CUTS. after[k + 1] / before_end[k - 1] count only the on-foot
    # seconds BEYOND the kept record, so a clean trailing drive's cut_end is
    # the suggested end at a cost of 0 s.
    k = blocks[0]["start_idx"] - 1
    while k >= 0 and not _on_foot(rs[k]):
        k -= 1
    if k >= 0:
        out["cut_end"] = {
            "bound": "end",
            "at": rs[k]["timestamp"],
            "discards_on_foot_s": after[k + 1],
        }
    k = blocks[-1]["end_idx"] + 1
    while k < len(rs) and not _on_foot(rs[k]):
        k += 1
    if k < len(rs):
        out["cut_start"] = {
            "bound": "start",
            "at": rs[k]["timestamp"],
            "discards_on_foot_s": before_end[k - 1],
        }
    cuts = [c for c in (out["cut_end"], out["cut_start"]) if c is not None]
    if cuts:
        out["cut"] = min(cuts, key=lambda c: c["discards_on_foot_s"])
    return out


def sustained_impossible_blocks(
    records: list[dict], sport: str | None = "running"
) -> list[dict]:
    """The blocks pace_sustained_impossible rejects a file for, in time order.

    The one definition the fatal check, the window suggestion and the
    verification of a suggested window all share: a window that leaves one
    of these inside it does not clear the file. Empty for a sport the pace
    bound does not reach (PACE_BOUND_SPORTS).
    """
    rs = sorted(
        (r for r in records if r.get("timestamp")), key=lambda r: r["timestamp"]
    )
    if sport not in PACE_BOUND_SPORTS or len(rs) < 2:
        return []
    return [b for b in impossible_speed_blocks(rs) if b["span_s"] >= PACE_SUSTAINED_S]


# THE PACE BOUNDS ARE A RUNNING BOUND, AND ONLY RUNNING IS BOUND BY THEM.
#
# A running pace bound is a fact about RUNNING. Applied sport-blind it rejects
# legitimate bike rides at the door: a descent over 24 km/h (4:00/mi) is
# ordinary cycling, not a unit error. §4.1 is download broadly, model strictly: a FATAL
# check that refuses
# to write bronze rows is the door, and the door must not encode one sport's
# physiology.
#
# Non-running sports are NOT given a looser bound here -- they are given NO
# bound, because this project has no idea yet what is impossible on a bike.
# Inventing one would be a threshold fitted to nothing, and §4.1 says
# strictness belongs in the derived layers, which can be re-run.
PACE_BOUND_SPORTS = ("running",)


def silver_checks(
    records: list[dict], coverage: dict, sport: str | None = "running"
) -> list[Finding]:
    """Physical plausibility: are the derived values possible? (§3 silver)

    `sport` gates the pace bounds only. It defaults to "running" so an existing
    caller keeps its behaviour, and the ingest path passes the file's own
    session.sport.
    """
    out: list[Finding] = []
    ordered = sorted(
        (r for r in records if r.get("timestamp")), key=lambda r: r["timestamp"]
    )

    # --- pace bounds -------------------------------------------------------
    # A single impossible sample is a GPS glitch and must NOT reject the file;
    # a SUSTAINED impossibility -- a unit error, or the watch still recording
    # in a vehicle -- must. The blocks come from the same function the window
    # suggestion reads, so the check and the suggestion cannot disagree about
    # where the impossible stretch is.
    bound = sport in PACE_BOUND_SPORTS
    blocks = impossible_speed_blocks(ordered) if bound else []
    sustained_s = max((b["span_s"] for b in blocks), default=0.0)
    spikes = sum(b["n_records"] for b in blocks)
    warn_spikes = sum(
        1
        for r in (ordered if bound else [])
        if r.get("enhanced_speed") is not None and r["enhanced_speed"] > PACE_WARN_MPS
    )
    if sustained_s >= PACE_SUSTAINED_S:
        out.append(
            Finding(
                "pace_sustained_impossible",
                FATAL,
                SILVER,
                f"speed exceeded 4:00/mi for {sustained_s:.0f} s continuously; a "
                f"sustained impossibility is a vehicle (the watch still recording "
                f"after the run, or before it) or a unit error, not a GPS glitch",
                observed=sustained_s,
                expected=PACE_SUSTAINED_S,
            )
        )
    elif spikes:
        fastest = max(
            r["enhanced_speed"] for r in ordered if r.get("enhanced_speed") is not None
        )
        out.append(
            Finding(
                "pace_spike",
                WARNING,
                SILVER,
                f"{spikes} sample(s) faster than 4:00/mi, peak {fastest:.2f} m/s "
                f"({pace_per_mile(fastest, '--:--')}/mi) — GPS glitch, "
                f"exclude the sample",
                observed=fastest,
                expected=PACE_FATAL_MPS,
            )
        )
    elif warn_spikes:
        out.append(
            Finding(
                "pace_spike",
                WARNING,
                SILVER,
                f"{warn_spikes} sample(s) faster than 5:00/mi",
                observed=float(warn_spikes),
                expected=0.0,
            )
        )

    # --- heart rate bounds -------------------------------------------------
    hrs = [r["heart_rate"] for r in ordered if r.get("heart_rate") is not None]
    bad = [h for h in hrs if h < HR_MIN or h > HR_MAX]
    if bad:
        out.append(
            Finding(
                "heart_rate_range",
                WARNING,
                SILVER,
                f"{len(bad)} heart-rate samples outside {HR_MIN}-{HR_MAX} bpm "
                f"(min {min(bad)}, max {max(bad)})",
                observed=float(len(bad)),
                expected=0.0,
            )
        )

    out.extend(movement_checks(ordered))

    # --- GPS coverage: §15.4 cannot exclude what is not flagged ------------
    # THE MESSAGE NAMES WHAT IT GATED. It said "NOT usable for terrain-based
    # analysis" while the same report returned full terrain results, because
    # the warning gates NOTHING by itself: §14c is per-portion, so this run's
    # own terrain numbers come from its positioned records. The one gate that
    # reads coverage is select_runs' MIN_COVERAGE_FOR_TERRAIN, and it removes
    # the run from CROSS-RUN terrain comparisons only.
    frac = coverage.get("coverage_fraction")
    if frac is not None and frac < COVERAGE_WARN:
        from trail_factors.analysis.select import MIN_COVERAGE_FOR_TERRAIN as cross_min

        gated = (
            f"BELOW {cross_min:.0%}, so this run is EXCLUDED from cross-run "
            f"terrain comparisons (select_runs: matched_terrain_drift, "
            f"segment_effort_drift, efficiency_factor, grade_binned_pace and "
            f"the other terrain primitives over a selection)"
            if frac < cross_min
            else f"at or above {cross_min:.0%}, so NOTHING is gated: this run still "
            f"enters cross-run terrain comparisons"
        )
        out.append(
            Finding(
                "gps_coverage",
                WARNING,
                SILVER,
                f"only {frac * 100:.0f}% of records carry position"
                + (
                    ""
                    if coverage.get("contiguous")
                    else f" across {coverage.get('islands')} separate blocks"
                )
                + f"; {gated}. This run's OWN terrain results are computed from "
                f"its positioned portion (§14c) and are not gated by this "
                f"warning",
                observed=frac,
                expected=COVERAGE_WARN,
            )
        )
    if not coverage.get("contiguous") and frac:
        out.append(
            Finding(
                "gps_scattered",
                WARNING,
                SILVER,
                f"positioned records fall in {coverage.get('islands')} blocks, not one",
                observed=float(coverage.get("islands") or 0),
                expected=1.0,
            )
        )
    return out


def gold_checks(con) -> list[tuple]:
    """Historical plausibility: does this run resemble the others? (§3 gold)

    Runs over the whole corpus, so it must be a separate pass — inline it
    would depend on ingest order, since the first file has no history.
    Returns (file_sha256, activity_id, run_id, Finding) tuples.
    """
    out = []

    # --- a run must never hold two activities from the same source (§4) ----
    for run_id, source, n in con.execute("""
        SELECT run_id, source, count(*) FROM activities
        GROUP BY 1, 2 HAVING count(*) > 1
    """).fetchall():
        out.append(
            (
                None,
                None,
                run_id,
                Finding(
                    "run_source_uniqueness",
                    FATAL,
                    GOLD,
                    f"run {run_id} holds {n} activities from source '{source}'; "
                    "pairing "
                    f"is broken (§4: a run never takes two files from the same source)",
                    observed=float(n),
                    expected=1.0,
                ),
            )
        )

    # --- weather must be present on every OUTDOOR run (§8) -----------------
    #
    # Weather needs only a coordinate and a timestamp, both of which every
    # positioned run has, so a gap is a FETCH FAILURE and not an expected
    # absence -- unlike a segment match or a Garmin self-evaluation, which have
    # legitimate reasons to be missing. A silently absent value would drop the
    # run from every temperature-aware model without anything noticing.
    for aid, fh, rid in con.execute("""
        SELECT a.activity_id, a.file_hash, a.run_id
        FROM activities a
        WHERE a.coverage_fraction > 0
          AND NOT EXISTS (SELECT 1 FROM weather_records w
                          WHERE w.activity_id = a.activity_id)
    """).fetchall():
        out.append(
            (
                fh,
                aid,
                rid,
                Finding(
                    "weather_missing",
                    WARNING,
                    GOLD,
                    "outdoor run has no weather; §8 treats this as a fetch failure, "
                    "not a gap -- run `weather` to fetch it",
                    observed=0.0,
                    expected=1.0,
                ),
            )
        )

    # --- provisional weather awaiting archive backfill (§8) ----------------
    for aid, fh, rid, n in con.execute("""
        SELECT w.activity_id, a.file_hash, a.run_id, count(*)
        FROM weather_records w JOIN activities a USING (activity_id)
        WHERE NOT w.is_final GROUP BY 1, 2, 3
    """).fetchall():
        out.append(
            (
                fh,
                aid,
                rid,
                Finding(
                    "weather_provisional",
                    INFO,
                    GOLD,
                    "weather came from the forecast endpoint because the archive lags "
                    "several days; re-run `weather --refetch` once it catches up (§8)",
                    observed=float(n),
                ),
            )
        )

    # --- distance/duration against the corpus distribution ----------------
    rows = con.execute("""
        SELECT activity_id, file_hash, run_id, source, total_distance_m, duration_s
        FROM activities WHERE source = 'garmin_watch' AND total_distance_m > 0
    """).fetchall()
    if len(rows) >= 8:
        import statistics

        speeds = [r[4] / r[5] for r in rows if r[5]]
        med = statistics.median(speeds)
        # A fixed 2.5x factor could NEVER fire: one runner's ratios stay well inside 2x
        # of the median, so the check read as passing while testing nothing.
        # MAD is robust and scales to the actual spread.
        mad = statistics.median([abs(x - med) for x in speeds]) or 1e-9
        lo, hi = med - 5 * mad, med + 5 * mad
        for aid, fh, rid, _src, dist, dur in rows:
            if not dur:
                continue
            v = dist / dur
            if v > hi or v < lo:
                out.append(
                    (
                        fh,
                        aid,
                        rid,
                        Finding(
                            "mean_speed_vs_corpus",
                            WARNING,
                            GOLD,
                            f"mean speed {v:.2f} m/s is outside median +/- 5*MAD "
                            f"({lo:.2f}-{hi:.2f} m/s; median {med:.2f}, MAD {mad:.3f})",
                            observed=v,
                            expected=med,
                        ),
                    )
                )
    return out


def persist(con, file_sha256, activity_id, run_id, findings) -> None:
    """Idempotent: replace this file's rows rather than appending (§3)."""
    from datetime import datetime

    now = datetime.now(UTC).replace(tzinfo=None)
    con.execute("DELETE FROM validation_results WHERE file_sha256 = ?", [file_sha256])
    for f in findings:
        con.execute(
            "INSERT INTO validation_results VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                file_sha256,
                activity_id,
                run_id,
                f.check,
                f.severity,
                f.layer,
                f.message,
                f.observed,
                f.expected,
                now,
            ],
        )


def oracle_checks(activity_row: dict, api: dict) -> list[Finding]:
    """Compare our parsed values against Garmin's own reported ones.

    The oracle is the cached API listing, not a CSV export: it is regenerable
    (CLAUDE.md), carries fields the CSV lacks, and needs no manual step that
    could go stale. See data/raw/garmin_api/.
    """
    out: list[Finding] = []
    for label, ours, theirs, tol in (
        ("distance", activity_row.get("total_distance_m"), api.get("distance"), 0.01),
        # API `duration` is the FIT total_timer_time, NOT total_elapsed_time.
        # They coincide only on runs with no pauses (§9).
        ("duration", activity_row.get("timer_time_s"), api.get("duration"), 0.01),
    ):
        if ours is None or theirs is None or not theirs:
            continue
        rel = abs(ours - theirs) / theirs
        if rel > tol:
            out.append(
                Finding(
                    f"oracle_{label}",
                    WARNING,
                    GOLD,
                    f"our {label} {ours:,.2f} differs from Garmin's {theirs:,.2f} "
                    f"by {rel * 100:.2f}%",
                    observed=ours,
                    expected=theirs,
                )
            )
        else:
            out.append(
                Finding(
                    f"oracle_{label}",
                    INFO,
                    GOLD,
                    f"{label} matches Garmin's reported value ({theirs:,.2f})",
                    observed=ours,
                    expected=theirs,
                )
            )
    return out


def persist_gold(con, findings) -> None:
    """Replace all gold-layer rows. Gold is a corpus-wide pass (see §3)."""
    from datetime import datetime

    now = datetime.now(UTC).replace(tzinfo=None)
    con.execute("DELETE FROM validation_results WHERE layer = ?", [GOLD])
    for file_sha, aid, rid, f in findings:
        con.execute(
            "INSERT INTO validation_results VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                file_sha or "",
                aid,
                rid,
                f.check,
                f.severity,
                f.layer,
                f.message,
                f.observed,
                f.expected,
                now,
            ],
        )


# --- Movement-ceased detection (SPEC.md §4.3) -------------------------------
# The trigger is FROZEN DISTANCE + ZERO CADENCE + ZERO SPEED, sustained.
# NOT heart rate: across a real stop of a few minutes HR can fall only partway
# and then wobble back up, never approaching resting, so an HR-to-resting rule
# misses the real case. HR is corroboration, never the trigger.
# NOT impossible speed: that only catches driving, and an isolated
# impossible-speed sample is far more often a GPS re-fix artifact (§9).

STOP_SPEED_MPS = 0.20
# Rate-based, not absolute: a stationary GPS still drifts. A real stop of a
# few minutes can accumulate several metres of jitter, which a small absolute
# cap would miss.
# 0.10 m/s is an order of magnitude below walking (~1.3 m/s).
STOP_DRIFT_MPS = 0.10
STOP_MIN_S = 120.0  # shorter than this is a normal pause, not a stop


def _drift_ok(a, b, span):
    """Distance drift rate across a block, tolerant of a missing distance.

    NOT `(x or 0)`: a missing distance would become 0 and make the difference
    enormous, silently REJECTING a real stop (CLAUDE.md, the falsy trap). When
    distance is unavailable the cadence+speed conjunction already gates this,
    so an unmeasurable drift is accepted rather than treated as failure.
    """
    d0, d1 = a.get("distance"), b.get("distance")
    if d0 is None or d1 is None or not span:
        return True
    return abs(d1 - d0) / span <= STOP_DRIFT_MPS


def _moved(a, b):
    d0, d1 = a.get("distance"), b.get("distance")
    return abs(d1 - d0) if d0 is not None and d1 is not None else 0.0


def _is_stopped(rec):
    spd = rec.get("enhanced_speed")
    cad = rec.get("cadence")
    return (spd is not None and spd <= STOP_SPEED_MPS) and (
        cad is not None and cad == 0
    )


def detect_movement_ceased(records: list[dict]) -> dict:
    """Find leading, trailing and mid-run blocks where movement stopped."""
    rs = sorted(
        (r for r in records if r.get("timestamp")), key=lambda r: r["timestamp"]
    )
    out = {
        "trailing_s": 0.0,
        "leading_s": 0.0,
        "midrun_s": 0.0,
        "suggested_valid_end": None,
        "suggested_valid_start": None,
        "trailing_distance_m": 0.0,
    }
    if len(rs) < 10:
        return out
    flags = [_is_stopped(r) for r in rs]

    # trailing: maximal suffix of stopped samples
    i = len(rs) - 1
    while i >= 0 and flags[i]:
        i -= 1
    if i < len(rs) - 2:
        first_stop = rs[i + 1]
        span = (rs[-1]["timestamp"] - first_stop["timestamp"]).total_seconds()
        moved = _moved(first_stop, rs[-1])
        if span >= STOP_MIN_S and _drift_ok(first_stop, rs[-1], span):
            out["trailing_s"] = span
            out["trailing_distance_m"] = moved
            out["suggested_valid_end"] = rs[i]["timestamp"]

    # leading: maximal prefix
    j = 0
    while j < len(rs) and flags[j]:
        j += 1
    if j > 1:
        span = (rs[j - 1]["timestamp"] - rs[0]["timestamp"]).total_seconds()
        if span >= STOP_MIN_S and _drift_ok(rs[0], rs[j - 1], span):
            out["leading_s"] = span
            out["suggested_valid_start"] = rs[j]["timestamp"]

    # mid-run blocks: informational only. total_timer_time already excludes
    # auto-paused time, so a mid-run stop does NOT corrupt headline metrics.
    longest, run_start = 0.0, None
    for k, f in enumerate(flags):
        if f and run_start is None:
            run_start = k
        elif not f and run_start is not None:
            if run_start > 0 and k < len(rs) - 1:
                longest = max(
                    longest,
                    (
                        rs[k - 1]["timestamp"] - rs[run_start]["timestamp"]
                    ).total_seconds(),
                )
            run_start = None
    out["midrun_s"] = longest
    return out


def movement_checks(records: list[dict]) -> list[Finding]:
    """WARNING only, never FATAL, and never auto-truncating (§4.3)."""
    m = detect_movement_ceased(records)
    out: list[Finding] = []
    if m["trailing_s"]:
        out.append(
            Finding(
                "trailing_stop",
                WARNING,
                SILVER,
                f"movement ceased {m['trailing_s']:.0f} s before the recording ended "
                f"(distance moved {m['trailing_distance_m']:.1f} m); suggested "
                f"valid_end_utc {m['suggested_valid_end']}. "
                f"NOT applied — confirm first",
                observed=m["trailing_s"],
                expected=0.0,
            )
        )
    if m["leading_s"]:
        out.append(
            Finding(
                "leading_stop",
                WARNING,
                SILVER,
                f"no movement for the first {m['leading_s']:.0f} s; suggested "
                f"valid_start_utc {m['suggested_valid_start']}. NOT applied — confirm "
                "first",
                observed=m["leading_s"],
                expected=0.0,
            )
        )
    if m["midrun_s"] >= STOP_MIN_S:
        out.append(
            Finding(
                "midrun_stop",
                INFO,
                SILVER,
                f"longest mid-run stop {m['midrun_s']:.0f} s; headline metrics are "
                f"NOT corrupted — total_timer_time already excludes paused time",
                observed=m["midrun_s"],
                expected=None,
            )
        )
    return out


# --- Stops as structure (SPEC.md §4.3, §6.3) --------------------------------
# A stop DURING a run is training data and a free HR recovery test. A stop
# AFTER a run is junk. Both are recorded; only trailing/leading junk narrows
# the valid window.

MAX_SAMPLE_DT_S = 20.0


def detect_stops(records: list[dict], min_s: float = STOP_MIN_S) -> list[dict]:
    """Every sustained stop, classified leading / midrun / trailing."""
    rs = sorted(
        (r for r in records if r.get("timestamp")), key=lambda r: r["timestamp"]
    )
    if len(rs) < 10:
        return []
    flags = [_is_stopped(r) for r in rs]
    out, i = [], 0
    while i < len(rs):
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(rs) and flags[j + 1]:
            j += 1
        span = (rs[j]["timestamp"] - rs[i]["timestamp"]).total_seconds()
        if span >= min_s and _drift_ok(rs[i], rs[j], span):
            block = rs[i : j + 1]
            hrs = [r["heart_rate"] for r in block if r.get("heart_rate") is not None]
            resume = rs[j + 1].get("heart_rate") if j + 1 < len(rs) else None
            pos = (
                "leading" if i == 0 else ("trailing" if j == len(rs) - 1 else "midrun")
            )
            lat = next(
                (
                    r.get("position_lat")
                    for r in reversed(block)
                    if r.get("position_lat") is not None
                ),
                None,
            )
            lon = next(
                (
                    r.get("position_long")
                    for r in reversed(block)
                    if r.get("position_long") is not None
                ),
                None,
            )
            out.append(
                {
                    "start_utc": rs[i]["timestamp"],
                    "end_utc": rs[j]["timestamp"],
                    "duration_s": span,
                    "position": pos,
                    "hr_at_stop": hrs[0] if hrs else None,
                    "hr_at_resume": resume,
                    "hr_min": min(hrs) if hrs else None,
                    "hr_drop": (hrs[0] - min(hrs)) if hrs else None,
                    "lat": semicircles_to_degrees(lat) if lat is not None else None,
                    "lon": semicircles_to_degrees(lon) if lon is not None else None,
                }
            )
        i = j + 1
    return out


def moving_time_s(records: list[dict]) -> tuple[float, float]:
    """(moving_s, stopped_s). Summed over samples, so auto-paused gaps — which
    contain no records — are excluded from both, matching Garmin."""
    rs = sorted(
        (r for r in records if r.get("timestamp")), key=lambda r: r["timestamp"]
    )
    mov = stop = 0.0
    for i in range(len(rs) - 1):
        dt = min(
            (rs[i + 1]["timestamp"] - rs[i]["timestamp"]).total_seconds(),
            MAX_SAMPLE_DT_S,
        )
        if _is_stopped(rs[i]):
            stop += dt
        else:
            mov += dt
    return mov, stop
