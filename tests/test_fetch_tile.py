"""Acquiring DEM tiles from 3DEP (§5a.1) — resolution, verification, bounds.

THE BUG THIS EXISTS FOR. Every missing-tile filename this project reported was
one 10 km cell too far south. USGS names a tile by its NORTH-west corner and
`tile_filename` was built on the south-west corner that `tile_grid_id` returns.
Nothing caught it because the tile on disk was always found by its own
georeferencing, never by its name -- the name was only ever printed at the
athlete, who would have fetched the wrong tile and found it did not help.

So the convention is pinned HERE: on a synthetic tile written to the USGS
layout, and -- as a measurement -- against every real tile that happens to be
on disk (those tests skip when none is loaded).

The second class of bug is the one the network path invites: a truncated
download, an HTML error page, or a valid tile for the wrong ground, all of
which arrive as a file and none of which fail until much later inside an
elevation lookup. Every one of them is refused before the file is kept.

Every negative has a positive twin.
"""

from __future__ import annotations

import http.server
import io
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile

from tests.corpus import use_config
from trail_factors.transform import dem as D
from trail_factors.transform import dem_fetch as F

# SYNTHETIC GROUND. Every fixture below is a cell of the NAD83 / UTM 16N grid
# (EPSG:26916) around cell x40y500 -- arbitrary US ground, chosen only because
# 3DEP is US-only and `resolve` refuses anything else before a request.
GRID = "EPSG:26916"
CELL = (40, 500)  # as `tile_grid_id` indexes it (SW corner)
USGS_CELL = "x40y501"  # as USGS names it (NW corner)
SUFFIX = "XX_Synthetic_2020"
# Top-left corner of a small synthetic tile whose centre lies inside CELL.
EASTING, NORTHING = 400_000.0, 5_010_000.0


# THE CELL GRID IS THE CONFIGURED PROJECTION'S UTM ZONE. The fixtures are
# cells of GRID, so that projection is pinned for the whole module rather
# than read from whatever config/athlete.yaml holds.
_CONFIG = None


def setUpModule():
    global _CONFIG
    _CONFIG = use_config(projection_crs=GRID)
    _CONFIG.__enter__()


def tearDownModule():
    _CONFIG.__exit__(None, None, None)


def _write_geotiff(
    path: Path,
    easting: float = EASTING,
    northing: float = NORTHING,
    epsg: int = 26916,
    geokeys: bool = True,
    shape=(16, 16),
) -> Path:
    """A tiny but genuinely georeferenced GeoTIFF, placed where we say."""
    extratags = [
        (33550, "d", 3, (1.0, 1.0, 0.0), True),
        (33922, "d", 6, (0.0, 0.0, 0.0, easting, northing, 0.0), True),
    ]
    if geokeys:
        extratags.append((34735, "H", 8, (1, 1, 0, 1, 3072, 0, 1, epsg), True))
    tifffile.imwrite(path, np.zeros(shape, dtype=np.float32), extratags=extratags)
    return path


def _usgs_named_tiles() -> list[Path]:
    """Loaded tiles in the `USGS_one_meter_x..y.._<project>` layout."""
    return [
        p
        for p in D.loaded_tiles()
        if len(p.stem.split("_")) > 4 and p.stem.split("_")[3].startswith("x")
    ]


def _suffix_of(p: Path) -> str:
    return "_".join(p.stem.split("_")[4:])


