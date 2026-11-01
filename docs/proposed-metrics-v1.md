# Proposed metrics — v1

**What this is.** Six candidate additions to §6 (M1–M6), registered in SPEC.md §17.2. They were designed against what a training block *prescribes* rather than against the data in the abstract: for each one, this note says what it measures, why an athlete's training would need it, how it is defined, which filters it declares, and what makes it refuse. When this note was written none of them was built. §17.2 holds their current status; M3 has since been built and promoted.

**The organising observation.** A training block for an athlete working on a run/walk gait over hilly terrain prescribes things the system could not see at all: time spent in the gait mode, descent dose, run/walk cycle structure, late-run capacity, and terrain specificity as a distribution. It may also prescribe weekly vertical gain, which nothing reported. Everything below follows from that gap list, and the metrics are ordered by value, not by build cost.

**One governance note up front.** §6.6's choice-versus-capacity rule is the spine of all of this. Every metric below declares which side it sits on. A metric that mixes them is the §6.6 failure under a new name.

---

## M1. Mode fraction — how much of a run was actually in the mode

**Status if built today:** `unavailable` on every activity from a device without running-dynamics channels.

**Why training needs it.** A block that develops a specific gait ("the mode") prescribes sessions of mode running with duration as the only stated variable. Without a detector, nothing in the system can tell whether a single second of a run was in that mode, so "N minutes in the mode" is an unverifiable claim. Every downstream question — does the mode cost less at matched pace, does a pace floor exist, does tissue tolerance in the gait extend — is conditioned on a variable that does not exist yet.

The detector refers to the gait only as "the mode"; it carries no display name, and none should be hardcoded.

**Definition.** A per-record binary with a confidence, aggregated to `mode_run_time_fraction` over `rwd_run` records only.

Inputs are the wrist running-dynamics channels — `cadence`, `vertical_oscillation`, `vertical_ratio`, `stance_time` — with `path_derived_speed` and `grade_pct` as conditioners. A typical description of such a gait gives the expected signature: *lower vertical oscillation and shorter ground contact at matched pace and grade*, with cadence above the athlete's default gait at the same pace. That is a prior to test, not a rule to encode.

**Method, and why it must be supervised.** Do not threshold. The training set comes from the athlete marking windows within a run where they were in the mode. The athlete labels windows; the model fits on labelled windows and reports a posterior per record. A hand-set cadence threshold would encode a hypothesis as data, which is the §14a arm-motion error. Pace is likewise a prior, never the detector: keying on pace would make any later pace-versus-mode finding circular.

**Filters declared:** `split_type` (`rwd_run` only), `dynamics_source` (wrist vs accessory pod), `n_labelled_windows`, `n_labelled_runs`, `sampling_regime`.

**Refusal conditions.**
- `unavailable` when the activity has no running-dynamics channels. Older devices without those channels cannot contribute: the mode series starts from the first file that carries them and cannot be backfilled.
- `unavailable` until the labelled set clears a stated floor (proposal: ≥20 windows across ≥8 runs, at least 3 of them negative examples).
- `refused` on `smart_recording`: vertical oscillation and ground contact time at a ~4-second grain are not the same measurement as at 1 Hz.

**What it unlocks — the argument for ranking it first.** Once mode is a field, three existing primitives become mode-conditioned and answer questions they currently cannot: grade-binned pace *in mode versus out*; matched-terrain drift *in mode versus out*, which tests the claim that the mode trades leg cost for cardiac cost; and efficiency factor *in mode*, which is the fitness series that mode sessions actually generate.

---

## M2. Descent dose — the eccentric ledger

**Status if built today:** `ok` on any DEM-covered run.

**Why training needs it.** On a course with long descents, the deciding factor is often whether the knee extensors survive the total descent, so a plan may prescribe descent exposure as a dose. **Nothing in the system counted descent.** Without this metric any such prescription is asserted, not verified, and a shortfall is invisible.

**Definition — deliberately unweighted at first.**

`descent_m_by_grade_band`: total elevation loss over records with negative grade, bucketed into the existing grade bands, split by `split_type`. Plus `descent_m_running` (the `rwd_run` subtotal) as the headline, because hiking down and running down are different loads, and §6.2's pooling ban applies here for the same reason.

Report per run, per week, and as a 28-day rolling sum.

