"""Per-run athlete context: run_type, exclude_from_trend, walk_protocol,
sun_exposure. THIS BUILD HAS NO SOURCE FOR ANY OF THEM.

`load_answers()` always returns an empty mapping, so every reader takes its
labelled "not collected" path: a run_type is null and says so, nothing is
excluded from a trend, no run's walks are poolable, and a shade correction is
never applied. None of those is a default standing in for an answer -- each
reader states that the value was not collected.

A later version re-enables run_type through user-defined run types. Until
then, a run_type FILTER is rejected by name (src/trail_factors/analysis/select.py)
rather
than silently matching nothing.
"""

from __future__ import annotations

# The one label every reader uses for a value this build does not collect.
NOT_COLLECTED = "not collected"
NO_RUN_TYPE_SOURCE = "no run_type source in this build"


def load_answers() -> dict:
    """Per-run context keyed by run_id. Always empty in this build."""
    return {}
