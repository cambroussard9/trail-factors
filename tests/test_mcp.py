"""MCP server and parameterised primitives (SPEC.md §12.6).

The server is a THIN WRAPPER: these tests pin that boundary, the filter
vocabulary, and the envelope contract. Every negative case has a positive twin.

Tests that call a tool against data read the corpus named by TRAIL_FACTORS_DB
(tests/corpus.py) and skip without one. Every tool call runs inside
`state_sandbox()` and under `use_config(...)`: the tools read athlete state
and configuration, and a test must see neither the real directory nor the
real config file -- only what it names. Cluster filters use "OTHER", which is
every positioned run when no cluster is configured, so no test needs to know
where anyone runs.
"""

import asyncio
import contextlib
import json
import re
import time
import unittest
from unittest import mock

from trail_factors import mcp_server as M
from trail_factors.analysis import over, select, versions
from trail_factors.analysis.select import FilterUnavailable
from trail_factors.ingest.answers import NO_RUN_TYPE_SOURCE, NOT_COLLECTED
from trail_factors.paths import REPO_ROOT, DATA_DIR
# Bound at import, BEFORE any sandbox patches trail_factors.paths: the configured
# locations, for the tests that are about where things live.
from trail_factors.paths import ATHLETE_DIR as CONFIGURED_ATHLETE_DIR
from trail_factors.paths import JOBS_FILE as CONFIGURED_JOBS_FILE
from trail_factors.paths import RAW_DIR as CONFIGURED_RAW_DIR
from tests.corpus import CorpusTestCase, one, require_corpus, use_config
from tests.isolation import fingerprint, git_ignores, state_sandbox

SERVER = REPO_ROOT / "src" / "trail_factors" / "mcp_server.py"

# THE ONE CLUSTER NAME THAT EXISTS ON EVERY CORPUS. With no clusters
# configured, every positioned run is "OTHER" (report.cluster_of).
ANY_CLUSTER = "OTHER"

LATEST_RUN = """
    SELECT run_id FROM activities
    WHERE source = 'garmin_watch' AND sport = 'running'
    ORDER BY start_time_utc DESC LIMIT 1"""

# The words that name the two removed features. No tool, brief or payload
# may carry them: a chat that reads one will ask the athlete for answers that
# nothing stores. SPELLED BACKWARDS here, so that a repo-wide search for the
# removed features finds no test that mentions them -- the guard still
# searches for the words the right way round.
_REMOVED_WORDS = tuple(w[::-1] for w in (
    "eriannoitseuq", "noitirtun", "lack", "eirolac", "weivretni"))
REMOVED_FEATURE_WORDS = re.compile(
    "|".join(_REMOVED_WORDS) + "|§ ?" + "16" + r"\.4", re.I)

# `sync` starts a job that DOWNLOADS from Garmin; it is never called in a
# sweep. Every other registered tool is.
NOT_SWEPT = {"sync"}


def registered_tools() -> list:
    """The tools the server actually registers, introspected -- so a tool
    added or removed next month is swept without anyone editing a list."""
    return asyncio.run(M.mcp.list_tools())


def call_tool(name: str, args: dict) -> dict:
    """Through the MCP layer, exactly as a chat calls it."""
    r = asyncio.run(M.mcp.call_tool(name, args))
    return json.loads(r.content[0].text)


def owner_projection() -> str:
    """The projection the corpus was BUILT in, or SkipTest.

    Terrain primitives recompute path distance, so they need the CRS the
    corpus owner configured; a synthetic one would put the ground somewhere
    else. This is the one config value these tests read from the real file,
    and they skip by name when it is unset.
    """
    from trail_factors import config
    crs = config.get().projection_crs
    if crs is None:
        raise unittest.SkipTest(
            "projection_crs is not set in config/athlete.yaml: matched-terrain "
            "drift needs the projection this corpus was built in")
    return crs


class _Isolated(unittest.TestCase):
    """Sandboxed athlete state, raw layer and job file; config names only
    what `config_fields()` returns."""

    def config_fields(self) -> dict:
        return {}

    def setUp(self):
        super().setUp()
        fields = self.config_fields()           # before any patch is taken
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.state = stack.enter_context(state_sandbox())
        stack.enter_context(use_config(**fields))


class _CorpusTools(_Isolated, CorpusTestCase):
    """As _Isolated, plus a corpus that the SERVER also points at."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(M, "DB_FILE", require_corpus())
        p.start()
        self.addCleanup(p.stop)

    def latest_run(self) -> str:
        return one(self.con, LATEST_RUN,
                   what="garmin_watch running activity")[0]


class TestThinWrapper(unittest.TestCase):
    """No analysis in the server layer. Two implementations would drift."""

    def test_the_server_contains_no_sql(self):
        src = SERVER.read_text()
        body = src.split('"""', 2)[2]
        # SELECT and FROM were missing, so the check could not fire on any
        # SQL actually present -- the server held three queries while this
        # test passed. Same family as §4.2's "a check that cannot fire".
        for banned in ("SELECT ", " FROM ", "GROUP BY", "JOIN ", "median(", "avg("):
            self.assertNotIn(banned, body,
                             f"{banned} is analysis and belongs in src/trail_factors/analysis/")

    def test_that_check_would_fire_on_real_sql(self):
        """The presence twin: a banned-substring test is worthless unless the
        substrings actually occur in the SQL this project writes."""
        sample = (REPO_ROOT / "src" / "trail_factors" / "analysis" / "queries.py").read_text()
        self.assertIn("SELECT ", sample)
        self.assertIn(" FROM ", sample)

    def test_the_server_exposes_no_arbitrary_query_tool(self):
        for banned in ("run_sql", "query", "execute_sql", "raw"):
            self.assertFalse(hasattr(M, banned), f"{banned} must not exist")

    def test_every_writing_tool_targets_ONLY_athlete_state(self):
        """The property is stronger than "one writer": NO tool can write to the
        corpus at all, because every connection is read-only. Each writer
        writes one JSON file under the athlete-state directory."""
        src = SERVER.read_text()
        self.assertNotIn("write=True", src, "no tool needs a writable database")
        from trail_factors.ingest.ride_blocks import RIDE_BLOCKS
        from trail_factors.paths import ATHLETE_DIR
        self.assertIn("save_ride_blocks", src)
        self.assertTrue(RIDE_BLOCKS.is_relative_to(ATHLETE_DIR))

    def test_the_writers_are_the_ONLY_tools_whose_name_says_save(self):
        """A third writer must be a deliberate act, not something that arrives
        because a tool was named save_* and nobody noticed."""
        writers = {n for n in dir(M) if n.startswith(("save_", "write_",
                                                      "set_", "delete_"))}
        # save_ride_blocks IS that deliberate act. The watch cannot record
        # resistance or RPM and nothing else in this system does, so it
        # competes with no existing channel. Perceived effort is Garmin's own
        # self-evaluation (session_load), so a second effort channel would be
        # two places for one answer.
        self.assertEqual(writers, {"save_ride_blocks"})

    def test_the_registered_writers_are_the_same_set(self):
        """THE TWIN: `dir(M)` sees module functions, the chat sees REGISTERED
        tools. A writer registered under another name would slip past the
        check above."""
        names = {t.name for t in registered_tools()}
        self.assertEqual({n for n in names if n.startswith(
            ("save_", "write_", "set_", "delete_"))}, {"save_ride_blocks"})


