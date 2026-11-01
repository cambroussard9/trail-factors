"""Milestone 5: the segment matcher (SPEC.md §5, §5.3, §12.3).

WHAT IS MATCHED. A segment is a SLOPE -- one monotonic stretch of terrain,
keyed on its two ENDPOINTS and traversable either way. All of that is a
property of terrain; none of it is a property of a traversal.

THE UNIT CHANGED IN MILESTONE 6 and the reason matters. It was a prominence
hill, which is up-then-down BY DEFINITION -- a peak with a col either side. On
the first trail this was developed on that IS the terrain (measured there:
under a fifth of a hill's span is flat, and the longest flat is 620 m), which
is why prominence found clean hills. But that is a property of ONE TRAIL, not
of hills. A climb can top onto a plateau with no col; on a point-to-point
course ascents and descents can be separated by MILES of flat. The ascent and
the descent are separate features that happen, on rollers, to be adjacent.

ENDPOINTS ARE DIRECTION-NORMALISED, so running a face up and running it down
are two EFFORTS on ONE segment. That is deliberate: it is what makes the §6.3
tau comparison like-for-like -- recovery after climbing slope X against
recovery after descending the SAME slope X.

FRECHET IS DELIBERATELY ABSENT. It belongs to the polyline design that §5.0
replaced. It measures whether two curves follow the same LINE, and §5.1 records
that runners do not: switchback shortcuts, a different line on wide
trail, impromptu cuts. It would penalise exactly the variation §5.1 calls
normal, while ignoring elevation -- the signal §5.3 measures as stable to half
a metre.

NO PACE AND NO HEART RATE ANYWHERE IN THE MATCH KEY. Median HR was measured
nearly flat across grade bands (§6.2), so it barely discriminates terrain;
§14e adds that zones are not even on a stable scale across a corpus. Every matching
signal
here is deterministic geometry, which is also what makes the result
reproducible. HR is RECORDED on an effort, never used to match one.

ELEVATION IS DEM-ONLY. Garmin's peak-to-peak error is ~10 m (§5a-bis.4),
roughly 20x the DEM's run-to-run SD -- substituting it would destroy the
discriminator.
"""

from __future__ import annotations

import hashlib
import math
import statistics

from trail_factors.transform.dem import project

CALC_VERSION = 1

# --- thresholds, FITTED to §5.3's measurements rather than picked ------------
#
# MEASURED CORRECTION: on the development corpus the elevation and prominence
# gates are NON-BINDING. Removing either, or both, produces a byte-identical
# library -- same ids, same effort counts -- because the closest two
# traversals in different segments are 41.1 m apart, beyond MATCH_RADIUS_M.
# Position alone decides every grouping here.
#
# So peak elevation is the most REPEATABLE signal (§5.3: SD 0.54 m run-to-run),
# but it is NOT currently the discriminating one; an earlier comment here
# claiming it was "the strongest discriminator" described behaviour this code
# does not exhibit. The gates are kept as conservative insurance -- a
# conjunction can only SPLIT a cluster, never merge one, so they cost nothing
# and would bind on denser terrain where peaks sit closer together. Reproduce with
# build_library(tr, elev_tol_m=1e9).
#
# REFITTED FOR SLOPES (§12.4). §5.3's 0.54 m was the run-to-run SD of a single
# PEAK elevation. A slope's endpoints are bounded by where a hysteresis
# threshold was crossed, which is noisier: across same-face ascent/descent
# pairs within one run, endpoint elevations differ by a median of 0.5 m, p90
# 3.5 m and a maximum of 4.4 m. 5 m clears the observed maximum.
#
# Elevation cannot be the discriminator there in any case -- a whole trail
# can span a few tens of metres, so distinct slopes overlap heavily in
# elevation.
# Position discriminates; elevation is the insurance (see ELEV_TOL_M's use in
# `compatible`).
ELEV_TOL_M = 5.0

# The place anchor is a PEAK, so this is the same quantity Milestone 5 fitted
# and it lands on the same plateau. Swept over the corpus with identity =
# (place, side):
#
#   radius        25   30   35   40   45   50   60
#   segments      29   27   26   23   20   20   19
#   bidirectional 12   12   12   12   11   11   11
#   ref distinct  11   11   11   11   10   10   10
#   consistency    0    0    0    0    1    1    1   failures
#   max place SD 0.80 0.80 0.80 0.80 1.99 1.99 1.99  m
#
# 25-40 m is flat on every measure and 45 m breaks: place-elevation SD jumps
# 0.80 -> 1.99 m and a segment merges. 30 m is mid-plateau, and is the same
# value Milestone 5 fitted for peak identity -- unsurprising, since it is the
# same peaks.
MATCH_RADIUS_M = 30.0

