"""§19 venue identity: which VENUE a run was on, from ANCHOR places.

A cluster (a configured <CLUSTER>) can pool venues with a several-fold
spread in climbing, so a percentile against it measures which venue the run
was on as much as how it went. A venue is defined by a few ANCHOR segments
the athlete names -- the climb that makes a venue recognisably itself -- and a
run belongs to a venue if it crosses one of them. §19.1 measured why not the full segment set: it is
a property of the ROUTE, and a venue has many routes.

ANCHORS ARE RUN-ANCHORED AND COORDINATE-FREE (§19.3): `(run_id, direction,
at_distance_m)`, a point INSIDE one traversal of the slope on a run whose raw
file never changes. A segment_id is founder-derived and moves when an older
run is backfilled; the anchor RESOLVES, at read time, to whichever segment
that traversal now belongs to. Segment names use the same anchor, and the
place-coordinate store they replaced is gone before it was ever written.

`at_distance_m` is the traversal's MIDPOINT, not its start: resolution is by
containment, and a boundary that moves a few metres on a silver rebuild must
not push the anchor off its own slope.

Every assignment state but `assigned` falls back to the CLUSTER, labelled --
`mixed` included (the athlete's ruling): a link run is pooled
with the cluster and says so, never with either venue.
"""

from __future__ import annotations

# A link run takes the venue whose anchor efforts carry at least this share of
# the anchor distance over every venue it touched; below it, `mixed`. The
# athlete's ruling (§19.6), not a fit. Inclusive.
LINK_SHARE_MIN = 0.60

ASSIGNED, MIXED, UNASSIGNED, NONE = "assigned", "mixed", "unassigned", "none"
# NO VENUE IS DEFINED AT ALL -- a fresh install or an empty athlete-state
# directory. Its own state, because `unassigned` says "venues exist and this
# run crossed none of their anchors", which is a claim about the run that
# nothing checked when there is no anchor to cross.
NOT_CONFIGURED = "not_configured"
NOT_CONFIGURED_REASON = "no venues are configured"

# The ONLY keys an anchor may carry. A coordinate in the athlete-state
# directory is a position stored beside the athlete's own answers, which are
# backed up and passed around (§19.3); the store refuses one by name.
ANCHOR_KEYS = ("run_id", "direction", "at_distance_m")
FORBIDDEN_ANCHOR_KEYS = ("lat", "lon", "peak_lat", "peak_lon", "place_x",
                         "place_y", "x", "y", "easting", "northing")


def validate_anchor(anchor: dict) -> dict:
    """An anchor is exactly ANCHOR_KEYS, typed. Anything else is refused."""
    bad = sorted(k for k in anchor if k in FORBIDDEN_ANCHOR_KEYS)
    if bad:
        raise ValueError(f"an anchor may not carry a position: {bad}. Anchors "
                         f"are run-anchored (run_id, direction, at_distance_m) "
                         f"precisely so athlete state holds no coordinate")
    extra = sorted(set(anchor) - set(ANCHOR_KEYS))
    missing = [k for k in ANCHOR_KEYS if k not in anchor]
    if extra or missing:
        raise ValueError(f"an anchor is exactly {ANCHOR_KEYS}; "
                         f"missing {missing}, unknown {extra}")
    if anchor["direction"] not in ("ascent", "descent"):
        raise ValueError(f"direction must be ascent or descent, "
                         f"not {anchor['direction']!r}")
    if not isinstance(anchor["at_distance_m"], (int, float)):
        raise ValueError("at_distance_m must be a number of metres")
    return {k: anchor[k] for k in ANCHOR_KEYS}


def anchor_for_effort(con, activity_id: str, slope_index: int) -> dict:
    """The run-anchored identity of one traversal: its run, direction, and
    the midpoint of its distance span."""
    row = con.execute("""
        SELECT a.run_id, t.direction, t.start_distance_m, t.end_distance_m
        FROM slope_traversals t JOIN activities a USING (activity_id)
        WHERE t.activity_id = ? AND t.slope_index = ?""",
        [activity_id, slope_index]).fetchone()
    if row is None:
        raise ValueError(f"no traversal {slope_index} on {activity_id}")
    run_id, direction, sd, ed = row
    return {"run_id": run_id, "direction": direction,
            "at_distance_m": round((sd + ed) / 2.0, 1)}


