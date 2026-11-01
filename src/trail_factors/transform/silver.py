"""Silver layer: derived, rebuildable, per-record (SPEC.md §3, §12.2).

Grade uses ONE window (25 m) because grade is a local physical quantity acting
through Minetti's cost-of-gradient curve at the scale a runner's mechanics
respond to — tens of metres (§9). Cumulative gain is NOT computed here: it is
a reporting choice with no true value (§5a-bis) and belongs at gold, with its
window stated in the output.

Outside a terrain-usable block, grade and GAP are NULL rather than flagged
numbers. A flagged number gets used by a query that forgets to check the flag;
NULL fails loudly (§12.2).
"""

from __future__ import annotations

import numpy as np

from trail_factors.transform.dem import project

CALC_VERSION = 1

GRADE_WINDOW_M = 25.0  # §9: the scale grade acts on, not a preference
GRADE_HALF_M = GRADE_WINDOW_M / 2
ELEV_SMOOTH_M = 25.0  # rolling median over DISTANCE, not samples
BLOCK_MAX_GAP_S = 30.0  # §14c positioned-block definition
BLOCK_MIN_SPAN_S = 60.0
BLOCK_MIN_DIST_M = 200.0
PROMINENCE_MIN_M = 8.0  # §5.0; stable across 0-50 m smoothing here


def path_distance(lats, lons, crs: str | None = None) -> np.ndarray:
    """Cumulative along-path distance in metres, and the projected (x, y).

    In `crs` when given (the run's resolved projection, dem.run_projection);
    otherwise the configured `projection_crs`, else the UTM zone derived from
    these positions -- which are a whole run here, so deriving from them is
    deriving from the run (dem.project).
    """
    x, y = project(lats, lons, crs)
    return np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))]), x, y


def smooth_by_distance(values, cum, window_m=ELEV_SMOOTH_M) -> np.ndarray:
    """Rolling median over a DISTANCE window.

    NOT a sample count: at ~7.5 m spacing a '25-sample' window is ~190 m of
    path, and mislabelling the two produced wrong figures once already
    (§5a-bis).
    """
    out = np.empty(len(values), dtype=float)
    for i in range(len(values)):
        lo = np.searchsorted(cum, cum[i] - window_m / 2)
        hi = np.searchsorted(cum, cum[i] + window_m / 2)
        out[i] = np.median(values[lo : max(hi, lo + 1)])
    return out


def positioned_blocks(timestamps, cum) -> list[tuple[int, int, bool]]:
    """(start, end, usable) per §14c: consecutive fixes <=30 s apart, usable
    when the block spans >=60 s and >=200 m."""
    blocks, start = [], 0
    for i in range(1, len(timestamps) + 1):
        gap = (
            (timestamps[i] - timestamps[i - 1]).total_seconds()
            if i < len(timestamps)
            else float("inf")
        )
        if gap > BLOCK_MAX_GAP_S:
            span = (timestamps[i - 1] - timestamps[start]).total_seconds()
            dist = cum[i - 1] - cum[start]
            blocks.append(
                (start, i - 1, span >= BLOCK_MIN_SPAN_S and dist >= BLOCK_MIN_DIST_M)
            )
            start = i
    return blocks


def grade_series(elev_smooth, cum, blocks) -> np.ndarray:
    """Signed grade in percent, NaN where it cannot be trusted.

    A record needs its FULL +/-12.5 m window inside a usable block, else the
    first and last records of every block carry a half-computed grade. Costs
    about 1.0% of records (measured).
    """
    grade = np.full(len(cum), np.nan)
    for a, b, usable in blocks:
        if not usable:
            continue
        for i in range(a, b + 1):
            if cum[i] - cum[a] < GRADE_HALF_M or cum[b] - cum[i] < GRADE_HALF_M:
                continue
            lo = np.searchsorted(cum, cum[i] - GRADE_HALF_M)
            hi = min(np.searchsorted(cum, cum[i] + GRADE_HALF_M), b)
            run = cum[hi] - cum[lo]
            if run > 5.0:
                grade[i] = 100.0 * (elev_smooth[hi] - elev_smooth[lo]) / run
    return grade