# §12.3's consistency check, applied to the PLACE elevation. Peaks reproduce
# to SD 0.19-0.66 m (§5.3), so 1.5 m is ~2.5x the observed spread -- close to
# the original 1.0 m because it measures the same stable quantity again. It is
# NOT applied to the col: cols scatter at SD 2.20 m by nature, and testing them
# would flag correct segments.
MAX_SEGMENT_ELEV_SD_M = 1.5

# Two flanks of one peak are different segments. The bearing from the place
# anchor toward the low end separates them: measured bimodal over the
# same-face pairs -- a cluster at a 12.5 degree median for the same flank and
# one near 155 degrees for opposite flanks -- so 90 degrees is where the
# distribution actually divides.
SIDE_TOL_DEG = 90.0

# How far from a slope's high end a prominence peak may sit and still anchor
# it. Median distance high-end -> nearest peak is 60 m; 100 m captures about
# two thirds of slopes. A slope with no peak in range keeps its own high end as the
# anchor -- the honest fallback for a climb that tops onto a plateau, where
# there is no summit to inherit from (§5.4).
PEAK_SNAP_M = 100.0


def to_utm(lats, lons, crs: str | None = None):
    """Positions in metres, through dem.project.

    In `crs` when given -- the projection already resolved for the run these
    points belong to. Otherwise the configured `projection_crs`, else the UTM
    zone derived from these positions, which is right only when they are a
    whole run. The name is historical: nothing here requires UTM.
    """
    return project(lats, lons, crs)


def segment_id_for(
    lo_x: float, lo_y: float, hi_x: float, hi_y: float, projection: str | None = None
) -> str:
    """Content-derived id, from the FOUNDING traversal's ENDPOINTS at 1 m.

    Deterministic and stable: it does not move when later traversals shift the
    centroid, and it does not renumber when a new segment is added. That
    matters because athlete-supplied segment NAMES key on it (§14), and a name
    that detached on rebuild would be the records_valid failure class again --
    correct-looking until something notices.

    `projection` is the DERIVED zone's CRS, and None when the projection is
    configured. Under a derived projection two founders in different zones
    can round to the same metres for places hundreds of kilometres apart, so
    the zone goes into the hash. Under a configured one every traversal shares
    a single frame, and the string hashed is exactly the one ids were always
    built from -- configuring `projection_crs` changes no id. (Configuring it
    AFTER running derived does re-found the ids, like any backfill; names are
    run-anchored for exactly that reason.)
    """
    key = f"{round(lo_x)}:{round(lo_y)}:{round(hi_x)}:{round(hi_y)}"
    if projection is not None:
        key = f"{projection}|{key}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def projection_key(traversal: dict) -> str | None:
    """The frame a traversal's x/y are in, for identity: None when configured.

    A traversal without the key (an older caller, a synthetic test) is in the
    configured frame by assumption -- the behaviour that existed before
    derivation.
    """
    return traversal.get("projection") if traversal.get("projection_derived") else None


def bearing_deg(x0: float, y0: float, x1: float, y1: float) -> float:
    """Compass bearing from one projected point to another."""
    return math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360.0


