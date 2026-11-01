"""Command dispatch. Everything else lives in a sibling module.

usage: uv run trail-factors <command> [args]

  sync [days]                     fetch, ingest, validate, report new runs
  ingest <file.fit>               ingest one raw file
  ingest-all                      ingest every raw .fit
  verify                          per-activity ingest summary
  validate [--refresh]            full validation report
  coverage-report                 GPS coverage per downloaded file
  confirm-window <garmin-id>      confirm the suggested window
  confirm-file <garmin-id|sha> [--end 'YYYY-MM-DD HH:MM:SS'] [--start ...]
  rejected-files                  files with findings and no activity row
  weather [activity] [--refetch]  fetch + interpolate weather
  dem [activity] | silver [activity] | match | recovery
  retile | fetch-tile <cell> [--zone NN] | fetch-tile --missing [--yes]
  name-segment <segment-id> <name>
  venue-anchor '<venue name>' <segment-id> | venues
  venue-candidates <CLUSTER>      propose venue anchors; writes nothing
  report [activity]               the post-run report
  note <run-id> <text>            free-text run note
  garmin-login                    one-time, interactive
  garmin-download <garmin-id> | garmin-download-all [days]
  garmin-list history             sweep account SUMMARIES into the lookup dir
  hr-ceiling                      HR ceiling picture from those summaries
"""

from __future__ import annotations

import sys
from pathlib import Path

from trail_factors.cli.analysis import recovery_build, report_cmd
from trail_factors.cli.athlete import (
    add_note,
    confirm_file,
    confirm_window,
    rejected_files,
)
from trail_factors.cli.common import ensure_schema
from trail_factors.cli.garmin import (
    garmin_download,
    garmin_download_all,
    garmin_history,
    garmin_login,
    hr_ceiling,
)
from trail_factors.cli.ingest import ingest_all
from trail_factors.cli.report import coverage_report, sync, validate_cmd, verify
from trail_factors.cli.terrain import (
    dem_populate,
    fetch_tile,
    match_build,
    name_segment,
    silver_build,
    venue_anchor,
    venue_candidates,
    venues_cmd,
)
from trail_factors.cli.weather import weather_build
from trail_factors.ingest.load import DB_PATH, ingest
from trail_factors.paths import RAW_DIR, display_path