def minetti_cost(gradient: float) -> float:
    """Metabolic cost of running on a gradient, J/kg/m (Minetti 2002, §6.1).

    The population curve. §6.1 calls for refitting it to this athlete's own
    HR-vs-grade data; until then this is explicitly not personal.
    """
    i = max(-0.45, min(0.45, gradient))
    return 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6


_FLAT_COST = minetti_cost(0.0)


def gap_series(speed_mps, grade_pct) -> np.ndarray:
    """Grade-adjusted pace. NaN wherever grade is NaN — GAP inherits the
    trustworthiness of the grade it is built on."""
    out = np.full(len(speed_mps), np.nan)
    for i, (v, g) in enumerate(zip(speed_mps, grade_pct, strict=False)):
        if v is None or np.isnan(g):
            continue
        out[i] = v * minetti_cost(g / 100.0) / _FLAT_COST
    return out


def prominences(elev) -> list[tuple[int, float, float]]:
    """1-D topographic prominence: (index, elevation, prominence).

    Prominence is a property of TERRAIN rather than of a traversal (§5.0): a
    hill has the same prominence run uphill, downhill, fast or walked.
    """
    out = []
    n = len(elev)
    for i in range(1, n - 1):
        if not (elev[i] >= elev[i - 1] and elev[i] > elev[i + 1]):
            continue
        j = i
        while j > 0 and elev[j - 1] <= elev[i]:
            j -= 1
        left = elev[j : i + 1].min() if j < i else elev[i]
        k = i
        while k < n - 1 and elev[k + 1] <= elev[i]:
            k += 1
        right = elev[i : k + 1].min() if k > i else elev[i]
        out.append((i, float(elev[i]), float(elev[i] - max(left, right))))
    return out


def detect_hills(elev_smooth, cum, min_prominence=PROMINENCE_MIN_M) -> list[dict]:
    """Hill traversals for one run. Matching across runs is milestone 5."""
    hills = []
    for i, top, prom in prominences(elev_smooth):
        if prom < min_prominence:
            continue
        j = i
        while (
            j > 0
            and elev_smooth[j - 1] <= elev_smooth[i]
            and top - elev_smooth[j] < prom
        ):
            j -= 1
        k = i
        while (
            k < len(elev_smooth) - 1
            and elev_smooth[k + 1] <= elev_smooth[i]
            and top - elev_smooth[k] < prom
        ):
            k += 1
        hills.append(
            {
                "peak_index": i,
                "peak_distance_m": float(cum[i]),
                "peak_elevation_m": top,
                "prominence_m": prom,
                "start_index": j,
                "end_index": k,
                "length_m": float(cum[k] - cum[j]),
            }
        )

    # DOUBLE-TOP DEDUP. A broad summit with two local maxima of equal height
    # emits one entry per maximum, and the prominence walk finds the SAME key
    # col for both -- so they come out with identical (start_index, end_index)
    # and identical prominence over one stretch of trail. That is one hill the
    # runner crossed once, not two crossings.
    #
    # Found while building milestone 5: a few percent of traversals were
    # duplicates, inflating segment effort counts. Distinct
    # extents are kept, so a genuine out-and-back re-crossing -- different
    # stretch of path, same place -- is untouched; the milestone-5 matcher is
    # what recognises those as the same segment.
    #
    # Keep the highest peak; tie breaks on the lower index so the result is
    # deterministic.
    best: dict[tuple[int, int], dict] = {}
    for h in hills:
        key = (h["start_index"], h["end_index"])
        prev = best.get(key)
        if prev is None or h["peak_elevation_m"] > prev["peak_elevation_m"]:
            best[key] = h
    return sorted(best.values(), key=lambda h: h["peak_index"])


