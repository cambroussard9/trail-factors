"""Trail Factors CLI (SPEC.md §12).

    uv run trail-factors sync
    uv run trail-factors verify

Split from a single 1,500-line module. Names are re-exported
here so `from trail_factors.cli import X` keeps working -- the split is a code
reorganisation and must not change any caller or any answer.
"""

from trail_factors.cli.analysis import recovery_build, report_cmd
from trail_factors.cli.athlete import (
    add_note,
    confirm_window,
    outstanding_confirmations,
)
from trail_factors.cli.common import TABLES, ensure_schema, resolve_activity_id
from trail_factors.cli.garmin import (
    already_downloaded,
    garmin_download,
    garmin_download_all,
    garmin_login,
)
from trail_factors.cli.ingest import ingest_all
from trail_factors.cli.main import DB_COMMANDS, main
from trail_factors.cli.report import (
    assign_cluster,
    coverage_report,
    sync,
    validate_cmd,
    verify,
)
from trail_factors.cli.terrain import (
    corpus_missing_cells,
    dem_populate,
    fetch_tile,
    load_traversals,
    match_build,
    name_segment,
    silver_build,
)
from trail_factors.cli.weather import weather_build
from trail_factors.ingest.load import DB_PATH

__all__ = [
    "DB_COMMANDS",
    "DB_PATH",
    "TABLES",
    "add_note",
    "already_downloaded",
    "assign_cluster",
    "confirm_window",
    "corpus_missing_cells",
    "coverage_report",
    "dem_populate",
    "ensure_schema",
    "fetch_tile",
    "garmin_download",
    "garmin_download_all",
    "garmin_login",
    "ingest_all",
    "load_traversals",
    "main",
    "match_build",
    "name_segment",
    "outstanding_confirmations",
    "recovery_build",
    "report_cmd",
    "resolve_activity_id",
    "silver_build",
    "sync",
    "validate_cmd",
    "verify",
    "weather_build",
]
