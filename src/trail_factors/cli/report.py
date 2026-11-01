"""Reporting and the prompted sync workflow (SPEC.md §12)."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trail_factors import config
from trail_factors.ingest.load import DB_PATH
from trail_factors.paths import display_path

from trail_factors.cli.athlete import advice_lines, outstanding_confirmations
from trail_factors.cli.common import TABLES
from trail_factors.cli.garmin import RAW_GARMIN, already_downloaded, garmin_download_all
from trail_factors.cli.ingest import ingest_all, rescue_line
from trail_factors.cli.weather import weather_build


def verify(db_path: Path = DB_PATH) -> None:
    """Per-activity report. Never aggregate across activities: they are
    different devices at different sampling rates (§9)."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        print("=" * 68)
        print("RUNS  (the grain coaching reads — volume aggregates go here)")
        print("=" * 68)
        for run_id, disp, start, n, srcs in con.execute("""
            SELECT r.run_id, r.local_start_display, r.start_time_utc,
                   count(a.activity_id), string_agg(a.source, ' + ' ORDER BY a.source)
            FROM runs r LEFT JOIN activities a ON a.run_id = r.run_id
            GROUP BY 1,2,3 ORDER BY 3
        """).fetchall():
            print(f"  {run_id}   local {disp}")
            print(f"    sources: {n}  ({srcs})")
        print()

        acts = con.execute("""
            SELECT activity_id, source, sport, start_time_utc, local_tz,
                   duration_s, total_distance_m, sampling_regime, median_gap_s
            FROM activities ORDER BY source
        """).fetchall()
        for (aid, src, sport, start, tz, dur, dist, regime, gap) in acts:
            print("=" * 68)
            print(f"{src}   activity_id {aid}")
            print("=" * 68)
            print(f"  start (UTC)       {start}")
            print(f"  local tz offset   UTC{tz:+.0f}   sport {sport}")
            print(f"  duration          {dur:,.1f} s  ({dur/60:.1f} min)")
            print(f"  total distance    {dist:,.2f} m  ({dist/1609.344:.2f} mi)")
            print(f"  median record gap {gap:.2f} s  ->  sampling_regime {regime}")

            # NO COORDINATES IN TERMINAL OUTPUT. This used to print the lat/lon
            # range and the first records at 6 dp -- a pasted log would place
            # the run to the metre. Coverage answers the question `verify` is
            # for ("did the positions arrive?") without saying where they are.
            n, hr, n_pos, dmax = con.execute("""
                SELECT count(*), avg(heart_rate),
                       count(*) FILTER (WHERE lat IS NOT NULL AND lon IS NOT NULL),
                       max(distance_m)
                FROM records WHERE activity_id = ?
            """, [aid]).fetchone()
            hr_txt = f"{hr:.1f} bpm" if hr is not None else "none recorded"
            print(f"  record count      {n:,}")
            print(f"  avg heart rate    {hr_txt}   (UNWEIGHTED — see §9)")
            # A GPS-less run (treadmill, §8) has no position at all, and must
            # read as that rather than crash: a crash here is invisible when
            # the command is read through a pager, which masks the exit code.
            if not n_pos:
                print("  positioned        none recorded (no GPS on this run)")
            else:
                print(f"  positioned        {n_pos:,} of {n:,} records "
                      f"({n_pos / n * 100:.1f}%)")
            dmax_txt = f"{dmax:,.2f}" if dmax is not None else "none recorded"
            print(f"  max distance_m    {dmax_txt}")
            for tbl in ("laps", "splits"):
                c = con.execute(
                    f"SELECT count(*) FROM {tbl} WHERE activity_id = ?", [aid]
                ).fetchone()[0]
                print(f"  {tbl:<16}  {c:,}")

            # Position is shown as present/absent only (see above).
            cols = ["elapsed_s", "altitude_device_m", "distance_m",
                    "heart_rate", "cadence_raw_rpm", "speed_mps", "gps_accuracy"]
            print("  first 5 records:")
            print("    " + "".join(f"{c:>18}" for c in cols))
            for r in con.execute(f"""
                SELECT {','.join(cols)} FROM records WHERE activity_id = ?
                ORDER BY timestamp LIMIT 5
            """, [aid]).fetchall():
                print("    " + "".join(
                    f"{'NULL':>18}" if v is None
                    else (f"{v:>18.2f}" if isinstance(v, float) else f"{v:>18}")
                    for v in r))
            print()

        print("=" * 68)
        print("TABLE TOTALS")
        print("=" * 68)
        for tb in TABLES:
            c = con.execute(f"SELECT count(*) FROM {tb}").fetchone()[0]
            print(f"  {tb:<16} {c:>7,}")
    finally:
        con.close()