class TestFilterVocabulary(unittest.TestCase):
    """Designed once, applied uniformly. What cannot be asked is documented."""

    def test_an_unknown_filter_is_rejected_by_name(self):
        problems = select.validate_filter({"group_by": "week"})
        self.assertTrue(problems)
        self.assertIn("group_by", problems[0])

    def test_positive_twin_every_documented_filter_is_accepted(self):
        """run_type is documented and accepted EMPTY: a caller forwarding every
        parameter with its default is not rejected."""
        self.assertEqual(select.validate_filter({
            "run_type": "", "start_date": "2025-01-01",
            "end_date": "2025-06-30", "cluster": "CLUSTER_A", "last_n": 5,
            "segment_id": "seg000000001", "direction": "ascent"}), [])
        self.assertEqual(select.validate_filter({"run_type": None}), [])

    def test_a_non_empty_run_type_is_rejected_by_name(self):
        """NOT a problem in the list: the fix is to omit it, not correct it.
        Matching it against nothing would return an empty selection that
        reads as "no runs of that type"."""
        with self.assertRaises(FilterUnavailable) as cm:
            select.validate_filter({"run_type": "continuous"})
        self.assertIn(NO_RUN_TYPE_SOURCE, str(cm.exception))
        self.assertIn("user-defined run types", str(cm.exception))
        # A ValueError, so a caller with only the generic handler still
        # refuses rather than computing.
        self.assertTrue(issubclass(FilterUnavailable, ValueError))

    def test_a_bad_direction_is_rejected(self):
        self.assertTrue(select.validate_filter({"direction": "sideways"}))

    def test_a_bad_last_n_is_rejected(self):
        for bad in (0, -1, "five", True):
            self.assertTrue(select.validate_filter({"last_n": bad}), bad)

    def test_a_bad_date_is_rejected(self):
        self.assertTrue(select.validate_filter({"start_date": "Jan 2026"}))


class TestSelectionStatesExclusions(_Isolated, CorpusTestCase):

    def _all_runs(self) -> dict:
        """Every garmin_watch running activity -> coverage, read directly."""
        return dict(self.con.execute("""
            SELECT activity_id, coverage_fraction FROM activities
            WHERE source = 'garmin_watch' AND sport = 'running'""").fetchall())

    def test_low_coverage_runs_are_excluded_with_a_reason(self):
        """Exactly the runs below the coverage gate are excluded, each with a
        reason -- computed independently from `activities`, so the check
        holds on any corpus rather than on one that happens to have some."""
        sel = select.select_runs(self.con, {})
        cov = [e for e in sel["excluded"] if e["kind"] == "coverage"]
        for e in cov:
            self.assertIn("coverage", e["reason"])
        low = {aid for aid, c in self._all_runs().items()
               if c is None or c < select.MIN_COVERAGE_FOR_TERRAIN}
        self.assertEqual({e["activity_id"] for e in cov}, low)
        # Every exclusion is one of the stated kinds, never an unlabelled drop.
        # Coverage is the only kind: nothing is excluded on the athlete's
        # say-so in this build, because nothing collects it.
        self.assertLessEqual({e["kind"] for e in sel["excluded"]}, {"coverage"})

    def test_positive_twin_usable_runs_are_included(self):
        sel = select.select_runs(self.con, {})
        runs = self._all_runs()
        if not any(c is not None and c >= select.MIN_COVERAGE_FOR_TERRAIN
                   for c in runs.values()):
            self.skipTest("this corpus has no run above the coverage gate")
        self.assertGreater(sel["n_included"], 0)
        for r in sel["included"]:
            self.assertGreaterEqual(r["coverage_fraction"],
                                    select.MIN_COVERAGE_FOR_TERRAIN)
        # run_type is null AND labelled on every included row.
        for r in sel["included"]:
            self.assertIsNone(r["run_type"])
            self.assertEqual(r["run_type_status"], NOT_COLLECTED)
        # Nothing vanishes: every run is included or excluded, never both.
        inc = {r["activity_id"] for r in sel["included"]}
        exc = {e["activity_id"] for e in sel["excluded"]}
        self.assertEqual(inc | exc, set(runs))
        self.assertEqual(inc & exc, set())

    def test_last_n_is_applied_after_exclusions(self):
        """'The last five long runs' must mean five USABLE runs, not five
        candidates of which two were dropped."""
        full = select.select_runs(self.con, {"cluster": ANY_CLUSTER})
        sel = select.select_runs(self.con, {"cluster": ANY_CLUSTER, "last_n": 3})
        self.assertEqual(sel["n_included"], min(3, full["n_included"]))
        self.assertEqual(sel["included"], full["included"][:3])
        for r in sel["included"]:
            self.assertGreaterEqual(r["coverage_fraction"],
                                    select.MIN_COVERAGE_FOR_TERRAIN)

    def test_the_rule_is_always_stated(self):
        sel = select.select_runs(self.con, {"cluster": ANY_CLUSTER,
                                            "last_n": 5})
        self.assertIn("cluster", sel["rule"])
        self.assertIn("last_n", sel["rule"])

    def test_a_run_type_selection_is_refused_not_emptied(self):
        with self.assertRaises(FilterUnavailable):
            select.select_runs(self.con, {"run_type": "continuous",
                                          "last_n": 5})
        # THE TWIN: the same selection without it answers.
        self.assertIn("rule", select.select_runs(self.con, {"run_type": "",
                                                            "last_n": 5}))


class TestParameterisedPrimitives(_Isolated, CorpusTestCase):

    def config_fields(self) -> dict:
        return {"projection_crs": owner_projection()}

    def test_a_typical_selection_answers(self):
        """A typical request: HR drift over the last five sessions."""
        r = over.drift(self.con, cluster=ANY_CLUSTER, last_n=5)
        self.assertEqual(r["primitive"], "matched_terrain_drift")
        self.assertIn("last_n", r["selection"]["rule"])

    def test_a_typed_selection_is_refused_by_name(self):
        """The same request "of a type" has no source in this build."""
        with self.assertRaises(FilterUnavailable) as cm:
            over.drift(self.con, run_type="continuous", last_n=5)
        self.assertIn(NO_RUN_TYPE_SOURCE, str(cm.exception))

    def test_every_result_states_what_was_excluded(self):
        r = over.drift(self.con, cluster=ANY_CLUSTER, last_n=5)
        self.assertIn("excluded", r)
        for e in r["excluded"]:
            self.assertTrue(e["reason"])

    def test_a_gated_run_appears_in_excluded_not_silently_dropped(self):
        """Every run that produced no value is NAMED in `excluded`, and
        nothing in `excluded` is also reported with a value."""
        r = over.segment_effort_drift(self.con, cluster=ANY_CLUSTER)
        no_value = {p["run_id"] for p in r["per_run"] if p["value"] is None}
        gated = {e["run_id"] for e in r["excluded"]
                 if "no two efforts" in e["reason"]}
        self.assertEqual(no_value, gated, "runs with no usable pair must be named")
        valued = {p["run_id"] for p in r["per_run"] if p["value"] is not None}
        self.assertEqual(valued & {e["run_id"] for e in r["excluded"]}, set())

    def test_every_primitive_states_the_filters_it_applied(self):
        """A chat reading a number with no visible filters cannot know what it
        is looking at, and correctly refuses to lean on it."""
        for fn in (over.drift, over.segment_effort_drift, over.efficiency,
                   over.delta_hr):
            r = fn(self.con, cluster=ANY_CLUSTER, last_n=3)
            f = r["applied_filters"]
            self.assertIsInstance(f, dict, r["primitive"])
            self.assertIn("grain", f, r["primitive"])

    def test_the_two_drifts_are_named_separately_and_differ(self):
        """Same concept, several times apart, because they are different
        quantities. Neither may be presented as 'the' drift."""
        a = over.drift(self.con, cluster=ANY_CLUSTER, last_n=5)
        b = over.segment_effort_drift(self.con, cluster=ANY_CLUSTER, last_n=5)
        self.assertNotEqual(a["primitive"], b["primitive"])
        self.assertNotEqual(a["applied_filters"]["grain"],
                            b["applied_filters"]["grain"])

    def test_section_6_7_is_record_grain_and_agrees_across_paths(self):
        """§6.7 pairs RECORDS, so on a well-covered run it has many more pairs
        than the effort-grain measure has efforts -- and the per-run
        primitive and the cross-run tool must report the SAME figure for the
        same run, or two implementations have drifted apart.

        The subject is the running activity with the most enriched records:
        the run with the most ground to pair."""
        from trail_factors.analysis import primitives as P
        aid, rid, day = one(self.con, """
            SELECT a.activity_id, a.run_id,
                   strftime(a.start_time_utc, '%Y-%m-%d')
            FROM activities a JOIN records_enriched e USING (activity_id)
            WHERE a.source = 'garmin_watch' AND a.sport = 'running'
            GROUP BY ALL ORDER BY count(*) DESC LIMIT 1""",
            what="running activity with enriched records")
        v = P.matched_terrain_drift(self.con, aid)
        if not v["n"]:
            self.skipTest("the best-covered run has no §6.7 record pairs")
        seg = P.segment_effort_drift(self.con, aid)
        self.assertGreater(v["n"], seg.get("n") or 0,
                           "record pairing should give many more pairs than "
                           "effort pairing")
        r = over.drift(self.con, start_date=day, end_date=day)
        [row] = [p for p in r["per_run"] if p["run_id"] == rid] or [None]
        if row is None:
            self.skipTest("the best-covered run is excluded from selection")
        self.assertEqual(row["n"], v["n"])
        self.assertAlmostEqual(row["value"], v["value"], places=9)

    def test_an_unknown_filter_raises_rather_than_silently_ignoring(self):
        with self.assertRaises(ValueError):
            over.drift(self.con, group_by="week")

    def test_grade_table_is_never_pooled_across_run_and_walk(self):
        r = over.grade_table(self.con, cluster=ANY_CLUSTER, last_n=3)
        kinds = {row["split_type"] for row in r["table"]}
        self.assertTrue(kinds <= {"rwd_run", "rwd_walk"})

    def test_segment_history_rejects_an_unknown_segment(self):
        with self.assertRaises(LookupError):
            over.segment_history(self.con, "no-such-segment")


