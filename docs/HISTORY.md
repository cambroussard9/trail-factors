# Development history

Development happened over 156 commits in a private repository from September to October 2026.

This is a condensed account of how Trail Factors was built, grouped by commit
date. Section numbers (§) refer to [SPEC.md](../SPEC.md), where each design
decision is recorded in full. Commits that only recorded the author's own
athlete-supplied data are left out, as is work on parts of the private system
that are not in this release.

Two kinds of event are marked inline and collected again at the end:

- **RETRACTED / CORRECTED**: a finding or claim that was later shown to be
  wrong and withdrawn or rewritten.
- **REVIEW FIX**: a defect found by a deliberate review or audit.

## Milestones

### 2026-09-05: foundations

- Project set up (git, uv, Python 3.12) and a read-only FIT inspection script
  written before any ingest code; the spec was revised from what the files
  actually contained.
- Dual-source design: every run recorded by both watch and phone, kept as
  complementary sources rather than duplicates (§14b).
- **Milestone 0**: one Garmin FIT file into DuckDB, with `sampling_regime`
  detected from the observed record spacing (§12).
- The phone file ingested too; a `runs` table added above `activities`, with
  identity anchored in UTC and local time used for display only.
- Garmin Connect downloader scaffolding, with guards against compounding
  Garmin's rate limit (fail fast on HTTP 429, reject empty credentials, back
  off after a failed login). Selection policy written: download broadly, model
  strictly (§4.1).
- **Milestone 1**: batch ingest, timestamp validation and an idempotent
  re-run. A validation layer persists its findings at all three layers (§4.2).
- Movement-ceased detection and the valid window (§4.3); stops stored as
  structure rather than discarded as noise.
- Athlete-supplied state (window confirmations) made to survive a database
  rebuild; a `sync` command added; Garmin's run/walk splits made primary for
  moving time; tests isolated from athlete state.
- Coaching-layer design written (§16); per-portion GPS gating and the
  principle that a missing layer degrades output rather than forfeiting the
  run (§3.1).
- Working rules adopted: every figure in the spec must be reproducible, and
  every scripted text replacement must assert its anchor matched.
- **Milestone 1.5**: local 1 m DEM elevation lookup (§5a).
- **RETRACTED / CORRECTED**: the terrain corpus count was corrected; the DEM
  work contradicted an earlier prediction.
- **REVIEW FIX**: a cross-reference audit of the spec, and an audit of the
  first download step, each led to corrections.

### 2026-09-06: elevation, silver layer, segments, report, MCP server

- Two external checks confirmed the DEM and showed the watch's elevation
  profile is inflated (§5a-bis.1).
- **Milestone 2**: the silver layer, gated by resolution invariance (§12.2).
- Watch speed shown to be fused and grade-biased; GPS chording isolated as the
  source of part of that bias; grade-binned pace delivered split by run and
  walk (§9, §6).
- Garmin's summary given a declared status per field (oracle for some,
  known-wrong for others), and a one-directional bias in split durations fixed
  by using timer time.
- **Milestone 5**: the segment matcher, plus altitude provenance; §5 amended.
- **REVIEW FIX**: an adversarial review of the matcher; 22 findings survived
  verification and all were fixed (§12.3).
- **Milestone 5.5**: the segment unit became a slope rather than a prominence
  hill, then segment identity became `(place, side)` (§12.4). The commits
  call it "Milestone 6"; SPEC §12.4 numbers it 5.5.
- **Milestone 3**: weather auto-fetch with a per-row source label, and
  free-text annotations.
- **Milestone 6 begins**: HR recovery (§6.3) built; the recovery time constant
  is refused because it varied too much to report.
- `cli.py` split into a package, guarded by three golden queries.
- The §16.2 post-run report, whose output schema is the contract; it opens
  with a NOTABLE section.
- The MCP server: a thin wrapper over parameterised primitives, every result
  carrying `calc_version` and its applied filters; raw data unreachable from
  MCP; `sync` run as a background job (§12.6).
