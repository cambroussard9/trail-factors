# Trail Factors — Design Spec

**What this is:** a training-analysis system for ONE trail/ultra runner, built around that runner's own Garmin `.fit` files. It ingests per-second data, adds weather and terrain context, segments trails into named slopes, and computes gated, provenance-carrying metrics that a coaching chat reads through an MCP server. Single user by design: no multi-tenancy, no app, no web UI.
**Status:** a working system, released as a reference implementation of the design. It is shaped around one athlete's data and devices; running it on yours means supplying your own data and configuration (below). Sections record not only what was built but the failure classes that were found and how — those are the most reusable part of this document.
**Running it on your own data:** every athlete-specific value lives in `config/athlete.yaml` (gitignored; copy `config/athlete.example.yaml`, which documents each field). It is validated strictly on load — unknown keys, wrong types and out-of-range values are errors naming the field and line. Paths default to repo-relative locations and are overridable by environment variables documented in `.env.example`; the database is `TRAIL_FACTORS_DB`. The whole `data/` directory is gitignored, including `data/athlete/`, which holds input that cannot be rebuilt from anything — back it up yourself.
**Units:** feet for elevation, miles for distance, °F. Store SI internally, convert at display.

**Pace is displayed as `M:SS/mi` and is never stored.** There is no pace
column anywhere; pace is derived at display time from m/s, or from the
seconds and metres it came from, by `src/trail_factors/units.py` — one formatter, one
mile constant (1609.344 exact). Decimal minutes are forbidden in output:
`13.45 min/mi` reads as 13:45 and is 13:27, an 18 s error always in the
same direction and in the range a reader will not question. A stored
`13.45` cannot be told apart from a stored `13:27` downstream, which is
why the rule is store-SI-format-late rather than a formatting preference.
Formatted pace is carried in tool payloads (`measured.*_pace_per_mile`,
each grade-table row's `pace_per_mile`) BESIDE the quantity it came from,
so a chat quoting a pace never converts one by hand. Internal thresholds
stay in m/s (`PACE_FATAL_MPS`); only display passes through the formatter.

---

## 1. Problem statement

Garmin Connect reports population-fitted averages over runs. On rolling trail terrain, averaging destroys the signal:

- An average HR over a run with eight climb/descent cycles tells you nothing about whether today was hard or the trail was hard.
- Grade, temperature, dew point, fatigue state, and pace all move at once. Nothing in Connect isolates them.
- Interval and form work (fartlek, short repeats, form drills) has huge within-session variance; session averages are meaningless.

**Goal:** hold per-second data plus context (weather, terrain identity, subjective state) long enough to build *personal* models, then use residuals against those models to answer questions Connect can't.

---

## 2. The central construct

Fit a personal baseline model on own history:

```
predicted_HR = f(grade_adjusted_speed, grade, elapsed_time, cumulative_ascent, dew_point, ...)
```

The interesting quantity is the **residual**: how much higher/lower HR was than *this athlete's own model* predicted for that exact terrain, pace, and conditions.

One construct, many answers:

| Question | Residual view |
|---|---|
| Was HR high, or was the trail hard? | Mean residual for the run |
| Is the athlete under-recovered? | Residual elevated in first 20 min, before fatigue could explain it |
| Does a breathing protocol help? | Residual grouped by protocol annotation |
| Is HR drift starting later? | Slope of residual vs. elapsed time, tracked weekly |
| Is the athlete fitter? | Refit monthly — the coefficients *are* the fitness |

Linear model or gradient boosting. Usable at ~30 runs. Improves weekly.

### 2.1 What the model is FOR

**The model is a HILL-PLUS-WEATHER NORMALISER.** On a trail never run before, in weather hotter than the training weather, HR looks bad. Given enough history, the model should say what HR was EXPECTED for that terrain and that weather, so that the residual is about the athlete and not about the hill or the heat.

That is the acceptance criterion. A model that cannot separate *the hill* and *the heat* from *the athlete* has not done its job, however good its fit.

### 2.2 AVERAGE GRADE IS NOT A SUFFICIENT STATISTIC

Three runs with identical gain and identical distance — one sustained mile-long climb, six rollers, and constant up-and-down with no flat — cost completely different amounts. **A per-record model sees identical grade values in all three.** Total ascent cannot distinguish them either (§5a-bis: it has no true value at all). The structure has to enter the model explicitly or it is invisible to it.

**TWO ADDITIONS, at different grains.**

**(a) PER-RECORD: distance into the current climb (or descent).** The tenth minute at 10% costs more than the first, and grade alone cannot express that. Computable per record from the §5 slope segments as `path_distance_m - start_distance_m` of the containing traversal, with a signed direction and NULL outside any slope. It is a fatigue-within-feature term, and it is the cheapest of these to build.

**Coverage limit:** slope traversals exist only where a DEM tile does, so this covariate is NULL wherever no tile covers the ground — at one stage more than half the corpus — and the model must accept it as missing rather than zero. Zero and "not on a climb" are different states, and the falsy-default failure (`value or default`) has inverted results in this codebase twice (CLAUDE.md).

**(b) PER-RUN: the climb signature, promoted from descriptor to INPUT — see §6.5.**

### 2.3 THE k(fitness, date) TERM GETS A DEDICATED INSTRUMENT

A **regular flat-ground session in one fixed gait**, if one is run, is the probe. Same surface, same form, same intent, each time. It is the one controlled observation available, and it exists because on flat ground **f(grade) drops out entirely**, g(weather) is measured and removable, and what remains is fitness and time-into-run.

**PROBE READINGS AT MATCHED ELAPSED TIME.** If session duration grows across a training block, any whole-run average is confounded with duration. Read the probe at fixed marks instead — pace-at-HR and HR-at-pace at, say, minute 25 — plus efficiency factor (§6.8), drift onset (§6.9) where the session is long enough, and cadence and windowed bests as form-consistency checks.

**THE TRAIL MODEL READS THE PROBE TRAJECTORY AS k(date).** Expected HR on a trail is computed from **the most recent probe**, the trail's climb signature, and the day's weather — *not* from a historical average that smears a training block into one number.

**TRANSFER ASSUMPTION, STATED SO IT CAN BE WRONG.** The probe measures **cardiovascular fitness in one gait on flat ground**. It does not measure structural or eccentric capacity. Those are tracked separately, by the per-effort descent metrics (§6.0e, §6.2). **Expected HR from the probe is an ENGINE prediction; leg cost is a separate term** and must not be folded into it.

**The probe series is NOT selectable by run type in this build.** `run_type` is not collected (§16.6), so a `run_type` filter is rejected by name rather than matching nothing; select the probe by cluster and date instead. A later version re-enables `run_type` through user-defined run types, at which point the probe becomes a type the athlete declares.

### 2.4 NEW-TRAIL UNCERTAINTY — §15 propagation applied to PREDICTION

On a trail never run before, the segment library has nothing. The prediction rests on the grade curve, the climb signature and weather alone, with no matched terrain to anchor it.

**The expected-HR estimate there MUST carry a wider interval and REPORT it, rather than returning a single number.** This is §15's confidence propagation pointed forward instead of backward. The width depends on whether the new trail's signature resembles anything in the corpus — which is precisely what §6.5's similarity measure is for. A prediction on a signature with no neighbour is an extrapolation and must say so.

### 2.5 CURRENT BLOCKER — data, not design

**Milestone 7 stays waiting on data.** Two measurements say why.

**The steady-effort gate passes a minority.** Applying §6.4's stated gate — second-half mean within ±5% of the first, and no block faster than +15% of the run's median lasting over 60 s — to the activities where both halves are computable, about a third passed the halves test, over half passed the surge test, and only around one in five passed BOTH. (Measured with path-derived speed, because GAP is refused until §6.1 refits; the gate as written is defined on GAP, so this is an approximation of it and will move when GAP lands.)

**Weather and fitness are confounded, and the sign is the wrong way round.** Efficiency factor against dew point across the corpus showed a moderate negative correlation: EF falls as dew point rises — but season and fitness moved together over the same period, so the weather and fitness terms are confounded. **Nothing in the corpus separates "heat cost me" from "I was less fit then."**

**The contrast the model needs is same hills, cooler weather, better fitness — and it arrives as the season turns.** Until then a fitted weather term would absorb the fitness trend and report a confident number with a season inside it.

### 2.6 THE WEATHER TERM — first estimate, and it is NOT DISTINGUISHABLE FROM ZERO

**The fit records the corpus fingerprint and the calc_version digest it was made against**, like every tool result, so the estimate can be tied to the data and code that produced it. This is a FEASIBILITY CHECK for the normaliser, not the normaliser.

**THE ANSWER: a small coefficient that cannot be told apart from zero.** Under the honest error the 95% interval spans zero by roughly ±1 bpm/°F. **Do not use the number.** Its value is that it says precisely what is missing.

#### The calibration set: a flat urban cluster

Runs on a flat urban route have grade ≈ 0, so §2's terrain term drops out and nothing else moves. **It needs no DEM — there is nothing to correct.**

The set was a handful of runs (single digits) over about three months, a few thousand `rwd_run` records, spanning a dew-point range of roughly 30 °F.

**THE COLLINEARITY WAS NOT THE PROBLEM — and that was a genuine piece of luck.** The run-level correlation between date and dew point was about +0.1, essentially orthogonal and far under the 0.7 stop. A cold, dry run arriving weeks *after* a humid one is what broke the seasonal trend. **A calibration set that happened to warm monotonically would have been unusable.**

Model, per record, `rwd_run` only, stopped time excluded, HR lag-corrected at 40 s, speed from the device odometer:

`hr_shifted ~ speed + dew_point + date + time_into_run`

Each coefficient was reported under three error bases — naive per-record, cluster-robust by run, and leave-one-run-out jackknife:

| error basis | verdict on the dew-point term |
|---|---|
| naive per-record | "distinguishable" — **and wrong** |
| cluster-robust, by run | not distinguishable |
| leave-one-run-out jackknife | not distinguishable |

**THE NAIVE ERROR WAS ABOUT 28× TOO SMALL, AND THAT IS THE METHODOLOGICAL FINDING.** Dew point and date vary ONLY BETWEEN RUNS. Treating thousands of correlated records as independent evidence about a run-level covariate is the same pseudo-replication that §6.9 records for drift onset — there, one 5-minute bin held 917 pairs drawn from 2 passages. **The effective n for the weather coefficient is the number of runs, not the number of records.** Any fit reporting a per-record standard error on a run-level covariate is making this mistake; the understatement is roughly sqrt(records per run). Cluster by run, or jackknife over runs, and report both.

**The date coefficient was unidentified outright:** its jackknife SE was more than ten times its value. A handful of runs over a few months cannot see a fitness trend.

**The point estimate also had the WRONG SIGN for a heat-cost model** — HR *falling* as dew point rises. At this uncertainty that is noise, not a finding, and it must not be reported as one.

#### The check set: home-trail efficiency factor — REFUSED by the stated collinearity stop

A couple of hundred segment efforts on the home trail, fitted with **segment fixed effects** so the fit is not measuring which slopes each run happened to cross. The implied coefficient had the opposite sign and was not distinguishable from zero under any error basis.

**But it is refused before that matters: `corr(dew point, date)` on that set was about +0.86, above the stated 0.7 stop.** This set cannot separate weather from fitness, and the reason is recorded in §2.5 — season and fitness moved together.

#### Which set to trust, given they DISAGREE IN SIGN

**Neither estimate is distinguishable from zero, so the disagreement is not evidence of anything** — two noise draws either side of zero.

**Trust the flat-ground DESIGN and distrust both NUMBERS.** The flat set is the only one that identifies the coefficient at all (date/dew-point correlation near zero against well above the stop); it simply had too few runs. The trail set has the runs and cannot separate the terms. **The fix is more flat-ground runs across a dew-point range, not a better estimator.**

**What would make this estimable:** at the observed between-run scatter, distinguishing a ±0.3 bpm/°F effect needs roughly 25–30 flat-ground runs spanning a comparable dew-point range. That is a season of flat-ground running, not a rebuild.

#### The flat-ground DEM deferral is REVERSED for this purpose only

§5a deferred DEM tiles for the flat cluster on cost grounds, because there was little grade to correct. **For the weather term that flatness is the ASSET, not the reason to skip it** — it is exactly why the terrain term drops out and the coefficient is identifiable there at all. The weather fit is therefore run on the flat cluster and needs no tiles.

**For TERRAIN analysis the tiles are a separate decision, sized by measurement.** Bounding the cluster's positioned records in the projection CRS (`projection_crs` in `config/athlete.yaml`) shows which 10 km grid cells they cross — in the original case two cells, not the four a rough guess suggested, each a 1 m tile of several hundred MB. The same local-only rule as §5a applies: bulk public tiles downloaded once and read locally, never a per-coordinate service, with the same coverage check on ingest. The original position here was that there should be no automated fetch at all; **§5a.1-bis reversed that** — `fetch-tile` resolves a whole 10 km cell against the USGS 3DEP index, once, which is acquiring the map rather than streaming position queries. The LOOKUP stays local: `src/trail_factors/cli/terrain.py` populates `dem_elevations` from tiles on disk and opens no network connection. Once a flat cluster has tiles, it gains grade; that does not change its role as the weather term's asset.

---

## 3. Layer architecture

Raw `.fit` files are the immutable source of truth. **Everything else is derived and rebuildable.** Formulas will change, and the whole history must be reprocessable against v2 of any calculation.

### Bronze — raw ingest
Parse FIT → DuckDB, exactly as recorded. No transformation, no cleaning.
- `record` messages (per-second)
- `session`, `lap`, `event` messages
- Developer fields if present

### Silver — derived / enriched
Most of the engineering value lives here.
- Smoothed altitude → grade → grade-adjusted pace
- Vertical speed (ft/hr ascent): the correct metric for power hiking, where pace means nothing
- Stride length = speed ÷ cadence
- HR-lag-corrected pairings (HR trails effort by ~20–30 s, so shift it before correlating)
- Segmentation into climbs / descents / flats
- Data-quality flags

### Gold — summaries
Per-run and per-segment rows. This is what the coaching layer reads. **Never let an LLM query bronze directly.**

### 3.1 Missing layers DEGRADE output, they do not forfeit the run

**A run with no matched terrain, no weather, or no Garmin self-evaluation is still a valid run with valid HR, pace, cadence and distance.** It takes part in every analysis that does not need the missing layer.

§15.4's exclusion rule is about **flagged (wrong) measurements**, not absent ones. It must not be read as forfeiting a run because one layer is unavailable.

| missing layer | what is lost | what remains fully usable |
|---|---|---|
| Garmin self-evaluation (§16.6) | session load for that activity | HR, pace, cadence, distance, drift (§6.7), decoupling (§6.4), all trends |
| weather (§8) | heat attribution, dew-point terms in §2 | everything else; the run still trains the model on terrain and pace |
| GPS **portions** | grade, GAP, segments **for those portions only** | HR, cadence, distance, moving/elapsed time, drift — for the whole run |
| segment match (§5) | same-hill comparison across runs | within-run matched terrain (§6.7), grade binning (§6.2) |
| DEM (§5a) | trustworthy elevation, absolute gain | every non-elevation analysis |
| running dynamics (§7) | GCT, vertical oscillation | HR, cadence, derived stride |

**The output says which layers were available.** A conclusion drawn without weather is not wrong. It is narrower, and it must say so.

**Time rules.**

- **Identity and joins are anchored in UTC.** `run_id`, all `timestamp` columns, and every time-range comparison use UTC.
- **Local time is for display and for calendar grouping only.** Grouping runs by day **must use the LOCAL date**, never the UTC date: anywhere west of Greenwich, a late-evening run crosses midnight UTC and would land on the wrong day. "A long run on a given day of the week" is a local-calendar fact, and so is time of day as an analysis dimension.
- Weather is the exception that proves the rule. It is fetched on **UTC** because that is how the archive is indexed (§8).

**Ingest rules.**

- **Pairing.** Two files belong to the same run when the sport matches, their start times fall within a tolerance window, **and** their time ranges overlap. Never require exact equality: a watch and a phone started and stopped together were measured a few seconds apart at each end (§9). A run never takes two files from the same `source`. `run_id` is created once, from the first file's UTC start, and stays stable when a second source is attached later.
- **`source` is derived from `file_id.manufacturer` in the file contents** (`'garmin'` / `'strava'`), never from the folder path or filename. Folders under the raw directory (`data/raw/` by default, overridable with `TRAIL_FACTORS_RAW_DIR`) are human organisation only and carry no meaning.
- **Content-hash dedup must not collapse the pair.** Every run is recorded twice, by the watch and by the phone, started and stopped together. The two files have different SHA-256s and are both kept as separate `activities` rows linked by `paired_activity_id`. Dedup exists to catch the *same file* ingested twice, not to pick a winner between devices.
- Filenames are preserved exactly as exported. The Garmin filename embeds the Garmin activity ID, a real external key back to Garmin Connect. It is worth keeping but it is not identity.

### 4.2 Validation layer — flags must be QUERYABLE

§15.4 requires that flagged measurements be excluded from coaching. That is only enforceable if the flags survive ingest, so validation writes rows rather than raising and vanishing.

**`validation_results`** is keyed on **all three grains**, because no single one covers every case:

- **`file_sha256` (NOT NULL)**: the only universal identity (§4). A file rejected as FATAL has no `activity_id`, and must not become silently invisible.
- **`activity_id` (nullable)**: most checks are file-scoped.
- **`run_id` (nullable)**: run-grain checks (two sources sharing a run, cross-source disagreement).

`value_observed` / `value_expected` are **DOUBLE**, so thresholds are queryable directly (`WHERE check_name='gps_coverage' AND value_observed < 0.5`).

**Severities.** FATAL = physically impossible: reject the file and write nothing. WARNING = suspicious: ingest it and record a flag §15.4 can exclude on. INFO = notable but fine.

**Layers ask different questions.** BRONZE checks parse fidelity: does the parsed data match the file? SILVER checks physical plausibility: are the derived values possible? GOLD checks historical plausibility: does this run resemble the others?

**Bronze and silver run inline at ingest; gold runs as a separate pass.** Bronze and silver decide whether a file is written at all, so they cannot be deferred. Gold compares a run against the rest of the corpus, so running it inline would make results depend on **ingest order**: the first file has no history. That is the same defect as the `runs.start_time_utc` order-dependence already fixed. Because `validation_results` is derived, all of it must be regenerable without re-ingesting.

**A single impossible sample must not reject a file.** A single GPS sample at ~20 m/s (44 mph) inside an otherwise usable run is a glitch: WARNING. What is FATAL is a **sustained** impossibility (>30 s), which points to a unit error rather than a glitch.

**The oracle is the cached API listing, not a CSV export:** `data/raw/garmin_api/activities_YYYY-MM-DD.json`. It is regenerable, carries fields a CSV lacks (`isElevationCorrected`, `movingDuration`, `startLatitude`, timezone), and needs no manual step that could silently go stale. Caching it to a dated file gives the CSV's only real advantage, an offline immutable snapshot, without the manual export. **It contains start coordinates, so it lives under the raw directory, which is gitignored with the rest of `data/`.**

**DECLARED STATUS PER FIELD: the summary is an oracle for some fields and a known-wrong reference for others.** Without this table, a future session reads any disagreement as a bug and "fixes" correct code to match a corrupted reference. Each comparable field in the Garmin summary carries one of three statuses:

| status | meaning | fields |
|---|---|---|
| **ORACLE** | we should match; **disagreement is OUR error** | run / walk / idle time (`splitSummaries[].duration`), per-lap time and distance, `lapCount`, total distance, elapsed and moving time |
| **COMPARISON ONLY** | **disagreement is expected and informative, not a failure** | total descent (`elevationLoss`), average and max cadence, average stride length, elevation **centre** (§5a-bis.4), `hrTimeInZone_*` — the last only when compared under the zone boundaries the file itself carries (§14e) |
| **DISCREDITED** | **do not use as a reference at all** | total ascent (§5a-bis: no true value, only smoothing conventions); average pace / average speed / max speed (§9: they inherit the grade-biased `speed_mps`); the elevation **profile** (§5a-bis.3: its shape is corrupted and it shows far more hills than the DEM does); **min and max elevation** (§5a-bis.4: amplitude inflated about 2x about a correct centre) |

**Verified against every Garmin activity in the corpus.** The ORACLE fields hold exactly: split **counts** all match, split **durations** match exactly via `total_timer_time`, split **distances** match to float32 precision (worst 0.02 m), and `lapCount` always matches. **That exactness is what makes the summary usable as an oracle at all.** A reference that merely agreed approximately could not distinguish our error from its own.

**A check that CANNOT FIRE is worse than no check, because it reads as passing.** `mean_speed_vs_corpus` originally used a fixed 2.5x factor against the corpus median. Observed mean speeds spanned well under 2x the median in either direction, so the bounds were unreachable: even a 1.75x factor fired zero times. It now uses **median ± 5×MAD**, which is robust and scales to the actual spread. **This was caught by asking whether the check could fire, not by it failing.** Interrogate every threshold that way: compute the observed distribution and confirm the bound sits within plausible reach.

**Insert by column NAME, not position.** Adding a column to `activities` broke the test suite twice through positional inserts. Row builders are dicts, and inserts name their columns.

### 4.1 Selection policy — download broadly, model strictly

The decision splits in two, and the halves have opposite answers.

**Download broadly.** Downloading a file that is later excluded costs one API call. *Not* downloading one that is later wanted costs another authentication fight with an endpoint that rate-limits (§9). Err toward pulling it.

**Model strictly.** Correctness lives in what enters an analysis, not in what sits in the raw directory. **Exclusions are enforced in the derived layers via `sport` / `sub_sport`, never by declining to ingest.** Raw is the immutable source of truth. A file withheld from ingest is a decision that cannot be revisited without going back to the API.

**GPS-less treadmill runs: ingest, exclude from anything pace-based.**
- **HR, cadence and duration are trustworthy.**
- **Speed, and therefore GAP, are NOT.** A watch with no foot pod (the FR245, for example) estimates treadmill speed from the **wrist accelerometer**, uncalibrated. This is a *stricter* exclusion than "no terrain": it rules out pace- and GAP-based analysis, not merely terrain-based analysis.
- They remain genuinely valuable as a **zero-grade anchor** for HR-vs-cadence and for HR drift at truly constant grade, which no outdoor run provides.

**Indoor cycling: ingest, never in the running HR baseline.**
- Cycling HR at a given effort differs from running HR. It must **never** enter the running HR baseline.
- It **does** enter the **fitness/date term as training load**. §14c requires that term across months of fitness change, and a block of combined bike-and-run training is load the model would otherwise be blind to.

**Skipped outright:** trivially short entries that carry no HR baseline value, no load value and no terrain (for example a pool entry of a few minutes).

### 4.3 Valid window — "was exercising", and why it is NOT GPS coverage

`activities.valid_start_utc` / `valid_end_utc` default to the recorded values. Analysis reads the `records_valid` view, which filters to the window. **Bronze is never deleted from** (§3).

**These two are orthogonal dimensions and MUST NOT be merged**, though they share a shape (the recorded window is wider than the valid one):

| | meaning | consequence |
|---|---|---|
| `coverage_fraction` | **was running, terrain unusable** | exclude from terrain analysis only; HR, cadence and pace stay valid |
| valid window | **was NOT running, nothing usable** | exclude entirely |

Collapsing them would, on one corpus run with poor GPS coverage, mark its first hour invalid and discard **an hour of legitimate HR and cadence** to solve a problem it does not have.

**The trigger is FROZEN DISTANCE + ZERO CADENCE + ZERO SPEED, sustained ≥120 s. Not heart rate.** On the real case that motivated this (a few minutes stationary at the end of a run), HR fell only partway and then wobbled back up. A rule waiting for HR to reach resting would have missed it outright. HR is corroboration; it must never be the trigger. Nor is impossible speed a trigger: that catches only driving.

**The 120 s threshold is validated against Garmin's own segmentation, not merely plausible.** Garmin's `rwd_stand` splits found **every** stop our detector found, with start times agreeing within **5 s**. Of all the `rwd_stand` splits in the corpus, the only ones longer than 120 s were exactly the ones we detected. Nothing was missed.

**Brief stands are common** (0–69 s: traffic lights, gates, retying a lace). Our detector discards them by design. **If stopped time becomes a tracked metric (see below), Garmin's splits capture a whole layer that our detector does not.**

**Known divergence, not a bug:** Garmin can split what we detect as a single stop into **two** stands a few seconds apart. Both readings are defensible (the athlete probably shifted position). Recorded rather than resolved.

**Threshold margins, measured rather than assumed.** On the confirmed stop, every sample reads cadence **exactly 0** and speed **exactly 0.00**: no jitter, so the margin is total rather than marginal. Across the corpus, a small fraction of near-zero-speed samples carry nonzero cadence; those are start/stop transitions, so requiring both may trim a few seconds at a stop's edges, which is immaterial against a 120 s minimum. **Frozen distance alone is NOT a usable independent check.** It also fires on GPS dropouts *while running*: most sustained frozen-distance blocks show running cadence at running speed. The conjunction is what identifies a stop.

**The drift threshold is rate-based, not absolute.** A stationary GPS still wanders: the real stop accumulated several metres over a few minutes, which an absolute 3 m cap missed. The bound is 0.10 m/s, an order of magnitude below walking.

**A stop DURING a run is training data. A stop AFTER a run is junk.** These are opposite cases and the design must not blur them.

**Mid-run stops are NEVER excluded from analysis, and never trimmed.** `records_valid` is a single pair of bounds, so it *cannot* remove a mid-run stop by construction. Reasons they must be kept:

- A session can contain a deliberate planned pause partway through (a stretching break, say). That is the session, not an interruption.
- Historical runs include comfort stops.
- An hour of running, ten minutes standing, then another hour is genuinely different from two hours continuous, **and the ten minutes is WHY the second hour looks different.** Removing it destroys the explanation.
- An ultra has pit stops: aid stations, shoe changes, taping a blister. **Elapsed time determines finish time.**

**Stops are recorded as STRUCTURE, in a `stops` table:** start, end, duration, position (leading/midrun/trailing), HR at stop, HR minimum, HR at resume, HR drop, and location.

**BOTH averages are real and neither corrects the other.**

| | formula | measures |
|---|---|---|
| moving pace | distance ÷ moving time | **fitness** |
| elapsed pace | distance ÷ elapsed time | **what a race day actually costs** |

Gold's `run_summary` view carries `elapsed_time_s`, `moving_time_s`, `total_stopped_s`, both speeds and `stop_count`. **Never report one pace without the other.**

**Stopped time is a TRAINABLE SKILL.** Ultra runners deliberately practise aid-station efficiency, so total stopped time and mean stop duration must be trackable over a training block like any other metric.

**Any analysis comparing runs must know whether a run contained stops.** Two runs of equal duration are not comparable if one contains ten minutes of standing. `run_summary.midrun_stop_count` exists for that filter.

Only **trailing and leading** stops narrow the valid window. A mid-run stop does not corrupt headline metrics anyway: `total_timer_time` excludes auto-paused time, which is why a run with a roughly nine-minute auto-paused gap partway through (not at the end) shows elapsed time exceeding timer time by exactly that gap.

**Moving time: ours, with Garmin as the oracle.** The FIT file carries no moving-time field (`time_standing` and `stand_count` are null). Our sum-of-moving-samples agrees with the API's `movingDuration` to a **median of under 2 s, with every file within 60 s**. Use ours, because it is transparent, reproducible and works offline, where Garmin's threshold is undocumented; keep theirs as a cross-check (§15). Compute it by **summing moving samples**, not as `elapsed − stopped`: auto-paused gaps contain no records, and subtracting produced a nine-minute error on one run before this was fixed.

**Schema objects must be ensured on every command, not only at ingest.** The `records_valid` view was once written into a module docstring rather than the DDL string, so it existed in no database at all, and the failure surfaced only when a command tried to use it. Two fixes: the view is defined at the END of the DDL in `src/trail_factors/ingest/schema.py` (it references `activities` and `records`), and **every CLI command that opens the database applies the DDL first**. `CREATE TABLE IF NOT EXISTS` and `CREATE OR REPLACE VIEW` are idempotent, so this is cheap. It does not backfill new columns on existing rows (a re-ingest still does that), but it guarantees the objects are present.

**Commands accept the Garmin activity id from the filename**, not only the internal SHA-256 prefix. Requiring a SQL lookup before every CLI command is friction on every invocation.

**Compute before writing, and write in a transaction.** The first `confirm-window` updated the window and *then* built its report. When the report failed, the UPDATE had already autocommitted and had silently narrowed a window the athlete never confirmed, which is exactly what this section forbids. Order matters as much as the rule.

**Distinguishing a deliberate pause from a forgotten stop: LOCATION discriminates, HR does not.**

On the trailing stop described above, HR fell and then **rose part of the way back**: non-monotonic, so "falls monotonically = ended, recovers then rises = resumed" does not separate the cases. Optical HR is also noisy when the wrist is still.

**Location does separate them.** The stop point sat within a few tens of metres of where several other runs in the same cluster **start**: the trailhead or parking area, not a point out on the route. A deliberate pause happens somewhere along the route; a forgotten stop happens where the car is. That case was judged a forgotten stop at the trailhead.

The general rule: compare the stop location against the *start* locations of other runs in the same cluster. A stop within ~100 m of a known start point is evidence of a forgotten stop. **This is evidence, not proof:** the athlete may genuinely rest at the trailhead before stopping the watch. The confirm step exists for exactly that residual ambiguity.

**The guarantee was weaker than advertised, and the gap was silent.** "Never auto-truncate without confirmation" held. **"Confirmation persists" did not.** `valid_start_utc` / `valid_end_utc` sat on `activities`, which ingest regenerates from raw, so the next rebuild discarded the confirmation and the system went back to reporting a pace inflated by the stationary tail, with no error. Confirmations now live in `window_confirmations`, keyed on `file_sha256`, which **ingest reads but never writes**; `activities.valid_*` is a derived projection, `COALESCE(confirmation, recorded)`. This is the pattern for all athlete-supplied state (CLAUDE.md): it lives outside the rebuild path, under `data/athlete/` (overridable with `TRAIL_FACTORS_ATHLETE_DIR`). That directory cannot be regenerated from anything and is **not** committed to git, so back it up yourself. Every athlete-state file follows the same pattern — notes, segment names, venues, ride blocks — not only window confirmations.

**Never FATAL. Never auto-truncate.** A false positive that silently truncated a real run would be worse than the original problem. Detection records a *suggested* window as a WARNING. `confirm-window <activity_id>` records the confirmation (in `window_confirmations.json`, through the same path as `confirm-file`), re-running detection from the raw file so the confirmed value is deterministic and rebuildable. **It does not change the activity on its own:** the window is applied at the next ingest, the only place a window is applied, so run `sync` afterwards. Until then the activity's records, splits and totals are still whole-file.

#### The bad-data loop — a rejected file names its own fix

The rule above covers a stationary tail on a file that ingested. **A file can also be REJECTED outright** — a drive home recorded as the end of a run reads as a sustained impossible speed, which is FATAL (§4.2), and a FATAL file writes no activity row. That is the case where a window matters most, and it has its own loop:

1. **THE REJECTION NAMES THE REASON AND THE NEXT STEP.** Both `sync` entry points (§8) report every file *this* sync failed to ingest, by Garmin id, with the validation message, and a sync with a rejection is **not a clean done**. The next step is a command, not advice: `uv run trail-factors confirm-file <garmin-id> --end 'YYYY-MM-DD HH:MM:SS'` (UTC; `--start` for a leading block; with no bound, the detector's suggested window is used). `rejected-files` lists every rejection still outstanding — with the suggested window, the file's own span and the exact command — because **a rejection persists across syncs and a job report does not**; a rejection that only appeared in the job that produced it is one nobody sees again.
2. **CONFIRMING IS BY GARMIN ID OR FILE SHA, NOT ACTIVITY ID.** A rejected file has no activity row, so `confirm-window` cannot resolve it. `confirm-file` validates the window against the file itself — inside the file's span, start before end — and writes `window_confirmations.json` in the athlete-state directory atomically, keeping earlier revisions with `source` and `confirmed_at`.
3. **THE NEXT SYNC INGESTS THE TRIMMED RUN.** Ingest walks every raw file without an activity row, so the rejected file is tried again, and **the confirmed window is applied BEFORE validation** — records, splits and session totals alike. Applying it afterwards would leave a file whose tail is a car ride rejected forever, with no confirmation able to rescue it. A split wholly outside the window is dropped; one straddling the edge is truncated, its distance **recomputed from the records, never scaled**; Garmin's whole-file totals are kept beside the windowed ones. A window that selects zero records raises: that is a wrong window, not an empty activity.

**THE ADVICE MUST BE TRUE OF THE FILE IT NAMES.** Each rule below exists because the plausible shortcut told an athlete something false about a run:

- **A CAR STRETCH AT AN EDGE IS CUTTABLE EVEN WHEN WALKING FOLLOWS IT.** A drive at the end of a recording, followed by several minutes of walking from the car, is still an edge: nothing on the far side is running. The detector proposes a window automatically only when the on-foot stretch beyond the drive is short; when it is longer, the advice still gives the `--end` (or `--start`) that removes the drive and **states its cost** — how much on-foot time beyond it the window discards — and the athlete chooses. A long walk after the car never makes the file "unrecoverable".
- **A STRETCH WITH A MID-RUN BLOCK IS NEVER CALLED FIXABLE BY AN EDGE WINDOW.** If an impossible-speed block sits between two stretches of running, no start/end window can remove it. Where the file also has an edge block, the edge window is not offered as the fix, and the advice says how many mid-run blocks would remain — otherwise the athlete confirms, re-syncs, and is handed the same rejection and the same command again. Where the detector could not place the stretch at all (no on-foot records, for instance a file with no cadence channel), the advice says that, not that the stretch is mid-run.
- **CONFIRMING ONE BOUND KEEPS THE OTHER.** `confirm-file` with only `--start` (or only `--end`) fills the other bound from the CURRENT confirmation for that file first, and from the detector only when there is none. An end the athlete confirmed earlier is never silently replaced by the detector's suggestion or dropped to no bound.
- **`rejected-files` COVERS THE WHOLE RAW TREE**, not only the Garmin download directory, so a rejected phone-app file is listed with its reason and command like any other. A Garmin id is shown only for a file that has one.
- **A RESCUED RUN IS ANNOUNCED.** The re-sync that ingests a previously rejected file at its confirmed window says so — it names the activity as now ingested at the confirmed window with its trimmed figures — and reports it, even though nothing new was downloaded. A rescue that only shows up as the absence of a REJECTED line is one the athlete never hears about.

**THE MCP `sync` JOB ALWAYS REPORTS REJECTIONS, AND RUNS WITHOUT TILES.** The terrain steps (dem, silver, match, recovery) need DEM tiles, and a fresh install has none. With no tiles, the job skips those steps with a note saying why instead of failing on them, and the rejection report is built from the ingest step's result whatever a later step does, so a terrain failure can never hide a rejected file or its command.

**CONFIRMING A WINDOW IS CLI-ONLY.** No MCP tool writes a confirmation, so the MCP `sync` job's rejection names the CLI command above. An MCP tool would sit inside the §12.6 boundary — it is athlete input written only to the athlete-state directory — and would need to be a thin wrapper over the same `confirm_file` path (so the two cannot disagree about what a valid window is), report a bad window as a retryable `rejected` with nothing written, and leave the re-ingest to the next `sync` job, which holds the write lock. It is not built in this release.

**A genuinely corrupt file has no masking mechanism** (one with corrupt timestamps, §9, is the standing example): no window rescues it, and it stays in `rejected-files`. **Confirmations are per-user state** under the athlete-state directory, gitignored with the rest of `data/`.

### Side tables
- `weather`: auto-fetched on ingest
- `annotations`: **free-text run notes ONLY.** Nothing downstream parses them, so a structured value written as prose is one no validator ever saw. Structured per-run context (`run_type` and the rest) is not collected in this build (§16.6), and notes must not become that channel by the back door.
- `segments` / `segment_efforts`: the terrain identity library

---

## 4. Data model (draft)

This is the original design sketch. Where it and the code differ, the DDL in `src/trail_factors/ingest/schema.py` is authoritative. In particular, segments were later redefined as direction-free places with directed efforts (§5), and the built quality flags are the five named in §15.4.

```
ingest_ledger               -- one row per FILE; the dedup authority
  file_sha256 (PK)          -- identity is file CONTENT, never the filename
  original_filename         current_path                source
  device_serial             device_model                activity_start_utc
  local_tz_offset           file_bytes                  ingested_at
  parse_status

runs                        -- one row per OUTING; the grain coaching reads
  run_id (PK)               -- UTC-anchored, format YYYY-MM-DDTHHMMZ
  start_time_utc            local_start_display        -- display ONLY
  local_tz                  sport / sub_sport          created_at
  -- Volume aggregates are per-RUN. Never sum volume over activities:
  -- one outing has 1-2 source files and would double-count (§14b).
  -- Source count is derived by counting activities, not stored.

activities                  -- one row per SOURCE FILE beneath a run
  activity_id (PK)          run_id (FK -> runs)        file_hash (dedup key)
  source_file
  start_time_utc            local_tz                    sport / sub_sport
  device                    total_distance_m            total_ascent_m
  duration_s                pipeline_version
  source                    -- garmin_watch | strava_phone
  -- No is_primary_source flag: which device is "primary" is a property of
  -- the file's RELATIONSHIP to others, not of the file, and would have to be
  -- rewritten whenever a second source is ingested. §14b also forbids
  -- anointing either device as authoritative. Source selection is a
  -- documented estimator choice in gold.
  sampling_regime           -- detected per-file from median record gap (§9),
                            -- NOT inferred from the file date
  median_gap_s              -- the measured gap; evidence for sampling_regime

records                     -- per-second, the big table
  activity_id (FK)          timestamp                   elapsed_s
  lat / lon                 altitude_device_m (unreliable) distance_m
  altitude_source           -- gps | barometric | dem_suspected | unknown (§14d)
  heart_rate                speed_mps                   gps_accuracy
  cadence_raw_rpm           -- one-leg, exactly as recorded; NOT steps/min
  fractional_cadence        -- steps/min is DERIVED IN SILVER (§9's formula).
                            -- Bronze stores raw (§3).
  -- gps_accuracy: nullable; Strava only. See §9 -- do NOT build flags on it
  temperature_c (IGNORE -- reads body heat, not air)
  -- Running dynamics: ABSENT on devices without the channel; nullable,
  -- populated where the device records them:
  vertical_oscillation      stance_time                 stance_time_pct
  vertical_ratio            step_length                 power

records_enriched            -- silver, materialized, rebuildable
  activity_id, elapsed_s (PK)
  altitude_dem_m            -- from DEM lookup, NOT from watch (see §5a)
  altitude_smooth_m         grade_pct                   gap_mps
  vertical_speed_m_hr       stride_length_m (derived)   hr_shifted
  segment_state             -- climb | descent | flat
  quality_flags             -- draft names; the built set is in §15.4
  calc_version

laps                        -- from FIT; free segmentation for track work
  activity_id, lap_index, start_time, duration_s, distance_m,
  avg_hr, max_hr, avg_cadence, avg_speed, trigger (manual|auto|distance)

weather
  activity_id (FK)          fetched_at                  source
  temp_f, dew_point_f, humidity_pct, wind_mph, wind_dir,
  precip_mm, precip_prev_24h_mm, cloud_cover, is_final
  -- fetched at the start location (rounded, §8) and actual start hour,
  -- interpolated

annotations                 -- FREE TEXT ONLY. Nothing parses it.
                            -- Fields an earlier draft listed here -- run_type,
                            -- per-channel effort, sleep, trail surface, body
                            -- scales -- are NOT collected in this build
                            -- (§16.6). Effort comes from Garmin's own
                            -- self-evaluation where the watch asks for one.
  run_id (FK)               note (free text)            noted_at
  -- Durable home is data/athlete/notes.json; restored on every schema ensure
  -- like window_confirmations. Ingest never writes to it.
  -- Holds what no field records: "tripped at 47:00", "stopped for gate".

segments                    -- the hill library
  segment_id (PK)           name (optional, user-assigned)
  canonical_polyline        -- 50 resampled points
  direction                 -- draft only: superseded by §5, where a segment
                            -- is direction-free and its EFFORTS are directed
  length_m, gain_ft, mean_grade_pct, max_grade_pct
  first_seen, times_run

segment_efforts             -- makes "show me that hill over 3 months" one query
  effort_id (PK)            segment_id (FK)             activity_id (FK)
  lap_index (nullable)      start_elapsed_s             duration_s
  avg_hr, max_hr, hr_at_start, hr_at_end
  avg_cadence, avg_gap, vertical_speed_m_hr
  recovery_tau_s            -- HR decay constant after the effort
  match_confidence
```

---

## 5. Terrain identity — the hill matcher

Requirement: an athlete who runs the same hilly loop many times — say an 8-hill loop ten times, eighty hill efforts — must be able to know that **hill 3 on lap 5 is hill 3 on lap 1, three months ago.**

**Do not** try to match arbitrary GPS traces to each other. Build a library instead. The original plan was:

1. **Detect climbs within a run** using a hysteresis segmenter (enter climb state above +3% sustained, exit below +1%, minimum 30 m length). Not a per-point threshold — that produces thousands of one-second "climbs."
2. Each detected climb yields a polyline. **Resample to 50 evenly-spaced points** by cumulative distance.
3. **Prefilter candidates** from `segments`: start point within ~30 m, end point within ~30 m, total length within ±15%, same direction.
4. **Confirm** with discrete Fréchet distance (or mean point-to-point distance after alignment) on the resampled polylines. Threshold ~20 m.
5. Match → assign existing `segment_id`. No match → create new segment.

The subsections below record how that plan was revised by measurement: the library is keyed on **prominence peaks** (§5.0), the unit is a **slope** rather than a hill (§5.4), segments are **direction-free** (below), and the peak is the anchor because it is the most repeatable feature (§5.3).

### 5.0 Build the library on PROMINENCE, not on hysteresis climbs

Measured on one run of the home trail, DEM elevation, 1-D topographic prominence (peak minus the higher of its two key saddles), at prominence thresholds from 2 m to 15 m and smoothing windows of 0, 25 and 50 m.

**The distribution has a natural break.** A handful of features have prominences of roughly 10–16 m; below them the next values drop to under 5 m, and nothing sits in between. Thresholds of 5 m and 8 m therefore return the **same set of hills**, and they do so at every smoothing window from 0 to 50 m. `PROMINENCE_MIN_M = 8.0` (`src/trail_factors/transform/silver.py`) sits inside that gap.

**The prominence count matched the athlete's own count of hills on the loop** — an algorithm that knew nothing about the description agreed with it, and the agreement was stable across 0–50 m of smoothing.

**The §5 hysteresis segmenter found roughly four times as many climbs on the same run.** It is not wrong — those are all stretches genuinely above +3% — but it **fragments each hill**: a hill with a bench, a flat shoulder or a brief dip becomes two or three climbs. The hysteresis count is a description of the *gradient profile*; the prominence count is a description of the *terrain*.

**Argument for prominence as the basis of the segment library:**

- **It matches how the athlete describes the trail**, which is the requirement §5 actually states — *"know that hill 3 on lap 5 is hill 3 on lap 1."* There is no stable "climb 17 of 32"; there is a stable hill 3.
- **It is threshold-robust.** The 5 m and 8 m thresholds return the same hills, and so does 0–50 m of smoothing. The hysteresis count moves with every parameter.
- **It is smoothing-robust in the one way that matters.** §5a-bis shows the *gain* number depends entirely on the smoothing window; the *hill count* does not. **Prominence extracts the part of the elevation signal that is stable.**
- **Prominence is a property of terrain, not of a traversal.** A hill has the same prominence run uphill, downhill, fast or walked — which is exactly what a segment library needs, whereas a climb's length and mean grade depend on where the runner happened to cross +3%.

**The hysteresis segmenter is retained for §4 climb/descent state**, which is a per-record label and a different job. **Segments are hills; states are stretches.** (§5.4 later refines the segment unit from a hill to a slope; the division of labour stays.)

**Open question:** the prominence threshold itself. 5 m and 8 m agree on the home trail because of the gap in its distribution. A flatter trail may have no such gap, and the threshold would then need to be set per-cluster or derived from the distribution rather than fixed.

**Never query AllTrails, OSM, or any trail database.** The §5a route-trace reasoning applies identically: a per-coordinate lookup to an external service is a route trace regardless of who receives it. **Trail identity comes from the athlete's own repeated traversals.** If a run is somewhere new, the segment library simply has no match — which is correct, not a gap to fill from a third party.

**THE RULE IS ABOUT THREE THINGS, NOT ABOUT "EXTERNAL DATA".** Stated precisely so it can be applied rather than guessed at, the prohibition is:

1. **No crowd-sourced trail geometry** — someone else's line for the ground the athlete runs.
2. **No per-coordinate lookups** — a query stream keyed on where the athlete was, which is a route trace whoever receives it.
3. **No matching against external lines** — nothing outside the athlete's own traversals may enter segment identity.

**A RACE COURSE PROFILE IS NONE OF THOSE, AND IS PERMITTED.** It is a labelled reference object for a specific target event: **imported once, stored separately, and NEVER joined to the segment library or used for matching.** It is not keyed on the athlete's position, it is not a substitute for their own traversals, and importing it emits nothing about where they run.

**This is the DEM-tile category, not the trail-database category** — the same distinction §5a already draws. USGS publishing elevation for a location is public data the athlete downloads in bulk and reads locally; a per-coordinate elevation *service* is a route trace. A course profile sits on the first side of that line for exactly the same reason: **bulk, one-off, local, and about the event rather than about the athlete.**

**Storage:** separate from `segments` and `slope_traversals`, with no foreign key between them and no code path that matches one to the other. A course profile is a **comparison target** — what the race demands — against which the athlete's own climb signature (§6.5) can be read. It must never become terrain the matcher can assign an effort to.

*Why this needed saying:* the original rule was written to stop trail geometry leaking into segment identity, and read literally it also forbade knowing what a target race looks like — which serves no privacy purpose and costs the athlete the most useful comparison a training block can have.

**Known constraints:**
- Consumer GPS is ±5–10 m open sky, worse under canopy. Tolerances must be generous.
- To compare traversals ("lap 5 vs lap 1"), align by **normalized distance along the segment**, not by absolute GPS position.
- ~~An uphill and its reverse descent are **different segments**. Direction is part of identity.~~ **AMENDED — segments are DIRECTION-FREE; direction belongs to the EFFORT.**
  - **`segments` are PLACES.** A peak location, a peak elevation, a prominence. A loop with eight hills has eight of them, not sixteen.
  - **`segment_efforts` are traversals — DIRECTED.** Each carries `direction` alongside HR, τ and vertical speed. This is already §4's schema shape.
  - **WHY THIS IS A CORRECTION, NOT A REVERSAL.** The original wording belongs to the **polyline design** — when a segment was a *climb polyline*, an uphill and its reverse genuinely were different objects, because a polyline's length and mean grade depend on where the runner crossed +3%. **§5.0 replaced polylines with prominence hills, and prominence is direction-free by construction** — a hill has the same prominence run up, down, fast or walked. The sentence survived the design change that invalidated its premise. **It is not a new opinion about direction; it is the polyline assumption left standing after the polyline was removed.**
  - **Two independent arguments converge here** (§15's standard for a real finding): §5.0's is *a priori* — prominence is a property of terrain, not of a traversal. §5.3's is *empirical* — peaks reproduce about 4x better than valleys (SD 0.54 m vs 2.20 m) across repeated runs of the same trail. **The segment anchors on the peak, never on a saddle or a minimum.**
  - **What this buys:** "hill 3 on lap 5" names one place unambiguously. Doubling the library would make "hill 3" name two, which is precisely what §5 wanted to avoid.
- The segment library is not optional. It is what makes elevation trustworthy — see §5a.

### 5.4 The prominence-hill unit was a HOME-TRAIL ARTIFACT — the unit is a SLOPE

**Recorded so nobody re-derives "a hill is up-then-down" from one trail's data.**

**A prominence hill is up-then-down BY DEFINITION** — a peak with a col either side. §5.0 adopted it and it worked: prominence found clean hills on the home trail. **But it worked because of what that trail is, not because of what hills are.** Measured over every prominence hill in the corpus there, the flat share *inside* a hill (col → peak → col) averaged under a fifth of its span, only a few percent of hills were more than 30% flat, and the whole trail contained a single flat stretch of a few hundred metres and nothing longer.

**On rolling terrain like that, most of every hill's span is actively climbing or descending, and there is no long flat at all.** An ascent and a descent genuinely are adjacent, so a peak-with-cols is a faithful unit.

**It is not faithful anywhere else.** On a trail with a long sustained climb that tops out onto a plateau, **there is no col to key on**. On a mountain race course, ascents and descents can be separated by **miles** of flat, and a peak-with-cols unit would glue them into one feature, attributing terrain the athlete never climbed. **The ascent and the descent are separate features that happen, on rollers, to be adjacent.**

**THE UNIT IS A SLOPE: one monotonic stretch of terrain, keyed on its extent and traversable either way.** Running a face up and running it down are two **efforts** on **one** segment. That is deliberate and it is what makes the §6.3 τ comparison like-for-like — recovery after climbing slope X against recovery after descending **the same slope X**. Splitting them into two segments would lose exactly that comparison.

**This is the same failure class as §5's direction rule**: a property of the available sample mistaken for a property of the domain, then encoded as a design rule. **Both were caught only by asking what the rule would do on terrain not yet run.**

**IDENTITY IS `(place, side)`, AND THIS DOES NOT REINTRODUCE UP-THEN-DOWN.**

A slope's own endpoints are too noisy to key on. Measured over dozens of ascent/descent pairs of the same face within a single run, endpoint positions scatter by **roughly 300–400 m** while their elevations agree to about **half a metre (median)** — a hysteresis boundary is where a threshold was crossed, not a feature of the ground, and **several boundary walks crossed into the neighbouring feature on shallow cols**. So identity is inherited from the **peak**, which is the one thing the matcher already validates: §5.3 measured peaks at **SD 0.54 m** against cols at **SD 2.20 m**. **The col is derived from the hysteresis walk and never keyed on**, which is also why the consistency check tests the place elevation and not the col — flagging col scatter would be a check firing on the truth.

**The unit is still a slope, not a hill.** Peak keying attaches a slope to a summit *when there is one*; it does not require one on both sides:
- **A summit followed by a flat has ONE slope**, because **hysteresis never enters on the flat side**. There is no descent to pair with, and that is correct rather than a gap — it is the climb-onto-a-plateau case.
- **The synthetic climb → 3 km flat → descent case still yields TWO segments belonging to DIFFERENT places**, because the two high ends sit at opposite ends of the flat, 3 km apart, and can never key to the same peak.

Both are pinned in `tests/test_slopes.py::TestPeakKeyingDoesNotReintroduceUpThenDown`.

**THE FIRST REAL VALIDATION OF SLOPE BOUNDARIES IS THE FIRST RUN ON TERRAIN WITH FLATS BETWEEN FEATURES** — a sustained climb onto a plateau, or a course with long flats between ascents and descents. A rolling trail cannot test the boundary rule — it has no flats between features — so until such a run exists, **the synthetic profiles in `tests/test_slopes.py` are the ONLY test, and the rolling home trail is a REGRESSION rather than a validation.** Do not read a passing result on rolling terrain as evidence that the boundary rule is right.

### 5.1 Route variability — a run is a SET OF TRAVERSALS, not a route

Real usage breaks every fixed-route assumption. **These patterns rule out any design that assumes a fixed route or a canonical ordering.**

- A trail can have **several entrances**, and a run covers **some subset** of its segments, not a fixed route.
- The trail is sometimes run **backwards**. Direction is a property of the **effort**, not of segment identity (see the amendment above) — and reverse traversals must be handled, not assumed away.
- Runs sometimes **start mid-trail**.
- A run may include an **out-and-back detour off the trail**, appearing at the **start or the middle**. The matcher should see this as a segment, because it is one; but it **must be identifiable** so it does not contaminate route-level elevation comparisons.
- Recording may **start after some walking**.
- Even at the same entrance, the **start point varies by a few metres**.

**CONSEQUENCE: matching must be by GPS position.** There is no canonical sequence to align against, no fixed start, and no guarantee that two runs of "the same trail" share a first segment, a direction, or an entrance. Any design keyed on *distance from run start*, on route templates, or on segment ordering is unusable.

**This does NOT invalidate §5a step 3.** The two distances are different quantities:

- **Distance from run start** — unusable. It depends on where recording began, which varies by entrance, by mid-trail starts, and by pre-start walking.
- **Distance along segment** — fine, and still the drift-immune elevation lookup. It is measured **after** GPS has identified *which* segment this is, and is anchored to the segment's own endpoints. Where the recording started is irrelevant to it.

GPS position identifies the segment; distance-along-segment then positions you within it.

### 5.2 First matcher target — a flat, open-sky cluster

Repeated terrain is grouped into **clusters**: named training areas declared in `config/athlete.yaml` under `clusters` (a centre and a radius each; see `config/athlete.example.yaml`). A run whose median position falls outside every cluster is labelled `OTHER`, never by a coordinate. A typical account has at least two kinds: a **hilly home trail** and a **flat urban cluster** (paved paths, open sky).

**Caveat on cluster membership:** if membership is taken from the Garmin API's `startLatitude`, note that §9 shows it is the first *known* position rather than the start — a run whose GPS fixed late can be filed under the wrong cluster. Confirm membership from the ingested trace, not the listing.

**Count the matcher set at the per-portion grain (§14c), not per activity.** A partly-covered activity can still contribute a long, clean positioned block; the whole-activity coverage rule excludes it, and a short fragment should be excluded regardless. Recount the matcher set on positioned blocks before concluding it is too thin — and expect the whole-activity count to be noticeably smaller than the raw count of runs in the cluster.

**Validate the matcher on the flat, open-sky cluster first.** Flat, paved and open-sky is the best-case GPS data in the account. Testing on a wooded or creek-bed trail first would conflate matcher bugs with GPS error, leaving no way to tell which was failing. The same cluster is also the elevation-noise-floor test (§5a-bis), so one set of data serves both.

---

### 5.3 Matcher error budget — MEASURED, not assumed

Measured across the full-coverage runs of the home trail's main route (`coverage_fraction = 1.00`, with an upper bound on DEM max elevation to exclude runs that left the main route). Reproduce with `min/max(altitude_dem_m)` per `activity_id` from `dem_elevations`, joined to `activities.coverage_fraction`.

| quantity | value |
|---|---|
| DEM **max** elevation, run to run | **SD 0.54 m**, full spread under 2 m |
| DEM **min** elevation, run to run | **SD 2.20 m**, full spread nearly 9 m |
| within-cluster peak elevation | SD roughly 0.1–0.7 m (sub-metre on every cluster; re-derive, since it moves as the corpus and the dedup rules change) |

**PEAKS ARE ABOUT 4x MORE REPEATABLE THAN VALLEYS: max SD 0.54 m against min SD 2.20 m.** A summit is a sharp feature the athlete must cross, so every traversal samples the same point; the low point is wherever the turnaround happened to fall, so it moves with the route choice.

**This is an independent argument for keying the matcher on peaks and never on saddles or minima** — and it arrives from a **different direction than §5.0's prominence argument**. §5.0 reasoned from *definition* (prominence is a property of terrain, not of a traversal); this reasons from *measurement* (peaks reproduce about 4x better in the actual corpus). Two independent lines converging on the same design choice is much stronger evidence than either alone, and per §15 that convergence is only meaningful because the arguments are genuinely independent — one is a priori, one is empirical.

**Consequence for thresholds:** a peak-elevation match gate of ±2 m is ~4 SD of the measured run-to-run spread — wide enough to absorb real variation, and far tighter than the ~10 m separation between distinct hills on the home trail. **The gate is fitted to the measurement, not picked** — and, like any threshold fitted on one trail, it is a property of that trail until other terrain confirms it.

**Garmin's own min/max elevation is NOT admissible here** — see §5a-bis.4. Its peak-to-peak error is ~10 m, roughly **20x the DEM's run-to-run SD**; substituting it would destroy the discriminator.

## 5a. Elevation — DEM lookup, not the watch

**Device constraint:** a watch without a barometric altimeter (the Forerunner 245 Music, which recorded much of the corpus, is one) writes GPS-derived altitude into the FIT file. Vertical is GPS's weakest axis — commonly 10–15% off on total gain, and inconsistently so. Per-second GPS altitude is not usable for grade.

**Solution: ignore the watch's altitude entirely. Look elevation up from a digital elevation model.**

- **USGS 3DEP** — free, national coverage at 1/3 arc-second (~10 m), and lidar-derived 1 m coverage over much of the US. It can be queried through the National Map elevation point service, or the tile can be downloaded once and queried locally (faster, offline, no rate limits — preferred, and required by §5a.1).
- **Open-Meteo Elevation API** — free fallback, but SRTM 90 m is too coarse for hill grades.

**Why this is better than a barometer:**
- **Deterministic.** The same trail returns the identical profile every traversal. No drift, no pressure error, no calibration.
- Terrain variation drops out of cross-traversal comparison completely. A barometric watch cannot offer this.
- Reprocessable: improve the DEM or the lookup, rerun the whole history.

Tiles live in the DEM directory — `data/dem/` by default, overridable with `TRAIL_FACTORS_DEM_DIR` — which is gitignored along with the rest of `data/`. Dropping a `.tif` there is the whole installation step.

### 5a.1 REQUIREMENT: the lookup must be LOCAL

**No per-coordinate query goes to any external service, government or commercial.** The tile is downloaded once and every lookup happens on this machine.

**The reason is privacy, and it is stronger than the speed argument §5a gives.** A public agency having published the elevation of a location is not the same as that agency receiving a query stream of an exact route, in order, with timestamps. **The elevation is public; the route is not.** A server log of tens of thousands of sequential points *is* a GPS trace of where the athlete runs and where they start from. Downloading a public map tile reveals only that someone wanted a map of a region.

**`getSamples` was tested and is REJECTED on this basis, so that it is not re-proposed.** The USGS 3DEP ImageServer `getSamples` endpoint works well: no API key, batches of 500 points in ~5 s, **1 metre** resolution, zero NoData. It is rejected purely because it would send the route. During the acceptance test a few hundred points from one cluster were sent; that was a one-off and it stops there.

**Dependency cost: ~38 MB, not the ~25 MB first estimated.**

| package | wheel |
|---|---|
| `numpy` | 5.1 MB |
| `pyproj` | 4.8 MB |
| `tifffile` | small |
| `imagecodecs` | 13.4 MB |

**`tifffile` does NOT decode LZW natively — it delegates to `imagecodecs`.** That was an incorrect claim in the original library comparison, and anyone re-evaluating the stack would hit it again. USGS 3DEP GeoTIFFs are LZW-compressed, internally tiled 256×256, float32.

`pyproj` is used rather than hand-rolled UTM maths deliberately: twenty lines of projection arithmetic is easy to get subtly wrong, and **a systematic coordinate error would corrupt every elevation in a way that still looks plausible** — the worst failure mode in a project built on residuals.

#### 5a.1-bis A TILE FETCH IS NOT A QUERY STREAM — `fetch-tile`

**The rule above forbids a per-coordinate query. It does not forbid acquiring the map.** §5a.1 rejected `getSamples` because it would send the route: every point, in order, with timestamps. Asking *"which public map product covers this 10 km square"* is a different act, and the difference is not a matter of degree:

| | `getSamples` (REJECTED) | `fetch-tile` (PERMITTED) |
|---|---|---|
| unit | one exact position | one **10 km cell** |
| volume | one request per record | one request per **cell, ever** |
| sequence | ordered, timestamped — a route | a set of regions, unordered |
| what it reveals | where the athlete runs, and when | that someone wanted a map |

A 10 km square is two orders of magnitude coarser than a position — **coarser than the ~1.1 km rounding §8 already applies** to a weather coordinate before it leaves the machine, which is an accepted disclosure. And a tile is fetched **once** and answers every run on that ground forever, so there is no ongoing stream to correlate against anything.

**The lookup itself did not change and must not.** `dem.py` opens no socket; acquisition lives in `dem_fetch.py` and nothing on the lookup path imports it. A test asserts both directions, because "these two things stay separate" is a convention until something enforces it.

**What IS disclosed, stated plainly:** the SET of cells fetched is a coarse list of the regions the athlete runs in. That is the county, not the trail, and it is the price of having terrain at all.

**Nothing outside the United States is fetchable, structurally, not by policy:** the only endpoint is the 3DEP index, 3DEP holds US data only, downloads are restricted to the USGS host by allowlist — **including after a redirect: a redirect to a host off the allowlist is refused, not followed** — and a cell outside US territory is refused before a request is sent. **`--missing` reads the refusal list and nothing else** — it cannot widen to "tiles near where you run", which would be a different disclosure than the one agreed here. A bulk pull over 1.5 GB is refused without `--yes`.

**THE FILENAME IS RESOLVED, NEVER CONSTRUCTED.** A 3DEP filename ends in an acquisition project name (of the form `<STATE>_<Project>_<Year>`), and that is not a function of position. Worse, the x/y cell is only meaningful in the CRS it was computed in: **a cell whose ground lies in another UTM zone resolves to a different x/y entirely**, because USGS names each tile in its own zone. A constructed filename works in the home zone and 404s everywhere else. `fetch-tile` queries the 3DEP index with the cell's **ground** as a WGS84 bbox and takes what actually covers it. The filename printed in a coverage gap (§16.2's `terrain_coverage`) is therefore labelled a guess — `filename_is_resolved: False`, its project suffix taken from the tiles on disk or `dem_tile_suffix_guess` in `config/athlete.yaml` — and carries the `fetch_command` that resolves it properly. That is with `projection_crs` configured. **With it unset** (the default), the cell is labelled in the run's DERIVED UTM zone (§5a.1-quinquies), `filename` is null — no guess is made — and `fetch_command` carries the zone: `fetch-tile <cell> --zone NN`.

**THE COMMAND IS `fetch-tile <cell> [--zone NN]`.** A cell name such as `x40y500` is only meaningful in the UTM zone it was computed in. With `projection_crs` configured, that zone is implied and `--zone` is not needed. With it unset, `--zone` is REQUIRED, and a bare cell name is refused with an error naming the flag, before any request is sent. Copy the `fetch_command` the coverage report prints rather than building one. **A cell with no USGS grid gets no fetch command at all:** a southern-hemisphere zone, or a northern zone above 23, has no NAD83 / UTM grid in which a USGS tile could be named, so `fetch_command` is null with a reason saying so. A command that dropped the hemisphere would read the cell in the northern grid, where the same numbers can name real ground inside the US — a tile for somewhere else entirely.

**Where several acquisitions cover one cell, the LARGEST file is chosen.** At fixed 10012×10012 dimensions a small file is an EDGE tile that is mostly NoData and compresses to a fraction of a full one — a cell can offer a small edge product and a full-size one, and the small one would populate almost nothing while looking like a success. Every candidate is printed, so the choice is visible rather than silent.

**A download is verified before it is kept**, and tag checks alone are not enough: a TIFF's header sits at the front of the file, so a 450 MB download that dies at 300 MB still opens, still reports its CRS and still lands on the right ground. Verification therefore checks the byte count (against the server's `Content-Length`; the index's declared size can be stale when USGS re-writes an object, so a disagreement with the index is reported rather than fatal), checks that the last byte any strip claims exists, checks the CRS key is present, and checks the tile's own georeferencing lands on the cell that was asked for. The file is written to `.part` and `os.replace`d into position only after all checks pass, so a half-arrived tile is never visible to `TileSet`.

**`retile` runs automatically** for the activities a new tile newly covers, and inherits §4.1's running-only HOLD — otherwise a fetch would end by walking newly covered cycling activities into the segment library, deciding how a bike ride is used by not thinking about it.

#### 5a.1-ter THE TILE NAMING CONVENTION WAS WRONG IN y (corrected)

**USGS names a 1 m tile by its NORTH-west corner; `tile_grid_id` returns the SOUTH-west corner** that a floor division naturally produces. The two were treated as the same thing, so **every missing-tile filename the project printed before the fix named the cell one 10 km step too far south.**

Measured against a tile on disk: its `ModelTiepointTag` northing and its 10012 px height show that the y in the filename is the TOP edge of the ground it covers. The 3DEP index confirms it independently: a cell the floor-division grid calls `yN` is served by a file named `y(N+1)`, same project.

**The GROUND was always right** — `easting_range` / `northing_range` in `coverage()` are computed from the floor cell and are correct, which is why no computed elevation was affected. Only the NAME was wrong, and the name was only ever printed for a human to act on. It is pinned in `tests/test_fetch_tile.py` as a measurement against a real tile, not as a comment.

**Lesson:** a value that is only ever displayed is still a value. Nothing downstream consumed the filename, so nothing downstream could catch it; it took fetching a tile by that name to show it.

#### 5a.1-quater PER-TILE CRS

`DemTile` originally applied one module-level CRS to every tile, which is true for tiles in the home UTM zone and false everywhere else. For a run in another UTM zone, a tile from that zone read under the home zone's CRS would place every pixel hundreds of kilometres from where it belongs. **Every USGS GeoTIFF states its own EPSG in `ProjectedCSTypeGeoKey`**, so each tile now reads its own, and the run's projection (§5a.1-quinquies) remains only as the CRS of the reporting grid and of the planar geometry in silver and the matcher.

#### 5a.1-quinquies THE PROJECTION: configured wins, otherwise derived per run — and the zone is part of identity

**An explicit `projection_crs` in `config/athlete.yaml` wins**, everywhere, and with it set behaviour and segment ids are unchanged. **When it is unset, each run's UTM zone is derived from that run's own positions** (WGS84 UTM, `EPSG:326NN` north / `EPSG:327NN` south), and the output says so rather than presenting the derived CRS as configured: `"projection": {"crs": "EPSG:326NN", "basis": "derived from this run's position: UTM zone NNN"}`. A configured value carries its own basis, so a reader can always tell a choice from an inference.

**THE ZONE BECOMES PART OF SEGMENT IDENTITY when it is derived.** Segment matching compares planar positions across runs, and each UTM zone is its own coordinate frame: two runs in different zones can carry identical easting/northing for places hundreds of kilometres apart. `MATCH_RADIUS_M` (§12.3) cannot see that — it would compare numbers from two frames as if they were one. So a place is keyed on its zone as well as its position, and **places in different zones never match**, whatever their coordinates, elevation or bearing. That is the same structural guarantee §14p records for a new trail, extended across frames. Under a configured `projection_crs` every run shares one frame and nothing about identity changes.

**MISSING TILES ARE NAMED IN THE SAME DERIVED ZONE.** With `projection_crs` unset, `coverage()` labels each missing 10 km cell in the grid of the run's derived zone, the same zone its terrain is projected in, so the two cannot disagree. `filename` is null (no suffix guess is made without a configured grid), and `fetch_command` carries `--zone NN`; `fetch-tile` refuses a bare cell name in this configuration (§5a.1-bis). Where the derived zone has no USGS grid (southern hemisphere, or a northern zone above 23) there is no fetch command.

**KNOWN LIMITATION — ONE SUMMIT, TWO ZONES.** The derived zone is the median of each run's OWN positions. Two runs over the same summit whose medians fall either side of a zone line (an out-and-back of a different length or direction on a trail a few kilometres from one) are matched in different frames, so they never match and that one slope forms **two segments**. Nothing in `list_segments` or `segment_history` says why. Setting `projection_crs` removes it, since every run then shares one frame.

### 5a.2 Home trail first; the flat cluster deferred, not skipped

**Tile:** one USGS 1 m lidar tile (several hundred MB, UTM projection) covering the home trail. It was verified to contain **every** coordinate in that cluster with a margin — coverage is checked against every point, not the cluster centroid, and a point outside the tile **raises** rather than returning NoData that would be silently smoothed over.

**Why the hilly trail first is not just about size.** The flat urban cluster is a loop with open sky and strong GPS. **It is where the DEM buys the LEAST** — little vertical to resolve, cleanest coordinates — **while costing the most**: several tiles, because a long loop straddles tile boundaries. The home trail is the opposite: poorer sky view, real grade, and the terrain that matters most for trail training. **It is both the higher-value case and the harder test. If the lookup works there, the flat cluster is trivial.**

**The flat cluster will eventually be wanted** — it can carry a large share of total volume, and flat ground is exactly what the weather term needs (§2.6). **Deferring costs nothing:** bronze keeps every coordinate and reprocessing is a first-class operation (§11).

**Testable prediction for when the flat cluster is added:** on flat ground with a clean fix, **DEM and smoothed GPS altitude should agree more closely than on the hilly trail.** If they disagree there, the problem is the lookup, not the GPS.

**The catch and its fix:** GPS horizontal error propagates into the lookup. Drift 8 m onto a side slope and you get that slope's elevation.

Mitigation, in order of implementation:
1. Smooth the GPS track (not the elevation) before lookup.
2. Once the segment library exists, build a **canonical elevation profile per segment** — median across all traversals, or a single clean reference trace.
3. Thereafter look up elevation by **distance along segment**, not by raw coordinate. Drift-immune.

This is why §5 is load-bearing rather than a convenience feature. (§5a-bis below measures step 1 and finds it does not do what was predicted.)

### 5a-bis. Cumulative gain is currently UNRESOLVED — neither source is a measurement

Cumulative gain computed from the phone's 1 Hz altitude on the reference run **declines monotonically with the smoothing window and never plateaus**:

| window | 1 s | 5 s | 11 s | 21 s | 31 s | 61 s | 121 s |
|---|---|---|---|---|---|---|---|
| gain, relative to the 1 s figure | 1.00 | 0.79 | 0.73 | 0.63 | 0.57 | 0.45 | 0.35 |

A real signal converges under smoothing. Noise keeps shrinking. This one keeps shrinking. For reference the two vendors' reported totals differ from each other by more than 2×, and summing Garmin's own per-record altitudes gives a larger figure than either.

**There are FOUR numbers for this one run, not three.** The Garmin Connect API reports an `elevationGain` / `elevationLoss` pair that differs again from the totals in the FIT's own `session` message.

| source | gain, relative to the FIT `session` total | mechanism |
|---|---|---|
| Garmin per-record sum | ~1.2× | raw GPS altitude, integrated |
| Garmin FIT `session` | 1.0× | watch's own on-device total |
| **Garmin Connect API** | **~0.85×** | **server-side DEM correction** |
| Strava | ~0.45× | its own (DEM-derived) altitude |

(Ratios rounded; the measured run is not identified here.)

The mechanism is explicit in the API response: the activity carries an **`isElevationCorrected`** flag. **Connect applies DEM correction server-side and returns the corrected figure, while the FIT retains the raw watch value.** So the same device yields two different official numbers depending on whether you read the file or the API, and neither matches the other vendor.

This strengthens the section's conclusion rather than complicating it: four numbers spanning **roughly a 2.7× range** for one run is not a measurement with error bars, it is an absence of measurement.

### 5a-bis.1 Two external checks: the DEM is right, Garmin's profile is not

Neither check involves a vendor, an algorithm, or this project's own maths.

**1. An independent elevation floor.** Where a trail runs beside a body of water, its published water level is an independent floor: no point on the trail can be below it, and a trail on the slope above it should sit well above it.

| series | minimum vs the floor |
|---|---|
| **our DEM (1 m lidar)** | **well above** — correct for a trail on the slope |
| Garmin FIT raw | above, by about half the DEM's margin |
| **Garmin GPX "corrected"** | **barely above, and repeatedly below it — in the water** |

**2. A published trail figure.** The trail's published distance and gain from a trail guide give an independent climb rate. The recorded run was that route plus doubling back, so its total gain **should exceed** the published figure, but its gain per mile should be comparable.

| source | gain per mile, relative to published |
|---|---|
| published | 1.0× |
| Strava reported | ~1.2× — consistent |
| **Garmin reported** | **~2.0× — the outlier** |

**Recording offset:** if recording starts partway along a trail rather than at the trailhead, **a comparison against a published gain figure reads low by the vertical skipped.** Check where recording actually starts before comparing to a guidebook.

**The watch invents vertical that does not exist.** On a flat urban trail, GPS relief was **over 3×** the DEM relief. On the hilly home trail the same comparison was **about 2×**. **This is the cleanest demonstration in the project that raw GPS altitude is not measuring terrain** — not merely noisy around a true value, but reporting three times the vertical range that the ground actually has.

**A flat paved trail produces MORE raw gain per mile than the hilly trail.** A run on the flat urban cluster (GPS-covered portion only):

| smoothing | 1 s | 5 s | 11 s | 21 s | 31 s | 61 s |
|---|---|---|---|---|---|---|
| gain per mile, relative to 1 s | **1.00** | 0.73 | 0.48 | 0.31 | 0.26 | 0.20 |

Its raw 1 s figure was **about 1.4×** the raw per-mile figure of a hilly-trail run, and **roughly 10×** the API's DEM-corrected figure for this same run. **The flatter trail yields the larger raw figure, at coarser sampling (6 s median vs 4 s) which should accumulate less noise, not more.** Raw GPS altitude is not measuring terrain, and the sweep again never converges.

**Note: relief does not bound cumulative gain on rolling terrain** — ten 12 m rollers give 120 m of gain within 12 m of relief. The relief figure is context, not proof; **the failure to converge is the evidence.**

**The DEM lookup handles partial coverage naturally, so this needs no separate work.** It requires a *coordinate per point*, not a continuous track: a run with a mid-run gap gets DEM elevation on **every positioned record** and loses only the gap itself. That reinforces the per-portion framing in §14c rather than adding to it.

**Do NOT pull external trail databases to reconstruct missing position.** That is real effort for a rare problem — a typical dark spot lasts seconds — and it would breach §5's rule against matching to external geometry. The gap is simply excluded.

**MEASURED with the local 1 m tile (the reference run, Smart Recording):**

| smoothing | GPS gain/mile | DEM gain/mile |
|---|---|---|
| 1 s | 1.34 | **1.00** |
| 11 s | 0.74 | 0.57 |
| 31 s | 0.35 | 0.33 |
| 61 s | 0.17 | 0.13 |

(Normalised to the unsmoothed DEM figure.) On the same scale the vendors' reported figures were Garmin **~1.1** and Strava **~0.5**. The DEM does not by itself produce a single defensible number — **the smoothing window still determines the answer.**

**SMOOTHING ORDER — §5a's prediction is CONTRADICTED by measurement.**

§5a.2 predicts that GPS horizontal error propagating into the lookup is the dominant residual, and therefore that **smoothing the track before lookup** should beat smoothing the elevation after. It does not:

| window | smooth track, then look up | look up, then smooth elevation |
|---|---|---|
| 1 s | 1.00 | 1.00 |
| 9 s | 0.88 | **0.62** |
| 31 s | 0.82 | **0.33** |

(Gain per mile, normalised to the 1 s figure.) Track smoothing barely moves the number. At a 31 s window it has displaced points by a **median of ~17 m and up to ~70 m** — far enough to leave the actual trail — and still only removes under a fifth of the gain, where elevation smoothing removes two-thirds.

**Interpretation:** at 1 m posting the residual gain is **not** dominated by horizontal error. It is dominated by **real micro-terrain that the 1 m DEM faithfully resolves** — roots, drainage, curb lips. Displacing the point sideways lands on different micro-terrain that is equally rough, so track smoothing cannot help. Only smoothing the elevation profile removes it.

**This does not invalidate §5a.2 step 3; it re-motivates it.** A canonical elevation profile per segment is valuable **not primarily because it is drift-immune, but because a median across traversals is elevation smoothing done properly** — anchored to fixed terrain rather than to an arbitrary window.

### 5a-bis.3 Garmin's corrupted SHAPE, not just its offset

Prominence-detected hills, our DEM against Garmin's GPX corrected profile, same trackpoints, 25 m smoothing:

| | hills ≥5 m | largest prominence | median position offset |
|---|---|---|---|
| our DEM | **N** | 1.0× | — |
| Garmin GPX corrected | **more than 2N** | **~2.5×** | **~50 m** (worst ~190 m) |

**Garmin's relief is 2.3× too large, so prominence finds spurious hills in it.** Only about a quarter of the DEM's hills align exactly; the rest sit tens of metres to ~190 m away. **Its profile is corrupted in SHAPE, not merely offset in absolute value** — which is why it cannot serve as a structural reference, and why an acceptance test built on agreement with it would fail a correct implementation.

A second reason it could not serve: **Garmin's corrected elevation is itself a DEM lookup**, so agreement would be partly circular — the §15.1 trap already recorded for Strava's elevation and for `movingDuration`.

### 5a-bis.2 Per-point comparison via GPX — and a retraction

**GPX carries what FIT does not: each vendor's SERVER-CORRECTED elevation, point by point.** That allows comparison of the *series* rather than of summary figures with unknown smoothing behind them. Garmin's API returns GPX (`ActivityDownloadFormat.GPX`); every point matched a FIT record on timestamp.

| series | relief (DEM = 1) | unsmoothed gain (DEM = 1) |
|---|---|---|
| Garmin FIT raw | 1.95× | 1.35× |
| **Garmin GPX corrected** | **2.35×** | **1.58×** |
| **our DEM** | **1.00** | **1.00** |

**Garmin's "correction" makes the profile WORSE, not better** — more relief and more gain than the raw watch data, and a minimum at the independent elevation floor (§5a-bis.1).

**Garmin's reported summary is not the integral of its own corrected series.** The series integrates to **1.69×** the figure Garmin reports. So Garmin *does* smooth for the summary and publishes the unsmoothed series.

**RETRACTION — two of them.**

1. **"Garmin applies essentially no smoothing" (an earlier conclusion) is WRONG.** It smooths by 1.69×. The apparent agreement between our unsmoothed DEM gain and Garmin's reported figure (within ~7%) is **two errors cancelling**: our unsmoothed integral of an accurate series happens to land near their smoothed integral of an inaccurate one. **Right answer, wrong reason.**
2. **"Chording from Smart Recording explains the Garmin/Strava difference" (§14b) is WRONG for elevation.** Garmin recorded at Smart Recording (~4 s gaps); Strava at 1 Hz. **Coarser sampling should accumulate LESS gain, yet Garmin reports more.** Sampling cannot explain the direction. The chording argument stands for *distance*, where the sign is right; it does not transfer to elevation.

**Smoothing windows are DISTANCE, not sample counts.** Earlier tables labelled windows "1 s, 5 s…" when they were sample counts; at a 7.5 m median spacing those are metres of path, not seconds. Restated properly, gain per mile, normalised to our unsmoothed DEM:

| window | 0 m | 25 m | 50 m | 100 m | 150 m | 250 m |
|---|---|---|---|---|---|---|
| our DEM | 1.00 | 0.86 | 0.74 | **0.60** | **0.54** | 0.44 |
| Garmin GPX corrected | 1.57 | 1.48 | 1.35 | 1.14 | 0.99 | 0.80 |
| Garmin FIT raw | 1.35 | 1.20 | 1.00 | 0.79 | 0.65 | 0.44 |

**Our DEM reaches Strava's reported climb rate at roughly a 110 m window and the published trail figure at roughly 200 m. Garmin's corrected series is still nearly twice our DEM at the same 250 m window** — it is structurally inflated, not merely unsmoothed.

**Consequences.**

- **Do not use any device-derived gain figure — the FIT session total, the API total, or the other vendor's — as a measurement or a target.**
- The DEM lookup is not an accuracy improvement. **Without it there is no usable elevation number at all.**
- Every grade figure in §14a and §14b is **provisional** — they derive from Strava GPS altitude and inherit this problem.

### 5a-bis.5 THE BAROMETER RESOLVES THE INFLATION (n = 1)

§5a-bis.4's ~2× amplitude inflation was a property of **GPS altitude**, not of Garmin. On the first file from a barometric watch (FR265), same trail and same DEM tile, the watch-to-DEM amplitude ratio is **1.083** against the GPS-altitude reference run's **1.946**. Median watch-minus-DEM **+0.51 m**, sd **0.89 m**.

**The RELIEF agreement is independent evidence** (pressure-derived amplitude against a LiDAR surface, no DEM in the watch); **the OFFSET agreement is not** (Garmin anchors the barometric datum to GPS and possibly to a server-side model). §15.1's circularity caveat therefore applies to the offset and not to the amplitude. The full entry, with the pressure-drift test that would settle the offset (same trail, different weather), is §14o.

### 5a-bis.4 Garmin's min/max elevation is AMPLITUDE-INFLATED about a correct centre

Measured over the full-coverage runs on the home trail's main route, comparing `dem_elevations` against the Garmin API listing's `minElevation` / `maxElevation`.

| | DEM SD | Garmin SD | Garmin − DEM |
|---|---|---|---|
| max elevation | 0.54 m | 0.89 m | Garmin **+10.2 m** |
| min elevation | 2.20 m | 1.76 m | Garmin **−12.1 m** |
| **range** | | | ratio **2.08×** (SD 0.32) |
| **centre** | | | **agree to 0.95 m** |

**The errors point in OPPOSITE directions — Garmin high at the peak, low at the trough. That rules out a datum offset** (for example NAVD88 orthometric height against the WGS84 ellipsoid, which can differ by tens of metres), because a datum offset would shift both the same way. What remains is **amplitude inflation about a correct centre**.

**This is §5a-bis.3's corrupted shape, summarised to two numbers** — the same ~2× defect that more than doubles the prominence hills found in Garmin's profile. The independent check agrees with us, not Garmin: **Strava's reported maximum matches the DEM's to about 0.4 m; Garmin's is ~10 m higher.**

**STATUS: DISCREDITED (§4.2), grouped with the elevation profile.** It was originally proposed as an ORACLE field. **Filed that way, a future session would "fix" a correct DEM to match an inflated reading** — the disagreement is not our error. **The centre is the one part that is not corrupted** (correct to ~1 m) and may be recorded as a COMPARISON-ONLY check.

**Garmin's max is stable run to run (SD 0.89 m) — it is precisely wrong, not noisy.** Stability is not accuracy, and a repeatable number is exactly the kind that gets trusted by default.

DuckDB `spatial` extension (`INSTALL spatial; LOAD spatial;`) provides ST_ functions for the prefilter. At this data volume brute force also works.

---

## 6. Analysis primitives (build in this order)

**0. Basic metrics — the run's own description. BUILT (`src/trail_factors/analysis/basic.py`).** Average HR, three paces, windowed bests, run/walk/stand time, cadence. None of these existed in the tool surface at first, and a coaching chat ended up reasoning about drift and efficiency with no access to the quantities those are deviations *from*.

**These are ANCHORS, NOT SIGNALS,** and average HR is the clearest case: §6.2's finding 1 is that HR is nearly flat across grade, so the run mean carries almost no information. It is here to size the deviations, not to be read.

**Sub-labels**, as used in the section comments of `src/trail_factors/analysis/basic.py` and cited elsewhere: **§6.0a** average heart rate · **§6.0b** run/walk/stand durations and the three paces · **§6.0c** windowed best paces · **§6.0d** cadence · **§6.0e** per-effort pace and windowed bests within one effort · **§6.0f** effort grade profile (what the effort average hides) · **§6.0g** per-effort pace BY DIRECTION, shipped as `hill_pace`.

**THREE FACTS ABOUT THE CORPUS DECIDED THE SHAPE** (one-off measurements, method stated in each row).

| fact | measurement | consequence |
|---|---|---|
| `records_enriched` is DEM-gated | it exists only for activities a DEM tile covers — fewer than half the corpus when this was built; most Garmin runs had zero enriched rows | HR, cadence, pace and the bests read `records`; only the grade-binned table reads `records_enriched`, and it declares itself unavailable elsewhere |
| the splits carry durations for every run | `rwd_*` splits with non-null `total_timer_time` on every Garmin activity, at both sampling regimes | run/walk/stand time and the class paces are **NOT sampling-regime dependent** — a coaching chat had believed they were |
| the device odometer is the only whole-corpus distance | `records.distance_m` is non-null on every record; `path_distance_m` falls far short on a low-coverage run (roughly 40% of the odometer on a 25%-coverage file) but agrees **within about 1% (0.989–1.014)** wherever coverage is adequate | the windowed bests use the odometer and declare `distance_provenance` — this is not §9's prohibition, which is on `records.speed_mps`, the *fused instantaneous speed*, a different field |

**Three paces, never pooled.** `overall` is **Garmin continuity**, and that is a specific denominator rather than an intention: measured against Garmin's activity listing, `activities.timer_time_s` **IS** Garmin's `duration` (max difference 0.0002 s) and `duration_s` **IS** its `elapsedDuration`. `moving_time_s` is **not** Garmin's `movingDuration` (median ~9 s apart, up to ~60 s) and is circular here anyway — `src/trail_factors/ingest/load.py` *defines* it as sum(`rwd_run` + `rwd_walk`). Running-only and walking-only come from the splits. §4.3 still holds: elapsed and moving are never reported one without the other.

**`MIN_CLASS_TIME_S = 120`, fitted.** Method: leave-one-split-out jackknife SD of the class pace, binned by total class time, over every Garmin activity. The dispersion falls from several hundred s/mile below 30 s of class time, to under 100 at 60–120 s, ~60 at 120–300 s, and under 30 above 300 s. The stated tolerance is 60 s/mile, which 120–300 meets and 60–120 does not. There is **no sharp knee** — the dispersion falls smoothly — so this is a tolerance made explicit, not a boundary discovered in the data. It gates a sizeable share of walk classes, which is the honest outcome: a walking pace from 13 s of walking is not a walking pace.

**Zero is not missing.** Some activities have no `rwd_walk` split and some no `rwd_stand`. Those are real zeros; `n_*_splits` is what distinguishes them from absent data.

**Windowed bests: sliding, interpolated, never an instantaneous maximum.** That is the design rule, and the corpus backs it — the single-step path-derived maximum reaches **2:27/mi (10.9 m/s)**, a GPS artifact, and 5 s and 10 s windows are still contaminated. Boundaries are **interpolated to exactly 30 s / 402.336 m / 1609.344 m**. Meets-or-exceeds reads as the conservative choice and is not: it reports a *different quantity every run* (winning quarter-mile windows covered **402.9–415.9 m**, so "quarter mile" would mean 3.4% further on one run than another), and its error is two-signed because a longer window reads *faster* through an acceleration. Against 1 Hz ground truth by decimation, interpolation wins at every size: mean absolute error **3.03 vs 4.70 s/mile** at 30 s, **1.78 vs 2.66** at 0.25 mi, **0.30 vs 0.36** at 1 mile.

*Validation.* An auto-lap mile is a sliding mile that happens to start on a lap boundary, so the sliding best can only be faster. On every full-coverage run with a mile lap it holds, worst case **+2.9 s** — sampling granularity, because Garmin cuts laps on its internal 1 Hz odometer, which is finer than the recorded Smart Recording samples. The invariant is **not available below full coverage**: partial-coverage runs violate it by ~30 s.

*Quality flags are NOT applied here, and the reason is measured.* **100% of one-mile windows on every activity that has a mile contain at least one flagged record**, so a reject-on-any-flag rule returns nothing, ever; and deleting a flagged record from the middle of a window and joining its neighbours is the straight-line invention the gap rule exists to prevent. The winning window's `flagged_record_fraction` is reported instead. This matters more here than elsewhere because **a maximum selects for positive measurement error in a way a mean does not**.

*No gait restriction.* On hilly run/walk sessions an all-`rwd_run` mile often does not exist at all. A best window is one contiguous stretch of real ground, not a pooled aggregate, so §6.2's ban on pooling gaits does not apply; but `run_time_fraction` / `walk_time_fraction` are reported, because a "best mile" can contain a substantial share of walking.

**Weights are computed BEFORE any filter (`record_weights`).** Deriving a sample's weight from the gap to the next *surviving* sample makes the weight a property of the filter. Measured on a 1 Hz run: the weight sum inflated **1,709 s → 2,068 s** (+21%, 360 spurious seconds) and mean running cadence moved by several spm. `weighted_mean()` takes a weight *map* rather than a sample list precisely so this cannot be written again.

**Stopped time is `stops` ∪ `rwd_stand`, merged.** Neither alone is enough: `stops` holds only a handful of rows corpus-wide by design (§4.3's 120 s / frozen-distance / zero-cadence / zero-speed conjunction), while `rwd_stand` carries standing time on most activities. Merged rather than subtracted in turn, because they overlap and double-subtraction can drive a weight negative.

**1. Grade-Adjusted Pace.** Start from the Minetti (2002) metabolic cost-of-gradient curve. Then refit the curve to own HR-vs-grade data. A personal GAP curve is the highest-value artifact in the system.

**THE REFIT IS FEASIBLE WHEN HR IS FLAT ACROSS GRADE, AND §6.2 IS WHERE THAT IS CHECKED.** Bin gated `rwd_run` records by grade, using **path-derived speed**, and compare the spread of pace with the spread of HR. **If pace varies several times more than HR across the grade range**, then at constant metabolic effort the pace ratio across grades *is* the inverse cost ratio, so the flat HR is **not an obstacle to the refit — it is the identifying assumption that makes it possible.** A runner whose HR tracked grade would need a joint model; a runner who holds effort roughly constant and lets pace absorb the gradient supplies the constant-effort condition for free. The fitted curve should also have Minetti's shape: speed peaking slightly downhill and falling in both directions.

**HR LAG CORRECTION — the shift matters, and the assumption survives it.** HR lags effort by roughly 40 s (§14b; `HR_LAG_S`), and short climbs last only 60–90 s, so raw binning assigns HR from ~40 s earlier to the current grade and smears it. Re-binned with HR shifted +40 s (§12.2's figure), the cross-bin spread can open slightly and the shape change meaningfully: climbing bins reading above flat and descending bins — a climb premium the raw binning hides. **Where that premium stays small against the pace variation**, a single-equation refit stands. **Carry the climb premium as a correction term rather than ignoring it or escalating to a joint model.**

**THE FITTED CURVE WILL BE FLATTER THAN MINETTI — WITH TWO CANDIDATE EXPLANATIONS, UNDETERMINED.**

Do **not** write this up as "this athlete is less affected by grade than the population model". That overstates what is measured, and there are at least two ways to get an apparently flat curve without the climbing actually being cheaper:

1. **Minetti measures VO2; we measure HR.** They diverge exactly where it matters. **Anaerobic contribution on steep pitches lifts power without lifting HR proportionally**, so an HR-based curve flattens at the top of the grade range even if metabolic cost is rising normally. This is a measurement-channel difference, not a physiological finding about the athlete.
2. **The 25 m grade window smooths peak grades** (§9). The steepest pitches are averaged with their approaches, so the samples labelled "+20%" were partly run at less than +20%. That **compresses the x-axis** and pushes the fit flatter, in the same direction as (1).

**A third explanation — a deliberate pacing strategy such as walking steep pitches (§12.2) — is also live**, and unlike the first two it would be a real behavioural finding rather than an artifact. **Nothing currently distinguishes the three.**

**The fit remains VALID FOR ITS PURPOSE regardless.** GAP exists to make two different trails comparable for *this* athlete, and a curve fitted to the athlete's own pace-vs-grade does that whichever explanation dominates. What must not be claimed is the physiological interpretation. **Separating the three needs either a VO2 or power channel, or a narrower grade window on 1 Hz data — both listed in §14 as open.**

**NEVER FIT FROM `records.speed_mps`.** The fused signal gives a **~3%** spread across the same bins against path-derived speed's ~15% — §9's grade bias flattens exactly the variation being fitted, and a fit from it would return a null that looks like a finding.

**THE FIT IS DATED AND REFITTABLE ON A ROLLING WINDOW.** Fitness changes over months; a curve fitted to one season may not describe the next. `gap_curve` stores the fit **with its date range**, sample counts per bin, and the corpus fingerprint it was fitted on, so a curve is never applied silently outside the period it was fitted to.

**THE DELIVERABLE — grade-binned pace, split by `split_type`.** GPS-derived speed (§9), which understates true path by ~5% from chording; the offset varies only 1.08→1.00 across the grade range, so between-bin comparison holds. The table is reported per `split_type` (`rwd_run`, `rwd_walk`) with, for each grade band: n, p10 / p50 / p90 speed, the p50 pace (formatted `M:SS/mi` only at display, through `src/trail_factors/units.py`), and median HR.

**Split, the structure appears.** Running pace slows steadily from the gentle-descent bins to the steepest climbing bins, and walking pace does the same from a slower base — against a pooled table that showed almost the same speed in every bin. **Where median HR stays inside a narrow band throughout** — a runner who holds effort roughly constant and lets pace absorb the gradient — that is exactly why §12.2's population-GAP test fails and why §6.1's personal refit is needed.

**HR CAN SIT IN A NARROW HABITUAL BAND THAT STRADDLES A ZONE BOUNDARY** (§14e). When it does, **"time in zone 2" measures where that line falls through a narrow, stable effort band — it does not describe a training decision.** A few bpm of drift, or a boundary that moves, relocates large amounts of time across the zone 2/3 border while the athlete does exactly the same thing.

**Combined with §14e's drift, this is a HARD REQUIREMENT on the coaching layer: any statement about zones must name the BOUNDARY SET and the DATE RANGE it applies to.** "38 minutes in zone 2" is not a fact on its own — it is a fact about a boundary set (e.g. the RUNNING set with its stated `z3Floor`) over a date range. Stated without both, it is the §15.4 failure mode: a number that reads as a measurement while actually encoding an unstated convention. **For an athlete whose effort sits astride a boundary, zone-based statements are weaker evidence than grade-binned pace**, which has no such boundary and is where §6.2's real structure lives.

**NEVER pool running and hiking in a grade-binned table.** Pooled medians on the reference run fell in a narrow speed range across every grade bin, which reads as "pace does not vary with grade" and is an artefact: each bin mixes `rwd_walk` (median well below running speed) with `rwd_run`, and the median lands between the two modes. Split by `split_type`, or report the distribution rather than the median. The raw data does show the expected spread — on the same run, a fast closing descent and the steep climb immediately after differ in pace by roughly a factor of three to four.

**2. Grade binning.** Bucket every second into grade bins (−20%, −15%, … +20%). Median HR and median pace per bin. Two different trails become directly comparable. *Solves the core problem before any modeling.*

**3. Climb-recovery time constant.**

**THE MILESTONE 6 SLOPE UNIT MAKES THE LIKE-FOR-LIKE TEST POSSIBLE — AND IMMEDIATELY SHOWS IT IS CONFOUNDED.** Because an ascent and the descent of the same face are two efforts on ONE segment, recovery after climbing slope X can be compared with recovery after descending the same slope X. **That comparison must not be made on τ alone.**

**τ measures recovery RATE, not cost.** A 3 bpm excursion recovering in 47 s and a 20 bpm excursion recovering in 89 s are not comparable quantities, and ranking them by τ would say the smaller effort recovered "better" when it was barely an effort at all. **The like-for-like comparison needs ΔHR and τ together**, and that is the analysis milestone's first task — not milestone 6's.

**TAU WAS NOT MEASURABLE WHEN BUILT — BUILT AND REFUSED.** §6.3 is implemented (`src/trail_factors/analysis/recovery.py`, `uv run trail-factors recovery`) and it **declines to report τ** unless the corpus supports it, on measurement rather than assumption.

Within one `(segment, direction)` — repeat traversals of the SAME slope, which should give the same τ — the within-group SD was about as large as the median τ itself: a coefficient of variation near 1.0. **The obvious explanation was tested and REJECTED**: on rolling terrain the decay tail usually contains the next climb (the median gap to the next effort was well under a minute, and few efforts had 300 s clear), so τ looked like recovery interrupted at a random point — but restricting to genuinely clear windows (≥120 s) did **not** fix it. Whatever the cause, τ carried no information there.

Per §4.2, τ is computed and stored as a diagnostic with `tau_reportable` and a per-effort `reason`, and is **not shown while the gate is closed**. A metric that cannot be measured must not be reported with a caveat attached. **A sustained climb topping onto a plateau is the terrain that would give a real recovery interval (§5.4).** The threshold is stated as `MAX_TAU_CV` and the gate is evaluated against the live corpus — `tau_reportable` is a CORPUS verdict stamped on every row, not a per-effort flag — so τ starts reporting on its own when the data supports it rather than needing a decision. When it does report, the payload carries `gate_basis` (how many `(segment, direction)` groups had enough estimates to vote), so a reader can see how thin the basis is.

**ΔHR ships and is the finding.** Method: max HR minus opening HR within each effort, over `segment_efforts`. **The ascent/descent asymmetry is large and reproduces**: across the corpus the median ascent rise is roughly double the median descent rise, and paired within one slope it is usually starker still — though not universal: at least one slope shows the reverse ordering. *(Re-derive before quoting any figure; the corpus grows.)*

**The τ figures themselves are UNRESOLVED and must not be quoted yet.** A first-pass measurement (τ as time to fall 1−1/e of the post-effort drop, efforts with a drop under 3 bpm excluded) gave the **opposite ordering** of ascent and descent τ to a figure carried in from another chat. **Neither should be believed until the analysis milestone settles the method**, because the sign of the difference depends on the inclusion threshold: descents produce small excursions, so whether they enter the sample at all is decided by exactly the cut that the confound makes arbitrary. **That instability is itself the strongest evidence for the confound**, and the reason CLAUDE.md requires a reproducible computation beside any figure.

After each detected climb, fit:
```
HR(t) = HR_floor + (HR_peak − HR_floor) · e^(−t/τ)
```
τ is a clean, personal, trackable number. Should drop with fitness. Likely the single best metric in the system — once it is measurable.

**A stop is an unplanned HR recovery test, and a SECOND SOURCE of τ.** How far HR falls and how fast during a mid-run stop is the same quantity this fits after a climb — obtained free on any run containing a stop, with the confound of continued movement removed entirely. The `stops` table carries `hr_at_stop`, `hr_min`, `hr_at_resume` and `hr_drop` for exactly this. Fit τ from both sources and compare; they should agree, and disagreement is informative (§15).

**4. Aerobic decoupling.** Ratio of (GAP speed ÷ HR) first half vs. second half. Direct measure of durability — the thing that determines ultra performance.

**REQUIRES A STEADY-EFFORT PRECONDITION. Without one it reports backwards on any run with a hard finish.**

On a run with a deliberate closing surge it returned a clearly **negative** figure — an apparent efficiency *gain* of several percent — while matched-terrain drift (§6.7) showed HR **rising** at matched speed on the same file. Cause: the surge lifted second-half speed well above the first half's, and the GAP/HR ratio rewards the faster half. Excluding the surge it was still negative. The metric assumes effort is held constant; that run did not hold it.

**The gate.** Before reporting decoupling, require that the two halves are effort-matched:
- time-weighted mean GAP of the second half within **±5%** of the first, **and**
- no sustained block faster than **+15%** of the run's median GAP lasting over 60 s.

If the gate fails, **return no value** — do not report a decoupling figure with a caveat attached (§15.4). Where the gate fails, §6.7 is the substitute: it is effort-matched by construction.

**5. Climb profile signature.** Represent each run as a sorted list of climb segments (length, gain, mean grade). 8 rollers vs. 3 sustained climbs have different signatures despite identical totals.

**REFRAMED FROM DESCRIPTOR TO MODEL INPUT.** This was written as a way of describing a run to a reader. It is also **the thing that makes two runs comparable**, which is what §2's residual model needs and what §2.2 says average grade cannot supply. The build is unchanged in kind; what changes is that it must be *callable as a feature vector*, not only printable.

**FOUR ADDITIONS.** The first three are scalars over the run's slope traversals; the fourth is the one that answers "which runs are comparable to this one".

| field | definition | why |
|---|---|---|
| `hill_fraction` | distance on slope traversals ÷ total path distance | separates a hilly run from a flat one before any grade term is read |
| `longest_climb_m` | max ascent traversal length | the sustained-climb axis; six rollers and one mile-long climb differ here and nowhere else |
| `n_climbs_over_{L}m` | count of ascent traversals longer than a stated L | the roller-count axis. L is NOT yet fitted — see the caution below |
| signature **similarity** | a distance between two sorted signatures | makes "runs comparable to this one" answerable, and sets the §2.4 prediction interval |

**CAUTION, MEASURED, AND IT IS THE SAME TRAP §5 HAS FALLEN INTO TWICE.** `hill_fraction` on the home trail cluster is **above 0.9 on every run** — the trail is essentially all slope, with almost no flats between features (§5). **A covariate that is ~1 everywhere carries no information and cannot be validated there.** Neither can a length threshold L be fitted on terrain that supplies no contrast.

So: build the fields, and **do not fit L or the similarity metric's weights on terrain like that.** The first real test is terrain with genuine flats between climbs — the same condition §5 and §14l already carry for `BENCH_MAX_M`. Recording the fields now is worthwhile precisely because the data that validates them has not arrived; recording a fitted threshold now would not be.

**The similarity measure is also the §2.4 interval.** A new trail whose signature has a near neighbour in the corpus gets a narrow expected-HR interval; one with no neighbour gets a wide one and must say why. That is the whole mechanism by which the model admits it is extrapolating.

**8. Efficiency Factor.** GAP speed ÷ HR on **matched terrain**, trended over time. Raw speed ÷ HR would measure which trail was run, not fitness. The familiar course is the natural home for it: it is usable per run as soon as the course is DEM-covered, but **too sparse to trend** until runs on that course are dense enough. *(This is §6.8, not §6.5: §6.5 is climb profile signature, and EF did not previously exist in §6.)*

**6. Segment-effort trending.** Same segment, all traversals, over time — HR at matched GAP, τ, vertical speed, **and pace — both forms.** `segment_history` originally returned duration and vertical speed but no pace at all, so a reader could see a slope getting slower without seeing how fast it was run. Every effort now carries **`mean_pace_per_mile`** (whole effort, summit to col, moving time) **and `best_windows`** (best 10 s and best 30 s inside the effort). **They are different quantities and must never be substituted:** on one long ascent the best 30 s was nearly twice as fast as the effort's mean pace. The mean is what the terrain cost; the window is the part that was actually run fast.

**A GRADE-BINNED MEDIAN REPORTS HOW THE ATHLETE CHOSE TO RUN, NOT WHAT THEY CAN RUN — §6.2 and §16 both turn on this.** On one run the athlete descended nine hills, eight of them easy and one fast. Nine fast records cannot move a 63-record median, so the table reads as "descents are slow" when what it measured was a decision to run eight of them easy. **The coach must read the grade table as CHOICE and the windowed bests as CAPACITY.** Neither is wrong; asking the wrong one is. A training prescription built on the median would be prescribing against the athlete's own pacing decisions rather than against their ability.

**Open question — grade versus surface and technicality.** Pace on a technical, rocky trail can run far slower than on a flat urban route — more than its gain per mile explains. Grade-binned pace (primitive 2) will let that be **decomposed**: how much of the gap is grade, and how much is surface and technicality — roots, rock, footing, line choice. **This is a question the silver layer enables, not something to build now.** It also matters for course prediction, since a course's ft/mile does not by itself say how runnable it is.

**7. Matched-terrain drift.** Pair records from the **same ground** within one run: <10–30 m apart, ≥10–20 min apart, both `rwd_run`, speed matched within tolerance. Report the mean HR delta (later minus earlier) normalised to bpm/hour.

**TWO MEASURES EXIST AND THEY ARE NOT THE SAME QUANTITY — reconciled after the coaching chat flagged a ~3x discrepancy between them on the same run.**

| | `matched_terrain_drift` (§6.7) | `segment_effort_drift` |
|---|---|---|
| grain | **record pairs** | whole **effort averages** |
| `split_type` | **`rwd_run` only** | none — walking included |
| speed matched | **yes, ±0.35 m/s** | no |
| terrain matched by | ≤20 m apart | segment identity |
| sample on the reference run | **hundreds of pairs** | **four pairs** |
| stability | narrow range over 18 tolerance combinations | the four pairs ranged over more than an order of magnitude |

**THE ROOT CAUSE WAS AN IMPLEMENTATION ERROR, NOT A MEASUREMENT DISPUTE.** The slope version was labelled §6.7 but **dropped the two filters §6.7 explicitly names** — `both rwd_run` and `speed matched within tolerance`. It is a legitimate quantity and is retained under its own name, but it was never §6.7.

**AND IT IS INHERENTLY NOISIER.** Normalising to bpm/hour amplifies short gaps: **a 20 bpm difference across 15 minutes reads as +80 bpm/hour**, which is a local HR difference extrapolated, not a sustained drift rate. With n=4 the median of such values is unstable; §6.7's hundreds of pairs are not.

**NEITHER IS "THE" DRIFT, and the report presents both under their own names.** `matched_terrain_drift` is the §6.7 measure and the default; `segment_effort_drift` is kept because matching terrain by segment identity is stronger than by a distance threshold, and it yields a value on runs where §6.7 finds no pairs. (`segment_effort_drift(direction=...)` is the direction-aware variant; it is never called "§6.7 by direction" — see CLAUDE.md.)

**EVERY PRIMITIVE RESULT STATES THE FILTERS IT APPLIED, as fields rather than prose** — `split_type`, `walking_paired`, `speed_matched_within_mps`, `min_separation_s`, `stopped_time_excluded`, `grain`. This came from the coaching chat's first session: it could not tell whether drift excluded walking and **correctly refused to lean on the number**. A result whose filters are invisible cannot be reasoned from, and that refusal was the right behaviour.

**This is the only cardiac-drift estimate in the system that uses NO elevation, so it does not inherit §5a-bis.** Terrain is held constant by identity rather than by a grade model, which means it is also free of the provisional GPS grades every other drift figure depends on.

*Evidence of stability.* An 18-combination sweep over distance (10/20/30 m), speed tolerance (0.20/0.35/0.50 m/s) and minimum separation (600/1200 s) returned values within a narrow band — stable across every tolerance tried.

**Order of operations matters, and getting it wrong is silent.** Filter first (both `rwd_run`, speed within tolerance), *then* deduplicate so each record is used at most once. An earlier pass greedily paired closest-first and filtered *after* deduplication; that discards valid pairs in a distance-biased way, and it produced a materially lower, selection-dependent figure. **That figure was wrong.** Filter, then deduplicate.

Requires repeated terrain within the run — see §14b on out-and-back structure.

**Caution on the HR–GAP slope.** It is **not identifiable from a single run**: fitting `HR ~ GAP` on one run with a closing surge gave a coefficient that fell by more than half when the surge was excluded. Steady running spans a narrow GAP band, so a surge supplies nearly all the leverage. **Do not trust the coefficient until many runs are fit jointly (§2).**

**9. Drift onset — WHEN drift begins. BUILT AND REFUSED (`src/trail_factors/analysis/drift_onset.py`).**

**A VIEW OF §6.7, NOT A NEW DETECTOR.** `matched_terrain_drift` already yields the hard thing: pairs on the same ground, ≥600 s apart, both `rwd_run`, speed matched ±0.35 m/s. This bins those pairs by the **later record's elapsed time** into 5-minute bins and reports the rate as a curve against time-into-run. No new baseline, no fitted K, no changepoint search.

**ONSET IS ONLY INTERPRETABLE CONDITIONED ON DEW POINT**, so dew point travels with every value. 92 °F at a 72 °F dew point is a different run from 92 °F at 50 °F, and an onset time quoted without it is not a measurement of the athlete.

**IT IS REFUSED, AND THE GATE IS COMPUTED FROM THE LIVE CORPUS** so it lifts itself — the same construction as §6.3's `MAX_TAU_CV`. Acceptance criteria: (a) split-half stability on odd/even seconds within a bin; (b) across ≥8 same-cluster runs at comparable dew point, SD < 0.5 × median. **(b) could not be evaluated when this was built**: there were too few 1 Hz Garmin runs.

**THE WAIT IS FOR LONG RUNS, NOT FOR RUNS — and this is the finding worth keeping.** Running `curve()` over every DEM-covered Garmin activity, the large majority came back `insufficient`, and **no run under about an hour has ever produced an onset**. The cause is structural — §6.7's 600 s minimum separation plus the first terrain revisit leaves no room for three usable 5-minute bins. **Short runs (§14g) can therefore never contribute to this metric, however many are recorded.** The time to the acceptance threshold is set by how often long runs on the same cluster happen, not by total run count.

**FOUR WAYS THIS WOULD PRODUCE A CONFIDENT WRONG ANSWER**, measured and recorded so they are not re-derived:

| threat | measurement | what the design does |
|---|---|---|
| **pseudo-replication** | one bin held **917 pairs drawn from 2 passages**; a sign test over raw pairs returns p ≈ 1e-73 on noise | the independent unit is a **passage** (later-records split on a 30 s gap); a bin needs ≥3 before its sign is read |
| **onset is largely run length** | r(onset, duration) ≈ **0.85** across the handful of runs that produce one — itself too few to lean on | recorded as a confound the cohort test must be read against; not yet a second gate |
| **the x-axis is the y-axis's own window** | on an out-and-back, gap is forced to T − 2t; median pair gap rises monotonically across bins on the reference run (from ~650 s to ~2,700 s) | `median_pair_gap_s` is reported per bin so the coupling is visible |
| **the floor is the metric's own** | §6.7 requires ≥600 s separation | no onset earlier than 10 min is representable; one at the floor is the filter, not the athlete |

**"No drift occurred" is a COUNTED CATEGORY** — `onset` / `no_onset` / `insufficient`, never a bare null and never a sentinel number. A sentinel would be averaged; collapsing `no_onset` into `insufficient` would make drift look universal.

**TIES ARE COUNTED.** Pairs with equal heart rates are evidence *for* the null and appear in every bin of the reference run. Dropping them would inflate the apparent majority in exactly the bins that decide onset.

**The bin width decides the answer** — on one run, onset moved by roughly a quarter of an hour across widths from 60 s to 600 s. 300 s is the specified width; it is reported as a field rather than presented as width-free.

**Acceptance criterion (a) is weak and is labelled as such.** Odd and even seconds of a 1 Hz file are the same passage a second apart; they cannot disagree unless the sensor is broken. It is retained because it would catch an alignment error, not because passing it is evidence.

---

## 7. Track / interval mode

Separate analysis path from trail. **Press the lap button on every rep** — free exact segmentation, saves writing changepoint detection.

### Available now (FR245, no accessory)
- **Speed decomposition:** speed = cadence × stride length, where stride length is *derived* (speed ÷ cadence), not measured. Still the core form metric. Work on a deliberate gait change (the run/walk "mode", §17.2 M1 — referred to generically; no display name is stored or hardcoded, §17.2.1) should show cadence rising at matched pace.
- **Cadence stability within rep and across reps.** Decay from rep 1→8 at matched pace indicates neuromuscular rather than aerobic limitation.
- **Recovery HR at fixed offsets:** HR at 60s and 120s post-rep. Compare across reps and across weeks.
- **Protocol comparison** (fartlek vs. structured): total work at matched mean HR. Which protocol bought more quality volume for the same cardiac cost.
- **Pace consistency:** within-rep coefficient of variation. Even splits vs. fade.

### Requires HRM-Pro Plus / HRM-Run / RD Pod
The FR245 emits **no running dynamics without a paired accessory.** These fields will be null on it. (A watch with built-in running dynamics, such as the FR265, records ground contact time, vertical oscillation/ratio and step length on its own — see §17.1b-bis — but GCT balance and stance-time percent still need an HRM-Pro or RD Pod.)
- **Ground contact time** — the direct test of whether a form change is taking. Cadence rising *and* GCT falling at the same pace is the signature of the gait shift. Without GCT this question is unanswerable.
- **Vertical oscillation / vertical ratio** — best form-efficiency number Garmin produces.
- **GCT balance** (left/right) — asymmetry and injury signal.
- **Measured step length** (vs. derived).

**Recommendation for an FR245 user: HRM-Pro Plus.** Highest-leverage purchase for this system. Three wins:
1. Restores all running dynamics above.
2. Eliminates optical HR cadence lock. Since the entire system rests on HR residuals, contaminated HR poisons every downstream model. This alone justifies it.
3. Records RR intervals → HRV without a watch upgrade. **Verify** the watch writes `hrv` messages to the FIT when paired before relying on this.

---

## 8. Weather ingest

**Source:** Open-Meteo. Free, no API key, historical archive available. **BUILT** — `uv run trail-factors weather`, and folded into `sync`.

**TWO ENTRY POINTS SHARE THE NAME `sync`, AND THEY DO DIFFERENT AMOUNTS OF WORK.**

- **The CLI `sync`** (`uv run trail-factors sync`, `src/trail_factors/cli/report.py`) downloads new activities (the whole account by default, §4.1), ingests, fetches weather, validates, and prints the plain-language report. **It does NOT run the terrain steps.** Those are separate commands — `dem`, `silver`, `match`, `recovery` (in that order; `recovery` must follow `match`), or `retile` after a new DEM tile — so a run synced from the CLI has no grade, slopes or segment efforts until they are run.
- **The MCP `sync` job** (`src/trail_factors/mcp_server.py`) downloads over a **400-day** window, then runs ingest, dem, silver, weather, match, recovery and validate. With no DEM tiles on disk it skips the terrain steps (dem, silver, match, recovery) with a note rather than failing, and it reports this sync's rejections in every case (§4.3).

**THE MCP SYNC IS EXPOSED AS A JOB, NOT A CALL (§12.6).** It is minutes of work and holds DuckDB's **exclusive write lock** throughout. A reader cannot open the file while a writer holds it (measured, cross-process), so a synchronous sync tool would make **every other tool call in the conversation fail** for its duration: it fails the conversation, not merely itself. `sync` therefore returns a job id immediately and `sync_status` polls it. Job state is a FILE (`TRAIL_FACTORS_JOBS_FILE`), not memory, because an MCP server can restart between a chat's calls and a job id that silently stopped meaning anything would be worse than none.

**NOTHING REACHABLE THROUGH THE MCP SERVER MAY MODIFY `data/raw/`** — not alter, not move, not rename, not delete. §3 made the raw layer immutable when the CLI was the only way in; this is the same guarantee for the new door, and it is **structural rather than a rule**: every database connection in the server is `read_only=True`, the module opens no file for writing, and the only writes reachable from any tool are the athlete-state directory (`data/athlete/`, overridable with `TRAIL_FACTORS_ATHLETE_DIR`) and the job-state file. **`sync`'s download is the one write into `data/raw/`, and it is APPEND-ONLY**: the downloader refuses to overwrite an existing file outright. Pinned by a test that fingerprints every file under `data/raw/` (size and mtime), runs every tool including failing ones, and asserts the tree is unchanged.

**PRIMARY SOURCE IS IEM (Iowa Environmental Mesonet) ASOS station data — DEFERRED, NOT REJECTED.** Open-Meteo is what is wired today and is sufficient for now, for a stated reason rather than by default:

- **Weather currently feeds nothing decisive.** The residual model (§12 milestone 7) and the §16.5 alert are both unbuilt. Until they exist, weather needs **"did it matter" precision, not exact readings** — and reanalysis gives that.
- **Wind is the one variable where a station clearly beats reanalysis**, and §14c measured wind at **r ≈ 0** (−0.009). There is no signal there to sharpen. (§14c also records why: trail runs under canopy and in sheltered terrain are not measured by a distant open-ground station, so **the null may be measuring the wrong place rather than measuring no effect** — which is an argument for an on-trail observation, not for a better station feed.)
- **Dew point, the variable that matters most (§8), transfers best over distance** — nearby stations tens of kilometres apart agreed within about 0.5 °C — so it is the variable least improved by moving to a nearer station.

**WIRE IEM WHEN THE RESIDUAL MODEL NEEDS STATION-QUALITY DATA**, and treat this paragraph as the deferral rather than re-deciding it from scratch.

**SOURCE PROVENANCE — `weather_source` ON EVERY ROW**, the same pattern as `records.altitude_source`: provenance travels **with** the value so a query can tell a reanalysis reading from a provisional forecast without joining anything.

| value | meaning |
|---|---|
| `forecast` | Open-Meteo forecast endpoint, used for runs newer than the archive lag. **Provisional.** |
| `era5` | ERA5 reanalysis archive. Final. |
| `assumed_indoor` | labelled assumption for a GPS-less treadmill run — never a measurement |

**ARCHIVE ROWS REPLACE FORECAST ROWS WHEN THEY ARRIVE.** `weather` re-fetches any cached provisional response once ERA5 has caught up and deletes/re-inserts per activity, so the replacement is atomic and no row is left carrying a stale provenance. **No separate backfill job, and nothing to remember.** **`sync` never blocks on weather** — the step is wrapped so a fetch failure cannot stop the ingest report.

**PRIVACY — THE COORDINATE IS COARSENED BEFORE IT LEAVES THE MACHINE.** §5a.1's constraint is that a per-coordinate query stream to an external service is a route trace. This is not that — it is **one coordinate per run** — but an exact start point still should not sit in a third party's logs. Coordinates are rounded to **2 decimal places (~1.1 km)** before the request, which displaces a start by hundreds of metres; `fetch_var` refuses unrounded input. **The rounding costs nothing meteorologically**: ERA5's archive grid is ~11 km and the finest forecast grid ~1–2 km, both coarser than the rounding. The value actually sent is stored in the cached response as `_requested_lat`/`_requested_lon`, because Open-Meteo echoes its own grid-cell centre and the response alone cannot demonstrate the coarsening.

**RESULT.** At first build every activity carried weather, each row labelled `era5`, `forecast` or `assumed_indoor`, with zero fetch failures. Raw responses are cached under `data/raw/weather/` (gitignored with the rest of `data/` — they carry the coarsened coordinate), so a full rebuild reproduces from cache with **no network calls at all**. Two gold checks: `weather_missing` (WARNING — a positioned run with no weather) and `weather_provisional` (INFO — awaiting archive backfill).

- Fetch at **trailhead lat/lon and actual start hour, in UTC**, hourly, interpolated across run duration.
- **Open-Meteo's archive is indexed by UTC. Pass UTC, never local time.** At a local offset of several hours, a morning run would otherwise fetch afternoon conditions, and dew point moves substantially across that gap — which would corrupt the single variable §8 exists to capture.
- **Store dew point, not just temperature.** Heat stress on cardiac drift tracks dew point far better than dry-bulb. This is the difference between a correlation you can see and one you can't.
- Store **precipitation in the preceding 24h** as an automated proxy for trail wetness. There is no observed trail surface to confirm it against in this build, so it stays a proxy and is labelled as one.
- The archive/reanalysis endpoint lags several days. For same-day ingest use the forecast endpoint's `past_days` parameter, label the row `forecast` (provisional), and let the next `weather` run replace it from the archive (above).
- **Weather should essentially ALWAYS be available for an outdoor run.** It needs only a coordinate and a timestamp, both of which every positioned run has. **Missing weather on an outdoor run is a FETCH FAILURE worth flagging, not an expected gap** — unlike a segment match or a Garmin self-evaluation, there is no legitimate reason for it to be absent.
- **Treadmill runs get an ASSUMED indoor default (~73 °F), never null.** A GPS-less, treadmill-classified run has no location to query, but null would be indistinguishable from a fetch failure and would silently drop the run from any temperature-aware model. Record the assumed value **and mark it assumed** — `weather_source = 'assumed_indoor'` — so it is distinguishable in the data from a measured reading and can be excluded from any analysis that requires measured conditions. **An assumed value that is labelled is usable; an unlabelled one is a lie.**
- **Never use the watch's temperature field.** It reads wrist/body heat.
- **Weather must be interpolated PER RECORD, not stored as a per-activity scalar.** A 3–4 hour long run in hot weather can span **10–15 °F within the single activity**. At that point within-run temperature change rivals between-run variation, and a run-level average destroys exactly what is being measured — the same error class as one average heart rate over eight climbs (§1). Hourly data — or a station's typical 20-minute observation cadence — is ample resolution for a 10–15 °F change over 3–4 hours.
- **Bonus from the long run: within a single activity, temperature rises while fitness is constant by definition.** That is a within-run identification strategy unavailable between runs. It remains confounded with fatigue, so it **complements** the cross-run comparison in §14c rather than replacing it.
- **Do not linearly interpolate wind direction.** It is a circular quantity — interpolating 350° to 10° passes through 180°, and a calm observation coded 0° corrupts its neighbours. Carry direction by nearest observation, or interpolate the wind *vector* (u, v) and recover direction. This measurably changed a result: `corr(HR residual, headwind)` came out **+0.076** carrying direction by nearest observation and **+0.052** interpolating it linearly.

---

## 9. FIT parsing gotchas

Everything in this section was found by reading real files from two Garmin watches (a Forerunner 245, GPS-only altitude, no running dynamics; and a Forerunner 265, barometric altitude, running dynamics) and the Strava iPhone app. Each finding names the method that settled it, so it can be re-tested when the hardware changes.

### 9.1 THE SECOND ALTITUDE CHANNEL, and the join that has no key

**`gps_metadata` carries `enhanced_altitude` and `enhanced_speed` and nothing else** — no timestamp, no position — on both watches. It cannot be joined to `record` on any field. It can only be joined on **file order**, and the rule is established by measurement rather than assumed:

> **Each record takes the LAST `gps_metadata` frame written BEFORE it.**

**How that was settled.** On the FR245 the two channels are demonstrably ONE series (identical minimum *and* maximum over a whole run), so the correct pairing is whichever reproduces the record's own altitude. On the reference file:

| pairing rule | exact matches |
|---|---|
| **last frame BEFORE the record** | **97.5%** |
| first frame AFTER the record | 36.9% |

The fix is written, then the record derived from it. On the FR265 the same rule gives 3.7% — because there the two channels are genuinely different, which is the finding, not a failure of the join.

**What is dropped, and it is not silent.** A `gps_metadata` frame with no record after it is unused — about 1% of frames on an FR265 file, GPS epochs logged while the activity was being saved. On Smart Recording files the several fixes between two records collapse to the newest, which is the grid the record channel is already on (roughly four frames per record used).

**Why it is stored per record rather than summarised.** On a barometric watch this is the ONLY altitude channel not derived from pressure, so §15.1's "is the watch independent of our DEM" question is unanswerable without it. It is stored as `altitude_gnss_m`.

### 9.2 ALTITUDE PROVENANCE IS MEASURED, NOT LOOKED UP

`ALTITUDE_SOURCE_BY_DEVICE` was the primary evidence and it **failed on the first file of a new device**: the watch reports `fr265_large`, the map knew `fr265` / `fr265_music` / `fr265s`, and every record would have been stored `altitude_source='unknown'` — silently, in the direction of "we don't know", for every future device too.

Provenance is a property of the DATA and is now derived from the two channels:

| measurement | verdict |
|---|---|
| identical to the GNSS channel (≥ 90% of records) | **`gps`** — one series, not two |
| diverging, and **smoother** than GNSS | **`barometric`** — a low-pass channel fed it |
| diverging, and rougher | **`unknown`** — say so; do not guess |

Measured: FR245 identical fraction **0.976**; FR265 **0.037**, with a standard deviation about 20% lower than the GNSS channel's. Any threshold from 0.1 to 0.9 separates them; 0.90 is a **declared editorial threshold**, said to be one.

**The device map is kept as a CROSS-CHECK.** `altitude_source_device` is stored beside the measured answer and `altitude_source_agrees` compares them, because a disagreement is a finding — a missing map entry, or a firmware change in what the watch feeds `enhanced_altitude`.

**`altitude_source_basis` says where the label came from**: `measured`, `device_map`, or `declared`. **"Could not be measured" and "measured and disagrees" are different facts.** An indoor run has coverage 0.0, no position, no GNSS channel and no altitude at all; such runs fall back to the map with `agrees = NULL`, rather than reading `unknown` and appearing to contradict it.

### 9.3 THE SPLIT TAXONOMY STOPPED BEING A PARTITION, and it doubled the silver layer

**The FR265 writes an `interval_active` split spanning the WHOLE RUN**, on top of the `rwd_run` / `rwd_walk` / `rwd_stand` partition. Every time-range join from `records` to `splits` without a `split_type` filter therefore matches each record **twice**.

**What that did, measured on the first FR265 file before the fix:**

- `records_enriched` held **exactly twice as many rows as distinct timestamps** — a doubled series;
- the doubling inserted a zero-distance, zero-time step between every real pair, so §15.4's quality flags fired on **99.99% of records** (all but one);
- `grade_binned_pace` returned **no bins at all**, because every record was excluded as a quality artifact;
- validation's `split_distance_consistency` fired at "100.0% apart" — the splits summed to exactly twice the session distance — on a file with nothing wrong with it.

**None of those look like a parsing bug.** They look like a run recorded badly.

**The fix is in three places and one guard.** The silver build and the two unfiltered `LEFT JOIN splits` in `drift_onset` and `primitives` now restrict to `split_type LIKE 'rwd\_%'`; the validator sums the partition and **names** any non-partition split it excluded; and the silver build asserts one row per timestamp and **raises** if that ever stops being true, because a doubled series does not look broken.

- **GPS is in semicircles.** `degrees = semicircles × 180 / 2^31`. Classic first-day bug.
- **Cadence: `steps_per_minute = (cadence_raw_rpm + fractional_cadence) × 2`. RESOLVED.** `cadence` in a running FIT record is one-leg (revolutions per minute), and the obvious alternative formula is subtly wrong. No manually counted run was needed; the Garmin API summary already carried the oracle.

  **The doubling is confirmed outright:** `maxRunningCadenceInStepsPerMinute == maxDoubleCadence` on **every** activity in the cached API listing. Garmin's own field naming says the steps/min figure *is* the doubled cadence.

  **The two candidates are separated by the MAX, not the average — by PARITY.**

  | | A: `(cadence + fractional) × 2` | B: `cadence × 2 + fractional` |
  |---|---|---|
  | reachable values | `2c` or `2c+1` — **every integer** | `2c` or `2c+0.5` — **odd integers impossible** |
  | avg vs oracle, time-weighted, FR245 corpus | median **+0.063** spm, SD 2.238 | median −0.295 spm, SD 2.240 |
  | max, exact integer match | **~68% of activities** | ~24% |

  **More than half of Garmin's max values on the FR245 are ODD integers. Candidate B is arithmetically incapable of producing an odd integer, so it is refuted rather than merely outperformed.**

  **The average alone cannot settle it**, and that is worth keeping: the two medians differ by 0.36 spm and the SDs match to three decimals. Worse, on "within 1 spm" **B scores better (~85% vs ~76% of activities)** — judging on the average alone selects the wrong formula.

  *Residual, fully explained.* The three worst rows (+11.09, +7.03, +6.41 spm) are exactly the three activities carrying >100 s of sample gaps over 20 s, which the `dt BETWEEN 1 AND 20` weighting drops. A long run with no gaps over 20 s matches at **+0.04 spm**. About three quarters of activities fall within 1 spm and over 90% within 2.

  **RE-VERIFIED ON THE FR265, and the original argument does NOT transfer.** The parity argument rests on Garmin's max values being odd; the first FR265 file reported an **even** max, so parity cannot discriminate on it at all. The **average** does, against the API's `averageRunCadence`:

  | candidate | error of the mean over all records vs the API |
  |---|---|
  | `(cadence + fractional) × 2` | **−0.0029** spm |
  | `cadence × 2 + fractional` | −0.3438 spm |

  A 100× separation, on the device where parity is silent. **The formula holds across the hardware change**, confirmed by a different argument from the one that established it — which is worth more than the same argument twice. **A hardware change re-tests a resolved question.**

  *Reproducing Garmin's basis requires TIMER-time weights, not moving-time ones.* `averageRunningCadenceInStepsPerMinute` **includes zero-cadence records despite its name** — restricting to non-zero biases it by several spm (+3.64 in the measurement). So our own "mean running cadence" (`rwd_run`, cadence > 0, moving time) is a **different quantity by design** and must never be reconciled to Garmin's figure. `record_weights(..., paused=[])` is what makes Garmin's basis expressible; the test in `tests/test_basic.py` pins the median at <1 spm.

  *Consequence for `records`:* `cadence_raw_rpm` stays raw one-leg in bronze (§3). steps/min is derived at read time by `src/trail_factors/analysis/basic.py`, which is the single place the formula lives.
- **Prefer "Every Second" recording** over Smart Recording. Smart Recording samples at variable intervals and destroys per-second work. The code does not assume either: `sampling_regime` is detected per file from the observed median record gap.
- Prefer `enhanced_altitude` / `enhanced_speed` over the legacy fields.
- **Optical HR cadence lock:** wrist HR sometimes latches onto step rate. Flag any window where HR ≈ cadence within a few bpm for 30+ seconds. A chest strap eliminates this and is recommended for this kind of analysis.
- **HR lag: longer than the usual rule of thumb. PROVISIONAL.** Never correlate instantaneous HR with instantaneous grade — shift or convolve first. Measured by detrended cross-correlation of HR against Minetti GAP on one Smart Recording run: the correlation **peaked at ~41 s** with a plateau over roughly 34–49 s, and was noticeably lower at the commonly cited 20 s. Restricting to `rwd_run` samples moved the peak to ~37 s. So the working figure is **~35–45 s, not 20–30 s** (`HR_LAG_S` = 40). *Caveats:* ~4 s Smart Recording sampling caps lag resolution at ~4 s, both series were linearly detrended first (the drift would otherwise dominate), r is modest, and this is one run. **Re-measure on 1 Hz data before treating it as settled.**
- **Do not use a GPS-only watch's altitude field for anything.** The FR245 has no barometer; its altitude is GPS-derived and unreliable. See §5a. (A barometric watch is a different instrument; §9.2 says which one a file came from.)
- **Smooth the GPS track before DEM lookup**, then smooth the resulting elevation before differentiating. Differentiating raw per-second elevation produces ±40% grades that are pure noise regardless of source. Rolling median, then Savitzky–Golay or a ~20–30 m distance-window smooth.
- Honest expectation: "exact grade for this specific hill" is not achievable per-second. What *is* achievable — and is actually better — is a stable, repeatable estimate over a ~25 m window that returns the **same value every traversal**, because it comes from fixed terrain rather than a drifting sensor.
- **Running dynamics fields will be null** on the FR245 without an accessory. The schema accepts them as nullable, so a device or strap that supplies them requires no migration.

**Dual-source (Strava iPhone) gotchas.** All verified against a watch/phone file pair recorded on the same run.

- **Strava splits one sample across TWO `record` messages.** Roughly half the record messages carry only `{timestamp, distance}`, the other half carry `{timestamp, position_lat, position_long, enhanced_altitude, enhanced_speed, speed, gps_accuracy}`, and a couple carry only `{timestamp}`. A parser that emits one row per record message produces twice as many rows as samples, all half-empty. **Merge on timestamp before use.** Garmin does not do this — one message per sample.
- **Duplicate timestamps exist.** Under half a percent of positioned Strava timestamps carry two distinct positions (sub-second sampling truncated to 1 s). Deduplicate explicitly. **Sorting by `(timestamp, lat, lon)` instead of timestamp alone reorders those duplicates into a zigzag and inflated the computed path by ~43 m** — sort by timestamp only, with a stable sort.
- **`file_id.time_created` is NOT activity start on Strava files.** It is file-export time — about 10 minutes after the run ended on the measured file. Garmin's happens to equal start time; Strava's does not. **Always use `session.start_time`.**
- **Strava files have no `serial_number`.** `file_id` carries only `type`, `manufacturer='strava'`, `product=101`, `time_created`. The device identifier is a `device_model` such as `'iPhoneNN,N'` — a *model* string, not a unique device ID. `ingest_ledger.device_serial` is therefore null for phone files.
- **Strava's device fields are FIT developer fields**, declared by `field_description` messages under `developer_data_id.application_id = b' com.strava.ios '` (space-padded; strips to `com.strava.ios`): `device_model`, `device_manufacturer`, `device_os_version`, `mobile_app_version`, `activity_type`, `autopause_enabled`. **Caveat: `fitdecode` 0.11.0 surfaces these inline in `frame.fields` exactly like standard fields, and its `FieldData` exposes no `is_dev` attribute.** So they are *not* silently dropped by our parser — the opposite risk applies: they look standard, and the only way to know they are not is to read the `field_description` messages. Do not assume a field present on `device_info` is standard FIT.
- **Pair on a tolerance window, never exact equality.** On the measured pair Strava started **2 s before** Garmin and ended **3.3 s before**. Match on start-time tolerance **plus** time-range overlap.
- **Do not ingest Strava `lap` messages.** Both laps in the measured file were corrupt duplicates of each other: `total_elapsed_time` exceeded `total_timer_time` by ~10 minutes of phantom elapsed time, and the second lap started ~10 minutes *after* the activity ended (the export-time offset again). Garmin's laps were internally consistent (elapsed == timer, summing to the session total). Phone laps carry no usable segmentation.
- **`local_tz` is derivable** from `activity.timestamp` vs `activity.local_timestamp`. Present and consistent on Garmin and Strava alike.
- **`cycle_length16` is a trap.** Present in nearly every Garmin record (over 99%) and **zero in every single one** — its only distinct value is `0.0`. It is *not* stride length. A naive parser produces a `stride_length` column that looks almost fully populated and is entirely zeros. Derive stride as `speed ÷ (steps per second)` instead, and treat the result as derived, not measured (§7).

**Garmin Connect API gotchas** (measured in the first audit against an activity whose FIT we hold).

- **`startTimeGMT` equals `session.start_time` exactly and is safe to use.** Unlike Strava's `file_id.time_created` (which is export time, ~10 min after the run), the Garmin API's GMT start is the real activity start. `duration`, `distance` and `startLatitude` also matched the FIT exactly.
- **`duration` from the API is `total_timer_time`, NOT `total_elapsed_time`.** The two coincide only on runs with no pauses, which is why the first calibration appeared to match exactly. On a run with a long pause the FIT's elapsed time exceeded its timer time by about 9 minutes, and the API's `duration` was the timer value. Verified across four files. `activities` stores both: `duration_s` (elapsed, wall clock) and `timer_time_s` (timer, == API `duration`). **Compare oracles against `timer_time_s`.** Doing so took the oracle-duration mismatches to zero across the corpus.
- **`movingDuration` ≠ `duration`.** On the calibration activity they differed by a **59 s pause gap**. Which one is correct depends on the question, and the difference matters for time-weighted aggregates: weighting by wall-clock includes paused time that carries no movement. Store both.
- **A stray impossible speed is a GPS RE-FIX artifact, not a driving event.** One activity carried a single **19.7 m/s (44 mph)** sample at **46% elapsed**, not in the tail. Its neighbourhood: distance frozen for **59 s** with cadence 0 and no position, then the fix returns and the jump from the last known point yields one absurd speed. **HR sat at a standing level throughout — standing, not driving.** This is the general signature of a re-fix after a stationary GPS dropout and **it will recur**: frozen distance, absent position, then a single large jump. Exclude the sample; do not infer a vehicle from it. A corpus scan found **no driving anywhere** — every activity ends with running-level HR and cadence. (A genuine car ride at the end of a file is a different case, handled by the confirmed window, §4.3.)
- **`speed_mps` is FUSED, SMOOTHED, LAGGED and GRADE-BIASED. Do not use it for grade-dependent analysis.** Garmin fuses GPS with the wrist accelerometer, and the accelerometer-to-speed mapping is calibrated on flat running. Measured against speed derived from consecutive GPS positions, pooled over a set of trail activities:

  | grade bin | watch ÷ GPS |
  |---|---|
  | −99..−12% | 0.989 |
  | −12..−6% | 0.960 |
  | −6..−2% | 0.956 |
  | −2..+2% | 0.955 |
  | +2..+6% | 0.990 |
  | +6..+12% | 1.024 |
  | **+12..+99%** | **1.090** |

  **CHORDING CORRECTION — and it isolates the finding rather than removing it.** GPS-derived speed uses straight-line displacement between samples, which understates path length on a curving trail (§14b). Measured against Strava's ~4× denser 1 Hz trace of the same run, Garmin's Smart Recording path was **5.0% short overall** — and the loss is **NOT uniform**: 1.081 on steep descents falling to 0.998 on steep climbs. Correcting each bin by its own chording factor:

  | grade bin | watch ÷ chorded GPS | chording factor | **watch ÷ true** |
  |---|---|---|---|
  | −18..−12% | 0.989 | 1.081 | **0.915** |
  | −12..−6% | 0.960 | 1.067 | **0.900** |
  | −6..−2% | 0.956 | 1.056 | **0.905** |
  | −2..+2% | 0.955 | 1.044 | **0.915** |
  | +2..+6% | 0.990 | 1.032 | **0.959** |
  | +6..+12% | 1.024 | 1.037 | **0.988** |
  | **+12..+99%** | 1.090 | 0.998 | **1.092** |

  **Flat and descending ground collapses to a uniform ~0.90–0.92; only the climb bias remains, at +1.09.** The apparent 4.5% flat offset was chording in our own reference, not a watch error. **The real finding is a ~19 percentage-point grade-dependent swing**, robust across two independent references (+14 pp against Garmin's own chorded GPS, +20 pp against Strava 1 Hz), in the predicted direction: on steep climbs, where running gives way to power hiking, arm swing does not slow in proportion to actual speed, so the wrist over-reads.

  The residual uniform ~0.91 is **not** established as a watch error: §14b holds that neither raw path is ground truth — sparse sampling under-reports via chording and 1 Hz over-reports via jitter — so the absolute level is uncertain while **the grade-dependence is the robust part.**

  The watch's speed is also **5× smoother than GPS-derived speed** (median adjacent |Δ| 0.028 vs 0.151 m/s), quantised (a few hundred distinct values per run), and **lags GPS by ~2 samples (~10 s on Smart Recording)**.

  **This is the same class of finding as "do not use the watch's altitude" (§5a).** Consequences: **grade-dependent analysis must use GPS-derived speed**; §14a's cadence work is unaffected (cadence is measured, not fused, and the stride-length derivation there used speed only as a ratio); and **treadmill runs have no GPS to fall back on**, so their speed is wrist-derived with no correction available (§4.1 already excludes them from pace-based analysis for exactly this reason — this measurement is the quantitative backing for that rule).

- **Timestamps can be corrupt, and `sampling_regime` will NOT surface it.** One file carried a **52-day gap between two consecutive records**, giving a record span thousands of times its reported `total_elapsed_time`. Its median gap was a perfectly normal 5.0 s. **The median is robust to outliers by design — correct behaviour, but it means regime detection cannot be relied on to detect timestamp corruption.** It needs its own check. Two FATAL validations run before anything is written (`src/trail_factors/ingest/validate.py`): record span exceeding `session.total_elapsed_time` by more than 1.5x plus a 600 s tolerance, and any single inter-record gap above 3600 s. A file failing either is **rejected outright** — no rows, no partial ingest.
- **GPS coverage can cover only PART of a run, and nothing in the API tells you.** One activity had position on about a third of its records — **all of them in the final ~30% of the activity.** The first hour had HR, cadence, speed and distance but no `position_lat`/`position_long`, and **no `enhanced_altitude` either** (altitude appears only on positioned records). There was no dropout inside the positioned stretch (median gap 5 s); GPS simply was not fixed for the first hour.
- **`hasPolyline: true` and a present `startLatitude` do NOT mean GPS covers the activity.** On that run both are set, yet two thirds of it has no position. Worse, **`startLatitude` is the first KNOWN position, not the start location** — there it was where the athlete was an hour in. Any clustering or location filter built on the API listing inherits that error.
- **Distance recorded during a GPS-less stretch is wrist/accelerometer-derived**, with the same uncalibrated-speed caveat as treadmill running (§4.1). So `session.total_distance` matching the API exactly proves only that both read the same field — it is **not** a validation of GPS distance.
- **`timeZoneId` is a Garmin-internal integer, not an IANA zone name.** Do not feed it to a timezone library. Derive the offset from `activity.timestamp` vs `activity.local_timestamp` in the FIT, as above.
- **`elevationGain` from the API is DEM-corrected, the FIT's is not** (§5a-bis). The `isElevationCorrected` flag says which.

- **Timestamps are stored as naive UTC, never `TIMESTAMPTZ`.** FIT records UTC already; the local offset lives in `activities.local_tz`. Storing tz-aware values makes DuckDB require `pytz` in *every* query path, including interactive sessions. **Do not convert these columns to tz-aware.**
- **Unweighted aggregates over `records` are WRONG on Smart Recording files.** Sampling is denser where values change and sparser where they are steady, so a plain `avg()` over-weights the variable sections. `avg(heart_rate)` happened to match the session message on an early file **because that run was homogeneous** — it will not hold on interval sessions or runs with long steady blocks. **Every mean over `records` must be time-weighted or distance-weighted.** This is the aggregation form of "never index by row position as a proxy for time".
- **Split duration must come from `total_timer_time`, NEVER from `end_time - start_time`.** Record timestamps are 1-second resolution, so end-start truncates sub-second split boundaries. The error is **predominantly one-directional**: about 97% of split-type groups and about 97% of individual splits come out LOW, so it does not average away, it accumulates. Measured against Garmin's own `splitSummaries`: `total_timer_time` reproduces Garmin's per-type totals **exactly on every group** (max diff 0.000 s); `end_time - start_time` matches **none** of them (mean −8.0 s, worst −46 s). This was not cosmetic — it fed `activities.moving_time_s`, the PRIMARY moving time, understating it by a mean of 18 s and by up to **~2%** on the worst run, which inflated moving pace by the same proportion. **A one-directional bias that quietly shrinks run time is exactly the class of error that survives review**: every individual value looks plausible, and the sign is only visible in aggregate. `splits.duration_s` is retained for provenance; do not sum it. (Laps had the identical bug and the identical fix: `laps.total_timer_time` is the lap duration, `laps.duration_s` is provenance.)

- **Strava records carry no cadence at all.** No `cadence`, no `fractional_cadence`, no step or stride field of any kind. The only phone-side cadence figure in the file is `session.total_strides`, which differed from Garmin's by a few percent. Cadence cannot be cross-validated between devices per-record — only in aggregate.

---

## 10. Build stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Ecosystem |
| Env / deps | `uv` | Fast, single tool, no venv fiddling |
| Database | DuckDB | Single file, embedded, columnar, native Parquet, spatial extension, zero ops |
| Dataframes | Polars | Fast, good DuckDB interop |
| FIT parsing | `fitdecode` | Reads standard and developer fields (see §9 for the developer-field caveat) |
| Auto-download | `garminconnect` (unofficial) | Optional; removes manual export |
| Configuration | YAML via PyYAML `safe_load` | One athlete file, validated strictly on load (below) |
| Chat interface | MCP server (`mcp`), **stdio transport only** | A thin wrapper over the same functions the CLI calls (§16) |
| Agent | Claude Code | Runs locally with filesystem access and executes Python directly against the DuckDB file |
| Version control | Git | Non-negotiable safety net |

**No API layer between the editor and the agent** — Claude Code runs locally and executes Python directly against the DuckDB file. The MCP server is the only interface built for a chat client, and it binds no host or port: a client launches it as a subprocess and talks over stdio.

**DuckDB caveat:** single writer at a time. Don't run ingest and an interactive query session against the same file simultaneously. Not a problem at this scale, just a thing to know. The database path is `TRAIL_FACTORS_DB` (default `data/ultra.duckdb`).

**Athlete configuration.** Everything specific to one athlete lives in `config/athlete.yaml` (gitignored; `TRAIL_FACTORS_CONFIG` points elsewhere). `config/athlete.example.yaml` is the committed template, one comment per field: `clusters`, `projection_crs`, `reference_run_garmin_id`, `unresolved_peak_run`, `hr_above_default_bpm`, `time_above_ladder_bpm`, `route_fingerprint_accepted`, `dem_tile_suffix_guess`, `goal_event`. **It is validated strictly on load**: an unknown key, a wrong type or an out-of-range value is an error naming the field and the line, never a silent default. A missing file means defaults. `projection_crs` is optional: set, it wins everywhere; unset, each run's UTM zone is derived from its own positions and labelled as derived (§5a.1-quinquies).

**Garmin credentials.** Our code never writes the password to disk. The OAuth token store lives **outside the repo** (default `~/.garminconnect`, overridable by `TRAIL_FACTORS_GARMIN_TOKENSTORE`), and a path inside the repo is refused rather than trusted to `.gitignore`.

### 10.1 ACCOUNT-HISTORY LOOKUPS — separate from the corpus, and kept so

**The problem.** An athlete usually has Garmin history predating the corpus, and some questions need it — the clearest being "what is the true HR ceiling?". Downloading every FIT file to answer it would be hundreds of heavy requests for a handful of numbers.

**The design answer is the LIST endpoint.** Activity summaries already carry `maxHR`, and they are small JSON responses. A sweep pages the whole account with `get_activities(offset, limit)` and stores seven fields per activity: date, type, duration, distance, maxHR, avgHR, id. The sweep is `garmin-list history`; it writes the summaries to the lookup directory (`TRAIL_FACTORS_LOOKUP_DIR`, default `data/lookup/`), where `src/trail_factors/lookup.py` and `hr-ceiling` read them. The rules below are the design for any lookup data.

**THESE ARE NOT CORPUS DATA, AND THE SEPARATION IS STRUCTURAL.** They live in the lookup directory, which exists for exactly this: things fetched to decide what to fetch next.

- **`maxHR` in a summary is a SINGLE SAMPLE**, which §6.0 records as a spike candidate — a maximum selects for positive measurement error. A summary carries neither records nor a cadence series, so it **cannot be checked for cadence lock or quality flags**. It cannot answer the question; it can only say where to look.
- **Nothing is ingested, joined to a run, or read by anything that computes.** A test walks `src/trail_factors/analysis`, `src/trail_factors/ingest` and `src/trail_factors/transform` and asserts none of them imports the module, with a positive twin asserting the CLI does.
- **NO COORDINATES, BY CONSTRUCTION.** The API returns `startLatitude`/`startLongitude` on every activity. The projection keeps seven fields and drops the rest **at the boundary**, so the stored file cannot hold a position even by accident. A test asserts no stored value is coordinate-shaped.
- **Gitignored**, because it is regenerable from Garmin — like everything under `data/`.

**THE WORKFLOW IS THREE STEPS AND THE MIDDLE ONE IS THE ATHLETE'S.** Sweep the summaries → read the distribution and pick candidates → download those in full and let §6.0's `max_hr_30s` decide. `hr-ceiling` reports the middle step: the highest maxHR with its date and activity, the distribution with an **isolation test** (how many activities sit within 5 and 10 bpm of the top — one reading alone at the ceiling is what a spike looks like), the same restricted to the **4.5–5.5 km band** where a maximal 5K effort would show a real ceiling, and the fastest eight in that band **ranked by pace, not by maxHR** — ranking by the untrusted figure would select for the artifact.

**The first sweep's lesson: check the corpus before downloading.** The highest maxHR in the whole account history turned out to be a run already in the corpus and already through §6.0's 30 s ceiling and cadence-lock checks, with several other activities within 10 bpm of it. The history **confirmed** the ceiling rather than raising it, and showed the figure was repeated rather than isolated — no download was needed for the top figure.

---

## 11. Repo conventions

```
trail-factors/
  CLAUDE.md              # project brief, data model, conventions — agent reads every session
  SPEC.md                # this document
  README.md
  .env.example           # every TRAIL_FACTORS_* environment variable, documented
  config/
    athlete.example.yaml # committed template; copy to athlete.yaml (gitignored)
  docs/                  # design notes (proposed metrics)
  data/                  # GITIGNORED AS A WHOLE
    raw/                 # .fit files, immutable, never edited
      garmin/            # watch exports — filename embeds the Garmin activity ID
      strava/            # iPhone app exports
    dem/                 # USGS 3DEP tiles (*.tif)
    athlete/             # athlete-supplied state — NOT rebuildable; back it up
    lookup/              # account-summary lookups (§10.1), never ingested
    golden/              # private golden query outputs for tests
    ultra.duckdb         # rebuildable from raw/ + athlete/
  pyproject.toml         # package `trail-factors`; commands trail-factors, trail-factors-mcp
  src/
    trail_factors/       # the importable package (installed editable by `uv sync`)
      paths.py           # every path, repo-root anchored, env-overridable
      config.py          # loads and validates config/athlete.yaml
      ingest/            # FIT parsing, dedup, validation, weather fetch, athlete state
      transform/         # DEM lookup and fetch, silver layer, segment matcher
      analysis/          # primitives from §6, report, selection
      cli/               # one module per command group (`trail-factors`)
      mcp_server.py      # stdio MCP server `trail-factors` (`trail-factors-mcp`)
  tests/
    fixtures/            # small synthetic fixtures only
    golden/              # capture/check script for the golden queries
```

- `CLAUDE.md` is the spine. Keep it current; it's the highest-leverage file in the repo.
- **Paths are repo-relative defaults, each overridable by an environment variable** (`src/trail_factors/paths.py`, documented in `.env.example`): `TRAIL_FACTORS_DB`, `TRAIL_FACTORS_DATA_DIR`, `TRAIL_FACTORS_RAW_DIR`, `TRAIL_FACTORS_DEM_DIR`, `TRAIL_FACTORS_ATHLETE_DIR`, `TRAIL_FACTORS_LOOKUP_DIR`, `TRAIL_FACTORS_JOBS_FILE`, `TRAIL_FACTORS_CONFIG`, `TRAIL_FACTORS_GARMIN_TOKENSTORE`, `TRAIL_FACTORS_GARMIN_AUTH_STATE`, and `TRAIL_FACTORS_GOLDEN_DIR` for tests. A relative value is resolved against the repo root, never the current directory — a process launched from an arbitrary cwd must not create a phantom empty database.
- **The whole `data/` directory is gitignored, including `data/athlete/`.** Raw files contain home GPS coordinates; derived data is rebuildable; athlete-supplied state is personal and **cannot be regenerated from any source**, so it must be backed up outside the repository.
- **Never rename a raw file.** Original export names are preserved as-is. The Garmin name embeds the Garmin activity ID — a real external key back to Connect. Identity is the SHA-256 of file contents, not the filename; the `garmin/` and `strava/` folders are human organization only and must never be used to infer `source` (see §4).
- Git commit after every working milestone. Commit *before* asking the agent for a risky refactor.
- **Test the math, not everything.** Smoothing, grade calc, segmenter, τ fit, semicircle conversion. These get refactored and silently break.
- **Tests that need the corpus run only when `TRAIL_FACTORS_DB` is set**, and skip with a stated reason otherwise. **No test creates a database file**; tests that write athlete state redirect the path at a temporary directory.
- Every derived table carries `calc_version`. Reprocessing is a first-class operation, not a rescue procedure.

---

## 12. Build order

Thin vertical slices. Each one works end to end before the next starts.

| # | Milestone | Done when |
|---|---|---|
| 0 | Parse one FIT file → DuckDB → run one SQL query | You can `SELECT avg(heart_rate) FROM records` **and** `activities.sampling_regime` is populated from the observed median record gap |
| 1 | Ingest CLI: all files, content-hash dedup, idempotent re-run | Re-running ingest twice changes nothing |
| 1.5 | DEM elevation lookup (§5a) — replaces watch altitude | Known climb returns plausible, repeatable gain across two traversals |
| 2 | Silver layer: DEM elevation, grade, GAP, quality flags, **hill detection** | **Resolution invariance** (§12.2), plus a grade-binned HR/pace table for any run |
| 3 | Weather auto-fetch + annotation CLI | **DONE.** Every run has per-record dew point; free-text notes (`data/athlete/notes.json`) restore into `annotations` on every schema ensure |
| 4 | Climb/descent segmenter | Climbs detected on a known loop match what you'd draw by hand |
| 5 | Segment library + matcher | Same hill, 10 laps, correctly grouped |
| 6 | Analysis primitives (§6) as reusable functions — **§12.5**; §6.3 is built | τ and decoupling computed for every run; each function declares its minimum `sampling_regime` and refuses to run below it (§15.4) |
| 7 | Baseline residual model | Trend chart of residual over 8 weeks |
| 8 | Coaching layer | Later. Curated gold views + tool functions only. |

**Milestone 0 is the whole test.** If one file parses, cleans, and produces a grade-binned table end to end, everything after is additive.

**Intended ingest workflow — prompted, not scheduled.** This affects design, so it is recorded here rather than discovered later.

- **NO scheduled trigger.** The athlete prompts it after a run, once the activity should have reached Garmin. A daily cron is overkill for one person's runs, and it would fail silently on the days it had nothing to do.
- **On that prompt:** fetch new activities, ingest them, run validation, and report anything flagged **conversationally** — *"this run has a few minutes of no movement at the end, forgotten stop?"* — rather than making the athlete hunt for it. The CLI stays the mechanism; the conversation is the interface. **A file that did not ingest at all is the exception**: confirming its window is CLI-only (`confirm-file`, §4.3), and the rejection names that exact command.
- **Open question: how soon after a run does an activity become available through the API?** Worth measuring so the wait is known rather than guessed.

### 12.2 Milestone 2 — the silver layer, planned before building

**Hill DETECTION moves to silver; hill MATCHING stays at milestone 5.** §5.0 makes the seam clear: detection is per-traversal and needs only DEM elevation plus cumulative distance, both available at this point; matching groups traversals into canonical hills and needs the matching work and a labelled test set. Detecting early also **de-risks milestone 5**, which would otherwise land an unvalidated detector and an unvalidated matcher together — and it gives the other primitives their natural unit, since a hill is the scale at which grade, GAP and τ mean anything.

**ONE grade window, 25 m, recorded in the row. Cumulative gain is NEVER stored per record.**

The two are different kinds of quantity:

- **Grade is a local physical quantity.** It acts through Minetti's cost-of-gradient curve, measured at the scale over which a runner's mechanics respond to a slope — tens of metres. §9 already says ~25 m. **That is a physiological scale, not a reporting preference, so one window is correct.**
- **Cumulative gain is a reporting choice with no true value.** §5a-bis measured gain per mile falling steeply as the smoothing window grows (0 m → 100 m → 250 m), and AllTrails, Strava and Garmin each report a different, defensible figure for the same ground. **It is computed at gold, on demand, with the window stated in the output** — never stored as a number that outlives its window.

`grade_window_m` is stored alongside `grade_pct` so a change is detectable in the data, the same discipline as `garmin_selfeval_scale` stored beside every Garmin self-evaluation (§16.6). Not several windows side by side: silver is rebuildable, so recomputing is cheap, and multiple columns invite "which one is real".

**Outside a terrain-usable block, grade is NULL — not a flagged number.** A flagged number gets used: some future query averages `grade_pct` without checking the flag and silently produces a plausible wrong answer. **NULL fails loudly; a flag fails quietly.** Per §3.1 the row still exists with HR, cadence, speed and distance fully valid.

**A record also needs its full ±12.5 m window inside the block**, otherwise the first and last records of every block carry a half-computed grade. **Measured cost: about 1% of records, ~2.7 per block end.**

**ACCEPTANCE TEST: RESOLUTION INVARIANCE.**

Re-sample the same path from progressively coarser DEM postings and count prominence hills. Measured on the reference run:

| DEM posting | 1 m | 3 m | 9 m | 21 m |
|---|---|---|---|---|
| hills ≥8 m | **8** | **8** | **8** | **8** |

**A feature that survives 21× coarsening is in the terrain, not in the method.** If the lookup were producing artefacts, coarsening would destroy them.

Why this and not agreement with Garmin's profile: **Garmin's shape is corrupted, not merely offset** (§5a-bis.3 — 18 hills to our 8, prominences inflated 2.5×, offsets to 190 m), and its corrected elevation is itself a DEM lookup, so agreement would be partly circular (§15.1). **A test a correct implementation cannot pass is worse than no test.**

**Blind spot, stated explicitly: resolution invariance cannot catch a systematic error affecting all resolutions equally** — a wrong CRS shifting everything by a constant would pass. **The two external checks of §5a-bis.1 (an independent elevation floor and a published trail profile) are the absolute anchors that cover exactly that**, which is why they remain load-bearing rather than being one-off curiosities.

**Secondary diagnostic: geographic hill consistency across runs.** A hill is at a *place*, so shortcuts and different lines do not move it. Across the home-trail activities: median offset **12 m**, 59% within 20 m — but with a **p90 of 186 m**, because the "did this run go there" filter (a track passing within 40 m) is crude: a run passing to the side never crosses the summit and matches some other hill. **A diagnostic, not a gate, until that filter is better.**

**RESULT — the GAP test FAILS, and the failure is informative.** Measured on the reference run with HR lagged 40 s (correlation of HR with each speed series):

| | all records | `rwd_run` only |
|---|---|---|
| watch speed | +0.463 | **+0.522** |
| GPS speed | +0.416 | +0.466 |
| GAP (watch) | +0.457 | **+0.400** |
| GAP (GPS) | **+0.477** | +0.437 |

**GAP beats raw speed in exactly one of four comparisons**, and within running samples it is clearly *worse* than raw speed. The version committed at milestone 2 passed only marginally (0.457 vs 0.463 on a slightly different sample) — **a pass that did not survive a harder look.**

**Why it fails, and it is not a bug.** Minetti's curve assumes a runner holds effort roughly constant and slows on gradient. Where HR stays near-flat across the whole grade range (§14a) — as it does for a runner who walks or hikes climbs rather than running them harder — GAP multiplies speed by the metabolic cost ratio, so wherever effort is held down on climbs it **predicts high effort precisely where the runner has chosen low effort.**

**This is direct evidence for §6.1's instruction to refit the curve personally.** The population Minetti curve does not describe the athlete's *observed* behaviour on gradient. The cause of the flatness is not established by the data (see §6.1's undetermined-flatness caveat); an earlier draft attributed it to pacing strategy and was softened for asserting a cause it had not checked. **Until refitted, GAP must not be treated as validated**, and the milestone-2 gate rests on resolution invariance alone.

**Secondary: GAP must beat raw pace at predicting HR**, using **HR lagged 40 s** — the centre of §14b's measured lag plateau rather than its single-run peak, so it is not tuned to one run. At ~5 s median sampling that is ~8 samples. If HR correlates no better with GAP than with raw speed, GAP is a transform that adds no information.

**The historical backfill is higher priority than "more data" suggests.** An account's history of past runs (§14c) is not merely a bigger corpus — it is **the temperature spread the §2 residual model needs** to separate heat from fitness, and it makes that comparison available without waiting out a season. Treat the backfill as a prerequisite for milestone 7, not as optional volume. Note the same span brings months of fitness change, so the model must carry a fitness/date term alongside temperature.

**`sampling_regime` is bound to Milestone 0, not Milestone 6.** The §15.4 gate that decides which analyses may run reads `activities.sampling_regime`, and the *declaration mechanism* in `src/trail_factors/analysis/` is correctly ordered at Milestone 6 — but the **column it reads must be populated at ingest**. Computing the median gap between consecutive `record` timestamps is a few lines during parsing; adding it later means **reprocessing every file**. Populate it at Milestone 0 even though nothing consumes it until Milestone 6.

---

### 12.3 Milestone 5 — the segment matcher, planned before building

Planned against measured data, the way §12.2 was. **BUILT — `src/trail_factors/transform/matcher.py`, `uv run trail-factors match`, tests in `tests/test_matcher.py`.** The plan below is as written before building; results and one correction follow at the end. (The segment unit later changed from a prominence hill to a slope — §12.4 — but the matching principles here carried forward.)

#### 1. The match key is a PLACE. Fréchet is a leftover from the polyline design.

§5's Fréchet design was written for **climb polylines**. §5.0 replaced those with **prominence hills**, and that changes what a match is.

**A hill is a place** — a peak location, a peak elevation, a prominence. All three are properties of terrain; none is a property of a traversal. The match key is exactly those three:

| signal | measured spread | role |
|---|---|---|
| **peak elevation (DEM)** | **SD 0.54 m** run-to-run (§5.3); 0.19–0.66 m within cluster | most REPEATABLE signal — but see the measured correction below: on this corpus it does not bind |
| peak position | cluster spread 13–31 m | primary key |
| prominence | SD 0.63–1.98 m | confirmation, deliberately looser |

**DROP FRÉCHET — it is actively wrong here, not merely redundant.** It measures whether two curves follow the same *line*, and §5.1 records that a trail runner does not: switchback shortcuts, a different line on wide trail, impromptu cuts. **Fréchet would penalise exactly the variation §5.1 calls normal**, while ignoring peak elevation — the signal §5.3 measures as stable to half a metre. Retaining it would fit the tool to the old design rather than to the data.

**Elevation source is settled.** DEM only. Garmin's peak-to-peak error is ~10 m (§5a-bis.4), roughly **20x the DEM's run-to-run SD** — substituting it would destroy the discriminator.

**NO PACE AND NO HR ANYWHERE IN THE MATCHER.** Where median HR is near-flat across grade bands (§6.2) — a runner holding effort roughly constant and letting pace absorb gradient — HR carries almost no discriminating information. §14e adds a second reason: HR is not even on a stable scale across the corpus. **Every signal in the match key is deterministic geometry**, which is also why the matcher is reproducible.

#### 2. Labelled sets, stated BEFORE thresholds

Thresholds must be fitted to data, not picked. Both label sets already exist without new annotation:

- **KNOWN POSITIVES — within-run repeat crossings.** An out-and-back crosses some hills twice in a single run: the most-repeated clusters average **about two traversals per activity**. These are the same hill *by construction*. They are identified through §6.7's record-level matched-terrain machinery, which is **independent of hill detection** — so this is not circular.
- **KNOWN NEGATIVES — distinct hills within one run.** The reference run's traversals resolve to **one fewer distinct hill than crossings** (two of the traversals are one hill crossed twice). Every other pairing within that run is a known negative: **every pair of distinct hills in that run.**

**Fitted, not picked:** clustering appeared stable across a **10–60 m radius** — the same number of large clusters at every radius in that range, with the reference run's distinct-cluster count constant throughout (corrected below). Peak-elevation tolerance follows from §5.3's measured SD of 0.54 m: **±2 m is ~4 SD**, wide enough for observed variation and far tighter than the ~10 m separating distinct hills here.

#### 3. Direction — prominence is direction-free, and §5 needs amending

Prominence does not preserve direction, and should not: that is §5.0's whole argument for using it.

**§5 originally said "an uphill and its reverse descent are different segments." That conflates two things and was amended:**

- **`segments` are hills — places, DIRECTION-FREE.** N hills give N segments, not 2N.
- **`segment_efforts` are traversals — DIRECTED.** Each carries `direction` alongside HR, τ and vertical speed.

This is already §4's schema shape. It keeps "hill 3 on lap 5" unambiguous, which is what §5 actually wants; doubling the library would make "hill 3" name two different places. **Direction is a property of the effort, not of the terrain.**

§5.3 adds independent support: **peaks reproduce 4x better than valleys** (SD 0.54 m vs 2.20 m), so the segment anchors on the **peak** and never on a saddle or a minimum.

#### 4. A run on a NEW trail must produce NO matches — the guarantee is structural

**A candidate must fall within the match radius of an existing peak.** A trail kilometres from every library hill generates no candidate at all. **This is a hard geometric bound, not a tunable threshold that could drift.**

**THE OBVIOUS TEST IS UNAVAILABLE, AND MUST NOT BE FAKED.** At the time, the other clusters had **zero hill traversals** — only the home trail's DEM tile was held (§5a.2). They would pass a new-trail test *trivially, by having no data*, which demonstrates nothing about the matcher.

**The real test is a SPATIAL HOLD-OUT INSIDE THE HOME TRAIL:** build the library excluding one hill cluster, then feed the runs that cross it. Every traversal must **create a new segment, not match a neighbouring one**. That exercises the actual failure mode — a false match to something *nearby* — which geographic distance alone would never catch.

#### 5. Acceptance test, stated before building

Stated in advance, the way resolution invariance was in §12.2.

- **GATE — no false matches under spatial hold-out.** Hold out one cluster; feed every run that crosses it. **Zero traversals may match an existing segment; all must create a new one.** This is the failure mode that matters most, so it is the gate rather than a check.
- **CORRECTNESS — the library recovers the trail.** Built from every home-trail activity, hill count must be stable across the 10–60 m radius range, and the reference run's traversals must resolve to **one fewer distinct segment than traversals**, with the twice-crossed hill matched to itself.
- **CONSISTENCY — within-segment peak-elevation SD < 1 m.** Measured at 0.19–0.63 m within clusters and 0.54 m across runs (§5.3). **A segment whose peak elevations disagree by more than a metre has merged two hills.** Needs no labelling, and catches the merge failure automatically.

#### Known unresolved before building

See *Known unresolved before building — RESOLVED* at the end of this section.

#### RESULT — as built

**The library built from the home-trail corpus passes all three acceptance criteria.** Figures below are post-review; see *Adversarial review* at the end of this section for what changed and why.

| criterion | result |
|---|---|
| **GATE — spatial hold-out** | **0 false matches**, every segment held out in turn |
| **CORRECTNESS — reference run** | **one fewer distinct segment than traversals**, the twice-crossed hill matched to itself |
| **CONSISTENCY — peak-elevation SD** | every segment under 1 m; **max 0.66 m** |

**Direction confirms the out-and-back structure without being told about it.** The heavily-repeated segments each carry both directions, in comparable numbers. Nothing in the matcher knows the trail is an out-and-back; that split falls out of the geometry.

**A MILESTONE 2 DEFECT SURFACED WHILE BUILDING THIS, and is fixed.** `detect_hills` emitted **one entry per local maximum**, so a broad summit with two tops of equal height produced TWO traversals with **identical `start_index`, `end_index` and prominence** — one stretch of trail, crossed once, counted twice. About **5% of traversals** were duplicates, inflating segment effort counts by the same amount.

Fixed by deduplicating on the `(start_index, end_index)` extent, keeping the highest peak and breaking ties on the lower index. **Distinct extents are deliberately kept**, so a genuine out-and-back re-crossing — different stretch of path, same place — is untouched; recognising *those* as one segment is the matcher's job, not the detector's.

**The reference run was NOT affected** — its traversals all have distinct extents, and the twice-crossed hill's two crossings are far apart in path distance, so the one-fewer result is a real re-crossing rather than a double-top artifact. **The library was unchanged by the fix: same segment count, same ids.** The duplicates were extra members of existing segments, never extra segments — which is itself evidence the matcher was doing the right thing with them.

**CORRECTION to this plan's threshold claim.** The plan said clustering is *"stable at 10–60 m radius"*. **That figure came from raw peak-proximity clustering and the plateau is narrower than it claimed.** Measured over the home-trail traversals — reproduce with `M.build_library(traversals, radius_m=r)` over `hill_traversals`:

| radius (m) | 10 | 15 | 20 | 25 | **30** | 40 | 50 | 60 |
|---|---|---|---|---|---|---|---|---|
| segments, relative to 30 m | 1.83 | 1.25 | 1.17 | 1.00 | **1.00** | 1.00 | 0.83 | 0.83 |
| reference-run distinct | n | n | n | n | **n** | n | n−1 | n−1 |

The real plateau is **25–40 m** on segment count and 10–40 m on the reference-run criterion; at 50 m two hills merge. **`MATCH_RADIUS_M = 30 m` sits in the MIDDLE of that plateau**, not at the 40 m edge the original claim would have permitted — chosen to sit as far as possible from either failure edge.

**MEASURED CORRECTION — the elevation and prominence gates do NOT bind on this corpus.** Removing either, or both, yields a byte-identical library: same ids, same effort counts. The closest two traversals in **different** segments are **41.1 m** apart, beyond `MATCH_RADIUS_M = 30`, so **position alone decides every grouping here.** Reproduce with `M.build_library(tr, elev_tol_m=1e9)`.

This corrects the plan's discriminator table above. **Peak elevation is the most REPEATABLE signal (§5.3, SD 0.54 m) but is not currently the discriminating one** — the home trail's hills are simply far enough apart that proximity separates them. The gates are retained as conservative insurance: a conjunction can only **split** a cluster, never merge one, so they cost nothing and would bind on denser terrain. **Stating this matters because §5.3's "strongest discriminator" claim described behaviour the code does not exhibit** — the kind of figure CLAUDE.md requires be re-derivable rather than believed.

**Thresholds as built:** `ELEV_TOL_M = 2.0` (~4 SD of §5.3's measured 0.54 m), `MATCH_RADIUS_M = 30.0` (mid-plateau, above), `PROM_TOL_M = 6.0` (~3 SD of the loosest within-cluster prominence SD), `MAX_SEGMENT_ELEV_SD_M = 1.0`.

**The suspected merge in the loosest cluster did NOT materialise.** Its successor segment has a 30 m spatial spread — the widest — but a peak-elevation SD of **0.66 m**, comfortably inside the consistency bound. **The wide spread is one hill approached along different lines, not two hills chained.** The consistency check is what settles this, exactly as the plan intended; the radius was not tightened to make the symptom go away.

**Segments are located by the REFERENCE RUN.** A `segment_id` is unrecognisable on its own, so `match` prints, per segment, the path distance at which the reference run crosses it and the order it is met — and then lists the reference run's crossings in traversal order with distance in km and miles. **That is how the athlete identifies a hill** ("the one at about 1 km", "the third on the way out"), and without it the library cannot be named at all. The reference run is athlete-specific and comes from config: `reference_run_garmin_id` in `config/athlete.yaml`.

**Segment names are athlete state.** `data/athlete/segment_names.json`, set with `name-segment <id> <name>`, held outside the rebuild path — a name that silently detached would be the `records_valid` failure class again. (As first built, a name was keyed on the content-derived `segment_id`; review finding 6 below re-anchored it to the place, and §19.3 later moved it to a run-anchored, coordinate-free store, `(run_id, direction, at_distance_m)`.)

#### Adversarial review — 22 findings survived verification

The built matcher was reviewed by five independent dimension reviewers, each finding verified by three adversarial lenses instructed to refute. 47 findings raised, **22 survived**. The substantive ones, all fixed:

**1. DUAL-SOURCE DOUBLE COUNTING (high).** `load_traversals` read every `hill_traversals` row with no `run_id` filter, so **both recordings of a dual-recorded run entered the library as separate crossings** — precisely what the `runs` table exists to prevent (§4). Worse than double counting: the two devices place the same peak up to **90 m apart**, 3x `MATCH_RADIUS_M`, so the two copies of ONE crossing landed in **different segments, in opposite directions**. The §12.3 consistency check cannot catch that — it detects merges, never splits. Fixed by picking one activity per `run_id`; the watch, because it is the only source of HR. That removed roughly 8% of traversals. **This also bounds the matcher's positional error empirically: two devices recording the same physical crossing disagree by up to 90 m, which is why segment identity must not rest on position alone at finer scales.**

**2. THE ACCEPTANCE GATE WAS SELF-CANCELLING (high).** The hold-out set was derived from the same radius being tested, so **inflating `MATCH_RADIUS_M` merged neighbouring hills into the held-out segment and removed the very traversals that would have been the false-match target.** The hold-out grew faster than the radius; the gate passed at every radius up to **4 km**. This is §4.2's "a check that cannot fire is worse than no check" in the project's most important test. Fixed by freezing hold-out membership at the shipped radius, plus a test that asserts the gate **does** fire at a 400 m radius. (A fixed geographic ball was tried first and rejected: the closest two segment centroids are 54.9 m apart, so any ball wide enough for one hill swallows its neighbour.)

**3. THE TWO MATCHING PATHS DISAGREED (high).** `build_library` grouped on a pairwise traversal-to-traversal predicate; `match_traversal` compared against the segment's **centroid and median**. Two real members of the widest segment sat 30.1 m from its centroid and 6.94 m from its median prominence — **they joined the library but then failed to match it**, which would have surfaced in production as a phantom "new segment" for a hill already in the library. Unified on one pairwise predicate.

**4. STOPPED TIME CONTAMINATED EVERY EFFORT METRIC (high).** `duration_s`, `vertical_speed_mps` and `avg_hr` used raw wall clock and weighted each HR sample by the full interval to the next — so a sample taken standing still carried the whole stop as its weight. On the one traversal overlapping a real stop this moved **avg_hr by 12 bpm** — enough to read as a physiologically distinct effort when it was a rest — and **understated vertical speed by 2.1x**, in the exact metric §6.6 trends for fatigue. Stops are still never dropped from bronze (§4.3); they are excluded from these derived rates only. The HR loop also dropped the final sample entirely (up to 6% of a span).

**5. DIRECTION WAS DERIVED FROM A COLLAPSED CHORD (medium).** Bearing came from the first and last GPS fix of the traversal. **On an out-and-back the traversal leaves near where it entered, so that chord measured 1–9 m — smaller than GPS error.** Direction labels were noise. Now measured across a ±50 m window around the peak, with `None` rather than a confident angle below a 20 m chord.

**6. NAMES DETACHED ON BACKFILL (high).** `segment_id` is founder-derived and the founder is the earliest traversal, so **ingesting a historical file re-founds the segment and changes its id** — an id-keyed name silently moves to the wrong hill. Names were re-anchored to the **place** and re-attached by the matcher's own gates, which is what §5 (amended) says a segment is, and pinned by a test that backfills a synthetic older run and asserts the name follows. (§19.3 has since replaced the place anchor with a run-anchored one, so no coordinate is stored in athlete state.)

**7. `gain_m` MIXED TWO ELEVATION SERIES (medium).** It subtracted a **raw** DEM elevation at the start from a **smoothed** peak, putting micro-terrain noise into the gain. Both ends now come from the smoothed series.

**Test weaknesses fixed:** there was **no positive assertion that the matcher ever matches anything** — the whole incremental path could be stubbed to `return None` with every test still green; the plateau tests passed if `build_library` ignored `radius_m`; the prominence gate had no test in the loosening direction; the bearing test was blind to a swapped or mirrored convention; three acceptance tests reported ok on an empty corpus. 12 → 17 matcher tests.

**Not fixed, recorded instead:** `test_no_heart_rate_or_pace_in_the_match_key` is a substring grep over source text rather than a behavioural test. It is kept deliberately — the property it guards (§12.3's "no HR, no pace") is about what the code is *allowed to reference*, which is exactly what a source check tests.

#### Known unresolved before building — RESOLVED

~~**One cluster had a 31 m spatial spread and prominence SD 1.98 m**~~ — looser than the other four large clusters. It might have been two nearby hills chained together by single-link clustering. **Surface it with the consistency test during the build rather than choosing a radius that hides it**; a threshold picked to make the symptom disappear is the §4.2 "check that cannot fire" failure in another form. Resolved above: one hill, approached along different lines.

### 12.4 Milestone 5.5 — the segment unit becomes a SLOPE

Planned before building, the way §12.2 and §12.3 were. **BUILT.** *(Renumbered from milestone 6 — this is segment-library work, and §12's build order reserves milestone 6 for analysis primitives.)* See §5.4 for why the previous unit was an artifact of the home trail's terrain.

**Design.** Prominence decides significance; grade hysteresis decides boundaries.
- **Significance** reuses `PROMINENCE_MIN_M = 8 m` twice — a slope must gain or lose ≥ 8 m to exist, and an interruption reversing < 8 m is texture, not a boundary. One threshold, two uses; no new tunable.
- **Boundaries** come from a **single-pass three-state partition** over distance: ascent (≥ +3%), descent (≤ −3%), flat. **A flat belongs to neither neighbour by construction**, and ascents cannot overlap descents.
- **Bench absorption** merges an interruption only when it is **both** short (≤ `BENCH_MAX_M`) **and** insignificant (reverses < 8 m), iterated to a fixed point.

**`BENCH_MAX_M = 400 m`, fitted from below only — and that limitation is the point.** Two attempts to fit it from the home trail both failed, for the same reason: the gap-length distribution is **unimodal from 13 m to 620 m with no natural break**, and HR barely changes across a gap, with **no trend against gap length** (HR is flat across grade, §6.2). What *is* fittable is the largest genuine bench inside a stretch that is unambiguously one ascent: **p50 74.8 m, p95 186.4 m, p99 294.2 m, max 310.9 m** over the home trail's prominence ascents. 400 m clears that by 29% and sits 7.5× under the 3 km that must split. **The upper bound comes from the requirement, not from data, because the home trail has no long flats.**

**Three design errors that prototyping caught before they shipped:**
1. **Two independent hysteresis passes produce OVERLAPPING segments** — 119 overlapping pairs, including a descent spanning 1,994 m that swallowed two entire climbs. Only a single-pass partition makes non-overlap structural.
2. **Absorption must iterate to a fixed point.** One pass left the reference run with several times too many ascents and descents; iterating brings it to the real count.
3. **Chained benches manufacture a "climb" that is mostly flat** — a 1,400 m ascent averaging **+0.9%**. Fixed with a **merge veto**: refuse any merge whose result falls below the §5 hysteresis exit grade (1%). Vetoing keeps the component slopes rather than discarding their vertical.

**THE MATCH KEY CHANGED, and the measurement forced it.** The plan said key on the two endpoints. That fails, measurably. Using the out-and-back as a labelled positive set — **the ascent/descent pairs of the SAME face within a SINGLE run**:

| | median | p90 | max |
|---|---|---|---|
| low endpoint distance | 15.5 m | 290.9 m | **310.4 m** |
| high endpoint distance | 51.5 m | 231.0 m | **380.3 m** |
| **low elevation difference** | **0.5 m** | 2.1 m | 4.4 m |
| **high elevation difference** | **0.6 m** | 3.5 m | 4.0 m |
| detected length difference | **42%** | 82% | |

**A hysteresis boundary is where a threshold was crossed, and that point is not symmetric between climbing and descending the same ground** — endpoints scatter by up to 380 m while the *elevations* agree to a 0.5 m median, because elevations belong to the terrain and boundaries belong to the detector. **Keying position on the endpoints left only about one segment in ten carrying both directions**, which defeats the entire purpose of the slope unit.

**Identity is therefore `(place, side)`** — the peak the slope hangs from, plus which flank it descends (and, when the projection is derived per run rather than configured, the UTM zone the place sits in — §5a.1-quinquies). **The col is derived from the hysteresis walk and never keyed on.** Side is settled by the bearing from the place anchor toward the low end, which is measurably **bimodal** on the same pairs: a cluster at a **12.5° median** for the same flank and one near **155°** for opposite flanks, so **90° is where the distribution divides** rather than a picked number. A slope whose high end has no prominence peak within `PEAK_SNAP_M = 100 m` keeps its own high end as the anchor — the honest fallback for a climb that tops onto a plateau.

**Thresholds as built.** Because the place anchor is a peak, the radius lands on the same plateau Milestone 5 fitted — reproduce with `M.build_library(tr, radius_m=r)`:

| radius (m) | 25 | **30** | 35 | 40 | 45 | 50 | 60 |
|---|---|---|---|---|---|---|---|
| segments, relative to 30 m | 1.07 | **1.00** | 0.96 | 0.85 | 0.74 | 0.74 | 0.70 |
| bidirectional | b | **b** | b | b | b−1 | b−1 | b−1 |
| reference-run distinct | n | **n** | n | n | n−1 | n−1 | n−1 |
| consistency failures | 0 | **0** | 0 | 0 | 1 | 1 | 1 |
| max place-elevation SD (m) | 0.80 | **0.80** | 0.80 | 0.80 | 1.99 | 1.99 | 1.99 |

**25–40 m is flat on every measure and 45 m breaks.** `MATCH_RADIUS_M = 30 m` is mid-plateau and is the same value Milestone 5 fitted, which is unsurprising: it is the same peaks. `ELEV_TOL_M = 5 m`, `SIDE_TOL_DEG = 90`, `MAX_SEGMENT_ELEV_SD_M = 1.5 m` (~2.5x the measured place-elevation spread, and close to Milestone 5's 1.0 m because it measures the same stable quantity).

**Result:** the slope library built from the home-trail corpus has **max place-elevation SD 0.80 m**. The reference run's slope traversals resolve to **fewer distinct slopes than traversals**, with ascent and descent of the same face sharing a segment — so the §6.3 τ comparison is available. All §12.3 acceptance criteria still pass, and all six review fixes carry forward unchanged.

### 12.5 Milestone 6 — analysis primitives

Planned before building, the way §12.2, §12.3 and §12.4 were. **§6.3 is built; the table below records the state at planning time.**

**Every primitive applies the same six filters**, each already established, and a primitive that skips one is wrong rather than merely different:

1. **One activity per `run_id`** — dual recording double-counts (the §12.3 review).
2. **Stopped time excluded from all rates** — but never dropped from bronze (§4.3).
3. **`terrain_usable` per-portion**, not per-activity (§14c).
4. **Never pool `rwd_run` with `rwd_walk`** (§6.2).
5. **Path-derived speed, never `speed_mps`** (§9, and §6.1's speed-error measurement).
6. **Each function declares its minimum `sampling_regime` and refuses below it** (§15.4) — §12's own done-when for this milestone.

**What can be validated on a young corpus, and what has to wait:**

| primitive | state |
|---|---|
| §6.1 GAP refit | **Now** — thousands of gated records, with pace varying widely at near-constant HR |
| **§6.3 τ + ΔHR** | **BUILT.** ΔHR ships; **τ refused on measurement** (see §6.3) |
| §6.5 climb profile signature | **Now** — `slope_traversals` already holds length/vertical/grade for every DEM-covered run |
| §6.7 matched-terrain drift | **Now** — within-run matched pairs exist wherever a run revisits terrain |
| §6.4 decoupling | **Thin** — few runs reach 60 or 90 minutes, even before the steady-effort gate rejects any |
| §6.8 Efficiency Factor | **Waiting** — needs a dense series of comparable runs on one cluster, which only regular repeat runs provide |

**§6.4 carries TWO different 5%s** and they must stay visually distinct in output: the gate's **±5%** GAP match between halves, and the **<5%** decoupling target. They are unrelated quantities.

**Acceptance tests, stated before building.** τ never returns without ΔHR, and returns nothing below the ΔHR floor. Decoupling returns nothing when the gate fails — pinned on a run with a mid-run surge, which must yield no value rather than a plausible-looking decoupling figure. A GAP refit from `speed_mps` must fail loudly rather than return a quiet null. Matched-terrain drift reports ascent and descent separately. Every primitive refuses below its declared sampling regime. **Every negative test has a positive twin** (§12.3's review lesson).

### 12.6 MCP server and parameterised primitives

**BUILT — `src/trail_factors/mcp_server.py`, with run selection in `src/trail_factors/analysis/select.py` and background jobs in `src/trail_factors/analysis/jobs.py`.** This is the door a coaching chat uses. Written up after building, from what the code enforces.

**A THIN WRAPPER.** Every tool calls the same function the CLI calls; the server holds no SQL, no thresholds and no interpretation. Two implementations of "what is a report" would drift, and §15.4's exclusion rules must hold identically whichever path is used. If a tool needs something the shared layer does not expose, the fix is a function in `src/trail_factors/analysis/`, not code in the server.

**STDIO ONLY.** `main()` runs the server with no transport argument, so it is a child process of the client that launched it and binds no host and no port. Every tool answers about one person's training; a network transport would offer that to anything that can reach the socket. Do not add one.

**READ-ONLY CONNECTIONS, OPENED PER CALL.** Every database connection is `read_only=True`, including the save tool's (it reads the database only to resolve which run a reference means). Reads are live, on a short-lived connection, exactly as `report` does from the CLI. DuckDB permits one writer, so a connection attempt gets `CONNECT_TIMEOUT_S = 5` and then **names** the lock rather than hanging — a read that blocks for minutes looks exactly like a server that never started.

**THE BOUNDARY IS ATHLETE INPUT VS COMPUTED VALUES, NOT "READ-ONLY".** The chat may SUBMIT athlete input through a validated contract — in this build that is `save_ride_blocks` (§18) alone; confirming a recording window is CLI-only (§4.3) — and those writes go only to `data/athlete/`. It may NEVER modify anything the system computed: no bronze, no silver, no gold. **Nothing reachable from a tool may modify `data/raw/`**; the one write there is the `sync` job's download, and it is append-only (§8).

**THE ENVELOPE.** Every tool returns the same JSON shape — `tool`, `outcome`, `payload`, `text`, `problems`, `remedy`, `retryable`, `corpus_fingerprint`, `calc_version` — and **tools never raise**: an exception crossing the boundary reads to a chat as "the tool is broken", which is worse than any real outcome. The `outcome` vocabulary is deliberately **disjoint** from §16.2's four per-primitive statuses, because a tool-level failure and a measurement-level absence are different things:

| outcome | meaning |
|---|---|
| `result` | the call succeeded; the payload is the answer |
| `rejected` | the request was malformed or ambiguous (an unknown filter, a date naming several activities) |
| `absent` | the reference resolved to nothing (no such run, no such job) |
| `refused` | the thing exists and this tool cannot serve it (a running primitive asked about a non-running activity, a missing athlete configuration key) |
| `broken` | the call failed: no corpus, a locked database, a save that landed but cannot be read back, or a defect |

**`retryable` has one written definition** (`RETRYABLE_MEANS`): *is there a next call that can succeed?* True when the caller can act — wait for a lock, add a flag, fix a field — **and nothing was written**. False when no next call helps, or when **the write already landed** and resending would duplicate it. It is deliberately not "an identical retry may succeed", which would make every corrigible rejection false.

**`corpus_fingerprint` is never a bare null.** The fingerprint says the DATA moved and `calc_version` says the CODE moved (a digest of the live constants registered in `versions.calc_params()`, so changing one moves it whether or not anyone bumps a number); a failure path that could not take a fingerprint says `{"status": "not_taken", "reason": ...}` instead. The full parameter list behind the digest travels once, in `spec_context`. The envelope carries no absolute path (`repo_root` was removed, and paths are scrubbed as a backstop; §14m-bis).

**PARAMETERISED PRIMITIVES, NOT A QUERY TOOL.** The computation is fixed and gated; only the SELECTION is free. The filter vocabulary is designed once and applied uniformly: `run_type`, `start_date`, `end_date`, `cluster`, `last_n`, `segment_id`, `direction` (`ascent` / `descent`). Anything not on that list is not askable, and **a filter a given tool cannot honour is REJECTED by name, never silently dropped** — a dropped filter returns a plausible answer to a different question. There is no arbitrary SQL, no arbitrary aggregation, no bronze access, no chat-defined metric, and no write to anything computed. **Exclusions are always stated**: every result says what was included, what was excluded and why. **`run_type` stays in the vocabulary but has no source in this build** (§16.6): a non-empty `run_type` filter is rejected by name — `FilterUnavailable`, outcome `rejected`, retryable, remedy *omit run_type* — with the message "no run_type source in this build", and every row that carried a run type reports `run_type: null` with `run_type_status: "not collected"`. A later version re-enables it through user-defined run types. A trend exclusion flag is likewise not collected, so no run is excluded on that ground, and results say so.

**SYNC IS A JOB, NOT A CALL.** The MCP `sync` (§8 lists its steps) takes minutes and holds DuckDB's exclusive write lock throughout, so a synchronous version would make every other tool call fail for its duration. It returns a job id at once and `sync_status` polls it. Job state is a file (`JOBS_FILE`: `data/jobs.json` by default, overridable with `TRAIL_FACTORS_JOBS_FILE`), written atomically, kept out of both `data/raw/` and `data/athlete/` — a server can restart between a chat's calls, and a job id that silently stopped meaning anything would be worse than none. `sync_status` never opens the database, so it answers while the sync it is watching holds the lock. A sync whose new files did not all ingest reports them in `problems` and is not a clean done.

**KNOWN LIMITATIONS, stated rather than fixed in this release:**

- **DATE FILTERS USE THE UTC DATE; `list_runs` SHOWS THE LOCAL DATE.** `start_date` and `end_date` compare against the UTC date of a run's start (`select.py`), while `list_runs` displays each run's local date. An evening run close to local midnight can therefore have a UTC date one day later, and a filter on the date `list_runs` showed misses it — and a load sum over a local week shifts it into the next day or week. This departs from the convention that runs are grouped by LOCAL date; until the filters change, widen a date range by a day when an evening run matters.
- **ONE SUMMIT CAN BECOME TWO SEGMENTS ACROSS A ZONE LINE** when `projection_crs` is unset, because each run's frame is derived from its own median position (§5a.1-quinquies). Setting `projection_crs` removes it.

## 13. Working method (three-chat setup)

- **Ideas chat** — novel formulas, equations, what's worth measuring. Feeds this doc.
- **Planning/coding chat** — architecture, milestone specs, spec → prompt for Claude Code.
- **Coaching chat** — later. Reads gold layer only (through the stdio MCP server). Has its own project docs (the athlete's plan and constraints), which are personal and do not belong in this repository.

Cross-checking generated code against a second model is reasonable for verification of the *math* (GAP curve, τ fit, Fréchet). Less useful for architecture, where consistency matters more than a second opinion.

---

## 14. Open questions

### 14o. THE FR265's FIRST FILE — what the new hardware settles, and what it does not

The first file from a barometric, multiband, running-dynamics watch (FR265), recorded at 1 Hz on the home trail with full GPS coverage. **n = 1**, and every claim below is labelled accordingly. The section is kept because it shows how to read a new device: against the old one, on the same ground, against the same DEM tile.

#### THE BAROMETER RESOLVES §5a-bis's AMPLITUDE INFLATION

Same trail, same DEM tile, every point covered:

| | **amplitude ratio** (watch relief / DEM relief) |
|---|---|
| **FR265** (barometric) | **1.083** |
| **FR245** reference run (GPS altitude) | **1.946** |

§5a-bis.4 measured Garmin's elevation amplitude as inflated **~2×** about a correct centre. The FR245 reference run reproduces that (1.946); the FR265 collapses it to **1.083**. Watch-minus-DEM across the run: median **+0.51 m**, sd **0.89 m**.

**WHICH HALF OF THAT AGREEMENT IS INDEPENDENT, AND WHICH IS NOT — this distinction is the whole value of the number.**

- **The RELIEF agreement IS independent evidence.** Amplitude is derived from pressure change, and the watch has no DEM aboard. A ratio near 1 against a 1 m LiDAR surface is the barometer and the DEM agreeing about the SHAPE of the ground by two unrelated routes.
- **The OFFSET agreement is NOT.** Garmin auto-calibrates the barometer's absolute datum against GPS, and possibly against a server-side elevation model. A sub-metre median offset says the datum is anchored; it does **not** say the anchor is independent of a DEM. §15.1's circularity caveat applies to the offset and not to the relief.

**THE PRESSURE-DRIFT TEST IS STILL OPEN** and is what would settle the offset: the same trail in different weather, comparing offsets. A barometric altimeter tracks pressure, so a passing front should move the offset while leaving the relief alone. Until that is run, `altitude_source = barometric` records the SENSOR, not a claim of independence.

#### MULTIBAND: NO VISIBLE EFFECT AT n = 1, ON THE WRONG GROUND FOR THE TEST

Normalised jitter (§15.4's own measure — raw deviation over median point spacing, because the raw figure is a sampling-regime artifact):

| device | regime | median step | jitter-flagged |
|---|---|---|---|
| **FR265** | 1hz | 2.22 m | **0.36%** |
| FR245 | 1hz | 1.91 m | 0.34% |
| FR245 | 1hz | 1.82 m | 0.25% |
| FR245 | 1hz | 2.84 m | 0.00% |
| FR245 | smart | 7.49 m | 4.14% |

The FR265 sits **inside** the FR245's 1 Hz range. Coverage cannot discriminate either — 1.000 on the FR265 and on the recent FR245 runs. **The home trail is open sky and both receivers saturate there**, so this is not evidence against multiband; it is the wrong ground for the test. **The test is the one canopy-covered stretch where §14h's signal problems actually occur**, and where the FR245's failure is a matter of record.

#### RUNNING DYNAMICS ARE HERE, AND THE ACCESSORY-ONLY ONES ARE NOT

Present and populated per record: `vertical_oscillation`, `vertical_ratio`, `stance_time`, `step_length`, `power`, `accumulated_power`. **`stance_time_balance` and `stance_time_percent` are declared and NULL on every record — exactly as §7 predicted**: they need an HRM-Pro Plus, HRM-Run or RD Pod, and the wrist alone cannot produce them.

**The first file's medians are stored as an easy-run BASELINE** — the reference against which a mode signature will be read (§17.2's M1). The values are the athlete's own and are not reproduced here; what generalises is the units.

**THE UNITS ARE NOT THE OBVIOUS ONES** and are carried in every result: oscillation and step length are **millimetres**, contact time is **milliseconds**, and vertical ratio is a **percent** that is already the ratio of the first two. A reader who assumes centimetres is out by 10× in a number that still looks plausible.

#### HRV AND RESPIRATION: DECLARED, NULL

`session` gained `rmssd_hrv`, `sdrr_hrv`, `avg_respiration_rate`, `max_respiration_rate` and `min_respiration_rate`. **All five are NULL on the first file.** The fields exist; the watch did not fill them. No `hrv` or `rr_interval` messages appear anywhere in the file. Do not plan on this data until a file arrives carrying it.

#### GARMIN RUNNING POWER IS A MODEL OUTPUT, NOT A MEASUREMENT

`power` is stored per record and `session` carries `normalized_power` and `total_work`. **There is no power meter on a running watch.** These are Garmin's estimate from speed, grade, cadence and running dynamics — a model whose coefficients are unpublished and can change with a firmware update.

**Usable for WITHIN-DEVICE comparison** — one run against the next on the same watch and firmware — **and never as ground truth**, never against another manufacturer's figure, and never as the dependent variable in anything §2 fits. A model output regressed against the inputs of the same model is not a measurement of the athlete.

### 14n. THE PROBE MONOTONICITY TEST — the falsifier for §2.3

**Prediction, stated before the data exists so it can fail:** across a training block, a regular flat-ground probe — run in the configured run/walk mode form (§17.2, M1; the mode's name is a parameter) — should trend **monotonically** once the weather term is removed. Same surface, same form, same intent, read at matched elapsed time (§2.3).

**If it does not trend monotonically, one of exactly two things is wrong, and they are distinguishable:**

| symptom | reading | next move |
|---|---|---|
| probe scatter tracks dew point after the weather term is applied | **the weather term is wrong** — g(weather) is mis-specified or mis-fitted | refit g on the probe series itself, where f(grade) is absent by construction |
| probe scatter is uncorrelated with dew point but still non-monotonic | **the probe is not controlled** — surface, form, intent or pacing varied between sessions | tighten the protocol before trusting k(date) |

**This is the only place in the design where the weather term can be tested in isolation**, because flat ground is the one surface where f(grade) drops out entirely. That is what makes the probe worth running regularly even when the trail data is richer.

**Do not read a single non-monotonic session as a failure.** The test is the trend across a block. Trail runs alone can show a dew-point-to-efficiency-factor correlation with no way to attribute it — when seasons and fitness move together, weather and fitness are confounded — and the probe exists precisely to break that tie.


### 14k. A pooled segment is PLATEAU ABSORPTION, not a fork — and the obvious fix mirrors it

**The report was that one segment pools a short, steep direct line with a much longer, nearly flat route between the same endpoints — a fork that `(place, side)` cannot distinguish. It is not a fork. It is a detector failure, and the first-guess repair makes it worse.**

**WHAT IS ACTUALLY THERE.** Every descent on the segment is short and steep (roughly 130–220 m at −5 to −9%); every ascent is long and nearly flat (roughly 0.7–1.5 km at about +1%). The separation is perfect by direction across every effort, which is already the wrong signature for a boundary-walk failure — those are sporadic. The long members' **first ~140 m is the same ground as the short descents** (records metres apart); the rest is a plateau excursion reaching ~300 m off the short effort's line, wandering and returning to the same summit having gained slightly *negative* height. Path length against straight-line displacement gives **sinuosity ~5.9**.

It is **not** two slopes with a col: the max internal reversal (~5.9 m) is below `PROMINENCE_MIN_M = 8.0`. There is no col inside it.

**THE MECHANISM.** `silver.py`'s merge veto tests the grade of the **cumulative result**, never of the **piece being absorbed**. The merge trace shows most merges adding pieces at well under ±1% — every one below `SLOPE_EXIT_PCT`, one **losing height over hundreds of metres** — each passing because vertical banked by the first legitimate merge still carries the running total above 1%.

**WHY THE FIX IS NOT SHIPPED.** Adding the missing added-piece veto was implemented and measured over every DEM-covered activity. **It does not remove the absorption; it mirrors it onto the descents:**

| | ascents (median) | descents (median) | efforts >400 m (asc / desc) |
|---|---|---|---|
| current | 531.6 m at 2.22% | 189.0 m at 5.99% | 74 / 20 |
| with added-piece veto | 162.5 m at 7.69% | **444.9 m at 2.63%** | 11 / **69** |

The plateau simply moves onto the descents. The residual needs a threshold below **this plateau's ~5.9 m texture**, and `PROMINENCE_MIN_M = 8.0 m` sits *above* it — so the project's own significance constant cannot resolve slope from plateau here. **Fitting a new one on the home trail would be the third instance of the error §5 has already corrected twice** (the direction rule, the prominence-hill unit). The decisive measurement is terrain with genuine flats between features.

**SO: THE RULE IS UNCHANGED AND THE COMPOSITION IS CARRIED INSTEAD.** Every effort now reports `len_steep_m` / `len_moderate_m` / `len_gentle_m` / `len_flat_m` (signed by its own direction), `steep_mean_grade_pct`, `tail_after_last_steep_m` and `wrong_sign_m` (`basic.effort_profile`). A reader who can see that a stored −2.4% descent has a −10.2% steep part does not need the boundary moved.

**A SECOND, INDEPENDENT DEFECT AT THE SAME PLACE.** `SIDE_TOL_DEG = 90` plus single-link union-find welds **three** mutually incompatible flanks into one id: **nearly half of the segment's member pairs fail the matcher's own `compatible` predicate**, all on the bearing gate. Two populations whose bearings are ~147° apart never pairwise match — they are chained by a third population of efforts that straddles the 90° cut on either side. **`SIDE_TOL_DEG` is also an input to the segment id hash** (`matcher.py`), so changing it 90 → 45 rewrites a majority of existing segment ids. Not attempted here; it needs a sweep against segment count, bidirectional count and consistency failures first.

**AND IT IS NOT ONE SEGMENT.** Ranked by max/min |mean_grade_pct| across a segment's efforts, four segments sit past a clean empty band in the distribution (nothing between ratios of ~4.1 and ~7.0). They hold over a third of all traversals. `tests/test_segment_pooling.py` names them and fails if a fifth appears or one is repaired — the threshold is the empty band, so every value inside it partitions the library identically.

**Unresolved:** the worst offender on both ratios has **no incompatible pairs and every member anchored to a real prominence peak**, and is explained by neither defect above. It has not been traced.

**Also measured, and it corrects a premise:** about two thirds of traversals snap to a real prominence peak; **about a third use the own-high-end fallback**, including every member of the plateau segment. §12.4's "peaks reproduce at SD 0.54 m" does not cover those — though on this segment the fallback anchors reproduce to ~15 m of spread and ~0.15 m of elevation SD, so they are not the mechanism here.

### 14l. Descent run-out is real but rare, and the stated mechanism does not exist

**The premise — "hysteresis exits below +1%, so a descent runs until the ground nearly flattens" — is wrong about the code.** `detect_slopes` assigns state with a plain three-state threshold on ±`SLOPE_ENTER_PCT` (3.0). `SLOPE_EXIT_PCT` appears in exactly **one** place: the merge veto. **There is no hysteresis exit and no run-out rule to fix.**

**There is also no flat run-out at the boundary.** The contiguous near-flat (|grade| < 3%) stretch at a descent's exit edge is corpus-wide **median 6.1 m, p90 10.7 m, max 33.0 m** — the endpoint snap stopping the instant terrain levels, exactly as designed.

**What the reference run's descents actually look like** (fraction gentler than −5%, and the steep-part mean the stored average hides; segments labelled A–F):

| segment | length | stored grade | gentler than −5% | steep-part mean | run-out |
|---|---|---|---|---|---|
| A | 93 m | −13.6% | 17% | −15.9% | 16 m |
| B | 129 m | −8.6% | **25%** | −9.7% | 22 m |
| C | 188 m | −8.6% | 29% | −10.5% | 13 m |
| D | 154 m | −6.1% | 60% | −12.0% | 101 m |
| E | 273 m | −4.2% | 64% | −9.5% | 128 m |
| F | 430 m | −2.4% | **81%** | **−10.2%** | **225 m** |

So a "−8.6% descent" is **a quarter gentle, not a third**, and only 22 m of that trails. But segment F stores −2.4% for ground whose steep part means −10.2%, and over half its length comes after the last steep sample. **The concern is real and it varies enormously by effort — which is why the profile is carried per effort rather than a global rule being changed.**

**Three further measurements that redirect the question:**
1. **The gentle terrain is INTERIOR** (mean position 0.53 of the effort, thirds 42%/56%/56%), and it comes from bench absorption — the ±3% seed runs are only ~41% of descent length, absorption adds ~51%, the endpoint snap ~8%. **No boundary adjustment removes interior terrain.**
2. **Ascents are worse on every symmetric measure.** |grade| < 3% is ~35% of descent length against **~41% of ascent length**; stored grade understates the steep-part mean by ~1.9× on descents against **~4.2× on ascents**. A handful of efforts — all ascents — run **more than half their distance downhill**.
3. **The one genuinely descent-specific effect is in the tail.** Fraction after the last steep sample: descents median ~11%, but **about one descent in six exceeds a third** (max ~76%); ascents median ~4%, very few past a third. Run-out is descent-biased, affects about one descent in six, and is not the typical case.

**`BENCH_MAX_M` is NOT lowered.** 400 m was fitted **from below** (the longest non-climbing stretch inside an unambiguous ascent, ~311 m), and §5 records that the home trail cannot fit it from above. The "gentler than −5%" fraction is also regime-dependent — 56.2% at 8.45 m sample spacing against 46.2% at 2.03 m on the *same run* — so a threshold fitted on it would be a sampling artifact, the same failure the raw-jitter threshold was rejected for.

### 14p. A RUN OUTSIDE EVERY DEM TILE — the refusal now names the fix

**The problem was not the refusal, it was the silence.** A run on ground no loaded tile covered produced a report with every terrain primitive absent and no explanation. The refusal is correct — §5a's local-tile rule means no tile, no terrain — but an unexplained absence reads as breakage, and the athlete cannot act on it.

**DETECTION.** `TileSet.coverage()` reports, per run, how many positions fall outside every loaded tile and which 10 km grid cells would cover them. The report carries it as `terrain_coverage`, and a gap is the **top item in NOTABLE** at salience 900 — above every flag except a fatal one, because it explains everything else that is missing from the page.

**IT REPORTS A GRID CELL, NEVER A COORDINATE.** A 10 km cell is exactly what fetching a tile requires and is two orders of magnitude coarser than a position — the same reasoning that rounds weather coordinates to ~1.1 km before they leave the machine (§8). With `projection_crs` configured, the x/y cell is computed from the projected coordinates in the grid of that projection (see §5a.1-ter: USGS names a tile by its NORTH-west corner, and outside the configured UTM zone the x/y is not a USGS filename at all — `coverage()` says so with `filename_is_resolved: False` and `in_grid_crs_zone`). **The project-and-year suffix is taken from the tiles already on disk, else from `dem_tile_suffix_guess` in `config/athlete.yaml`, and is labelled a guess**, because it is a USGS acquisition name, not a function of position, and a filename stated with false confidence sends the athlete to a 404. **With `projection_crs` unset** (the default), cells are labelled in the run's derived UTM zone (§5a.1-quinquies), `filename` is null because no guess is made, and `fetch_command` carries `--zone NN`; a southern or zone-above-23 cell has no USGS grid and so no fetch command. `fetch-tile` (§5a.1-bis) resolves the real filename against the 3DEP index rather than constructing it.

**DEM IS MULTI-TILE.** `DemTile` originally loaded one file from a hard-coded default (that constant is now removed), so a second trail meant editing code. `TileSet` loads every `USGS_*.tif` in the DEM directory (`data/dem/` by default, `TRAIL_FACTORS_DEM_DIR` to override) and queries them as one surface; **dropping a file in is the entire installation step.** A point covered by none still raises `OutsideTile` — loudly, as §5a requires — and the caller turns that into the named gap above.

**`retile` IS THE SINGLE COMMAND.** It finds the activities a newly-added tile now covers, and re-runs dem → silver for those only. A full re-ingest to pick up a tile would rebuild every record in the corpus to add a few thousand, and **a rebuild that touches more than it needs to is one that eventually stops being run.**

**The matcher step is corpus-wide, and that is not an oversight.** The segment library is a global object: a new trail's places must be created in the same pass that could match them against existing ones. It is the only step that cannot be scoped. (`retile` also runs `recovery` after `match`, as the MCP `sync` job does; the CLI `sync` runs neither, §8.)

**THE MATCHER'S NEW-TRAIL BEHAVIOUR IS STRUCTURAL, NOT INCIDENTAL.** `compatible` tests position FIRST, at `MATCH_RADIUS_M = 30 m`. Places kilometres apart therefore cannot join whatever their elevation or bearing — a new trail at the same altitude as the home trail is still rejected on distance. The lifecycle is pinned in `tests/test_new_trail.py`: a first run **creates** its places at `n_efforts = 1`, the second visit **matches** them, and adding a trail neither renumbers nor merges the existing library.

**Those tests use SYNTHETIC traversals**, because they must not depend on any particular course's tile being on disk. **The real validation is the first run on a new course once its tile is in** — the same standing condition §5 already carries for the slope boundary rule, which the home trail cannot validate either.

### 14m-bis. CLOSING THE BOUNDARY AUDIT — a chat CAN reach the file, and nothing in the server can stop it

**A second chat queried the database directly.** It reported reading a database file outside the repository and got results one run behind. This closes §14m, which was opened over the first incident.

**THE MECHANISM — and it is a COPY, not a stray file.**

The coaching chat ran in a **sandboxed VM with its own home directory** (`/sessions/…`), which reached the repository through a folder grant in the desktop client's configuration (`remoteSessionFolderGrants`). During one piece of analysis it **copied the database into that sandbox** and ran direct SQL against the copy. The next run made the copy stale. **The "sync lag" it reported was the snapshot talking.**

**THE GRANT IS THE MECHANISM, NOT THE TOOL SURFACE.** A chat with a shell and a folder grant can copy anything the grant reaches, and once it holds a copy nothing about the tool surface applies to it — not the gates, not the filters, not the fingerprint. This is not a hole in MCP; it is what a filesystem grant means. **Nothing in this repository can prevent it**; whether to grant a chat the repository folder is the user's decision. The invitations that made it convenient are removed (below).

> **CORRECTING THE INVESTIGATION.** An earlier version of this section said "the origin could not be determined — no trace remains." The search was sound and its negative result was correct: the reported file did not exist on the host machine, and there was exactly one database file on it. **But the search space was wrong.** The copy was never on that filesystem — it was inside a VM nobody thought to consider. "No trace remains" was a conclusion about the wrong disk, and it read as stronger than it was. When an artifact cannot be found, the next question is whether it was ever where the search was pointed.

**WHAT IS STILL ESTABLISHED, and was never in doubt: the CODE did not create it.** Every connection goes through the one resolved database path (`src/trail_factors/paths.py`: `data/ultra.duckdb` by default, `TRAIL_FACTORS_DB` to override), every MCP server connection is read-only, there is no copy or export path in the repository, and no test creates a database file — corpus tests skip when `TRAIL_FACTORS_DB` is unset.

**"ONE RUN BEHIND" IS A DIAGNOSTIC SIGNATURE, and worth keeping.** A live direct read of the real file returns *current* data; only a COPY can be stale. So staleness distinguishes the two failure modes, and it also rules out the phantom-empty-database failure §11 fixed — that yields *no* rows, not old ones. **If a chat's numbers are behind the corpus rather than absent or wrong, suspect a snapshot.**

**AND THE FINGERPRINT WOULD HAVE CAUGHT IT.** Every tool result carries `corpus_fingerprint`; numbers computed from a copy carry none. A conversation mixing the two has one labelled corpus and one unlabelled set of numbers that disagree with it — which is exactly the condition the fingerprint exists to make visible. The tell was available and was not read.

**THE ANSWER TO THE AUDIT'S QUESTION, PLAINLY: YES, A CHAT WITH FILESYSTEM ACCESS CAN OPEN THE DATABASE, AND NOTHING IN THE TOOL SURFACE CAN PREVENT IT.** MCP constrains what the SERVER exposes. It cannot constrain a client that also has a shell. **The folder grant, not the tool surface, is the only actual control**, and it belongs to whoever configures the client.

**WHAT MADE IT INVITING, and both are now removed:**

| | was | now |
|---|---|---|
| `repo_root` in EVERY envelope | the absolute repository path, handed over on every single result, consumed by nothing | **removed.** `corpus_fingerprint` already identifies which corpus answered |
| `report`'s `run.source_file` | `data/raw/garmin/<id>_ACTIVITY.fit` — a path into the one directory holding home GPS | **`garmin_file_id`**: the id is the provenance a reader needs; the directory is not |

Combined with the documentation naming the database's default location, the envelope was handing every chat the absolute repo root and the report was handing it a path under `data/raw/`. **Neither removal PREVENTS a direct read. They remove the invitation**, which is the only thing code on this side of the boundary can do. Do not re-add either.

**AND spec_context NOW SAYS WHAT TO DO INSTEAD.** The previous wording said the boundary does not constrain a client with a shell. It now adds the part that matters: a number read straight from the file carries **none** of the gates — no stopped-time exclusion, no quality flags, no one-activity-per-run_id, no HR lag, no refusal — so it will look like a tool result, disagree with one, and the disagreement will read as a finding. **Both incidents ended in retracted numbers.** The instruction is now explicit: *if a quantity you need has no tool, say so and stop* — that report gets the metric built, which is exactly what happened here (M7).

### 14m. The coaching chat's "read silver records directly" was outside the MCP boundary, not a hole in it

**Audited: every registered tool, every read path called and its full envelope inspected. Nothing returns record-grain data and nothing leaks a coordinate.**

The finest grain any payload carries is the **effort** — on the order of a hundred-plus rows against tens of thousands of records (roughly 150:1). The largest list any call returns is `list_runs`, one row per run. The only bare numeric arrays are length 2 (`[temp_min, temp_max]`, `[dew_min, dew_max]`). The database holds about twenty positional columns; **none appears in any payload** — lat/lon are converted to the projected CRS for pair distances or reduced to a cluster name and discarded.

**What almost certainly happened: the chat ran the query itself.** It was Claude Code, with Bash and Read. Every envelope then handed it `repo_root`, `report` handed it `run.source_file` under `data/raw/`, and the database's location was documented. **Opening that file from a shell is a literal description of "bypassing the tool layer".** The MCP boundary constrains what the SERVER exposes; it cannot constrain a client that also has a shell. `spec_context` now says so, and says that numbers obtained that way carry none of the gates or provenance that make a tool result trustworthy.

**Three real defects the audit found, all fixed:**
1. `context.py` claimed `records_enriched` was "the layer the tools actually read" — false since §6.0 reads `records`, and **the exact sentence a chat would cite to believe it had read silver records**. Replaced with the true distinction: bronze is READ, no tool RETURNS a bronze row.
2. `applied_filters.grain` names the **input** grain, so a scalar run mean declares `grain: "records"` — misreadable in precisely the direction of the chat's claim. Now documented.
3. `test_the_server_contains_no_sql` banned `GROUP BY`/`JOIN`/`median(`/`avg(` but not `SELECT` or `FROM`, so **it could not fire on any SQL actually present** — and three queries had accumulated in the server. Same family as §4.2's "a check that cannot fire". The SQL moved to `queries.py` and the check now covers it, with a presence twin.

### 14i. Where the fastest samples of a descent sit — RESOLUTION-LIMITED, NOT A FINDING

**This is recorded as an observation inside the measurement's resolution, and must not be cited as a finding.**

On one Smart Recording run, the fastest samples of a short, steep descent appeared at the **run-out below the steepest core** rather than on the pitch. That ordering is *inside the resolution of the measurement* and cannot be resolved by it:

- **Path-derived speed is a BACKWARD-LOOKING AVERAGE over the sampling interval** — ~4 s under Smart Recording.
- **Grade is a POINT measurement at the record's position.**
- The two are therefore offset by roughly half a sampling interval, systematically.

A short hill descended fast lasts well under half a minute — **only a handful of Smart Recording samples** — so the speed-versus-grade offset is **a sizeable fraction of the hill**. "Fastest at the bottom of the pitch" and "fastest on the run-out" are **the same samples read against different reference points**.

A runner's own account of such a descent — accelerating through it — would be consistent with a peak at the bottom. **Do not record "fastest on the run-out" as a finding.** The test is a 1 Hz run on which that hill is taken hard: at 1 Hz the same descent carries several times as many samples and the offset shrinks in proportion.

**The practical consequence survives either way, which is why the ambiguity is tolerable:** wherever the peak sits, **a switchback at the bottom of a pitch discards it and a straight preserves it.** That is a course-reading rule, not a physiological claim, and it does not depend on resolving the offset.

### 14j. Garmin's fused best pace agrees with the odometer at the instantaneous peak

**A calibration point for `speed_mps` on DESCENTS specifically, and a retraction.**

On two separate runs, Garmin's fused best pace agreed with the odometer-derived figure **to within ~10 s/mile at the same timestamp**.

**The watch's reading was correct. The coaching chat's "GPS noise under canopy" explanation was wrong and has been retracted.**

This does not overturn §9 — the grade bias is real and **compresses the range** of fused speed across grade, which is why `speed_mps` still must not be used for grade-dependent analysis. What it establishes is narrower and worth having: **at the instantaneous peak on a descent, the fused and odometer sources agree.** A disagreement there is evidence of something, rather than the expected state.

- **Vertical per mile is a planning number, and it is unknown pending §5a.** What "similar terrain" means to the matcher depends on it, and it must come from DEM-derived elevation — **never from device ascent or any substitute derived from the FIT files**, which §5a-bis shows are inflated.
- ~~Which watch model?~~ **Resolved: the corpus began on a Forerunner 245 Music.** No barometer (→ §5a DEM lookup). No running dynamics without accessory (→ §7).
- **A chest strap or running pod is worth adding early, if at all.** Data recorded before it arrives permanently lacks the accessory-only dynamics (§14o) and chest-strap HR.
- Does the FR245 write `hrv` (RR interval) messages when paired with a chest strap? Verify against a real file. If yes, HRV work does not need a watch upgrade.
- **Which DEM source, and at what resolution?** Test a candidate against a known climb before committing (§5a records the choice of 3DEP 1 m tiles).
- **How much older road data to backfill?** Weeks of non-specific running still help calibrate the HR model's flat-terrain response. Worth ingesting.
- Segment naming: auto-generated IDs vs. manual names ("the wall," "switchback 3"). Manual names make the coaching chat much more readable — this became `name-segment` (§12, milestone 5).
- A phone app (Strava) can record a parallel activity from phone GPS, independent of the watch. Two independent position traces of the same run = a real GPS error estimate under canopy. **Capture both files going forward**; analysis deferred to milestone 1.5+.
- **TEST — a barometric watch: do NOT assume it is an independent elevation source.** Garmin auto-calibrates barometric altitude against GPS and possibly against a DEM server-side, so a "barometric" profile may be **a DEM lookup wearing a costume** — the same circularity as `movingDuration` and the `rwd_*` splits (§15.1). **The test:** run the same trail twice on days with materially different weather. **Barometric error drifts slowly with pressure; DEM error is spatial and repeats identically every traversal.** If the two profiles differ in a way that tracks the pressure change, the barometer contributes real signal. **If they are identical, it is calibrated to a fixed source and adds nothing independent.** Until measured, treat it as unknown-provenance per §15.1. (§14o's first file settled the relief half; the offset half is this test.)
- **TEST — before switching watches, record one more clean 1 Hz run on the old watch on the home trail**, so the hardware boundary is **measurable rather than merely a discontinuity in the data**. Without it, every old-to-new comparison confounds the watch change with everything else that changed.
- **TEST — sync the watch immediately before a run and check whether GPS coverage returns to 100%.** This tests the stale-prediction-data hypothesis in §14c. **If confirmed, "sync before running" becomes a data-collection requirement, not a convenience** — the coverage failures in the corpus clustered in recent files. Worth settling before a long block generates the §14c dataset.
- **The heat-vs-fitness question needs runs spanning a wide temperature range at comparable terrain.** A season's spread of temperatures **is the dataset.** **Prioritise keeping terrain comparable across seasons (same trails) so temperature is the variable that moves** — otherwise heat and fitness stay confounded (§2.6, §14n).
- **`PAIR_START_TOLERANCE_S` is 180 s, chosen because the only known pair differed by 2 s. The correct value is unvalidated** — it has never been tested against a near-miss. A run-then-hike sequence starting within 3 minutes of the run ending would be the failure case. **Revisit if back-to-back activities are ever recorded close together.** The failure is quiet in both directions: too tight splits one run into two, too loose merges two runs into one, and neither raises.
- **Dual-source recording is a validation period, not permanent architecture — and its original reason may already be resolved.** Dual recording began because the two devices disagreed on distance and elevation. **§14b established that the distance disagreement is CHORDING from Smart Recording, not a device difference.** So the motivating problem is likely already explained. **A 1 Hz run on the same trail tests it** (TEST below). **If the gap closes, dual-source recording can end**, and a better watch ends the need regardless. Phone recording exists to validate whether Every Second recording on the watch closes the distance and ascent gaps (§14b), and to build the labelled test set for the §5 matcher. Build the `runs` table and pairing logic **simply — it must work, it does not need to be optimised, and it will see few runs.**
- **TEST — run a technical descent hard, both devices, watch at 1 Hz.** The only way to test the arm-motion hypothesis in §14a. Under Smart Recording a short descent can carry as few as 3–6 watch samples; at 1 Hz it would carry several times that.
- **TEST — same trail, watch at 1 Hz, both devices.** Does the distance gap close (the chording hypothesis in §14b), and does the ascent gap close?
- **Download the phone app's original files for the historical runs**, not only going forward. For every run recorded before the watch was switched to Every Second, the phone file is the **only 1 Hz positional data that exists**; the Garmin file is the only source of HR and cadence. Complementary, not redundant.
- ~~Do the `split` messages need their own table in the schema?~~ **Resolved: yes — ingested at milestone 0.** These are Garmin's run/walk detection; `split_type` takes the values `rwd_walk`, `rwd_run` and `rwd_stand`, and they tile the whole activity. Reasoning: on the hilly runs measured, the `rwd_walk` segments averaged nearly the same HR as `rwd_run` while sitting on roughly twice the grade — **walks can be power hiking at aerobic parity, not rest.** Any HR model treating them as low-effort will be biased. They also auto-label the power-hiking segments §3 needs. Two caveats for the schema:
  - `total_elapsed_time` is rounded to whole seconds — summing a run's splits drifts by seconds against the session total. ~~Derive timing from `start_time` / `end_time` instead.~~ **CORRECTED: that advice was wrong and produced a real bug — use `total_timer_time`.** `end_time - start_time` inherits the 1 s timestamp resolution and truncates, understating **nearly every split-type group and nearly every individual split** — a one-signed error that accumulates. See §9.
  - `end_position_lat`, `end_position_long`, and `start_elevation` are **always null**. A start position is recorded; an end position is not.

### 14a. Cadence reliability

**Hypothesis under test:** arm-raising for balance on fast or technical descents decouples wrist-measured cadence from true foot cadence. The FR245 derives cadence from the wrist accelerometer, so the mechanism is plausible.

**Verdict: UNSUPPORTED but UNTESTED — not disproven.** See the limitation and the co-occurrence gap below.

**Decoupling test.** Fit a flat-ground baseline on `rwd_run` samples at grade −2..2%: `cadence = a + b × speed`. Residual = actual − predicted; negative means fewer steps than expected. Then aggregate the residual over every detected descent that carries `rwd_run` samples, and test whether faster descents lose more cadence.

**Result on the first file examined: no relationship between descent speed and cadence loss.** The correlation between descent speed and cadence residual was close to zero, and its sign was *positive* — the faster half of descents had a slightly *higher* residual than the slower half, the opposite of what the hypothesis predicts. Every descent in that test is a negative control.

**Cadence can be flat across the grade range.** Binned by grade (`rwd_run` samples only), where median cadence varies by only a few spm from −10% to +10%, speed is modulated almost entirely by stride length. **For gait work (§17.2, M1) this means speed on hills is carried by stride length, not turnover** — so a cadence change is a deliberate gait change, not a side effect of grade.

**Low-cadence steep-descent samples are genuine hiking, not sensor error.** Samples far below the running band on steep descents are independently labelled `rwd_walk` by Garmin's own run/walk detector. Split by label, almost every `rwd_walk` sample sits below the running cadence range and only a few percent of `rwd_run` samples do — the two labels separate cleanly on cadence and speed.

**Cadence dropouts** (0 spm while moving >1.5 m/s) were under 1% of samples, and fell mostly on climbs rather than steep descents — again the opposite of the predicted pattern.

**Run/walk transitions are the real instability, not descents.** The file's single worst cadence excursion came immediately after a walk→run boundary: cadence roughly doubled in a couple of seconds at near-constant speed, driving derived stride length implausibly short. That spike was the maximum cadence anywhere in the file and matches `session.max_running_cadence` — **which Garmin records in strides/min, so it reads as half the steps/min value.** Cadence **spiked** — the inverse of the hypothesis. **Treat samples within a few seconds of a `split_type` change as suspect** (this became §15.4's `split_transition` flag).

**Independent stride-count cross-check.** `session.total_strides`: on the same run the watch counted a few percent *more* strides than the Strava iOS app, not fewer. Strava iOS uses Core Motion's pedometer reading from wherever the phone was carried, a different body location. Direction is opposite to the hypothesis. *Caveats:* stride definitions may differ between platforms, and this is session-level only — it cannot be localised to any descent.

**LIMITATION — and precisely what it does and does not constrain.** Garmin's per-record cadence is **already averaged over an internal window** before being written to the file. Combined with Smart Recording's ~4 s sampling, there are **two filters between gait and data**. A brief (2–4 s) decoupling event is invisible regardless of analysis method. Every Second recording removes one filter, not both.

**This constrains TRANSIENT analysis only. It does NOT flag Smart Recording cadence as an artifact.** The measurements above are correct, merely sparse — see the artifact vs. resolution-limit split in §15.4. Specifically:

- The descent null result **rules out sustained decoupling** across a descent and **remains a usable finding**.
- The flat cadence-by-grade relationship **remains a usable finding**, and is a valid input to coaching.
- What is *not* ruled out is **transient** decoupling. Detecting that requires 1 Hz, and any analysis targeting it must refuse to run on Smart Recording files rather than return a null it cannot support.

**Why one run cannot settle it.** The scenario requires *fast* **and** *technical* together. The file examined contained neither in combination: where the trail was steep and technical it was hiked (`rwd_walk`); where it was run fast the descents were smooth. **If fast-and-technical does not co-occur in a file, that file cannot test the hypothesis**, however many descents it has.

**Detector for future runs:** `rwd_run` **+** high speed **+** negative grade **+** strongly negative cadence residual **+** stride spike. A null from this detector is meaningful only on a 1 Hz file where that combination actually occurs.

### 14b. Watch vs. phone

All figures below come from one watch/phone file pair on one trail: treat them as indicative device characteristics, not established constants.

**Track separation at matched timestamps.**

| median | p90 | p95 | p99 | max | >25 m | >15 m |
|---|---|---|---|---|---|---|
| 5.9 m | 9.1 m | 10.1 m | 12.0 m | 13.4 m | 0 | 0 |

Agreement is far tighter than published single-band-under-canopy figures. The ~30 m segment-matching tolerance in §5 is generous — comfortably so.

**Caveat: agreement is not accuracy.** Both receivers can share the same multipath error, and on that trail they largely did. This is an **upper bound on disagreement**, not a measurement of error against ground truth.

**Per-point altitude difference (garmin − strava).**

| median | mean | stdev | range |
|---|---|---|---|
| −4.0 m | −1.8 m | 7.2 m | −14.4 .. +13.4 |

The devices agree on *absolute* elevation to within a few metres — yet Garmin reported **more than twice** Strava's total ascent. Garmin's altitude is **noisy, not biased**: it oscillates around roughly the right value, and every oscillation sums into cumulative gain. This is direct confirmation of §5a — the fix is not calibration, it is **not differentiating a noisy signal**.

**Distance difference is a sampling artifact, not drift.** Each platform's reported distance was compared against the length of its own GPS path:

| | reported vs own GPS path |
|---|---|
| Garmin (Smart Recording) | reported **~0.5% longer** than its own path |
| Strava (1 Hz) | reported **~1.7% shorter** than its own path |

Decimating Strava's 1 Hz track to one point every 4–5 s reproduces Garmin's path length: Garmin's point count matches Strava sampled at 4–5 s.

- The ~2.7% gap between the two reported distances is **chording** from Smart Recording — straight lines between sparse points cut every corner. Expect it to mostly close at 1 Hz.
- Both platforms already correct in **opposite directions**: Garmin *adds* distance over its own GPS path (accelerometer fusion); Strava *removes* distance (jitter smoothing).
- **Neither raw path is ground truth.** The two reported values likely bracket it. Store both, flag the source, treat neither as authoritative.

**`gps_accuracy` (Strava only) is not a quality signal.** It is present only on the positioned half of the phone's record messages (see §9):

| value | share |
|---|---|
| 1 | 0.1% |
| 2 | 98.3% |
| 3 | 0.8% |
| 4 | 0.8% |

p99 = 3, max = 4. Near-constant and optimistic — it reported 2 m through terrain where the two tracks differed by 10 m. **Ingest the column but build no quality flags on it.** Inter-device disagreement is the better error signal.

**Record-field split confirmed.**

| | fields |
|---|---|
| garmin only | `heart_rate`, `cadence`, `fractional_cadence`, `cycle_length16` (**all zero** — see §9) |
| strava only | `gps_accuracy`, `speed` |
| both | `timestamp`, `position_lat`, `position_long`, `distance`, `enhanced_altitude`, `enhanced_speed` |

The phone contributes **no physiology at all** — it is a position/elevation second opinion and nothing more. Garmin additionally carries 4 undocumented fields (`unknown_90`, `unknown_134`, `unknown_135`, `unknown_136`); Strava carries none.

**Paired traces are a test harness for §5 — this changes how the matcher is built.** Cross-run hill matching has no ground truth. But the watch and phone traces of the *same* hill are a **known-positive** pair, and two different hills within the same run are **known-negatives**. Every dual-recorded run therefore yields free labelled test data: N positives and N(N−1)/2 negatives. Matching tolerances can be calibrated against genuine receiver disagreement (the separation table above) instead of tuned by eye.

**Implication: the matcher must be callable on an arbitrary pair of traces, not only against the stored segment library. Design for that from the start.**

**An out-and-back run supplies a SECOND free test set.** Pairing records <20 m apart and ≥600 s apart on an out-and-back yields hundreds of matched-terrain pairs, and a large share of them have **mirrored grade** (the same ground traversed in the opposite direction).

Those mirrored pairs are **known-negatives for direction-sensitive matching** — same ground, opposite direction. (§5 originally required these to be *different segments*; since the slope redesign, segments are direction-free places and direction lives on the effort, so the mirrored pairs test that two efforts on one segment get opposite directions.) They come from a single file and need no second device. This **supplements** the watch/phone harness above rather than replacing it: the phone pair tests positional tolerance, the out-and-back pairs test direction discrimination.

It is also what makes §6.7 possible at all — matched-terrain drift needs repeated terrain within one run.

**Across runs, a corpus typically holds a few repeated-terrain clusters** (defined in `config/athlete.yaml` under `clusters`; a run outside every cluster is `OTHER`). Here the two that mattered were the home trail and a flat urban cluster. The flat urban cluster is the designated first matcher target (§5.2): flat, paved, open sky, so a failure there is a matcher bug rather than GPS error.


### 14c. Environment vs. fatigue — method established, question NOT answered

**Read this section as a METHOD VALIDATION, not as a conclusion about heat.** The single-run finding below is not the point. The point is the cross-run machinery it establishes.

**The question this is for.** Comparing a cool-season run against a hot-season run, at matched terrain and matched pace, to determine whether an HR increase is **heat** or **fitness loss**. That is answerable only by the §2 residual model fit across many runs with temperature as a term. **This analysis establishes and validates the method. It does not answer the question.**

**What one run can and cannot do.** Across a single run's window, interpolated station temperature, dew point and relative humidity were each collinear with elapsed time at |r| ≥ 0.96 (wind was less tightly coupled, r ≈ 0.8, but still monotonic). **Heat and fatigue are one dimension within a single run and no fit can separate them.**

**One driver CAN be eliminated — by the sign of an exogenous variable, not by a fit.** In the run examined, three of the four weather variables moved toward *lower* heat stress over the run (dew point fell, humidity fell substantially, wind rose); only dry bulb rose. Meanwhile HR drifted upward at matched terrain and matched speed (§6.7). **Rising humidity was therefore eliminated as the driver — it went the wrong way.** Temperature and fatigue remained perfectly confounded with each other and with time. This is the general move: **within one run you cannot fit a weather term, but a variable that moved the wrong way for the observed effect can still be ruled out.**

**State the window with every corpus count.** An early audit understated the dataset's time span; a full query showed it was much longer. A count of activities is window-dependent, so **always state the window** it was taken over, and query the account rather than trusting a remembered range.

**CORRECTION — `coverage_fraction` is the WRONG GRAIN for gating terrain analysis.**

It is a per-*activity* number, so it forces an all-or-nothing call on a run that may be 80% clean. GPS drops mid-run and recovers — a trail can have a dark spot lasting seconds — and **the terrain data before and after a gap is perfectly good**.

**Coverage must gate WHICH PORTIONS of a run are terrain-usable, not whether the run is.**

**Mechanism (implemented with silver, §12 milestone 2).** Derive usability per record from position presence and local continuity, not from an activity-level average:

- A **positioned block** is a maximal run of consecutive records carrying position with **≤30 s** between them.
- A block is **terrain-usable** when it spans **≥60 s and ≥200 m** — enough to support the ~30 m grade window of §9.
- `records_enriched.terrain_usable` is true for records inside such a block. Grade, GAP and segment matching read only those records; **HR, cadence, distance and pace read everything.**

**The figures are REPRODUCIBLE — the `terrain_blocks` view implements exactly this definition, so the prose and a query cannot drift apart (the trap §14c already hit once):**

```sql
-- activities contributing terrain data
SELECT count(DISTINCT b.activity_id) FROM terrain_blocks b
JOIN activities a USING (activity_id)
WHERE b.terrain_usable AND a.source = 'garmin_watch';

-- terrain-usable records
SELECT sum(b.n_records) FROM terrain_blocks b
JOIN activities a USING (activity_id)
WHERE b.terrain_usable AND a.source = 'garmin_watch';
```

**Recounted at that grain, the usable corpus was far larger than the whole-activity rule implied:** the number of activities contributing terrain data rose by more than half, about 80% of watch records were terrain-usable, and **roughly a fifth of all usable terrain records came from activities the whole-activity rule discards entirely.** The clearest case was an activity at about a third coverage whose GPS never fixed for most of the run and then tracked cleanly for its last stretch — one long usable block that the old rule threw away.

**`coverage_fraction` remains useful as a per-activity summary and a §15.4 warning, but it must not be the gate.**

**GPS coverage, screened from the traces — the whole-activity view (see the correction above).**

**SUPERSEDED — this whole-activity definition is kept only to explain older, smaller terrain counts.** The gating definition is the per-portion one above. A run is *whole-activity* terrain-capable when it has at least one source with `coverage_fraction >= 0.99 AND gps_contiguous`.

```sql
SELECT count(*) FROM runs r WHERE EXISTS (
  SELECT 1 FROM activities a WHERE a.run_id = r.run_id
    AND a.coverage_fraction >= 0.99 AND a.gps_contiguous);
```

The coverage buckets used in that screen (watch files only; a file rejected for corrupt timestamps, §9, never reaches them):

| bucket | definition |
|---|---|
| full | `coverage_fraction >= 0.99`, contiguous |
| near-full | 0.90–0.99, contiguous |
| partial or scattered | < 0.90 |
| no GPS at all | no positioned records |

Under the whole-activity rule, terrain-based work (segments, grade, matched-terrain drift §6.7) had only about half the corpus to work with. **The Garmin API's `startLatitude` count overstated even that and missed every partial-coverage run** — do not count GPS-capable runs from the API summary.

**Two counting traps, both hit during the audit:**
- A naive `SELECT count(*) FROM activities WHERE coverage_fraction >= 0.99 AND gps_contiguous` **double-counts dual-recorded runs**, because it includes the Strava phone activity of a watch/phone pair. Count at the **run** grain, or filter `source = 'garmin_watch'`.
- A short fragment sitting just *below* the 0.99 threshold is excluded here — and it must also be excluded from the matcher set (§5.2), so the two sections agree. Check that thresholds applied in two places draw the same line.

**The `startLatitude` concern was real but changed nothing.** It *is* the first known fix rather than the start, but its drift from the true first position measured in the tens of metres at most, and **no run changed cluster** when reassigned from the trace centroid. Confirm from the trace as §5.2 says, but do not expect it to move anything.

**GPS coverage failure was SYSTEMATIC, not random.** Bucketed by calendar period, median coverage was 100% with no late fixes for months, then dropped sharply with late fixes (>300 s to first fix) on most runs in a later period.

**Canopy did not explain it.** Both clusters degraded over the same window — **the open-sky urban cluster failed too.** Hour of day, duration and distance were all negligible correlates (|r| ≤ 0.22).

**Leading hypothesis: stale satellite prediction data (EPO/ephemeris)**, which the watch caches during sync and which balloons cold-fix time when it goes stale. The *upload* pattern is an independent check: blocks of near-consecutive Garmin activity IDs spanning weeks indicate long stretches between syncs. Infrequent sync → stale prediction → long cold-fix times, regardless of sky view. **Practical consequence: sync the watch regularly before trusting a cold start.**

**Look for clusters you did not define.** The audit found a location cluster that had never been configured, on terrain comparable to neither configured cluster. Clusters now come from `config/athlete.yaml`; a run outside every configured cluster is labelled `OTHER`, never placed by coordinate.

Both consequences of a corpus spanning many months matter, and they pull in opposite directions:

- **Temperature spread is BETTER than a single-season block would give.** More identifying variation for the temperature term.
- **The fitness confound is WORSE.** Many months of fitness change, not a few weeks. **The model unambiguously requires a genuine fitness/date term** — over that span, fitness is not approximately constant and cannot be treated as noise.

**The identifying variation is DAY-TO-DAY temperature swing, not a seasonal trend.** Where temperature is large and *non-monotonic* within a season, a cool day one week against a hot day two weeks earlier puts temperature and fitness moving in **opposite** directions, which is exactly what breaks the confound. Seasonal trend alone would not — it moves monotonically with fitness, reproducing the within-run problem at a longer timescale. **Residual risk:** if fitness changes a great deal across the corpus, the model needs *both* terms or it will attribute one to the other.

**Historical volume should be read from the data, not recalled** — an audit can find longer historical runs than had been assumed, which matters for the durability work in §6.4.

**Headwind — a LIMITATION, not a finding.** Headwind component is the only environmental variable not collinear with time (r ≈ 0 with elapsed), because heading swings constantly while wind direction does not. After removing GAP and the elapsed trend, `corr(residual, headwind)` was **+0.05 to +0.08** depending on how station direction is carried between observations (§8). No detectable effect.

**Do not read that as "wind does not matter."** The trail ran through sheltered terrain, and the nearest station was an open-ground anemometer some distance from it. **Station wind is close to meaningless for conditions on a sheltered trail, so the null may be measuring the wrong place rather than measuring no effect.** The same caveat applies with less force to temperature (canyon shade, cold-air pooling) and **least of all to dew point**, which is an air-mass-scale quantity that transfers well over distance — which is why several stations tens of kilometres apart agreed within 0.5 °C. **This is the concrete argument for an on-trail record of perceived conditions, which this build does not collect:** no station and no gridded model resolves trail microclimate.

---

### 14h-CORRECTION. The dropout finding was an AGGREGATION ERROR

**RETRACTED: "dozens of mid-run dropout spans across the DEM-covered activities, all at one location."** Re-checked when the first 1 Hz run reported 100% coverage:

- **All of the gap spans came from ONE activity** — a run already flagged `gps_coverage` at about 25% and `gps_scattered` (positioned records in several separate blocks). They clustered at one location because they were one run, not because the location was special.
- **Exactly one DEM-covered activity had ANY mid-run gap**, and corpus-wide only a handful did, with nearly all gap onsets belonging to that single fragmented run.
- **The suspected site was not on the main route.** The reference run's closest approach was several hundred metres, and the only activity with fixes near it was the same fragmented one.

**Everything downstream of that error is void**: the location prediction "holding", the sky-view comparison (it measured a place one run visited), and the riparian-vegetation reframe built on top of it. **The bare-earth caveat and the tile-edge limitation below remain correct** — they are properties of the method, not of the finding.

**The lesson is the aggregation trap: before reading a pattern as a property of a place, count how many independent runs produce it.** Thirty spans from one run are one observation.

**WHAT THE DATA ACTUALLY SHOWS: there is no recurring, location-fixed dropout in this corpus.** That does NOT contradict an on-the-ground observation of dead spots where cell signal also fails. It means **absence of position is the wrong detector**: the watch appears to keep emitting positions through degraded reception rather than dropping records, so `coverage_fraction` cannot see a dead spot at all. Any future test must look for degraded fixes — track jitter, speed discontinuity — not missing ones.

**Checked on the first 1 Hz run, and it was clean.** Track jitter (perpendicular deviation from a ±3 s chord) was **median 0.53 m, p90 1.53 m**, with one stretch of near-zero jitter that was the athlete **standing still** (speed 0.00 throughout), not dead-reckoning. So on that run there was no evidence of interpolated positions.

**That run's 100% coverage therefore distinguished nothing.** Its route did not pass near the suspected site, and almost every prior DEM-covered run had no mid-run gaps either. **It is not evidence for stale ephemeris and not evidence for dead-reckoning.** The stale-ephemeris test (§14) still needs a run that actually crosses a known dead spot.

### 14i-bis. Heat forced walking, and the pair does not separate dry bulb from dew point

A run intended as **HR-capped** in hot conditions could not be held under the cap while running: the cap forced walking for a large share of moving time, and running HR still sat above the cap. **This is a heat observation, not a protocol failure** — the cap was unachievable at that temperature while running at all.

It was compared with a run on the same ground in different conditions:

| | hotter run | comparison run |
|---|---|---|
| temperature | **higher** (by several °F) | lower |
| **dew point** | **LOWER** (by several °F) | **higher** |
| walking share of moving time | **substantially higher** | lower |
| median HR running | same | same |

**THE COMPLICATION WORTH RECORDING: dew point was LOWER on the hotter day.** §8 holds that heat stress on cardiac drift tracks dew point better than dry bulb — but here the run with more forced walking had more dry bulb and *less* dew point. So this pair does not separate the two variables in the direction §8 predicts, and **cannot be used as evidence for either**. It is one pair; a dataset spanning seasons (§14c) is what settles it.

**Median running HR was identical on both runs** — the difference showed up entirely as *how much* running happened, not how hard it was. That is the same pattern as §6.2's flat HR: a runner who holds effort roughly constant lets something else absorb the load. Here the absorbing variable was the run/walk ratio rather than pace. **Methodological consequence: under an HR cap, heat cost appears in the walking share, not in running HR**, so a heat analysis that reads only running HR will see nothing.

### 14h. GPS dead spots: terrain masking REFUTED, riparian vegetation the leading candidate (SUPERSEDED — see 14h-CORRECTION above)

**READ 14h-CORRECTION FIRST.** The finding this section analysed — repeated mid-run dropouts at one fixed location — was an aggregation error: every dropout span came from a single fragmented run. The CONCLUSIONS below are therefore void. The section is kept because its METHODS are sound and reusable: how to separate two GPS failure causes by their signature, how to test terrain masking against a DEM, and two limitations of that test that apply to every future sky-view measurement.

**THE HYPOTHESIS AS IT STOOD.** A reported dead spot sat where the trail drops into a narrow drainage, with sky occluded on both sides. **Cell signal was reported to fail at the same place, which is independent corroboration**, since both need line of sight. Vegetation on the surrounding open slopes was not expected to block L-band much, so terrain masking replaced canopy as the leading explanation.

**TERRAIN MASKING SEPARATES CLEANLY FROM STALE EPHEMERIS, and §14 had previously conflated them:**

| cause | signature |
|---|---|
| **terrain masking** | mid-run dropouts at **FIXED LOCATIONS**, on every traversal |
| **stale ephemeris** | cold-start failure at the **START** of a run |

**THE LOCATION PREDICTION APPEARED TO HOLD** — dropout spans across the DEM-covered home-trail runs all fell in one small area, and the spans were short (tens of metres straight-line). This is the observation 14h-CORRECTION retracted.

**THE MECHANISM TEST.** Computed from the 1 m DEM already on disk: horizon elevation angle over 36 azimuths out to 500 m, at the midpoint of each dropout span, compared against control points on full-coverage runs more than 150 m away. **The dropout site had MORE open sky than roughly 90% of the control points, not less.** If terrain were occluding the sky on both sides, its horizon angle would sit at the top of the control distribution; it sat at the bottom.

**What this test does and does not rule out.** The DEM is **bare-earth lidar — it contains no vegetation**, so this tests *terrain* masking specifically and says nothing about canopy either way. It covers only ground inside a loaded tile. And a first attempt measured the last fix *before* each gap, which is where the signal still worked; the corrected method uses the **midpoint of the gap span**.

**TWO CHALLENGES TO THE MEASUREMENT, BOTH CHECKED — and both left the result standing.**

**Challenge 1: mean horizon angle is the wrong statistic.** GPS satellites are not uniformly distributed across the sky. At mid-northern latitudes the 55° orbital inclination leaves a void toward the pole and concentrates satellite-time toward the equator-facing sky. A drainage open along its axis but walled on both sides could show a LOW mean while blocking the sky that matters. **Recomputed by 30° azimuth sector (mean of sector maxima over the equator-facing sectors) and as a GPS-weighted mean horizon.** Both statistics put the dropout site at **p0 — the most open of every point measured.** Correcting for satellite geometry *strengthened* the result rather than reversing it; one sector had a negative horizon angle, i.e. the terrain falls away.

**GENERAL LIMITATION — HORIZON RAYS TRUNCATE NEAR A TILE EDGE.** Rays run to 500 m, so **any site within 500 m of the loaded tile's boundary has truncated horizon in that direction**, and those azimuths return no data rather than a low angle. This is a property of the method, not of any site, and applies to every future sky-view measurement. Measure the site's distance to each tile edge before reading the result: here the site sat a few hundred metres from one edge and several kilometres from the other three, which is exactly why the azimuths facing that edge came back empty. **The other directions were unaffected; the near-edge sky was simply unmeasured.** A claim that a site is open in every direction would overstate what such a measurement checked.

**Challenge 2: the straight-line midpoint may not be where the athlete ran.** With no fixes during a dropout, the midpoint interpolates across terrain rather than following a trail that may drop into a notch. **Checked against real fixes from other runs:** the nearest positioned fix and the centroid of real fixes within 40 m both sat about 5 m from the straight-line midpoint, and DEM relief across those fixes was a few metres. **The interpolation was not the problem — the sampled location was on the trail, and it was not a deep notch.**

**That check also showed that almost no other run had any positioned fix near the site.** At the time this was read as near-universal signal loss at one spot; 14h-CORRECTION shows the simpler reading — the other runs did not go there.

#### The reframed hypothesis — RIPARIAN VEGETATION

**VOID — built on the aggregation error.** It is recorded for the reasoning pattern, not the conclusion.

The argument was: the ground is demonstrably open (p0 horizon angle), yet the sky appeared occluded and cell signal also failed; **a bare-earth DEM cannot see vegetation by construction**, and drainage bottoms typically carry denser riparian canopy than the surrounding open slopes. A closed canopy would explain open terrain, occluded sky, shared cell-signal failure and a fixed location at once:

| observation | riparian vegetation would explain it |
|---|---|
| open terrain (p0 horizon angle) | canopy is not in a bare-earth DEM — the ground genuinely is open |
| occluded sky | a closed canopy attenuates L-band far more than sparse vegetation |
| cell signal fails too | same attenuation, same line-of-sight requirement, no coincidence needed |
| one consistent location | the canopy is where the drainage is, and the drainage does not move |
| near-deterministic loss | vegetation is a permanent feature, not a weather- or ephemeris-dependent one |

**TWO TESTS were specified, and both remain the right tests for any real dead spot:**

1. **LOOK UP.** Observe whether the canopy is closed at the spot. Trivial, decisive, free — do it first, because it makes the second test interpretable.
2. **MULTIBAND GNSS (the FR265).** Multiband recovers weak signal under canopy but **not** under solid structure. **If coverage at the spot improves, it was vegetation. If it does not, it is a structure or an occlusion finer than the 20 m sampling step** used in the horizon measurement.

**THE METHOD POINT THAT SURVIVES: record the prediction for new hardware BEFORE the hardware arrives**, so the result is a test rather than a rationalisation. Under terrain masking, multiband would improve such a spot **less** than expected (it cannot see satellites terrain blocks); under canopy it **should** improve. Either outcome is informative. And per 14h-CORRECTION, the detector must be degraded fixes (jitter, speed discontinuity), not missing ones.

**Terrain masking should not be reinstated without a measurement that overturns the p0 result.**

### 14g. New data-collection pattern — short runs from a second entrance

**THE PATTERN.** If runs start from a DIFFERENT entrance to a known trail, covering a subset of the usual route and skipping its first part, possibly with a GPS-poor start, four consequences follow.

**Four consequences to CONFIRM on the first such run, not assume.** Each is a prediction this design makes; a failure is a finding, not a nuisance.

1. **Cluster assignment must resolve FROM THE TRACE, not from the start point** (§14c). §9 already records that `startLatitude` is the first KNOWN position rather than the start location, so a start-point classifier would be wrong here by construction.
2. **The matcher must return a SUBSET of the known places, not new ones.** A subset is correct — the route skips part of the loop. **A new place is a FALSE POSITIVE to investigate**, and it is exactly what the §12.3 hold-out gate exists to catch.
3. **Expect a leading GPS gap.** The §4.3 leading-stop detector must not read it as a forgotten start, and the §14c **per-portion** terrain gate must degrade that portion without discarding the run's HR — §3.1's rule that missing layers degrade rather than forfeit.
4. **Sync the watch before each such run.** This runs §14's stale-ephemeris test on every one of them instead of occasionally, which is the fastest available route to settling it.

**This also relieves §6.8's density problem**: EF on a cluster run infrequently is too sparse to trend, and a higher run frequency fixes that within weeks — though only for the subset of slopes the shorter route covers, which is itself worth checking against the matcher's output.

### 14f. Dew point is the variable a temperature check misses

**FIRST CONFIRMED INSTANCE, and it is the §16.5 case.** On the reference run, the start temperature was unremarkable while the **dew point sat above the ~21 °C / 70 °F line where evaporative cooling largely stops working**, and held there for the whole run. A pre-run check of temperature and rain alone would have read it as unremarkable; the dew point that makes it oppressive is invisible to that check. §8 records that heat stress on cardiac drift tracks dew point far better than dry bulb, so this is exactly the condition the §16.5 alert exists to surface **before** the run rather than explain after it.

**CORRECTION to a figure carried in from another chat:** the dew point quoted there was several degrees lower than the value measured from `weather_records` for that activity. The error was in the conservative direction — the real value was **worse** than reported, which strengthens the §16.5 case rather than weakening it. **The lesson: re-derive a weather figure from `weather_records`, never carry one in from a conversation.**

**CAVEAT — such a reading is PROVISIONAL while the run is inside the archive lag.** Its `weather_source` is `forecast`, not `era5`, and the figure should be re-read once ERA5 backfills it. **Do not treat it as final** until `weather` reports the row as `era5`.

### 14d. Hardware and recording configuration changes

**1. RECORDING SWITCHED TO EVERY SECOND (1 Hz), from Smart Recording.** This is the largest of the three changes because it alters the dominant error term in §14b.
- **Chording loses 5.0% of path length at 7.5 m sample spacing (§9/§14b). At 1 Hz that spacing falls to ~2 m and most of the loss goes away** — a straight-line chord across 2 m of a curving trail under-reports far less than one across 7.5 m.
- **The watch-vs-GPS speed comparison (§9, grade-biased `speed_mps`) was measured on a 4 s file and MUST be redone once 1 Hz FR245 runs exist.** The chording correction is an input to that comparison, so changing the sampling rate changes the correction and therefore the conclusion. Do not carry the 4 s result forward.
- **`sampling_regime` needs nothing set — it is detected from the file.** Detection reads the observed median inter-record gap against `ONE_HZ_MAX_MEDIAN_GAP_S = 1.5 s`. The Strava phone file ingests as `1hz` (median gap 1.0 s), while every Smart Recording Garmin file ingested as `smart_recording` (median gaps 3.0–9.0 s). A 1 Hz watch file reports `1hz` automatically.
- **One clean 1 Hz FR245 run on the home trail before a watch swap is the hardware-boundary baseline.** Without it every FR245-to-FR265 comparison confounds the watch change with the recording-rate change.

**2. The FR265 has MULTIBAND GNSS (L1 + L5).** Record precisely what this does and does not fix, because the tempting inference is wrong.
- **DOES fix: horizontal error, canopy dropouts, and the coverage failures §14c found in a minority of runs.** Dual-frequency resolves most ionospheric delay and rejects multipath, which is the mechanism behind both the wander and the dropouts.
- **DOES NOT fix the need for the DEM.** §5a-bis measured that horizontal error is **not the dominant term**: smoothing the *track* barely moved the gain figure, while smoothing the *elevation* moved it a lot. GPS altitude is roughly 1.5–3x worse than horizontal for geometric reasons that multiband does not change. **Better positioning does not make GPS altitude usable, and the DEM is not a workaround to be retired when the hardware improves.**
- **DOES NOT fix the grade-biased `speed_mps` (§9).** That is accelerometer/GPS fusion inside the watch, not a positioning problem. A better fix does not change what the fusion does with it.
- **Corollary:** multiband improves the *inputs to the matcher* (§5.3) by reducing position scatter, but the matcher's discriminating signal is DEM elevation, which multiband does not touch.

**3. Barometer — no change.** Already recorded in §14 with its calibration test (same trail, materially different weather). Treat as unknown-provenance per §15.1 until that test is run.

**SCHEMA IMPACT.**
- **Running dynamics: NO migration needed.** `vertical_oscillation`, `stance_time`, `stance_time_pct`, `vertical_ratio`, `step_length` and `power` all exist on `records` and are all nullable. FR265 files populate them; FR245 files leave them NULL.
- **Barometric altitude: DONE.** `records.altitude_gps_m` is renamed **`altitude_device_m`**, with a co-located **`altitude_source`** column (`gps` | `barometric` | `dem_suspected` | `unknown`). **Provenance is readable without joining `activities.device`**, which was the requirement. Chosen over separate per-sensor columns because a device reports exactly ONE altitude series, so per-sensor columns would be all-NULL-but-one per row, force a `coalesce` into every query, and need a schema change per new provenance — whereas a value+source pair absorbs a new provenance as a new string. It also matches the pattern already in this schema: `moving_time_s` / `moving_time_source`.
- **The old name was ALREADY WRONG before the FR265 — for Strava, since first ingest.** §5a-bis.1 and §15.4 conclude the phone's altitude is most likely a **server-side DEM**, not GPS: its ascent and descent close to within about 1% on a loop that physically closes, which a per-second GPS series does not do by accident. Ingested as `dem_suspected` (evidence-based, unconfirmed) so a query can exclude it from any comparison against our own DEM **without re-deriving the §15.1 circularity argument**. Verified after rebuild: FR245 records carry `gps`, Strava records carry `dem_suspected`.
- **`unknown` is deliberate and must stay.** An unclassified device gets `unknown` rather than defaulting to `gps`. §15.1 makes provenance decide whether elevation is INDEPENDENT of our DEM, and a wrong default there produces a confident comparison of a DEM against itself.
- ~~**Barometric altitude: MIGRATION IS NEEDED, and it is a naming problem, not a missing column.**~~ `enhanced_altitude` is mapped to `records.altitude_gps_m`. On a barometric watch that FIT field carries **barometric** altitude, so FR265 data would land in a column named `_gps_` and become indistinguishable from FR245 GPS altitude by schema alone. **That silently defeats the §14 barometer provenance test**, which depends on knowing which sensor produced a value. Provenance is still recoverable by joining `activities.device`, but the column name would be an active lie. Blast radius is small (5 references). *(Resolved by the rename above.)*

### 14e-ter. THE MAX-HR QUESTION: separating a recorded ceiling from a demonstrated one

**A number reached on an effort the athlete does not consider maximal is a *recorded* ceiling, not a *demonstrated* one.** If the efforts that produced a corpus peak were rated submaximal, the peak says only that the ceiling is at least that high. §14e-bis's arithmetic quietly assumes the recorded and demonstrated ceilings are the same; this section is the tooling for telling them apart.

`hr_above(threshold, selection)` and `time_above_hr(...)` exist to do that. Both work at **record grain**, on **raw `heart_rate`** — never `hr_shifted`, because the lag correction aligns HR to the effort that caused it, which is what matching needs, while a ceiling wants the reading as recorded.

#### The session summary and the record stream are BOTH the watch

§6.0 originally stored `max_hr_device` beside the record-stream maximum and called a difference between them "an INGEST finding". **That framing was wrong and the name invited it.** Both figures come from the SAME FIT FILE, written by the same watch: `session.max_heart_rate` is a summary the watch computed, and the record stream is what it chose to write. There is no second source to disagree with.

**What the difference actually measures is RESOLUTION.** The watch samples heart rate continuously and writes a record only on the Smart Recording schedule; the summary is computed over the full internal stream. The record-stream maximum is therefore a **decimated view** of it. Measured across the corpus:

| regime | disagreement | direction |
|---|---|---|
| **1 Hz** | **none** — every activity agrees exactly | — |
| **Smart Recording** | **about 40%** of activities | summary higher on every one, by **+1 or +2 bpm** |

**The sign never reverses, and it cannot: skipping a sample can lose a peak, never invent one.** The disagreement rate rises with the run's own median gap: 0% at 1 s, then 20–60% at 3–9 s.

**CONSEQUENCE FOR THE CEILING QUESTION.** On a Smart Recording run the record stream **understates what the watch saw by up to 2 bpm**, so the corpus peak and the best-effort figures read from records are low by that much. The field is now `session_max_hr`, the comparison is `session_agrees`, and the note says which figure answers which question: **the summary is the better ceiling; the record stream is the only one that can be placed in time or checked for cadence lock.**

#### What the corpus says near the ceiling

`hr_above` returns **EXCURSIONS, not records**, because a sustained bout and a spike must not look alike. Each excursion carries: date, start offset, span, record count, flagged-record count, peak, mean, the 30 s window centred on the peak, pace, grade, cadence, and `sampling_regime`. An excursion breaks on a below-threshold record **or a gap over 3x that run's own median gap** — measured per run, since a hole at 1 Hz is a normal interval under Smart Recording.

**THE FINDING THAT MATTERED WAS CONCENTRATION.** Read this way, half of the excursions above the high threshold belonged to ONE run — and that run's provenance was the open question. Without it, the remaining excursions were a few seconds each, two records apiece, touching the threshold exactly. A ceiling that rests on one run is a different fact from one the corpus repeats.

#### Time at or above, and the answer to "repeatedly or twice"

`time_above_hr` reports, over a ladder of thresholds, the **minutes** and the **number of runs** at or above each one — and **every total a second time with the dominant run removed**. Time at a heart rate is a DOSE, not a rate, so stopped time is not subtracted (timer-time weights, capped per record).

On this corpus, every second above the highest rung came from that one run, and the large majority of the time above the next rung down did too. **The with/without pair is what answers "has this been reached repeatedly, or once?"** — a total alone cannot.

#### What that run looks like from the inside

A dominant run's longest excursion can be **sustained, not a touch**: a bout of minutes held within a few bpm of its peak. Where §14e-bis's checks rule out cadence lock and fatal flags, **the number is real; what is unestablished is that it is a maximum.** A heart that holds a high rate for minutes on an effort not rated maximal has not been shown its ceiling — it has been shown **a floor under its ceiling**.

**THE TOOLS REPORT, THEY DO NOT DECIDE.** Flagged records are **counted and kept**, never dropped: at the top of the range a §15.4 flag may mark a real artifact or a real spike and the flag alone cannot say which, so the athlete judges with the record in front of them. The dominant run is **marked and never excluded**, with every total reported a second time without it — answering a ceiling question by deleting the evidence would be the worst available failure.

**WHAT WOULD SETTLE IT.** Not more analysis of the existing corpus: a deliberate maximal effort — a ramp test, or a hard finish rated 10/10 — recorded at 1 Hz. If every high excursion in a corpus is **Smart Recording**, the coarser resolution, the corpus cannot settle the question on its own.

### 14e-bis. Is the zone anchor below or above the demonstrated ceiling?

This was built to test a specific worry: that zones are anchored to a max HR the athlete has never reached, shifting every boundary upward. **The check compares the declared anchor** (`time_in_zone.max_heart_rate`, read from each file) with the corpus's highest single sample, its highest 30 s rolling mean, and Garmin's own summary for that run — and the gap can run either way.

**Before believing a peak, run three independent checks:**
- **Not a spike:** the single highest sample and the highest 30 s rolling mean should agree within about 1 bpm on the same run, showing the rate was held rather than touched.
- **Not cadence lock** — the standing artifact for wrist optical HR: cadence through that window should sit clearly apart from HR, with only a few percent of the run's records within 3 bpm of the step rate (a weak physiological HR–cadence correlation, not a lock).
- **Not flagged:** the run should carry no §15.4 flags, and a long, sustained effort is stronger evidence than a brief one.

**IF THE ANCHOR SITS BELOW THE CEILING, THE CONSEQUENCE RUNS THE OPPOSITE WAY TO THE WORRY, and it is worth stating carefully.** Boundaries are `percent_max_hr` of the anchor. Re-anchoring to a higher ceiling moves every boundary up by the same percentages — and HR can sit in a narrow habitual band that **straddles the zone 2 / zone 3 line** (§14e). Re-anchoring upward would then move real minutes **from zone 3 into zone 2**, so an anchor below the ceiling, if anything, **under-counts** zone-2 time rather than over-counting it. Time-in-zone in that band measures where a line falls more than it measures the training.

**REPORTED, NEVER CORRECTED.** `max_hr_over`'s `declared_ceiling` block states the gap and refuses to act on it. Re-anchoring a zone scale is an athlete decision with a coach behind it; a tool that silently recomputed the boundaries would rewrite every time-in-zone figure in the corpus on its own authority.

**THE COMPARISON IS GATED ON THE SELECTION ITSELF.** Over a selection of easy runs that never came near the ceiling, the declared-vs-demonstrated gap measures the SELECTION, not the heart — and it would print beside "every zone boundary is shifted up by the gap". So `declared_ceiling` is reported only if **a run in the selection reached within `CEILING_BAND_BPM` (5 bpm) of the demonstrated 30 s ceiling**. Otherwise it is **refused**, naming the selection's best 30 s value and the band it had to reach. The ceiling is bounded to the selection's `end_date` — the same no-look-ahead bound the declared values carry — so a later run cannot move the band under an earlier selection. The run that set the ceiling is always in its own band, so a selection containing it always qualifies. No run type enters the rule; it needs nothing the athlete did not record on the watch.

**WHEN THE GATE PASSES, THE GAP IS MEASURED AGAINST THE DEMONSTRATED CEILING, NOT THE SELECTION'S PEAK.** Passing the gate says the selection came near the ceiling; it does not make the selection's own best the ceiling. A selection can sit inside the band while the run that set the ceiling lies outside it, and a gap taken from the selection's peak would then point the wrong way — "anchored above what this heart has done" when the evidence in the same payload shows the heart did more. So the gap is declared anchor vs the demonstrated ceiling (bounded to the selection's `end_date`), and **the selection's own peak is shown separately, labelled as the selection's**, never as "the corpus peak".

**AND THE CEILING CARRIES ITS CIRCUMSTANCES.** `max_hr_over` returns the winning run's date, run_id and dew point — because **a maximum found on a hot or compromised run is a different fact from one found on a hard effort.** Its run type and whether it was declared unrepresentative are **not collected** in this build (§16.6), and the result says so rather than implying the run was representative: it has not been declared anything. **Absent context is not FALSE context.**

### 14e. HR zones are DATED CONFIGURATION, not a constant

**The configured zones come from Garmin's RUNNING profile** (`get_heart_rate_zones`, sport=RUNNING; method, max and resting HR are part of the configuration). **Use the RUNNING set, never DEFAULT.** The DEFAULT profile on an account can differ substantially (different `z2Floor` / `z3Floor`) and would misclassify nearly every record.

**Validate the zone set two ways.** Garmin's stored `hrTimeInZone_2` for a recent run matched the time-in-zone the athlete read off the watch; recomputing time-weighted from our own `records` over the same bounds came within **~1%**, and all five zones agreed within seconds to tens of seconds.

**A HABITUAL BAND CAN STRADDLE A ZONE BOUNDARY.** If HR sits in a narrow band across every grade (§6.2) and that band crosses the zone 2 / zone 3 line, then "time in zone 2" is not a training decision — it is the **top of zone 2 and the bottom of zone 3**, and the split between them is a boundary artifact. A coaching layer that reports "time in zone 2" without that context will describe where a line falls as if it were what the athlete did.

**THE ZONES MOVE, AND GARMIN'S STORED TIME-IN-ZONE IS NOT ON A FIXED SCALE.** Fitting, per activity in date order, the zone-2/3 boundary that best reproduces Garmin's own `hrTimeInZone_2` showed the effective boundary **drifting across months** (agreement with a fixed recomputation poor), then **settling** — today's `z3Floor` reproduces Garmin's numbers only for activities after the last change. Boundaries can move on their own when Garmin's resting-HR auto-update (`restingHrAutoUpdateUsed`) is on.

**Consequences.**
- **Never pool Garmin's stored `hrTimeInZone_*` across the corpus** — it would compare at least two different definitions of zone 2. This is the same failure class as §6.2's "never pool run and walk": a quantity that looks like one measurement but is two.
- **Recompute time-in-zone from raw HR with one explicit boundary set**, versioned like `garmin_selfeval_scale` (§16.6).
- **Boundaries are ATHLETE CONFIGURATION and must be stored DATED**, in `data/athlete/` outside the rebuild path (CLAUDE.md).

> ~~they cannot be regenerated from the FIT files, and the historical values are already lost for activities before the last change. Only the current set is recoverable from the API.~~
>
> **REFUTED. The historical boundaries are NOT lost — every FIT carries them.** Each file holds `time_in_zone` messages with `hr_zone_high_boundary` and `hr_calc_type`, at both lap and session scope. A file can record a different boundary set *and a different method* (`hr_calc_type`) from today's.
>
> **A corpus can hold several distinct boundary sets**: one is today's, matching `hr_zones.json`; the others differ in anchor, spacing and method, and some may cover a single activity each.
>
> **And the FIT's own `time_in_zone` reproduces Garmin's `hrTimeInZone_*` BETTER on the activities before the last change than after it**: median max |difference| 0.00 s and worst about 2 s before, against 0.00 s median but a worst case of over two minutes after.
>
> **CONSEQUENCES.** Time in zone is computable on **every** Garmin activity, not only those after the last change. It must be computed **per activity from that activity's own recorded boundaries**, never by applying today's configuration retroactively. `hr_zones.json` remains the *current* athlete configuration and the thing the coach reasons about going forward; it is not the historical record, and the files are.
>
> **The no-pooling rule SURVIVES, for a better reason.** Time in zone must still never be pooled across a boundary change — but not because the past is unknowable. It is because genuinely different scales exist, and a minute in "zone 2" means a different physiological thing under each. Knowing the boundaries makes the pooling *detectable*, and in principle makes conversion possible; it does not make pooling valid.
>
> *Why the original claim was wrong:* it was inferred from the Garmin **API**, which returns only the current zone set. Nobody looked in the FIT. The lesson is §4.2's: the file is the record, and the API is a convenience over it.
- Status is **COMPARISON ONLY** (§4.2), and only for activities recorded under the current boundary set.

## 15. Cross-signal triangulation and confidence propagation

**Principle.** Quantities in this system are estimated by multiple sources with **uncorrelated failure modes**. Agreement is evidence. Disagreement is *localizing* — it points at a specific point on a specific segment, not at a vague global error bar. The goal is never a single "true" value. It is **a value plus a confidence derived from inter-source agreement.**

This reframes the whole system: §14b's watch-vs-phone comparison is not a curiosity, it is the first instance of the general method.

### 15.1 Elevation estimators

| estimator | failure mode |
|---|---|
| barometer (on a watch that has one) | drifts with weather pressure over hours |
| GPS altitude | always unreliable — the weakest axis of a fix |
| DEM at GPS coordinates | fails when **horizontal** position is wrong |
| Strava / Garmin "corrected" elevation | inherits *their* DEM and smoothing — **not independent** of our own DEM lookup |
| segment canonical profile (§5a step 3) | drift-immune once built from enough traversals |

**The Strava/Garmin corrected figures must not be treated as an independent check.** On one dual-recorded closed loop, Strava's total ascent and total descent balanced to within **about 1%**, while Garmin's differed by **about 5%**. A noisy per-second GPS altitude series does not balance to 1% by accident. Strava's near-perfect closure is the signature of a **smooth, deterministic elevation source — most likely a DEM applied server-side.** If so, comparing our DEM lookup against Strava's number is comparing a DEM to a DEM, and the agreement would be meaningless.

**Second instance of the same pattern: Garmin's `rwd_*` splits and the API's `movingDuration` are NOT independent estimators.** Splits agree with `movingDuration` (median **−2.4 s**) more closely than with our own detector (**−7.0 s**), which is evidence `movingDuration` is derived server-side from the same on-device segmentation. **Checking one against the other is circular — the same trap as Strava's DEM-derived elevation above.** Garmin's `rwd_*` algorithm is also undocumented, so *"in the file"* is not the same as *"transparent"*. **OUR detector is the only channel whose threshold can be stated, which makes it the independent signal rather than the fallback.** Splits are used as primary (in-file, finer-grained, distinguishes run from walk); the detector is retained alongside as the uncorrelated channel, and is the only source for Strava files, which carry no split messages.

**Note — Strava does not crowdsource the position track.** The trace is the phone's own GPS. Only *segments* and *heatmaps* are crowd-derived. Do not attribute crowd-averaging properties to the position data.

### 15.2 Cadence estimators

Wrist accelerometer · phone pedometer (session total only) · pace · heart rate · Garmin `rwd_run`/`rwd_walk` labels.

§14a is the worked example: an anomalously low cadence reading on a steep descent was correctly resolved as **hiking, not sensor error**, by four independent channels agreeing — the `rwd_walk` label, the matching speed, the plausible derived stride, and the whole-run cadence distribution. No single channel would have settled it.

### 15.3 Trail width context

Median inter-device separation between the watch and phone traces is **about 6 m** (§14b), on singletrack roughly **2 m wide**. At least one of the two traces is therefore **regularly off-trail** — frequently both.

This is cosmetic for mapping and **fatal for DEM lookup by raw coordinate**: an 8 m lateral error on a side slope returns that slope's elevation, not the trail's. It directly reinforces **§5a step 3 — look elevation up by *distance along segment*, not by coordinate.** The segment library is not a convenience feature; it is the mechanism that makes elevation trustworthy at all.

### 15.4 HARD REQUIREMENT — quality flags propagate to the coaching layer

Binding on §12 milestone 8.

- **Quality flags propagate from silver → gold → coaching layer.** They are not dropped at any tier boundary.
- **Every recommendation must be traceable to measurements that passed their quality checks.**

**These are two different things and must not be conflated.**

**The carrier is `records_enriched.quality_flags`.** Before it existed, every report had to state that per-record exclusion was NOT ENFORCED, because the carrier this section names did not exist. A report must still say so for any primitive that cannot honour the flags (below).

**EACH SIGNAL IS A NAMED FLAG, NEVER A COMPOSITE SCORE.** A score invites "0.4, a bit unreliable, use it anyway"; a named flag forces the exclusion this section demands and says *which* artifact was found. Stored comma-separated, empty when clean.

**EVERY THRESHOLD IS DERIVED FROM THE MEASURED DISTRIBUTION, NOT ASSUMED.**

| flag | rule | where the threshold came from |
|---|---|---|
| `gps_jitter` | normalised jitter > **2.0** | p99 is **1.63** (1 Hz) and **1.52** (Smart); 2.0 flags **0.34% / 0.82%** |
| `speed_discontinuity` | \|accel\| > **3.0 m/s²** | PHYSICAL, not a percentile: 0→10.8 km/h in one second. Nothing in the 1 Hz corpus exceeds it (max 2.5); Smart Recording's max is **7.1** |
| `cadence_dropout` | 0 spm while > 1.5 m/s | measured **1.46% / 0.50%** of records; §14a found a handful on the reference run |
| `cadence_stuck` | identical non-zero cadence ≥ **15 s** while speed varies > 0.3 m/s | runs of frozen cadence measured at median 6 s, **p95 14 s**, max 20 s at 1 Hz |
| `split_transition` | within **3 s** of a `split_type` change | §14a's cadence excursion at a run/walk change; measured decay: cadence sits well below the running baseline at 0 s and is back within about 1% of it **by 3 s** |

**JITTER MUST BE NORMALISED BY POINT SPACING, and that is the load-bearing decision.** Raw perpendicular deviation is **not comparable across sampling regimes** — p99 is **3.11 m at 1 Hz against 13.58 m at Smart Recording**, purely because points sit ~4× further apart. Divided by the run's own median step length the two agree: **1.63 against 1.52**. A raw threshold would flag most Smart Recording data and almost no 1 Hz data, measuring the regime rather than the quality. Corpus flag rates are **4.80% (1 Hz) against 9.95% (Smart)** — the same order, which a raw threshold would not have produced.

**ARM-MOTION DECOUPLING IS DELIBERATELY NOT FLAGGED**, despite being named in this section's own artifact list. **§14a measured it**: the trend sign came out POSITIVE across the descents measured — the opposite of the hypothesis — and run/walk transitions turned out to be the real instability. **A flag for it would encode an unsupported hypothesis as data.** It stays unflagged until something measures it, and §14a's transient caveat is a RESOLUTION LIMIT, not an artifact.

**WHAT HONOURS THE FLAGS, AND WHAT CANNOT.** Record-grain primitives exclude flagged records outright — `matched_terrain_drift` and `grade_binned_pace`, each declaring `quality_flags_excluded: true`. **Segment-grain primitives cannot**: `efficiency_factor`, `delta_hr` and `segment_effort_drift` read `segment_efforts`, which aggregates records upstream of the flags, and each declares `quality_flags_excluded: false`. **Stated rather than implied**, and the report repeats it in `unenforced`.

**The exclusion measurably moves numbers**, which is the point: on the reference run, excluding flagged pairs removed about a quarter of `matched_terrain_drift`'s record pairs and lowered the drift estimate modestly — still inside §14a's parameter-sweep range, so this is a refinement rather than a contradiction.

#### Artifact — the measurement is WRONG

Carried in `records_enriched.quality_flags`. Cadence lock, arm-motion decoupling (§14a), run/walk transition spikes, GPS dropout, pauses.

- **EXCLUDED from recommendations, not down-weighted. No partial credit.**
- Concretely: if cadence on a descent carries a possible-artifact flag, **the coach must not make cadence recommendations for that segment** — not a hedged one, not a caveated one. None.
- **Rationale:** a confident recommendation built on an artifact is worse than no recommendation. **An unflagged 22-sample trend beats a flagged 200-sample one.**

#### Resolution limit — the measurement is CORRECT but sparse

Carried in `activities.sampling_regime`. Smart Recording's ~4 s gaps do **not** corrupt cadence: **a steady cadence sampled every 4 s is still the same cadence.**

**Low resolution does NOT trigger exclusion.** It instead **gates which analyses are valid**:

| analysis class | examples | minimum regime |
|---|---|---|
| **Aggregate over a segment** | mean cadence on a descent, cadence at matched pace, week-over-week trends | **valid at Smart Recording** — coach on these |
| **Transient detection** | 2–4 s decoupling events (§14a), within-rep cadence decay (§7), recovery HR at precise offsets, τ fits (§6.3) | **requires 1 Hz** |

- **Every analysis function declares its minimum `sampling_regime`.**
- A transient-class analysis must **REFUSE to run** on a Smart Recording file — raise, do not return a misleading result.

**Rationale for the split:** excluding artifacts protects the athlete. Excluding low-resolution data discards *valid measurement* — it would silence coaching on every historical run for no epistemic gain.

---

## 17. Per-session metrics (IN PROGRESS)

**STATUS.** Tier 1 (Garmin parity) is not yet written up: there is no §17.1 heading, and the measured gap list it would draw on is §17.5. Its one built piece, the resampled series, is written up as §17.1b and §17.1b-bis below. §17.3 (Tier 3) and §17.4 (the promotion path) are written below. §17.2's candidate register was opened first because M1's definition had to be settled **before** it is built, and because the M5 ruling it depends on is in §5.

**`docs/proposed-metrics-v1.md` is in the repository** and its M1–M6 are incorporated below. They are CANDIDATES: a candidate becomes a standing Tier 2 metric only through §17.4's promotion path.

**Build order, accepted from the document: M3 → M2 → M6 → M1 → M4 → M5.** M3 first because it works on the whole existing corpus and answers a live training question.

**The document's own §2 finding is accepted and is already recorded:** the hill-plus-weather normaliser is not a new metric, it is §2's central construct, and three of its four terms are currently refused or too sparse. That is consistent with §2.1–§2.5.

### 17.1b The resampled series: `session_series`

**BUILT — `src/trail_factors/analysis/series.py`, the `session_series` tool.** The data behind Garmin's own charts, as an aggregate. The coach reasons "over the run" — where the drift started, where the walking clustered, whether cadence held on the last climb — and none of that is answerable from run-level scalars. **Nothing has to be fetched**: the cached Garmin summary carries no series, so the series IS the FIT already in `records`.

**BINNED, NEVER RAW.** A raw series is a bronze row, and no tool returns one (§3, §14m). A bin is an aggregate, not a record, which is what keeps that invariant.

**`BIN_S = 30 s`, and the WALK SPLITS decided it, not payload size.** In a run/walk practice many `rwd_walk` splits are shorter than a 30 s bin. 30 s is about the largest bin that still resolves a typical short walk as a distinguishable fraction of a bin rather than as noise. A bin needs `MIN_BIN_COVERAGE = 0.5` of its span covered by records before its means are reported; below that its pace would be a straight line drawn across missing ground.

**STATE IS SECONDS, NEVER A MAJORITY LABEL — the one rule here that is not negotiable.** Every bin carries `run_s` / `walk_s` / `stand_s`. A bin that was 20 s running and 10 s walking says so. A majority label would erase the minority gait in every mixed bin, which on a run/walk session is most of the walking — the failure the series exists to avoid.

**EVERY BIN CARRIES BOTH INDICES**: elapsed seconds AND path distance, so "at minute 40" and "at mile 4" are both answerable from one payload. Each bin also carries pace (`mps`, bin distance over moving seconds), HR and `steps_per_min`; means over records are time-weighted, as everywhere in this project.

**ELEVATION BY DISTANCE IS A ROUTE FINGERPRINT, SO IT IS OPT-IN.** Matched against a known trail's profile it can identify where the athlete runs without a single coordinate. Bins carry `elevation_m` **only when `route_fingerprint_accepted: true` is set in the athlete config** (`config/athlete.yaml`; documented in `config/athlete.example.yaml`); otherwise every bin's elevation is null and the payload says why and how to opt in. The flag is read at call time, never cached. **Position (latitude/longitude) is never emitted, either way.** Elevation and grade exist only where a DEM tile covers the run.

### 17.1b-bis Per-bin running dynamics

**Each bin carries `stance_time_ms`, `step_length_mm`, `vertical_oscillation_mm` and `vertical_ratio_pct`, each as `{value, n, unit}`**, in Garmin's units unchanged (millimetres, milliseconds, percent). Same four fields, same units and same `rwd_run`-only gate as §6.0's `running_dynamics` — reused, not restated, so the two cannot drift.

**`vertical_ratio` IS THE DEVICE'S OWN FIELD and is never recomputed** from oscillation and step length. Deriving it would silently substitute our arithmetic for Garmin's on records where the device wrote a ratio and dropped one of its inputs — which it does.

**n IS PER FIELD, NOT PER BIN.** The device drops the four fields independently: one can be on a handful of records where another is on thousands. A single per-bin n would present a five-sample mean as though the bin's full record count stood behind it.

**THREE ABSENCES, NEVER 0.0, ALL DISTINGUISHABLE** (`dynamics_reason`):

| reason | meaning |
|---|---|
| `no_device_channel` | no record in this ACTIVITY carries the fields — a watch without running dynamics, a phone file, or a non-running sport |
| `no_running_in_bin` | the device records them, but this bin has no `rwd_run` records (walking, standing, a gap). Walking dynamics are not running dynamics and are never substituted |
| null, with that field's n = 0 | running is present and the device records the field, but it wrote none in this bin — the device dropping a field mid-run |

**GAIT CONVENTIONS DIFFER WITHIN ONE BIN, AND THE PAYLOAD SAYS SO** (`gait_conventions`). `steps_per_min` is over ALL records in the bin with cadence > 0 — running, walking and standing steps pooled — and is unchanged; the four dynamics fields are `rwd_run` only. They describe different record sets and must not be read as one gait. **§15.4 flags are excluded from the dynamics where `records_enriched` covers the run**, and `flags_available` says when they could not be: the same figure with and without flags is not the same quantity, so an uncovered run is labelled rather than refused.

### 17.2 Tier 2 — CANDIDATE metrics (promotion path, not yet standing)

| id | metric | §6.6 side | document's status | **verified status** | what unblocks it |
|---|---|---|---|---|---|
| M3 | run/walk cycle economics | **choice** | `ok` on every Garmin activity | **BUILT** — `ok` only where the ≥4-run-block refusal rule passes, `gated` elsewhere | — promoted to standing |
| M7 | **time in HR zone BY GAIT** | **choice** | *(not in the document — raised by use)* | **BUILT**, `ok` corpus-wide | — promoted to standing |
| M2 | descent dose (eccentric ledger) | **capacity** (dose) | `ok` on any DEM-covered run | **confirmed: DEM-covered runs only** | DEM tiles for the rest |
| M6 | pace-floor compliance | **choice** | `ok` | **confirmed** — and see the measurement below | nothing |
| M1 | mode fraction | **capacity** | `unavailable` without running dynamics | **confirmed, and it is total** | a dynamics-capable watch + labels (§17.2.1) |
| M4 | capacity retention | **capacity** | `gated` on most runs | plausible; gate unbuilt | enough hard-finish sessions accumulating |
| M5 | terrain signature distance | **capacity** (demand) | `ok` run-to-run | **overstated: DEM-gated** | DEM tiles; course profiles now PERMITTED (§5) |

**THREE CORRECTIONS TO THE DOCUMENT'S OWN STATUS LINES, measured against the corpus:**

1. **M3 is not `ok` on every activity.** Every Garmin activity carries `rwd_run` splits, but **only about two thirds have the ≥4 run blocks the document's own refusal rule requires.** The status line and the refusal rule contradict each other; the refusal rule is right.
2. **M5 is DEM-gated.** `grade_time_histogram` needs `grade_pct`, which exists only where a DEM tile does — not "any run". Run-to-run comparison is available on that subset only.
3. **M5's governance question is RESOLVED** (§5). A race course profile is a labelled reference object, imported once, stored separately, never joined to the segment library and never matched against. It is the DEM-tile category, not the trail-database category. **M5 may compare against a race profile once one is imported.**

**M6'S FLOOR IS THE ATHLETE'S DECLARED VALUE, NOT THE DOCUMENT'S DEFAULT** — a ruling by the athlete supersedes the proposal's figure. **No floor parameter is stored in this build** (there is no `floor_pace_per_mile` in the code or the config), so M6 has no declared value to read until one is added. And the measurement the document asks for already returns something worth seeing: the share of `rwd_run` moving time above the floor is the quantity to watch, and it can be small. That share is what moves as the gait develops — and that is precisely why `floor_pace_per_mile` must be a **dated declared parameter**, not a constant.

#### M3 IS BUILT AND PROMOTED (`src/trail_factors/analysis/cycles.py`, `run_walk_cycles` tool)

A cycle is one `rwd_run` block plus the walk that follows it; `rwd_stand` does not break one — standing at a road crossing is not the athlete abandoning the structure. A run block with no following walk reports `walk_duration_s = None`, never 0: **no walk and a zero-length walk are different things.**

**`purchase` and `decay` are returned as PAIRED SERIES, never as a fitted slope.** A coefficient over four points has an interval wider than itself, and §6.1 already records what happens when one is trusted from a single run. Every purchase pair carries `elapsed_at_next_run_s`, because time into the run is the obvious confound: later walks are longer AND later runs are slower, and a slope through that reports fatigue as the walk's doing.

**A run can show ACCELERATION rather than decay** — later run blocks faster than early ones, not the decay the structure defends against. A fitted slope would report that as a property of the walks; the paired series leaves it visible as what it is.

**A LIMITATION WORTH WATCHING.** §6.0's `MIN_CLASS_TIME_S = 120 s` gate, applied per run block as the document specifies, **can remove most of a run's blocks from the pace series** wherever run blocks are mostly shorter than the gate. The duration is still reported for every block, but the decay series then rests on a minority of the blocks. That gate was fitted for CLASS paces (leave-one-split-out over a class total), and whether it is the right gate for a single block is a separate question that has not been measured. If the decay series proves too thin to read, fit a per-block gate rather than lowering this one.

**M3 GATES POOLING ON WHAT DECIDED THE WALKS, AND IN THIS BUILD THAT IS NOT COLLECTED.** The purchase curve reads a run block's pace against the preceding walk, which measures *what the walk bought* only if something other than willingness decided the walk — an HR cap or a timer. Walks taken by feel are uncorrelated with terrain and HR, so the same arithmetic would measure willingness instead. The gate is carried on the result (`walk_protocol`, `walk_protocol_status`, `poolable_with_other_runs`, `pooling_note`) and is a constant in this build, `_walk_protocol_gate()` in `src/trail_factors/analysis/cycles.py`: `walk_protocol` null, labelled "not collected", `poolable_with_other_runs` false. No poolable protocol set exists, so nothing about it is registered in `calc_params()`. **With no source for the protocol, every run reports `walk_protocol_status: "not collected"` and is not poolable with other runs** — structure that has not been established cannot be assumed. Each run's own series is still returned in full.

#### M3's PER-WALK RECOVERY IS GATED ON THE HR LAG

**A WALK MUST OUTLAST THE HR TAIL TO BE MEASURABLE, and that is a property of the INSTRUMENT, not of the athlete's walks.**

`hr_recovered_bpm` is the walk's first HR reading minus its last. Heart rate lags effort by ~40 s (`HR_LAG_S`, §14a), so on a 15 s walk the closing reading reflects what the athlete was doing **25 s before the walk began** — on hilly terrain, usually the climb that prompted it. The figure then reads **negative**, and it reads negative because the measurement cannot see the walk.

Observed on the run that prompted the gate: every negative-recovery walk was preceded by climbing.

**THE GATE.** `hr_recovered_bpm` is **withheld** below `MIN_WALK_FOR_RECOVERY_S = HR_LAG_S + 5 s = 45 s`, with `hr_recovery_status: "shorter_than_hr_lag"` and the reason attached. **The walk still counts as a cycle** — only the recovery figure is suppressed. **The threshold is derived, not picked**: it moves with `HR_LAG_S` by construction, so a better lag estimate re-gates every walk in the corpus without anyone remembering to change a second number. It is registered in `calc_params`, so changing it moves the digest.

**WHAT IT CHANGED.** On that run most walks were suppressed, and the median recovery over the measurable remainder was far larger than the median when every walk was pooled. The old figure was dominated by walks the instrument could not see. `n_recovery_measurable` and `n_recovery_shorter_than_hr_lag` now sit beside the median, because a median over a minority of walks is a different claim from a median over all of them.

> **One qualification on the original observation.** The shortest walk that happened to return a positive figure on that run is not a threshold: measured at the 45 s gate, walks only just above it also returned positive figures. The direction of the finding holds; an observed minimum on one run is not a rule.

**THE COACHING CONSEQUENCE: a deliberate recovery walk should be 60 s or longer to be visible to this metric.** That is a statement about what the system can measure, **not a criticism of short breathers.** A 15 s reflexive walk over a steep pitch may be exactly the right thing to do and this metric will never have an opinion about it — the athlete should not lengthen walks to satisfy an instrument. What the rule means is: *if you want to know whether a recovery walk worked, make it long enough to be measured.*

**M1'S TRAINING DESIGN IN THE DOCUMENT IS SUPERSEDED BY §17.2.1.** The document proposes labelled windows generally; the ruling makes the flat probe session a **100%-mode positive class by construction** and trail runs **annotated by mile range afterwards**, with ranges outside an annotation negative or unknown and never assumed positive. Lap marks were considered and set aside.

### 17.2.1 M1 — MODE. Definition, superseding the proposal where they differ

**MODE IS A GAIT** — a specific running form the athlete trains deliberately, under a name of their choosing.

**THE LABEL IS NOT IN CODE.** This build carries no gait name: the mode is referred to generically, and **a name, if one is ever given, is configuration, never hardcoded**. A rename must not touch code, a schema, or any stored row — the same reason HR zones are dated configuration rather than constants (§14e).

**PRIOR, NOT DETECTOR.** A gait of this kind may have **a pace below which it stops being mechanically efficient**. So pace faster than that floor is a *strong prior* for mode. **It is not the measurement.** The point of running dynamics is to SEE mode in the dynamics rather than infer it from pace: **cadence, vertical oscillation, vertical ratio and ground contact time, at matched pace and grade.** A detector that keyed on pace would be circular — it would find mode wherever the athlete ran fast, which is the thing to be explained.

**TRAINING SET — TWO SOURCES, AND LAP MARKS WERE CONSIDERED AND SET ASIDE.**

1. **The flat probe session (§2.3) is run 100% in mode BY CONSTRUCTION.** It is the positive class, every time it is run on a dynamics-capable watch. This is why the probe earns its keep twice: it anchors k(fitness) *and* it is the only unambiguous positive label the design has.
2. **Trail runs are annotated BY MILE RANGE after the run** — "mile 1–2 and 4–5 were mode" — in a free-text note until a structured label protocol exists. **Ranges outside the annotation are negative or unknown, never assumed positive.** Silence is not a positive label.

**Label collection does not wait for the classifier.** The classifier waits for the floor the proposal sets; the labels do not, and an unlabelled session is unlabelable afterwards (a label cannot honestly be reconstructed months later).

**STATUS WHEN DEFINED — MEASURED, AND IT WAS ZERO.** When M1 was defined, every running-dynamics column was entirely NULL across the corpus: `vertical_oscillation`, `stance_time`, `vertical_ratio`, `step_length` and `power` all 0 populated rows; only `cadence_raw_rpm` was populated. The recording watch had no running-dynamics channel, and `annotations` held **0 rows**, so no trail labels existed either. **M1 had no training data of either class.** The schema already accepted every field as nullable, so nothing needed migrating — it was purely a wait for a dynamics-capable device. (Dynamics are now ingested where the device records them; see `session_series`.)

**TERRAIN EXPECTATION — TO TEST, NOT TO ASSUME.** An athlete may expect the gait on, say, descents and flats but not on climbs. Write such expectations down before the data exists, so the detector can disagree with them.

### 17.2.2 MODE CAPACITY — the width of an interval, and why M1 is a fitness metric

**Mode is bounded below by mechanics and above by physiology, and the gap between them can be narrow.**

| bound | source | value |
|---|---|---|
| **floor** — below this the gait does not work | **M6's pace floor** | the athlete's declared, dated floor |
| **ceiling** — above this HR leaves zone 2 | **M1's cardio constraint** | top of zone 2, from the activity's own zone boundaries (§14e) |

**WHEN THE INTERVAL IS NARROW, MODE IS GATED BY CARDIO, NOT BY LEGS.** The gait needs paces faster than the floor; if at those paces HR leaves zone 2, the engine — not the mechanics — is the limit. **M6's pace floor and M1's cardio ceiling are the two bounds of the SAME interval**, and **"mode capacity" means the width of that interval, measured in run time.**

**That is why mode fraction is a direct measure of what the block trains.** As fitness improves, the pace sustainable at the zone-2 ceiling rises, the ceiling moves away from the floor, and the interval widens. **Mode fraction over the block therefore measures how much of a run the engine can afford in the gait** — not how well the athlete executes it.

**Report it alongside the zone-2 pace ceiling from the flat probe (§2.3), so the two can be seen moving together.** Either alone is ambiguous: a rising mode fraction on an easier course is not fitness, and a rising zone-2 pace ceiling that does not widen the interval has not yet bought anything on the trail.

*Caution carried from §6.2:* HR can sit in a narrow habitual band that straddles a zone boundary, so time-in-zone measures where a line falls as much as what the heart did. The ceiling is then a boundary the athlete already sits astride, and small HR differences move time across it. Mode capacity must be reported with the zone boundary set and its effective date, never as a bare number.

### 17.2.3 Tier 2 — THE STANDING SET

**The report renders this tier first.** Measured against the live code.

| metric | § | always / conditional | what can refuse it | ranked against | calc_version |
|---|---|---|---|---|---|
| average HR, three paces, run/walk/stand, windowed bests, cadence, cadence by grade | §6.0 | always | source with no cadence; no DEM tile (grade table only) | **none — not in `primitives`, so no percentile** | digest |
| matched_terrain_drift | §6.7 | always | no qualifying record pairs | same cluster, last 8 | digest, including its tolerances (`drift_speed_tol_mps` and the rest are registered in `calc_params()`) |
| segment_effort_drift | §6.7-adjacent | always | no two same-direction efforts ≥10 min apart | same cluster | digest |
| efficiency_factor | §6.8 | always | no segment efforts with HR | same cluster | digest |
| delta_hr_ascent / delta_hr_descent | §6.3 | always | no `segment_recovery` rows | same cluster | digest; carries `applied_filters` (it was once the only `ok` primitive without them) |
| climb_recovery_tau | §6.3 | always | **gated by a CORPUS verdict** — within-slope CV against `MAX_TAU_CV` 0.5, voted only by (segment, direction) groups with 3+ estimates; reported with `gate_basis` | — | digest, `MAX_TAU_CV` included |
| drift_onset | §6.9 | always | refused until 8 same-cluster 1 Hz runs over 62 min exist | — | digest |
| aerobic_decoupling | §6.4 | always | **unavailable — not built**; needs §6.1 first | — | — |
| grade_adjusted_pace | §6.1 | always | **refused — GAP unvalidated** | — | — |

**THE ATHLETE'S THREE NAMED FOCUS METRICS: one ships, one was invisible, one is ILL-POSED AS ASKED.**

**(a) Time to HR drift onset (§6.9)** — built, always computed, refused until its self-lifting gate opens. Nothing to add.

**(b) Average pace on hills, ascent and descent separately** — **was NOT a callable quantity when first examined.** `basic.effort_paces` computed exactly the right thing (mean + best-10s/30s per effort, stopped time excluded) but was called from **one place only**, `over.segment_history(segment_id=…)`. Getting the number meant one tool call per segment and hand-aggregation, and **the §16.2 report carried no effort pace at all** — `climb_signature` renders direction, length, vertical and grade, with no pace and no HR. The data was fully stored and direction-labelled; **it was one GROUP BY away and it was invisible.** It now ships as `hill_pace` (§6.0g), built on `basic.effort_pace_by_direction`.

**(c) HR drift uphill vs downhill (§6.7 direction split) — ILL-POSED, and the reason is the metric's own precondition.** **A direction is a property of a RECORD; a §6.7 pair has two.** Measured: **about 41% of the corpus's §6.7 pairs join records travelling in OPPOSITE directions.** That is not a defect — §14b names out-and-back structure as the metric's *precondition*, and an out-and-back is precisely what makes the revisit a reverse traversal. Restricting to same-direction pairs **discards about three quarters of them and flips the ascent–descent sign between runs.** **Do not build it this way.**

**SHIPPED AS `segment_effort_drift(direction=…)`, UNDER ITS OWN NAME AND NEVER AS "§6.7 BY DIRECTION."** Its pairs are same-direction by construction (`a.direction = b.direction`), which is exactly what §6.7 cannot offer.

**Read a direction ratio from it only with the caveat below.** Compute it both pooled at pair level over one activity per `run_id` and aggregated the way the tool reports it (median of per-run medians), and check that the two agree in sign.

#### THE CEILING-EFFECT CAVEAT — the asymmetry is REAL but is NOT a pure eccentric cost

The pattern to check for, comparing the earlier and later effort of each same-direction pair against the top of zone 2 (illustrative):

| | earlier effort HR | later effort HR | rise | headroom to the zone-2 ceiling |
|---|---|---|---|---|
| ascent | starts high | barely higher | **small** | **small** |
| descent | starts well below the ascents | ends near where the ascents end | **large** | **roughly twice the ascents'** |

**If descents start lower with more headroom, the decisive detail is whether both directions END in nearly the same place.** If they do, the later efforts converge, which is what a ceiling effect looks like rather than what a cost difference looks like.

**So the honest reading is: "descents start with room and rise into the band the ascents were already in," not "descents cost more."** If direction groups start with different headroom below a ceiling, some unquantified part of the rate ratio is headroom. The caveat travels with the number in `applied_filters.ceiling_effect_caveat` so a coach cannot read the ratio without it.

### 17.3 Tier 3 — ON DEMAND AND TRENDS

The parameterised primitives via MCP. **The vocabulary is NOT uniform, and a tool that cannot honour a filter REJECTS it by name rather than ignoring it** — the contract that was silently broken once and is now pinned by `SELECTION_SUPPORT` in `src/trail_factors/mcp_server.py` plus a test asserting signatures and table cannot drift apart. `SELECTION_SUPPORT` is the authoritative per-tool list; the groups below are its shape.

| group | filters | tools (examples) |
|---|---|---|
| selection | `run_type, start_date, end_date, cluster, last_n` | `matched_terrain_drift`, `segment_effort_drift` (plus `direction`), `efficiency_factor`, `grade_binned_pace`, `cadence_by_grade` |
| selection + segment | the above **plus** `segment_id, direction` | `delta_hr`, `segment_history` |
| per-run — `run` only | none | `report`, `basic_metrics`, `drift_onset`, `hill_pace`, `session_series`, `run_walk_cycles` |

`run_type` stays in every selection tool's signature but has no source in this build, so a non-empty value is rejected by name (§16.6) rather than silently matching nothing.

Dates are ISO `YYYY-MM-DD`; a malformed date is rejected, never reinterpreted. **Retro/trend analysis over the last N sessions** is `last_n` on the selection tools, plus `segment_history` for one slope over time. **There is no arbitrary SQL, no arbitrary aggregation, and no bronze access** — a quantity is a §6 primitive or it does not exist.

### 17.4 THE PROMOTION PATH

**When the athlete asks for something novel for one session and it proves useful, it becomes a standing Tier 2 metric.** Naming the path is what stops useful things staying ad hoc.

**Adding a metric requires FIVE things. The cost is deliberately stated so it is paid rather than avoided:**

1. **A function** in `src/trail_factors/analysis/`, computing it once, that both the CLI and MCP call. Never two implementations.
2. **A gate declaration** — what refuses it, measured rather than assumed, as a constant so the gate lifts itself when the data supports it (the `MAX_TAU_CV` pattern). A metric that cannot refuse is a metric that will one day be confidently wrong.
3. **A comparison set** — what it is ranked against, cluster-based, and the `n` below which no percentile is given.
4. **A report line** in §16.2, with `applied_filters` as FIELDS, and registration of its constants in `versions.calc_params()` so the digest moves when it does.
5. **A test**, with a positive twin. An absence-only assertion is vacuous (CLAUDE.md).

**A candidate that cannot yet satisfy (2) stays a candidate.** That is the whole function of §17.2's register.

### 17.5 WHAT IS MISSING, ranked by cost

The gap list as drawn up, with what has since closed marked. It is kept whole because the ranking — cheap contract violations first — is the method.

**Cheap, and each closes a stated contract violation:**

1. **`applied_filters` on `delta_hr_ascent` / `delta_hr_descent`** (~4 lines). They were the *only* OK primitives in the §16.2 payload with no filter block, against a rule that says every result states its filters as fields. **Closed.**
2. **Expose `direction` on `segment_effort_drift`** (~10 lines) — focus metric (c), already same-direction by construction. **Closed.**
3. **`max(splits.max_speed)`, `session.max_heart_rate`, `session.max/avg_running_cadence`** — each is one column or one read, and each reproduced Garmin's summary exactly on every activity checked. Stride length is `distance ÷ steps`. **Session max HR is now stored (`activities.session_max_hr`); the rest remain open.**
4. **Register §6.7, §6.3 and §6.0e/f constants in `versions.calc_params()`** (~13 lines, one deliberate digest break). Until then `DRIFT_SPEED_TOL_MPS` or `MAX_TAU_CV` could change and **the digest did not move** — exactly the failure the digest exists to prevent. **Closed.**

**Medium:**

5. **An effort-pace-by-direction read** — focus metric (b), plus a report line. Everything needed is already in `segment_efforts`. **Closed: `basic.effort_pace_by_direction`, exposed as `hill_pace`.**
6. **Fix `laps` to store `total_timer_time`** and render per-mile splits including the partial final lap. Requires a re-ingest. **The column is closed: `laps.total_timer_time` is now the authoritative lap duration.**
7. **Per-activity HR zone boundaries from the FIT**, unlocking time-in-zone on every activity rather than only those recorded under the current zone set. **Closed: `activity_hr_zones`.**
8. **The resampled series** (§17.1b), once the grain decision is made. **Closed: `session_series`, 30 s bins.**

**Large, and data-gated rather than code-gated:** §6.1's GAP refit, §6.4's decoupling (which depends on it), §6.9's cohort, and M1's dynamics.

**SHOULD PROBABLY NOT BE BUILT:** a record-grain direction split of §6.7 — it discards about three quarters of pairs, voids the 18-combination stability sweep that made §6.7 trustworthy, and answers a question `segment_effort_drift` already answers honestly.

---

## 18. Stationary rides

**BUILT — `src/trail_factors/ingest/ride_blocks.py` (the writer), `src/trail_factors/analysis/rides.py` (the reads); tools `save_ride_blocks`, `ride_blocks`, `resistance_history`, `time_in_resistance`.** Written up after building, from what the code enforces.

**WHAT THE WATCH GIVES ON A STATIONARY RIDE: HEART RATE, AND NOTHING ELSE.** A `cycling` / `indoor_cycling` activity recorded by a wrist watch with no bike sensors paired carries `heart_rate` on every record and **no cadence, speed, position or power**. `distance_m` is present but a constant **0.0** rather than null — a fake zero, not a measurement — and there are no `rwd_*` splits. The watch knows WHEN the heart did what and nothing about what the legs were doing. **Resistance, RPM and block boundaries exist only in the athlete's memory**, and this is where they are stored.

**THE ONLY MCP WRITER, DELIBERATELY.** `save_ride_blocks` writes `data/athlete/ride_blocks.json` (validated, atomic, revision-keeping, no-clobber: replacing an existing log needs `supersede` passed as an argument beside the payload, never as a field inside it). It competes with no other channel, because nothing else records these quantities. **Perceived effort is Garmin's own self-evaluation** where the watch asks for one (§16.6); the ride log does not record it, and does not become a structured channel for how the body felt. **An athlete-typed average HR is not a field** — the watch's record-grain HR is authoritative and the typed figure survives only in `raw_text`.

**TWO RELIABILITIES, BOTH ATHLETE-TYPED, NEITHER SENSOR-GRADE — AND THE SCHEMA SAYS WHICH IS WHICH.** `machine_totals` is the console's end-of-session readout: machine-reported, and the machine is its only source (`distance`, which is **refused without a `distance_unit`**, and `avg_rpm`). `blocks` and `events` are athlete-recalled. `provenance` is a stored field, not a convention, so a recalled RPM range is never mistaken for a measurement.

**RAW TEXT IS THE RECORD; THE BLOCKS ARE AN INTERPRETATION OF IT.** `raw_text` is **required** and kept **verbatim through every revision** — the same reason bronze keeps the raw FIT file. If a parse was wrong, the text is what makes that recoverable.

**AMBIGUITY IS REFUSED, NEVER RESOLVED.** The tool takes already-structured blocks. Shorthand where a boundary is missing, or a later entry restates an earlier minute, cannot be parsed consistently, and guessing writes a confident number into the one directory that cannot be rebuilt. **Parsing is the coaching chat's job, at save time, with the athlete present to resolve it**; the code rejects a contradiction and names it.

**BLOCKS ARE END-ANCHORED IN, EXPLICIT `[start_min, end_min)` OUT.** The log is a sequence of `(resistance, rpm?, end_min)`: the knob is turned while pedalling, so each block starts exactly where the last ended and the first starts at 0. **`start_min` is derived and REJECTED if supplied** — one way to state a boundary means no way for two statements to disagree. Blocks tile without gaps, cannot overlap or be zero length, and cannot run past the activity's own timer time.

**AN OFF-BIKE BREAK IS AN EVENT, NOT A GAP.** The clock keeps running through a short break (a water bottle, a restroom), so the break lands INSIDE a block, and its HR is recovery rather than work at that resistance. It is logged in `events`, and the read flags the block `contains_event` rather than silently averaging it in.

**`resistance` IS DECIMAL KNOB-TURNS FROM CLOSED**, read by eye on a swivel knob. Quarter turns are the expected resolution and are **not enforced** — an eyeballed in-between value is a real observation. **The ceiling is DECLARED, NOT MEASURED** (`RESISTANCE_MAX_DECLARED`, labelled `declared_editorial_not_measured`); it is a logging-error detector, and an optional `resistance_max_turns` in `data/athlete/bike_config.json` replaces it and changes the label to `measured_by_athlete`. The declared default is deliberately never written into `data/athlete/`, where it would read as athlete input.

**`rpm` IS STORED AS THE RANGE STATED, NEVER A MIDPOINT.** "80–85" is what is remembered; a midpoint would invent precision the recollection does not have. A single value is stored as `[v, v]` so every reader handles one shape.

### 18.1 HR at matched resistance — the longitudinal cut

**ONE JOIN, NOT A NEW ENGINE.** Per-block HR comes from `session_series`' 30 s bins (§17.1b): each bin is assigned to the block whose `[start_min, end_min)` contains it, and summarised (mean of bin means, min, max, and end-minus-start across the block — a difference, deliberately not called a rate).

**THE POINT IS HR AT MATCHED RESISTANCE OVER CALENDAR TIME** (`resistance_history`). Blocks are grouped by `(resistance, rpm range)` where both are present; **blocks with no stated RPM group SEPARATELY** from blocks that have one, because no stated cadence is not a cadence that happens to match. Same resistance and cadence at lower HR, months apart, is the strength-endurance signal. **It is the PRIMARY read, because it rests on no assumption** — unlike §18.2's derived quantities.

**EFFECTIVE n IS BLOCKS, NEVER HR RECORDS.** A block is one exposure to one resistance; its several hundred HR samples are one observation of it. This is the pseudo-replication rule of §2.6 and §6.9 again. Every figure reports `n_blocks` and `n_sessions`, and **fewer than `MIN_BLOCKS_FOR_TREND = 3` blocks reports `trend_status: insufficient`** and says how many it has, rather than drawing a line through two points. A block containing an off-bike event is **included and flagged**, so the longitudinal cut can drop it.

### 18.2 Time in resistance, RPM reconciliation, and the load index

**THE WORDS WATTS, POWER AND kJ DO NOT APPEAR IN ANY FIELD NAME OR ANY OUTPUT, AND A TEST ENFORCES IT.** The console reports a constant distance per revolution: it is a **rev counter** with a fixed wheel constant, not a calibrated ergometer. There is no absolute calibration to recover, so a number in watts would be a fabrication wearing a unit. Console distance is stored and enters **no** derivation. Nothing — power, speed or distance — is estimated from resistance.

**TIME IN RESISTANCE** (`time_in_resistance`): minutes per resistance value and per **0.5-turn band**, per LOCAL ISO week. **A band is HALF-OPEN, `[x, x + 0.5)`, named by its lower edge** — the same convention as time-in-zone. `round()` was tried and is wrong: banker's rounding sends an exact quarter value down in one band and up in the next, and quarter-turn values are ordinary log entries.

**RPM RECONCILIATION** (`reconcile_rpm`): per-block point estimates inside each stated range that reproduce the console's whole-ride `avg_rpm`. Any set of values inside the ranges that hits the total is equally good, so **every block is put at the SAME RELATIVE POSITION `t` within its own range** (`rpm_i = lo_i + t·(hi_i − lo_i)`): one unknown, one equation, one answer, and no block singled out to absorb the residual. A console total **outside the achievable range is reported as `no_solution`, never clamped** — clamping would manufacture agreement and hide a misremembered range or a misread console. A block with no stated RPM leaves the total unattributable, so the whole reconciliation is `unavailable`.

**THE LOAD INDEX** (`load_index`): `Σ resistance × reconciled_rpm × minutes`, **UNITLESS**, with its one assumption carried in the payload wherever it appears — resisting torque rises in proportion to knob turns, which is almost certainly wrong in detail and adequate for TREND on one machine compared only against itself. **The common-`t` rule is also what makes the index INVARIANT TO BLOCK SPLITTING**: splitting a block into two with the same range leaves `t`, each piece's RPM and the index unchanged. It needs reconciled RPM and is `unavailable` without it.

---

## 16. Coaching layer

Three modes, specified separately because they read different data, run at different frequencies, and carry different confidence. Binding on §12 milestone 8, and gated by §15.4 throughout: **a mode that cannot source a claim does not make it.**

All three read **gold only** (§3). None of them queries bronze.

### 16.1 Plan development

**Reads:** gold across the full history. **Frequency:** lowest. **Context:** highest.

Produces or adjusts the training program — weekly structure, volume progression, which sessions serve which adaptation. This is the only mode allowed to reason about trends over months, because it is the only one that reads enough history to see them.

It must state which measurements a recommendation rests on, and per §15.4 must **exclude** flagged measurements rather than down-weight them. A plan built on 20 clean runs is better than one built on 40 with 20 flagged.

### 16.2 Post-run analysis

**Reads:** one run against recent history. **Frequency:** after each run. **Output:** on-track, or something to watch.

**When a run is ambiguous, the useful output names what would disambiguate it.** A single run rarely distinguishes fatigue from heat from an off day, but a *targeted* next-day observation often does — how the legs feel tomorrow morning, before anything else — and the athlete is the only available instrument for some of it. A suggestion must be **specific and next-day**; a vague "keep an eye on it" says nothing. **Nothing in the system asks for, collects or stores the answer** (§16.6): the observation is the athlete's, and it enters the conversation, not the corpus.

**BUILT — `uv run trail-factors report [run]`, and the CLI `sync` ends with it for each new run**, so "finished a run" produces the thing the chat reads in one step.

**PRESENT-ONLY.** The report does not perform §16.3's elimination: that needs the weather baseline and the athlete's own account weighed together, and **the chat has both while the function has neither.** Every number is presented with its scale and its provenance, and no verdict is attached.

**THE OUTPUT SCHEMA IS THE CONTRACT.** The chat reasons from the structure and cannot see the code, so anything the structure cannot express is invisible to it.

**FOUR STATUSES, NEVER A BARE NULL.** These mean different things, and a reader that conflates them will either over-claim or refuse to say anything useful:

| status | meaning | how to read it |
|---|---|---|
| `ok` | measured | use it |
| `gated` | computed, then rejected by a STATED gate | the run does not support the claim — **not** that the athlete did badly |
| `refused` | analysis class invalid at this `sampling_regime` (§15.4) | not a finding; not worth mentioning |
| `unavailable` | an input is missing — no GPS, no HR, not built yet | say what is missing only if it matters |

**A single `null` collapses all four, and that is the main way this output could mislead.**

**THE REPORT OPENS WITH `NOTABLE` — where to look first.** It lists any primitive at **p90+ or below p10** against the comparison set, any **gate that failed**, and any **validation flag**, ordered by salience: fatal flags (the data may be wrong, so everything below is suspect) → extreme percentiles, most extreme first → failed gates → warning flags → info. **A `refused` or `unavailable` primitive is never notable** — §15.4 says a refusal is not a finding, and surfacing it as one would train the reader to ignore the section.

**It is still presentation.** "Unusual against your own recent history on this cluster" is an observation; nothing there says whether the run was good, and a failed gate says a **claim cannot be made**, not that the athlete performed badly. **No brief mode** — the full report always follows.

**EVERY PRIMITIVE WITH HISTORY CARRIES ITS PERCENTILE** against the comparison set, alongside that set's median/p25/p75 and n — in the form *"drift X bpm/hour, cluster median Y, p75 Z"*. **This is still presentation, not verdict**: it is what lets the reader tell an unusual number from an ordinary one. Without it the reader has raw values and no scale. **The percentile is withheld when the population is under 3**, rather than inventing a scale that is not there.

**THE COMPARISON SET IS CLUSTER-BASED, not a time window.** Same cluster, then same venue (§19), then same `run_type` — each step only where at least 3 members remain. Comparing like to like is the point of the segment library, and **a time window mixes trails**. Short runs and long runs on the same home trail share a cluster but must not be each other's baseline once there are enough of each (§14g) — otherwise a shorter route reads as a fitness decline. **In this build the `run_type` step never applies**: `run_type` is not collected (§16.6), so the step is skipped and the `rule` string says "not collected" rather than implying the set was narrowed. `annotations` is free text by design and is never parsed for a type.

**CLUSTER IS ASSIGNED FROM THE TRACE**, using the median position over positioned records — never from the start point. §9 records that `startLatitude` is the first KNOWN position rather than the start location, so a start-point classifier is **wrong by construction** on a run that opens with a GPS gap, which is exactly what a trail entrance under poor sky view produces (§14g). The clusters themselves are declared in `config/athlete.yaml` (`clusters`); a run outside every cluster is `OTHER`, never a coordinate.

**THE FULL GRADE-BINNED TABLE IS INCLUDED**, split by `split_type` and never pooled. Ten lines read better than a summary: the shape is the information, and pooling reads as "pace does not vary with grade" purely because each bin mixes walking with running (§6.2).

**WHAT THE REPORT STATES THAT IT CANNOT ENFORCE.** §15.4 requires flagged measurements be EXCLUDED, not down-weighted. When the report was first built, **`records_enriched.quality_flags` did not exist**, so per-record artifact exclusion could not be applied at all, and the report said so rather than implying it checked. That column now exists and record-grain primitives exclude flagged records; what remains unenforceable is still stated in the report's **`unenforced` block**: segment-grain primitives (`efficiency_factor`, `delta_hr`, `segment_effort_drift`) read `segment_efforts`, which aggregates records upstream of the flags, so each declares `quality_flags_excluded=false` in its `applied_filters`; and arm-motion decoupling is deliberately not flagged (§14a, §15.4). Activity-grain flags from `validation_results` are enforced.

### 16.3 Flag classification

**Output:** green / yellow / red on a single run, each driving a **concrete change to the next session** — not a description of how the run went.

**The logic is ELIMINATIVE, not threshold-based.** A red flag is not "HR residual exceeded X". It is a conclusion reached by ruling alternatives out:

| alternative | how it is excluded |
|---|---|
| fatigue accumulating **within** the run | HR was elevated **from the start**, not drifting up (§6.7) |
| heat | weather within the normal range for recent runs (§8, §14c) |
| a harder route | known terrain, matched segment (§5), or matched-terrain comparison (§6.7) |
| a data artifact | no §15.4 quality flag on the measurements used |
| ordinary variation | the athlete's own account, in the conversation — heavy legs, poor sleep (not stored in this build, §16.6) |

**Only when the alternatives are excluded does it recommend scrapping a session.** This is §15 triangulation applied to a decision: **the eliminative structure is what makes the recommendation trustworthy enough to act on.** A threshold alone produces a number; elimination produces a reason, and a reason can be argued with.

Corollary: if the alternatives *cannot* be excluded — no weather, no matched terrain, no account from the athlete — the honest output is **yellow with the gap named**, never red.

### 16.5 Weather timing — build the alert, defer the recommendation

**BUILD: a passive alert.** In many climates the temperature can swing by tens of degrees within a single day in the shoulder seasons. **The failure mode is not checking** — running in the afternoon on the strength of a cool morning reading. Flag when conditions at the usual run time differ substantially from recent runs. This needs no personal model: it is a comparison against the athlete's own recent conditions, and the claim is only *"today is unlike your last few runs"*.

**DEFER: an optimal-time recommendation.** Not because it would be unhelpful, but because **there is no personal model behind it yet.** Advising *"wait until 9am"* today would rest on general heat-stress principles rather than this athlete's data — the confident-but-ungrounded output §15.4 exists to forbid. **Revisit once the §2 residual model carries a fitted temperature term**, at which point the coach can say *"at 85 °F your HR runs N bpm high at this pace"* using **this athlete's** coefficient, and the advice inherits its confidence from a measurement rather than from a textbook.

**Dew point is the specific value-add.** An athlete typically already checks temperature and rain on a weather app; the alert must not duplicate that. What an app will not show is that **the early morning can be worse than mid-morning despite being cooler**, because dew point is higher at dawn and falls as the day warms — and §8 identifies dew point as the variable cardiac drift actually tracks. That is the information this feature exists to surface.


### 16.6 Per-run context and session load — what this build collects, and what it does not

**PER-RUN CONTEXT IS NOT COLLECTED IN THIS BUILD.** Four per-run values have readers and no source: `run_type`, `exclude_from_trend`, `walk_protocol` and `sun_exposure`. `load_answers()` in `src/trail_factors/ingest/answers.py` always returns an empty mapping, and every reader takes a labelled path with one shared label, `"not collected"`. **None of them is a default standing in for an answer** — each reader states that the value was not collected, because a default would be indistinguishable from a real value and would enter every comparison as one.

| value | what its reader does in this build |
|---|---|
| `run_type` | rows carry `run_type: null` with `run_type_status: "not collected"`; a non-empty `run_type` filter is **rejected by name** ("no run_type source in this build" — outcome `rejected`, retryable, remedy *omit run_type*); the comparison set's run_type step is skipped and labelled (§16.2) |
| `exclude_from_trend` | no run is excluded from a comparison on that ground, and results say the flag was not collected — never that the run was declared representative |
| `walk_protocol` | no run's walk cycles are poolable with another's (§17.2, M3), with `walk_protocol_status: "not collected"` |
| `sun_exposure` | no shade correction is applied to the estimated heat stress, and the result says the value was not collected |

**WHY REJECT THE FILTER RATHER THAN DROP IT.** A `run_type` filter that silently matched nothing would return an empty answer — or, dropped, a plausible answer to a different question. The parameter stays on every selection tool so tool signatures do not change shape; **a later version re-enables it through user-defined run types**, where the athlete declares the types and the filter gains a source.

**ABSENT IS NOT FALSE.** A sick day, a hangover or a first run back is real data about that day and false data about fitness. This build has no field that marks it, so the chat must hear it from the athlete and weigh it in conversation (§16.3); nothing in the corpus will.

**NOTHING IN THE SYSTEM ASKS FOR ANSWERS.** `spec_context` carries no question script and no rating anchors, and the CLI `sync` prints no reminder to answer anything. The only post-run inputs are what the watch recorded — including the watch's own self-evaluation below — and a recording-window confirmation where a file needs one (§4.3).

**SESSION LOAD = GARMIN'S OWN RPE × TIMER MINUTES (Foster).** Newer Garmin watches ask, seconds after an activity is saved, for an **effort rating 1–10** and a **5-point "feel"**. Both are written to the FIT `session` message and both reach the corpus:

| FIT field | stored as | encoding |
|---|---|---|
| `session.workout_rpe` (N × 10) | `activities.garmin_rpe` = N | ÷10 |
| `session.workout_feel` (0/25/50/75/100) | `activities.garmin_feel` (1–5) | 0/25/50/75/100 → 1–5 |

Garmin's own API returns the same raw values as the file, so the file is faithful. **`garmin_selfeval_scale` is stored with every value**, so a firmware change in the encoding is visible in the data rather than inferred later.

- **`foster_load_au = garmin_rpe × timer_time_s / 60`**, in arbitrary units, one activity at a time (`src/trail_factors/analysis/session_load.py`). It is in the report payload and in the `session_load` MCP tool, the only load figure in this build. Its `filters()` state the formula, the RPE source and scale, and the duration basis.
- **TIMER TIME, NOT MOVING TIME.** `moving_time_s` is defined as the sum of `rwd_run` and `rwd_walk` time, so it excludes standing and runs low in one direction — an understatement that accumulates in a weekly sum. Load is a session DOSE, not a rate, so the exclude-stopped-time rule for rates (CLAUDE.md) does not reach it.
- **AN UNRATED ACTIVITY SAYS SO.** No rating — the device did not ask, or the athlete declined — is status `unavailable` with the reason "no Garmin self-evaluation on this activity". It is **never priced at 0** and never filled from another activity: a zero would enter a weekly sum as a rest day that was not one.
- **A SELECTION'S LOAD SPANS EVERY SPORT.** Load is a dose, and the legs carry Tuesday's ride into Thursday's run, so `session_load` over a selection (dates, `last_n`, cluster) sums every activity in it, not only running ones. A selection that silently dropped rated rides would report a lighter week than the one that happened, with one sign, and say nothing.
- **UNRATED AND UNPRICED ARE COUNTED SEPARATELY.** An activity with no rating is *unrated*; a rated activity that cannot be priced (no timer time, say) is *unpriced*, with its reason. Neither is a zero. The sum is labelled a lower bound only when at least one activity in the selection is unrated or unpriced; when every one is priced, the sum is the selection's load and says so.
- **FEEL IS SHOWN, NEVER USED.** It answers a different question from effort, and **the direction of its 1–5 decoding is UNVERIFIED** — written down before any observation away from the middle of the scale could distinguish the two polarities. Pricing a session with it would price an unverified scale.
- **ONE CHANNEL, AND SAID TO BE ONE.** The rating is a single number taken seconds after stopping, dominated by how the end of the session felt; it cannot separate a hard engine day from a hard legs day. The load is the watch's single rating, not a binding constraint across systems.

## 19. Venue identity — BUILT (§19.7 records what changed between plan and build)

**Problem.** A cluster (configured in `config/athlete.yaml`) can pool several distinct venues — for example a rolling trail, a moderately hilly one and a steep one — so a cluster-based comparison set can mix venues with a several-fold spread in climbing. Measured over the home cluster's running activities, segment-effort ascent per mile varied by more than an order of magnitude from run to run. A percentile against that pool measures which venue the run was on as much as how it went. **Constraint:** no external map data (§5).

### 19.1 The proposed key, and why the SEGMENT SET is the wrong one

Proposed: the set of segment `(place, side)` identities a run touches, with the athlete naming venues in the athlete-state directory. **Measured, the full set does not separate venues.** Grouping the cluster's runs by Jaccard similarity of their segment sets gave, at ≥ 0.3, one large group, a few small ones and several singletons; at ≥ 0.5, most runs fell into groups of their own. Runs touch anywhere from one to over a dozen segments. The same venue run in the other direction, as a partial loop, or with a segment the matcher created on that run alone produces a different set. Set equality or similarity is a property of the ROUTE, and a venue has many routes.

**Counter-proposal: ANCHOR PLACES, assigned by membership.** A venue is defined by a few named anchor segments that every route there must cross — the climb that makes a venue that venue. A run belongs to a venue if it has an effort on one of its anchors. Membership survives partial loops, direction and extra segments; set similarity does not. The athlete names 1–3 anchors per venue once, the way a segment is named.

### 19.2 Assignment

- **One venue:** the run has efforts on anchors of exactly one venue → that venue.
- **Two venues (a link run):** the venue whose anchor efforts carry the larger share of the run's total segment-effort distance, **if that share is ≥ 60%**; otherwise `mixed`. `venues_touched` lists every venue with its share. A `mixed` run is its own comparison group and is **never pooled into either venue** — a link run is neither. (*Both the share's denominator and the treatment of `mixed` changed at build — see §19.6 and §19.7.*)
- **No segments at all (a flat run):** `venue = none`, reason "no segment efforts". It falls back to the cluster, labelled, exactly as before venues existed.
- **Segments, but no anchor:** `venue = unassigned`, falls back to the cluster, and the report lists it so the athlete can name an anchor. Never guessed.

### 19.3 Stability when the segment library re-founds

A `segment_id` is not stable across re-founding, and the original segment-name store (`save_segment_name`) keyed on projected place coordinates **and stored `peak_lat`/`peak_lon` — exact positions in the athlete-state directory**, which at the time was committed to git. No names had been written through it, so nothing leaked, but the venue store must not repeat it, and segment naming was moved off it before first use. Even now that `data/` is gitignored, coordinates do not belong in athlete state: it is the one store the athlete backs up and carries around, and nothing in it needs a position.

**Anchors are stored RUN-ANCHORED, coordinate-free:** `(reference run_id, direction, path distance into that run where the anchor effort starts)`. Raw files are immutable, so that reference never moves. After any re-found, an anchor RESOLVES to whichever segment the reference run's effort at that distance now belongs to. If it resolves to nothing (the matcher no longer finds a slope there), the venue reports `anchor_unresolved` with the anchor named, rather than silently losing members. A test re-founds the library on a fixture and asserts every anchor resolves to the same runs before and after. (*Built with the traversal MIDPOINT rather than its start — §19.7.*)

### 19.4 Where venue sits in `comparison_set`

**cluster → venue → run_type**, each step only where it leaves at least `MIN_SAME_TYPE` (3) members, and the `rule` string records which steps applied. For example, "same cluster (TRAIL_A); same venue (North Hill); run_type not collected in this build, so no run_type step and no run excluded from the trend on the athlete's say-so". Venue comes BEFORE run_type: the terrain difference between venues is larger than any effect run_type is meant to control, and a long run at a rolling venue compared against a long run at a steep one is the mismatch this exists to stop. The no-look-ahead bound, sport match and exclusions all apply unchanged. With a few dozen runs spread over several venues, the smallest venue may sit below 3 for a while; the fallback says so rather than borrowing another venue. Because the set excludes the run itself and anything after it, **the venue step can narrow a set only from a venue's fourth eligible run onwards.** In this build `run_type` is not collected (§16.6), so the last step is always skipped and the `rule` labels it "not collected".

### 19.5 What changes in stored outputs

- **Golden queries: nothing.** `grade_binned_pace`, `recovery_by_slope` and `matched_terrain_drift` are scoped by a fixed activity list, not by comparison sets. To be confirmed with `capture.py --check` at build.
- **Changes:** every report percentile, `delta_hr`'s population and `notable`'s p90/p10, all of which read `comparison_set`; the `rule` string; and `test_report`'s comparison-set assertions, which become venue-aware.
- **New athlete state:** `venues.json` in the athlete-state directory (venue name → run-anchored anchors), written through `atomic_write`/`revise` like every other athlete file.

### 19.6 Decisions for the athlete before building

1. Anchors (§19.1) rather than the full segment set: accept or reject.
2. The 60% share threshold for a link run, and whether `mixed` should instead pool with nothing.
3. Whether segment naming moves to the run-anchored, coordinate-free store at the same time (§19.3).

**Decided by the athlete:** anchors accepted; 60% link threshold; a `mixed` run **falls back to the cluster, labelled, like `unassigned`** (this replaces §19.2's "its own comparison group"); segment naming moves to the run-anchored store now, and the coordinate-storing path is removed before first use.

### 19.7 As built (`src/trail_factors/analysis/venues.py`)

- **Anchor = `(run_id, direction, at_distance_m)`, where `at_distance_m` is the traversal's MIDPOINT, not its start** (a departure from §19.3). Resolution is by containment: the anchor resolves to the segment of the traversal on that run, in that direction, whose `[start_distance_m, end_distance_m]` contains the point. A start-point anchor would fall off its own slope the first time a silver rebuild moved the boundary a few metres later. No traversal → `anchor_unresolved`, named; two → `anchor_ambiguous`.
- **The 60% share is over ANCHOR distance among the venues touched**, not over "the run's total segment-effort distance" as §19.2 wrote. A venue owns only its 1–3 anchors, so the literal reading could never reach 60% on any run. A run that crosses one venue's anchors only has share 1.0. Inclusive at 0.60; `LINK_SHARE_MIN` is in `calc_params()`.
- **A segment that anchors two venues decides neither** — dropped from both and listed in `shared_anchor_segments`; `venue-anchor` refuses to create one.
- **`none` says WHY**, looked up rather than asserted: terrain computed and no slope found, or no terrain at all (whether it has slopes is unknown).
- **comparison_set:** `cluster → venue → run_type`, each step only at ≥ `MIN_SAME_TYPE`; every step and every fallback is in `rule`, and the payload carries a `venue` block. With no `venues.json` every run is `none` or `unassigned` and **every comparison set keeps its members** — only the rule text gains the label. Golden queries were byte-identical across the build.
- **Segment names** live in `segment_names.json` as `{name, anchor, named_at}` with revisions. `matcher.attach_names` and the lat/lon + place x/y writer are deleted; both stores refuse a position key (`FORBIDDEN_ANCHOR_KEYS`) on write AND read.
- **Commands:** `venue-candidates <CLUSTER>` PROPOSES anchors — segments crossed by at least a few of the cluster's runs, grouped by how far their crossing runs overlap, each group a *likely* venue with its climbing figures — and writes nothing; `venue-anchor '<venue>' <segment-id>` adds one anchor; `venues` shows resolution and runs per venue. Nothing writes `venues.json` until the athlete confirms anchors: **naming a venue is the athlete's act.**
- **Both departures above (anchor-distance share, midpoint anchors) were ACCEPTED by the athlete** before the first anchors were stored. What the first assignment taught, as method:
  - **Choosing an anchor.** The rule used was "the climb crossed by the most of this venue's runs". On a small venue that can tie several ways; the tie was broken by **size**, because short pitches near the slope detector's minimum length are the likeliest to vanish on a silver rebuild, and an anchor that vanishes reports `anchor_unresolved`.
  - **Adding anchors to recover unassigned runs.** After the first anchors, most `unassigned` runs crossed only climbs that the largest venue's runs also crossed. Further anchors were added to that venue **only after checking, before writing, that no run of any other venue crosses them** — otherwise a new anchor would turn single-venue runs into link runs. That check moved most of the unassigned runs into the venue; no `mixed` run appeared.
  - **What anchors cannot do, by construction.** The runs still `unassigned` afterwards were of two kinds: runs crossing only one venue's non-anchor climbs (resolvable by another anchor), and runs crossing only segments **no other run has ever crossed**. Anchors cannot place a run that shares no climb with any other run; that is a property of membership-by-anchor, not a defect to tune away. A non-running activity on a venue's ground (a hike) reads `none`, held by the running-only sport filter.