class TestTheUSGSNamingConvention(unittest.TestCase):
    """The y in a USGS name is the cell's TOP edge."""

    def test_a_tile_laid_out_as_usgs_does_is_named_for_its_NORTH_west_corner(self):
        """A full 10 km tile whose top-left corner sits on a cell corner, as a
        USGS tile does: the name its own centre produces is the y of its TOP
        edge, one above the cell `tile_grid_id` returns."""
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "t.tif", shape=(4, 4))
            t = D.DemTile(p)
            # Stretch the georeferencing to a full cell without writing
            # 400 MB of zeros: only the centre's cell is under test.
            t.sx = t.sy = D.TILE_SPAN_M / 4
            x = t.ox + t.width * t.sx / 2
            y = t.oy - t.height * t.sy / 2
            tx, ty = D.tile_grid_id(x, y)
            self.assertEqual((tx, ty), CELL)
            self.assertEqual(t.oy / D.TILE_SPAN_M, ty + 1)  # name's y = TOP
            self.assertEqual(
                D.tile_filename(tx, ty, SUFFIX),
                f"USGS_one_meter_{USGS_CELL}_{SUFFIX}.tif",
            )

    def test_every_loaded_tile_is_named_for_its_NORTH_west_corner(self):
        """MEASURED AGAINST THE FILES, not against a comment: for each real
        USGS tile on disk, the name's y is its top edge."""
        tiles = _usgs_named_tiles()
        if not tiles:
            self.skipTest("no USGS-named DEM tile is loaded in the DEM dir")
        for p in tiles:
            with self.subTest(tile=p.name):
                t = D.DemTile(p)
                _, ty = F.parse_cell(p.name)
                self.assertAlmostEqual(t.oy / D.TILE_SPAN_M, ty + 1, places=2)
                self.assertLess(t.oy - t.height * t.sy, t.oy)

    def test_a_point_on_a_loaded_tile_names_that_tile(self):
        """THE WHOLE POINT. Take ground a tile demonstrably covers, run it
        through the same path `coverage()` uses, and require the filename that
        comes out to be the file that is sitting there."""
        tiles = _usgs_named_tiles()
        if not tiles:
            self.skipTest("no USGS-named DEM tile is loaded in the DEM dir")
        for p in tiles:
            with self.subTest(tile=p.name):
                t = D.DemTile(p)
                x = t.ox + t.width * t.sx / 2
                y = t.oy - t.height * t.sy / 2
                tx, ty = D.tile_grid_id(x, y)
                self.assertEqual(D.tile_filename(tx, ty, _suffix_of(p)), p.name)

    def test_the_two_conventions_differ_by_exactly_one_in_y(self):
        self.assertEqual(
            D.tile_filename(*CELL, "S"), f"USGS_one_meter_{USGS_CELL}_S.tif"
        )
        self.assertEqual(F.parse_cell(USGS_CELL), CELL)

    def test_parse_cell_round_trips_the_name_the_report_prints(self):
        for tx, ty in (CELL, (-29, 422), (0, 0)):
            name = D.tile_filename(tx, ty, SUFFIX)
            self.assertEqual(F.parse_cell(name), (tx, ty))


class TestACellFromAnotherZoneIsRefusedNotMisread(unittest.TestCase):
    """The same x/y in two adjacent UTM zones is ~600 km apart. Reading one
    as the other would fetch, verify and KEEP a tile for the wrong ground."""

    def test_a_zone_marked_filename_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            F.parse_cell("USGS_1M_15_x40y501_XX_Synthetic_2020_D20.tif")
        self.assertIn("zone 15", str(ctx.exception))

    def test_our_own_grid_name_is_accepted(self):
        """THE TWIN: the guard must not reject the names `dem` actually prints."""
        self.assertEqual(F.parse_cell(f"USGS_one_meter_{USGS_CELL}_{SUFFIX}.tif"), CELL)


class TestNothingOutsideTheUS(unittest.TestCase):
    def test_a_foreign_cell_is_refused_before_a_request_is_sent(self):
        with mock.patch.object(F, "_open") as op:
            # Far west of zone 16N: the Pacific, not US ground.
            with self.assertRaises(F.NotInTheUS):
                F.resolve(-300, 500)
        op.assert_not_called()

    def test_a_US_cell_is_not_refused(self):
        """THE TWIN: a bounds check that refused everything would pass above."""
        self.assertIsNotNone(F._in_the_us(F.cell_bbox(*CELL)))

    def test_the_download_host_is_an_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                F.download(
                    {
                        "url": "https://example.com/tile.tif",
                        "filename": "tile.tif",
                        "size_bytes": 1,
                    },
                    dest_dir=Path(d),
                    progress=lambda *_: None,
                )

    def test_the_allowlist_holds_only_the_USGS_host(self):
        self.assertEqual(F.ALLOWED_HOSTS, ("prd-tnm.s3.amazonaws.com",))


