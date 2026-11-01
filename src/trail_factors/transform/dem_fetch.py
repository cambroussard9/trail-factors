"""Acquiring DEM tiles from USGS 3DEP (SPEC.md §5a.1).

WHY THIS IS NOT A VIOLATION OF THE LOCAL-ONLY RULE. §5a forbids per-coordinate
lookups against an external service, because a stream of sequential
coordinates is a route trace no matter who receives it. That rule is about
QUERYING. This module ACQUIRES: it asks "which public map product covers this
10 km square" and downloads the square. Three properties make it a different
act, and all three are enforced here rather than asserted:

  - The unit is a 10 km CELL, not a position. One request per cell, and the
    cell is derived from the refusal list, which itself never held a
    coordinate. A 10 km square is two orders of magnitude coarser than a
    position -- coarser than the ~1.1 km rounding §8 already applies before a
    weather request leaves the machine.
  - It is ONE-OFF, not per-run. A tile is fetched once and answers every run
    on that ground forever. There is no ongoing stream to correlate.
  - What it reveals is that someone wanted a map of a county. The elevation
    lookup itself stays local and stays offline: `dem.py` opens no socket, and
    nothing in this module is called from the lookup path.

The remaining exposure is honest and worth stating: the SET of cells fetched
is a coarse list of the regions the athlete runs in. That is already public
information at this granularity -- it is the county, not the trail.

NOTHING HERE IS FETCHED OUTSIDE THE UNITED STATES, structurally: the only
endpoint is the 3DEP index, 3DEP holds US data only, downloads are restricted
to the USGS host by allowlist, and a cell outside US territory is refused
before any request is sent.

THE GRID IS THE CONFIGURED PROJECTION'S UTM ZONE (`dem.usgs_grid_crs`). A cell
name like x40y500 means nothing without the zone it was computed in, so with
no `projection_crs` set nothing here can run, and it says so.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import tifffile

from trail_factors import config
from trail_factors.paths import DEM_DIR
from trail_factors.transform.dem import (TILE_SPAN_M, WGS84, transformer, usgs_grid_crs,
                               usgs_grid_for_zone, zone_label)

INDEX_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"
DATASET = "Digital Elevation Model (DEM) 1 meter"

# THE DOWNLOAD HOST IS AN ALLOWLIST, NOT A PREFERENCE. The index returns a URL
# and we do not follow it anywhere else: a redirect to another host is a
# refusal, not a fetch.
#
# ENFORCED ON EVERY HOP, NOT ONLY THE FIRST. This comment used to be true of
# the URL we were handed and false of where we ended up: `urlopen` follows
# redirects on its own and the final URL was never re-checked. Every request
# now goes through `_open`, whose redirect handler refuses any hop that is not
# https to an allowlisted host, and which re-checks the final URL after
# opening -- for the index request and the download alike.
ALLOWED_HOSTS = ("prd-tnm.s3.amazonaws.com",)
INDEX_HOSTS = (urllib.parse.urlparse(INDEX_URL).netloc,)
ALLOWED_SCHEMES = ("https",)

INDEX_TIMEOUT_S = 90
DOWNLOAD_TIMEOUT_S = 600
CHUNK = 1 << 20

# US territory, coarsely. This is the SECOND check, not the only one -- 3DEP
# holds nothing outside the US, so the dataset restriction is the structural
# guarantee. This one stops a query about foreign ground from being SENT.
US_BOXES = (
    ("CONUS",        24.0,  49.5, -125.5,  -66.5),
    ("Alaska",       51.0,  72.0, -180.0, -129.0),
    ("Hawaii",       18.5,  22.5, -161.0, -154.5),
    ("PR/USVI",      17.5,  18.6,  -68.0,  -64.5),
    ("Guam/CNMI",    13.2,  20.6,  144.5,  146.2),
    ("Am. Samoa",   -14.6, -14.1, -171.1, -169.4),
)


class NotInTheUS(ValueError):
    """The cell is outside US territory, so 3DEP cannot hold it anyway."""


class NoProductCovers(LookupError):
    """3DEP publishes no 1 m tile covering this cell.

    A REFUSAL, not an error: some ground genuinely has no 1 m lidar. Reported
    as such rather than retried or silently substituted with a coarser
    product, which would put a different resolution into the same column.
    """


class NotAGeoTIFF(ValueError):
    """What arrived is not a readable, georeferenced tile."""


class OffAllowlist(ValueError):
    """A request, a redirect or a final URL left the allowlist. Refused
    before any byte of the response was written anywhere."""


def _check_url(url: str, allowed: tuple[str, ...], what: str,
               schemes: tuple[str, ...] = ALLOWED_SCHEMES) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in schemes or parsed.netloc not in allowed:
        raise OffAllowlist(
            f"refused {what} to {parsed.netloc or url!r} "
            f"({parsed.scheme or 'no scheme'}): only "
            f"{', '.join(f'{s}://{h}' for s in schemes for h in allowed)} "
            f"is allowed. Nothing was written.")


class _AllowlistRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it stays https on an allowlisted host.

    `redirect_request` is the one place urllib consults before following a
    3xx, so refusing here means the off-list host is never contacted.
    """

    def __init__(self, allowed: tuple[str, ...],
                 schemes: tuple[str, ...] = ALLOWED_SCHEMES):
        super().__init__()
        self.allowed, self.schemes = tuple(allowed), tuple(schemes)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _check_url(newurl, self.allowed, f"a {code} redirect from "
                       f"{urllib.parse.urlparse(req.full_url).netloc}",
                       self.schemes)
        except OffAllowlist:
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(allowed: tuple[str, ...],
                  schemes: tuple[str, ...] = ALLOWED_SCHEMES):
    return urllib.request.build_opener(_AllowlistRedirects(allowed, schemes))


