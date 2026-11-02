"""DEM, silver layer and the segment matcher (§5, §5a, §12.2, §12.4)."""

from __future__ import annotations

import math

from trail_factors import config
from trail_factors.cli.common import resolve_activity_id
from trail_factors.ingest.load import DB_PATH
from trail_factors.paths import ATHLETE_DIR, DEM_DIR, display_path

# A SINGLE NAMED CELL IS ALWAYS FETCHED; A BULK --missing OVER THIS ASKS FIRST.
# One 1 m tile is 100-500 MB and the corpus's full refusal list is several
# gigabytes. A command that quietly pulls 4 GB because it was run with the
# wrong flag is a command that gets run once and then never trusted.
FETCH_CONFIRM_BYTES = 1_500_000_000


def dem_populate(activity_ref: str | None = None) -> None:
    """Populate dem_elevations from the LOCAL tile (§5a). No network."""
    import duckdb

    from trail_factors.analysis.basic import db_elevation
    from trail_factors.transform.dem import CALC_VERSION, OutsideTile, TileSet

    tiles = TileSet()
    if not tiles:
        raise FileNotFoundError(
            f"no DEM tiles in {display_path(DEM_DIR)}. Fetch one from USGS "
            f"3DEP; the report names the exact tile a run needs."
        )
    print(f"tiles loaded: {', '.join(tiles.names)}")
    con = duckdb.connect(str(DB_PATH))
    try:
        if activity_ref:
            targets = [resolve_activity_id(con, activity_ref)]
        else:
            # RUNNING ONLY, AND THIS IS A HOLD, NOT A RULE.
            #
            # §4.1's "download broadly, model strictly" put every sport in raw
            # and bronze. The terrain layers have no sport awareness at all,
            # so without this they would silently take outdoor CYCLING
            # activities through dem -> silver -> match and
            # into the SEGMENT LIBRARY -- deciding how a bike ride is used by
            # not thinking about it, which is the decision that was explicitly
            # deferred.
            #
            # This restores exactly the coverage the derived layers had before
            # the backlog landed. It is a placeholder for a real policy, and
            # `silver` and `match` inherit it for free because both take their
            # targets from dem_elevations.
            targets = [
                r[0]
                for r in con.execute("""
                SELECT DISTINCT r.activity_id FROM records r
                JOIN activities a USING (activity_id)
                WHERE r.lat IS NOT NULL AND a.sport = 'running'
            """).fetchall()
            ]
        done = skipped = kept = 0
        outside: dict[tuple[str, str], dict] = {}
        for aid in targets:
            rows = con.execute(
                """
                SELECT timestamp, lat, lon FROM records
                WHERE activity_id = ? AND lat IS NOT NULL ORDER BY timestamp
            """,
                [aid],
            ).fetchall()
            if not rows:
                continue
            try:
                # PER-POINT TILE NAMES. This line referenced a `tile` variable
                # that stopped existing when DemTile became TileSet, so the
                # command raised NameError for anyone who ran it -- and nobody
                # did between that change and the first FR265 rebuild.
                el, tile_names = tiles.elevation(
                    [r[1] for r in rows], [r[2] for r in rows], with_tiles=True
                )
            except OutsideTile:
                # ELEVATION ALREADY STORED IS NOT A GAP. Its tile is gone from
                # disk, but the rows stand and nothing needs fetching.
                if db_elevation(con, aid)["complete"]:
                    kept += 1
                    continue
                # Loud, not silent, AND NAMED. A count alone told the athlete
                # that something was skipped but not which tile would fix it,
                # so the report showed a page of absent terrain numbers with
                # no reason (§5a).
                cov = tiles.coverage([r[1] for r in rows], [r[2] for r in rows])
                tally_missing(outside, cov)
                skipped += 1
                continue
            con.execute("BEGIN")
            con.execute("DELETE FROM dem_elevations WHERE activity_id = ?", [aid])
            con.executemany(
                "INSERT INTO dem_elevations VALUES (?,?,?,?,?)",
                [
                    (aid, r[0], float(v), tn, CALC_VERSION)
                    for r, v, tn in zip(rows, el, tile_names, strict=False)
                ],
            )
            con.execute("COMMIT")
            done += 1
        print(
            f"populated {done} activities; {skipped} outside every loaded tile"
            + (
                f"; {kept} outside the loaded tiles kept the elevation already stored"
                if kept
                else ""
            )
        )
        for line in missing_tile_lines(outside):
            print(line)
        n, lo, hi = con.execute(
            "SELECT count(*), min(altitude_dem_m), max(altitude_dem_m) "
            "FROM dem_elevations"
        ).fetchone()
        # An empty table has no range; say so rather than format a None.
        print(
            f"dem_elevations: {n:,} rows"
            + (f", {lo:.1f} .. {hi:.1f} m" if lo is not None else "")
        )
    finally:
        con.close()


def tally_missing(outside: dict, cov: dict) -> None:
    """Add one activity's missing cells to `dem`'s tally, in place.

    KEYED BY (grid, cell), never the cell name alone -- the same key
    `corpus_missing_cells` uses. With no projection configured each run's
    cells are labelled in its OWN derived zone, and x61y335 in zone 14 and in
    zone 15 are two pieces of ground 600 km apart: merging them printed one
    line, one count and one command for both.
    """
    for m in cov["missing_tiles"]:
        key = (cov["crs"], m["usgs_cell"])
        entry = outside.setdefault(
            key, {**m, "grid_crs": cov["crs"], "n_activities": 0}
        )
        entry["n_activities"] += 1