def angular_diff(a: float, b: float) -> float:
    """Smallest absolute angle between two bearings, 0-180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def compatible(
    a: dict, b: dict, radius_m: float | None = None, elev_tol_m: float | None = None
) -> bool:
    """The pairwise predicate: are these two slopes the same (place, side)?

    IDENTITY IS INHERITED FROM THE PEAK, which is the one thing the matcher
    already validates. §5.3 measured peaks reproducing to SD 0.54 m and cols to
    SD 2.20 m -- a summit is a sharp feature you must cross, a col is wherever
    the profile happened to bottom out. The COL IS DERIVED FROM THE HYSTERESIS
    WALK AND NEVER KEYED ON.

    Keying on the slope's own endpoints was tried first and measurably fails.
    For 53 ascent/descent pairs of the SAME face within a SINGLE run --
    labelled positives, since an out-and-back runs one face both ways -- the
    endpoint positions scatter by a median of 15-52 m, a p90 of 231-291 m and a
    maximum of 380 m, and detected lengths differ by a median of 42%. A
    hysteresis boundary is where a threshold was crossed; that point is not
    symmetric between climbing and descending the same ground, and three
    boundary walks crossed into the neighbouring feature on shallow cols.

    SIDE distinguishes the two flanks of one peak. Measured on the same 53
    pairs, the bearing from the place anchor toward the low end agrees to a
    median of 12.5 degrees for the same flank, with a second cluster near 155
    degrees for opposite flanks -- bimodal, so 90 degrees is the natural split
    rather than a picked number.
    """
    radius_m = MATCH_RADIUS_M if radius_m is None else radius_m
    elev_tol_m = ELEV_TOL_M if elev_tol_m is None else elev_tol_m
    # SAME FRAME FIRST. x/y in two different UTM zones are not comparable: a
    # place in one zone and a place hundreds of kilometres away in the next
    # can carry numerically identical coordinates, and the radius test below
    # would call them one place. Under a configured projection every
    # traversal shares one key, so this never fires there.
    if projection_key(a) != projection_key(b):
        return False
    if math.hypot(a["place_x"] - b["place_x"], a["place_y"] - b["place_y"]) > radius_m:
        return False
    if abs(a["place_elevation_m"] - b["place_elevation_m"]) > elev_tol_m:
        return False
    if angular_diff(a["side_bearing_deg"], b["side_bearing_deg"]) > SIDE_TOL_DEG:
        return False
    return True


def matches(traversal: dict, segment: dict) -> bool:
    """Does one traversal belong to one existing segment?

    Tested against the segment's MEMBERS, not its centroid summary -- the SAME
    predicate build_library groups with. Comparing against the centroid instead
    made the two paths disagree: two real members of the widest segment sat
    30.1 m from its centroid and 6.94 m from its median prominence, so they
    joined the library but then failed to match it. A traversal already in the
    library that the matcher cannot recognise is a contradiction, and it would
    have surfaced as a phantom "new segment" the first time the incremental
    path was used in production.

    Segments built here always carry "members"; a bare summary dict (as used in
    tests) is treated as a single member.
    """
    members = segment.get("members") or [segment]
    return any(compatible(traversal, m) for m in members)


def build_library(
    traversals: list[dict],
    radius_m: float = MATCH_RADIUS_M,
    elev_tol_m: float = ELEV_TOL_M,
) -> list[dict]:
    """Group traversals into segments. Returns segments, each with 'members'.

    ORDER-INDEPENDENT BY CONSTRUCTION. Connected components over the symmetric
    "could match" relation, not sequential assignment. Ingest-order dependence
    is a defect this project has already fixed once (§4.2, runs.start_time_utc),
    and a library that differed depending on which run was ingested first would
    be unreproducible in exactly the same way.

    Single-link over the CONJUNCTION of gates (both endpoints, both endpoint
    elevations). Chaining is still possible in principle; §12.3 deliberately
    does not suppress it with a tighter radius, because the consistency check
    is what should surface a merge -- a threshold chosen to make the symptom
    disappear is §4.2's "check that cannot fire" in another form.
    """
    n = len(traversals)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        for j in range(i + 1, n):
            if compatible(traversals[i], traversals[j], radius_m, elev_tol_m):
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for i, tr in enumerate(traversals):
        groups.setdefault(find(i), []).append(tr)

    def sd(vals):
        return statistics.pstdev(vals) if len(vals) > 1 else 0.0

    segments = []
    for members in groups.values():
        # Founding traversal = earliest by start time, then slope_index.
        # Ordering is total and derived from data, so the id is reproducible.
        # It is NOT stable against backfill -- an older run re-founds the
        # segment -- which is exactly why athlete names and venue anchors are
        # RUN-ANCHORED rather than keyed to this id
        # (src/trail_factors/analysis/venues.py).
        founder = min(members, key=lambda m: (m["sort_key"], m["slope_index"]))
        lo_x = statistics.fmean([m["lo_x"] for m in members])
        lo_y = statistics.fmean([m["lo_y"] for m in members])
        hi_x = statistics.fmean([m["hi_x"] for m in members])
        hi_y = statistics.fmean([m["hi_y"] for m in members])
        lows = [m["low_elevation_m"] for m in members]
        highs = [m["high_elevation_m"] for m in members]
        px = statistics.fmean([m["place_x"] for m in members])
        py = statistics.fmean([m["place_y"] for m in members])
        spread = max(math.hypot(m["place_x"] - px, m["place_y"] - py) for m in members)
        segments.append(
            {
                # Identity is (place, side): the founding traversal's PLACE anchor
                # plus its flank, not the noisy col.
                "segment_id": segment_id_for(
                    founder["place_x"],
                    founder["place_y"],
                    round(founder["side_bearing_deg"] / SIDE_TOL_DEG),
                    0,
                    projection=projection_key(founder),
                ),
                # The frame every member's x/y is in -- one per segment, since
                # `compatible` never joins two -- and how it was chosen.
                "projection": founder.get("projection"),
                "projection_derived": bool(founder.get("projection_derived")),
                "members": members,
                "lo_x": lo_x,
                "lo_y": lo_y,
                "hi_x": hi_x,
                "hi_y": hi_y,
                "mid_x": statistics.fmean([m["mid_x"] for m in members]),
                "mid_y": statistics.fmean([m["mid_y"] for m in members]),
                "place_x": statistics.fmean([m["place_x"] for m in members]),
                "place_y": statistics.fmean([m["place_y"] for m in members]),
                "place_elevation_m": statistics.median(
                    [m["place_elevation_m"] for m in members]
                ),
                "side_bearing_deg": founder["side_bearing_deg"],
                "low_lat": founder["low_lat"],
                "low_lon": founder["low_lon"],
                "high_lat": founder["high_lat"],
                "high_lon": founder["high_lon"],
                # Median, not mean: robust to one mis-detected traversal.
                "low_elevation_m": statistics.median(lows),
                "high_elevation_m": statistics.median(highs),
                "vertical_m": statistics.median([m["vertical_m"] for m in members]),
                "length_m": statistics.median([m["length_m"] for m in members]),
                "mean_grade_pct": statistics.median(
                    [abs(m["mean_grade_pct"]) for m in members]
                ),
                "n_efforts": len(members),
                "n_ascents": sum(1 for m in members if m["direction"] == "ascent"),
                "n_descents": sum(1 for m in members if m["direction"] == "descent"),
                "low_elevation_sd_m": sd(lows),
                "high_elevation_sd_m": sd(highs),
                "place_elevation_sd_m": sd([m["place_elevation_m"] for m in members]),
                "endpoint_spread_m": spread,
                "first_seen_utc": founder.get("start_utc"),
            }
        )
    # Deterministic output order; ids are content-derived so this is stable.
    segments.sort(key=lambda s: s["segment_id"])
    return segments


def match_traversal(traversal: dict, segments: list[dict]) -> dict | None:
    """Match ONE traversal against an EXISTING library, or None.

    This is the incremental path, and the one the new-trail guarantee runs
    through (§12.3). A candidate must fall within MATCH_RADIUS_M of an existing
    peak, so a run on a trail kilometres away generates no candidate at all --
    a hard geometric bound rather than a tunable threshold.

    Where several segments pass every gate, the nearest peak wins; ties break
    on segment_id so the result never depends on list order.
    """
    hits = [s for s in segments if matches(traversal, s)]
    if not hits:
        return None
    # Nearest by CENTROID for the tie-break -- a stable, order-free summary --
    # even though membership is decided pairwise above. Distance is the sum
    # over both endpoints, so a slope matching one end well and the other
    # poorly loses to one that matches both.
    return min(
        hits,
        key=lambda s: (
            math.hypot(
                traversal["place_x"] - s["place_x"], traversal["place_y"] - s["place_y"]
            ),
            s["segment_id"],
        ),
    )


def consistency_failures(
    segments: list[dict], max_sd_m: float = MAX_SEGMENT_ELEV_SD_M
) -> list[dict]:
    """Segments whose PLACE elevations disagree too much to be one slope.

    Checks the PLACE, not the col. Identity is (place, side) and the col is
    derived from the hysteresis walk rather than keyed, so col scatter is
    expected and must not read as a merge -- §5.3 measured cols at SD 2.20 m
    against peaks at 0.54 m. Flagging on the col would be a check firing on the
    truth, which is §4.2's failure mode inverted.
    """
    return [
        s
        for s in segments
        if s["n_efforts"] > 1 and s["place_elevation_sd_m"] > max_sd_m
    ]