def _open(url: str, allowed: tuple[str, ...], timeout: float,
          schemes: tuple[str, ...] = ALLOWED_SCHEMES):
    """Open `url` with the allowlist enforced before, during and after.

    Before: the URL itself. During: every redirect hop (_AllowlistRedirects).
    After: the response's final URL, as a second, independent check -- a
    handler that someone later swaps out must not be the only guard.
    """
    _check_url(url, allowed, "a request", schemes)
    resp = _build_opener(allowed, schemes).open(url, timeout=timeout)
    final = resp.geturl() if hasattr(resp, "geturl") else getattr(resp, "url", None)
    try:
        _check_url(final if final is not None else "", allowed,
                   "the final URL of a request", schemes)
    except OffAllowlist:
        resp.close()
        raise
    return resp


# THE ZONE A CELL WAS NAMED IN, when no projection is configured. Coverage
# labels a run's missing cells in the UTM zone derived from that run's own
# positions, and its `fetch_command` carries that zone as `--zone NN`; the CLI
# sets it here for the duration of one cell's resolve and download. A
# configured projection_crs always wins over it.
#
# THE HEMISPHERE IS PART OF THE ZONE. Zone 18 is both 18N (Vermont) and 18S
# (Patagonia); read as a bare number, a southern cell's x/y lands in the
# northern grid, on ground that can be inside the US and pass every guard
# below. So the zone is set as (zone, is_north), with no default hemisphere,
# and a southern one is refused in `grid_crs` before any request.
_DERIVED_ZONE: contextvars.ContextVar[tuple[int, bool] | None] = \
    contextvars.ContextVar("derived_zone", default=None)


@contextlib.contextmanager
def derived_zone(zone: int | None, north: bool | None):
    """Resolve cells in UTM zone (`zone`, `north`)'s USGS grid.

    `zone` None means no zone was given. `north` must be stated whenever a
    zone is: there is no default hemisphere, for the reason above.
    """
    if zone is not None and north is None:
        raise ValueError(f"UTM zone {zone} was given without its hemisphere; "
                         f"pass north=True or north=False")
    token = _DERIVED_ZONE.set(None if zone is None else (zone, north))
    try:
        yield
    finally:
        _DERIVED_ZONE.reset(token)