class TestTheTwoDriftsStateTheirFilters(unittest.TestCase):
    """Each drift measure says which of §6.7's filters it applies."""

    def test_section_6_7_excludes_walking_and_matches_speed(self):
        """The two filters the earlier implementation dropped."""
        from trail_factors.analysis import primitives as P
        f = P.drift_filters()
        self.assertEqual(f["split_type"], "rwd_run")
        self.assertFalse(f["walking_paired"])
        self.assertIsNotNone(f["speed_matched_within_mps"])

    def test_the_segment_measure_admits_it_does_neither(self):
        """Positive twin: the other measure must state the opposite honestly."""
        from trail_factors.analysis import primitives as P
        f = P.segment_drift_filters()
        self.assertTrue(f["walking_paired"])
        self.assertIsNone(f["speed_matched_within_mps"])


class TestCalcVersion(unittest.TestCase):
    """The fingerprint says the DATA moved; this says the CODE moved."""

    def test_the_digest_changes_when_a_parameter_changes(self):
        from trail_factors.transform import silver as S
        before = versions.digest()
        original = S.GRADE_WINDOW_M
        try:
            S.GRADE_WINDOW_M = original + 10.0
            self.assertNotEqual(versions.digest(), before)
        finally:
            S.GRADE_WINDOW_M = original
        self.assertEqual(versions.digest(), before)

    def test_the_named_parameters_are_all_covered(self):
        # `dem_tile` was one of these and was removed deliberately: it named
        # one athlete's home tile. Tiles are now whatever is on disk.
        p = versions.calc_params()
        for key in ("grade_window_m", "speed_source", "dem_resolution_m",
                    "segment_unit", "gap_curve",
                    "stopped_time_excluded_from_rates",
                    "session_load_formula", "session_load_feel_used"):
            self.assertIn(key, p)
        self.assertIs(p["session_load_feel_used"], False)

    def test_no_parameter_belongs_to_a_removed_feature(self):
        """A digest entry for code that no longer exists would move the
        digest for nothing, and read as if the feature were still live."""
        p = versions.calc_params()
        stale = [k for k in p if REMOVED_FEATURE_WORDS.search(k)
                 or k.startswith(("rpe_", "capacity_"))
                 or k == "cycles_structured_protocols"]
        self.assertEqual(stale, [])
        self.assertIsNone(REMOVED_FEATURE_WORDS.search(json.dumps(p,
                                                                  default=str)))

    def test_the_version_carries_the_params_not_just_a_hash(self):
        """An opaque difference is nearly as bad as a hidden one."""
        v = versions.calc_version()
        self.assertIn("params", v)
        self.assertGreater(len(v["params"]), 15)
        self.assertIsInstance(v["pipeline_version"], int)


class TestEnvelopeWithoutCorpus(_Isolated):
    """The envelope tools that answer with no corpus at all."""

    def _out(self, raw):
        return json.loads(raw)

    def test_the_envelope_carries_the_version_reference_only(self):
        """The ~80 parameters were most of a sync_status response. Every
        result carries {pipeline_version, digest}; the parameters are one
        call away."""
        r = self._out(M.sync_status())
        self.assertEqual(set(r["calc_version"]), {"pipeline_version", "digest"})

    def test_positive_twin_spec_context_carries_the_params_for_that_digest(self):
        full = self._out(M.spec_context())["payload"]["calc_version"]
        self.assertGreater(len(full["params"]), 15)
        self.assertEqual(full["digest"],
                         self._out(M.sync_status())["calc_version"]["digest"])
        self.assertEqual(full["digest"], versions.digest(full["params"]))


class TestEnvelope(_CorpusTools):
    """Tools never raise; every outcome is legible to a chat."""

    def _out(self, raw):
        return json.loads(raw)

    def test_a_good_call_is_a_result_with_both_versions(self):
        r = self._out(M.report())
        self.assertEqual(r["outcome"], "result")
        self.assertIsNotNone(r["corpus_fingerprint"])
        self.assertIsNotNone(r["calc_version"])

    def test_an_unknown_run_is_absent_not_broken(self):
        r = self._out(M.report(run="no-such-run"))
        self.assertEqual(r["outcome"], "absent")
        self.assertTrue(r["remedy"])

    def test_a_bad_filter_is_rejected_with_the_allowed_list(self):
        r = self._out(M.matched_terrain_drift(cluster="CLUSTER_A", last_n=-3))
        self.assertEqual(r["outcome"], "rejected")
        self.assertTrue(r["problems"])

    def test_a_bad_payload_is_rejected_with_every_problem(self):
        r = self._out(M.save_ride_blocks(payload=json.dumps(
            {"run_id": self.latest_run(), "nonsense": 1})))
        self.assertEqual(r["outcome"], "rejected")
        self.assertGreaterEqual(len(r["problems"]), 2)
        self.assertIn("nothing was written", r["remedy"])
        self.assertTrue(r["retryable"])

    def test_bad_json_is_rejected_not_broken(self):
        r = self._out(M.save_ride_blocks(payload="not json at all"))
        self.assertEqual(r["outcome"], "rejected")

    def _sweep_args(self, name: str) -> dict:
        """The smallest arguments each registered tool needs to answer."""
        if name == "segment_history":
            return {"segment_id": one(
                self.con, "SELECT segment_id FROM segments LIMIT 1",
                what="segment")[0]}
        if name == "save_ride_blocks":
            return {"payload": "{}"}         # rejected: a sweep writes nothing
        return {}

    def test_every_registered_tool_answers_without_error(self):
        """THE ACCEPTANCE SWEEP, over the tools the server REGISTERS rather
        than a list someone has to keep in step. Every tool but the writer
        (handed an empty payload on purpose) returns a RESULT on a corpus
        with a run; none returns `broken`."""
        self.latest_run()                    # skips on a corpus with no run
        tools = registered_tools()
        self.assertGreater(len(tools), 20, "the sweep found the tools")
        for t in tools:
            if t.name in NOT_SWEPT:
                continue
            with self.subTest(tool=t.name):
                r = call_tool(t.name, self._sweep_args(t.name))
                want = "rejected" if t.name == "save_ride_blocks" else "result"
                self.assertEqual(r["outcome"], want,
                                 f"{t.name}: {r['text'][:300]}")

    def test_no_registered_tool_names_a_removed_feature(self):
        """Names, descriptions and argument schemas: what a chat reads before
        it calls anything."""
        for t in registered_tools():
            blob = json.dumps({"name": t.name, "description": t.description,
                               "schema": t.input_schema})
            self.assertIsNone(REMOVED_FEATURE_WORDS.search(blob), t.name)

    def test_the_fingerprint_carries_no_answers_count(self):
        fp = self._out(M.list_runs(limit=1))["corpus_fingerprint"]
        self.assertEqual(set(fp), {"activities", "latest_start_utc", "notes"})