# --- Milestone 6: the segment unit is a SLOPE, not a prominence hill --------
#
# A prominence hill is up-then-down BY DEFINITION -- a peak with a col either
# side. On the first trail this was developed on that is the real terrain,
# which is why prominence found clean hills there: measured on that trail,
# under a fifth of a hill's span is flat and the longest flat gap anywhere is
# 620 m. But that is a property of ONE TRAIL, not of hills. A long climb can
# top out onto a plateau -- no col. On a point-to-point course, ascents and
# descents can be separated by MILES of flat, and gluing them into one feature
# would attribute terrain the athlete never climbed.
#
# THE UNIT IS A SLOPE: one monotonic stretch of terrain, keyed on its ENDPOINTS
# and traversable either way. Running it up and running it down are two EFFORTS
# on ONE segment -- which is what makes the §6.3 tau comparison like-for-like:
# recovery after climbing slope X against recovery after descending slope X,
# same ground.

SLOPE_ENTER_PCT = 3.0  # §5 hysteresis: enter a sustained stretch
SLOPE_EXIT_PCT = 1.0  # §5 hysteresis: leave it. Reused as the floor
# on a MERGED slope's mean grade -- see the
# merge veto in detect_slopes.
SLOPE_MIN_VERTICAL_M = PROMINENCE_MIN_M  # significance, REUSED not re-picked

# An interruption is absorbed only if it is BOTH short and insignificant.
#
# FITTED FROM BELOW, measured over 100 prominence ascents: the
# longest non-climbing stretch inside a stretch that is unambiguously ONE
# ascent is 310.9 m (p95 186 m, p99 294 m). 400 m clears that by 29%.
#
# NOT fitted from above -- the development trail cannot do it. Two attempts
# failed and both failures are the same fact: the gap-length distribution is
# unimodal from 13 m to 620 m with no natural break, and HR barely changes
# across a gap, with no trend against its length (§6.2: HR is flat across
# grade). The upper bound comes from the requirement instead: a 3 km flat must
# split, and point-to-point separations are miles. 400 m sits 7.5x under the
# first and 4x under a mile.
#
# THE FIRST REAL VALIDATION OF THIS BOUNDARY IS THE FIRST RUN ON TERRAIN WITH
# REAL FLATS BETWEEN FEATURES (§5). Until then the synthetic tests are the
# only test and the development trail is a regression, not a validation.
BENCH_MAX_M = 400.0


def profile_grade(elev, cum, window_m=GRADE_WINDOW_M) -> np.ndarray:
    """Centred grade over a DISTANCE window, with no block gating.

    grade_series() is the production path and NaNs anything outside a usable
    positioned block. This is the ungated form, for synthetic profiles and as
    a fallback where block structure is not meaningful.
    """
    elev = np.asarray(elev, dtype=float)
    cum = np.asarray(cum, dtype=float)
    out = np.full(len(elev), np.nan)
    half = window_m / 2
    for i in range(len(elev)):
        lo = np.searchsorted(cum, cum[i] - half)
        hi = min(np.searchsorted(cum, cum[i] + half), len(elev) - 1)
        span = cum[hi] - cum[lo]
        if span > 0:
            out[i] = 100.0 * (elev[hi] - elev[lo]) / span
    return out


def _state_runs(state) -> list[list]:
    """Maximal runs of equal state, as [start, end, state]."""
    runs, i = [], 0
    while i < len(state):
        j = i
        while j + 1 < len(state) and state[j + 1] == state[i]:
            j += 1
        runs.append([i, j, int(state[i])])
        i = j + 1
    return runs