class TestVerificationRefusesWhatIsNotATile(unittest.TestCase):
    def test_an_html_error_page_is_not_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tile.tif"
            p.write_bytes(b"<html><body>404 Not Found</body></html>")
            with self.assertRaises(F.NotAGeoTIFF):
                F.verify_tile(p)

    def test_a_truncated_tile_is_not_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "t.tif")
            half = p.read_bytes()[: len(p.read_bytes()) // 2]
            p.write_bytes(half)
            with self.assertRaises(F.NotAGeoTIFF):
                F.verify_tile(p)

    def test_a_tiff_with_no_CRS_is_not_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "t.tif", geokeys=False)
            with self.assertRaises(F.NotAGeoTIFF) as ctx:
                F.verify_tile(p)
            self.assertIn("ProjectedCSTypeGeoKey", str(ctx.exception))

    def test_a_valid_tile_for_the_WRONG_GROUND_is_not_kept(self):
        """The index returning a neighbour, or our cell arithmetic being off by
        one, both look like a perfectly good GeoTIFF."""
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "t.tif")
            with self.assertRaises(F.NotAGeoTIFF) as ctx:
                F.verify_tile(p, cell=(45, 450))  # ~500 km away
            self.assertIn("not on the ground", str(ctx.exception))

    def test_a_short_file_is_refused_against_the_declared_size(self):
        """The index says how many bytes the tile is. A download that ends
        early is short, and that is the cheapest possible detector."""
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "t.tif")
            with self.assertRaises(F.NotAGeoTIFF) as ctx:
                F.verify_tile(p, expect_bytes=p.stat().st_size + 1)
            self.assertIn("truncated download", str(ctx.exception).lower())

    def test_the_declared_size_MATCHING_is_accepted(self):
        """THE TWIN."""
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "t.tif")
            self.assertEqual(
                F.verify_tile(p, expect_bytes=p.stat().st_size)["size_bytes"],
                p.stat().st_size,
            )

    def test_a_valid_tile_for_the_RIGHT_ground_IS_kept(self):
        """THE TWIN: a verifier that refused everything would pass all four
        tests above and refuse every real download."""
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "t.tif")
            info = F.verify_tile(p, cell=CELL)
            self.assertEqual(info["crs"], GRID)
            self.assertEqual(info["pixel_m"], 1.0)

    def test_every_loaded_tile_verifies_against_its_own_cell(self):
        """Against real files when any are loaded: each one's georeferencing
        lands on the cell its name describes, in its own grid."""
        tiles = _usgs_named_tiles()
        if not tiles:
            self.skipTest("no USGS-named DEM tile is loaded in the DEM dir")
        for p in tiles:
            with self.subTest(tile=p.name):
                crs = D.DemTile(p).crs
                with use_config(projection_crs=crs):
                    info = F.verify_tile(p, cell=F.parse_cell(p.name))
                self.assertEqual(info["crs"], crs)
                self.assertGreater(info["width"], 0)


