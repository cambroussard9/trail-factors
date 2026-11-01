"""Account-history LOOKUPS. Not corpus data, and deliberately not near it.

WHAT THIS IS FOR. An account can hold years of Garmin history predating the
corpus, and one question needs it: what is the true HR ceiling? Answering
that by downloading every FIT file would cost hundreds of heavy requests to
learn a handful of numbers. The API's activity SUMMARIES already carry maxHR,
and they are small JSON responses on a list endpoint -- cheap enough to sweep
the whole account.

WHAT THIS IS NOT. **These summaries are never ingested.** They are a lookup
used to CHOOSE which files to fetch, not data to reason from:

  * `maxHR` in a summary is a SINGLE SAMPLE. §6.0 records why that is a spike
    candidate -- a maximum selects for positive measurement error -- and a
    summary cannot be checked for cadence lock or quality-flagged records
    because it carries neither the records nor the cadence series.
  * Nothing here is joined to a run, a segment, or a date in the corpus.
  * Nothing in src/trail_factors/ingest or src/trail_factors/analysis reads this module. A test asserts it.

So the workflow is: sweep the summaries, read the distribution, pick a handful
of candidates, download THOSE in full, and let §6.0's `max_hr_30s` decide what
the ceiling actually is.

NO COORDINATES, BY CONSTRUCTION. The API returns `startLatitude` and
`startLongitude` on every activity. This module projects seven fields and
drops the rest, so the stored file cannot hold a position even by accident --
which is what makes it safe to keep outside data/raw/. A test asserts no
stored key looks like a coordinate.
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timezone

from trail_factors.paths import LOOKUP_DIR, display_path

HISTORY_FILE = LOOKUP_DIR / "garmin_activity_summaries.json"

# One request per page. 200 is the largest the endpoint reliably returns, and
# a whole account of a few hundred activities is a handful of pages.
PAGE_SIZE = 200

# THE SEVEN FIELDS, and nothing else. Everything the API offers beyond these
# is dropped at the boundary rather than filtered later: a projection that
# happens at read time is one somebody eventually forgets to apply.
SUMMARY_FIELDS = ("activity_id", "start_local", "activity_type",
                  "duration_s", "distance_m", "max_hr", "avg_hr")

# The 5K band. A real ceiling shows up in a maximal SHORT effort rather than
# in a long easy run, and 5 km is the commonest distance run flat out -- so the
# band is where to look for one. Wide enough for a short-measured course and a
# GPS-truncated track race; narrow enough that a 10K cannot wander in.
BAND_MIN_M, BAND_MAX_M = 4500.0, 5500.0


def project(activity: dict) -> dict:
    """One API summary, reduced to the seven fields this lookup keeps."""
    return {
        "activity_id": str(activity.get("activityId")),
        "start_local": activity.get("startTimeLocal"),
        "activity_type": (activity.get("activityType") or {}).get("typeKey"),
        "duration_s": activity.get("duration"),
        "distance_m": activity.get("distance"),
        "max_hr": activity.get("maxHR"),
        "avg_hr": activity.get("averageHR"),
    }


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    return json.loads(HISTORY_FILE.read_text())


def save_history(rows: dict) -> None:
    """Atomic, like every other state file in this project (CLAUDE.md)."""
    LOOKUP_DIR.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(dict(sorted(rows.items())), indent=2)
    tmp = HISTORY_FILE.with_name(HISTORY_FILE.name + f".{os.getpid()}.tmp")
    tmp.write_text(blob)
    os.replace(tmp, HISTORY_FILE)


def sweep(api, page_size: int = PAGE_SIZE, max_pages: int = 50,
          progress=print) -> dict:
    """Page back through the whole account, storing SUMMARIES only.

    LIST CALLS ONLY. This never touches the download endpoint, which is the
    expensive one and the one that draws throttling (§10). Each page is paced
    by the shared REQUEST_DELAY_S.

    RESUMABLE AND MERGING. The file is written after every page, so a rate
    limit costs the pages not yet fetched and nothing already held. Re-running
    merges by activity id rather than replacing, so a later sweep picks up new
    activities without discarding old ones.
    """
    from garminconnect import GarminConnectTooManyRequestsError

    from trail_factors.ingest.garmin_api import REQUEST_DELAY_S, record_failure
    import time

    rows = load_history()
    before = len(rows)
    try:
        total = api.count_activities()
        progress(f"{total} activities on the account; {before} already held")
    except Exception:
        total = None
        progress(f"{before} activities already held (account total unknown)")

    fetched = 0
    for page in range(max_pages):
        offset = page * page_size
        if total is not None and offset >= total:
            break
        time.sleep(REQUEST_DELAY_S)
        try:
            batch = api.get_activities(offset, page_size)
        except GarminConnectTooManyRequestsError as exc:
            # STOP, KEEP WHAT WE HAVE, AND SAY SO. Never retry in a loop:
            # repeated attempts extend Garmin's rate limit (CLAUDE.md).
            record_failure("429 during history sweep")
            save_history(rows)
            progress(f"  RATE LIMITED after {fetched} activities. Stored what "
                     f"was fetched; re-run later to resume.\n  {exc}")
            return rows
        if not batch:
            break
        for a in batch:
            row = project(a)
            if row["activity_id"] != "None":
                rows[row["activity_id"]] = row
        fetched += len(batch)
        save_history(rows)
        progress(f"  page {page + 1}: {len(batch)} activities "
                 f"({fetched} fetched, {len(rows)} held)")
        if len(batch) < page_size:
            break

    rows["_meta"] = {
        "swept_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "account_total": total,
        "what_this_is": "Garmin activity SUMMARIES. A lookup for choosing which "
                        "files to download; NEVER ingested into the corpus. "
                        "maxHR here is a single sample and is a spike candidate "
                        "(§6.0) -- it cannot be checked for cadence lock or "
                        "quality flags without the records.",
    }
    save_history(rows)
    progress(f"stored {len(rows) - 1} summaries in {display_path(HISTORY_FILE)}")
    return rows


def _rows(history: dict | None = None) -> list[dict]:
    rows = load_history() if history is None else history
    return [r for k, r in rows.items() if k != "_meta"]


def _date(row: dict) -> str:
    return (row.get("start_local") or "")[:10]


def ceiling_report(history: dict | None = None,
                   corpus_ids: set[str] | None = None) -> dict:
    """What the summaries say about the HR ceiling, and where to look next.

    EVERY FIGURE HERE IS A SINGLE-SAMPLE MAXIMUM and is labelled as one. The
    report's job is to say which activities are worth downloading in full, not
    to answer the question -- §6.0's `max_hr_30s` answers it, on records this
    lookup does not have.
    """
    rows = [r for r in _rows(history) if r.get("max_hr")]
    out = {
        "source": display_path(HISTORY_FILE),
        "is_not": "corpus data. These are API summaries used to choose which "
                  "files to download; nothing here is ingested.",
        "figures_are": "SINGLE-SAMPLE maxima (§6.0): a maximum selects for "
                       "positive measurement error, and a summary carries "
                       "neither the records nor the cadence series, so it "
                       "cannot be checked for cadence lock or quality flags.",
        "n_activities_with_hr": len(rows),
        "n_activities_total": len(_rows(history)),
    }
    if not rows:
        out["status"] = "unavailable"
        out["reason"] = ("no summaries stored yet -- run "
                         "`uv run trail-factors garmin-list history`")
        return out
    out["status"] = "ok"

    peak = max(rows, key=lambda r: r["max_hr"])
    out["highest_max_hr"] = {**peak, "date": _date(peak),
                             "in_corpus": (corpus_ids is not None
                                           and peak["activity_id"] in corpus_ids)}
    out["distribution"] = _distribution([r["max_hr"] for r in rows])
    out["by_activity_type"] = _by_type(rows)

    band = [r for r in rows
            if r.get("distance_m") is not None
            and BAND_MIN_M <= r["distance_m"] <= BAND_MAX_M]
    out["five_k_band"] = {
        "band_m": [BAND_MIN_M, BAND_MAX_M],
        "why": "a maximal short effort is where a real ceiling shows, and "
               "5 km is the distance most often run flat out",
        "n": len(band),
        "distribution": _distribution([r["max_hr"] for r in band]) if band else None,
        "highest": (max(band, key=lambda r: r["max_hr"]) if band else None),
        # THE DOWNLOAD SHORTLIST. Fastest first, because that is where a
        # maximal effort lives -- not highest maxHR first, which would rank by
        # the very figure that cannot be trusted yet.
        "fastest": _fastest(band, 8, corpus_ids),
    }
    return out


def _distribution(values: list[float]) -> dict:
    """Enough shape to see whether a high reading is ISOLATED or REPEATED."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0}
    lo, hi = int(min(vals) // 5 * 5), int(max(vals) // 5 * 5 + 5)
    bins = []
    for edge in range(lo, hi, 5):
        n = sum(1 for v in vals if edge <= v < edge + 5)
        if n:
            bins.append({"bpm": [edge, edge + 5], "n": n})
    top = vals[-1]
    return {
        "n": len(vals),
        "min": vals[0], "max": top,
        "median": statistics.median(vals),
        "p90": vals[int(0.9 * (len(vals) - 1))],
        "histogram_5bpm": bins,
        # THE ISOLATION TEST, stated as counts rather than left to the reader:
        # one activity at the top and none within 5 bpm is a different fact
        # from six clustered there.
        "n_within_5_of_max": sum(1 for v in vals if v >= top - 5),
        "n_within_10_of_max": sum(1 for v in vals if v >= top - 10),
        "top_is_isolated": sum(1 for v in vals if v >= top - 5) == 1,
    }


def _by_type(rows: list[dict]) -> list[dict]:
    out = {}
    for r in rows:
        t = r.get("activity_type") or "unknown"
        out.setdefault(t, []).append(r["max_hr"])
    return sorted(
        ({"activity_type": t, "n": len(v), "max": max(v),
          "median": statistics.median(v)} for t, v in out.items()),
        key=lambda d: d["max"], reverse=True)


def _fastest(band: list[dict], n: int, corpus_ids: set[str] | None) -> list[dict]:
    """The fastest activities in the band, with their maxHR attached."""
    usable = [r for r in band if r.get("duration_s")]
    ranked = sorted(usable, key=lambda r: r["duration_s"] / r["distance_m"])[:n]
    out = []
    for r in ranked:
        pace_s_per_km = r["duration_s"] / (r["distance_m"] / 1000.0)
        out.append({
            **r,
            "date": _date(r),
            "pace_per_km": f"{int(pace_s_per_km // 60)}:{int(pace_s_per_km % 60):02d}",
            "in_corpus": corpus_ids is not None and r["activity_id"] in corpus_ids,
            "download": f"uv run trail-factors garmin-download {r['activity_id']}",
        })
    return out