def detect_slopes(
    elev, cum, grade=None, bench_max_m=BENCH_MAX_M, min_vertical_m=SLOPE_MIN_VERTICAL_M
) -> list[dict]:
    """Monotonic slopes along one run. Returns direction-normalised endpoints.

    A SINGLE-PASS THREE-STATE PARTITION. Every sample is ascent, descent or
    flat, so a flat belongs to NEITHER neighbour and ascents cannot overlap
    descents -- both guaranteed structurally rather than policed by a test.
    Running ascent and descent detection as two independent passes was tried
    first and produced 119 overlapping pairs on this corpus, including a
    descent that swallowed two entire climbs.

    Absorption iterates TO A FIXED POINT: one pass left the reference run at
    33 ascents and 33 descents, where iterating gives 8 and 7.
    """
    elev = np.asarray(elev, dtype=float)
    cum = np.asarray(cum, dtype=float)
    if grade is None:
        grade = profile_grade(elev, cum)
    grade = np.asarray(grade, dtype=float)

    state = np.where(
        np.isnan(grade),
        0,
        np.where(
            grade >= SLOPE_ENTER_PCT, 1, np.where(grade <= -SLOPE_ENTER_PCT, -1, 0)
        ),
    )
    runs = _state_runs(state)

    # An interruption is the WHOLE span between two runs of the same state, not
    # just the single run between them. A 25 m grade window puts a neutral
    # transition zone on each side of a dip, so a 5 m dip inside a climb reads
    # as ascent / flat / descent / flat / ascent -- five runs. Looking only at
    # immediate triples never merged it.
    changed = True
    while changed:
        changed = False
        for k in range(len(runs) - 1):
            if runs[k][2] == 0:
                continue
            nxt = next(
                (j for j in range(k + 1, len(runs)) if runs[j][2] == runs[k][2]), None
            )
            if nxt is None or nxt == k + 1:
                continue
            a, b = runs[k][1], runs[nxt][0]
            gap = cum[b] - cum[a]
            if gap > bench_max_m:
                continue
            inner = elev[a : b + 1]
            # How far the profile moves AGAINST the enclosing direction.
            # Reusing the prominence threshold: a reversal smaller than a
            # significant feature is texture, not a boundary.
            rev = elev[a] - inner.min() if runs[k][2] > 0 else inner.max() - elev[a]
            if rev >= min_vertical_m:
                continue
            # MERGE VETO. Absorption iterates, so several benches can chain and
            # produce a "sustained ascent" that is mostly flat -- the reference
            # run had a 1,400 m ascent averaging +0.9%, which is terrain
            # drifting upward, not a climb. Refuse any merge whose RESULT falls
            # below the §5 hysteresis exit grade. Vetoing the merge keeps the
            # component slopes rather than discarding the vertical.
            m0, m1 = runs[k][0], runs[nxt][1]
            span = cum[m1] - cum[m0]
            if span <= 0 or abs(100.0 * (elev[m1] - elev[m0]) / span) < SLOPE_EXIT_PCT:
                continue
            runs[k] = [runs[k][0], runs[nxt][1], runs[k][2]]
            del runs[k + 1 : nxt + 1]
            changed = True
            break

    # SNAP ENDPOINTS TO LOCAL EXTREMA.
    #
    # A hysteresis boundary is where a THRESHOLD was crossed, which is not a
    # feature of the terrain and is not symmetric: climbing a face you enter
    # the sustained state at a different point than you leave it descending the
    # same face. Measured before this step, only about a tenth of segments
    # carried both directions -- low endpoints agreed to 4-30 m but high endpoints
    # differed
    # by 25-70 m, and ascents ran systematically longer than the matching
    # descents. That defeats the whole point of keying a slope on its
    # endpoints (§5): the ascent and descent of one face must be ONE segment.
    #
    # Walking out to the bounding local minimum and maximum gives endpoints
    # that belong to the ground, so both directions converge on the same pair.
    # The walk stops as soon as the profile stops improving -- on a flat that
    # is immediately, so this cannot reach across the flats that §12.4 exists
    # to exclude -- and is capped at bench_max_m regardless.
    def walk(i, step, better):
        limit = cum[i] + step * bench_max_m
        j = i
        while 0 <= j + step < len(elev) and better(elev[j + step], elev[j]):
            if (cum[j + step] - limit) * step > 0:
                break
            j += step
        return j

    snapped = []
    for a, b, s in runs:
        if s != 0:
            # STRICT improvement. With >= the walk crosses a perfectly level
            # stretch, which would drag the endpoints out across exactly the
            # flats this design excludes.
            if s > 0:
                a = walk(a, -1, lambda nxt, cur: nxt < cur)
                b = walk(b, +1, lambda nxt, cur: nxt > cur)
            else:
                a = walk(a, -1, lambda nxt, cur: nxt > cur)
                b = walk(b, +1, lambda nxt, cur: nxt < cur)
        snapped.append([a, b, s])
    # Snapping can push two neighbours onto a shared extremum; clip so the
    # partition property survives.
    for k in range(1, len(snapped)):
        if snapped[k][0] < snapped[k - 1][1]:
            mid = (snapped[k - 1][1] + snapped[k][0]) // 2
            snapped[k - 1][1], snapped[k][0] = mid, mid
    runs = snapped

    slopes = []
    for a, b, s in runs:
        if s == 0 or b <= a:
            continue
        vertical = abs(elev[b] - elev[a])
        if vertical < min_vertical_m:
            continue
        length = cum[b] - cum[a]
        # ENDPOINTS ARE DIRECTION-NORMALISED: low end and high end, not start
        # and end. That is what lets one slope carry both an ascent effort and
        # a descent effort (§5, amended).
        lo_i, hi_i = (a, b) if elev[a] <= elev[b] else (b, a)
        slopes.append(
            {
                "start_index": a,
                "end_index": b,
                "low_index": lo_i,
                "high_index": hi_i,
                "direction": "ascent" if s > 0 else "descent",
                "start_distance_m": float(cum[a]),
                "end_distance_m": float(cum[b]),
                "low_elevation_m": float(elev[lo_i]),
                "high_elevation_m": float(elev[hi_i]),
                "vertical_m": float(vertical),
                "length_m": float(length),
                "mean_grade_pct": (
                    100.0 * (elev[b] - elev[a]) / length if length > 0 else 0.0
                ),
            }
        )
    return slopes


