# Trail Factors

Training analysis for trail and ultra running, built for one athlete's own
data. It ingests Garmin FIT files (and optional phone-app recordings of the
same runs), derives terrain from public USGS elevation tiles, matches repeated
climbs into a segment library, and produces a per-run report whose OUTPUT
SCHEMA is the contract for a coaching chat that reads it through an MCP server.
Single user, no UI.

The full design is in SPEC.md. **Read it before making an architectural
decision.** Section numbers (§N) below refer to it. This file is the short
version: the rules that were learned the hard way, the commands, and the
conventions every change must keep.

## Setup

- Athlete-specific values live in `config/athlete.yaml` (gitignored). Copy
  `config/athlete.example.yaml`, which has a one-line comment per field:
  `clusters`, `projection_crs`, `reference_run_garmin_id`,
  `unresolved_peak_run`, `hr_above_default_bpm`, `time_above_ladder_bpm`,
  `route_fingerprint_accepted`, `dem_tile_suffix_guess`, `goal_event`. The
  file is loaded with `yaml.safe_load` and validated STRICTLY: an unknown
  key, a wrong type or an out-of-range value is an error naming the field and
  line. Never a silent default for a typo.
- Paths default to repo-relative locations (`src/trail_factors/paths.py`, anchored at the
  repo root, never the cwd, so a server launched elsewhere cannot create a
  phantom empty database). Each is overridable by an `TRAIL_FACTORS_*`
  environment variable documented in `.env.example`; the database is
  `TRAIL_FACTORS_DB`. Nothing loads `.env` automatically.
- Garmin login: this code never writes the password anywhere. The OAuth token
  store lives OUTSIDE the repo (default `~/.garminconnect`); a token-store or
  auth-state path inside the repo is refused.
- The MCP server (`src/trail_factors/mcp_server.py`) is **stdio only**. It binds no host and
  no port; do not add a network transport.

## Design invariants

Each rule below is stated with the reason it exists. Most were found by a
number that looked plausible and was wrong. **A new device, a new trail or a
new field does not produce errors; it produces plausible numbers.**

### Ingest, identity and time

- **Raw files in `data/raw/` are immutable source of truth.** Everything else
  is derived and must be rebuildable from scratch. Deleting the database and
  running `ingest-all` is safe; athlete decisions are restored from
  `data/athlete/`.
- **Identity is anchored in UTC.** `run_id`, every timestamp and every
  time-range comparison are UTC. Local time is for display, day-of-week and
  time-of-day only; group runs by LOCAL date, never UTC date.
- **Timestamps are stored as naive UTC, never TIMESTAMPTZ.** FIT records UTC;
  the offset lives in `activities.local_tz`.
- **Split and lap durations come from `total_timer_time`, never
  `end - start` and never `total_elapsed_time`.** Record timestamps are 1 s
  resolution, so end−start truncates with one sign and the error accumulates.
  `splits.duration_s` and `laps.duration_s` are PROVENANCE ONLY (elapsed,
  including auto-pause); `laps.total_timer_time` is the lap duration and
  reproduces Garmin's own lap figures where elapsed does not.
- **`activities.timer_time_s` is Garmin's `duration`; `duration_s` is its
  elapsed duration.** `moving_time_s` is NOT Garmin's moving duration: it is
  defined as sum(rwd_run + rwd_walk), so it excludes standing and is circular.
  Use `timer_time_s` for anything claiming Garmin continuity.