def missing_tile_lines(outside: dict) -> list[str]:
    """`dem`'s report of the tally: each cell with ITS OWN fetch command.

    A generic `fetch-tile <cell>` omitted the `--zone` that a cell labelled
    in a derived zone needs, so following it got "no zone was given"; and
    where no USGS grid exists there is no command to give at all, only the
    reason.
    """
    lines = []
    for e in sorted(outside.values(), key=lambda e: -e["n_activities"]):
        # No filename when it cannot even be guessed (no projection or no
        # suffix): name the cell instead.
        name = (
            e["filename"]
            if e["filename"] is not None
            else f"cell {e['usgs_cell']} (filename unresolved)"
        )
        lines.append(
            f"  MISSING TILE  {name} in {e['grid_crs']}  "
            f"(needed by {e['n_activities']} activities)"
        )
        if e["fetch_command"] is not None:
            lines.append(f"      -> {e['fetch_command']}")
        else:
            lines.append(f"      no fetch command: {e['fetch_unavailable_reason']}")
    if any(e["fetch_command"] is not None for e in outside.values()):
        lines.append(
            "  each command above (or `fetch-tile --missing` for all "
            "of them) resolves the real filename against the 3DEP "
            "index, verifies the download and re-runs retile. The "
            "filenames above are GUESSES -- the project suffix is "
            "copied from the tiles on disk, and outside the "
            "projection's UTM zone the x/y is wrong too."
        )
    return lines