class TestTheDownloadIsAtomic(unittest.TestCase):
    def test_a_failed_download_leaves_nothing_behind(self):
        """A half-written tile must never be visible to TileSet, which globs
        the directory and would load it on the next call."""
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            product = {
                "url": f"https://{F.ALLOWED_HOSTS[0]}/x.tif",
                "filename": "x.tif",
                "size_bytes": 100,
            }

            class _Resp:
                # A size is stated, so this reaches the read it exists to
                # test (a missing size is refused before any byte is read).
                headers = {"Content-Length": "100"}

                def read(self, _n):
                    raise OSError("connection reset")

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            with mock.patch.object(F, "_open", return_value=_Resp()):
                with self.assertRaises(OSError):
                    F.download(product, dest_dir=dest, progress=lambda *_: None)
            self.assertEqual(list(dest.iterdir()), [])

    def test_a_tile_that_fails_verification_is_deleted_not_kept(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            body = b"<html>404</html>"
            product = {
                "url": f"https://{F.ALLOWED_HOSTS[0]}/x.tif",
                "filename": "x.tif",
                "size_bytes": len(body),
            }

            class _Resp:
                def __init__(self):
                    self._b = [body, b""]

                def read(self, _n):
                    return self._b.pop(0)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            with mock.patch.object(F, "_open", return_value=_Resp()):
                with self.assertRaises(F.NotAGeoTIFF):
                    F.download(product, dest_dir=dest, progress=lambda *_: None)
            self.assertEqual(list(dest.iterdir()), [])

    def _served(self, body, content_length, index_size):
        class _Resp:
            headers = (
                {}
                if content_length is None
                else {"Content-Length": str(content_length)}
            )

            def __init__(self):
                self._b = [body, b""]

            def read(self, _n):
                return self._b.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp(), {
            "url": f"https://{F.ALLOWED_HOSTS[0]}/t.tif",
            "filename": "t.tif",
            "size_bytes": index_size,
        }

    def test_a_STALE_INDEX_size_does_not_refuse_a_complete_download(self):
        """THE STALE-INDEX CASE. USGS re-wrote the object; the index still
        declares the old size. Every byte the server declared arrived."""
        with tempfile.TemporaryDirectory() as d:
            body = _write_geotiff(Path(d) / "src.tif").read_bytes()
            dest = Path(d) / "dem"
            resp, product = self._served(body, len(body), len(body) + 2921)
            with mock.patch.object(F, "_open", return_value=resp):
                res = F.download(
                    product, dest_dir=dest, cell=CELL, progress=lambda *_: None
                )
            self.assertEqual(res["status"], "fetched")
            self.assertIs(res["index_size_agrees"], False)
            self.assertEqual(res["served_bytes"], len(body))

    def test_NO_CONTENT_LENGTH_is_refused_even_when_the_body_is_whole(self):
        """THE FALLBACK IS GONE. A valid tile whose byte count even matches
        the index is refused when the server states no size: without it a
        dropped chunked response is indistinguishable from a whole one."""
        with tempfile.TemporaryDirectory() as d:
            body = _write_geotiff(Path(d) / "src.tif").read_bytes()
            dest = Path(d) / "dem"
            for index_size in (len(body), 0):
                resp, product = self._served(body, None, index_size)
                with mock.patch.object(F, "_open", return_value=resp):
                    with self.assertRaises(F.NotAGeoTIFF) as ctx:
                        F.download(product, dest_dir=dest, progress=lambda *_: None)
                self.assertIn("no Content-Length", str(ctx.exception))
                self.assertFalse(
                    dest.exists() and list(dest.iterdir()), "nothing may be left behind"
                )

    def test_a_download_short_of_CONTENT_LENGTH_is_still_refused(self):
        """THE TWIN: the guard against truncation is unchanged in strength --
        a dropped connection ends short of what the server declared."""
        with tempfile.TemporaryDirectory() as d:
            body = _write_geotiff(Path(d) / "src.tif").read_bytes()
            dest = Path(d) / "dem"
            resp, product = self._served(body, len(body) + 1, len(body) + 1)
            with mock.patch.object(F, "_open", return_value=resp):
                with self.assertRaises(F.NotAGeoTIFF) as ctx:
                    F.download(product, dest_dir=dest, progress=lambda *_: None)
            self.assertIn("Truncated", str(ctx.exception))
            self.assertEqual(list(dest.iterdir()), [])

    def test_the_move_into_place_is_a_replace(self):
        src = Path("src/trail_factors/transform/dem_fetch.py").read_text()
        self.assertIn("os.replace", src)
        self.assertIn(".part", src)


class TestTheLookupPathStaysOffline(unittest.TestCase):
    """§5a: the elevation LOOKUP is local. Acquisition is a separate module,
    and this is what keeps them separate."""

    def test_dem_py_opens_no_socket(self):
        body = Path("src/trail_factors/transform/dem.py").read_text()
        for banned in ("urllib", "requests", "http", "socket"):
            self.assertNotIn(banned, body, banned)

    def test_the_fetch_module_is_never_imported_by_the_lookup(self):
        for path in Path("src/trail_factors/transform").glob("*.py"):
            if path.name == "dem_fetch.py":
                continue
            self.assertNotIn("dem_fetch", path.read_text(), str(path))

    def test_the_CLI_may_import_it(self):
        """THE TWIN: the boundary test must be able to fail."""
        self.assertIn("dem_fetch", Path("src/trail_factors/cli/terrain.py").read_text())


class TestPerTileCRS(unittest.TestCase):
    """A tile from another UTM zone read under the configured one would place
    every pixel hundreds of kilometres away. A corpus with runs in more than
    one zone makes this live, not hypothetical."""

    def test_each_tile_reports_its_own_crs(self):
        """A tile from ANOTHER zone than the configured one reads as its own."""
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "other.tif", epsg=26918)
            self.assertEqual(D.DemTile(p).crs, "EPSG:26918")

    def test_a_tile_in_the_configured_zone_still_reads_as_that_zone(self):
        """THE TWIN: reading the GeoKey must not lose the ordinary case."""
        with tempfile.TemporaryDirectory() as d:
            p = _write_geotiff(Path(d) / "home.tif")
            self.assertEqual(D.DemTile(p).crs, GRID)