class TestRawLayerIsUntouchableStructurally(_Isolated):
    """The structural half of the raw-layer property: no corpus needed."""

    def test_the_sandbox_is_the_raw_layer_the_code_sees(self):
        """THE TWIN that makes TestRawLayerIsUntouchable's behavioural check
        mean something: the raw path the server's sync would write to IS the
        sandbox, not the real one."""
        from trail_factors import paths
        from trail_factors.cli import garmin
        self.assertEqual(paths.RAW_DIR, self.state / "raw")
        self.assertTrue(garmin.RAW_GARMIN.is_relative_to(self.state))

    def test_every_database_connection_in_the_server_is_read_only(self):
        """Structural: a chat cannot write to the corpus at all."""
        src = SERVER.read_text()
        self.assertIn("read_only=True", src)
        self.assertNotIn("read_only=False", src)
        self.assertNotIn("read_only=not", src)
        self.assertEqual(src.count("duckdb.connect"), 1,
                         "one connection site, so the flag cannot be missed")

    def test_the_server_module_opens_no_files_for_writing(self):
        src = SERVER.read_text()
        for banned in ("write_text", "write_bytes", "open(", "unlink",
                       "shutil", "os.remove", "os.rename", ".rename(",
                       "mkdir", "rmtree"):
            self.assertNotIn(banned, src, f"{banned} must not appear")

    def test_no_reachable_write_path_targets_data_raw(self):
        """The only write reachable from a tool is athlete state."""
        from trail_factors import paths
        from trail_factors.ingest import ride_blocks as RB
        self.assertTrue(RB.RIDE_BLOCKS.is_relative_to(paths.ATHLETE_DIR))
        self.assertFalse(RB.RIDE_BLOCKS.is_relative_to(paths.RAW_DIR))


class TestRawLayerIsUntouchable(_CorpusTools):
    """NOTHING reachable through the MCP server may touch the raw layer.

    §3 makes the raw layer immutable, but that was written when the CLI was the
    only way in. These tests check the property STRUCTURALLY and BEHAVIOURALLY,
    not by reading the rule. The behavioural check runs against a SANDBOXED
    raw layer seeded with synthetic files: every src path constant pointing at
    the raw directory points there, so a tool that wrote into the raw layer
    would write into it and be caught -- and the real one is never at risk.
    """

    def _seed_raw(self):
        raw = self.state / "raw"
        (raw / "garmin").mkdir()
        (raw / "garmin" / "10000000001_ACTIVITY.fit").write_bytes(
            b"synthetic: not a FIT file")
        (raw / "weather").mkdir()
        (raw / "weather" / "cache.json").write_text("{}")
        return raw

    def test_no_tool_call_alters_any_raw_file(self):
        """The behavioural check: run every tool, compare the whole tree --
        and the athlete-state tree, which only a successful save may change."""
        raw = self._seed_raw()
        athlete = self.state / "athlete"
        before = fingerprint(raw)
        self.assertEqual(len(before), 2, "the seeded raw layer is in place")
        athlete_before = fingerprint(athlete)
        calls = [(t.name, {"segment_id": "no-such-segment"}
                  if t.name == "segment_history" else
                  {"payload": "{}"} if t.name == "save_ride_blocks" else {})
                 for t in registered_tools() if t.name not in NOT_SWEPT]
        calls += [("report", {"run": "no-such-run"}),
                  ("save_ride_blocks", {"payload": json.dumps(
                      {"run_id": "nope", "raw_text": "x", "blocks": []})})]
        for name, args in calls:
            call_tool(name, args)
        self.assertEqual(fingerprint(raw), before,
                         "a tool call changed something under the raw layer")
        self.assertEqual(fingerprint(athlete), athlete_before,
                         "a read or a rejected save changed athlete state")

    def _junk(self) -> str:
        return json.dumps({"run_id": self.latest_run(), "junk": True})

    def test_a_REJECTED_ride_block_save_creates_nothing(self):
        """The athlete-state directory cannot be regenerated from anything, so
        a refused payload must leave no file behind -- an empty or partial
        log is indistinguishable from a real one."""
        from trail_factors.ingest import ride_blocks as RB
        log = RB.RIDE_BLOCKS
        self.assertTrue(log.is_relative_to(self.state))
        out = json.loads(M.save_ride_blocks(payload=self._junk()))
        self.assertEqual(out["outcome"], "rejected")
        self.assertTrue(out["problems"])
        self.assertFalse(log.exists(), "a rejected save created the log")

    def test_a_REJECTED_save_leaves_an_existing_log_byte_identical(self):
        """THE TWIN: with a log already present, the rejection neither
        replaces nor removes it."""
        from trail_factors.ingest import ride_blocks as RB
        log = RB.RIDE_BLOCKS
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("{}\n")
        before = log.read_bytes()
        out = json.loads(M.save_ride_blocks(payload=self._junk()))
        self.assertEqual(out["outcome"], "rejected")
        self.assertEqual(log.read_bytes(), before)


class TestSelectionSupportTable(unittest.TestCase):
    """The support table the rejections below are built from."""

    def test_every_tool_declares_the_filters_it_supports(self):
        """The signature and the support table must not drift apart -- that
        drift is what produced the bug."""
        import inspect
        for tool, supported in M.SELECTION_SUPPORT.items():
            params = set(inspect.signature(getattr(M, tool)).parameters)
            for f in M.SELECTION_FILTERS:
                self.assertIn(f, params,
                              f"{tool} must ACCEPT {f} so it can reject it "
                              f"by name rather than have it dropped")
            for f in supported:
                self.assertIn(f, params, f"{tool} claims to support {f}")


class TestNoFilterIsEverSilentlyDropped(_CorpusTools):
    """THE BUG: the MCP layer silently DROPS arguments a tool's signature does
    not declare, and the standing brief told the chat that every primitive took
    the same seven filters. So grade_binned_pace(segment_id=..., direction=...)
    returned the whole corpus, byte-identical to the unfiltered call, with no
    error. The chat followed the documented contract and was handed a number
    for a different question.

    Same failure class as the falsy-default and end_time-minus-start_time bugs:
    every individual value looks plausible.
    """

    def _call(self, name, args):
        import asyncio
        r = asyncio.run(M.mcp.call_tool(name, args))
        return json.loads(r.content[0].text)

    def test_an_unsupported_filter_is_rejected_not_ignored(self):
        r = self._call("grade_binned_pace",
                       {"segment_id": "seg000000001", "direction": "descent"})
        self.assertEqual(r["outcome"], "rejected")
        self.assertIn("segment_id", r["text"])
        self.assertIn("direction", r["text"])

    def test_the_rejection_names_a_tool_that_can_answer(self):
        """A rejection the reader cannot act on is only marginally better than
        a wrong number."""
        r = self._call("grade_binned_pace", {"segment_id": "seg000000001"})
        self.assertIn("segment_history", r["text"])

    def test_a_per_run_tool_rejects_selection_filters(self):
        for tool in ("report", "basic_metrics", "drift_onset"):
            r = self._call(tool, {"cluster": "CLUSTER_A"})
            self.assertEqual(r["outcome"], "rejected", tool)
            self.assertIn("cluster", r["text"], tool)

    def test_supported_filters_still_narrow(self):
        """The positive twin. Rejecting everything would pass every test above
        and be useless."""
        wide = self._call("grade_binned_pace", {})
        self.assertEqual(wide["outcome"], "result")
        if wide["payload"]["selection"]["n_included"] < 2:
            self.skipTest("this corpus has fewer than two usable runs")
        narrow = self._call("grade_binned_pace",
                            {"cluster": ANY_CLUSTER, "last_n": 1})
        self.assertEqual(narrow["outcome"], "result")
        self.assertEqual(narrow["payload"]["selection"]["n_included"], 1)
        self.assertGreater(wide["payload"]["selection"]["n_included"],
                           narrow["payload"]["selection"]["n_included"])

    def test_a_segment_filter_still_works_where_it_is_supported(self):
        r = self._call("delta_hr", {"direction": "ascent",
                                    "cluster": ANY_CLUSTER})
        self.assertEqual(r["outcome"], "result")
        self.assertEqual(r["payload"]["applied_filters"]["direction"], "ascent")

    def test_a_malformed_date_is_rejected_not_ignored(self):
        """'09/01/2026' is ten characters, so a length check passed it and the
        caller got the whole corpus back labelled as September."""
        r = self._call("grade_binned_pace", {"start_date": "09/01/2026"})
        self.assertEqual(r["outcome"], "rejected")
        self.assertIn("YYYY-MM-DD", r["text"])

    def test_a_well_formed_date_still_narrows(self):
        """The positive twin. The cut date is the corpus's own: the date of
        its most recent usable run, so exactly the runs on or after it stay."""
        wide = self._call("grade_binned_pace", {})["payload"]["selection"]
        if wide["date_range"] is None or wide["date_range"][0] == wide["date_range"][1]:
            self.skipTest("this corpus has usable runs on fewer than two dates")
        last = wide["date_range"][1]
        r = self._call("grade_binned_pace", {"start_date": last})
        self.assertEqual(r["outcome"], "result")
        n = r["payload"]["selection"]["n_included"]
        self.assertGreaterEqual(n, 1)
        self.assertLess(n, wide["n_included"])
        # The tool and the selection function agree on what the date kept.
        self.assertEqual(
            n, select.select_runs(self.con, {"start_date": last})["n_included"])