def grid_crs() -> str:
    """The USGS grid cells are named in, or an error saying why there is none."""
    grid = usgs_grid_crs()
    if grid is not None:
        return grid
    crs = config.get().projection_crs
    if crs is None:
        derived = _DERIVED_ZONE.get()
        if derived is None:
            raise config.ConfigError(
                "projection_crs is not set and no zone was given: a cell name "
                "like x40y500 means nothing without the UTM zone it was "
                "computed in. Pass the zone the coverage report names "
                "(`fetch-tile x40y500 --zone NN`, as its fetch_command "
                "prints it), or set projection_crs. No request was sent.")
        zone, north = derived
        grid = usgs_grid_for_zone(zone, north)
        if grid is not None:
            return grid
        raise NotInTheUS(
            f"no USGS grid exists for UTM zone {zone_label(zone, north)}: "
            f"NAD83 / UTM is defined for northern zones 1-23 only, so no USGS "
            f"tile can be named in it; 3DEP holds US data only, and no "
            f"request was sent")
    raise NotInTheUS(
        f"projection_crs {crs} is not a northern UTM zone that NAD83 / UTM "
        f"covers, so it implies no USGS tile grid; 3DEP holds US data only, "
        f"and no request was sent")


def parse_cell(name: str) -> tuple[int, int]:
    """`x40y500`, or a full USGS filename, to the grid cell THIS MODULE uses.

    Accepts the USGS name (north-west corner) as `dem` reports it and returns
    the south-west-cornered pair the rest of the code indexes by, so the two
    conventions are converted in exactly one place.
    """
    stem = Path(name).stem
    parts = stem.split("_")
    # A NAME FROM ANOTHER ZONE IS NOT OUR CELL. USGS writes newer tiles as
    # `USGS_1M_NN_x..y.._...`, where NN is the UTM zone -- so the same x/y
    # means something entirely different in our configured grid. Reading it
    # as ours would fetch, verify and keep a tile for ground hundreds of
    # kilometres away. Refuse, and name what to pass instead.
    if len(parts) > 2 and parts[1] == "1M" and parts[2].isdigit():
        raise ValueError(
            f"{name!r} is named in UTM zone {parts[2]}, not the configured "
            f"grid this corpus indexes by. Pass the cell as `dem` reports it "
            f"(for example x40y500) and the real filename will be resolved.")
    token = next((p for p in parts if p.startswith("x") and "y" in p), stem)
    if not token.startswith("x") or "y" not in token:
        raise ValueError(f"{name!r} is not a tile name like x40y500")
    xs, _, ys = token[1:].partition("y")
    return int(xs), int(ys) - 1


def cell_bbox(tx: int, ty: int) -> tuple[float, float, float, float]:
    """The cell's ground as a WGS84 bbox: min lon, min lat, max lon, max lat."""
    tf = transformer(grid_crs(), WGS84)
    x0, y0 = tx * TILE_SPAN_M, ty * TILE_SPAN_M
    x1, y1 = x0 + TILE_SPAN_M, y0 + TILE_SPAN_M
    lons, lats = tf.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    return min(lons), min(lats), max(lons), max(lats)


def _in_the_us(bbox) -> str | None:
    lon0, lat0, lon1, lat1 = bbox
    clon, clat = (lon0 + lon1) / 2, (lat0 + lat1) / 2
    for name, la0, la1, lo0, lo1 in US_BOXES:
        if la0 <= clat <= la1 and lo0 <= clon <= lo1:
            return name
    return None