def _haversine_km(a1, o1, a2, o2):
    import math
    p1, p2 = math.radians(a1), math.radians(a2)
    dp, dl = p2 - p1, math.radians(o2 - o1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))

def assign_cluster(lat, lon):
    """The named training area a position falls in (§16.2 comparison sets).

    CLUSTERS ARE ATHLETE CONFIGURATION, read at CALL time from
    `config/athlete.yaml` (`clusters:`), never a constant in code: they are
    where someone lives and trains. With none configured every positioned
    run is "OTHER".

    "OTHER" CARRIES NO COORDINATE. It used to be `OTHER(lat,lon)` at 2 dp,
    and through `report.cluster_of` that position reached the report and the
    MCP payload -- the one place position is never emitted.
    """
    if lat is None or lon is None:
        return "NO_GPS"
    clusters = config.get().clusters
    if not clusters:
        return "OTHER"
    best = min(((c.name, _haversine_km(lat, lon, c.lat, c.lon), c.radius_km)
                for c in clusters), key=lambda x: x[1])
    return best[0] if best[1] <= best[2] else "OTHER"

def coverage_report(src: Path = RAW_GARMIN) -> None:
    """Screen every downloaded FIT for real GPS coverage. Reads files, not the DB."""
    import statistics
    from datetime import date, timedelta

    from trail_factors.ingest import fit_file as ff
    from trail_factors.ingest.garmin_api import client, list_activities

    api_by_id = {}
    try:
        api = client()
        end = date.today()
        for a in list_activities(api, (end - timedelta(days=400)).isoformat(), end.isoformat()):
            api_by_id[str(a["activityId"])] = a
    except Exception as exc:
        print(f"  (API listing unavailable: {exc}; skipping startLatitude comparison)\n")

    rows = []
    for path in sorted(src.glob("*.fit")):
        aid = path.name.split("_")[0]
        msgs = ff.read_messages(path)
        recs = ff.coalesce_records(msgs.get("record", []))
        cov = ff.gps_coverage(recs)
        regime, gap = ff.detect_sampling_regime(recs)
        first = ff.first_positioned(recs)
        meta = api_by_id.get(aid, {})
        api_lat, api_lon = meta.get("startLatitude"), meta.get("startLongitude")
        drift = (_haversine_km(first[0], first[1], api_lat, api_lon) * 1000
                 if first and api_lat is not None else None)
        pos = [(ff.semicircles_to_degrees(r["position_lat"]),
                ff.semicircles_to_degrees(r["position_long"]))
               for r in recs if r.get("position_lat") is not None]
        centre = ((statistics.median([p[0] for p in pos]), statistics.median([p[1] for p in pos]))
                  if pos else (None, None))
        rows.append(dict(aid=aid, local=meta.get("startTimeLocal"), cov=cov, regime=regime,
                         gap=gap, first=first, drift=drift,
                         cluster=assign_cluster(*centre),
                         dist_km=(meta["distance"] / 1000
                                  if meta.get("distance") is not None else None)))

    rows.sort(key=lambda r: r["local"] or "")
    print("=" * 118)
    print(f"{'activity':<12}{'local start':<20}{'km':>6}{'recs':>6}{'pos':>6}{'cov%':>6}"
          f"{'contig':>7}{'isl':>4}{'gps/total_s':>14}{'fix@s':>7}{'drift_m':>9}{'regime':>16}{'cluster':>12}")
    print("=" * 118)
    for r in rows:
        c = r["cov"]
        km = f"{r['dist_km']:.1f}" if r["dist_km"] is not None else "-"
        cov = (f"{c['coverage_fraction'] * 100:.0f}"
               if c["coverage_fraction"] is not None else "-")
        fix = (f"{c['first_fix_offset_s']:.0f}"
               if c["first_fix_offset_s"] is not None else "-")
        drift = f"{r['drift']:.0f}" if r["drift"] is not None else "-"
        print(f"{r['aid']:<12}{str(r['local'])[:19]:<20}{km:>6}"
              f"{c['records']:>6}{c['positioned']:>6}{cov:>6}"
              f"{('yes' if c['contiguous'] else 'no'):>7}{c['islands']:>4}"
              f"{c['gps_window_s']:>7.0f}/{c['total_elapsed_s']:<6.0f}"
              f"{fix:>7}{drift:>9}"
              f"{r['regime']:>16}{r['cluster']:>12}")
    return rows

