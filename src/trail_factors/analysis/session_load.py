"""Foster session load from Garmin's own post-run self-evaluation.

load = garmin_rpe x timer minutes, in arbitrary units (AU).

THE RPE IS GARMIN'S, AND ONLY GARMIN'S. Some watches ask "how hard was that?"
(1-10) and "how did you feel?" (1-5) when an activity is saved, and write the
answers into the FIT session message as `workout_rpe` (x10) and
`workout_feel` (0/25/50/75/100). Ingest decodes them into
activities.garmin_rpe / garmin_feel (src/trail_factors/ingest/load.py, SELFEVAL_SCALE).
An activity with no rating -- the device did not ask, or the athlete
declined -- is UNAVAILABLE and says so. It is never priced at 0, and it is
never filled from another activity.

FEEL IS SHOWN, NOT USED. It is a different question from effort, and the
direction of its 1-5 decoding is unverified; putting it in the load would
price an unverified scale.

TIMER TIME, NOT MOVING TIME. moving_time_s is defined as sum(rwd_run +
rwd_walk), so it excludes standing and runs low in one direction -- an
understatement that accumulates in a weekly sum. Load is a session DOSE, not a
rate, so the exclude-stopped-time rule for rates does not reach it.
"""

from __future__ import annotations

from trail_factors.ingest.load import SELFEVAL_SCALE

UNRATED_REASON = (
    "no Garmin self-evaluation on this activity: the device did "
    "not ask, or the rating was declined"
)


def filters() -> dict:
    return {
        "formula": "garmin_rpe x duration_min",
        "rpe_source": "activities.garmin_rpe (FIT session.workout_rpe / 10)",
        "rpe_scale": SELFEVAL_SCALE,
        "duration_basis": "activities.timer_time_s (Garmin `duration`)",
        "duration_basis_note": "NOT moving_time_s, which excludes standing "
        "and runs low in one direction",
        "feel_used": False,
        "feel_note": "garmin_feel is shown for context and never enters the "
        "load; its 1-5 direction is unverified",
        "unit": "AU (arbitrary units)",
        "grain": "one activity",
    }


def session_load(con, activity_id: str) -> dict:
    """The Foster load of one activity, or a stated reason it has none."""
    row = con.execute(
        """
        SELECT run_id, timer_time_s, garmin_rpe, garmin_feel
        FROM activities WHERE activity_id = ?""",
        [activity_id],
    ).fetchone()
    if row is None:
        raise LookupError(f"no activity {activity_id}")
    run_id, timer_s, rpe, feel = row
    minutes = timer_s / 60.0 if timer_s is not None else None
    out = {
        "activity_id": activity_id,
        "run_id": run_id,
        "garmin_rpe": rpe,
        "garmin_feel": feel,
        "duration_min": minutes,
    }
    if rpe is None:
        return {
            **out,
            "status": "unavailable",
            "reason": UNRATED_REASON,
            "foster_load_au": None,
        }
    if minutes is None:
        return {
            **out,
            "status": "unavailable",
            "reason": "the activity has no timer time, so the dose has no multiplier",
            "foster_load_au": None,
        }
    return {
        **out,
        "status": "ok",
        "reason": None,
        "foster_load_au": float(rpe) * minutes,
    }
