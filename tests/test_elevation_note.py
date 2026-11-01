"""The report says, where the DEM figures are, why they read lower than
Garmin's. Read-only against the corpus (TRAIL_FACTORS_DB)."""

import unittest

from trail_factors.analysis import report as REP
from tests.corpus import CorpusTestCase, one, use_config


class TestTheElevationNote(CorpusTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Subject by property: the running activity with the most slope
        # traversals, i.e. one whose climb signature has content.
        aid = one(cls.con, """
            SELECT t.activity_id FROM slope_traversals t
            JOIN activities a USING (activity_id)
            WHERE a.sport = 'running'
            GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1""",
                  what="running activity with slope traversals")[0]
        # Default config: nothing here depends on the athlete's settings, and
        # sections that need one degrade on their own.
        with use_config():
            cls.rep = REP.build(cls.con, aid)

    def test_the_payload_carries_it(self):
        self.assertEqual(self.rep["elevation_note"], REP.ELEVATION_NOTE)

    def test_it_is_printed_under_the_climb_signature(self):
        lines = REP.render(self.rep).splitlines()
        i = next(k for k, l in enumerate(lines) if "CLIMB SIGNATURE" in l)
        self.assertIn(REP.ELEVATION_NOTE, lines[i + 1])

    def test_it_is_device_specific_and_names_the_reference(self):
        """~2x is the FR245's GPS altitude; the FR265 barometer is 1.08x.
        A blanket "Garmin is 2x" would be wrong for the newer watch."""
        for s in ("2.08x", "1.08x", "FR245", "FR265", "reference"):
            self.assertIn(s, REP.ELEVATION_NOTE)


if __name__ == "__main__":
    unittest.main()