# --- §15.4 per-record quality flags ----------------------------------------
#
# §15.4 requires flagged measurements be EXCLUDED, not down-weighted, and names
# `records_enriched.quality_flags` as the carrier. The column did not exist, so
# every report had to state that per-record exclusion was NOT ENFORCED. This is
# that carrier.
#
# EACH SIGNAL IS A NAMED FLAG, NEVER A COMPOSITE SCORE. A score would let a
# reader treat 0.4 as "a bit unreliable" and use it anyway; a named flag forces
# the exclusion decision §15.4 demands, and says WHICH artifact was found.
#
# EVERY THRESHOLD BELOW IS DERIVED FROM THE MEASURED DISTRIBUTION of the
# development corpus, not assumed. §15.4 carries the tables.

# JITTER IS NORMALISED BY POINT SPACING, and that is the whole trick. Raw
# perpendicular deviation is not comparable across sampling regimes -- p99 is
# 3.11 m at 1 Hz against 13.58 m at Smart Recording, purely because points are
# ~4x further apart. Divided by the run's median step length the two agree:
# p99 1.63 against 1.52. A raw threshold would flag most Smart Recording data
# and almost no 1 Hz data, which is a regime artifact, not a quality signal.
#
# 2.0 sits just above p99 in both regimes and flags 0.34% (1 Hz) / 0.82%
# (Smart) -- outliers, not a tail.
JITTER_NORM_MAX = 2.0

# PHYSICALLY ANCHORED, not a percentile: 3 m/s^2 is 0 to 10.8 km/h in one
# second, which a trail runner cannot do. NOTHING in the 1 Hz corpus exceeds it
# (observed max 2.5); 0.045% of Smart Recording records do, and that file's max
# of 7.1 m/s^2 is exactly the kind of jump this exists for.
ACCEL_MAX_MPS2 = 3.0

# Cadence reading 0 while clearly moving. Measured 1.46% (1 Hz) / 0.50%
# (Smart); §14a found about 1% on the reference run, mostly on climbs.
CADENCE_DROPOUT_SPEED_MPS = 1.5

# Cadence frozen at one value while speed changes. Measured runs of identical
# non-zero cadence with speed varying >0.3 m/s: median 6 s, p95 14 s, max 20 s
# at 1 Hz. 15 s is just above p95, so ordinary steady running is not flagged.
CADENCE_STUCK_MIN_S = 15
CADENCE_STUCK_SPEED_RANGE_MPS = 0.3