class TestTheRefusalListIsTheWholeInput(unittest.TestCase):
    def test_missing_reads_coverage_and_nothing_else(self):
        """`--missing` must fetch what the corpus has already refused, not
        tiles NEAR where the athlete runs -- a wider set would be a different
        disclosure than the one that was agreed."""
        src = Path("src/trail_factors/cli/terrain.py").read_text()
        body = src[src.index("def corpus_missing_cells") : src.index("def fetch_tile")]
        self.assertIn("coverage(", body)
        self.assertIn("read_only=True", body)
        for widening in ("buffer", "neighbour", "neighbor", "adjacent"):
            self.assertNotIn(widening, body.lower())


class TestTheCoverageReportNamesAWorkingCommand(unittest.TestCase):
    def test_a_gap_carries_a_fetch_command_and_flags_the_guess(self):
        tiles = D.TileSet([])
        cov = tiles.coverage([45.20, 45.21], [-87.50, -87.49])
        m = cov["missing_tiles"][0]
        self.assertIn("fetch-tile", m["fetch_command"])
        self.assertIn(m["usgs_cell"], m["fetch_command"])
        self.assertFalse(m["filename_is_resolved"])
        self.assertTrue(m["in_grid_crs_zone"])  # inside zone 16N

    def test_ground_outside_the_grid_zone_says_its_xy_is_not_a_usgs_name(self):
        """THE TWIN, and the reason the flag exists: two zones east the cell
        id is a valid label for OUR grid and a 404 as a filename."""
        cov = D.TileSet([]).coverage([44.0], [-72.6])
        self.assertFalse(cov["missing_tiles"][0]["in_grid_crs_zone"])


