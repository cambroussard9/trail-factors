"""DuckDB schema for the bronze layer (SPEC.md §4).

All TIMESTAMP columns hold UTC. They are stored naive rather than as
TIMESTAMPTZ so that reading them back needs no timezone library; the local
offset is kept separately in activities.local_tz / ingest_ledger.local_tz_offset,
derived per §9 from activity.timestamp vs activity.local_timestamp.

Everything here is rebuildable from data/raw/. Dropping ultra.duckdb and
re-running ingest must reproduce it exactly.
"""

PIPELINE_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS ingest_ledger (
    file_sha256        VARCHAR PRIMARY KEY,   -- identity is CONTENT, not filename
    original_filename  VARCHAR NOT NULL,
    current_path       VARCHAR NOT NULL,
    source             VARCHAR NOT NULL,      -- garmin_watch | strava_phone
    device_serial      VARCHAR,               -- null on Strava files (§9)
    device_model       VARCHAR,
    activity_start_utc TIMESTAMP  ,           -- session.start_time, NOT time_created
    local_tz_offset    DOUBLE,
    file_bytes         BIGINT NOT NULL,
    ingested_at        TIMESTAMP   NOT NULL,
    parse_status       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS records_enriched (
    -- SILVER (§3, §12.2). Derived and rebuildable from bronze plus the DEM.
    activity_id     VARCHAR NOT NULL,
    timestamp       TIMESTAMP NOT NULL,
    elapsed_s       DOUBLE,
    path_distance_m DOUBLE,              -- along-path, in the DEM's projection
    altitude_dem_m  DOUBLE,              -- NOT the watch's altitude (§5a)
    altitude_smooth_m DOUBLE,            -- 25 m distance-window median
    grade_pct       DOUBLE,              -- NULL outside a usable block (§12.2)
    grade_window_m  DOUBLE,              -- recorded so a change is detectable
    gap_mps         DOUBLE,              -- NULL wherever grade is NULL
    terrain_usable  BOOLEAN NOT NULL,    -- §14c per-portion, not per-activity
    -- §15.4's ARTIFACT carrier: comma-separated flag names, '' when clean.
    -- Flagged measurements are EXCLUDED, not down-weighted -- a confident
    -- recommendation built on an artifact is worse than no recommendation.
    -- Named flags rather than a score, so the exclusion decision cannot be
    -- softened into "a bit unreliable, use it anyway".
    -- Distinct from sampling_regime, which is a RESOLUTION LIMIT and gates
    -- which analyses are valid rather than excluding anything.
    quality_flags   VARCHAR,
    calc_version    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hill_traversals (
    -- One row per hill CROSSED in one run. Grouping traversals into canonical
    -- hills is the milestone-5 matcher; this is detection only (§5.0, §12.2).
    activity_id      VARCHAR NOT NULL,
    hill_index       INTEGER NOT NULL,
    peak_distance_m  DOUBLE NOT NULL,
    peak_elevation_m DOUBLE NOT NULL,
    prominence_m     DOUBLE NOT NULL,
    length_m         DOUBLE NOT NULL,
    start_distance_m DOUBLE,               -- the hill's EXTENT along the path.
    end_distance_m   DOUBLE,               -- Not symmetric about the peak, so
                                           -- it cannot be reconstructed from
                                           -- peak_distance_m +/- length_m/2.
    peak_lat         DOUBLE,
    peak_lon         DOUBLE,
    calc_version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS slope_traversals (
    -- MILESTONE 6. One monotonic stretch of terrain crossed in one run.
    --
    -- A prominence hill is up-then-down BY DEFINITION. On a trail of tight
    -- rollers with almost no flat between features that is the real terrain,
    -- which is why prominence finds clean hills there -- but that is a
    -- property of THAT TRAIL, not of hills. A slope is the general unit: an ascent and
    -- a
    -- descent are separate features that happen, on rollers, to be adjacent.
    --
    -- ENDPOINTS ARE DIRECTION-NORMALISED (low/high, not start/end) so one
    -- slope carries both an ascent effort and a descent effort. That is what
    -- makes the §6.3 tau comparison like-for-like: recovery after climbing
    -- slope X against recovery after descending the SAME slope X.
    activity_id      VARCHAR NOT NULL,
    slope_index      INTEGER NOT NULL,
    direction        VARCHAR NOT NULL,   -- ascent | descent, of THIS traversal
    start_distance_m DOUBLE,
    end_distance_m   DOUBLE,
    length_m         DOUBLE,
    vertical_m       DOUBLE,
    mean_grade_pct   DOUBLE,
    low_elevation_m  DOUBLE,
    high_elevation_m DOUBLE,
    low_lat          DOUBLE,
    low_lon          DOUBLE,
    high_lat         DOUBLE,
    high_lon         DOUBLE,
    calc_version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    -- MILESTONE 5/6 (§12.3, §12.4). A segment is a SLOPE -- a place, and
    -- DIRECTION-FREE (§5, amended). Direction lives on segment_efforts,
    -- because it is a property of the EFFORT, not of the terrain.
    --
    -- Derived and rebuildable from slope_traversals. Athlete-supplied NAMES
    -- are NOT here -- they live in data/athlete/segment_names.json, held by a
    -- RUN-ANCHORED, coordinate-free anchor outside the rebuild path (§19.3).
    segment_id            VARCHAR PRIMARY KEY,
    low_lat               DOUBLE,
    low_lon               DOUBLE,
    high_lat              DOUBLE,
    high_lon              DOUBLE,
    low_elevation_m       DOUBLE,   -- median over efforts, not mean: robust
    high_elevation_m      DOUBLE,   -- to one mis-detected traversal
    vertical_m            DOUBLE,
    length_m              DOUBLE,
    mean_grade_pct        DOUBLE,
    n_efforts             INTEGER,
    n_ascents             INTEGER,
    n_descents            INTEGER,
    -- THE CONSISTENCY METRIC (§12.3). A segment whose endpoint elevations
    -- disagree by more than ~1 m has merged two slopes. Stored so the check is
    -- queryable rather than recomputed, per §4.2.
    low_elevation_sd_m    DOUBLE,
    high_elevation_sd_m   DOUBLE,
    endpoint_spread_m     DOUBLE,   -- max endpoint distance from the centroid
    first_seen_utc        TIMESTAMP,
    calc_version          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS segment_efforts (
    -- One TRAVERSAL of one slope. Directed (§5, amended).
    segment_id       VARCHAR NOT NULL,
    activity_id      VARCHAR NOT NULL,
    slope_index      INTEGER NOT NULL,
    start_utc        TIMESTAMP,
    end_utc          TIMESTAMP,
    direction        VARCHAR,       -- ascent | descent. The same slope run the
                                    -- other way is another effort, NOT another
                                    -- segment -- that is what keeps the §6.3
                                    -- tau comparison like-for-like.
    low_elevation_m  DOUBLE,
    high_elevation_m DOUBLE,
    vertical_m       DOUBLE,
    length_m         DOUBLE,
    mean_grade_pct   DOUBLE,
    duration_s       DOUBLE,        -- STOPPED TIME EXCLUDED (§12.3 review)
    vertical_speed_mps DOUBLE,      -- §6.6 trending
    avg_hr           DOUBLE,        -- TIME-WEIGHTED over moving time
    calc_version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_hourly (
    -- MILESTONE 3 (§8). Raw hourly series as fetched, one set per activity.
    -- Derived in the sense that it is re-fetchable, but NOT reproducible from
    -- data/raw alone -- the archive is external and revises -- so the raw JSON
    -- is cached under data/raw/weather/ the way the API listing is (§4.2).
    activity_id     VARCHAR NOT NULL,
    timestamp       TIMESTAMP NOT NULL,   -- naive UTC (CLAUDE.md)
    temp_c          DOUBLE,
    dew_point_c     DOUBLE,               -- §8: tracks heat stress far better
                                          -- than dry bulb
    humidity_pct    DOUBLE,
    wind_mps        DOUBLE,
    wind_dir_deg    DOUBLE,               -- CIRCULAR: never linearly
                                          -- interpolated (§8)
    precip_mm       DOUBLE,
    cloud_cover_pct DOUBLE,
    shortwave_wm2   DOUBLE,               -- PRECEDING-HOUR MEAN, open sky
    weather_source  VARCHAR NOT NULL,     -- forecast | era5 | assumed_indoor.
                                          -- Same pattern as
                                          -- records.altitude_source: provenance
                                          -- travels WITH the value.
    is_final        BOOLEAN NOT NULL,     -- false until ERA5 replaces it
    fetched_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_records (
    -- Weather PER RECORD, not a per-activity scalar (§8). A long run of
    -- several hours in a hot climate can span 10 F or more within one
    -- activity; at that point within-run
    -- change rivals between-run variation, and a run-level average destroys
    -- exactly what is being measured -- the same error class as one average
    -- heart rate over eight climbs (§1).
    --
    -- Derived and rebuildable from weather_hourly.
    activity_id     VARCHAR NOT NULL,
    timestamp       TIMESTAMP NOT NULL,
    temp_c          DOUBLE,
    dew_point_c     DOUBLE,
    humidity_pct    DOUBLE,
    wind_mps        DOUBLE,
    wind_dir_deg    DOUBLE,               -- carried by NEAREST observation,
                                          -- never interpolated (§8)
    precip_mm       DOUBLE,
    cloud_cover_pct DOUBLE,
    shortwave_wm2   DOUBLE,               -- the HOUR the record falls inside;
                                          -- never interpolated (a mean, not an
                                          -- instant)
    precip_prev_24h_mm DOUBLE,            -- trail-wetness proxy, per activity
    weather_source  VARCHAR NOT NULL,     -- forecast | era5 | assumed_indoor
    is_final        BOOLEAN NOT NULL,
    calc_version    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    -- ATHLETE-SUPPLIED STATE, and FREE TEXT ONLY.
    --
    -- A structured channel here would COMPETE with whatever later carries
    -- per-run context: two places to record run_type or RPE means two values
    -- that can disagree, and nothing to say which is current. So this table
    -- holds only an observation in the athlete's own words.
    --
    -- Durable home is data/athlete/notes.json; restored on every schema
    -- ensure, like window_confirmations. Ingest never writes to it.
    run_id    VARCHAR NOT NULL,
    note      VARCHAR NOT NULL,
    noted_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS segment_recovery (
    -- §6.3 (§12.5 milestone 6). One row per EFFORT: delta-HR and tau together,
    -- never tau alone. tau measures recovery RATE, not cost, so a 3 bpm
    -- excursion recovering fast is not comparable to a 20 bpm one.
    --
    -- tau_s may be NULL with `reason` set. That is the normal case until the
    -- corpus gate opens: measured within one (segment, direction), tau's SD
    -- can be as large as its median, and then it carries no information.
    -- `tau_reportable` on the run as a whole says whether it may be shown
    -- (§4.2: a metric that cannot be measured must not be reported with a
    -- caveat attached).
    segment_id      VARCHAR NOT NULL,
    activity_id     VARCHAR NOT NULL,
    slope_index     INTEGER NOT NULL,
    direction       VARCHAR NOT NULL,
    delta_hr_bpm    DOUBLE,               -- THE finding: peak minus opening HR
    tau_s           DOUBLE,               -- diagnostic; NULL unless earned
    drop_bpm        DOUBLE,
    clear_window_s  DOUBLE,               -- gap to the next effort
    tau_reportable  BOOLEAN NOT NULL,
    reason          VARCHAR,              -- why tau was declined, if it was
    calc_version    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dem_elevations (
    -- Derived and rebuildable from the raw coordinates plus a public tile
    -- (§3). NOT part of bronze: bronze holds the file as recorded, and
    -- altitude_device_m is what the watch said. This is what the ground says.
    activity_id    VARCHAR NOT NULL,
    timestamp      TIMESTAMP NOT NULL,
    altitude_dem_m DOUBLE NOT NULL,
    dem_tile       VARCHAR NOT NULL,
    calc_version   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS window_confirmations (
    -- ATHLETE-SUPPLIED STATE. Ingest NEVER writes to or deletes from this
    -- table. Derived tables are rebuilt from raw; athlete input cannot be,
    -- so it must sit outside the rebuild path (CLAUDE.md).
    -- Keyed on file_sha256 because §4 makes content the identity.
    file_sha256     VARCHAR PRIMARY KEY,
    valid_start_utc TIMESTAMP,
    valid_end_utc   TIMESTAMP,
    confirmed_at    TIMESTAMP NOT NULL,
    note            VARCHAR
);

CREATE TABLE IF NOT EXISTS stops (
    -- Stops are STRUCTURE, not noise. A mid-run stop is training data: a
    -- road crossing, a bathroom stop, an aid station. It is also a free
    -- HR recovery test (§6.3). NEVER excluded from analysis.
    activity_id   VARCHAR NOT NULL,
    stop_index    INTEGER NOT NULL,
    start_utc     TIMESTAMP NOT NULL,
    end_utc       TIMESTAMP NOT NULL,
    duration_s    DOUBLE NOT NULL,
    position      VARCHAR NOT NULL,      -- leading | midrun | trailing
    hr_at_stop    INTEGER,
    hr_at_resume  INTEGER,
    hr_min        INTEGER,
    hr_drop       INTEGER,               -- hr_at_stop - hr_min: free tau data
    lat           DOUBLE,
    lon           DOUBLE
);

CREATE TABLE IF NOT EXISTS validation_results (
    file_sha256    VARCHAR NOT NULL,   -- always present: the only universal
                                       -- identity (§4). A REJECTED file has no
                                       -- activity row but must not be invisible.
    activity_id    VARCHAR,            -- null for rejected files
    run_id         VARCHAR,            -- set for run-grain checks
    check_name     VARCHAR NOT NULL,
    severity       VARCHAR NOT NULL,   -- fatal | warning | info
    layer          VARCHAR NOT NULL,   -- bronze | silver | gold
    message        VARCHAR NOT NULL,
    value_observed DOUBLE,             -- numeric so thresholds are queryable
    value_expected DOUBLE,
    checked_at     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              VARCHAR PRIMARY KEY,   -- UTC-anchored: 2025-01-15T0700Z
    start_time_utc      TIMESTAMP,
    local_start_display VARCHAR,               -- DISPLAY ONLY, never a join key
    local_tz            DOUBLE,
    sport               VARCHAR,
    sub_sport           VARCHAR,
    created_at          TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id        VARCHAR PRIMARY KEY,
    run_id             VARCHAR NOT NULL,      -- FK -> runs.run_id
    file_hash          VARCHAR NOT NULL,      -- FK -> ingest_ledger.file_sha256
    source_file        VARCHAR NOT NULL,
    start_time_utc     TIMESTAMP  ,
    local_tz           DOUBLE,
    sport              VARCHAR,
    sub_sport          VARCHAR,
    device             VARCHAR,
    total_distance_m   DOUBLE,
    total_ascent_m     DOUBLE,                -- reported by device; see §5a-bis
    duration_s         DOUBLE,                -- session.total_elapsed_time (wall clock)
    moving_time_s      DOUBLE,                -- PRIMARY. From Garmin's rwd_* splits
                                              -- where present, else our detector.
    total_stopped_s    DOUBLE,                -- same source as moving_time_s
    moving_time_source VARCHAR,               -- splits | detector
    moving_time_detector_s DOUBLE,            -- OUR value, always. The only channel
                                              -- whose threshold can be stated, so it
                                              -- is the independent signal (§15.1).
    total_stopped_detector_s DOUBLE,
    timer_time_s       DOUBLE,                -- session.total_timer_time; == API
                                              -- duration
    source             VARCHAR NOT NULL,      -- garmin_watch | strava_phone
    sampling_regime    VARCHAR NOT NULL,      -- 1hz | smart_recording | unknown
    median_gap_s       DOUBLE,                -- evidence for sampling_regime
    valid_start_utc    TIMESTAMP,             -- "was exercising" window; defaults
    valid_end_utc      TIMESTAMP,             -- to the recorded values (§4.3).
                                              -- ORTHOGONAL to coverage_fraction:
                                              -- coverage = "was running, terrain
                                              -- unusable"; valid window = "was
                                              -- NOT running, nothing usable".
    coverage_fraction  DOUBLE,
    -- THE WATCH'S OWN SESSION SUMMARY of max HR. BOTH THIS AND THE RECORD
    -- STREAM COME FROM THE SAME FILE, written by the same watch -- the earlier
    -- name (`max_hr_device`) and its "ingest finding" note implied a contrast
    -- with something non-device, and there is none.
    --
    -- WHAT THE DIFFERENCE ACTUALLY MEASURES: the watch samples HR
    -- continuously but only WRITES a record on the Smart Recording schedule,
    -- while this summary is computed over the full internal stream. So the
    -- record-stream maximum is a DECIMATED view of it. Measured: 1 Hz files
    -- agree exactly; Smart Recording files often disagree, ALWAYS with the
    -- session figure higher by a beat or two, never lower -- which is what
    -- decimation predicts, since skipping a sample can only lose a peak and
    -- never invent one.
    session_max_hr     INTEGER,
    -- THE MAX HR THE ZONES ARE ANCHORED TO, from this file's own
    -- time_in_zone message (§14e). Every zone boundary is a percentage of it,
    -- so if the athlete has never reached it, every boundary is shifted up by
    -- the gap. Stored per file because it is dated configuration, not a
    -- constant -- one athlete's files can carry several boundary sets.
    declared_max_hr    INTEGER,
    -- ALTITUDE PROVENANCE, MEASURED (§9, §15.1). `altitude_source` is derived
    -- from the two channels on THIS file; `altitude_source_device` is what the
    -- device map expected. They are stored separately and compared, because a
    -- disagreement is a finding -- a new watch whose map entry is missing or
    -- wrong, or a firmware change in what the watch feeds `enhanced_altitude`.
    altitude_source          VARCHAR,
    -- measured | device_map | declared. WHERE THE LABEL CAME FROM, because
    -- "could not be measured" and "measured and disagrees" are different
    -- facts: an indoor run has no position, no GNSS channel and no altitude,
    -- so it falls back to the map rather than reading `unknown`.
    altitude_source_basis    VARCHAR,
    altitude_source_device   VARCHAR,
    altitude_source_agrees   BOOLEAN,
    altitude_divergence_m    DOUBLE,    -- median |record - gnss| over paired records
    altitude_identical_frac  DOUBLE,    -- fraction reproducing GNSS exactly
    altitude_sd_record_m     DOUBLE,
    altitude_sd_gnss_m       DOUBLE,
    -- GARMIN'S OWN POST-RUN SELF-EVALUATION (first seen on the FR265). One
    -- channel, asked seconds after the run, on Garmin's scale. It is the only
    -- perceived-effort source in this build: session load is garmin_rpe x
    -- timer minutes (src/trail_factors/analysis/session_load.py). Feel is stored and
    -- shown,
    -- never used, because its direction is unverified.
    garmin_rpe               DOUBLE,    -- 1-10, decoded from session.workout_rpe/10
    garmin_feel              INTEGER,   -- 1-5, decoded from session.workout_feel
    garmin_selfeval_scale    VARCHAR,   -- the decoding, stored with the value
                -- records with position / all records
    gps_contiguous     BOOLEAN,               -- one positioned block, or scattered

    -- THE CONFIRMED WINDOW, APPLIED (§4.3). When window_applied is
    -- true, total_distance_m / duration_s / timer_time_s / session_max_hr
    -- describe the CONFIRMED WINDOW and were recomputed from the trimmed
    -- records and clipped splits. Garmin's whole-file figures are kept in the
    -- *_whole_file columns -- they are what the device said about the FILE,
    -- which is still true about the file, and must never be read as the
    -- activity's own. When a file's tail is a car ride, the whole-file
    -- distance and timer are both inflated by the drive.
    window_applied            BOOLEAN,
    session_distance_m_whole_file DOUBLE,
    timer_time_s_whole_file   DOUBLE,
    duration_s_whole_file     DOUBLE,
    session_max_hr_whole_file  INTEGER,
    avg_heart_rate_trimmed    DOUBLE,   -- time-weighted over the trimmed records

    pipeline_version   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    activity_id           VARCHAR NOT NULL,
    timestamp             TIMESTAMP   NOT NULL,
    elapsed_s             DOUBLE,
    lat                   DOUBLE,             -- degrees, converted from semicircles
    lon                   DOUBLE,
    -- Renamed from altitude_gps_m. The FIT field is the same on
    -- every device; only the device says what fed it, so the old name was a
    -- claim the data could not support -- already wrong for Strava (§15.4)
    -- before the FR265 made it wrong for Garmin too. UNRELIABLE either way (§5a).
    altitude_device_m     DOUBLE,
    -- Co-located with the value ON PURPOSE: provenance must be readable
    -- without joining activities.device (§14d). gps | barometric |
    -- dem_suspected | unknown.
    altitude_source       VARCHAR,
    -- THE INDEPENDENT CHANNEL (§9, added with the first FR265 file).
    -- `gps_metadata.enhanced_altitude`, paired to the record by FILE
    -- ORDER because gps_metadata carries no timestamp. On a GPS watch this is
    -- the SAME series as altitude_device_m (97.6% identical on the FR245); on
    -- a barometric one it is not (3.7%), and that difference is what
    -- `altitude_source` is now measured from rather than looked up.
    --
    -- IT IS THE ONLY ALTITUDE CHANNEL NOT DERIVED FROM PRESSURE on a
    -- barometric watch, so §15.1's "is the watch independent of our DEM"
    -- question is unanswerable without it.
    altitude_gnss_m       DOUBLE,
    distance_m            DOUBLE,
    heart_rate            INTEGER,
    cadence_raw_rpm       INTEGER,            -- one-leg, as recorded (§9)
    fractional_cadence    DOUBLE,             -- steps/min formula UNRESOLVED (§9)
    speed_mps             DOUBLE,             -- from enhanced_speed
    gps_accuracy          INTEGER,            -- Strava only; no quality flags (§14b)
    temperature_c         DOUBLE,             -- IGNORE: reads body heat (§4)
    -- Absent on FR245 without an accessory. Nullable so adding a strap
    -- later needs no migration (§9).
    vertical_oscillation  DOUBLE,
    stance_time           DOUBLE,
    stance_time_pct       DOUBLE,
    vertical_ratio        DOUBLE,
    step_length           DOUBLE,
    power                 INTEGER
);

-- §14e: THE ZONE BOUNDARIES IN FORCE WHEN THE FILE WAS RECORDED.
--
-- §14e originally recorded that older boundaries "cannot be regenerated from
-- the FIT files" and were lost. That was REFUTED: every FIT carries a
-- `time_in_zone` message with `hr_zone_high_boundary` and `hr_calc_type`, and
-- one athlete's files can carry several distinct boundary sets.
-- The claim came from the Garmin API, which returns only the CURRENT set.
--
-- Storing them makes time-in-zone computable on every activity from ITS OWN
-- scale, instead of applying today's configuration retroactively. It does not
-- make pooling across a boundary change valid -- each set is a real scale
-- and a minute in "zone 2" means a different thing under each -- it makes the
-- pooling DETECTABLE.
-- AN ATHLETE-STATE FILE WRITTEN FROM A READ-ONLY PATH HAS NO TABLE HERE.
-- ensure_schema() runs only on WRITE connections and the MCP server opens
-- read-only connections exclusively, so a projection refreshed only here
-- would leave every read returning EMPTY while the save reported success.
-- Any projection introduced for an athlete-state file must be refreshed by
-- its own WRITE path, and its reads must detect staleness rather than return
-- empty (CLAUDE.md).

CREATE TABLE IF NOT EXISTS activity_hr_zones (
    activity_id       VARCHAR NOT NULL,
    zone              INTEGER NOT NULL,     -- 1..5
    low_bpm           INTEGER,              -- previous zone's high + 1
    high_bpm          INTEGER,
    hr_calc_type      VARCHAR,              -- percent_hrr | percent_max | custom
    time_in_zone_s    DOUBLE,               -- Garmin's own figure, for cross-check
    source            VARCHAR               -- 'fit_session'
);

CREATE TABLE IF NOT EXISTS laps (
    activity_id   VARCHAR NOT NULL,
    lap_index     INTEGER NOT NULL,
    start_time    TIMESTAMP  ,
    duration_s    DOUBLE,                    -- total_elapsed_time. PROVENANCE
                                             -- ONLY: includes auto-paused time,
                                             -- so a lap pace built on it is
                                             -- inflated. Do NOT use for rates.
    total_timer_time DOUBLE,                 -- AUTHORITATIVE for lap totals,
                                             -- exactly as for splits (§9).
                                             -- Reproduces Garmin's
                                             -- minActivityLapDuration.
    distance_m    DOUBLE,
    avg_hr        INTEGER,
    max_hr        INTEGER,
    avg_cadence   INTEGER,
    avg_speed     DOUBLE,
    trigger       VARCHAR
);

CREATE TABLE IF NOT EXISTS splits (
    activity_id        VARCHAR NOT NULL,
    split_index        INTEGER NOT NULL,
    split_type         VARCHAR,              -- rwd_run | rwd_walk | rwd_stand
    start_time         TIMESTAMP  ,
    end_time           TIMESTAMP  ,
    duration_s         DOUBLE,               -- end-start. PROVENANCE ONLY: 1 s
                                             -- timestamp resolution makes it
                                             -- understate nearly every group (§9).
                                             -- Do NOT sum this.
    elapsed_reported_s DOUBLE,               -- rounded to whole seconds by Garmin
    total_timer_time   DOUBLE,               -- AUTHORITATIVE for split totals:
                                             -- reproduces Garmin exactly (§9).
    total_distance_m   DOUBLE,
    avg_speed          DOUBLE,
    max_speed          DOUBLE,
    total_ascent_m     DOUBLE,
    total_descent_m    DOUBLE,
    avg_vert_speed     DOUBLE,
    start_lat          DOUBLE,               -- end_position is always null (§14b)
    start_lon          DOUBLE
);

-- Analysis reads this, never `records`: it trims LEADING and TRAILING junk
-- only. It can never exclude a mid-run stop, because the window is a single
-- pair of bounds -- mid-run stops are training data (§4.3).
-- Defined last because it references activities and records.
CREATE OR REPLACE VIEW records_valid AS
    SELECT r.* FROM records r JOIN activities a USING (activity_id)
    WHERE r.timestamp >= coalesce(a.valid_start_utc, r.timestamp)
      AND r.timestamp <= coalesce(a.valid_end_utc, r.timestamp);

-- Gold: never report one pace without the other (§4.3).
CREATE OR REPLACE VIEW run_summary AS
    SELECT a.run_id, a.activity_id, a.source, a.start_time_utc, a.local_tz,
           a.total_distance_m,
           a.duration_s      AS elapsed_time_s,
           a.moving_time_s,
           a.total_stopped_s,
           a.total_distance_m / nullif(a.duration_s, 0)    AS elapsed_speed_mps,
           a.total_distance_m / nullif(a.moving_time_s, 0) AS moving_speed_mps,
           (SELECT count(*) FROM stops s WHERE s.activity_id = a.activity_id)  AS
           stop_count,
           (SELECT count(*) FROM stops s WHERE s.activity_id = a.activity_id
              AND s.position = 'midrun')                                       AS
              midrun_stop_count,
           a.sampling_regime, a.coverage_fraction, a.gps_contiguous
    FROM activities a;

-- §14c: terrain usability at the RIGHT GRAIN. coverage_fraction is a
-- per-activity average and forces an all-or-nothing call on a run that may be
-- 80% clean. A positioned BLOCK is consecutive positioned records <=30s apart;
-- it is terrain-usable when it spans >=60s AND >=200m, enough for the ~30m
-- grade window of §9. Grade/GAP/segments read only usable records; HR,
-- cadence, distance and pace read everything (§3.1).
CREATE OR REPLACE VIEW terrain_blocks AS
    WITH positioned AS (
        SELECT activity_id, timestamp, distance_m,
               CASE WHEN date_diff('second',
                        lag(timestamp) OVER (PARTITION BY activity_id ORDER BY
                        timestamp),
                        timestamp) > 30
                    OR lag(timestamp) OVER (PARTITION BY activity_id ORDER BY timestamp)
                       IS NULL
                    THEN 1 ELSE 0 END AS new_block
        FROM records WHERE lat IS NOT NULL
    ), numbered AS (
        SELECT *, sum(new_block) OVER (PARTITION BY activity_id
                                       ORDER BY timestamp) AS block_index
        FROM positioned
    )
    SELECT activity_id, block_index,
           count(*)                                        AS n_records,
           min(timestamp)                                  AS block_start,
           max(timestamp)                                  AS block_end,
           date_diff('second', min(timestamp), max(timestamp)) AS span_s,
           max(distance_m) - min(distance_m)               AS distance_m,
           (date_diff('second', min(timestamp), max(timestamp)) >= 60
            AND max(distance_m) - min(distance_m) >= 200)  AS terrain_usable
    FROM numbered GROUP BY 1, 2;
"""
