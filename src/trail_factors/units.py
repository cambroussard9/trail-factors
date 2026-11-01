"""Display formatting for units. NOTHING HERE IS EVER STORED (SPEC.md §14).

Pace is stored as m/s, or as the seconds and metres it is derived from, and is
turned into a string only at the moment it is printed. There is no pace column
anywhere and there must not be one: a stored "13.45" cannot be told apart from
a stored "13:27" by anything downstream, and the two are 18 seconds apart.

WHY DECIMAL MINUTES ARE WRONG. "13.45 min/mi" reads as thirteen minutes and
forty-five seconds. It is thirteen minutes twenty-seven. The error is small,
always in the same direction, and lands in exactly the range a reader will not
question -- so it is never caught. Every displayed pace goes through here.

One formatter, one constant. Two implementations of MM:SS would drift, and the
site that drifted would be the one nobody was reading closely.
"""

from __future__ import annotations

# The international mile, exact. The codebase had 1609.34 and 1609.344 in
# different files; the difference is 4 parts per million and invisible at any
# precision displayed here, but one number is easier to trust than two.
METERS_PER_MILE = 1609.344


def mmss(seconds_per_mile: float) -> str:
    """Seconds per mile as M:SS. Rounds to the nearest second.

    Rounding BEFORE the split, not after, so 13.999 min/mi renders 14:00 and
    never 13:60.
    """
    total = int(round(seconds_per_mile))
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"


def pace_per_mile(mps: float | None, placeholder: str | None = None) -> str | None:
    """Speed in m/s as pace M:SS per mile.

    Returns `placeholder` when there is no pace to show. Zero is not a speed
    to divide by, and a negative one is a defect upstream -- neither is
    silently turned into a number that looks real.
    """
    if mps is None or mps <= 0:
        return placeholder
    return mmss(METERS_PER_MILE / mps)


def pace_over(
    seconds: float | None, meters: float | None, placeholder: str | None = None
) -> str | None:
    """Pace M:SS per mile over a duration and a distance.

    Takes the two measured quantities rather than a precomputed speed so the
    caller never has to build an intermediate it would then have to name.
    """
    if seconds is None or meters is None or seconds <= 0 or meters <= 0:
        return placeholder
    return mmss(seconds * METERS_PER_MILE / meters)


def miles(meters: float | None) -> float | None:
    """Metres as miles. Distance, not pace -- kept here for the one constant."""
    if meters is None:
        return None
    return meters / METERS_PER_MILE