def anchor_for_segment(con, segment_id: str) -> dict:
    """An anchor for a CURRENT segment: its earliest effort. Any member would
    do -- the raw file never changes -- so the choice is only made total."""
    row = con.execute("""
        SELECT activity_id, slope_index FROM segment_efforts
        WHERE segment_id = ? ORDER BY start_utc, activity_id, slope_index
        LIMIT 1""", [segment_id]).fetchone()
    if row is None:
        raise ValueError(f"no segment {segment_id}; run `match` first")
    return anchor_for_effort(con, row[0], row[1])


def resolve_anchor(con, anchor: dict) -> dict:
    """Which segment the anchor's traversal belongs to NOW.

    `anchor_unresolved` when no traversal on that run, in that direction,
    contains the point -- the matcher no longer finds a slope there -- rather
    than silently losing the venue's members.
    """
    rows = con.execute("""
        SELECT DISTINCT e.segment_id
        FROM slope_traversals t
        JOIN activities a USING (activity_id)
        JOIN segment_efforts e
          ON e.activity_id = t.activity_id AND e.slope_index = t.slope_index
        WHERE a.run_id = ? AND t.direction = ?
          AND t.start_distance_m <= ? AND t.end_distance_m >= ?""",
        [anchor["run_id"], anchor["direction"], anchor["at_distance_m"],
         anchor["at_distance_m"]]).fetchall()
    if len(rows) == 1:
        return {"status": "resolved", "segment_id": rows[0][0]}
    if not rows:
        return {"status": "anchor_unresolved", "segment_id": None,
                "reason": f"no {anchor['direction']} traversal on run "
                          f"{anchor['run_id']} contains "
                          f"{anchor['at_distance_m']:.0f} m"}
    return {"status": "anchor_ambiguous", "segment_id": None,
            "reason": f"{len(rows)} segments contain the point: "
                      + ", ".join(sorted(r[0] for r in rows))}


def names_by_segment(con, names: list[dict]) -> dict:
    """segment_id -> athlete name, each name resolved through its anchor."""
    out = {}
    for entry in names:
        hit = resolve_anchor(con, entry["anchor"])
        if hit["segment_id"] is not None:
            out[hit["segment_id"]] = entry["name"]
    return out


def classify(efforts: list[tuple[str, float]],
             anchors_by_venue: dict[str, set]) -> dict:
    """One run's venue from its efforts. PURE, so the rule is testable alone.

    `efforts` are (segment_id, length_m) for every segment effort on the run;
    `anchors_by_venue` maps venue name -> its resolved anchor segment ids.

    THE SHARE IS OVER ANCHOR DISTANCE, among the venues touched. §19.2 says
    "of the run's total segment-effort distance", but a venue owns only its
    1-3 anchors, so read literally no link run could ever reach 60% -- the
    anchors are a small fraction of any run's climbing. Over anchor distance,
    a run that crossed one venue's anchors only has share 1.0.

    With NO venue defined, every run is `not_configured`, ahead of the
    efforts check: no run could be assigned whatever it crossed, and that is
    the cause a reader needs. A venue whose anchors all failed to resolve is
    still a configured venue, so that case stays `unassigned`.
    """
    if not anchors_by_venue:
        return {"state": NOT_CONFIGURED, "venue": None, "venues_touched": [],
                "reason": NOT_CONFIGURED_REASON}
    if not efforts:
        return {"state": NONE, "venue": None, "venues_touched": []}
    dist: dict[str, float] = {}
    for seg, length in efforts:
        for v, segs in anchors_by_venue.items():
            if seg in segs:
                dist[v] = dist.get(v, 0.0) + (length if length is not None
                                              else 0.0)
    if not dist:
        return {"state": UNASSIGNED, "venue": None, "venues_touched": [],
                "reason": "this run crosses segments but no venue's anchor"}
    total = sum(dist.values())
    touched = sorted(({"venue": v, "anchor_distance_m": d,
                       "share": d / total if total else 1.0 / len(dist)}
                      for v, d in dist.items()),
                     key=lambda t: (-t["share"], t["venue"]))
    top = touched[0]
    if top["share"] >= LINK_SHARE_MIN - 1e-9:
        return {"state": ASSIGNED, "venue": top["venue"],
                "venues_touched": touched}
    return {"state": MIXED, "venue": None, "venues_touched": touched,
            "reason": f"a link run: no venue carries {LINK_SHARE_MIN:.0%} of "
                      f"its anchor distance (largest {top['venue']} at "
                      f"{top['share']:.0%})"}