DB_COMMANDS = {
    "ingest",
    "ingest-all",
    "verify",
    "validate",
    "confirm-window",
    "retile",
    "fetch-tile",
    "rejected-files",
    "sync",
    "dem",
    "silver",
    "match",
    "name-segment",
    "venue-anchor",
    "venues",
    "venue-candidates",
    "weather",
    "note",
    "recovery",
    "report",
    "hr-ceiling",
}


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "silver":
        ensure_schema()
        silver_build(args[1] if len(args) > 1 else None)
        return
    if args and args[0] == "weather":
        ensure_schema()
        rest = [a for a in args[1:] if a != "--refetch"]
        weather_build(rest[0] if rest else None, refetch="--refetch" in args)
        return
    if args and args[0] == "report":
        ensure_schema()
        report_cmd(args[1] if len(args) > 1 else None)
        return
    if args and args[0] == "recovery":
        ensure_schema()
        recovery_build()
        return
    if args and args[0] == "match":
        ensure_schema()
        match_build()
        return
    if args and args[0] == "note":
        if len(args) < 3:
            raise SystemExit("usage: note <run-id> <free text>")
        ensure_schema()
        add_note(args[1], " ".join(args[2:]))
        return
    if args and args[0] == "name-segment":
        if len(args) < 3:
            raise SystemExit("usage: name-segment <segment-id> <name>")
        ensure_schema()
        name_segment(args[1], " ".join(args[2:]))
        return
    if args and args[0] == "venue-anchor":
        if len(args) != 3:
            raise SystemExit("usage: venue-anchor '<venue name>' <segment-id>")
        ensure_schema()
        venue_anchor(args[1], args[2])
        return
    if args and args[0] == "venue-candidates":
        if len(args) != 2:
            raise SystemExit(
                "usage: venue-candidates <CLUSTER>  (a cluster "
                "name from config/athlete.yaml)"
            )
        ensure_schema()
        venue_candidates(args[1])
        return
    if args and args[0] == "venues":
        ensure_schema()
        venues_cmd()
        return
    if args and args[0] == "retile":
        from trail_factors.cli.terrain import retile

        retile()
        return
    if args and args[0] == "fetch-tile":
        ensure_schema()
        zone, rest, it = None, [], iter(args[1:])
        for a in it:
            if a == "--zone":
                v = next(it, "")
                if not v.isdigit():
                    raise SystemExit("--zone takes a UTM zone number, e.g. --zone 14")
                zone = int(v)
            elif not a.startswith("--"):
                rest.append(a)
        fetch_tile(
            rest[0] if rest else None,
            missing="--missing" in args,
            yes="--yes" in args,
            zone=zone,
        )
        return
    if args and args[0] == "dem":
        ensure_schema()
        dem_populate(args[1] if len(args) > 1 else None)
        return
    if args and args[0] == "sync":
        ensure_schema()
        sync(int(args[1]) if len(args) > 1 else None)
        return
    if args and args[0] in DB_COMMANDS:
        ensure_schema()
    if args and args[0] == "confirm-file":
        # CONFIRM A WINDOW FOR A RAW FILE, ingested or not. The files that
        # need one most are the ones that failed validation and have no
        # activity_id for `confirm-window` to resolve.
        if len(args) < 2:
            raise SystemExit(
                "usage: confirm-file <garmin-id|sha> [--end 'YYYY-MM-DD HH:MM:SS'] "
                "[--start '...'] [--note '...']"
            )

        def _opt(name):
            return (
                args[args.index(name) + 1]
                if name in args and args.index(name) + 1 < len(args)
                else ""
            )

        out = confirm_file(
            args[1],
            valid_end=_opt("--end"),
            valid_start=_opt("--start"),
            note=_opt("--note"),
        )
        print(f"confirmed {out['file']} (revision {out['revision']})")
        print(f"  file span      {out['file_start_utc']} .. {out['file_end_utc']}")
        # WHERE EACH BOUND CAME FROM: the argument, the stored confirmation
        # it kept, or the detector -- so a bound kept from an earlier
        # confirmation is visibly kept, not silently replaced.
        src_ = out["bound_source"]
        for k in ("start", "end"):
            v = out[f"confirmed_valid_{k}"]
            print(
                f"  valid_{k:<9}{v}"
                + (f"  (from {src_[k]})" if src_[k] is not None else "  (no bound)")
            )
        print(f"  next: {out['next']}")
        return
    if args and args[0] == "rejected-files":
        ensure_schema()
        import duckdb as _d

        _con = _d.connect(str(DB_PATH), read_only=True)
        try:
            r = rejected_files(_con)
        finally:
            _con.close()
        print(
            f"{r['n_rejected']} file(s) in {display_path(RAW_DIR)}/ with "
            f"findings and NO activity row\n"
        )
        for f in r["rejected"]:
            name = (
                f["garmin_id"]
                if f["garmin_id"] is not None
                else f["ref"]
                if f["ref"] is not None
                else "?"
            )
            print(f"  {name:<16} {f['file_sha256'][:12]}  {f['checks']}")
            sw = f.get("suggested_window")
            if sw:
                print(
                    f"     file span  {sw['file_span'][0]} .. {sw['file_span'][1]}"
                    f"  ({sw['n_records']:,} records)"
                )
                print(
                    f"     suggested  start {sw['valid_start']}  end {sw['valid_end']}"
                )
            for m in f["messages"][:3]:
                print(f"     - {m[:110]}")
            # A record with no raw file found has no advice at all; it says
            # so rather than printing an empty line where the command goes.
            if f["file"] is None:
                print(
                    f"     no raw file under {display_path(RAW_DIR)}/ "
                    f"hashes to this sha; no advice can be built"
                )
            else:
                print(f"     next step: {f['next_step']}")
                if f.get("confirm_with"):
                    print(f"     run: {f['confirm_with']}")
        print(f"\n{r['how_to_clear']}")
        return
    if args and args[0] == "confirm-window":
        confirm_window(args[1])
        return
    if args and args[0] == "validate":
        validate_cmd(refresh="--refresh" in args)
        return
    if args and args[0] == "ingest-all":
        ingest_all()
        return
    if args and args[0] == "coverage-report":
        coverage_report()
        return
    if args and args[0] == "garmin-download-all":
        # DEFAULT: the whole account (§4.1). Passing 400 here was a second
        # door filter hiding behind the first -- the sport filter was removed
        # and the window silently kept every older run out anyway.
        garmin_download_all(int(args[1]) if len(args) > 1 else None)
        return
    if args and args[0] == "garmin-download":
        garmin_download(args[1])
        return
    if args and args[0] == "garmin-list":
        # `garmin-list history` pages back through the WHOLE account and stores
        # summaries (§10.1). The original `garmin-list [months]` audit listing
        # was a one-off and is gone.
        if len(args) == 2 and args[1] == "history":
            garmin_history()
            return
        raise SystemExit("usage: garmin-list history")
    if args and args[0] == "hr-ceiling":
        hr_ceiling()
        return
    if args and args[0] == "garmin-login":
        garmin_login()
        return
    if args and args[0] == "ingest":
        path = Path(args[1])
        rows = ingest(path)
        print(f"Ingested {display_path(path)}")
        print(f"  sha256      {rows['file_sha256']}")
        print(f"  activity_id {rows['activity_id']}")
        print(
            f"  records {len(rows['records']):,}  laps {len(rows['laps'])}  "
            f"splits {len(rows['splits'])}\n"
        )
        verify()
    elif args and args[0] == "verify":
        verify()
    else:
        print(__doc__)