- Per-record quality flags (§15.4); pace displayed as `M:SS/mi` and never
  stored; basic metrics (§6.0); drift onset (§6.9) built and refused; the
  cadence formula resolved (§9).
- Per-effort pace, an effort grade profile, a segment-pooling guard, and a fix
  for the MCP layer silently dropping filters.
- **RETRACTED / CORRECTED**: two earlier elevation claims retracted; the grade-adjusted pace test did not survive scrutiny; the
  claim about why pace is flat across grade was softened to "undetermined";
  the GPS dead-spot finding was first reframed (terrain masking refuted) and
  then retracted as an aggregation error once 1 Hz data was available
  (§14h-CORRECTION).
- A discrepancy between two drift measures reconciled (§6.7).
- **REVIEW FIX**: an audit of what the MCP boundary does and does not
  protect, closed on 2026-09-08.

### 2026-09-07: per-session metrics and the residual-model design

- §2 amended: the residual model is a hill-plus-weather normaliser, and the
  climb signature (§6.5) becomes a model input; a falsification test for the
  fitness term written (§14n).
- §17 written: per-session metrics in tiers, with a promotion path; candidate
  metrics M1-M6 registered (§17.2) and M3 (run/walk cycles) built.
- Lap durations switched to timer time; hill pace by direction, direction-aware
  effort drift and the binned `session_series` shipped.
- The weather term fitted for the first time (§2.6): not distinguishable from
  zero, and recorded as such.
- Golden queries pinned to a fixed activity set; the rule that a run-level
  covariate has n = runs, not n = records, written down.
- **RETRACTED / CORRECTED**: the claim that historical HR zone boundaries
  were lost was refuted (every FIT file carries its own, §14e); stale figures
  corrected.

### 2026-09-08: boundary audit closed, zones by gait, DEM coverage

- The MCP boundary audit closed with its mechanism identified (§14m-bis).
- M7, time in HR zone by gait (`zone_by_gait`).
- Runs outside every DEM tile detected, with the fix named; `retile` added
  (§14p).

### 2026-09-12: second watch model and account history

- Support for the Forerunner 265: a second altitude channel from GNSS
  metadata, altitude provenance measured per file rather than looked up, and
  three assumptions shaped by the older watch that had broken quietly (§9.1,
  §9.2, §9.3).
- M3's per-walk recovery gated on the HR lag (§17.2); a maximum-HR metric
  added to §6.0.
- Account-history lookups: sweep activity summaries to choose downloads, while
  ingesting nothing (§10.1).
- **RETRACTED / CORRECTED**: a premise about the HR zone configuration was
  refuted on measurement.

### 2026-09-14 to 2026-09-17: ceiling tools and every sport at the door

- Two `NameError`s in the tail of `sync` fixed, with a guard for the whole
  class.
- `hr_above` and `time_above_hr`: is an HR ceiling demonstrated, or only
  recorded? (§14e-ter)
- Selection policy enforced at ingest: every sport comes in, bucketed, and
  nothing else is decided there (§4.1).
- **RETRACTED / CORRECTED**: the session summary had been framed as a second
  source; it is the same watch at a different resolution.

### 2026-09-19 to 2026-09-20: robustness, tiles, dynamics, rides

- Sync-job state moved out of the athlete-state directory.
- `fetch-tile`: resolve a DEM tile against the USGS 3DEP index, download it
  and verify it (§5a.1-bis).
- A report section that raises degrades alone instead of taking the whole
  payload down; a missing field is never coerced to zero.
- MCP error contract: a misplaced control flag is rejected by name, and
  `retryable` gets one written definition (§12.6).
- `session_series` gains per-bin running dynamics, from running records only,
  with a sample count per field (§17.1b-bis).
- Stationary-bike block annotations: the athlete supplies resistance and
  cadence the watch cannot record (§18), plus the §18.2 reads (time in
  resistance, cadence reconciliation, load index).
- **RETRACTED / CORRECTED**: two claims in the stationary-ride module.

### 2026-09-29: statuses that support their values

