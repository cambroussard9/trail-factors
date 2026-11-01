"""Analysis primitives (SPEC.md §6, §12.5 milestone 6)."""

from __future__ import annotations

from trail_factors.cli.common import resolve_activity_id
from trail_factors.ingest.load import DB_PATH


def recovery_build() -> None:
    """§6.3: delta-HR and tau per effort, reported as a pair (§12.5)."""
    import duckdb

    from trail_factors.analysis import recovery as R

    con = duckdb.connect(str(DB_PATH))
    try:
        efforts = con.execute("""
            SELECT e.segment_id, e.activity_id, e.slope_index, e.direction,
                   e.start_utc, e.end_utc,
                   (SELECT min(s2.start_utc) FROM segment_efforts s2
                    WHERE s2.activity_id = e.activity_id
                      AND s2.start_utc > e.end_utc) AS next_start
            FROM segment_efforts e ORDER BY e.activity_id, e.start_utc
        """).fetchall()
        if not efforts:
            print("no segment efforts; run `match` first")
            return

        rows, by_group = [], {}
        for sid, aid, idx, direction, s0, s1, nxt in efforts:
            during = [
                r[0]
                for r in con.execute(
                    "SELECT heart_rate FROM records WHERE activity_id = ? "
                    "AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
                    [aid, s0, s1],
                ).fetchall()
            ]
            clear = (nxt - s1).total_seconds() if nxt is not None else R.MAX_TAIL_S
            tail = con.execute(
                "SELECT timestamp, heart_rate FROM records WHERE activity_id = ? "
                "AND timestamp > ? AND timestamp <= ? + (INTERVAL 1 SECOND * ?) "
                "ORDER BY timestamp",
                [aid, s1, s1, min(clear, R.MAX_TAIL_S)],
            ).fetchall()
            res = R.effort_recovery(during, tail, clear)
            res |= {
                "segment_id": sid,
                "activity_id": aid,
                "slope_index": idx,
                "direction": direction,
            }
            rows.append(res)
            if res["tau_s"] is not None:
                by_group.setdefault((sid, direction), []).append(res["tau_s"])

        verdict = R.tau_is_reportable(by_group)
        con.execute("BEGIN")
        con.execute("DELETE FROM segment_recovery")
        con.executemany(
            """INSERT INTO segment_recovery (segment_id, activity_id, slope_index,
                 direction, delta_hr_bpm, tau_s, drop_bpm, clear_window_s,
                 tau_reportable, reason, calc_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    r["segment_id"],
                    r["activity_id"],
                    r["slope_index"],
                    r["direction"],
                    r["delta_hr_bpm"],
                    r["tau_s"],
                    r["drop_bpm"],
                    r["clear_window_s"],
                    verdict["reportable"],
                    r["reason"],
                    R.CALC_VERSION,
                )
                for r in rows
            ],
        )
        con.execute("COMMIT")

        print(f"recovery: {len(rows)} efforts\n")
        print("  §6.3 reports delta-HR and tau TOGETHER. tau measures recovery")
        print("  RATE, not cost -- a 3 bpm excursion recovering fast is not")
        print("  comparable to a 20 bpm one.\n")
        print(
            f"  {'segment':<14}{'dir':<9}{'n':>4}{'median dHR':>12}"
            f"{'n with tau':>12}{'median tau':>12}"
        )
        for sid, direction, n, d, nt, tv in con.execute("""
            SELECT segment_id, direction, count(*),
                   median(delta_hr_bpm), count(tau_s), median(tau_s)
            FROM segment_recovery GROUP BY 1, 2
            HAVING count(*) >= 3 ORDER BY count(*) DESC, 1, 2""").fetchall():
            tv_txt = f"{tv:.0f}s" if tv is not None else "-"
            print(f"  {sid:<14}{direction:<9}{n:>4}{d:>11.1f}{nt:>12}{tv_txt:>12}")

        print()
        if verdict["reportable"]:
            print(
                f"  tau IS reportable: within-slope CV {verdict['cv']:.2f} "
                f"over {verdict['n_groups']} slopes"
            )
        else:
            print(f"  TAU IS NOT REPORTABLE -- {verdict['reason']}")
            print("  Stored as a diagnostic only. §4.2: a metric that cannot be")
            print("  measured must not be shown with a caveat attached. A")
            print("  sustained climb topping onto a plateau is the terrain that")
            print("  would give a real recovery interval (§5.4).")
        asc, desc = con.execute("""
            SELECT median(delta_hr_bpm) FILTER (WHERE direction = 'ascent'),
                   median(delta_hr_bpm) FILTER (WHERE direction = 'descent')
            FROM segment_recovery""").fetchone()
        if asc is not None and desc is not None:
            print(
                f"\n  delta-HR by direction: ascent {asc:.1f} bpm, "
                f"descent {desc:.1f} bpm -- the confound §6.3 records, and why"
            )
            print("  a like-for-like comparison needs BOTH numbers.")
    finally:
        con.close()


def report_cmd(activity_ref: str | None = None) -> None:
    """§16.2 post-run report: one run against recent history."""
    import duckdb

    from trail_factors.analysis.report import build, render_safe

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        if activity_ref:
            aid = resolve_activity_id(con, activity_ref)
        else:
            row = con.execute(
                "SELECT activity_id FROM activities WHERE source = 'garmin_watch' "
                "ORDER BY start_time_utc DESC LIMIT 1"
            ).fetchone()
            if row is None:
                print("no activities")
                return
            aid = row[0]
        rep = build(con, aid)
        text, err = render_safe(rep)
        # A FORMATTING FAILURE MUST NOT LOOK LIKE A FAILED REPORT.
        print(
            text
            if not err
            else f"[render failed: {err}]\nThe payload built correctly; this is a "
            f"formatting defect. Re-run through the MCP `report` tool to read it."
        )
    finally:
        con.close()