**On weighting — the thing to resist.** It is tempting to collapse this to one scalar with a grade weight, `Σ loss × w(grade)`, since a metre lost at −15% costs more than a metre at −3%. **There is no measurement of `w` for the athlete, and a shape chosen from the literature would be a convention presented as a number** — exactly the §6.2 GAP problem. Ship the banded vector. A scalar can be defined later if something measures the weights; until then the bands carry the information and the reader can see the distribution.

**Filters declared:** `split_type`, `terrain_usable`, `coverage_fraction`, `grade_window_m`.

**Companion field — `vertical_balance`.** Gain and loss reported separately, never as one "elevation" number, with `gain_loss_ratio`. A loop or out-and-back has a ratio of ~1.0 by construction; a point-to-point course with net descent sits well below 1.0. The ratio makes visible the one thing symmetric local terrain cannot supply, and makes the obvious workaround — hike the climbs, run the descents — measurable as `descent_m_running / ascent_m_running`, which *can* be pushed well below 1.0 on symmetric terrain.

---

## M3. Run/walk cycle economics — does the walk buy anything

**Status if built today:** `ok` on watch-recorded activities with enough cycles (see the refusal rule below). It needs no DEM, no GPS coverage and no 1 Hz. §17.2 records the measured count.

**Why training needs it.** For an athlete training a run/walk gait, the run/walk structure is not a concession, it is the delivery mechanism: it keeps every running minute in the pace range where the gait works. The open question is whether, as fitness rises and a given pace becomes easy aerobic running, a fixed walk still earns its place. That is answerable from data the athlete already has, and it decides the shape of structured run/walk sessions.

**Definition.** From the `rwd_*` split sequence, reconstruct cycles: each `rwd_run` block and the `rwd_walk` block that follows it. Per cycle report `cycle_index`, `run_duration_s`, `run_pace_per_mile`, `run_mean_hr`, `walk_duration_s`, `hr_at_walk_start`, `hr_at_walk_end`, `hr_recovered_bpm`, `elapsed_at_cycle_start_s`.

Then two derived series, both within one run:
- **Purchase:** run pace of cycle *n+1* against walk duration of cycle *n*, controlling for `elapsed_at_cycle_start_s`.
- **Decay:** run pace by `cycle_index`, which is the fade signal the run/walk ratio is supposed to defend against.

**Why this is cheap, and why it should probably be built first despite ranking third.** §6.0 established that the `rwd_*` splits carry durations on every watch-recorded activity in both sampling regimes. M3 therefore works on the entire historical corpus, including the oldest reference runs. It is the only metric here with immediate history. (An activity imported into Garmin rather than recorded on the watch has no `rwd_*` splits at all, and reports that with a reason rather than a zero.)

