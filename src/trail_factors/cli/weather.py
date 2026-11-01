"""Weather ingest commands (SPEC.md §8, milestone 3)."""

from __future__ import annotations

import datetime

from trail_factors.cli.common import resolve_activity_id
from trail_factors.ingest.load import DB_PATH


def weather_build(activity_ref: str | None = None, refetch: bool = False) -> None:
    """Milestone 3: fetch weather per activity and interpolate PER RECORD (§8).

    Skip-if-cached, like ingest: a cached response is reused unless `refetch`.
    That keeps the command safe to re-run and keeps a re-run reproducing what
    was actually used rather than what the archive says today.
    """
    import duckdb

    from trail_factors.ingest import weather as W

    con = duckdb.connect(str(DB_PATH))
    try:
        if activity_ref:
            targets = [resolve_activity_id(con, activity_ref)]
        else:
            targets = [
                r[0]
                for r in con.execute(
                    "SELECT activity_id FROM activities ORDER BY start_time_utc"
                ).fetchall()
            ]
        done = indoor = cached = failed = backfilled = 0
        for aid in targets:
            stamps = [
                r[0]
                for r in con.execute(
                    "SELECT timestamp FROM records WHERE activity_id = ? "
                    "ORDER BY timestamp",
                    [aid],
                ).fetchall()
            ]
            if not stamps:
                continue
            start, end = stamps[0], stamps[-1]
            pos = con.execute(
                "SELECT lat, lon FROM records WHERE activity_id = ? "
                "AND lat IS NOT NULL ORDER BY timestamp LIMIT 1",
                [aid],
            ).fetchone()

            if pos is None:
                # No coordinate: treadmill or a fully GPS-less run. §8 requires
                # a LABELLED assumption rather than NULL, which would be
                # indistinguishable from a fetch failure.
                rows = W.indoor_rows(stamps)
                source, is_final, prev24 = W.ASSUMED_INDOOR, True, 0.0
                indoor += 1
                hourly = []
            else:
                payload = None if refetch else W.load_cache(aid)
                # ARCHIVE REPLACES FORECAST once ERA5 catches up (§8). A cached
                # provisional row is re-fetched automatically, so the backfill
                # needs no separate job and no one has to remember it.
                if payload is not None and W.needs_backfill(payload, start):
                    payload, backfilled = None, backfilled + 1
                if payload is None:
                    try:
                        payload = W.fetch(pos[0], pos[1], start, end)
                    except Exception as exc:
                        failed += 1
                        print(f"  FETCH FAILED {aid}: {type(exc).__name__}: {exc}")
                        continue
                    W.save_cache(aid, payload)
                else:
                    cached += 1
                hourly = W.to_rows(payload)
                source = W.source_of(payload)
                is_final = bool(payload["_is_final"])
                prev24 = W.precip_prev_24h(hourly, start)
                rows = W.interpolate(hourly, stamps)
                done += 1

            now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
            con.execute("BEGIN")
            con.execute("DELETE FROM weather_hourly WHERE activity_id = ?", [aid])
            con.execute("DELETE FROM weather_records WHERE activity_id = ?", [aid])
            if hourly:
                con.executemany(
                    """INSERT INTO weather_hourly (activity_id, timestamp, temp_c,
                         dew_point_c, humidity_pct, wind_mps, wind_dir_deg,
                         precip_mm, cloud_cover_pct, shortwave_wm2,
                         weather_source, is_final, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            aid,
                            r["timestamp"],
                            r["temp_c"],
                            r["dew_point_c"],
                            r["humidity_pct"],
                            r["wind_mps"],
                            r["wind_dir_deg"],
                            r["precip_mm"],
                            r["cloud_cover_pct"],
                            r["shortwave_wm2"],
                            source,
                            is_final,
                            now,
                        )
                        for r in hourly
                    ],
                )
            con.executemany(
                """INSERT INTO weather_records (activity_id, timestamp, temp_c,
                     dew_point_c, humidity_pct, wind_mps, wind_dir_deg,
                     precip_mm, cloud_cover_pct, shortwave_wm2,
                     precip_prev_24h_mm, weather_source, is_final, calc_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        aid,
                        r["timestamp"],
                        r["temp_c"],
                        r["dew_point_c"],
                        r["humidity_pct"],
                        r["wind_mps"],
                        r["wind_dir_deg"],
                        r["precip_mm"],
                        r["cloud_cover_pct"],
                        r["shortwave_wm2"],
                        prev24,
                        source,
                        is_final,
                        1,
                    )
                    for r in rows
                ],
            )
            con.execute("COMMIT")

        print(
            f"weather: {done} fetched ({cached} cached, {backfilled} refetched), "
            f"{indoor} assumed "
            f"indoor, {failed} FAILED"
        )
        gaps = con.execute("""
            SELECT count(*) FROM activities a
            WHERE NOT EXISTS (SELECT 1 FROM weather_records w
                              WHERE w.activity_id = a.activity_id)""").fetchone()[0]
        if gaps:
            print(
                f"  {gaps} activities still have NO weather -- §8 treats that "
                f"as a fetch failure, not an expected gap"
            )
        rng = con.execute("""
            SELECT min(temp_c), max(temp_c), min(dew_point_c), max(dew_point_c)
            FROM weather_records WHERE weather_source <> 'assumed_indoor'""").fetchone()
        if rng[0] is not None:
            print(
                f"  temp {rng[0] * 9 / 5 + 32:.0f}-{rng[1] * 9 / 5 + 32:.0f} F, "
                f"dew point {rng[2] * 9 / 5 + 32:.0f}-{rng[3] * 9 / 5 + 32:.0f} F"
            )
        prov = con.execute(
            "SELECT weather_source, is_final, count(DISTINCT activity_id) "
            "FROM weather_records GROUP BY 1, 2 ORDER BY 3 DESC"
        ).fetchall()
        for src, final, n in prov:
            flag = "" if final else "  (NOT final -- backfill from archive)"
            print(f"  {src:<22}{n:>3} activities{flag}")
    finally:
        con.close()