def validate_cmd(refresh: bool = False, verbose: bool = True) -> None:
    """Run the gold-layer pass and print the full validation report.

    Bronze and silver run inline at ingest (they gate whether a file is
    written). Gold runs here because it compares each run against the rest of
    the corpus — inline it would depend on ingest order (§3).
    """
    from trail_factors.ingest import validate as V
    from trail_factors.ingest.garmin_api import cache_listing, client, load_cached_listing

    if refresh:
        path = cache_listing(client())
        print(f"cached API oracle -> {display_path(path)}\n")

    con = duckdb.connect(str(DB_PATH))
    try:
        findings = V.gold_checks(con)

        oracle = load_cached_listing()
        if not oracle:
            print("  (no cached API listing; run `validate --refresh` for oracle checks)\n")
        else:
            for aid, fh, rid, src_file, dist, dur in con.execute("""
                SELECT activity_id, file_hash, run_id, source_file,
                       total_distance_m, timer_time_s
                FROM activities WHERE source = 'garmin_watch'
            """).fetchall():
                gid = Path(src_file).name.split("_")[0]
                api = oracle.get(gid)
                if not api:
                    continue
                for f in V.oracle_checks(
                        {"total_distance_m": dist, "timer_time_s": dur}, api):
                    findings.append((fh, aid, rid, f))
        V.persist_gold(con, findings)

        print("=" * 92)
        print("VALIDATION REPORT")
        print("=" * 92)
        for sev in ("fatal", "warning", "info"):
            rows = con.execute("""
                SELECT layer, check_name, count(*)
                FROM validation_results WHERE severity = ?
                GROUP BY 1, 2 ORDER BY 3 DESC
            """, [sev]).fetchall()
            total = sum(r[2] for r in rows)
            print(f"\n{sev.upper()}  ({total})")
            for layer, check, n in rows:
                print(f"  {layer:<8}{check:<28}{n:>5}")

        if not verbose:
            return
        print("\n" + "=" * 92)
        print("FATAL — files rejected, no rows written")
        print("=" * 92)
        for fh, msg in con.execute("""
            SELECT file_sha256, message FROM validation_results
            WHERE severity = 'fatal' ORDER BY file_sha256
        """).fetchall():
            print(f"  {fh[:12]}  {msg[:100]}")

        print("\n" + "=" * 92)
        print("WARNING — ingested, and EXCLUDED from matching analyses per §15.4")
        print("=" * 92)
        for aid, check, msg, obs in con.execute("""
            SELECT coalesce(activity_id, '(rejected)'), check_name, message, value_observed
            FROM validation_results WHERE severity = 'warning'
            ORDER BY check_name, value_observed
        """).fetchall():
            v = f"{obs:.3f}" if obs is not None else "-"
            print(f"  {aid:<18}{check:<26}{v:>9}  {msg[:60]}")
    finally:
        con.close()