class TestRunTypeIsRejectedByName(_CorpusTools):
    """run_type stays in every signature and is REJECTED BY NAME when
    non-empty: this build has no source for it. A later version re-enables it
    through user-defined run types without changing any signature."""

    def _call(self, name, args):
        return call_tool(name, args)

    def test_every_tool_that_takes_it_rejects_it(self):
        """Introspected: every registered tool with a run_type parameter,
        selection tools and per-run tools alike."""
        takers = [t.name for t in registered_tools()
                  if "run_type" in t.input_schema.get("properties", {})]
        for must in ("report", "list_runs", "max_hr_over",
                     "matched_terrain_drift", "session_load"):
            self.assertIn(must, takers)
        for name in takers:
            args = {"run_type": "continuous"}
            if name == "segment_history":
                args["segment_id"] = "no-such-segment"
            with self.subTest(tool=name):
                r = self._call(name, args)
                self.assertEqual(r["outcome"], "rejected", r["text"][:300])
                self.assertIn(NO_RUN_TYPE_SOURCE, r["text"])
                self.assertIn(name, r["text"])
                self.assertTrue(r["retryable"])
                self.assertIn("omit run_type", r["remedy"])
                self.assertIn("user-defined run types", r["remedy"])

    def test_positive_twin_an_empty_run_type_answers(self):
        self.latest_run()
        for name in ("report", "list_runs", "max_hr_over",
                     "matched_terrain_drift", "session_load"):
            with self.subTest(tool=name):
                r = self._call(name, {"run_type": ""})
                self.assertEqual(r["outcome"], "result", r["text"][:300])

    def test_list_runs_labels_every_row_not_collected(self):
        self.latest_run()
        r = self._call("list_runs", {"limit": 5})
        self.assertTrue(r["payload"])
        for row in r["payload"]:
            self.assertIsNone(row["run_type"])
            self.assertEqual(row["run_type_status"], NOT_COLLECTED)


class TestSessionLoad(_CorpusTools):
    """Foster load = Garmin's own RPE x timer minutes. An unrated activity
    says so; feel is shown and never used."""

    def _rated_unrated(self):
        """One rated and one unrated running activity, by property."""
        rated = self.con.execute("""
            SELECT run_id FROM activities
            WHERE source = 'garmin_watch' AND sport = 'running'
              AND garmin_rpe IS NOT NULL AND timer_time_s IS NOT NULL
            ORDER BY start_time_utc DESC LIMIT 1""").fetchone()
        unrated = self.con.execute("""
            SELECT run_id FROM activities
            WHERE source = 'garmin_watch' AND sport = 'running'
              AND garmin_rpe IS NULL
            ORDER BY start_time_utc DESC LIMIT 1""").fetchone()
        return (rated[0] if rated else None, unrated[0] if unrated else None)

    def test_a_rated_run_is_priced_from_rpe_and_timer_minutes(self):
        rated, _ = self._rated_unrated()
        if rated is None:
            self.skipTest("this corpus has no run with a Garmin self-evaluation")
        r = json.loads(M.session_load(run=rated))
        self.assertEqual(r["outcome"], "result", r["text"])
        [row] = r["payload"]["rows"]
        self.assertEqual(row["status"], "ok")
        self.assertIsNone(row["reason"])
        self.assertAlmostEqual(row["foster_load_au"],
                               row["garmin_rpe"] * row["duration_min"])
        f = r["payload"]["filters"]
        self.assertIs(f["feel_used"], False)
        self.assertIn("timer_time_s", f["duration_basis"])

    def test_an_unrated_run_says_so_and_is_never_zero(self):
        from trail_factors.analysis import session_load as SL
        _, unrated = self._rated_unrated()
        if unrated is None:
            self.skipTest("this corpus has no run without a self-evaluation")
        r = json.loads(M.session_load(run=unrated))
        self.assertEqual(r["outcome"], "result", r["text"])
        [row] = r["payload"]["rows"]
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["reason"], SL.UNRATED_REASON)
        self.assertIsNone(row["foster_load_au"])
        self.assertIn("unavailable", r["text"])

    def test_feel_never_moves_the_load(self):
        """Shown, never used: the same RPE and minutes price the same however
        the feel field reads."""
        from trail_factors.analysis import session_load as SL
        rated, _ = self._rated_unrated()
        if rated is None:
            self.skipTest("this corpus has no run with a Garmin self-evaluation")
        aid = self.con.execute("SELECT activity_id FROM activities "
                               "WHERE run_id = ? AND source = 'garmin_watch'",
                               [rated]).fetchone()[0]
        row = SL.session_load(self.con, aid)
        self.assertIn("garmin_feel", row)
        self.assertEqual(row["foster_load_au"],
                         float(row["garmin_rpe"]) * row["duration_min"])

    def test_a_selection_labels_every_row_and_sums_only_the_rated(self):
        r = json.loads(M.session_load(last_n=20))
        self.assertEqual(r["outcome"], "result", r["text"])
        p = r["payload"]
        s = p["summary"]
        self.assertEqual(s["n"], len(p["rows"]))
        self.assertEqual(s["n_rated"] + s["n_unrated"], s["n"])
        rated = [x for x in p["rows"] if x["status"] == "ok"]
        self.assertEqual(len(rated), s["n_priced"])
        self.assertEqual(s["n_priced"] + s["n_rated_unpriced"], s["n_rated"])
        for x in p["rows"]:
            self.assertIn(x["status"], ("ok", "unavailable"))
            if x["status"] != "ok":
                self.assertTrue(x["reason"])
                self.assertIsNone(x["foster_load_au"])
        if rated:
            self.assertAlmostEqual(s["sum_foster_load_au_priced"],
                                   sum(x["foster_load_au"] for x in rated))
        else:
            self.assertIsNone(s["sum_foster_load_au_priced"])
        self.assertEqual(s["n_unrated"],
                         sum(1 for x in p["rows"] if x["garmin_rpe"] is None))
        self.assertEqual(s["is_lower_bound"], s["n_priced"] < s["n"])
        self.assertEqual("LOWER BOUND" in s["note"], s["is_lower_bound"])

    def test_a_rated_session_of_another_sport_is_in_its_days_selection(self):
        """On the corpus: a rated non-running session is priced in the
        selection for its own day, not left out of the dose."""
        row = self.con.execute("""
            SELECT activity_id, CAST(start_time_utc AS DATE)
            FROM activities
            WHERE source = 'garmin_watch' AND sport <> 'running'
              AND garmin_rpe IS NOT NULL AND timer_time_s IS NOT NULL
            ORDER BY start_time_utc DESC LIMIT 1""").fetchone()
        if row is None:
            self.skipTest("this corpus has no rated non-running session")
        aid, day = row
        r = json.loads(M.session_load(start_date=str(day), end_date=str(day)))
        self.assertEqual(r["outcome"], "result", r["text"])
        [mine] = [x for x in r["payload"]["rows"] if x["activity_id"] == aid]
        self.assertEqual(mine["status"], "ok")
        self.assertNotEqual(mine["sport"], "running")
        self.assertIn("every sport", r["payload"]["selection_rule"])

    def test_the_report_carries_it_with_its_filters(self):
        r = json.loads(M.report())
        self.assertEqual(r["outcome"], "result", r["text"][:300])
        sl = r["payload"]["session_load"]
        self.assertIn(sl["status"], ("ok", "unavailable"))
        self.assertIn("filters", sl)
        self.assertEqual(set(r["payload"]["athlete"]),
                         {"garmin_self_evaluation", "notes", "notes_status",
                          "notes_projection"})


