"""Bronze ingest (SPEC.md §4, milestone 1)."""

from __future__ import annotations

from pathlib import Path

from trail_factors.paths import RAW_DIR


def ingest_all(src: Path | None = None) -> dict:
    """Ingest every raw .fit. A rejected or broken file does not stop the run.

    Returns {ok, rejected, failed, warned, advice, rescued}. `rejected` and
    `failed` stay (file name, message) pairs -- the sync job reads them that
    way. `advice` holds one rejection_advice per REJECTED file: the reason in
    plain words, the next step, and the exact command where a window can fix
    it. `rescued` holds one entry per file that was REJECTED BEFORE this call
    and ingested now with a confirmed window applied -- the end of the
    bad-data loop, which used to happen in silence.
    """
    from trail_factors.cli.athlete import advice_lines, raw_ref, rejection_advice
    from trail_factors.ingest.load import ingest
    from trail_factors.ingest.validate import ValidationError

    # Resolved at CALL time, so a redirected RAW_DIR is honoured.
    src = RAW_DIR if src is None else src
    files = sorted(src.rglob("*.fit"))
    print(f"{len(files)} raw files\n")
    prior, prior_error = _rejected_before()
    ok, rejected, failed, warned, advice, rescued = [], [], [], [], [], []
    for path in files:
        try:
            rows = ingest(path)
            ok.append(path.name)
            act = rows["activity"]
            if act["window_applied"] and rows["file_sha256"] in prior:
                rescued.append(_rescue(path, rows, raw_ref(path)))
            for w in rows.get("warnings") or []:
                warned.append((path.name, w))
                print(f"  WARN   {path.name}: {w}")
        except ValidationError as exc:
            rejected.append((path.name, str(exc)))
            advice.append(rejection_advice(path, str(exc), exc.checks))
            print(f"  REJECT {path.name}: {exc}")
        except Exception as exc:
            failed.append((path.name, f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL   {path.name}: {type(exc).__name__}: {exc}")
    print(
        f"\ningested {len(ok)}, rejected {len(rejected)}, failed {len(failed)}, "
        f"warnings {len(warned)}"
    )
    for (name, _exc), a in zip(rejected, advice, strict=False):
        print(f"  REJECTED {name}")
        print("\n".join(advice_lines(a, indent="           ")))
    for name, exc in failed:
        print(f"  FAILED   {name}: {exc}")
    for r in rescued:
        print(f"  RESCUED  {r['file']}: {rescue_line(r)}")
    if prior_error is not None:
        print(
            f"  (could not read the earlier rejections, so a rescued file "
            f"is not announced: {prior_error})"
        )
    # The four keys are the sync job's contract and are pinned by a test;
    # `advice` and `rescued` are ADDED beside them, never folded into
    # `rejected`, whose (name, message) pairs the job unpacks.
    return {"ok": ok, "rejected": rejected, "failed": failed, "warned": warned} | {
        "advice": advice,
        "rescued": rescued,
        "rescued_unknown": prior_error,
    }


def _rejected_before() -> tuple[set, str | None]:
    """File shas with a rejection on record BEFORE this ingest: the same
    source rejected-files reads (findings with no activity row).

    Read once, up front, because a successful ingest replaces the file's
    findings. (set(), None) for a database that does not exist yet -- nothing
    can have been rejected; (set(), reason) where it could not be read, so
    the caller can say it does not know rather than report "none".
    """
    import duckdb

    from trail_factors.ingest import load

    db = load.DB_PATH  # at call time, as ingest() resolves it
    if not Path(db).exists():
        return set(), None
    try:
        con = duckdb.connect(str(db), read_only=True)
    except duckdb.Error as exc:
        return set(), f"{type(exc).__name__}: {exc}"
    try:
        return {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT file_sha256 FROM validation_results "
                "WHERE activity_id IS NULL"
            ).fetchall()
        }, None
    except duckdb.CatalogException:
        return set(), None  # no table yet: nothing was rejected
    finally:
        con.close()


def _rescue(path: Path, rows: dict, ref: str) -> dict:
    """What a rescued file became: the confirmed window and the trimmed
    totals, beside the whole-file figures the window removed."""
    a = rows["activity"]
    return {
        "file": path.name,
        "ref": ref,
        "file_sha256": rows["file_sha256"],
        "activity_id": a["activity_id"],
        "valid_start_utc": a["valid_start_utc"],
        "valid_end_utc": a["valid_end_utc"],
        "duration_s": a["duration_s"],
        "total_distance_m": a["total_distance_m"],
        "duration_s_whole_file": a.get("duration_s_whole_file"),
        "distance_m_whole_file": a.get("session_distance_m_whole_file"),
    }


def rescue_line(r: dict) -> str:
    """One rescued file in words, shared by ingest-all and sync."""

    def fig(dist, dur):
        km = f"{dist / 1000.0:.2f} km" if dist is not None else "- km"
        mins = f"{dur / 60.0:.0f} min" if dur is not None else "- min"
        return f"{km} in {mins}"

    return (
        f"now ingested at the confirmed window {r['valid_start_utc']} .. "
        f"{r['valid_end_utc']} UTC: "
        f"{fig(r['total_distance_m'], r['duration_s'])} "
        f"(the whole file was "
        f"{fig(r['distance_m_whole_file'], r['duration_s_whole_file'])})"
    )
