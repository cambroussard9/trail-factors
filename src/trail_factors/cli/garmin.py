"""Garmin Connect download commands (SPEC.md §10)."""

from __future__ import annotations

from pathlib import Path

from trail_factors.ingest.load import DB_PATH
from trail_factors.paths import RAW_DIR, display_path

# Where downloaded FIT files land. Garmin's own filenames are kept (§11).
RAW_GARMIN = RAW_DIR / "garmin"


def _km(m) -> str:
    """Metres as km for display; an absent distance is shown as absent."""
    return f"{m / 1000:.2f}" if m is not None else "-"


def _bpm(v) -> str:
    return f"{v:.0f}" if v is not None else "-"


def garmin_login() -> None:
    """One-time interactive login. The password is read with getpass, used
    immediately, and never stored, echoed, or written anywhere by us."""
    import getpass

    from trail_factors.ingest import garmin_api as G

    try:
        # Fail before asking for anything: a recent failure, or a credential
        # path inside the repo, is refused with nothing sent.
        G.check_backoff()
    except (G.AuthBackoff, G.CredentialPathError) as exc:
        raise SystemExit(str(exc)) from exc

    email = input("Garmin Connect email: ").strip()
    if not email:
        raise SystemExit("Email is empty. Nothing sent to Garmin.")

    print("\n  The password prompt shows NOTHING as you type - no characters,")
    print("  no dots, no cursor movement. That is normal, not a frozen terminal.")
    print("  A password manager cannot autofill a terminal prompt: paste it or")
    print("  type it manually, then press Enter.\n", flush=True)
    password = getpass.getpass("Garmin Connect password (not echoed): ")
    if not password.strip():
        raise SystemExit(
            "Password is empty. Nothing sent to Garmin.\n"
            "Nothing appears while typing - if you pasted, the paste may not "
            "have registered. Try again."
        )

    G.interactive_login(email, password)
    print(
        f"OK - token saved to {display_path(Path(G.TOKENSTORE))} "
        f"(TRAIL_FACTORS_GARMIN_TOKENSTORE). Password was not stored."
    )


def garmin_history(page_size: int = 200) -> None:
    """Sweep the WHOLE account for activity SUMMARIES. No file downloads.

    This is `garmin-list history`. It pages back through everything the
    account holds and STORES the seven fields that answer "which files are
    worth downloading" -- date, type, duration, distance, maxHR, avgHR.

    IT IS A LOOKUP, NOT DATA. Nothing here enters the corpus (§10.1), and the
    maxHR it reports is a single sample -- a spike candidate that cannot be
    checked for cadence lock or quality flags without the records themselves.
    """
    from trail_factors.ingest.garmin_api import client
    from trail_factors.lookup import HISTORY_FILE, ceiling_report, sweep

    print("Sweeping Garmin account history — SUMMARIES ONLY, no downloads.\n")
    sweep(client(), page_size=page_size)
    print(f"\nStored in {display_path(HISTORY_FILE)}")
    print("  NOT corpus data: never ingested, never joined to a run. It exists")
    print("  to choose which files to download in full.\n")
    r = ceiling_report()
    if r.get("status") == "ok":
        print(
            f"  highest maxHR seen: {r['highest_max_hr']['max_hr']:.0f} bpm on "
            f"{r['highest_max_hr']['date']}"
        )
        print("  read the whole picture with:  uv run trail-factors hr-ceiling")