def assign_all(con, venues: list[dict] | None = None) -> dict:
    """{"by_run": run_id -> assignment, "anchors": per-venue resolution}.

    Runs with NO segment efforts are `none`, with the cause looked up rather
    than asserted: terrain computed and flat, or no terrain at all, in which
    case whether it has slopes is unknown.
    """
    from trail_factors.ingest.athlete_state import load_venues

    venues = load_venues() if venues is None else venues
    anchors_by_venue: dict[str, set] = {}
    resolution = []
    for v in venues:
        segs = set()
        for a in v["anchors"]:
            hit = resolve_anchor(con, a)
            resolution.append({"venue": v["name"], "anchor": a, **hit})
            if hit["segment_id"] is not None:
                segs.add(hit["segment_id"])
        anchors_by_venue[v["name"]] = segs
    # A segment that is two venues' anchor decides nothing: refused, named.
    owners: dict[str, list] = {}
    for v, segs in anchors_by_venue.items():
        for s in segs:
            owners.setdefault(s, []).append(v)
    shared = {s: vs for s, vs in owners.items() if len(vs) > 1}
    for s, vs in shared.items():
        for v in vs:
            anchors_by_venue[v].discard(s)

    efforts: dict[str, list] = {}
    for rid, seg, length in con.execute("""
            SELECT a.run_id, e.segment_id, e.length_m
            FROM segment_efforts e JOIN activities a USING (activity_id)
            ORDER BY a.run_id, e.start_utc""").fetchall():
        efforts.setdefault(rid, []).append((seg, length))
    with_terrain = {r[0] for r in con.execute(
        "SELECT DISTINCT a.run_id FROM dem_elevations d "
        "JOIN activities a USING (activity_id)").fetchall()}
    by_run = {}
    for (rid,) in con.execute("SELECT run_id FROM runs").fetchall():
        a = classify(efforts.get(rid, []), anchors_by_venue)
        if a["state"] == NONE:
            a["reason"] = ("no segment efforts: terrain was computed and no "
                           "slope was found" if rid in with_terrain else
                           "no segment efforts: this run has no terrain (no "
                           "DEM elevations), so whether it has slopes is "
                           "unknown")
        by_run[rid] = a
    return {"by_run": by_run, "anchors": resolution,
            "shared_anchor_segments": shared,
            "n_venues": len(venues)}