- A primitive's `ok` status now requires a value, and an absence reason must
  name what it actually checked.
- **RETRACTED / CORRECTED**: absence reasons that asserted causes nobody had
  checked were replaced by reasons derived at call time.

### 2026-10-08: the bad-data loop

- A confirmed window is applied before validation, so a file rejected for a
  contaminated tail can be rescued; confirmation works by Garmin id or file
  hash (§4.3).
- `sync` reports the files that did not ingest.
- Golden queries re-captured, because the window was now actually applied.

### 2026-10-13: cleanup

- SonarQube-driven cleanup of the athlete CLI module.

### 2026-10-31: comparison sets, weather, tiles, venues

- Comparison sets honour athlete exclusions and the subject's sport, and use
  no look-ahead (candidates start before the subject run).
- `sync_status` and a new `ping` never touch the database; the response
  envelope slimmed (`calc_version` becomes `{pipeline_version, digest}`).
- The `confirm-window` command became a thin wrapper over `confirm-file`, so
  a window is never half-applied.
- The recovery-gate test pins the gate's rule on fixtures instead of a corpus
  count.
- `max_hr_over` includes the session summary and bounds declared values to
  the selection's end date and sport; the ceiling comparison is gated by
  measurement.
- Weather: archive lag treated as a duration, an `is_final` flag on
  conditions, shortwave radiation added, and an estimated WBGT shown beside
  temperature (display only).
- `fetch-tile` checks completeness against the served `Content-Length` rather
  than a possibly stale index, and refuses a download with no
  `Content-Length`; `retile` runs `recovery` after `match`; DEM rasters load
  lazily.
- New tool `mile_splits` (per mile or quarter mile); windowed bests use one
  time-at-distance rule; `session_series` keeps a bin's HR when it has too
  few distance records, and per-bin dynamics became opt-in.
- GNSS pairing: a mid-run loss leaves nulls, never a stale reading, and a
  reading after a recording gap over 60 s is treated as missing.
- Venue identity (§19): planned, then built (anchors, assignment, and
  venue-aware comparison sets); `venue-candidates` proposes anchors and
  writes nothing; the first venues were configured from athlete-confirmed
  anchors.
- A regression in the ceiling and dose tools, which had begun dropping
  athlete-excluded runs, was fixed.

## Retractions and corrections

| Date | What was withdrawn or corrected |
|---|---|
| 2026-09-05 | The terrain corpus count was corrected. |
| 2026-09-05 | The DEM work contradicted an earlier prediction. |
| 2026-09-06 | Two earlier elevation claims retracted (§5a-bis.2 records the retractions). |
| 2026-09-06 | The grade-adjusted pace test did not survive scrutiny (§12.2). |
| 2026-09-06 | The explanation for pace flatness across grade softened to undetermined (§6.1). |
| 2026-09-06 | GPS dead spots: terrain masking refuted, then the whole dropout finding retracted as an aggregation error (§14h-CORRECTION). |
| 2026-09-07 | "Historical HR zone boundaries are lost" refuted: every FIT file carries its own (§14e). Stale figures corrected. |
| 2026-09-12 | A premise about the HR zone configuration refuted on measurement. |
| 2026-09-16 | The session summary had been called a second source; it is the same watch (§14e-ter). |
| 2026-09-20 | Two claims in the stationary-ride module corrected. |
| 2026-09-29 | Absence reasons that asserted unchecked causes replaced with derived ones. |

## Review fixes

| Date | Review | Outcome |
|---|---|---|
| 2026-09-05 | Cross-reference audit of the spec | Findings fixed. |
| 2026-09-05 | Audit of the first download step | Findings recorded; dataset corrected. |
| 2026-09-06 | Adversarial review of the segment matcher | 47 findings raised, 22 survived verification, all fixed (§12.3). |
| 2026-09-06 | MCP boundary audit | Closed on 2026-09-08 with the mechanism identified (§14m-bis). |
| 2026-10-13 | SonarQube static analysis | Dead parameter, shadowed name, unused variable and dispatcher split cleaned up. |