def resolve(tx: int, ty: int) -> list[dict]:
    """Which 3DEP 1 m products actually cover this cell, best first.

    RESOLVED, NEVER CONSTRUCTED. The x/y cell is computed in the configured
    grid; the real tile is named in its OWN UTM zone and carries an
    acquisition-project suffix that is not derivable from coordinates.
    Building a filename works only inside the grid's zone and only by luck --
    measured on the development corpus, most missing cells outside the grid's
    zone resolved to a different x/y entirely.

    RANKED BY SIZE, DESCENDING, and this is deliberate. Several acquisitions
    can cover one cell; at fixed 10012x10012 dimensions the small ones are
    EDGE tiles that are mostly NoData and compress to a fraction of a full
    one. One measured cell offered an 11 MB and a 341 MB product, and the
    11 MB file would populate almost nothing while looking like a success.
    Every candidate is returned so the choice is visible, not just the winner.
    """
    bbox = cell_bbox(tx, ty)
    where = _in_the_us(bbox)
    if where is None:
        raise NotInTheUS(
            f"cell x{tx}y{ty + 1} is outside US territory; 3DEP holds US data "
            f"only, and no request was sent")
    query = urllib.parse.urlencode({
        "datasets": DATASET,
        "bbox": ",".join(f"{v:.6f}" for v in bbox),
        "prodFormats": "GeoTIFF",
        "max": 50,
    })
    with _open(f"{INDEX_URL}?{query}", INDEX_HOSTS,
               INDEX_TIMEOUT_S) as resp:
        payload = json.load(resp)

    clon, clat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    out = []
    for item in payload.get("items", []):
        bb = item.get("boundingBox") or {}
        if not bb:
            continue
        # The index returns neighbours whose bbox merely OVERLAPS ours. Only a
        # product containing the cell CENTRE is the tile for this cell.
        if not (bb["minX"] <= clon <= bb["maxX"] and bb["minY"] <= clat <= bb["maxY"]):
            continue
        url = item.get("downloadURL") or ""
        host = urllib.parse.urlparse(url).netloc
        if host not in ALLOWED_HOSTS:
            continue
        out.append({"title": item.get("title"),
                    "url": url,
                    "filename": Path(urllib.parse.urlparse(url).path).name,
                    "size_bytes": int(item.get("sizeInBytes") or 0),
                    "published": item.get("publicationDate"),
                    "region": where})
    if not out:
        raise NoProductCovers(
            f"3DEP publishes no 1 m GeoTIFF covering cell x{tx}y{ty + 1} "
            f"({payload.get('total', 0)} nearby products, none containing it). "
            f"That ground has no 1 m lidar; terrain stays refused there.")
    return sorted(out, key=lambda p: -p["size_bytes"])


def verify_tile(path: Path, cell: tuple[int, int] | None = None,
                expect_bytes: int = 0) -> dict:
    """Open it as a georeferenced GeoTIFF, or refuse to keep it.

    A TRUNCATED DOWNLOAD AND AN HTML ERROR PAGE BOTH ARRIVE AS A FILE. Size
    alone does not distinguish them from a tile, and a corrupt tile in
    data/dem/ would not fail at fetch time -- it would fail much later, inside
    an elevation lookup, on a run that used to work.

    When `cell` is given it also checks the tile's own georeferencing lands on
    the ground that was asked for, which catches the index returning a
    neighbour and catches our own cell arithmetic being wrong.

    TAG CHECKS ALONE ARE NOT ENOUGH, and a test caught this. A TIFF's header
    and tags sit at the FRONT of the file, so a 450 MB download that dies at
    300 MB still opens, still reports its CRS, and still lands on the right
    ground -- it is only the pixels that are missing. Two checks close that:
    the declared size from the index, and the last byte any strip claims,
    which must exist. Neither decodes the raster, so both stay cheap.
    """
    try:
        with tifffile.TiffFile(path) as tf:
            page = tf.pages[0]
            tags = {t.name: t.value for t in page.tags.values()}
            scale = tags["ModelPixelScaleTag"]
            tie = tags["ModelTiepointTag"]
            meta = tf.geotiff_metadata or {}
            epsg = meta.get("ProjectedCSTypeGeoKey")
            height, width = page.shape
            last_byte = max((int(o) + int(c) for o, c
                             in zip(page.dataoffsets, page.databytecounts)),
                            default=0)
    except Exception as exc:
        raise NotAGeoTIFF(f"{path.name} is not a readable TIFF: {exc}") from exc
    on_disk = path.stat().st_size
    if expect_bytes and on_disk != expect_bytes:
        raise NotAGeoTIFF(
            f"{path.name} is {on_disk:,} bytes; the 3DEP index declares "
            f"{expect_bytes:,}. A short file is a truncated download, not a "
            f"tile — the tags at the front of it read perfectly well.")
    if last_byte > on_disk:
        raise NotAGeoTIFF(
            f"{path.name} claims image data out to byte {last_byte:,} but the "
            f"file ends at {on_disk:,}. Truncated.")
    if epsg is None:
        raise NotAGeoTIFF(f"{path.name} carries no ProjectedCSTypeGeoKey, so "
                          f"nothing can say where its pixels are")
    ox, oy = float(tie[3]), float(tie[4])
    info = {"crs": f"EPSG:{int(epsg)}", "width": int(width), "height": int(height),
            "pixel_m": float(scale[0]),
            "size_bytes": on_disk,
            "easting_range": [ox, ox + width * float(scale[0])],
            "northing_range": [oy - height * float(scale[1]), oy]}
    if cell is not None:
        tf_back = transformer(info["crs"], WGS84)
        lon, lat = tf_back.transform(ox + width * float(scale[0]) / 2,
                                     oy - height * float(scale[1]) / 2)
        lon0, lat0, lon1, lat1 = cell_bbox(*cell)
        pad = 0.2
        if not (lon0 - pad <= lon <= lon1 + pad and lat0 - pad <= lat <= lat1 + pad):
            raise NotAGeoTIFF(
                f"{path.name} is a valid GeoTIFF but its centre is not on the "
                f"ground cell x{cell[0]}y{cell[1] + 1} describes")
    return info