- **A confirmed valid window is applied BEFORE validation.** Validation runs
  while rows are built, so a window applied later can never rescue a file
  whose tail is a car ride. Splits wholly outside the window are DROPPED;
  a straddling split is TRUNCATED, and its distance is RECOMPUTED from the
  records (written to the FIT's own `total_distance` key), never scaled.
  Session totals are recomputed and Garmin's originals kept in
  `*_whole_file`. A window that selects zero records RAISES.
- **The tiling identity is the sharpest ingest check:** the `rwd_*` splits
  partition the activity, so their timer sum must equal its duration.
- **`confirm-file` works by Garmin id or file SHA, not activity id**, because
  the files that most need a window are the ones that never got an activity
  row. Confirmations REVISE with history, `source` and `confirmed_at`.
- **Sync keeps every step's result.** A file rejected during a sync is
  reported in `problems` with its message; a sync with problems is not a
  clean `done`. Report only THIS sync's rejections; `rejected-files` lists the
  persistent ones.
- **The bad-data loop is a closed loop, and every step names the next.** A
  file whose tail is a car ride, whose GPS is broken or whose values are
  implausible is REJECTED, not silently dropped:
  1. `sync` (or `ingest-all`) prints the rejection with the failing check
     and the next step: what to trim, and the exact command.
  2. `uv run trail-factors rejected-files` lists every file with
     findings and no activity row: its checks, its span, a SUGGESTED window,
     and the `confirm-file` command that applies it.
  3. `uv run trail-factors confirm-file <garmin-id|sha> [--end
     'YYYY-MM-DD HH:MM:SS'] [--start '...'] [--note '...']` stores the
     window (UTC). With no bound it takes the detector's suggestion; an
     explicit bound always wins. A bound outside the file's own span, or a
     start not before the end, is refused.
  4. Re-run `sync` (it walks every raw file, so the confirmed file is retried
     with its window applied before validation), then `retile` for terrain.
     The re-sync ANNOUNCES the rescued run (now ingested at the confirmed
     window, with its trimmed figures) and reports it, even when nothing new
     was downloaded. `confirm-window` likewise only records a confirmation;
     the activity changes at the next ingest.
  The advice must be true of the file: a car stretch at an edge followed by
  long walking is still cut by a window, at a STATED cost in on-foot time; a
  file with a mid-run impossible-speed block is never called fixable by an
  edge window; a stretch the detector could not place says so rather than
  calling it mid-run. `confirm-file` with one bound keeps the OTHER bound
  from the current confirmation before falling back to the detector.
  `rejected-files` covers the whole raw tree, phone-app files included.
  Confirming is **CLI only**: no MCP tool writes a window, so the MCP `sync`
  job's `problems` name the CLI command. An MCP tool would be a thin wrapper
  over `confirm_file(..., source="mcp")`: a write tool outside the read-only
  connection, `retryable` per `RETRYABLE_MEANS`, a read-back through
  `rejected-files`, and a remedy-sweep entry. Window confirmations are
  per-user state in the athlete-state directory (gitignored with all of
  `data/`).
- **Dual recording: anything that consumes `activities` must decide what to
  do about it.** A run may be recorded twice (watch plus phone app). The two
  files are complementary (phone: position and accuracy; watch: HR, cadence)
  and both are kept, but a per-activity query counts one run twice unless it
  picks one per `run_id`. Use the `runs` table. The matcher once bypassed it
  and split one crossing into two segments, which its consistency check
  could not see: that check detects merges, never splits.
- **An imported activity is not a recorded one.** Run/walk segmentation
  happens on the watch, so a file imported into Garmin has no `rwd_*` splits.
  Report the absence with a reason; never invent a zero.
- **The two `sync`s are different pipelines.** The CLI `sync [days]`
  downloads (the whole account by default), ingests, fetches weather,
  validates and reports; it does NOT run dem/silver/match/recovery, so run
  those (or `retile`, which picks up any running activity without terrain)
  afterwards. The MCP `sync` tool runs the full pipeline over a 400-day
  download window: download, ingest, dem, silver, weather, match, recovery,
  validate. With no DEM tiles it SKIPS the terrain steps with a note rather
  than failing, and its rejection report is built from the ingest result
  whatever a later step does: a terrain error must never hide a rejection.
- **The MCP `sync` is a JOB, not a call.** It holds the write lock for
  minutes, so it returns a job id and `sync_status` polls. Job state is a
  file, written atomically, so a restarted server can read it.
- **`recovery` must run after `match`** in every pipeline that runs `match`
  (the MCP `sync` job, `retile`, and by hand), or `segment_recovery` goes
  stale while `segment_efforts` is current.

### Sampling, weighting and aggregation

- **Sampling is not uniform.** Detect the regime per file from the observed
  median gap (Smart Recording ~4 s median, up to 20 s; Every Second is 1 Hz)
  and store it as `sampling_regime`. Never infer it from the date. Never use
  row position as a proxy for time.
- **Every mean over records is time- or distance-weighted.** Smart Recording
  samples densely where values change, so a plain `avg()` over-weights
  variable sections.
- **Weights are computed BEFORE filters** (`basic.record_weights`). Lagging an
  already-filtered series makes a sample's weight a property of the filter.
  `weighted_mean()` takes a weight MAP, so the bug is unrepresentable.
  `grade_binned_pace` (§6.2) still lags inside its filter: a known instance,
  left because the golden query pins its output.
- **Derived RATES exclude stopped time; bronze never drops it.** Stops are
  structure and stay in `records`, but duration, vertical speed and
  time-weighted HR subtract them. A DOSE is different: time at a heart rate
  and Foster load use timer time, because the heart does not stop at a road
  crossing.
- **`sum()` skips NULLs**, so a partly-null class silently sums a subset with
  one sign. Declare it (`partial_sums`, a stated lower bound) and name a fully
  missing field in `unavailable_fields`. Never coerce a missing value to 0.
- **Windowed bests are interpolated sliding windows, never an instantaneous
  max**, and are not comparable across sampling regimes. Quality flags cannot
  be applied to them (nearly every long window contains a flagged record), so
  the flagged fraction is reported instead: a MAXIMUM selects for positive
  measurement error in a way a mean does not.
- **Jitter is normalised by point spacing.** Raw jitter is a regime artifact;
  normalised, 1 Hz and Smart Recording agree.

### Devices and file format

- **Cadence is `(cadence_raw_rpm + fractional_cadence) × 2`** (§9), settled by
  the PARITY of Garmin's max cadence on one device and by the average on
  another. Garmin's average includes zero-cadence records, so the
  running-only figure is a different quantity and is never reconciled to it.
- **Some devices write an `interval_active` split spanning the whole run** on
  top of rwd_run/walk/stand, so an unfiltered time-range join to `splits`
  matches every record twice. Joins restrict to `split_type LIKE 'rwd\_%'`
  and the silver build RAISES if rows exceed distinct timestamps.
- **Altitude provenance is MEASURED, not looked up** (§9.2): identical to the
  GNSS channel (≥90%) → `gps`; diverging and smoother → `barometric`;
  rougher → `unknown`. The device map is a cross-check only; the watch's
  reported product name did not match the map. `altitude_source_basis` keeps
  "could not be measured" apart from "measured and disagrees".
- **`altitude_gnss_m` joins by FILE ORDER**: `gps_metadata` carries no
  timestamp, so each record takes the last frame written before it (measured,
  not assumed). A GNSS loss leaves NULL, never a stale value; after a
  recording gap over `GNSS_UNKNOWN_AGE_AFTER_GAP_S` (60 s) the reading's age
  is unknowable and provenance treats it as missing.
- **The session summary and the record stream are both the watch.** On Smart
  Recording the record stream is a decimated view, so its max HR can only be
  lower than the summary's (by a beat or two), never higher. The summary is
  the better ceiling; only the record stream can be placed in time or checked
  for cadence lock.
- **Running dynamics are per field, with Garmin's units** (mm, %, ms, mm) and
  the unit carried in every result. The device drops fields independently, so
  `n` is per field. `vertical_ratio` is the device's own field and is never
  recomputed from the other two. Accessory-only fields are NULL without a pod.
- **Garmin running power is a model output**, not a measurement: within-device
  comparison only, never a dependent variable in §2.
- **Garmin's summary has a declared status per field (§4.2)**: some fields
  are oracles; min/max elevation and total ascent are DISCREDITED
  (barometer-free devices inflate elevation amplitude roughly 2x).
- **Foster load = `garmin_rpe` × timer minutes** (`src/trail_factors/analysis/session_load.py`),
  carried in the report and served by the `session_load` MCP tool. It is a
  session DOSE, so it uses timer time, not moving time. An activity with no
  Garmin self-evaluation is `unavailable` with a reason saying so, never a
  zero. `garmin_feel` is shown beside it and never used: the direction of
  its encoding is unverified. The scale string is stored with every value.
  **Over a selection, `session_load` spans EVERY sport** (a dose: a ride's
  load is in Thursday's legs), and counts *unrated* (no rating) apart from
  *unpriced* (rated, but not priceable, with a reason). The sum is labelled a
  lower bound only when one of those is non-zero.
- **Indoor rides write `distance_m` as a constant 0.0, not NULL**: a fake
  zero that should read as absent. **This is an OPEN known defect:**
  `session_series` still reports `mps: 0.0` on every bin of such a ride, so
  do not trust a speed or pace from an indoor ride.

### Terrain, DEM and segments

- **DEM is multi-tile (`TileSet`).** Dropping a `.tif` into the DEM dir is the
  install step. Each tile reads its OWN CRS from `ProjectedCSTypeGeoKey`;
  applying one zone to every tile would place pixels hundreds of km away.
- **The projection is DERIVED per run unless configured.** With
  `projection_crs` unset, each run's WGS84 UTM zone (EPSG:326NN north,
  327NN south) comes from that run's own positions, and the output says so
  (`projection: {crs, basis: "derived from this run's position: UTM zone
  NN"}`). An explicit `projection_crs` always wins, and with it set the
  behaviour and segment ids are unchanged. Projected coordinates are only
  comparable within one zone: two zones can give identical numbers for
  places hundreds of km apart.
- **`fetch-tile` resolves a tile against the 3DEP index, never constructs a
  filename.** The project suffix is an acquisition, not a function of
  position, and the x/y cell is only meaningful in its own CRS. USGS names a
  tile by its NORTH-west corner (a floor division gives the south-west).
  Downloads are verified (served `Content-Length`, last strip present, tags),
  written as `.part` and `os.replace`d. Where several acquisitions cover a
  cell, the largest wins. The host is an allowlist and a redirect off it is
  REFUSED. `--missing` refuses a bulk pull over 1.5 GB without `--yes`.
- **A missing tile is reported as a 10 km GRID CELL, never a coordinate**,
  with the fetch command, as the top NOTABLE item. A cell is what fetching
  needs and is far coarser than a position.
- **With `projection_crs` unset, a cell is labelled in the run's DERIVED
  zone**: `filename` is null (no guess), and `fetch_command` carries
  `--zone NN`, because a cell name means nothing without its zone;
  `fetch-tile <cell>` without `--zone` is refused in that configuration. A
  southern-hemisphere zone or a northern zone above 23 has no USGS grid, so
  such a cell gets NO fetch command (dropping the hemisphere would name a
  cell in the northern grid, which can be real US ground somewhere else).
- **The lookup is local.** `dem.py` opens no socket and nothing on the lookup
  path imports `dem_fetch.py`; tests assert both. §5's rule forbids a
  per-coordinate query stream, not acquiring the map once.
- **Running-only HOLD.** `dem_populate`, `retile` and silver skip non-running
  sports, so a bike ride is never walked into the segment library by
  accident. It is a placeholder for a real policy. Never store a read-only
  terrain profile into `dem_elevations`: silver takes its targets from it.
- **`retile` rebuilds only newly covered activities, except the matcher,
  which is corpus-wide by necessity**: the segment library is global.
- **The segment unit is a SLOPE** (§5.4): a monotonic stretch traversable
  either way. Ascent and descent of one face are two EFFORTS on ONE segment.
  Segments are PLACES and direction-free; efforts are directed.
- **Segment identity is `(place, side)`**: the peak a slope hangs from plus
  its flank. The col is derived and NEVER keyed on: peaks reproduce far more
  tightly than cols. **A derived projection's zone is part of the place**:
  places in different zones never match, whatever their coordinates.
- **A new trail cannot merge into an old one**: `compatible` rejects on
  position first at `MATCH_RADIUS_M` (30 m). Read §12.3's adversarial review
  before changing matcher code.
- **Segments are ordered by a reference run** (`reference_run_garmin_id` in
  config): path distance and crossing order make hills nameable ("the one at
  1 km"). Without it, segments list by effort count.
- **No hysteresis exit in `detect_slopes` (§14l)**; `SLOPE_EXIT_PCT` appears
  only in the merge veto. **The merge veto tests the CUMULATIVE grade**, which
  allows plateau absorption (§14k). The obvious fix moves the artifact onto
  descents and is not shipped; `basic.effort_profile` carries the composition.
  `tests/test_segment_pooling.py` keeps the pooling pattern detectable (with
  a positive twin) and checks every effort carries its grade composition.
- **A threshold fitted on one trail is a property of that trail.** A trail
  with no flats between features cannot validate the slope boundary rule; the
  synthetic profiles in `tests/test_slopes.py` are the test and real runs are
  a regression. Do not fit `BENCH_MAX_M`, `PROMINENCE_MIN_M`, §6.5's `L` or
  the similarity weights on a trail where they carry no information.

### Venues and comparison sets

- **Comparison sets are CLUSTER-based, never time windows**:
  cluster → venue (→ run_type, inert in this build: run_type is not
  collected). A time window mixes trails and turns a shorter route into an
  apparent fitness decline. Clusters come from config; a run outside every
  cluster is `OTHER`, never a coordinate.
- **A venue is a few athlete-named ANCHOR segments** (§19). A run belongs if
  it crosses one; a link run needs 60% of the touched anchor distance;
  `mixed`/`unassigned`/`none` fall back to the cluster, labelled.
- **Anchors and segment names are RUN-ANCHORED
  `(run_id, direction, at_distance_m)` and coordinate-free.**
- **`venue-candidates` PROPOSES and writes nothing**; naming a venue is the
  athlete's act.

### Heart rate, zones and ceilings

- **HR zones are dated configuration, not a constant (§14e).** Every FIT
  carries its own boundaries in `time_in_zone`; time in zone is computed from
  EACH FILE'S boundaries (`activity_hr_zones`), with the current config as a
  stated fallback. Never pool Garmin's stored zone times across a boundary
  change, and report a week spanning two boundary sets SPLIT.
- **`hr_zone_high_boundary` is the array of zone FLOORS despite its name.**
  Reading it as highs shifts every zone by 1 bpm, and HR can sit in a narrow
  habitual band straddling a zone boundary, so time-in-zone measures where a
  line falls and one bpm moves real minutes.
- **Max HR two ways (§6.0):** `max_hr_recorded` (single sample, a spike
  candidate) and `max_hr_30s` (30 s time-weighted rolling mean, flagged
  records excluded). Raw HR, never lag-shifted: the lag aligns HR to effort
  for matching; a ceiling wants the peak as recorded.
- **`hr_above` returns EXCURSIONS, not records**, so a sustained bout and a
  spike do not look alike. Each carries pace, grade and cadence. An excursion
  breaks on a below-threshold record or a gap over 3x that run's own median.
- **Quality flags are reported, never applied, in the HR-ceiling tools**: at
  the top of the range a flag cannot tell artifact from real spike. A run
  named in `unresolved_peak_run` is MARKED, never excluded, with totals
  repeated without it.
- **A corpus max carries its circumstances** (date, dew point; `run_type`
  null, labelled "not collected"). Absent context is not false context.
- **A ceiling comparison is gated on the selection ITSELF.** The
  declared-vs-demonstrated comparison in `max_hr_over` is reported only if a
  run in the selection reached within the band (5 bpm) of the demonstrated
  30 s ceiling, with the evidence bounded to the selection's end date.
  Otherwise it is REFUSED, naming the selection's best 30 s value and the
  band. Over easy runs the gap measures the selection, not the heart. No
  run_type groups enter the rule. **When it passes, the gap is measured
  against the demonstrated ceiling, not the selection's own peak**, which is
  shown separately and labelled as the selection's: a selection inside the
  band can still sit below the run that set the ceiling.
- **Re-anchoring zones is an athlete decision, never a tool's.** Report the
  declared-vs-demonstrated gap; never correct it. Tests pin its SIGN.
- **Account summaries (`data/lookup/`) are a LOOKUP, never ingested** (§10.1).
  A summary's maxHR is a single unverifiable sample, and the projection stores
  no coordinates by construction. Rank download candidates by PACE, never by
  the untrusted maxHR, which would select for the artifact.

### Drift, recovery and run/walk metrics

- **Two drift measures; neither is "the" drift (§6.7).**
  `matched_terrain_drift` pairs RECORDS (`rwd_run` only, speed matched
  ±0.35 m/s, ≤20 m apart, ≥600 s separation). `segment_effort_drift` pairs
  whole efforts, same direction by construction. Never call the latter
  "§6.7 by direction": a §6.7 pair joins two records that often travel in
  OPPOSITE directions, because out-and-back is its precondition.
- **Direction asymmetry in drift may be a ceiling effect**, not a cost
  difference: efforts that start with different headroom can converge on the
  same end value. Read the start values before reading the slope.
- **`hill_pace` (§6.0g): read `comparable_ground.length_ratio` first.** Ascents
  and descents need not cover the same ground, so part of any direction
  difference is terrain.
- **§6.3 delta-HR ships; tau is gated by `MAX_TAU_CV`** (0.5).
  `tau_reportable` is a CORPUS verdict stamped on every row, reported with
  `gate_basis`. Tests pin the gate's RULE and invariants, never the count.
- **§6.9 drift onset is a view of §6.7 and is refused on short runs**: the
  600 s separation leaves no room for three 5-minute bins. The independent
  unit is a PASSAGE, not a pair.
- **M3 `run_walk_cycles`**: a cycle is one rwd_run block plus the following
  walk; a run block with no walk has `walk_duration_s = None`, never 0.
  `purchase` and `decay` are PAIRED SERIES, never a fitted slope over four
  points. If §6.0's 120 s gate makes the series too thin, fit a per-block gate
  rather than lowering it.
- **Per-walk HR recovery is withheld below `MIN_WALK_FOR_RECOVERY_S =
  HR_LAG_S + 5 s`**: HR lags effort ~40 s, so a short walk's closing reading
  reflects the effort before it. Derived from `HR_LAG_S`, so a better lag
  estimate re-gates automatically.
- **M3 pools across runs only when something other than willingness decided
  the walks.** On a by-feel run the purchase arithmetic measures
  willingness, not what the walk bought. No walk-protocol source exists in
  this build, so every run reports it as "not collected" and is NOT
  poolable; per-run cycles still report. An unknown protocol is never
  assumed to be a structured one.
- **M7 `zone_by_gait`: standing is a GAIT here, not paused time.** It reads
  lower than Garmin's zone time by design, and a test asserts that direction.
- **M1 (mode) is a gait, referred to only as "the mode"**: no display name
  is stored or hardcoded. Detect it in dynamics at matched pace and grade,
  not by pace, which would be circular. M6's pace floor is an athlete
  parameter.
- **`session_series` state is SECONDS (`run_s`/`walk_s`/`stand_s`), never a
  majority label**: many bins contain both gaits. Position is never emitted;
  the elevation-by-distance profile is gated on `route_fingerprint_accepted`.
- **Stationary-bike units are not watts**: the console is a rev counter, so
  watts/power/kJ are forbidden in output (a test enforces it). An
  unattainable RPM total is REFUSED, never clamped. The effective n of a
  resistance trend is BLOCKS, never HR records. SPEC.md §18 holds the design
  (§18.1 matched-resistance HR, §18.2 time in resistance, RPM reconciliation
  and the load index).

### Statistics

- **A covariate that varies only between runs has n = RUNS, not records.**
  Weather, date, fitness, any per-run context: a per-record SE understates
  uncertainty by roughly sqrt(records per run). Cluster by run or jackknife
  over runs, and report both. Assume any new per-record regression on a
  run-level term has this bug until it shows clustered errors.
- **Weather is confounded with fitness** when the hot months are the less-fit
  months. A flat-ground cluster identifies the weather term (terrain drops
  out); a cluster where dew point tracks the date (correlation above 0.7) is
  refused before its coefficient matters.
- **Do not fit the residual model (milestone 7) early**: a model on four runs
  is a confident number with nothing behind it.

### Report, MCP contract and statuses

- **The §16.2 report's OUTPUT SCHEMA is the contract.** Four statuses, never a
  bare null: `ok`, `gated` (a stated gate rejected it), `refused` (invalid at
  this sampling regime: not a finding), `unavailable` (input missing).
- **`ok` requires a value**, enforced at assembly by
  `report.enforce_status_contract()` (demotes to `unavailable` with
  `defect: true`). The real fix is at the source: a primitive that produced
  no value downgrades itself, because only it knows why.
- **A section that raises degrades ALONE** (`report._section`) to
  `unavailable` with `error` and a `defect` note: the fault is ours, not
  absent data. **`render_safe()`**: formatting must not cost a payload.
- **The report opens with NOTABLE**, ordered by salience. A refusal is never
  notable.
- **Every primitive states its APPLIED FILTERS as fields**, never prose
  (`split_type`, `walking_paired`, `speed_matched_within_mps`, `grain`, ...).
  `grain` names the INPUT grain.
- **A reason must not assert a cause it did not check.** Derive absence
  reasons by looking (`basic.terrain_state()`, `basic.no_splits_reason()`);
  every count in a reason is computed at call time or deleted.
- **Every tool result carries a corpus fingerprint AND a calc_version.** The
  digest is derived from live constants in `calc_params()`, so changing a
  threshold moves it whether or not anyone bumps a number. Register every new
  threshold there. The fingerprint is never a bare null.
- **The MCP server is a THIN WRAPPER**: every tool calls the function the CLI
  calls; no SQL, thresholds or interpretation in the server. Parameterised
  primitives, never a query tool: selection is free (`run_type, start_date,
  end_date, cluster, last_n, segment_id, direction`); computation is fixed.
- **`run_type` is NOT COLLECTED in this build.** Every selection tool keeps
  the parameter, but a non-empty `run_type` filter is REJECTED BY NAME
  (`select.FilterUnavailable`, "no run_type source in this build"; outcome
  `rejected`, retryable, remedy: omit run_type). Every per-run row reports
  `run_type: null` labelled "not collected". `src/trail_factors/ingest/answers.py` is the
  one reader (`load_answers()` returns `{}`). A later version re-enables
  run_type through user-defined run types; do not invent a source before then.
- **The boundary is athlete input vs computed values.** The chat may submit
  athlete input through validation; it may never modify anything computed.
  The only MCP writer of athlete input is `save_ride_blocks`; window
  confirmations are written by the CLI (`confirm-file`, `confirm-window`).
  Nothing in the MCP server or the CLI asks the athlete for per-run answers.
  Nothing reachable through MCP modifies `data/raw/`: connections are
  read-only and a test fingerprints the raw tree. No tool returns record-grain
  data or a coordinate; the finest grain is the effort.
- **The MCP boundary constrains the server, not a client with a shell.** A
  client that can copy the database reads a snapshot no fingerprint covers.
  Numbers that lag rather than being absent are the signature of a copy. Do
  not add repo paths or `data/raw/` paths to tool output.
- **`retryable` means "is there a next call that can succeed?"**
  (`RETRYABLE_MEANS`): true when the caller can act and nothing was written;
  false when nothing helps or the write already landed.
- **A remedy is an instruction to a machine that cannot inspect the
  interface.** `tests/test_remedies.py` checks every remedy string: named
  flags exist, named functions and tools exist, "call again" remedies are
  retryable, and every `supersede` instruction says WHERE it goes.
- **A locked database fails in seconds, named** (`CONNECT_TIMEOUT_S = 5`),
  retryable, with a remedy saying do not re-ingest.

### Athlete-supplied state

- **Athlete input sits outside the rebuild path**: files in `data/athlete/`,
  keyed on `file_sha256` or `run_id`, never written by ingest. A rebuild that
  silently discards it is correct-looking until something notices.
- **Athlete-state files are read directly.** A projection table rebuilt only
  on write connections returned empty to every read-only reader while saves
  reported success. Any remaining projection (`annotations`,
  `window_confirmations`) is checked by `queries.projection_status()`, which
  reports `stale_projection` rather than an empty result.
- **A save asserts its own visibility** (`visible_to_read`); a failed
  read-back says DO NOT re-send, because the write landed.
- **`supersede` is an ARGUMENT beside the payload, never a field inside it**;
  inside, it is REJECTED (an unknown field to `save_ride_blocks`), never
  stripped, and the remedy says where it goes.
- **Null is not a value in a validated payload.** A range check that begins
  `if v is None: continue` lets null pass as validated; reject it by name.
- **A field only one of two paths populates is worse than one neither
  does**: the null reads as a fact about the session.
- **`save_ride_blocks`**: `raw_text` is required verbatim (the blocks are an
  interpretation of it); `rpm` is the range stated, never a midpoint;
  ambiguity is refused, not resolved.
- **`annotations` is free text only.** Structured per-run context (run_type
  and the like) belongs to the future user-defined run types, never to notes.

### Testing

- **Corpus tests read `$TRAIL_FACTORS_DB` and nothing else.** Unset, they SKIP
  with a reason; set to a missing path, they FAIL (a typo must not read as a
  pass). They find their subject by a property, never one athlete's run ids.
- **No test may create a database file.** `tests/__init__.py` snapshots
  database files at start and exits non-zero if one appears. Scratch
  databases are `:memory:` or a TemporaryDirectory.
- **Never write a test fixture into `data/athlete/`.** It is indistinguishable
  from real input and cannot be regenerated. Redirect the module's path
  constant at a TemporaryDirectory.
- **Round-trip tests for athlete state run the read in a separate process**;
  two handles in one process share cached catalog state.
- **Golden pinning** (`tests/golden/capture.py`): golden outputs are private,
  in `data/golden/` (`TRAIL_FACTORS_GOLDEN_DIR`), scoped to a fixed activity
  set. Three steps: `--pin` once to write `pinned_activities.txt` from the
  corpus as it is, a bare run to capture the outputs, then `--check` after
  every refactor (non-zero on a diff). A fresh checkout has none of these. Data arrival is not a reorganisation: a diff means the CODE changed an
  answer. Never add to the pinned set to make a failure go away; growing it is
  a deliberate commit that re-captures in the same change. A missing pinned
  activity is a hard failure.
- **A test that asserts only absence is vacuous without a presence twin.**
- **A pinning test is only as strong as the decision behind it.** When a
  review contradicts one, re-examine the decision; if it was never argued,
  delete the test and pin the corrected behaviour with a positive twin.

## Known limitations

- **Date filters compare the UTC date of a run's start, while `list_runs`
  shows its LOCAL date**, so an evening run near local midnight can fall on
  the next day in a `start_date`/`end_date` filter (and in a load sum over a
  local week). See SPEC.md §12.6.
- **With no `projection_crs`, two runs over the same summit whose median
  positions fall either side of a UTM zone line are matched in different
  frames and form TWO segments**, and nothing in the segment tools says why.
  Setting `projection_crs` removes it (SPEC.md §5a.1-quinquies).

## Commands

    uv run trail-factors sync [days]   # the normal workflow: fetch, ingest,
                                           # weather, validate, report each new
                                           # run (no dem/silver/match/recovery:
                                           # run `retile` or those steps after)
    uv run trail-factors ingest <file.fit> | ingest-all
    uv run trail-factors verify        # per-activity ingest summary
    uv run trail-factors validate [--refresh]   # full validation report
    uv run trail-factors coverage-report   # GPS coverage per file
    uv run trail-factors confirm-window <garmin-id>   # confirm the suggested
                                           # window; applied at the next sync
    uv run trail-factors rejected-files   # files with findings, no activity row,
                                           # each with a suggested window
    uv run trail-factors confirm-file <garmin-id|sha> [--start ...] [--end 'YYYY-MM-DD HH:MM:SS'] [--note '...']
                                           # then re-run sync to ingest it trimmed
    uv run trail-factors weather [activity] [--refetch]
    uv run trail-factors dem [activity] | silver [activity] | match | recovery
    uv run trail-factors fetch-tile <cell> [--zone NN]   # §5a.1-bis: resolve,
    uv run trail-factors fetch-tile --missing [--yes]   # verify, then retile
                                           # --zone is REQUIRED when projection_crs
                                           # is unset: copy the report's fetch_command
    uv run trail-factors retile        # rebuild only newly covered activities
    uv run trail-factors name-segment <segment-id> <name>
    uv run trail-factors venue-anchor '<venue>' <segment-id> | venues   # §19
    uv run trail-factors venue-candidates <CLUSTER>   # proposes; writes nothing
    uv run trail-factors report [activity]   # §16.2: the post-run report
    uv run trail-factors note <run-id> <text>   # free-text run note
    uv run trail-factors garmin-login  # one-time, interactive
    uv run trail-factors garmin-download <garmin-id> | garmin-download-all [days]
    uv run trail-factors garmin-list history   # sweep account SUMMARIES (no
                                           # downloads) into the lookup dir
    uv run trail-factors hr-ceiling    # reads those summaries; no network
    uv run trail-factors-mcp        # MCP server, stdio

First-time setup:

    cp config/athlete.example.yaml config/athlete.yaml   # then edit it
    cp .env.example .env                                 # optional; export what you need
    uv run trail-factors garmin-login

Tests:

    uv run python -m unittest discover tests                       # corpus tests skip
    TRAIL_FACTORS_DB=data/test-copy.duckdb uv run python -m unittest discover tests
    TRAIL_FACTORS_DB=data/test-copy.duckdb uv run python tests/golden/capture.py --pin   # once
    TRAIL_FACTORS_DB=data/test-copy.duckdb uv run python tests/golden/capture.py         # capture
    TRAIL_FACTORS_DB=data/test-copy.duckdb uv run python tests/golden/capture.py --check # after a refactor

Point `TRAIL_FACTORS_DB` at a COPY of your database for testing; tests open it
read-only and never create it.

- `sync` is **prompted, never scheduled**. Skip-if-present makes it safe to
  run after every run or once a week; the filesystem is the record.
- **Garmin auth: never retry in a loop.** A failed login is recorded and a
  retry inside 30 minutes is refused locally, because repeated attempts
  extend Garmin's rate limit. Delete the auth-state file
  (`TRAIL_FACTORS_GARMIN_AUTH_STATE`, default
  `~/.garminconnect_auth_state.json`) to override.

## Conventions

- Python 3.12, uv for dependencies, DuckDB, Polars.
- **Units: store SI internally**, display feet/miles/°F. Internal thresholds
  stay in m/s.
- **Pace is displayed `M:SS/mi` and is NEVER stored.** Format only at display
  through `src/trail_factors/units.py`, the one formatter and the one mile constant. Decimal
  minutes are wrong in a way nothing downstream can detect: 13.45 min/mi reads
  as 13:45 and is 13:27.
- **Never write `value or default`.** 0, 0.0 and False are legitimate values
  (speed 0, cadence 0, coverage 0) and the falsy shortcut converts them to the
  default. Use `value if value is not None else default`.
- **Atomic writes everywhere state is a file**: temp file + `os.replace`, via
  `atomic_write()` and `revise()` in `src/trail_factors/ingest/athlete_state.py`. A plain
  `write_text` read mid-write leaves unparseable JSON. Assume any new
  `*.write_text` on state is this bug.
- **Timestamps are naive UTC; means over records are weighted; derived rates
  exclude stopped time; anything over `activities` picks one file per
  `run_id`** (see Design invariants).
- **Any numeric claim in SPEC.md has a reproducible query or computation
  inline**, or is labelled a one-off measurement with its method. Recorded
  figures drift; re-derive before quoting.
- **A threshold fitted on one trail is a property of that trail** until shown
  otherwise. Before encoding a rule, ask what it would do on terrain not yet
  run.
- **Maintenance scripts assert every anchor** (`assert t.count(old) == 1`) and
  make a single write at the END, so a failed anchor aborts the whole edit.
- **`src/trail_factors/cli/` is a package**, one module per command group, re-exported from
  `__init__`. Patch the module that OWNS a name, not the re-export.
- **`data/` is entirely gitignored.** `data/raw/` holds GPS tracks; derived
  data is rebuildable; `data/athlete/` is personal and NOT rebuildable from
  anything, so **back it up yourself**. Deleting the database is safe;
  deleting `data/athlete/` is not.
- **`config/athlete.yaml` and `.env` are private** and gitignored. Only the
  example files are committed.
- **Never commit anything from `data/`, a real `.fit` file, or a DEM tile.**
  The set of tiles on disk identifies where someone runs. Only synthetic
  fixtures under `tests/fixtures/` or `demo/` may be committed.
- **Weather coordinates are rounded to 2 dp (~1.1 km)** before any request
  leaves the machine. Raw responses cache under `data/raw/weather/`, so a
  rebuild needs no network. `weather_source` is `forecast | era5 |
  assumed_indoor`; ERA5 replaces forecast when re-run.

## Working style

- Work in small steps, one milestone or one fix at a time (SPEC.md §12).
- Read SPEC.md before making an architectural decision, and the relevant
  section before changing a metric.
- Ask before adding a dependency or changing project structure.
- Explain what a change does and why, and what it would do on data not yet
  seen.
- Next work comes from real use: a number that reads wrong, a gate that fires
  too often, something the chat cannot see. If no tool answers a question,
  the fix is a tool, not a workaround.
- When an artifact cannot be found, ask whether the search space was right. A
  clean negative is only as good as where it looked.
- Never commit anything from `data/`.
