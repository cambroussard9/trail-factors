"""§6.3 climb-recovery: delta-HR and tau, always together (SPEC.md §6.3, §12.5).

THE PAIR IS THE POINT. tau measures recovery RATE, not cost. A small excursion
recovering quickly and a large excursion recovering slowly are not comparable
quantities, and ranking them by tau alone says the smaller effort recovered
"better" when it was barely an effort. So nothing here returns tau without the
delta-HR that produced it.

TAU MAY NOT BE REPORTABLE ON A GIVEN CORPUS, and the refusal is measured
rather than assumed. Within one (segment, direction) -- repeat traversals of the
SAME slope, which should give the same tau -- the within-group SD can be as
large as the median tau itself: a coefficient of variation near 1.0. That is
noise, and the gate below says so.

The obvious explanation was tested and REJECTED on rolling terrain. The decay
tail usually contains the next climb (the gap to the next slope is typically
well under a minute, and few efforts have 300 s clear), so tau looked like
recovery interrupted at a random point. Restricting to genuinely clear windows
did NOT fix it: at >=120 s clear the SD stayed about equal to the median.
Whatever the cause, tau did not carry information there.

§4.2: a metric that cannot be measured must not be reported with a caveat
attached. tau is computed and stored as a diagnostic, and `reportable` says
whether it may be shown. A sustained climb topping onto a plateau is the
terrain that would give a real recovery interval (§5.4).
"""

from __future__ import annotations

import math
import statistics

CALC_VERSION = 1

# Below this excursion tau is meaningless regardless of anything else: there is
# nothing to recover from. Descents on rolling terrain typically raise HR by
# only a few beats, and some barely at all, so a floor is what stops a
# non-effort being ranked as excellent recovery.
MIN_DELTA_HR_BPM = 8.0

# The decay window must be clear of the NEXT effort, or tau measures recovery
# interrupted by a new climb. On rolling terrain the gap between efforts is
# usually well under this.
MIN_CLEAR_WINDOW_S = 120.0
MAX_TAIL_S = 300.0

# tau is reportable only if repeat traversals of one slope agree this well.
# Measured on rolling terrain, the within-group SD was about equal to the
# median, so nothing passed. Stated as a threshold rather than a verdict so it
# starts passing on its own when the terrain or the data supports it.
MAX_TAU_CV = 0.5


def delta_hr(hr_series: list[int | None]) -> float | None:
    """Peak HR minus opening HR across an effort -- the EXCURSION, not the cost.

    Returns None only when there is no usable HR, never 0.0 -- a genuinely flat
    effort has delta 0 and that is a value (CLAUDE.md).
    """
    hr = [h for h in hr_series if h is not None]
    if len(hr) < 2:
        return None
    return float(max(hr) - hr[0])


def tau_estimate(tail: list[tuple], clear_window_s: float) -> dict:
    """Time for HR to fall (1 - 1/e) of its post-effort drop.

    `tail` is [(timestamp, heart_rate)] after the effort ends, already trimmed
    to the clear window. Returns a dict that ALWAYS records why it declined.
    """
    out = {"tau_s": None, "drop_bpm": None, "clear_window_s": clear_window_s,
           "reason": None}
    if clear_window_s < MIN_CLEAR_WINDOW_S:
        out["reason"] = f"only {clear_window_s:.0f}s clear before the next effort"
        return out
    pts = [(t, h) for t, h in tail if h is not None]
    if len(pts) < 6:
        out["reason"] = "too few HR samples in the decay window"
        return out
    start_hr = pts[0][1]
    floor_hr = min(h for _, h in pts)
    drop = start_hr - floor_hr
    out["drop_bpm"] = float(drop)
    if drop <= 0:
        out["reason"] = "HR did not fall after the effort"
        return out
    target = start_hr - drop * (1 - 1 / math.e)
    for stamp, hr in pts:
        if hr <= target:
            out["tau_s"] = (stamp - pts[0][0]).total_seconds()
            break
    if out["tau_s"] is None:
        out["reason"] = "HR never reached the 1-1/e target inside the window"
    elif out["tau_s"] <= 0:
        out["tau_s"], out["reason"] = None, "degenerate zero-length decay"
    return out


def effort_recovery(hr_during: list[int | None], tail: list[tuple],
                    clear_window_s: float) -> dict:
    """One effort's (delta-HR, tau). NEVER returns tau without delta-HR."""
    d = delta_hr(hr_during)
    est = tau_estimate(tail, clear_window_s)
    if d is None:
        est["tau_s"], est["reason"] = None, "no usable HR during the effort"
    elif d < MIN_DELTA_HR_BPM and est["tau_s"] is not None:
        est["tau_s"] = None
        est["reason"] = (f"delta-HR {d:.0f} bpm is below the {MIN_DELTA_HR_BPM:.0f} "
                         f"bpm floor -- nothing to recover from")
    est["delta_hr_bpm"] = d
    return est


def tau_is_reportable(taus_by_group: dict[tuple, list[float]]) -> dict:
    """Does tau repeat well enough on the SAME slope to be shown at all?

    The test is repeatability, not plausibility: repeat traversals of one slope
    in one direction should give the same tau. This is what currently fails.
    """
    groups = {k: v for k, v in taus_by_group.items() if len(v) >= 3}
    if not groups:
        return {"reportable": False, "cv": None, "n_groups": 0,
                "reason": "no slope has 3+ efforts with a tau estimate"}
    sds = [statistics.pstdev(v) for v in groups.values()]
    meds = [statistics.median(v) for v in groups.values()]
    med_sd = statistics.median(sds)
    med_tau = statistics.median(meds)
    cv = med_sd / med_tau if med_tau else None
    ok = cv is not None and cv <= MAX_TAU_CV
    return {"reportable": ok, "cv": cv, "n_groups": len(groups),
            "median_tau_s": med_tau, "median_within_group_sd_s": med_sd,
            "reason": None if ok else
            (f"within-slope SD {med_sd:.0f}s against a median tau of "
             f"{med_tau:.0f}s (CV {cv:.2f} > {MAX_TAU_CV}) -- tau is noise here")}


def summarise(efforts: list[dict]) -> dict:
    """Aggregate one (segment, direction): delta-HR is the finding, tau a
    diagnostic."""
    deltas = [e["delta_hr_bpm"] for e in efforts if e["delta_hr_bpm"] is not None]
    taus = [e["tau_s"] for e in efforts if e["tau_s"] is not None]
    return {
        "n_efforts": len(efforts),
        "n_with_delta": len(deltas),
        "median_delta_hr_bpm": statistics.median(deltas) if deltas else None,
        "n_with_tau": len(taus),
        "median_tau_s": statistics.median(taus) if taus else None,
    }