class _SyntheticServer(_Isolated):
    """The server pointed at an EMPTY scratch database, with the corpus
    fingerprint stubbed, so an envelope can be exercised with no corpus. The
    primitive under test is patched; nothing reads data/."""

    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path

        import duckdb
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "empty.duckdb"
        duckdb.connect(str(db)).close()
        for p in (mock.patch.object(M, "DB_FILE", db),
                  mock.patch("trail_factors.analysis.queries.corpus_fingerprint",
                             return_value={"status": "stub"})):
            p.start()
            self.addCleanup(p.stop)


class TestSessionLoadRunAndSelection(_SyntheticServer):
    """Both halves are documented filters; only the combination is not. So
    the rejection is RETRYABLE and names the fix -- not the generic "use only
    the documented filters", which reads as a dead end."""

    def test_a_run_and_a_selection_together_are_rejected_retryably(self):
        r = json.loads(M.session_load(run="2026-01-01T0000Z", last_n=3))
        self.assertEqual(r["outcome"], "rejected")
        self.assertIs(r["retryable"], True)
        self.assertIn("either run or the selection filters", r["remedy"])
        self.assertIn("nothing was computed", r["remedy"])
        self.assertNotIn("arbitrary", r["remedy"])

    def test_positive_twin_a_bad_filter_still_gets_the_generic_remedy(self):
        """The dedicated branch must not swallow every ValueError."""
        r = json.loads(M.session_load(start_date="01/02/2026"))
        self.assertEqual(r["outcome"], "rejected")
        self.assertIs(r["retryable"], False)
        self.assertIn("documented filters", r["remedy"])


class TestSessionLoadSpansEverySport(_SyntheticServer):
    """Load is a DOSE: a rated ride in the window is load the legs carried.
    The selection is stubbed, so this pins what session_load ASKS for and
    what it does with a mixed-sport answer."""

    def test_a_selection_asks_for_every_sport_and_counts_the_ride(self):
        from trail_factors.analysis import select as S
        chosen = {"rule": "every sport (sport=any), last_n=2",
                  "included": [{"activity_id": "a1", "sport": "running"},
                               {"activity_id": "a2", "sport": "cycling"}]}
        priced = {"a1": 4.0 * 50.0, "a2": 6.0 * 45.0}

        def row(_con, aid):
            return {"activity_id": aid, "run_id": f"run-{aid}",
                    "garmin_rpe": 1.0, "garmin_feel": None,
                    "duration_min": 1.0, "status": "ok", "reason": None,
                    "foster_load_au": priced[aid]}

        with mock.patch.object(S, "select_runs",
                               return_value=chosen) as sel, \
                mock.patch("trail_factors.analysis.session_load.session_load",
                           side_effect=row):
            r = json.loads(M.session_load(last_n=2))
        self.assertEqual(r["outcome"], "result", r["text"])
        self.assertEqual(sel.call_args.kwargs.get("sport"), S.SPORT_ANY)
        s = r["payload"]["summary"]
        self.assertEqual(s["n_by_sport"], {"running": 1, "cycling": 1})
        self.assertIn("every sport", s["sports"])
        self.assertAlmostEqual(s["sum_foster_load_au_priced"],
                               sum(priced.values()))
        self.assertEqual([x["sport"] for x in r["payload"]["rows"]],
                         ["running", "cycling"])


class TestMaxHrOverTextNeverCostsThePayload(_SyntheticServer):
    """A selection can have a peak and no fillable 30 s window. Formatting
    the one-line text must not throw away the computed payload."""

    def _result(self, w30):
        stub = {"max_hr_recorded": {"value": 180.0, "date": "2026-01-01"},
                "max_hr_30s": w30}
        with mock.patch("trail_factors.analysis.over.max_hr_over", return_value=stub):
            return json.loads(M.max_hr_over(last_n=1))

    def test_no_30s_window_still_returns_the_payload(self):
        r = self._result(None)
        self.assertEqual(r["outcome"], "result", r["text"])
        self.assertIsNone(r["payload"]["max_hr_30s"])
        self.assertIn("peak 180 bpm", r["text"])
        self.assertIn("no 30 s window", r["text"])

    def test_positive_twin_a_30s_window_is_printed(self):
        r = self._result({"value": 176.4})
        self.assertEqual(r["outcome"], "result", r["text"])
        self.assertIn("176 sustained over 30 s", r["text"])


