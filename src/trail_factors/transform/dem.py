"""Local DEM elevation lookup (SPEC.md §5a).

REQUIREMENT, not a preference: the lookup is LOCAL. No per-coordinate query
goes to any external service, government or commercial, because a stream of
sequential coordinates is a route trace regardless of who receives it. The
tile is a public map product; downloading it reveals only that someone wanted
a map of a county.

Source: USGS 3DEP 1 metre lidar, in NAD83 / UTM (each tile states its own
zone; see DemTile).

THE PROJECT'S METRIC CRS IS CONFIGURED OR DERIVED, NEVER A CONSTANT. Path
distance, slope endpoints and segment identity are computed in a projected CRS
in metres. It used to be one UTM zone hard-coded here, which is correct only
for ground inside that zone and quietly distorts everything outside it.

  * `projection_crs` in config/athlete.yaml, when set, ALWAYS wins -- every
    run is projected in it, and behaviour and segment ids are exactly what
    they were before derivation existed.
  * Unset, each RUN is projected in the WGS84 UTM zone (EPSG:326NN / 327NN)
    of the median of ITS OWN positions, and every output that carries terrain
    says so (`resolve_projection()["basis"]`). A derived projection is a
    choice the code made, so it is LABELLED, never silent.

Two runs in different derived zones can carry numerically identical
coordinates for places hundreds of kilometres apart, so the projection is part
of segment identity (matcher.compatible / segment_id_for). The only error left
is for positions that cannot be placed at all -- nothing finite to derive a
zone from -- and it names the setting that would fix it.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from trail_factors import config
from trail_factors.paths import CONFIG_FILE, display_path
from trail_factors.paths import DEM_DIR as _DEM_DIR

import numpy as np
import tifffile
from pyproj import CRS, Transformer

DEM_DIR = _DEM_DIR
WGS84 = "EPSG:4326"
CALC_VERSION = 1

# USGS writes NoData as a large negative float32 sentinel.
NODATA_BELOW = -1e30


# --------------------------------------------------------------------------
# THE PROJECTION -- configured, read at call time, transformers cached per CRS
# --------------------------------------------------------------------------

@lru_cache(maxsize=None)
def transformer(src: str, dst: str) -> Transformer:
    """One pyproj Transformer per (src, dst) pair, built on first use.

    Cached by the CRS STRINGS, so a different configured projection gets its
    own transformer rather than silently reusing the first one built -- which
    is what a module-level transformer built at import did.
    """
    return Transformer.from_crs(src, dst, always_xy=True)


def _centre(lats, lons) -> tuple[float, float] | None:
    lats = np.asarray(lats, dtype=float).ravel()
    lons = np.asarray(lons, dtype=float).ravel()
    ok = np.isfinite(lats) & np.isfinite(lons)
    if not ok.any():
        return None
    return float(np.median(lats[ok])), float(np.median(lons[ok]))


def utm_zone_at(lat: float, lon: float) -> tuple[int, bool]:
    """(zone, is_north) of the standard 6-degree UTM zone holding a point."""
    zone = min(max(int(math.floor((lon + 180.0) / 6.0)) + 1, 1), 60)
    return zone, lat >= 0


def utm_crs_for(zone: int, north: bool) -> str:
    """The WGS84 / UTM CRS of a zone: EPSG:326NN north, EPSG:327NN south."""
    return f"EPSG:{32600 + zone if north else 32700 + zone}"


def zone_label(zone: int, north: bool) -> str:
    """'14N' / '34S' -- the zone as people write it."""
    return f"{zone}{'N' if north else 'S'}"


# THE DERIVATION RULE, NAMED so a digest can register it: a change to how an
# unconfigured run is projected changes every path distance on such a run.
DERIVED_PROJECTION_RULE = "wgs84_utm_zone_of_median_position_per_run"
CONFIGURED_BASIS = "configured"


class Unplaceable(config.ConfigError):
    """No projection can be derived: there is no finite position to place.

    A ConfigError on purpose, and its text starts with the field name: the
    one thing that makes such input projectable is a configured
    `projection_crs`, so the existing "name the missing setting" handlers in
    the report and the MCP envelope describe it correctly.
    """


def resolve_projection(lats=None, lons=None) -> dict:
    """The CRS these positions are projected in, and WHY that one.

    {"crs": "EPSG:...", "basis": "configured" | "derived from this run's
    position: UTM zone NNx", "configured": bool}. `lats`/`lons` are ONE RUN's
    positions: the zone is derived from their median, so every point of the
    run lands in the same metric frame even if the run crosses a zone line.
    Configured always wins and ignores the positions.
    """
    configured = config.get().projection_crs
    if configured is not None:
        return {"crs": configured, "basis": CONFIGURED_BASIS,
                "configured": True}
    c = _centre(lats, lons) if lats is not None and lons is not None else None
    if c is None:
        raise Unplaceable(
            "projection_crs is not set, and these positions cannot be placed: "
            "none is a finite latitude/longitude, so there is no UTM zone to "
            "derive. Set projection_crs in "
            f"{display_path(CONFIG_FILE)} to project them anyway.")
    zone, north = utm_zone_at(*c)
    return {"crs": utm_crs_for(zone, north),
            "basis": f"derived from this run's position: UTM zone "
                     f"{zone_label(zone, north)}",
            "configured": False}


def run_projection(con, activity_id: str) -> dict:
    """resolve_projection() for one stored activity, from ALL its positions.

    The one place a run's derived zone is computed from the database, so the
    silver build, the matcher's traversals and the coverage report cannot
    disagree about which zone a run is in. Raises Unplaceable for a run with
    no position when nothing is configured.
    """
    lat, lon = con.execute("""
        SELECT median(lat), median(lon) FROM records
        WHERE activity_id = ? AND lat IS NOT NULL AND lon IS NOT NULL
    """, [activity_id]).fetchone()
    return resolve_projection([] if lat is None else [lat],
                              [] if lon is None else [lon])


def public_projection(p: dict | None) -> dict | None:
    """The {crs, basis} pair outputs carry -- the labelled form of a
    resolve_projection() result."""
    return None if p is None else {"crs": p["crs"], "basis": p["basis"]}


def utm_suggestion(lats, lons) -> str | None:
    """A sentence naming the UTM zone the positions centre on, or None.

    Used in the coverage note: the NAD83 grid USGS names tiles in is what
    `fetch-tile` needs configured, which a derived projection does not give
    it.
    """
    c = _centre(lats, lons) if lats is not None and lons is not None else None
    if c is None:
        return None
    zone, north = utm_zone_at(*c)
    hemi = "N" if north else "S"
    code = f"EPSG:{32600 + zone if north else 32700 + zone}"
    text = (f"positions centre on UTM zone {zone}{hemi}: set "
            f"projection_crs: {code}")
    usgs = usgs_grid_for_zone(zone, north)
    if usgs is not None:
        text += f" (or {usgs}, the NAD83 / UTM zone USGS names DEM tiles in)"
    return text


def projection_crs(lats=None, lons=None) -> str:
    """The metric CRS for these positions: configured, else derived from them.

    Raises Unplaceable only when nothing is configured and no position is
    finite.
    """
    return resolve_projection(lats, lons)["crs"]


def project(lats, lons, crs: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    """WGS84 positions to a metric CRS, as (x, y) in metres.

    `crs` is the projection already resolved for the RUN these points belong
    to (run_projection). Pass it whenever the points are a FEW of a run's
    positions -- a slope endpoint, a peak: deriving a zone from one point
    would put two ends of one slope in different frames when a run crosses a
    zone line. Without it the zone is derived from `lats`/`lons` themselves,
    which is right only when they are the whole run.
    """
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    if crs is None:
        if lats.size == 0:
            # Nothing to place and nothing to say about where: no CRS needed.
            return np.asarray(lons, dtype=float), np.asarray(lats, dtype=float)
        crs = projection_crs(lats, lons)
    x, y = transformer(WGS84, crs).transform(lons, lats)
    return np.asarray(x, dtype=float), np.asarray(y, dtype=float)


class OutsideTile(ValueError):
    """A coordinate falls outside the loaded tile.

    Raised rather than returning NaN: a point outside the tile must fail
    LOUDLY, never return NoData that gets silently smoothed over (§5a).
    """


def _crs_of(tf: "tifffile.TiffFile", name: str) -> str:
    """The tile's declared projected CRS, from the GeoTIFF GeoKeys.

    Falls back to the CONFIGURED projection only if the key is absent, and
    refuses when there is none -- a silent fallback to a fixed zone here would
    reintroduce exactly the zone bug this exists to prevent.
    """
    code = (tf.geotiff_metadata or {}).get("ProjectedCSTypeGeoKey")
    if code is not None:
        return f"EPSG:{int(code)}"
    configured = config.get().projection_crs
    if configured is None:
        raise ValueError(
            f"{name} carries no ProjectedCSTypeGeoKey and projection_crs is "
            f"not set, so nothing says where its pixels are")
    return configured


class DemTile:
    """One GeoTIFF, loaded once, queried locally."""

    def __init__(self, path: Path):
        # NO DEFAULT TILE. A default named one particular 10 km cell, so any
        # caller that forgot to say which tile silently read that one. A
        # caller wanting "whatever is on disk" uses TileSet().
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing. Download it from USGS 3DEP; it is gitignored "
                f"because it is ~430 MB of public data and re-downloadable.")
        self.path = path
        with tifffile.TiffFile(path) as tf:
            page = tf.pages[0]
            tags = {t.name: t.value for t in page.tags.values()}
            scale = tags["ModelPixelScaleTag"]
            tie = tags["ModelTiepointTag"]
            self.sx, self.sy = float(scale[0]), float(scale[1])
            self.ox, self.oy = float(tie[3]), float(tie[4])
            self.height, self.width = page.shape
            # THE RASTER IS LOADED ON FIRST ELEVATION LOOKUP, NOT HERE. It is
            # ~400 MB per tile (10012 x 10012 float32), and coverage -- which
            # every report computes, through a fresh TileSet -- needs only the
            # georeferencing above. Reading it eagerly made each report build
            # read and discard ~1.2 GB with three tiles on disk: most of the
            # build time, measured on the development corpus.
            self._grid = None
            # EACH TILE'S OWN CRS, NOT THE MODULE CONSTANT.
            #
            # One fixed CRS was once applied to every tile, which is true
            # inside that UTM zone and false everywhere else. A corpus with
            # runs in several zones would read a tile from any other zone
            # hundreds of kilometres from where it belongs. Every USGS
            # GeoTIFF states its own EPSG in ProjectedCSTypeGeoKey, so read
            # it rather than assume it.
            self.crs = _crs_of(tf, path.name)
        self.to_utm = transformer(WGS84, self.crs)

    @property
    def grid(self) -> np.ndarray:
        """The elevation raster, read on first use and kept."""
        if self._grid is None:
            with tifffile.TiffFile(self.path) as tf:
                self._grid = tf.pages[0].asarray()
        return self._grid

    def _pixel(self, lats, lons):
        xs, ys = self.to_utm.transform(np.asarray(lons), np.asarray(lats))
        return (np.asarray(xs) - self.ox) / self.sx, (self.oy - np.asarray(ys)) / self.sy

    def elevation(self, lats, lons) -> np.ndarray:
        """Bilinearly interpolated elevation, in metres.

        Bilinear rather than nearest: at 1 m posting, nearest-neighbour adds a
        quantisation step to every sample, and that noise is then differentiated
        into grade (§9).
        """
        col, row = self._pixel(lats, lons)
        outside = (col < 0) | (col >= self.width - 1) | (row < 0) | (row >= self.height - 1)
        if outside.any():
            n = int(outside.sum())
            raise OutsideTile(
                f"{n} of {len(col)} coordinates fall outside {self.path.name}. "
                f"A tile that does not cover the run must fail loudly rather "
                f"than return NoData (§5a). Fetch the neighbouring tile.")
        c0, r0 = np.floor(col).astype(int), np.floor(row).astype(int)
        fc, fr = col - c0, row - r0
        g = self.grid
        v = (g[r0, c0] * (1 - fc) * (1 - fr) + g[r0, c0 + 1] * fc * (1 - fr)
             + g[r0 + 1, c0] * (1 - fc) * fr + g[r0 + 1, c0 + 1] * fc * fr)
        if (v < NODATA_BELOW).any():
            raise OutsideTile(
                f"{int((v < NODATA_BELOW).sum())} coordinates hit NoData inside "
                f"{self.path.name} — a hole in the lidar coverage, not a miss.")
        return v


# --------------------------------------------------------------------------
# TILE COVERAGE — knowing what is NOT covered, and naming the fix
# --------------------------------------------------------------------------

# USGS 3DEP 1 m tiles are named for the 10 km grid cell they cover, in the
# tile's own NAD83 / UTM zone -- BY THE NORTH-WEST CORNER, not the south-west.
#
# MEASURED, NOT ASSUMED. A real tile's ModelTiepointTag (its top-left corner)
# sits on the y in its name, so the name's y is the TOP edge of the cell. An
# earlier version assumed the bottom edge, and `tile_filename` was built on
# that, so every missing-tile filename this module reported named the cell
# one south of the one needed. The 3DEP index confirms it independently: the
# cell our floor-based id calls x..y(N) is served by a file named x..y(N+1).
#
# The GROUND we report (easting_range / northing_range) was always right;
# only the NAME was wrong.
TILE_SPAN_M = 10_000

# THE GRID IS THE CONFIGURED PROJECTION'S UTM ZONE, IN NAD83. USGS names a 1 m
# tile in NAD83 / UTM zone NN (EPSG:269NN), so the grid every cell id is
# computed in is derived from `projection_crs`'s zone. Outside that zone's
# 6-degree band the x/y pair is a valid label for OUR grid and meaningless as
# a USGS filename -- the real tile is named in ITS OWN zone. Cells outside it
# must have their filename RESOLVED, never constructed.
#
# The acquisition-project suffix is NOT derivable from coordinates either: a
# different trail may sit in a different USGS project. It is taken from the
# tiles already on disk, else from `dem_tile_suffix_guess` in the config, and
# is reported as a GUESS, because a filename stated with false confidence
# sends the athlete to a 404. With neither, the filename is left unresolved.


def loaded_tiles() -> list[Path]:
    """Every DEM tile on disk. Adding a file is the whole installation step."""
    return sorted(DEM_DIR.glob("USGS_*.tif")) if DEM_DIR.exists() else []


def tile_grid_id(x: float, y: float) -> tuple[int, int]:
    """The 10 km grid cell a projected coordinate falls in."""
    return int(x // TILE_SPAN_M), int(y // TILE_SPAN_M)


def tile_filename(tx: int, ty: int, suffix: str) -> str:
    """The USGS filename for the grid cell `tile_grid_id` returned.

    Takes the cell as this module indexes it (by its SOUTH-west corner, the
    natural output of a floor division) and emits the name USGS uses (its
    NORTH-west corner), which is one cell higher in y. The two conventions
    differing by one is the whole reason this is a function.
    """
    return f"USGS_one_meter_x{tx}y{ty + 1}_{suffix}.tif"


def usgs_grid_for_zone(zone: int, north: bool) -> str | None:
    """EPSG:269NN, the NAD83 / UTM CRS USGS names tiles in, or None.

    NAD83 / UTM is defined for northern zones 1-23 only (EPSG:26901-26923),
    which spans every 3DEP longitude in North America. Anything else has no
    USGS tile name to construct.
    """
    return f"EPSG:{26900 + zone}" if north and 1 <= zone <= 23 else None


def utm_zone_of(crs: str) -> tuple[int, bool] | None:
    """(zone, is_north) when `crs` is a UTM projection, else None."""
    z = CRS.from_user_input(crs).utm_zone
    if not z:
        return None
    return int(z[:-1]), z[-1].upper() == "N"


def zone_lon_band(zone: int) -> tuple[float, float]:
    """The longitudes a UTM zone describes."""
    lo = -180.0 + 6.0 * (zone - 1)
    return lo, lo + 6.0


def usgs_grid_crs() -> str | None:
    """The USGS tile grid implied by the CONFIGURED projection, or None.

    None when no projection is configured, or when it is not a northern UTM
    zone NAD83 covers -- in both cases no USGS filename can be constructed.
    """
    crs = config.get().projection_crs
    if crs is None:
        return None
    uz = utm_zone_of(crs)
    return usgs_grid_for_zone(*uz) if uz is not None else None


def _coverage_grid(lats, lons) -> tuple[str, bool, int | None,
                                        bool | None, bool]:
    """The CRS missing cells are labelled in, for coverage().

    Returns (grid_crs, is_usgs_grid, zone, is_north, projection_configured);
    zone and is_north are None for a configured CRS that is not UTM. THE
    HEMISPHERE TRAVELS WITH THE ZONE: zone 18 alone names both Patagonia (18S)
    and Vermont (18N), and a zone number read without it lands a southern
    cell's x/y on northern ground. With a
    configured UTM projection inside NAD83's zones this is that zone's USGS
    grid -- the same grid `fetch-tile` resolves cells in. With no projection
    the cells are still LABELLED (a report must name its gap) in the zone
    DERIVED from the run's positions -- the same zone its terrain is
    projected in -- and the filename is left unresolved.

    `lats`/`lons` are the WHOLE run, not only its uncovered points, so the
    labelling zone and the projection zone cannot disagree.
    """
    crs = config.get().projection_crs
    if crs is not None:
        uz = utm_zone_of(crs)
        grid = usgs_grid_for_zone(*uz) if uz is not None else None
        if grid is not None:
            return grid, True, uz[0], uz[1], True
        return (crs, False, (uz[0] if uz is not None else None),
                (uz[1] if uz is not None else None), True)
    c = _centre(lats, lons)
    zone, north = utm_zone_at(*c) if c is not None else (1, True)
    grid = usgs_grid_for_zone(zone, north)
    if grid is not None:
        return grid, True, zone, north, False
    return utm_crs_for(zone, north), False, zone, north, False


def _no_fetch_reason(grid: str, zone: int | None,
                     north: bool | None) -> str:
    """Why a cell labelled in `grid` has no fetch command. Only called when
    `usgs_grid_for_zone` has already returned None for this zone, so the
    reason states what was checked and nothing more."""
    if zone is None:
        return (f"no USGS grid exists for {grid}: it is not a UTM projection, "
                f"so no USGS tile name can be computed in it and fetch-tile "
                f"cannot resolve this cell. No fetch command is given.")
    return (f"no USGS grid exists for UTM zone {zone_label(zone, north)}: "
            f"NAD83 / UTM, the grid USGS names 3DEP tiles in, is defined for "
            f"northern zones 1-23 only. A `--zone {zone}` would be read as "
            f"zone {zone}N and name different ground, so no fetch command is "
            f"given; a tile for this ground has to come from elsewhere.")


def _in_grid_zone(tx: int, ty: int, grid: str, zone: int | None) -> bool:
    """Is this cell inside the longitudes the grid's UTM zone describes?

    Emits a BOOLEAN, never the longitude it tested -- coverage() may report a
    10 km cell and must not report a position (§5a).
    """
    if zone is None:
        return False
    lon, _ = transformer(grid, WGS84).transform((tx + 0.5) * TILE_SPAN_M,
                                                (ty + 0.5) * TILE_SPAN_M)
    lo, hi = zone_lon_band(zone)
    return lo <= lon <= hi


def _suffix_guess() -> str | None:
    """The acquisition project of the tiles already present, else the
    configured guess, else None (the filename is then left unresolved)."""
    for p in loaded_tiles():
        parts = p.stem.split("_")
        if len(parts) > 4 and parts[3].startswith("x"):
            return "_".join(parts[4:])
    return config.get().dem_tile_suffix_guess


class TileSet:
    """Every loaded tile, queried as one surface.

    WHY A SET RATHER THAN A TILE. `DemTile` took a single path defaulting to
    the first trail's tile, so a run anywhere else failed and was counted as "outside the
    loaded tile" with no indication of which tile would fix it. Adding a
    second trail meant editing code. Now adding a tile to data/dem/ is the
    entire installation step, and a run outside every tile can be TOLD what
    to fetch.
    """

    def __init__(self, paths: list[Path] | None = None):
        self.tiles = [DemTile(p) for p in (paths if paths is not None
                                           else loaded_tiles())]

    def __bool__(self) -> bool:
        return bool(self.tiles)

    @property
    def names(self) -> list[str]:
        return [t.path.name for t in self.tiles]

    def elevation(self, lats, lons, with_tiles: bool = False):
        """Elevation from whichever tile covers each point.

        Still raises OutsideTile when a point is covered by NONE of them: a
        run that leaves the loaded surface must fail loudly (§5a), and the
        caller turns that into a named, actionable gap rather than a NaN.

        `with_tiles=True` also returns, per point, the FILENAME of the tile it
        came from. `dem_elevations` stores that per row, and with more than one
        tile loaded it is no longer a property of the run -- a track crossing a
        tile boundary draws from both. The single-tile era let the caller name
        the file once; a multi-tile TileSet cannot.
        """
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        out = np.full(lats.shape, np.nan)
        names = np.full(lats.shape, "", dtype=object)
        remaining = np.ones(lats.shape, dtype=bool)
        for tile in self.tiles:
            if not remaining.any():
                break
            idx = np.flatnonzero(remaining)
            col, row = tile._pixel(lats[idx], lons[idx])
            inside = ((col >= 0) & (col < tile.width - 1)
                      & (row >= 0) & (row < tile.height - 1))
            if not inside.any():
                continue
            take = idx[inside]
            out[take] = tile.elevation(lats[take], lons[take])
            names[take] = tile.path.name
            remaining[take] = False
        if remaining.any():
            raise OutsideTile(
                f"{int(remaining.sum())} of {lats.size} coordinates fall "
                f"outside every loaded tile ({', '.join(self.names) or 'none'})")
        return (out, names) if with_tiles else out

    def coverage(self, lats, lons) -> dict:
        """What is covered, what is not, and the tiles that would fix it.

        NEVER emits a coordinate. It reports the 10 km GRID CELLS that are
        missing, which is the information needed to fetch a tile and is two
        orders of magnitude coarser than a position -- the same reasoning that
        rounds weather coordinates to ~1.1 km before they leave the machine.
        """
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        n = int(lats.size)
        if n == 0:
            return {"n_points": 0, "n_covered": 0, "n_uncovered": 0,
                    "fraction_covered": None, "missing_tiles": [],
                    "loaded_tiles": self.names, "projection": None}
        covered = np.zeros(lats.shape, dtype=bool)
        for tile in self.tiles:
            col, row = tile._pixel(lats, lons)
            covered |= ((col >= 0) & (col < tile.width - 1)
                        & (row >= 0) & (row < tile.height - 1))
        missing = {}
        grid, usgs_grid, zone, north, configured = _coverage_grid(lats, lons)
        # THE PROJECTION THIS RUN'S TERRAIN IS COMPUTED IN, LABELLED. `crs`
        # below is the NAD83 grid tiles are NAMED in; this is the metric frame
        # path distance and segment identity use. They are different things
        # and both are stated.
        try:
            projection = public_projection(resolve_projection(lats, lons))
        except Unplaceable:
            projection = None
        if (~covered).any():
            xs, ys = transformer(WGS84, grid).transform(lons[~covered],
                                                        lats[~covered])
            # NO FILENAME WITHOUT A CONFIGURED GRID OR A SUFFIX. Either gap
            # makes the name a fabrication; None says "unresolved" outright.
            suffix = _suffix_guess() if configured and usgs_grid else None
            # NO COMMAND WHERE NO USGS GRID EXISTS. A southern zone, or a
            # northern one past 23, has no NAD83 / UTM grid; a `--zone NN`
            # here would be read as a NORTHERN zone and send fetch-tile to the
            # northern mirror of this cell -- which can be US ground, pass
            # every guard, and download a tile for the wrong continent.
            no_fetch = (None if usgs_grid
                        else _no_fetch_reason(grid, zone, north))
            for x, y in zip(np.atleast_1d(xs), np.atleast_1d(ys)):
                tx, ty = tile_grid_id(float(x), float(y))
                key = (tx, ty)
                cell = missing.setdefault(key, {"tile": f"x{tx}y{ty}",
                                                "usgs_cell": f"x{tx}y{ty + 1}",
                                                "filename": (tile_filename(tx, ty, suffix)
                                                             if suffix is not None
                                                             else None),
                                                "filename_is_resolved": False,
                                                "in_grid_crs_zone": (
                                                    usgs_grid and
                                                    _in_grid_zone(tx, ty, grid, zone)),
                                                "fetch_command": (
                                                    f"uv run trail-factors "
                                                    f"fetch-tile x{tx}y{ty + 1}"
                                                    + ("" if configured
                                                       else f" --zone {zone}")
                                                    if no_fetch is None else None),
                                                "fetch_unavailable_reason":
                                                    no_fetch,
                                                "n_points": 0,
                                                "easting_range": [tx * TILE_SPAN_M,
                                                                  (tx + 1) * TILE_SPAN_M],
                                                "northing_range": [ty * TILE_SPAN_M,
                                                                   (ty + 1) * TILE_SPAN_M]})
                cell["n_points"] += 1
        note = ("`filename` is a GUESS and `filename_is_resolved` says so: "
                "the project suffix is copied from the tiles already on disk "
                "(or the configured guess), and where `in_grid_crs_zone` is "
                "false the x/y pair is wrong too, because the real tile is "
                "named in its own UTM zone. A null `filename` means no guess "
                "could be made at all. The GROUND (easting_range/"
                "northing_range) is exact in `crs`. Run `fetch_command`, which "
                "resolves the real filename against the 3DEP index and "
                "downloads it; a null `fetch_command` means no USGS grid "
                "exists for this ground and `fetch_unavailable_reason` says "
                "why.")
        if not configured and missing:
            hint = utm_suggestion(lats, lons)
            note = ("projection_crs is not set, so terrain is projected in the "
                    "UTM zone derived from this run's position (`projection`) "
                    "and cells are labelled in that zone's grid. The filename "
                    "is UNRESOLVED; `fetch_command` carries the derived "
                    "northern zone (`--zone`), because a cell name means "
                    "nothing to `fetch-tile` without the zone it was computed "
                    "in. "
                    + (f"The {hint}. " if hint else "") + note)
        return {
            "n_points": n,
            "n_covered": int(covered.sum()),
            "n_uncovered": int((~covered).sum()),
            "fraction_covered": float(covered.mean()),
            "missing_tiles": sorted(missing.values(),
                                    key=lambda m: -m["n_points"]),
            "loaded_tiles": self.names,
            "crs": grid,
            "projection_configured": configured,
            "projection": projection,
            "filename_note": note,
        }