def download(product: dict, dest_dir: Path = DEM_DIR,
             cell: tuple[int, int] | None = None,
             progress=print) -> dict:
    """Fetch one product, verify it, and only then put it in data/dem/.

    Downloads to a `.part` file and `os.replace`s it into place after
    verification -- the same rule as every other write of state in this
    project. A tile that half-arrives must not be visible to `TileSet`, which
    globs the directory and would load it on the next call.
    """
    url = product["url"]
    # Checked before ANYTHING is created, including the destination folder.
    _check_url(url, ALLOWED_HOSTS, "a download")
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / product["filename"]
    if final.exists():
        return {"status": "already_present", "path": final,
                **verify_tile(final, cell)}
    part = final.with_suffix(final.suffix + ".part")
    expected = product.get("size_bytes") or 0
    got = 0
    # COMPLETENESS IS MEASURED AGAINST WHAT THE SERVER IS SERVING. The index
    # size was the only yardstick, and it went stale: USGS re-wrote objects
    # (a newer Last-Modified) while the 3DEP index still declared the older
    # sizes, a few kilobytes apart. Two downloads stopped at exactly the
    # server's own Content-Length, and both were refused as "truncated".
    # A dropped connection still fails here: it ends short of Content-Length.
    # A disagreement with the index is REPORTED, not fatal.
    #
    # NO Content-Length, NO TILE (a deliberate ruling). It used to fall back
    # to the index size -- the yardstick just shown to go stale -- and, with
    # no index size either, skipped the size check altogether, so a
    # connection that dropped mid-file on a chunked response would PASS on
    # the strip-end check alone. Without the server's own size, completeness
    # cannot be established, so it is refused.
    served = None
    try:
        # _open refuses an off-list redirect or final URL BEFORE `part` is
        # opened, so a refusal leaves nothing behind (and the except below
        # would remove it if it did).
        with _open(url, ALLOWED_HOSTS, DOWNLOAD_TIMEOUT_S) as resp, \
                open(part, "wb") as fh:
            headers = getattr(resp, "headers", None)
            length = headers.get("Content-Length") if headers is not None else None
            served = int(length) if length is not None else None
            if served is None:
                raise NotAGeoTIFF(
                    f"{part.name}: the server sent no Content-Length, so "
                    f"there is nothing to measure completeness against. "
                    f"Refused rather than assumed complete; retry later.")
            total = served
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total and got % (32 * CHUNK) < CHUNK:
                    progress(f"    {got / 1e6:,.0f} / {total / 1e6:,.0f} MB")
        if got != served:
            raise NotAGeoTIFF(
                f"{part.name}: received {got:,} of the {served:,} bytes the "
                f"server declared. Truncated.")
        info = verify_tile(part, cell, expect_bytes=served)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, final)
    return {"status": "fetched", "path": final, **info,
            "served_bytes": served, "index_declared_bytes": expected,
            "index_size_agrees": (served == expected if expected else None)}