# §14a: "treat samples within a few seconds of a split_type change as suspect",
# after a large cadence excursion at a walk->run boundary. Measured at 1 Hz,
# cadence settled back near its running baseline within ~3 s. The excursion
# lives in the first 3 seconds.
SPLIT_TRANSITION_S = 3

# DELIBERATELY ABSENT: arm-motion decoupling. §15.4 names it as an artifact
# type, but §14a MEASURED it and found the trend sign POSITIVE -- the opposite
# of the hypothesis -- across 29 descents, with run/walk transitions being the
# real instability instead. A flag for it would encode an unsupported
# hypothesis as data. It stays unflagged until something measures it, and
# §14a's transient caveat is a RESOLUTION LIMIT, not an artifact (§15.4).
ALL_FLAGS = (
    "gps_jitter",
    "speed_discontinuity",
    "cadence_dropout",
    "cadence_stuck",
    "split_transition",
)


def quality_flags(timestamps, xs, ys, speeds, cadences, split_types):
    """Per-record §15.4 artifact flags, comma-separated names ('' if clean).

    Inputs are parallel sequences over ONE activity, ordered by time.
    """
    n = len(timestamps)
    flags = [set() for _ in range(n)]
    if n < 7:
        return ["" for _ in range(n)]

    X = np.asarray(xs, dtype=float)
    Y = np.asarray(ys, dtype=float)
    secs = np.array([(t - timestamps[0]).total_seconds() for t in timestamps])

    # --- position jitter, normalised by this run's own step length ---------
    step = np.hypot(np.diff(X), np.diff(Y))
    med_step = float(np.median(step[step > 0])) if (step > 0).any() else 1.0
    for i in range(3, n - 3):
        ax, ay, bx, by = X[i - 3], Y[i - 3], X[i + 3], Y[i + 3]
        L = float(np.hypot(bx - ax, by - ay))
        if L <= 1e-6:
            continue
        dev = abs((bx - ax) * (ay - Y[i]) - (ax - X[i]) * (by - ay)) / L
        if dev / max(med_step, 1e-6) > JITTER_NORM_MAX:
            flags[i].add("gps_jitter")

    # --- speed discontinuity, from PATH-derived speed (§9) -----------------
    dt = np.diff(secs)
    with np.errstate(invalid="ignore", divide="ignore"):
        v = np.where(dt > 0, step / np.maximum(dt, 1e-9), np.nan)
    for i in range(1, len(v)):
        if dt[i] <= 0:
            continue
        a = abs(v[i] - v[i - 1]) / dt[i]
        if np.isfinite(a) and a > ACCEL_MAX_MPS2:
            flags[i + 1].add("speed_discontinuity")

    # --- cadence dropout ---------------------------------------------------
    for i in range(n):
        if (
            cadences[i] == 0
            and speeds[i] is not None
            and speeds[i] > CADENCE_DROPOUT_SPEED_MPS
        ):
            flags[i].add("cadence_dropout")

    # --- cadence lock ------------------------------------------------------
    i = 0
    while i < n:
        j = i
        while j + 1 < n and cadences[j + 1] == cadences[i]:
            j += 1
        if cadences[i] and secs[j] - secs[i] >= CADENCE_STUCK_MIN_S:
            win = [speeds[k] for k in range(i, j + 1) if speeds[k] is not None]
            if win and (max(win) - min(win)) > CADENCE_STUCK_SPEED_RANGE_MPS:
                for k in range(i, j + 1):
                    flags[k].add("cadence_stuck")
        i = j + 1

    # --- run/walk transition spikes (§14a) ---------------------------------
    for i in range(1, n):
        if split_types[i] != split_types[i - 1]:
            for k in range(i, n):
                if secs[k] - secs[i] > SPLIT_TRANSITION_S:
                    break
                flags[k].add("split_transition")

    return [",".join(sorted(f)) for f in flags]