class TestARedirectCannotLeaveTheAllowlist(unittest.TestCase):
    """THE ALLOWLIST USED TO STOP AT THE FIRST URL. `urlopen` follows
    redirects by itself and the final URL was never re-checked, so a 302 from
    the allowed host could have sent the download anywhere. Every hop is now
    checked, and so is where the response finally came from."""

    ALLOWED = F.ALLOWED_HOSTS[0]

    def _redirect(self, newurl, start=None):
        handler = F._AllowlistRedirects(F.ALLOWED_HOSTS)
        req = urllib.request.Request(
            start if start is not None else f"https://{self.ALLOWED}/a.tif"
        )
        return handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, newurl)

    def test_a_redirect_to_another_host_is_refused(self):
        with self.assertRaises(F.OffAllowlist) as ctx:
            self._redirect("https://elsewhere.invalid/a.tif")
        self.assertIn("elsewhere.invalid", str(ctx.exception))
        self.assertIn("Nothing was written", str(ctx.exception))

    def test_a_redirect_that_drops_https_is_refused(self):
        with self.assertRaises(F.OffAllowlist):
            self._redirect(f"http://{self.ALLOWED}/a.tif")

    def test_a_redirect_that_stays_on_the_allowlist_is_followed(self):
        """THE TWIN: a handler that refused every redirect would pass both
        tests above and break any legitimate move inside the bucket."""
        new = self._redirect(f"https://{self.ALLOWED}/moved/a.tif")
        self.assertIsNotNone(new)
        self.assertEqual(new.full_url, f"https://{self.ALLOWED}/moved/a.tif")

    def test_a_final_url_off_the_allowlist_is_refused(self):
        """The second, independent guard: whatever the handler did, the
        response's own final URL is checked before a byte is read."""
        closed = []

        class _Resp:
            def geturl(self):
                return "https://elsewhere.invalid/a.tif"

            def close(self):
                closed.append(True)

        class _Opener:
            def open(self, url, timeout=None):
                return _Resp()

        with mock.patch.object(F, "_build_opener", return_value=_Opener()):
            with self.assertRaises(F.OffAllowlist) as ctx:
                F._open(f"https://{self.ALLOWED}/a.tif", F.ALLOWED_HOSTS, 1)
        self.assertIn("elsewhere.invalid", str(ctx.exception))
        self.assertEqual(closed, [True])

    def test_a_final_url_on_the_allowlist_is_returned(self):
        """THE TWIN."""

        class _Resp:
            def geturl(self):
                return f"https://{TestARedirectCannotLeaveTheAllowlist.ALLOWED}/b.tif"

        class _Opener:
            def open(self, url, timeout=None):
                return _Resp()

        with mock.patch.object(F, "_build_opener", return_value=_Opener()):
            resp = F._open(f"https://{self.ALLOWED}/a.tif", F.ALLOWED_HOSTS, 1)
        self.assertIsInstance(resp, _Resp)

    def test_both_requests_go_through_the_guarded_opener(self):
        """The index and the download each use `_open`, with their OWN
        allowlist -- a guard on one path only would leave the other open."""
        calls = []

        def fake_open(url, allowed, timeout, schemes=F.ALLOWED_SCHEMES):
            calls.append(allowed)
            raise F.OffAllowlist("stop here")

        with mock.patch.object(F, "_open", side_effect=fake_open):
            with self.assertRaises(F.OffAllowlist):
                F.resolve(*CELL)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(F.OffAllowlist):
                    F.download(
                        {
                            "url": f"https://{self.ALLOWED}/x.tif",
                            "filename": "x.tif",
                            "size_bytes": 1,
                        },
                        dest_dir=Path(d),
                        progress=lambda *_: None,
                    )
                self.assertEqual(list(Path(d).iterdir()), [])
        self.assertEqual(calls, [F.INDEX_HOSTS, F.ALLOWED_HOSTS])


class TestARealRedirectThroughTheOpener(unittest.TestCase):
    """END TO END, NO NETWORK: a real HTTP 302 from a server on 127.0.0.1,
    followed (or not) by the very opener production builds. The scheme is
    widened to http for the test only; the allowlist logic is unchanged."""

    @classmethod
    def setUpClass(cls):
        hits = cls.hits = []

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                port = self.server.server_address[1]
                if self.path == "/away":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{port}/tile")
                    self.end_headers()
                elif self.path == "/stay":
                    self.send_response(302)
                    self.send_header("Location", "/tile")
                    self.end_headers()
                else:
                    body = b"tile-bytes"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *a):
                pass

        try:
            cls.server = http.server.HTTPServer(("127.0.0.1", 0), _H)
        except OSError as exc:  # a sandbox with no loopback sockets
            raise unittest.SkipTest(f"cannot bind 127.0.0.1: {exc}") from exc
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _open(self, path):
        allowed = (f"127.0.0.1:{self.port}",)
        return F._open(
            f"http://127.0.0.1:{self.port}{path}", allowed, 5, schemes=("http",)
        )

    def test_a_302_to_another_host_is_refused_before_it_is_contacted(self):
        self.hits.clear()
        with self.assertRaises(F.OffAllowlist) as ctx:
            self._open("/away")
        self.assertIn(f"localhost:{self.port}", str(ctx.exception))
        self.assertEqual(self.hits, ["/away"])  # /tile was never asked

    def test_a_302_on_the_same_host_is_followed(self):
        """THE TWIN."""
        self.hits.clear()
        with self._open("/stay") as resp:
            self.assertEqual(resp.read(), b"tile-bytes")
        self.assertEqual(self.hits, ["/stay", "/tile"])


if __name__ == "__main__":
    unittest.main()
