"""config/athlete.yaml: strict validation, every error naming field and line.

Every rejection has a positive twin: a validator that refused everything would
pass the negative cases alone.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trail_factors import config as C
from trail_factors.paths import REPO_ROOT

EXAMPLE = REPO_ROOT / "config" / "athlete.example.yaml"


class TestValid(unittest.TestCase):
    def test_example_file_loads_and_is_neutral(self):
        cfg = C.load(EXAMPLE)
        self.assertEqual(cfg.source, "config/athlete.example.yaml")
        self.assertFalse(cfg.route_fingerprint_accepted)
        self.assertIsNone(cfg.reference_run_garmin_id)
        self.assertIsNone(cfg.unresolved_peak_run)
        self.assertEqual([c.name for c in cfg.clusters], ["HOME_TRAIL"])

    def test_every_field_in_the_example_has_a_comment(self):
        lines = EXAMPLE.read_text().splitlines()
        for i, line in enumerate(lines):
            if line and not line.startswith((" ", "#")) and ":" in line:
                self.assertTrue(
                    lines[i - 1].startswith("#"),
                    f"top-level field without a comment: {line}",
                )

    def test_missing_file_is_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = C.load(Path(d) / "nope.yaml")
        self.assertEqual(cfg, C.AthleteConfig())
        self.assertEqual(cfg.clusters, ())

    def test_empty_file_is_defaults(self):
        self.assertEqual(C.parse("", "f.yaml"), C.AthleteConfig())

    def test_full_valid_file(self):
        cfg = C.parse(
            "clusters:\n"
            "  - {name: A, lat: 10.5, lon: -20.25, radius_km: 3}\n"
            "projection_crs: 'EPSG:32633'\n"
            "reference_run_garmin_id: 12345678901\n"
            "unresolved_peak_run: 2025-01-31T1405Z\n"
            "hr_above_default_bpm: 175\n"
            "time_above_ladder_bpm: [160, 170]\n"
            "route_fingerprint_accepted: true\n"
            "dem_tile_suffix_guess: XX_Project_2020\n"
            "goal_event: a 50 km trail race\n",
            "f.yaml",
        )
        self.assertEqual(cfg.clusters, (C.Cluster("A", 10.5, -20.25, 3.0),))
        self.assertEqual(cfg.reference_run_garmin_id, "12345678901")
        self.assertEqual(cfg.time_above_ladder_bpm, (160.0, 170.0))
        self.assertTrue(cfg.route_fingerprint_accepted)

    def test_require_names_the_field(self):
        with self.assertRaisesRegex(C.ConfigError, "projection_crs is not set"):
            C.AthleteConfig().require("projection_crs")
        self.assertEqual(
            C.AthleteConfig(projection_crs="EPSG:32633").require("projection_crs"),
            "EPSG:32633",
        )


class TestRejected(unittest.TestCase):
    def assertRejected(self, text, *fragments):
        with self.assertRaises(C.ConfigError) as cm:
            C.parse(text, "f.yaml")
        for frag in fragments:
            self.assertIn(frag, str(cm.exception))

    def test_unknown_key_names_field_and_line(self):
        self.assertRejected(
            "goal_event: x\nclusterz: []\n", "f.yaml line 2", "clusterz", "unknown key"
        )

    def test_unknown_nested_key(self):
        self.assertRejected(
            "clusters:\n  - name: A\n    lat: 1\n    lon: 1\n"
            "    radius_km: 1\n    colour: red\n",
            "line 6",
            "clusters[0].colour",
            "unknown key",
        )

    def test_missing_cluster_key(self):
        self.assertRejected(
            "clusters:\n  - {name: A, lat: 1, lon: 1}\n", "clusters[0]", "radius_km"
        )

    def test_wrong_types(self):
        self.assertRejected(
            "hr_above_default_bpm: '180'\n",
            "line 1",
            "hr_above_default_bpm",
            "must be a number",
        )
        self.assertRejected("hr_above_default_bpm: true\n", "must be a number")
        self.assertRejected(
            "route_fingerprint_accepted: 1\n",
            "route_fingerprint_accepted",
            "true or false",
        )
        self.assertRejected("clusters: {name: A}\n", "clusters", "list")
        self.assertRejected(
            "reference_run_garmin_id: true\n", "reference_run_garmin_id"
        )

    def test_out_of_range(self):
        self.assertRejected("hr_above_default_bpm: 300\n", "out of range")
        self.assertRejected(
            "clusters:\n  - {name: A, lat: 91, lon: 0, radius_km: 1}\n",
            "line 2",
            "clusters[0].lat",
        )
        self.assertRejected(
            "clusters:\n  - {name: A, lat: 0, lon: 0, radius_km: 0}\n",
            "clusters[0].radius_km",
        )
        self.assertRejected("hr_above_default_bpm: .nan\n", "out of range")

    def test_bad_formats(self):
        self.assertRejected(
            "unresolved_peak_run: 2025-01-31\n", "unresolved_peak_run", "run_id"
        )
        self.assertRejected(
            "projection_crs: 'EPSG:4326'\n", "projection_crs", "PROJECTED"
        )
        self.assertRejected("projection_crs: utm\n", "projection_crs")
        self.assertRejected(
            "time_above_ladder_bpm: [180, 170]\n", "strictly increasing"
        )
        self.assertRejected("time_above_ladder_bpm: []\n", "non-empty")

    def test_cluster_names(self):
        self.assertRejected(
            "clusters:\n  - {name: OTHER, lat: 0, lon: 0, radius_km: 1}\n", "reserved"
        )
        self.assertRejected(
            "clusters:\n  - {name: A, lat: 0, lon: 0, "
            "radius_km: 1}\n  - {name: A, lat: 1, lon: 1, "
            "radius_km: 1}\n",
            "line 3",
            "duplicate cluster",
        )

    def test_duplicate_key(self):
        self.assertRejected("goal_event: a\ngoal_event: b\n", "line 2", "duplicate key")

    def test_not_a_mapping_and_not_yaml(self):
        self.assertRejected("- a\n", "must be a mapping")
        self.assertRejected("a: [\n", "not valid YAML")

    def test_safe_load_refuses_python_objects(self):
        self.assertRejected(
            "goal_event: !!python/object/apply:os.system ['echo pwned']\n",
            "not valid YAML",
        )


class TestPathOverrides(unittest.TestCase):
    def run_paths(self, env):
        code = (
            "from trail_factors import paths as P; print(P.DB_FILE); "
            "print(P.CONFIG_FILE); print(P.GARMIN_TOKENSTORE)"
        )
        full = {
            k: v for k, v in os.environ.items() if not k.startswith("TRAIL_FACTORS_")
        }
        full.update(env)
        # Import from the repo while RUNNING elsewhere: the cwd is the point.
        full["PYTHONPATH"] = str(REPO_ROOT)
        out = subprocess.run(
            [sys.executable, "-c", code],
            env=full,
            cwd=tempfile.gettempdir(),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\n")
        return [Path(x) for x in out[:3]]

    def test_defaults_are_repo_anchored_from_any_cwd(self):
        db, cfg, tok = self.run_paths({})
        self.assertEqual(db, REPO_ROOT / "data" / "ultra.duckdb")
        self.assertEqual(cfg, REPO_ROOT / "config" / "athlete.yaml")
        self.assertEqual(tok, Path.home() / ".garminconnect")

    def test_relative_override_resolves_against_repo_root(self):
        db, _, _ = self.run_paths({"TRAIL_FACTORS_DB": "data/x.duckdb"})
        self.assertEqual(db, REPO_ROOT / "data" / "x.duckdb")

    def test_absolute_override_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            _, cfg, tok = self.run_paths(
                {
                    "TRAIL_FACTORS_CONFIG": f"{d}/a.yaml",
                    "TRAIL_FACTORS_GARMIN_TOKENSTORE": d,
                }
            )
        self.assertEqual(cfg, Path(d) / "a.yaml")
        self.assertEqual(tok, Path(d))

    def test_display_path_never_carries_the_home_directory(self):
        from trail_factors.paths import display_path

        self.assertEqual(display_path(REPO_ROOT / "data" / "x.duckdb"), "data/x.duckdb")
        shown = display_path(Path.home() / "elsewhere" / "x.duckdb")
        self.assertEqual(shown, "<outside repo>/x.duckdb")
        self.assertNotIn(str(Path.home()), shown)


if __name__ == "__main__":
    unittest.main()