def candidates(con, cluster: str, min_runs: int = 3,
               overlap: float = 0.5) -> dict:
    """PROPOSE anchors for one cluster's venues; choose nothing, write nothing.

    Segments crossed by at least `min_runs` of the cluster's runs are grouped
    by how far their CROSSING RUNS overlap (single-link, Jaccard >= overlap):
    two slopes the same runs keep crossing are probably one venue. Each group
    is a LIKELY venue, never a named one -- naming it is the athlete's act.

    Grouping SEGMENTS, not runs, because an anchor is a segment: §19.1
    measured that grouping runs by their whole segment set does not separate
    venues. `overlap` is a presentation choice for the proposal and decides
    nothing stored, so it is not in calc_params.

    Two climbing figures, labelled apart: a segment's own ft/mi is its
    STEEPNESS; `runs_climb_ft_per_mi` is the median whole-run climbing of the
    runs that cross it (segment-effort ascent over run distance, §19's
    measure) -- the figure that tells one venue from another.
    """
    from trail_factors.analysis.report import cluster_of
    from trail_factors.units import METERS_PER_MILE

    FT = 3.28084
    runs = {}
    for aid, rid, start, dist in con.execute("""
            SELECT DISTINCT a.activity_id, a.run_id, a.start_time_utc,
                   a.total_distance_m
            FROM segment_efforts e JOIN activities a USING (activity_id)
            WHERE a.sport = 'running' ORDER BY a.start_time_utc""").fetchall():
        if cluster_of(con, aid) == cluster:
            runs[rid] = {"activity_id": aid, "date": str(start)[:10],
                         "distance_m": dist}
    by_seg: dict[str, set] = {}
    for rid, r in runs.items():
        up = 0.0
        for seg, direction, vert in con.execute(
                "SELECT segment_id, direction, vertical_m FROM segment_efforts "
                "WHERE activity_id = ?", [r["activity_id"]]).fetchall():
            by_seg.setdefault(seg, set()).add(rid)
            up += vert if direction == "ascent" and vert is not None else 0.0
        r["climb_ft_per_mi"] = (up * FT / (r["distance_m"] / METERS_PER_MILE)
                                if r["distance_m"] else None)

    cand = sorted((s for s, rs in by_seg.items() if len(rs) >= min_runs),
                  key=lambda s: (-len(by_seg[s]), s))
    parent = {s: s for s in cand}

    def find(s):
        while parent[s] != s:
            s = parent[s]
        return s

    for i, a in enumerate(cand):
        for b in cand[i + 1:]:
            ra, rb = by_seg[a], by_seg[b]
            if len(ra & rb) / len(ra | rb) >= overlap - 1e-9:
                parent[find(b)] = find(a)
    groups: dict[str, list] = {}
    for s in cand:
        groups.setdefault(find(s), []).append(s)

    def median(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        m = len(vals) // 2
        return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2

    out_groups = []
    for segs in groups.values():
        members = set().union(*(by_seg[s] for s in segs))
        rows = []
        for s in segs:
            grade, length, vert, n_up, n_down = con.execute(
                "SELECT mean_grade_pct, length_m, vertical_m, n_ascents, "
                "n_descents FROM segments WHERE segment_id = ?", [s]).fetchone()
            anchor = anchor_for_segment(con, s)
            span = con.execute("""
                SELECT t.start_distance_m, t.end_distance_m, a.start_time_utc
                FROM slope_traversals t JOIN activities a USING (activity_id)
                WHERE a.run_id = ? AND t.direction = ?
                  AND t.start_distance_m <= ? AND t.end_distance_m >= ?""",
                [anchor["run_id"], anchor["direction"], anchor["at_distance_m"],
                 anchor["at_distance_m"]]).fetchone()
            crossing = sorted(by_seg[s], key=lambda r: runs[r]["date"])
            rows.append({
                "segment_id": s, "n_runs": len(crossing),
                "n_runs_in_group": len(members),
                "grade_pct": grade, "ft_per_mi": grade * 52.8,
                "length_mi": length / METERS_PER_MILE, "vertical_ft": vert * FT,
                "n_ascents": n_up, "n_descents": n_down,
                "runs_climb_ft_per_mi": median(
                    runs[r]["climb_ft_per_mi"] for r in crossing),
                "runs": [{"run_id": r, "date": runs[r]["date"]}
                         for r in crossing],
                "reference": {**anchor, "date": str(span[2])[:10],
                              "at_mi": anchor["at_distance_m"] / METERS_PER_MILE,
                              "span_mi": (span[0] / METERS_PER_MILE,
                                          span[1] / METERS_PER_MILE)},
            })
        rows.sort(key=lambda r: (-r["n_runs"], r["segment_id"]))
        climbs = [runs[r]["climb_ft_per_mi"] for r in members]
        out_groups.append({
            "n_runs": len(members), "n_segments": len(segs),
            "runs_climb_ft_per_mi": median(climbs),
            "runs_climb_range_ft_per_mi": (
                min(v for v in climbs if v is not None),
                max(v for v in climbs if v is not None)),
            "candidates": rows})
    out_groups.sort(key=lambda g: (-g["n_runs"], -g["n_segments"]))
    return {"cluster": cluster, "n_runs": len(runs), "min_runs": min_runs,
            "overlap": overlap, "groups": out_groups,
            "n_runs_without_a_candidate": len(
                set(runs) - set().union(set(), *(by_seg[s] for s in cand)))}