def sync(days: int | None = None) -> None:
    """One command: fetch new activities, ingest, validate, report in plain language.

    Works identically for one run or five — the filesystem is the record of
    what is already held, so there is no date tracking to drift (§12).

    THE WINDOW DEFAULTS TO THE WHOLE ACCOUNT (§4.1). It was 400 days, which
    silently made the corpus's reach a property of how recently `sync` was
    first run: every run older than the window had never been fetched and
    nothing said so. Skip-if-present makes a full enumeration cheap -- one
    extra list call per sync -- and it is the only version that cannot
    quietly leave history behind.
    """
    dest = RAW_GARMIN
    before = already_downloaded(dest)

    print("Checking Garmin for new activities...\n")
    garmin_download_all(days, dest)
    new_ids = sorted(already_downloaded(dest) - before)

    print("\n" + "-" * 66)
    if new_ids:
        print(f"Downloaded {len(new_ids)} new activit"
              f"{'y' if len(new_ids) == 1 else 'ies'}: {', '.join(new_ids)}")
    else:
        print("No new activities on Garmin - everything is already downloaded.")
    print("-" * 66 + "\n")

    ingest_result = ingest_all()
    print()
    # Weather is skip-if-cached, so this is safe to run every sync and costs
    # one request per genuinely new activity. §8 wants it on every outdoor run,
    # and fetching it here means a gap is noticed the same day rather than
    # months later when a temperature-aware model quietly drops the run.
    try:
        weather_build()
    except Exception as exc:            # never let weather block the report
        print(f"  weather step failed: {type(exc).__name__}: {exc}")
    print()
    validate_cmd(verbose=False)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        print("\n" + "=" * 66)
        print("WHAT THIS MEANS")
        print("=" * 66)

        if new_ids:
            rows = con.execute("""
                SELECT regexp_extract(source_file, '([0-9]+)_ACTIVITY', 1),
                       start_time_utc, total_distance_m / 1000.0,
                       moving_time_s / 60.0, coverage_fraction
                FROM activities WHERE source = 'garmin_watch'
                ORDER BY start_time_utc DESC LIMIT ?
            """, [len(new_ids)]).fetchall()
            for gid, start, km, mins, cov in rows:
                if gid in new_ids:
                    km_txt = f"{km:.2f}" if km is not None else "-"
                    min_txt = f"{mins:.0f}" if mins is not None else "-"
                    cov_txt = f"{cov * 100:.0f}%" if cov is not None else "-"
                    print(f"  {gid}  {start}  {km_txt} km in {min_txt} min"
                          f"  GPS {cov_txt}")
            print()

        # THE END OF THE BAD-DATA LOOP IS ANNOUNCED. A re-sync after
        # confirm-file downloads nothing, so the new-run lines above stay
        # empty and the rescued run used to arrive in silence -- no line, no
        # report. It is named here with its trimmed figures and gets its
        # report below.
        rescued = ingest_result.get("rescued", [])
        if rescued:
            print(f"{len(rescued)} previously rejected file(s) INGESTED:")
            for r in rescued:
                print(f"  {r['ref']}  {rescue_line(r)}")
            print()
        if ingest_result.get("rescued_unknown"):
            print(f"  (earlier rejections could not be read, so a rescued "
                  f"file may not be named above: "
                  f"{ingest_result['rescued_unknown']})\n")

        pending = outstanding_confirmations(con)
        if pending:
            print(f"NEEDS YOUR CONFIRMATION ({len(pending)}):")
            for aid, src, check, obs, msg in pending:
                gid = Path(src).name.split("_")[0]
                what = ("no movement at the END" if check == "trailing_stop"
                        else "no movement at the START")
                print(f"\n  {gid}: {obs:.0f} s of {what}.")
                print(f"    Forgotten stop, or a deliberate pause?")
                print(f"    If forgotten:  uv run trail-factors confirm-window {gid}")
                print(f"    If deliberate: leave it - elapsed time counts (§4.3)")
        else:
            print("Nothing needs your confirmation.")

        # EVERY REJECTION NAMES ITS REASON AND ITS NEXT STEP. This used to
        # close with "Corrupt beyond use; nothing to do", which is false of a
        # run whose tail is a car ride -- the case one confirmed window fixes.
        #
        # SCOPED TO THIS SYNC'S FILES when it downloaded any, as the sync job
        # is: re-raising every historical rejection on every sync is a report
        # nobody reads. Older ones are counted and pointed at rejected-files.
        advice = ingest_result.get("advice", [])
        this_sync = [a for a in advice
                     if not new_ids or a["file"].split("_")[0] in new_ids]
        older = len(advice) - len(this_sync)
        if this_sync:
            print(f"\n{len(this_sync)} file(s) REJECTED and not ingested:")
            for a in this_sync:
                print(f"\n  {a['ref']}  ({a['file']})")
                print("\n".join(advice_lines(a)))
        if advice:
            print("\n" + (f"{older} older rejection(s) are not repeated here. "
                     if older else "")
                  + f"Everything still outstanding: {advice[0]['list_command']}")

        newflag = con.execute("""
            SELECT check_name, count(*) FROM validation_results
            WHERE severity = 'warning'
              AND check_name NOT IN ('constant_column', 'gps_coverage',
                                     'gps_scattered', 'trailing_stop', 'leading_stop')
            GROUP BY 1 ORDER BY 2 DESC
        """).fetchall()
        if newflag:
            print("\nOther warnings (expected on this corpus, not new problems):")
            for name, n in newflag:
                print(f"  {name}: {n}")

        # END WITH THE §16.2 REPORT for each new run, so "finished a run"
        # produces the thing the coaching chat reads in ONE step rather than
        # requiring a second command nobody remembers (§12: the CLI is the
        # mechanism, the conversation is the interface). A RESCUED run is new
        # to the corpus too, and gets its report the same way.
        to_report = []
        for gid in new_ids:
            row = con.execute(
                "SELECT activity_id FROM activities WHERE source_file LIKE ?",
                [f"%{gid}_ACTIVITY%"]).fetchone()
            if row is not None:
                to_report.append((gid, row[0]))
        for r in rescued:
            if r["activity_id"] not in {aid for _, aid in to_report}:
                to_report.append((r["ref"], r["activity_id"]))
        if to_report:
            from trail_factors.analysis.report import build, render_safe
            for label, aid in to_report:
                print()
                try:
                    _rep = build(con, aid)
                    _text, _err = render_safe(_rep)
                    print(_text if not _err else f"[render failed: {_err}]")
                except Exception as exc:       # never let it hide the ingest
                    print(f"  report failed for {label}: "
                          f"{type(exc).__name__}: {exc}")
    finally:
        con.close()