def hr_ceiling() -> None:
    """Report the HR ceiling picture from the stored summaries. NO network."""
    import duckdb

    from trail_factors.lookup import ceiling_report

    corpus_ids = set()
    if DB_PATH.exists():
        con = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            corpus_ids = {
                r[0]
                for r in con.execute(
                    "SELECT regexp_extract(source_file, '([0-9]+)_ACTIVITY', 1) "
                    "FROM activities"
                ).fetchall()
                if r[0]
            }
        finally:
            con.close()
    r = ceiling_report(corpus_ids=corpus_ids)
    if r.get("status") != "ok":
        raise SystemExit(r.get("reason", "no summaries stored"))

    print("=" * 74)
    print("HR CEILING — from Garmin activity SUMMARIES")
    print("=" * 74)
    print(f"  source   {r['source']}")
    print(
        f"  {r['n_activities_with_hr']} of {r['n_activities_total']} activities "
        f"carry a maxHR"
    )
    print("\n  EVERY FIGURE BELOW IS A SINGLE-SAMPLE MAXIMUM. A maximum selects for")
    print("  positive measurement error, and a summary carries no records — so none")
    print("  of these can be checked for cadence lock or quality flags (§6.0).")

    h = r["highest_max_hr"]
    print("\n" + "-" * 74)
    print(
        f"  HIGHEST EVER   {h['max_hr']:.0f} bpm   {h['date']}   "
        f"{h['activity_type']}   {_km(h['distance_m'])} km"
    )
    print(
        f"                 activity {h['activity_id']}"
        f"{'   (already in the corpus)' if h['in_corpus'] else '   NOT in the corpus'}"
    )

    d = r["distribution"]
    print("\n  DISTRIBUTION across all activities")
    print(
        f"    min {d['min']:.0f}   median {d['median']:.0f}   p90 {d['p90']:.0f}   "
        f"max {d['max']:.0f}"
    )
    print(
        f"    within 5 bpm of the max: {d['n_within_5_of_max']}   "
        f"within 10: {d['n_within_10_of_max']}"
    )
    if d["top_is_isolated"]:
        print("    THE TOP READING IS ISOLATED — nothing else within 5 bpm. That is")
        print("    what a spike looks like, and what downloading it would settle.")
    for b in d["histogram_5bpm"][-10:]:
        print(
            f"      {b['bpm'][0]:>3}-{b['bpm'][1]:<3} {'#' * min(b['n'], 40)} {b['n']}"
        )

    band = r["five_k_band"]
    print("\n" + "-" * 74)
    print(
        "  THE 5K BAND  "
        f"({band['band_m'][0] / 1000:.1f}–{band['band_m'][1] / 1000:.1f} km)"
        f"   {band['n']} activities"
    )
    print(f"    {band['why']}")
    if band["n"]:
        bd = band["distribution"]
        print(
            f"    min {bd['min']:.0f}   median {bd['median']:.0f}   max {bd['max']:.0f}"
        )
        print("\n    FASTEST IN THE BAND — the download shortlist")
        print(
            f"      {'date':<12}{'pace/km':>9}{'dist':>8}{'maxHR':>7}{'avgHR':>7}  "
            "activity"
        )
        for f in band["fastest"]:
            mark = " *in corpus" if f["in_corpus"] else ""
            print(
                f"      {f['date']:<12}{f['pace_per_km']:>9}"
                f"{_km(f['distance_m']):>7}k{_bpm(f['max_hr']):>7}"
                f"{_bpm(f['avg_hr']):>7}  {f['activity_id']}{mark}"
            )
        print("\n    Download the ones worth checking, then read max_hr_30s:")
        print(f"      {band['fastest'][0]['download']}")
        print("      uv run trail-factors ingest-all   # then: report, or max_hr_over")


def garmin_download(activity_id: str, dest: Path = RAW_GARMIN) -> Path:
    """Download one activity's ORIGINAL upload and extract the .fit.

    Filenames are preserved exactly as Garmin names them (§11): the Garmin
    name embeds the activity id, a real external key back to Connect.
    """
    import io
    import zipfile

    from trail_factors.ingest.garmin_api import client, download_original

    dest.mkdir(parents=True, exist_ok=True)
    api = client()
    blob = download_original(api, activity_id)
    print(f"  downloaded {len(blob):,} bytes")

    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fit")]
            if not names:
                raise SystemExit(f"no .fit inside the zip: {z.namelist()}")
            name = names[0]
            out = dest / Path(name).name
            if out.exists():
                raise SystemExit(f"refusing to overwrite existing {display_path(out)}")
            out.write_bytes(z.read(name))
    else:
        out = dest / f"{activity_id}_ACTIVITY.fit"
        if out.exists():
            raise SystemExit(f"refusing to overwrite existing {display_path(out)}")
        out.write_bytes(blob)
    print(f"  wrote {display_path(out)} ({out.stat().st_size:,} bytes)")
    return out