class TestSyncStepsDegradeAlone(_Isolated):
    """A step that fails costs only itself; the rejections ingest found are
    reported whatever happens after. Every step function is patched: no
    network, no corpus, no DEM."""

    GID = "10000000001"
    TERRAIN = ("dem", "silver", "match", "recovery")

    def run_sync(self, tiles, fail=None, reject=True, rescued=()):
        """sync_work with every step stubbed. `fail` maps a step name to the
        exception it raises; `reject` makes ingest reject the new file.
        Returns (result, steps that actually ran)."""
        called = []
        ingest = {"ok": [], "failed": [],
                  "rejected": ([(f"{self.GID}_ACTIVITY.fit",
                                 "pace_sustained_impossible: synthetic")]
                               if reject else []),
                  "rescued": list(rescued)}
        fail = fail if fail is not None else {}

        def step(name, out=None):
            def fn(*a, **k):
                called.append(name)
                if name in fail:
                    raise fail[name]
                return out
            return fn

        seen = iter([set(), {self.GID}])
        patches = [
            mock.patch("trail_factors.cli.garmin.already_downloaded",
                       side_effect=lambda dest: next(seen)),
            mock.patch("trail_factors.cli.garmin.garmin_download_all"),
            mock.patch("trail_factors.cli.ingest.ingest_all", step("ingest", ingest)),
            mock.patch("trail_factors.cli.terrain.dem_populate", step("dem")),
            mock.patch("trail_factors.cli.terrain.silver_build", step("silver")),
            mock.patch("trail_factors.cli.weather.weather_build", step("weather")),
            mock.patch("trail_factors.cli.terrain.match_build", step("match")),
            mock.patch("trail_factors.cli.analysis.recovery_build", step("recovery")),
            mock.patch("trail_factors.cli.report.validate_cmd", step("validate")),
            mock.patch("trail_factors.transform.dem.loaded_tiles",
                       return_value=(["USGS_synthetic.tif"] if tiles else [])),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = M.sync_work(lambda s: None)
        return result, called

    def test_no_tiles_skips_terrain_with_a_note_and_still_reports_rejections(self):
        """THE FRESH INSTALL. dem_populate raises on an empty data/dem/, and
        the job used to die there -- after ingest had rejected the file, so
        the rejection and its command were never returned."""
        r, called = self.run_sync(tiles=False, fail={
            "dem": FileNotFoundError("no DEM tiles in data/dem")})
        for s in self.TERRAIN:
            self.assertEqual(r["steps"][s], "skipped", s)
            self.assertNotIn(s, called)
        self.assertEqual([x["step"] for x in r["skipped_steps"]],
                         list(self.TERRAIN))
        self.assertIn("fetch-tile", r["skipped_steps"][0]["reason"])
        self.assertEqual(r["steps"]["weather"], "done")
        self.assertEqual(r["steps"]["validate"], "done")
        self.assertEqual(r["step_errors"], [])
        self.assertEqual([x["garmin_id"] for x in r["rejected"]], [self.GID])
        self.assertIs(r["clean"], False)        # the rejection, not the skip

    def test_a_rescued_run_is_announced(self):
        """The re-sync after confirm-file downloads nothing; without this the
        job would say nothing about the run it brought into the corpus."""
        rescue = {"ref": "10000000009", "file": "10000000009_ACTIVITY.fit",
                  "window": ["2025-01-15 07:00:00", "2025-01-15 07:30:00"]}
        r, _ = self.run_sync(tiles=False, reject=False, rescued=[rescue])
        self.assertEqual(r["n_rescued"], 1)
        self.assertEqual(r["rescued"][0]["ref"], "10000000009")
        twin, _ = self.run_sync(tiles=False, reject=False)
        self.assertEqual((twin["n_rescued"], twin["rescued"]), (0, []))

    def test_positive_twin_no_tiles_and_nothing_rejected_is_clean(self):
        """No tiles is a STATE: on its own it does not make a sync unclean."""
        r, _ = self.run_sync(tiles=False, reject=False)
        self.assertEqual(len(r["skipped_steps"]), len(self.TERRAIN))
        self.assertEqual(r["problems"], [])
        self.assertIs(r["clean"], True)

    def test_a_failing_step_is_named_and_the_rejections_still_arrive(self):
        r, called = self.run_sync(tiles=True, fail={
            "match": RuntimeError("synthetic matcher failure")})
        self.assertEqual(r["steps"]["match"], "failed")
        [err] = r["step_errors"]
        self.assertEqual(err["step"], "match")
        self.assertIn("synthetic matcher failure", err["error"])
        self.assertTrue(any(p.startswith("STEP match FAILED")
                            for p in r["problems"]))
        # recovery reads what match writes: skipped, never run over stale
        # efforts. validate is independent and still runs.
        self.assertEqual(r["steps"]["recovery"], "skipped")
        self.assertNotIn("recovery", called)
        self.assertIn("match", r["skipped_steps"][0]["reason"])
        self.assertEqual(r["steps"]["validate"], "done")
        self.assertEqual([x["garmin_id"] for x in r["rejected"]], [self.GID])
        self.assertIs(r["clean"], False)

    def test_a_failed_ingest_reports_no_count_rather_than_zero(self):
        r, _ = self.run_sync(tiles=True, fail={
            "ingest": RuntimeError("synthetic ingest failure")})
        self.assertIsNone(r["n_ingested"])
        self.assertEqual(r["step_errors"][0]["step"], "ingest")
        self.assertIs(r["clean"], False)

    def test_positive_twin_every_step_runs_with_tiles(self):
        r, called = self.run_sync(tiles=True)
        self.assertEqual(called, ["ingest", "dem", "silver", "weather",
                                  "match", "recovery", "validate"])
        self.assertEqual(set(r["steps"].values()), {"done"})
        self.assertEqual((r["skipped_steps"], r["step_errors"]), ([], []))
        self.assertEqual(r["n_ingested"], 0)


class TestWalkProtocolIsNotCollected(_CorpusTools):
    """An unknown protocol is never poolable -- what decided the walks makes
    purchase and decay different quantities -- and the arithmetic is still
    reported for the run on its own. Moved here from the deleted gate tests:
    "we do not know" is the only state this build has, and it must say so."""

    def test_an_unknown_protocol_is_labelled_and_not_poolable(self):
        aid = one(self.con, """
            SELECT a.activity_id
            FROM activities a JOIN splits s USING (activity_id)
            WHERE a.source = 'garmin_watch' AND s.split_type = 'rwd_walk'
            GROUP BY 1 HAVING count(*) >= 8
            ORDER BY a.activity_id LIMIT 1""",
            what="run with 8+ rwd_walk splits")[0]
        r = call_tool("run_walk_cycles", {"run": aid})
        self.assertEqual(r["outcome"], "result", r["text"][:300])
        p = r["payload"]
        self.assertIsNone(p["walk_protocol"])
        self.assertEqual(p["walk_protocol_status"], NOT_COLLECTED)
        self.assertIs(p["poolable_with_other_runs"], False)
        self.assertTrue(p["pooling_note"])
        # REPORTED, not withheld: the run still carries its cycles.
        self.assertTrue(p["cycles"])


class TestSyncRejectionsNameTheNextStep(_Isolated):
    """A file the sync rejects is NOT in the corpus, and nothing downstream
    mentions it again. So the rejection itself must carry the reason and the
    exact command -- and say the command is CLI-only, so a chat does not go
    looking for a tool that does not exist. No corpus and no network: the
    ingest result is synthetic, and the raw files do not exist, so the window
    suggestion fails -- which must not cost the report."""

    GID = "10000000001"
    INGEST = {"rejected": [(f"{GID}_ACTIVITY.fit",
                            "pace_sustained_impossible: synthetic")],
              "failed": [("10000000002_ACTIVITY.fit", "synthetic read error")]}

    def rows(self, new_ids=()):
        return M.sync_rejections(self.INGEST, list(new_ids),
                                 self.state / "raw" / "garmin")

    def test_a_rejection_carries_the_exact_command(self):
        rows, problems = self.rows()
        [rej] = [r for r in rows if r["kind"] == "rejected"]
        self.assertEqual(rej["garmin_id"], self.GID)
        step = rej["next_step"]
        self.assertIn(f"uv run trail-factors confirm-file {self.GID}",
                      step["confirm_command"])
        self.assertIn("--end", step["confirm_command"])
        self.assertIn("rejected-files", step["list_command"])
        self.assertIn("sync", step["then"])
        self.assertIs(step["reachable_from_mcp"], False)
        self.assertIn("suggest_error", rej, "the missing file was survived")
        text = " ".join(problems)
        self.assertIn(self.GID, text)
        self.assertIn("pace_sustained_impossible", text)
        self.assertIn(M.CONFIRM_IS_CLI_ONLY, text)

    def test_a_failed_file_is_not_told_a_window_will_fix_it(self):
        """THE TWIN: a read error is not a validation finding."""
        rows, problems = self.rows()
        [fail] = [r for r in rows if r["kind"] == "failed"]
        self.assertNotIn("confirm_command", fail["next_step"])
        self.assertIn("will not fix", fail["next_step"]["what"])
        self.assertTrue(any("FAILED" in p for p in problems))

    def test_it_is_scoped_to_this_syncs_files(self):
        """A sync that downloaded other files does not re-raise old
        rejections -- those live in rejected-files."""
        rows, problems = self.rows(new_ids=["10000000003"])
        self.assertEqual((rows, problems), ([], []))
        rows, _ = self.rows(new_ids=[self.GID])
        self.assertEqual([r["garmin_id"] for r in rows], [self.GID])


class TestSyncIsFireAndForget(_Isolated):
    """A synchronous sync would hold the exclusive write lock for minutes and
    make every other tool call fail — it would fail the conversation, not just
    itself. Job state is sandboxed: the real job file is never read or
    written."""

    def _wait(self, jid, state):
        from trail_factors.analysis import jobs
        for _ in range(250):
            if jobs.status(jid)["state"] in (jobs.DONE, jobs.FAILED):
                break
            time.sleep(0.02)
        s = jobs.status(jid)
        self.assertEqual(s["state"], state)
        return s

    def test_sync_status_is_legible_with_no_jobs(self):
        r = json.loads(M.sync_status())
        self.assertEqual(r["outcome"], "result")

    def test_an_unknown_job_is_absent_not_broken(self):
        r = json.loads(M.sync_status(job_id="sync-9999"))
        self.assertEqual(r["outcome"], "absent")

    def test_a_job_returns_immediately_and_reports_its_steps(self):
        from trail_factors.analysis import jobs
        seen = []

        def work(progress):
            for step in ("download", "ingest"):
                seen.append(step)
                progress(step)
            return {"n_new": 0}

        jid = jobs.start(work, label="test")
        self.assertTrue(jid.startswith("sync-"))
        s = self._wait(jid, jobs.DONE)
        self.assertEqual(s["steps_done"], ["download", "ingest"])
        self.assertEqual(s["result"], {"n_new": 0})

    def test_the_job_file_is_the_sandboxed_one(self):
        """THE TWIN for every job test here: they write the sandbox's job
        file, and a test that wrote the real one would be caught."""
        from trail_factors.analysis import jobs
        jid = jobs.start(lambda progress: None, label="where")
        self._wait(jid, jobs.DONE)
        self.assertTrue(jobs.JOBS_FILE.is_relative_to(self.state))
        self.assertTrue(jobs.JOBS_FILE.exists())

    def test_polling_a_job_while_it_writes_never_loses_it(self):
        """The job file is POLLED WHILE IT IS WRITTEN, so the write must be
        atomic. It was not: `status()` reads without the lock -- deliberately,
        since job state is a file precisely so another process or a restarted
        server can read it, and a threading lock does not cross processes. A
        reader catching a half-written file fell into _load()'s except branch,
        got {}, and reported "no job <id>" for a running job.

        That is the exact failure this module was built to prevent, and it
        showed up as a suite that failed about one run in eight. This test
        reproduces it directly rather than waiting for the flake.
        """
        import threading

        from trail_factors.analysis import jobs

        jid = jobs.start(lambda progress: None, label="atomicity")
        stop = threading.Event()
        errors = []

        def churn():
            n = 0
            while not stop.is_set() and n < 400:
                n += 1
                jobs._update(jid, step=f"step-{n}")

        writer = threading.Thread(target=churn, daemon=True)
        writer.start()
        try:
            for _ in range(400):
                try:
                    self.assertIn("state", jobs.status(jid))
                except LookupError as exc:      # the bug, if it returns
                    errors.append(str(exc))
                    break
        finally:
            stop.set()
            writer.join(timeout=5)
        self.assertEqual(errors, [], "a concurrent reader lost the job")
        self._wait(jid, jobs.DONE)

    def test_a_failed_job_names_the_step_that_failed(self):
        """A Garmin outage and a corrupt file are different problems."""
        from trail_factors.analysis import jobs

        def boom(progress):
            progress("download")
            raise RuntimeError("garmin said no")

        jid = jobs.start(boom, label="test")
        s = self._wait(jid, jobs.FAILED)
        self.assertEqual(s["step"], "download")
        self.assertIn("garmin said no", s["error"])

    def test_job_state_lives_outside_data_raw_AND_outside_athlete(self):
        """Job state is operational and regenerable, so it lives apart from
        athlete state -- which means state that cannot be rebuilt from any
        source -- and outside the raw layer. Read from trail_factors.paths, the
        CONFIGURED location, not the sandbox."""
        import os
        self.assertFalse(CONFIGURED_JOBS_FILE.is_relative_to(CONFIGURED_RAW_DIR))
        self.assertFalse(CONFIGURED_JOBS_FILE.is_relative_to(CONFIGURED_ATHLETE_DIR))
        if not os.environ.get("TRAIL_FACTORS_JOBS_FILE", "").strip():
            self.assertTrue(CONFIGURED_JOBS_FILE.is_relative_to(DATA_DIR))

    def test_job_state_is_not_committed(self):
        """THE TWIN: living outside athlete state only helps if git ignores
        it where it landed."""
        self.assertTrue(git_ignores("data/jobs.json"))
        # ...and the check can fail: a source file is NOT ignored.
        self.assertFalse(git_ignores("src/trail_factors/mcp_server.py"))

    def test_the_download_path_refuses_to_overwrite(self):
        """APPEND-ONLY, structurally: §3's immutability holds even though sync
        writes into the raw layer."""
        src = (REPO_ROOT / "src" / "trail_factors" / "cli" / "garmin.py").read_text()
        self.assertEqual(src.count("refusing to overwrite existing"), 2)
        self.assertIn("if out.exists():", src)


class TestSpecContext(_Isolated):
    """The standing brief. It carries METHOD, never one athlete's numbers:
    the only athlete-specific lines come from configuration at call time."""

    HEADINGS = ("=== HOW TO READ A TOOL RESULT ===",
                "=== THE LAYERS, BY THEIR REAL NAMES ===",
                "=== THE FINDINGS THAT MAKE NUMBERS INTERPRETABLE ===",
                "=== WHAT THIS BUILD DOES NOT COLLECT ===",
                "=== ASKING FOR A QUANTITY ===",
                "=== HOW TO BEHAVE ===")

    def brief(self, **config):
        from trail_factors.analysis.context import spec_context
        with use_config(**config):
            return spec_context()

    def write_zones(self):
        """A synthetic running zone set in the SANDBOXED athlete directory."""
        from trail_factors.analysis import zones
        self.assertTrue(zones.ZONE_CONFIG.is_relative_to(self.state))
        zones.ZONE_CONFIG.write_text(json.dumps([{
            "sport": "RUNNING", "effective_from": "2025-01-01",
            "zone_floors": {"1": 100, "2": 120, "3": 140, "4": 160, "5": 175},
            "max_hr": 190}]))

    def test_it_names_the_layers_correctly(self):
        """A chat reasoning from convention assumes gold is safe to ask for."""
        b = self.brief()
        for token in ("BRONZE", "SILVER", "GOLD", "records_enriched",
                      "terrain_usable"):
            self.assertIn(token, b)

    def test_its_sections_come_in_order(self):
        b = self.brief()
        at = [b.find(h) for h in self.HEADINGS]
        self.assertNotIn(-1, at, dict(zip(self.HEADINGS, at)))
        self.assertEqual(at, sorted(at))

    def test_it_carries_method_findings_not_only_caveats(self):
        b = self.brief()
        for finding in ("NEARLY FLAT ACROSS GRADE", "BOUNDARY SET", "delta-HR",
                        "2.08x"):
            self.assertIn(finding, b)

    def test_the_goal_comes_from_config_and_only_from_config(self):
        bare = self.brief(goal_event=None)
        self.assertIn("Single athlete.", bare)
        self.assertNotIn("training for", bare)
        self.assertIn("training for a test event",
                      self.brief(goal_event="a test event"))

    def test_unconfigured_zones_are_stated_as_absent(self):
        b = self.brief()
        self.assertIn("NOT CONFIGURED", b)
        # No bpm range at all: the brief carries no measured physiology.
        import re
        self.assertIsNone(re.search(r"\b1\d\d-1\d\d\b", b),
                          "a bpm-like range appeared with no zones configured")

    def test_positive_twin_configured_zones_are_rendered(self):
        self.write_zones()
        b = self.brief()
        self.assertNotIn("NOT CONFIGURED", b)
        self.assertIn("z2 120-139", b)
        self.assertIn("z5 175-190", b)

    def test_it_states_what_cannot_be_asked_for(self):
        self.assertIn("no arbitrary aggregation", self.brief().lower())

    def test_it_asks_the_athlete_for_nothing(self):
        """Nothing in this build stores a reply, so the brief names no removed
        feature and says the uncollected values are not to be asked for."""
        self.write_zones()
        b = self.brief(goal_event="a test event")
        self.assertIsNone(REMOVED_FEATURE_WORDS.search(b),
                          REMOVED_FEATURE_WORDS.search(b))
        self.assertIn(NOT_COLLECTED, b)
        self.assertIn("Do not\nask the athlete for them", b)

    def test_it_quotes_the_run_type_rejection(self):
        """So a chat that passes run_type anyway recognises the rejection."""
        b = self.brief()
        self.assertIn(NO_RUN_TYPE_SOURCE, b)
        self.assertIn("user-defined run types", b)

    def test_the_banned_word_check_can_fire(self):
        """THE PRESENCE TWIN for the two sweeps that use it."""
        for w in _REMOVED_WORDS:
            for s in (w, w.upper(), f"a {w.capitalize()} log"):
                self.assertIsNotNone(REMOVED_FEATURE_WORDS.search(s), s)
        self.assertIsNotNone(REMOVED_FEATURE_WORDS.search("see §" + "16.4"))
        self.assertIsNone(REMOVED_FEATURE_WORDS.search("session load"))

    def test_it_is_bounded(self):
        """It has to fit in a conversation, every turn."""
        # 2500 words is ~3,300 tokens and still fits a conversation turn. The
        # guard exists to stop UNBOUNDED growth, not to hit a magic number:
        # raise it only for content that is mandated, not merely useful. It
        # stood higher while the brief carried a scripted list of questions
        # for the athlete; that content is gone, so the bound came back down.
        # Measured with zones configured, the longer form.
        self.write_zones()
        self.assertLess(len(self.brief(goal_event="a test event").split()), 2500)


if __name__ == "__main__":
    unittest.main()