def silver_build(activity_ref: str | None = None) -> None:
    """Build the silver layer. Rebuildable: deletes and recomputes (§3)."""
    import duckdb
    import numpy as np

    from trail_factors.transform import silver as S
    from trail_factors.transform.dem import run_projection

    con = duckdb.connect(str(DB_PATH))
    try:
        if activity_ref:
            targets = [resolve_activity_id(con, activity_ref)]
        else:
            targets = [
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT activity_id FROM dem_elevations"
                ).fetchall()
            ]
        n_rec = n_hill = n_grade = n_slope = 0
        # basis -> activities projected that way, for the summary line.
        bases: dict[str, int] = {}
        for aid in targets:
            rows = con.execute(
                """
                SELECT r.timestamp, r.elapsed_s, r.lat, r.lon, r.speed_mps,
                       d.altitude_dem_m, r.cadence_raw_rpm, s.split_type
                FROM records r JOIN dem_elevations d USING (activity_id, timestamp)
                LEFT JOIN splits s ON s.activity_id = r.activity_id
                     AND r.timestamp BETWEEN s.start_time AND s.end_time
                     -- THE rwd_* PARTITION ONLY. The FR265 writes an
                     -- `interval_active` split spanning the WHOLE RUN on top of
                     -- run/walk/stand, so an unrestricted time-range join
                     -- matches every record TWICE (§9).
                     AND s.split_type LIKE 'rwd\\_%' ESCAPE '\\'
                WHERE r.activity_id = ? ORDER BY r.timestamp
            """,
                [aid],
            ).fetchall()
            if len(rows) < 10:
                continue
            ts = [r[0] for r in rows]
            # ONE ROW PER TIMESTAMP, CHECKED. A split taxonomy that stops being
            # a partition doubles this series silently, and a doubled series
            # does not look broken -- it looks like a run with 3,604 zero-length
            # steps, which flagged 7,207 of 7,208 records as quality artifacts
            # on the first FR265 file. Fail loudly instead.
            if len(set(ts)) != len(ts):
                raise ValueError(
                    f"{aid}: {len(ts)} rows over {len(set(ts))} distinct "
                    f"timestamps -- the splits joined here are not a partition. "
                    f"Check for a new split_type that overlaps rwd_run / "
                    f"rwd_walk / rwd_stand."
                )
            lat = [r[2] for r in rows]
            lon = [r[3] for r in rows]
            # THE RUN'S projection, resolved once from ALL its positions --
            # the same call load_traversals makes, so path distance and
            # segment identity are in one frame per run.
            proj = run_projection(con, aid)
            bases[proj["basis"]] = bases.get(proj["basis"], 0) + 1
            cum, xs, ys = S.path_distance(lat, lon, crs=proj["crs"])
            elev = np.array([r[5] for r in rows], dtype=float)
            smooth = S.smooth_by_distance(elev, cum)
            blocks = S.positioned_blocks(ts, cum)
            grade = S.grade_series(smooth, cum, blocks)
            speed = [r[4] for r in rows]
            gap = S.gap_series(speed, grade)
            usable = np.zeros(len(rows), dtype=bool)
            for a, b, ok in blocks:
                if ok:
                    usable[a : b + 1] = True

            con.execute("BEGIN")
            con.execute("DELETE FROM records_enriched WHERE activity_id = ?", [aid])
            qflags = S.quality_flags(
                ts, xs, ys, speed, [r[6] for r in rows], [r[7] for r in rows]
            )
            con.executemany(
                """INSERT INTO records_enriched
                   (activity_id, timestamp, elapsed_s, path_distance_m,
                    altitude_dem_m, altitude_smooth_m, grade_pct, grade_window_m,
                    gap_mps, terrain_usable, quality_flags, calc_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        aid,
                        ts[i],
                        rows[i][1],
                        float(cum[i]),
                        float(elev[i]),
                        float(smooth[i]),
                        None if np.isnan(grade[i]) else float(grade[i]),
                        S.GRADE_WINDOW_M,
                        None if np.isnan(gap[i]) else float(gap[i]),
                        bool(usable[i]),
                        qflags[i],
                        S.CALC_VERSION,
                    )
                    for i in range(len(rows))
                ],
            )
            hills = S.detect_hills(smooth, cum)
            con.execute("DELETE FROM hill_traversals WHERE activity_id = ?", [aid])
            if hills:  # a run with no hill above threshold is normal
                con.executemany(
                    """INSERT INTO hill_traversals
                       (activity_id, hill_index, peak_distance_m,
                        peak_elevation_m, prominence_m, length_m,
                        start_distance_m, end_distance_m, peak_lat, peak_lon,
                        calc_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            aid,
                            n,
                            h["peak_distance_m"],
                            h["peak_elevation_m"],
                            h["prominence_m"],
                            h["length_m"],
                            float(cum[h["start_index"]]),
                            float(cum[h["end_index"]]),
                            lat[h["peak_index"]],
                            lon[h["peak_index"]],
                            S.CALC_VERSION,
                        )
                        for n, h in enumerate(hills)
                    ],
                )
            slopes = S.detect_slopes(smooth, cum, grade)
            con.execute("DELETE FROM slope_traversals WHERE activity_id = ?", [aid])
            if slopes:
                con.executemany(
                    """INSERT INTO slope_traversals
                       (activity_id, slope_index, direction, start_distance_m,
                        end_distance_m, length_m, vertical_m, mean_grade_pct,
                        low_elevation_m, high_elevation_m, low_lat, low_lon,
                        high_lat, high_lon, calc_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            aid,
                            n,
                            sl["direction"],
                            sl["start_distance_m"],
                            sl["end_distance_m"],
                            sl["length_m"],
                            sl["vertical_m"],
                            sl["mean_grade_pct"],
                            sl["low_elevation_m"],
                            sl["high_elevation_m"],
                            lat[sl["low_index"]],
                            lon[sl["low_index"]],
                            lat[sl["high_index"]],
                            lon[sl["high_index"]],
                            S.CALC_VERSION,
                        )
                        for n, sl in enumerate(slopes)
                    ],
                )
            con.execute("COMMIT")
            n_rec += len(rows)
            n_hill += len(hills)
            n_slope += len(slopes)
            n_grade += int((~np.isnan(grade)).sum())
        print(
            f"silver: {n_rec:,} records, {n_grade:,} with grade "
            f"({100 * n_grade / max(n_rec, 1):.0f}%), {n_slope} slope "
            f"traversals ({n_hill} prominence hills) across "
            f"{len(targets)} activities"
        )
        _print_projection_bases(bases)
    finally:
        con.close()


def _print_projection_bases(bases: dict[str, int]) -> None:
    """One line per projection basis: configured, or each derived zone.

    A derived projection is a choice the code made, so the output that
    depends on it says so rather than leaving the reader to assume a setting.
    """
    for basis, n in sorted(bases.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  projection: {basis} ({n} activit{'y' if n == 1 else 'ies'})")
    if any(b != "configured" for b in bases):
        print(
            "  projection_crs is not set, so each run is projected in the "
            "UTM zone of its own positions; runs in different zones never "
            "share a segment. Set projection_crs to fix one frame for "
            "every run."
        )


def load_traversals(con) -> list[dict]:
    """Every SLOPE traversal, enriched with the timing and HR of its crossing.

    ONE RECORDING PER RUN. Every run is recorded twice -- watch and phone,
    started and stopped together (CLAUDE.md) -- and both files produce their own
    rows. Feeding both to the matcher counts ONE crossing TWICE. That is what
    the `runs` table exists to prevent (§4), and this query originally bypassed
    it. It is worse than double counting: on one measured run the two devices
    placed the same feature up to 90 m apart, so the two copies of one
    crossing landed in DIFFERENT segments.

    The watch is chosen per run because it is the only source of heart rate,
    which segment_efforts records (§12). A documented estimator choice, not a claim that
    either device is authoritative (§14b) -- and the matcher never uses HR or
    pace, so the choice cannot bias which segments exist.

    HR is TIME-WEIGHTED (CLAUDE.md) and STOPPED TIME IS EXCLUDED from duration
    and the HR weights. Stops are structure and are never dropped from bronze
    (§4.3), but a sample taken while standing still must not carry the whole
    stop as its weight: on one real traversal that error moved avg_hr by
    double digits and understated vertical speed about twofold -- the exact
    metric §6.6 trends for fatigue.
    """
    from trail_factors.transform import matcher as M
    from trail_factors.transform.dem import run_projection

    rows = con.execute("""
        WITH ranked AS (
            SELECT a.activity_id, a.run_id, a.start_time_utc,
                   row_number() OVER (
                       PARTITION BY a.run_id
                       -- watch first; activity_id only to make it total
                       ORDER BY CASE a.source WHEN 'garmin_watch' THEN 0 ELSE 1 END,
                                a.activity_id) AS pick
            FROM activities a
        )
        SELECT s.activity_id, s.slope_index, s.direction, s.start_distance_m,
               s.end_distance_m, s.length_m, s.vertical_m, s.mean_grade_pct,
               s.low_elevation_m, s.high_elevation_m, s.low_lat, s.low_lon,
               s.high_lat, s.high_lon, ranked.start_time_utc
        FROM slope_traversals s JOIN ranked USING (activity_id)
        WHERE ranked.pick = 1
        ORDER BY ranked.start_time_utc, s.activity_id, s.slope_index
    """).fetchall()

    # Prominence peaks, per activity, for the PLACE anchor. Identity is
    # inherited from the peak because that is the one thing the matcher already
    # validates: §5.3 measured peaks reproducing to SD 0.54 m against cols at
    # SD 2.20 m. The col is derived from the hysteresis walk and never keyed on.
    #
    # ONE PROJECTION PER RUN, resolved from ALL its positions. Peaks and
    # endpoints are single points; deriving a zone from each would put the
    # two ends of one slope in different frames on a run that crosses a zone
    # line. Configured, every run resolves to the same CRS.
    projections: dict[str, dict] = {}

    def projection_of(aid_):
        if aid_ not in projections:
            projections[aid_] = run_projection(con, aid_)
        return projections[aid_]

    peaks: dict[str, list[tuple[float, float, float]]] = {}
    for aid_, plat, plon, pe in con.execute(
        "SELECT activity_id, peak_lat, peak_lon, peak_elevation_m "
        "FROM hill_traversals WHERE peak_lat IS NOT NULL"
    ).fetchall():
        px, py = M.to_utm([plat], [plon], projection_of(aid_)["crs"])
        peaks.setdefault(aid_, []).append((float(px[0]), float(py[0]), pe))

    out = []
    for (
        aid,
        idx,
        direction,
        start_d,
        end_d,
        length,
        vertical,
        grade,
        lo_e,
        hi_e,
        lo_lat,
        lo_lon,
        hi_lat,
        hi_lon,
        act_start,
    ) in rows:
        if start_d is None or end_d is None or lo_lat is None or hi_lat is None:
            continue
        span = con.execute(
            """
            SELECT e.timestamp, r.heart_rate
            FROM records_enriched e JOIN records r USING (activity_id, timestamp)
            WHERE e.activity_id = ? AND e.path_distance_m BETWEEN ? AND ?
            ORDER BY e.timestamp
        """,
            [aid, start_d, end_d],
        ).fetchall()
        if len(span) < 2:
            continue
        stops = con.execute(
            "SELECT start_utc, end_utc FROM stops WHERE activity_id = ?", [aid]
        ).fetchall()

        def moving_s(a, b, stops=stops):
            """Seconds between a and b with any stopped time removed."""
            total = (b - a).total_seconds()
            for s0, s1 in stops:
                lo, hi = max(a, s0), min(b, s1)
                if hi > lo:
                    total -= (hi - lo).total_seconds()
            return max(total, 0.0)

        t0, tN = span[0][0], span[-1][0]
        # The final sample closes on the span end rather than being dropped --
        # a left-endpoint rule with no closing interval silently discarded up
        # to 6% of a span.
        num = den = 0.0
        for i, srow in enumerate(span):
            hr = srow[1]
            if hr is None:
                continue
            nxt = span[i + 1][0] if i + 1 < len(span) else tN
            dt = moving_s(srow[0], nxt)
            num += hr * dt
            den += dt
        moving = moving_s(t0, tN)

        proj = projection_of(aid)
        lx, ly = M.to_utm([lo_lat], [lo_lon], proj["crs"])
        hx, hy = M.to_utm([hi_lat], [hi_lon], proj["crs"])
        # Snap the anchor to a prominence peak when one is close enough. A
        # slope that tops onto a plateau has no summit to inherit from, and
        # keeps its own high end -- the honest fallback (§5.4).
        place_x, place_y, place_e = float(hx[0]), float(hy[0]), hi_e
        near = [
            pk
            for pk in peaks.get(aid, [])
            if math.hypot(hx[0] - pk[0], hy[0] - pk[1]) <= M.PEAK_SNAP_M
        ]
        if near:
            place_x, place_y, place_e = min(
                near, key=lambda pk: math.hypot(hx[0] - pk[0], hy[0] - pk[1])
            )
        out.append(
            {
                "activity_id": aid,
                "slope_index": idx,
                "direction": direction,
                "lo_x": float(lx[0]),
                "lo_y": float(ly[0]),
                "hi_x": float(hx[0]),
                "hi_y": float(hy[0]),
                "mid_x": float(lx[0] + hx[0]) / 2,
                "mid_y": float(ly[0] + hy[0]) / 2,
                "place_x": place_x,
                "place_y": place_y,
                "place_elevation_m": place_e,
                # SIDE: which flank of the place this slope descends. Two flanks of
                # one peak are different segments.
                "side_bearing_deg": M.bearing_deg(
                    place_x, place_y, float(lx[0]), float(ly[0])
                ),
                "low_lat": lo_lat,
                "low_lon": lo_lon,
                "high_lat": hi_lat,
                "high_lon": hi_lon,
                # THE FRAME these x/y are in. Part of identity when derived: the
                # matcher never joins traversals from two different frames.
                "projection": proj["crs"],
                "projection_derived": not proj["configured"],
                "projection_basis": proj["basis"],
                "low_elevation_m": lo_e,
                "high_elevation_m": hi_e,
                "vertical_m": vertical,
                "length_m": length,
                "mean_grade_pct": grade,
                "start_distance_m": start_d,
                "start_utc": t0,
                "end_utc": tN,
                "duration_s": moving,
                # Vertical speed is over MOVING time, and is positive for both
                # directions -- it is a rate of vertical travel, not a signed one.
                "vertical_speed_mps": (vertical / moving) if moving > 0 else None,
                # CLAUDE.md: never `num or None` -- a genuinely zero HR sum differs
                # from no HR at all, and den == 0 is the real test.
                "avg_hr": (num / den) if den > 0 else None,
                "sort_key": act_start,
            }
        )
    return out


def match_build() -> None:
    """Milestone 5/6: build the SLOPE library from slope traversals (§12.4).

    Derived and rebuildable (§3): deletes and recomputes. Athlete-supplied
    segment names are NOT touched -- they live in data/athlete/ anchored to the
    place, not to the derived id.
    """
    import duckdb

    from trail_factors.analysis import venues as V
    from trail_factors.ingest.athlete_state import load_segment_names
    from trail_factors.transform import matcher as M

    con = duckdb.connect(str(DB_PATH))
    try:
        traversals = load_traversals(con)
        if not traversals:
            print("no slope traversals; run `silver` first")
            return
        segments = M.build_library(traversals)

        con.execute("BEGIN")
        con.execute("DELETE FROM segment_efforts")
        con.execute("DELETE FROM segments")
        con.executemany(
            """INSERT INTO segments (segment_id, low_lat, low_lon, high_lat,
                 high_lon, low_elevation_m, high_elevation_m, vertical_m,
                 length_m, mean_grade_pct, n_efforts, n_ascents, n_descents,
                 low_elevation_sd_m, high_elevation_sd_m, endpoint_spread_m,
                 first_seen_utc, calc_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    s["segment_id"],
                    s["low_lat"],
                    s["low_lon"],
                    s["high_lat"],
                    s["high_lon"],
                    s["low_elevation_m"],
                    s["high_elevation_m"],
                    s["vertical_m"],
                    s["length_m"],
                    s["mean_grade_pct"],
                    s["n_efforts"],
                    s["n_ascents"],
                    s["n_descents"],
                    s["low_elevation_sd_m"],
                    s["high_elevation_sd_m"],
                    s["endpoint_spread_m"],
                    s["first_seen_utc"],
                    M.CALC_VERSION,
                )
                for s in segments
            ],
        )
        con.executemany(
            """INSERT INTO segment_efforts (segment_id, activity_id, slope_index,
                 start_utc, end_utc, direction, low_elevation_m,
                 high_elevation_m, vertical_m, length_m, mean_grade_pct,
                 duration_s, vertical_speed_mps, avg_hr, calc_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    s["segment_id"],
                    m["activity_id"],
                    m["slope_index"],
                    m["start_utc"],
                    m["end_utc"],
                    m["direction"],
                    m["low_elevation_m"],
                    m["high_elevation_m"],
                    m["vertical_m"],
                    m["length_m"],
                    m["mean_grade_pct"],
                    m["duration_s"],
                    m["vertical_speed_mps"],
                    m["avg_hr"],
                    M.CALC_VERSION,
                )
                for s in segments
                for m in s["members"]
            ],
        )
        con.execute("COMMIT")

        # Names resolve through their RUN-ANCHORED anchors (§19.3), against
        # the efforts just written -- never through a stored coordinate.
        names = V.names_by_segment(con, load_segment_names())

        # WHERE ON THE LOOP each slope sits, keyed to the reference run. A
        # segment_id is unrecognisable on its own; "the one at 1.0 km, third
        # climb on the way out" is how the athlete identifies terrain, and
        # without it the library is hard to name.
        #
        # THE REFERENCE RUN IS CONFIGURATION (`reference_run_garmin_id`), read
        # here at call time. With none set -- or one not in this corpus --
        # there is NO reference ordering: segments are listed by effort
        # count, and the header says so rather than printing an id.
        ref_order, ref_dist, ref_rows = {}, {}, []
        ref_id = config.get().reference_run_garmin_id
        ref = None
        if ref_id is not None:
            ref = con.execute(
                "SELECT activity_id FROM activities WHERE source_file LIKE ?",
                [f"%{ref_id}%"],
            ).fetchone()
        if ref is None:
            print(
                "no reference run "
                + (
                    "is configured"
                    if ref_id is None
                    else "matches reference_run_garmin_id in this corpus"
                )
                + ": segments are listed by effort count. Set "
                "reference_run_garmin_id to order them along one run."
            )
        else:
            by_key = {
                (m["activity_id"], m["slope_index"]): s
                for s in segments
                for m in s["members"]
            }
            for aid, idx, sd, direction in con.execute(
                "SELECT activity_id, slope_index, start_distance_m, direction "
                "FROM slope_traversals WHERE activity_id = ? "
                "ORDER BY start_distance_m",
                [ref[0]],
            ).fetchall():
                seg = by_key.get((aid, idx))
                if seg is None:
                    continue
                ref_rows.append((len(ref_rows) + 1, sd, direction, seg))
                ref_order.setdefault(seg["segment_id"], len(ref_rows))
                ref_dist.setdefault(seg["segment_id"], sd)

        print(f"segments: {len(segments)} slopes from {len(traversals)} traversals")
        bases: dict[str, int] = {}
        for _, basis in {
            (tr["activity_id"], tr["projection_basis"]) for tr in traversals
        }:
            bases[basis] = bases.get(basis, 0) + 1
        _print_projection_bases(bases)
        print()
        print(
            f"  {'segment':<14}{'name':<16}{'ref#':>5}{'ref_km':>8}{'up':>4}"
            f"{'down':>6}{'vert_m':>8}{'len_m':>8}{'grade':>7}"
            f"{'loSD':>6}{'hiSD':>6}"
        )

        def place(s):
            return (ref_order.get(s["segment_id"], 10**6), -s["n_efforts"])

        for s in sorted(segments, key=place):
            o = ref_order.get(s["segment_id"])
            d = ref_dist.get(s["segment_id"])
            print(
                f"  {s['segment_id']:<14}{names.get(s['segment_id'], '')[:15]:<16}"
                f"{(str(o) if o else '-'):>5}"
                f"{(f'{d / 1000:.2f}' if d is not None else '-'):>8}"
                f"{s['n_ascents']:>4}{s['n_descents']:>6}"
                f"{s['vertical_m']:>8.1f}{s['length_m']:>8.0f}"
                f"{s['mean_grade_pct']:>6.1f}%"
                f"{s['low_elevation_sd_m']:>6.2f}{s['high_elevation_sd_m']:>6.2f}"
            )

        if ref_rows:
            distinct = len({s["segment_id"] for _, _, _, s in ref_rows})
            print(
                f"\n  REFERENCE RUN {ref_id} -- {len(ref_rows)} slope "
                f"traversals of {distinct} distinct slopes, in order:"
            )
            print(
                f"    {'#':>2} {'km':>6} {'mi':>6}  {'dir':<8}{'segment':<14}"
                f"{'vert_m':>7}{'len_m':>7}{'grade':>7}  name"
            )
            seen = {}
            for n, sd, direction, s in ref_rows:
                sid = s["segment_id"]
                again = f"  (again: #{seen[sid]})" if sid in seen else ""
                seen.setdefault(sid, n)
                print(
                    f"    {n:>2} {sd / 1000:>6.2f} {sd / 1609.34:>6.2f}  "
                    f"{direction:<8}{sid:<14}{s['vertical_m']:>7.1f}"
                    f"{s['length_m']:>7.0f}{s['mean_grade_pct']:>6.1f}%"
                    f"  {names.get(sid, '')}{again}"
                )
            print(
                "\n    name them with:  uv run trail-factors "
                "name-segment <segment> <name>"
            )

        bad = M.consistency_failures(segments)
        print()
        if bad:
            print(
                f"  CONSISTENCY FAILURES ({len(bad)}): endpoint elevations "
                f"disagree by more than {M.MAX_SEGMENT_ELEV_SD_M} m -- "
                f"these have probably merged two slopes (§12.3):"
            )
            for s in bad:
                print(
                    f"    {s['segment_id']}  low SD {s['low_elevation_sd_m']:.2f} m, "
                    f"high SD {s['high_elevation_sd_m']:.2f} m "
                    f"over {s['n_efforts']} efforts"
                )
        else:
            print(
                f"  consistency: all segments within "
                f"{M.MAX_SEGMENT_ELEV_SD_M} m endpoint-elevation SD"
            )
    finally:
        con.close()


def name_segment(segment_id: str, name: str) -> None:
    """Athlete-supplied segment name (§14). Lives outside the rebuild path.

    Stored against a RUN-ANCHORED anchor, never the segment_id and never a
    coordinate (§19.3): the anchor resolves to whichever segment holds that
    traversal after any re-founding. Renaming keeps the existing anchor.
    """
    import datetime

    import duckdb

    from trail_factors.analysis import venues as V
    from trail_factors.ingest.athlete_state import load_segment_names, save_segment_name

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        row = con.execute(
            "SELECT n_efforts FROM segments WHERE segment_id = ?", [segment_id]
        ).fetchone()
        if row is None:
            raise SystemExit(f"no segment {segment_id}; run `match` first")
        previous = next(
            (
                e
                for e in load_segment_names()
                if V.resolve_anchor(con, e["anchor"])["segment_id"] == segment_id
            ),
            None,
        )
        anchor = (
            previous["anchor"] if previous else V.anchor_for_segment(con, segment_id)
        )
    finally:
        con.close()
    entry = save_segment_name(
        name, anchor, datetime.datetime.now(datetime.UTC), previous=previous
    )
    print(
        f"named {segment_id} -> {name!r} ({row[0]} efforts, revision "
        f"{entry['revision']})"
    )
    print(
        f"  anchored to run {anchor['run_id']}, {anchor['direction']} at "
        f"{anchor['at_distance_m'] / 1609.34:.2f} mi -- survives a rebuild "
        f"and a backfill, and stores no coordinate"
    )


def venue_anchor(venue: str, segment_id: str) -> None:
    """Add `segment_id` as an anchor of `venue` (§19), run-anchored.

    REFUSED when the segment already anchors ANOTHER venue: one slope cannot
    decide between two venues, and the store will not hold the ambiguity.
    """
    import datetime

    import duckdb

    from trail_factors.analysis import venues as V
    from trail_factors.ingest.athlete_state import load_venues, save_venue_anchor

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        for v in load_venues():
            for a in v["anchors"]:
                if V.resolve_anchor(con, a)["segment_id"] == segment_id:
                    raise SystemExit(
                        f"segment {segment_id} is already an anchor of "
                        f"{v['name']!r}"
                        + ("" if v["name"] != venue else "; nothing to add")
                    )
        anchor = V.anchor_for_segment(con, segment_id)
    finally:
        con.close()
    entry = save_venue_anchor(venue, anchor, datetime.datetime.now(datetime.UTC))
    print(
        f"{venue!r}: anchor {len(entry['anchors'])} = segment {segment_id} "
        f"(run {anchor['run_id']}, {anchor['direction']} at "
        f"{anchor['at_distance_m'] / 1609.34:.2f} mi), revision "
        f"{entry['revision']}"
    )


def venue_candidates(cluster: str) -> None:
    """PRINT candidate anchors for a cluster's likely venues (§19). Writes
    nothing: the athlete names a venue and its anchors in plain words, and
    only then does `venue-anchor` store them."""
    import duckdb

    from trail_factors.analysis import venues as V

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        out = V.candidates(con, cluster)
    finally:
        con.close()
    print(
        f"CANDIDATE ANCHORS, {cluster}: {out['n_runs']} runs with segment "
        f"efforts. Nothing is written."
    )
    print(
        f"  A candidate is a segment crossed by >= {out['min_runs']} runs; "
        f"segments whose crossing runs overlap >= {out['overlap']:.0%} are "
        f"grouped as one LIKELY venue."
    )
    print(
        "  'steepness' is the segment's own climb; 'runs climb' is the "
        "median whole-run climbing of the runs that cross it.\n"
    )
    for i, g in enumerate(out["groups"], 1):
        lo, hi = g["runs_climb_range_ft_per_mi"]
        print(
            f"LIKELY VENUE {i}: {g['n_runs']} runs, {g['n_segments']} "
            f"candidate(s); its runs climb a median "
            f"{g['runs_climb_ft_per_mi']:.0f} ft/mi (range {lo:.0f}-{hi:.0f})"
        )
        for c in g["candidates"]:
            ref = c["reference"]
            print(
                f"  {c['segment_id']}  crossed by {c['n_runs']}/"
                f"{c['n_runs_in_group']} runs  |  {c['vertical_ft']:.0f} ft "
                f"over {c['length_mi']:.2f} mi, steepness "
                f"{c['ft_per_mi']:.0f} ft/mi ({c['grade_pct']:.1f}%)  |  "
                f"runs climb {c['runs_climb_ft_per_mi']:.0f} ft/mi"
            )
            print(
                f"      reference: run {ref['run_id']}, {ref['direction']} "
                f"at {ref['at_mi']:.2f} mi (spans {ref['span_mi'][0]:.2f}-"
                f"{ref['span_mi'][1]:.2f} mi); {c['n_ascents']} ascents, "
                f"{c['n_descents']} descents in the library"
            )
            print("      runs: " + ", ".join(r["run_id"] for r in c["runs"]))
        print()
    print(f"{out['n_runs_without_a_candidate']} run(s) cross no candidate.")
    print("To store one: uv run trail-factors venue-anchor '<venue>' <segment-id>")


def venues_cmd() -> None:
    """Every venue, how its anchors resolve, and how many runs it holds."""
    import duckdb

    from trail_factors.analysis import venues as V

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        out = V.assign_all(con)
    finally:
        con.close()
    print(f"{out['n_venues']} venue(s) in {display_path(ATHLETE_DIR / 'venues.json')}")
    for r in out["anchors"]:
        a = r["anchor"]
        print(
            f"  {r['venue']:<20} run {a['run_id']} {a['direction']:<8}"
            f"{a['at_distance_m'] / 1609.34:>6.2f} mi -> "
            f"{r['segment_id'] or r['status']}"
        )
    for s, vs in out["shared_anchor_segments"].items():
        print(f"  REFUSED: segment {s} anchors {', '.join(vs)}; it decides neither")
    counts: dict = {}
    for a in out["by_run"].values():
        k = a["venue"] if a["state"] == V.ASSIGNED else a["state"]
        counts[k] = counts.get(k, 0) + 1
    print("\nruns by venue:")
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<24}{n:>4}")


def retile() -> None:
    """Re-run dem -> silver -> match for activities a NEW TILE now covers.

    ONE COMMAND, NOT A FULL REBUILD. Adding a tile only ever makes previously
    uncovered activities computable; it changes nothing about the ones already
    enriched. Re-ingesting the corpus to pick up a tile would rebuild 28,000
    records to add a few thousand, and a rebuild that touches more than it
    needs to is a rebuild someone eventually avoids running.

    The matcher IS run corpus-wide, and that is not an oversight: the segment
    library is a global object, and a new trail's places must be created in the
    same pass that could match them to existing ones. It is the only step here
    that cannot be scoped to the new activities.
    """
    import duckdb

    from trail_factors.transform.dem import TileSet

    tiles = TileSet()
    print(f"tiles loaded: {', '.join(tiles.names) or 'NONE'}")
    if not tiles:
        print(f"nothing to do: {display_path(DEM_DIR)} holds no tiles")
        return

    con = duckdb.connect(str(DB_PATH))
    try:
        # THE SAME RUNNING-ONLY HOLD `dem_populate` CARRIES, for the same
        # reason. Without it `fetch-tile` would end by walking every newly
        # covered cycling activity through dem -> silver -> match and into the
        # segment library -- deciding how a bike ride is used by not thinking
        # about it, which is the decision §4.1 explicitly deferred. A HOLD,
        # not a rule: it restores the coverage the derived layers had before
        # the backlog landed and is meant to be replaced by a real policy.
        candidates = [
            r[0]
            for r in con.execute("""
            SELECT DISTINCT r.activity_id FROM records r
            JOIN activities a USING (activity_id)
            WHERE r.lat IS NOT NULL AND a.sport = 'running' AND NOT EXISTS (
                SELECT 1 FROM dem_elevations d
                WHERE d.activity_id = r.activity_id)
        """).fetchall()
        ]
        newly = []
        for aid in candidates:
            rows = con.execute(
                """
                SELECT lat, lon FROM records
                WHERE activity_id = ? AND lat IS NOT NULL""",
                [aid],
            ).fetchall()
            cov = tiles.coverage([r[0] for r in rows], [r[1] for r in rows])
            if not cov["missing_tiles"]:
                newly.append(aid)
    finally:
        con.close()

    if not newly:
        print(
            f"{len(candidates)} activities still have no DEM elevations, and "
            f"no loaded tile covers any of them. Nothing to re-run."
        )
        return

    print(f"{len(newly)} activities are now covered and will be rebuilt:")
    for aid in newly:
        print(f"  {aid}")
    for aid in newly:
        dem_populate(aid)
        silver_build(aid)
    # CORPUS-WIDE BY NECESSITY: the library is global (see the docstring).
    match_build()
    # RECOVERY MUST FOLLOW MATCH (CLAUDE.md): it reads segment_efforts, and
    # leaving it out is how segment_recovery went stale while
    # segment_efforts was current -- delta_hr then reported "no efforts" on
    # a run with 14. `sync` runs it; retile did not, and a new tile is
    # exactly when new efforts appear.
    from trail_factors.cli.analysis import recovery_build

    recovery_build()
    print("done. Re-run `report <run>` to see the terrain primitives.")


def _zone_of_cell(cell: dict) -> tuple[int, bool] | None:
    """(zone, is_north) a missing cell was labelled in; None if configured.

    THE HEMISPHERE IS RETURNED, NOT DROPPED. A southern cell's zone number
    alone would be resolved in the northern grid of the same number -- the
    northern mirror of its ground, which can be US territory and pass every
    guard. `derived_zone` refuses a southern zone; that needs to know it is
    one.
    """
    if cell.get("projection_configured"):
        return None
    from trail_factors.transform.dem import utm_zone_of

    return utm_zone_of(cell["grid_crs"])


def corpus_missing_cells() -> list[dict]:
    """The refusal list, and NOTHING ELSE.

    `--missing` fetches what the corpus has already refused to compute and no
    more. This reads the same coverage call `dem` prints, so the set of cells
    is exactly the set the athlete has already been shown -- it cannot widen
    to "tiles near where you run", which would be a different act with a
    different disclosure. A run whose elevation is already in the database
    has refused nothing, whatever tiles are on disk, so it adds no cell.
    """
    import collections

    import duckdb

    from trail_factors.analysis.basic import db_elevation
    from trail_factors.transform.dem import TileSet

    tiles = TileSet()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        acts = [
            r[0]
            for r in con.execute("""
            SELECT DISTINCT r.activity_id FROM records r
            WHERE r.lat IS NOT NULL""").fetchall()
        ]
        # KEYED BY (grid, cell), not the cell name alone. With no projection
        # configured each run's cells are labelled in its OWN derived zone,
        # and the same x/y in two zones is two different pieces of ground.
        cells: dict[tuple[str, str], dict] = {}
        counts: collections.Counter = collections.Counter()
        for aid in acts:
            if db_elevation(con, aid)["complete"]:
                continue
            pts = con.execute(
                """
                SELECT lat, lon FROM records
                WHERE activity_id = ? AND lat IS NOT NULL""",
                [aid],
            ).fetchall()
            cov = tiles.coverage([p[0] for p in pts], [p[1] for p in pts])
            for m in cov["missing_tiles"]:
                key = (cov["crs"], m["usgs_cell"])
                cells.setdefault(
                    key,
                    {
                        **m,
                        "grid_crs": cov["crs"],
                        "projection_configured": cov["projection_configured"],
                    },
                )
                counts[key] += 1
    finally:
        con.close()
    for key, cell in cells.items():
        cell["n_activities"] = counts[key]
    return sorted(cells.values(), key=lambda c: -c["n_activities"])


def fetch_tile(
    name: str | None = None,
    missing: bool = False,
    yes: bool = False,
    zone: int | None = None,
) -> None:
    """Resolve a DEM tile against the USGS 3DEP index and download it (§5a.1).

    THE SUFFIX IS RESOLVED, NEVER CONSTRUCTED. A filename's suffix is an
    acquisition project name, not a function of position, and outside the
    configured projection's UTM zone even the x/y cell is named differently.
    The index is asked what actually covers the cell's ground.

    `zone` is the UTM zone a cell name was computed in, needed only when no
    projection_crs is configured; coverage's fetch_command passes it as
    `--zone NN`. A configured projection always wins.
    """
    from trail_factors.transform import dem_fetch as F
    from trail_factors.transform.dem import TileSet

    if missing == bool(name):
        raise SystemExit("give a tile name (as `dem` reports it) or --missing")

    if missing:
        cells = corpus_missing_cells()
        if not cells:
            print(
                "no cell is missing: every activity with position is "
                "covered by a loaded tile. Nothing to fetch."
            )
            return
        print(f"the refusal list names {len(cells)} cells:")
        targets = []
        for c in cells:
            # Coverage already decided there is no USGS grid for this cell
            # and said why; nothing is resolved or sent for it.
            if c["fetch_command"] is None:
                print(
                    f"  {c['usgs_cell']} in {c['grid_crs']}: REFUSED — "
                    f"{c['fetch_unavailable_reason']}"
                )
                continue
            targets.append((c["usgs_cell"], c["n_activities"], _zone_of_cell(c)))
    else:
        # `--zone NN` is the northern zone coverage's fetch_command printed;
        # it only ever prints one for a zone that HAS a USGS grid, and every
        # such zone is northern.
        targets = [(name, None, (zone, True) if zone is not None else None)]

    plan, refused = [], []
    for cell_name, n_acts, cell_zone in targets:
        try:
            tx, ty = F.parse_cell(cell_name)
            with F.derived_zone(
                *(cell_zone if cell_zone is not None else (None, None))
            ):
                products = F.resolve(tx, ty)
        except (F.NotInTheUS, F.NoProductCovers, ValueError) as exc:
            refused.append((cell_name, str(exc)))
            print(f"  {cell_name}: REFUSED — {exc}")
            continue
        best = products[0]
        plan.append((cell_name, (tx, ty), best, products, cell_zone))
        held = best["filename"] in set(TileSet().names)
        suffix = f", needed by {n_acts} activities" if n_acts else ""
        print(
            f"  {cell_name} -> {best['filename']}  "
            f"{best['size_bytes'] / 1e6:,.0f} MB  [{best['region']}]{suffix}"
            f"{'  (already on disk)' if held else ''}"
        )
        for alt in products[1:]:
            print(
                f"      also available: {alt['filename']} "
                f"({alt['size_bytes'] / 1e6:,.0f} MB) — not chosen; the "
                f"largest product for a cell is the one with real data in it"
            )

    if not plan:
        print("nothing to fetch.")
        return

    total = sum(p[2]["size_bytes"] for p in plan)
    print(f"\n{len(plan)} tile(s), {total / 1e9:.2f} GB total")
    if missing and not yes and total > FETCH_CONFIRM_BYTES:
        print(
            f"that is over {FETCH_CONFIRM_BYTES / 1e9:.1f} GB. Re-run with "
            f"--yes to download, or name a single cell to fetch just that "
            f"one. Nothing was downloaded."
        )
        return

    fetched = []
    for _cell_name, cell, best, _, cell_zone in plan:
        print(f"\n{best['filename']}")
        try:
            with F.derived_zone(
                *(cell_zone if cell_zone is not None else (None, None))
            ):
                res = F.download(best, cell=cell, progress=print)
        except F.NotAGeoTIFF as exc:
            print(f"  DISCARDED — {exc}")
            continue
        except F.OffAllowlist as exc:
            print(f"  REFUSED — {exc}")
            continue
        mark = "already present" if res["status"] == "already_present" else "OK"
        print(
            f"  {mark}: {res['size_bytes'] / 1e6:,.1f} MB, "
            f"{res['width']}x{res['height']} at {res['pixel_m']:.0f} m, "
            f"{res['crs']}"
        )
        if res.get("index_size_agrees") is False:
            print(
                f"  note: the 3DEP index declares "
                f"{res['index_declared_bytes']:,} bytes; the server served "
                f"{res['served_bytes']:,} and all of them arrived. The index "
                f"is stale for this object, not the download."
            )
        if res["status"] == "fetched":
            fetched.append(res["path"].name)

    if not fetched:
        print("\nno new tile landed; not re-running retile.")
        return
    print(f"\n{len(fetched)} new tile(s) in {display_path(DEM_DIR)}. Running retile.\n")
    retile()