def already_downloaded(dest: Path) -> set[str]:
    """Garmin activity ids already on disk, from the preserved filenames (§11)."""
    return {
        p.name.split("_")[0]
        for p in dest.glob("*.fit")
        if p.name.split("_")[0].isdigit()
    }


def garmin_download_all(days: int | None = None, dest: Path = RAW_GARMIN) -> None:
    """Batch-download EVERY activity. Resumable, and does NOT ingest.

    §4.1: DOWNLOAD BROADLY, MODEL STRICTLY. This used to keep only
    `typeKey == "running"` and only the last 400 days, so a logged hike never
    arrived and neither did dozens of runs older than the window.
    Both were strictness AT THE DOOR, which is the one place it cannot be
    revised: a filter here decides what the corpus may ever contain, and
    undoing it costs another network pass. Sport, sub-sport and age are
    recorded and left for the derived layers to act on.

    Download and parse are separate passes on purpose: a parse failure must
    not abort a fetch that is expensive to restart (§9, rate limiting).

    `days` still narrows the window when given -- useful for a quick catch-up
    -- but the DEFAULT is the whole account.
    """
    from datetime import date, timedelta

    from garminconnect import GarminConnectTooManyRequestsError

    from trail_factors.ingest.garmin_api import (
        client,
        list_activities,
        list_all_activities,
        record_failure,
    )

    dest.mkdir(parents=True, exist_ok=True)
    api = client()
    if days is None:
        acts = list_all_activities(api)
    else:
        end = date.today()
        acts = list_activities(
            api, (end - timedelta(days=days)).isoformat(), end.isoformat()
        )
    # EVERY SPORT. The type is reported, never used to exclude.
    runs = sorted(acts, key=lambda a: a.get("startTimeGMT") or "")

    have = already_downloaded(dest)
    todo = [a for a in runs if str(a["activityId"]) not in have]
    import collections

    by_type = collections.Counter(
        (a.get("activityType") or {}).get("typeKey") or "unknown" for a in todo
    )
    print(
        f"{len(runs)} activities on the account; {len(have)} already on disk; "
        f"{len(todo)} to fetch"
    )
    for k, n in by_type.most_common():
        print(f"    {k:<22}{n:>4}")
    print()

    ok, failed = 0, []
    for i, a in enumerate(todo, 1):
        aid = str(a["activityId"])
        try:
            print(f"  [{i}/{len(todo)}] {aid}  {a.get('startTimeLocal')}", flush=True)
            garmin_download(aid, dest)
            ok += 1
        except GarminConnectTooManyRequestsError as exc:
            record_failure("429 during batch download")
            print(
                f"\n  RATE LIMITED after {ok} files. Stopping — rerun later to resume."
            )
            print(f"  {exc}")
            break
        except Exception as exc:  # keep going on per-file errors
            failed.append((aid, f"{type(exc).__name__}: {exc}"))
            print(f"      FAILED: {type(exc).__name__}: {exc}")
    print(
        f"\ndownloaded {ok}, failed {len(failed)}, remaining "
        f"{len(todo) - ok - len(failed)}"
    )
    if ok:
        got = collections.Counter(
            (a.get("activityType") or {}).get("typeKey") or "unknown" for a in todo[:ok]
        )
        print("  fetched by activityType:")
        for k, n in got.most_common():
            print(f"    {k:<22}{n:>4}")
    for aid, err in failed:
        print(f"  {aid}: {err}")