**Filters declared:** `min_class_time_s` (§6.0's fitted 120 s applies to the per-cycle pace), `n_cycles`, `sampling_regime`.

**Refusal.** `gated` when there are fewer than 4 complete cycles — a purchase curve from two points is not a curve. Cycles whose run block is under `MIN_CLASS_TIME_S` report duration but no pace, per §6.0.

---

## M4. Capacity retention — durability measured as speed, not heart rate

**Status if built today:** `gated` on most runs; `ok` by design on long runs that end with a deliberate hard effort.

**Why training needs it.** Aerobic decoupling is gated off by any hard finish, and matched-terrain drift measures HR at matched speed. Neither answers the question an ultra actually asks: **several hours in, how fast can the athlete still go when they try?** That is a capacity question (§6.6), and capacity is measured by best windows, not medians.

**Definition.** Best 30-second and best 5-minute `rwd_run` windows, computed separately within run quarters (or within a first-third / last-third split), **matched on grade band** so a fast opening on flat ground is not compared against a late climb. Report `retention_ratio` = late best ÷ early best, per grade band, with the absolute paces alongside.

**The gate, and how a plan can satisfy it.** A retention ratio is meaningless unless the athlete actually went hard both early and late — otherwise it measures pacing choice, not capacity. So: require a late effort within 5% of the run's own best window in that grade band, else return `gated`. A plan that prescribes a regular hard finish makes this a standing feature: a hard finish is what converts a long run into a durability measurement.

**Filters declared:** `split_type`, `grade_band`, `window_s`, `quarter_boundaries`, `flagged_record_fraction` (§6.0's rule — a maximum selects for positive measurement error).

**The pre-fatigued use.** Any session built to arrive at the running already fatigued asks exactly this question. Retention on that day, against retention on an ordinary long run, is the closest thing a training block produces to a rehearsal of the late stages of a race.

---

## M5. Terrain signature distance — venue choice as a distribution

**Status if built today:** `ok` for run-to-run comparison on DEM-covered runs (it needs per-record grade); `unavailable` against a race until a course profile exists.

**Why training needs it.** Venue selection by feet-per-mile uses a diluted average that hides everything. A trail can have a modest whole-trail ft/mi and a far steeper core; a gentle-looking trail can contain a 15% pitch. Two venues with identical ft/mi can be completely different training. **The quantity that matters is the distribution of time across grade bands**, which the silver layer already produces per record.

**Definition.** `grade_time_histogram`: the fraction of `rwd_run` + `rwd_walk` moving time in each grade band, per run, reported as a vector. Then `signature_distance(run_a, run_b)`: the earth-mover's distance between two normalised histograms. A small number means the two runs asked the legs the same question.

**Uses, in order of value.** Compare a new venue against a familiar hilly one before committing a long run to it. Compare a run against a race course profile. Detect that two runs with the same ft/mi are not interchangeable — which is the whole point.

**The governance question this raised.** Comparing against a *race* needs that race's profile, which means importing an external elevation series. The standing §5 rule forbids crowd-sourced trail geometry, per-coordinate lookups and matching against external lines; it exists to stop the segment matcher being contaminated. A race course profile supplied once, stored as a labelled reference object, and never joined to the segment library is a different thing — and it was left as a decision for the athlete rather than the tool. **It has since been ruled: §5 permits course profiles, stored separately and never matchable.** Until a profile exists, M5 is run-to-run only.

---

## M6. Pace-floor compliance — testing a stated constraint

**Status if built today:** `ok`.

**Why training needs it.** An athlete practising a gait may believe it stops being efficient below some pace. Such a claim is load-bearing for a plan (it is why the run/walk structure exists) and is rarely checked against data.

**Definition.** Over `rwd_run` records: `time_fraction_above_floor`, `time_fraction_below_floor`, and the running-pace distribution with the floor marked. `floor_pace_per_mile` is an **athlete-declared parameter with a date**, not a constant — the athlete may revise it, and a floor that moved should be visible as a revision rather than silently changing the series. Internally the floor is held in m/s; pace is formatted only at display (`src/trail_factors/units.py`).

**The real test, once M1 exists.** Cross the floor against mode fraction. If the claim is right, mode fraction should collapse below the floor. If it does not, the floor is a perception rather than a mechanical boundary — and that is worth knowing either way. **This is the cheapest available falsification test of a belief a plan is built on**, which is the reason to build it despite its simplicity.

The floor (mechanics) and the zone-2 cardiac ceiling (engine) are the two bounds of the same interval; see SPEC.md §17.2.2 (mode capacity). Report one beside the other — either alone is ambiguous.

---

## On the hill-plus-weather normaliser

A natural request is to run an unfamiliar venue at an unfamiliar temperature and still know whether the heart rate was good. That is not a new metric — **it is §2's central construct**, and its parts are already specified or built:

- terrain term → §6.1's personal GAP refit (blocked: needs a narrower grade window on 1 Hz data)
- weather term → §8's dew point, which §6.9 already establishes must condition any drift reading
- fitness term → §6.8 efficiency factor on matched terrain (built; too sparse to trend)
- effort anchor → Foster session load: Garmin's own post-activity RPE × timer minutes (`session_load`); an activity with no rating has no load, and Garmin's "feel" rating is shown but never used

The blocker is not the model; it is that three of the four terms are `refused` or too sparse. A regular density of runs is what makes it fittable, and any run on terrain with no history is where it is needed.

---

## Build order, if it were up to this document

1. **M3** (run/walk economics) — works on the whole existing corpus, needs no new inputs, and answers a live question about how run/walk sessions should be shaped.
2. **M2** (descent dose) — cheap, and any prescribed descent dose is unverifiable without it.
3. **M6** (pace floor) — trivial, and it tests a belief a plan may be built on.
4. **M1** (mode fraction) — highest value by a distance, but gated on running-dynamics data volume and a labelling mechanism. Start collecting labels early even if the classifier waits.
5. **M4** (capacity retention) — needs long runs with hard finishes to accumulate before there is anything to trend.
6. **M5** (terrain signature) — needed as soon as runs on unfamiliar terrain need comparing, but run-to-run only until a course profile exists.
