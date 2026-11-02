# Setup

This file is written for a coding agent (Claude Code, Codex, or similar) to
execute from top to bottom on the human's machine. Every step has a command
and a **Check**: the exact output, or the exact line to look for. If a check
does not match, stop and report the step number and what you saw instead of
improvising a fix.

Rules for the agent:

- Run every command from the **clone root** (the directory that holds
  `pyproject.toml`) unless the step says otherwise.
- **Never run anything that contacts Garmin.** Steps 6 and 7 (`garmin-login`,
  `sync`) and the Garmin commands in the CLI (`garmin-download`,
  `garmin-download-all`, `garmin-list`, `coverage-report`,
  `validate --refresh`) are the human's: stop, ask the human to run them in
  their own terminal, and wait for them to paste the output. Never type,
  store, echo or ask for the password yourself, and never point the token
  store somewhere else to get around this. In an agent session the CLI
  refuses these commands before anything is sent (it detects `CLAUDECODE` /
  `AI_AGENT`); that guard is a backstop, not permission.
- Never commit anything under `data/`, `config/athlete.yaml` or `.env`. All
  three are gitignored: `data/` holds GPS recordings, the database and the
  athlete's own input.
- Do not invent configuration values (training-area coordinates, activity
  ids). Ask the human.
- Steps marked **needs credentials** or **needs network** cannot be checked
  until the human has done step 6. Their expected output is quoted from the
  source code, not from a captured run.

Every check below was captured on macOS (arm64) with uv 0.12.10 and Python
3.12.14, in a copy of the repository, unless the step says it was not
executed. Machine-specific paths in the outputs are replaced with
`<clone>`, `<home>` or `<tmp>`.

---

## Step 0. Prerequisites

```sh
git --version
uv --version
uv python find 3.12
```

**Check**

- `git --version` prints `git version ...` (any recent version).
- `uv --version` prints `uv ...`. The checks in this file were captured
  with `uv 0.12.10`. If uv is missing, install it from
  https://docs.astral.sh/uv/ (ask the human first).
- `uv python find 3.12` prints a path to a Python 3.12 interpreter, ending
  in `python3.12` (or in `.venv/bin/python3` when run inside a clone that
  step 2 has already synced). The project
  pins Python 3.12 (`.python-version`; `requires-python = ">=3.12"` in
  `pyproject.toml`). If it prints an error instead, run
  `uv python install 3.12` and repeat the check.

---

## Step 1. Clone

```sh
git clone https://github.com/cambroussard9/trail-factors.git
cd trail-factors
grep -m1 '^name' pyproject.toml
```

**Check**

```
name = "trail-factors"
```

---

## Step 2. Install

```sh
uv sync
uv run trail-factors
```

`uv sync` creates `.venv/` and installs the `trail_factors` package from the
clone in editable mode, with two commands: `trail-factors` (the CLI) and
`trail-factors-mcp` (the MCP server). Data paths default to folders inside
the clone (`src/trail_factors/paths.py`), so always run from the clone.

**Check**

- On the first install, `uv sync`'s package list includes a line
  `+ trail-factors==0.1.0 (from file://<clone>)` (uv lists packages
  alphabetically, so it is not the last line). On a repeat run it prints
  only `Resolved ... packages` and `Checked ... packages` and exits 0.
- `uv run trail-factors` with no arguments exits 0 and prints the usage,
  starting with:

```
Command dispatch. Everything else lives in a sibling module.

usage: uv run trail-factors <command> [args]

  sync [days]                     fetch, ingest, validate, report new runs
```

  and ending with:

```
  demo                            build data/demo.duckdb from demo/; no network
```

---

## Step 3. Demo database (optional, no network)

```sh
uv run trail-factors demo
TRAIL_FACTORS_DB=data/demo.duckdb uv run trail-factors report | head -3
```

This builds `data/demo.duckdb` from the Parquet files in `demo/data/` (17
tables). It needs no Garmin account, no raw files and no network. The
database is gitignored and rebuildable: running the command again replaces
it. With `TRAIL_FACTORS_DB=data/demo.duckdb` set, the analysis commands
(`report`, `verify`, `validate`) and the MCP query tools work against it.
Commands that need a Garmin account, the network, raw FIT files or DEM tiles
(`sync`, `garmin-*`, `ingest-all`, `fetch-tile`, `retile`) have nothing to
work on there, and the demo holds no rides, so the ride tools find none.

**Check**

The demo command prints one line per table (`activities 11 rows`,
`runs 11 rows`, ...), then these two lines (the build time varies; it was
0.2 s when captured):

```
demo database built at data/demo.duckdb in 0.2 s, from 17 files and no network.
next: TRAIL_FACTORS_DB=data/demo.duckdb uv run trail-factors report
```

The report's first lines are:

```
======================================================================
POST-RUN REPORT  2026-10-28T1429Z   OTHER
======================================================================
```

---

## Step 4. Athlete configuration

```sh
cp config/athlete.example.yaml config/athlete.yaml
uv run python -c "from trail_factors import config; c = config.load(); print('config OK:', c.source, '| clusters:', [x.name for x in c.clusters], '| projection_crs:', c.projection_crs)"
```

`config/athlete.yaml` is gitignored. Every field is optional; a missing
field takes the default shown in the example. Ask the human for the values
they want, then edit the file. The fields, from the example's comments:

| Field | Meaning |
|---|---|
| `clusters` | Named training areas. A run belongs to the nearest one within `radius_km`, and comparison sets are built per area. Each has `name` (letters, digits, `_`, `-`, starting with a letter; `NO_GPS` and `OTHER` are reserved), `lat` and `lon` (the area centre in decimal degrees; 2 decimal places is plenty), and `radius_km` (0.1 to 500). |
| `projection_crs` | Projected CRS for terrain geometry. Leave `null` and each run's UTM zone is derived from that run's own position, and the output says so; segments in different zones never match. Set it (e.g. `EPSG:32633`) to pin one CRS for every run. |
| `reference_run_garmin_id` | Garmin activity id of the run that orders and labels the segment library ("the climb at 1.0 km"). `null` lists segments by effort count. It can only be chosen after the first sync. |
| `unresolved_peak_run` | `run_id` (`YYYY-MM-DDTHHMMZ`) of a run whose peak heart rate is not resolved; HR-ceiling totals are repeated without it. `null` for none. |
| `hr_above_default_bpm` | Default threshold (bpm) for the `hr_above` excursion list; a few beats below the declared maximum. Default 180. |
| `time_above_ladder_bpm` | Thresholds (bpm, strictly increasing) for `time_above_hr`. Default `[170, 175, 180, 185]`. |
| `route_fingerprint_accepted` | `true` only if the athlete accepts that elevation-by-distance in `session_series` can fingerprint a known route. Latitude and longitude are never emitted either way. Default `false`. |
| `dem_tile_suffix_guess` | USGS 3DEP project suffix to guess in a missing-tile filename; `null` leaves the filename unresolved until `fetch-tile` resolves it. |
| `goal_event` | Free-text goal shown in the coaching brief; `null` for none (at most 80 characters). |

The check command prints only names, never the coordinates, so it is safe
to run in a logged session.

**Check** (with the unedited example):

```
config OK: config/athlete.yaml | clusters: ['HOME_TRAIL'] | projection_crs: None
```

After editing, the line shows the human's cluster names. Validation is
strict: an unknown key, a wrong type or an out-of-range value raises
`trail_factors.config.ConfigError` naming the file, the line and the field.
For example, a stray key `foo: 1` on line 7 ends the traceback with:

```
trail_factors.config.ConfigError: config/athlete.yaml line 7: foo: unknown key (allowed: clusters, projection_crs, reference_run_garmin_id, unresolved_peak_run, hr_above_default_bpm, time_above_ladder_bpm, route_fingerprint_accepted, dem_tile_suffix_guess, goal_event)
```

---

## Step 5. Environment variables (optional)

```sh
uv run python -c "from trail_factors import paths as p; print('db:', p.display_path(p.DB_FILE)); print('raw:', p.display_path(p.RAW_DIR)); print('athlete:', p.display_path(p.ATHLETE_DIR)); print('config:', p.display_path(p.CONFIG_FILE)); print('garmin tokens:', p.display_path(p.GARMIN_TOKENSTORE), '| inside repo:', p.inside_repo(p.GARMIN_TOKENSTORE))"
```

All variables are optional and documented in `.env.example`. **Nothing loads
`.env` automatically**: export the variables in the shell
(`cp .env.example .env`, edit, then `set -a; . ./.env; set +a`), or set them
in the MCP client's server configuration (steps 10 and 11). A **relative**
path is resolved against the repo root, never the current directory.

| Variable | Default | What it holds |
|---|---|---|
| `TRAIL_FACTORS_DB` | `data/ultra.duckdb` (under the data dir) | The DuckDB database. The test suite runs its corpus tests only when this is set; for tests, point it at a copy, never the live database. |
| `TRAIL_FACTORS_DATA_DIR` | `data` | Root of the data directory; the defaults below sit under it. |
| `TRAIL_FACTORS_RAW_DIR` | `data/raw` | Raw activity files (`.fit`), weather and API caches. |
| `TRAIL_FACTORS_DEM_DIR` | `data/dem` | USGS DEM tiles (`*.tif`). |
| `TRAIL_FACTORS_ATHLETE_DIR` | `data/athlete` | Athlete-supplied state (window confirmations, segment names, notes, ride logs). Per-user and not rebuildable: back it up. |
| `TRAIL_FACTORS_LOOKUP_DIR` | `data/lookup` | Account-summary lookups (never ingested). |
| `TRAIL_FACTORS_JOBS_FILE` | `data/jobs.json` | Sync-job state file. |
| `TRAIL_FACTORS_CONFIG` | `config/athlete.yaml` | Athlete configuration (step 4). |
| `TRAIL_FACTORS_GARMIN_TOKENSTORE` | `~/.garminconnect` | Garmin OAuth token directory, written by `garmin-login`. Must be outside the repo; a path inside it is refused. |
| `TRAIL_FACTORS_GARMIN_AUTH_STATE` | `~/.garminconnect_auth_state.json` | Failed-login lockout state (a timestamp and an error string; no secret). Must be outside the repo. |
| `TRAIL_FACTORS_GOLDEN_DIR` | `data/golden` | Test only: private golden outputs for `tests/test_golden_pinning.py`. |

**Check** (no variables set):

```
db: data/ultra.duckdb
raw: data/raw
athlete: data/athlete
config: config/athlete.yaml
garmin tokens: <outside repo>/.garminconnect | inside repo: False
```

With `TRAIL_FACTORS_DB=data/demo.duckdb` exported, the first line reads
`db: data/demo.duckdb`.

---

## Step 6. Garmin login (HUMAN STEP, interactive)

**Agent: do not run the login yourself.** Ask the human to run this in
their own terminal, from the clone root:

```sh
uv run trail-factors garmin-login
```

What the human will see, from `src/trail_factors/cli/garmin.py`:

1. `Garmin Connect email: ` (typed normally).
2. A notice that the password prompt shows nothing while typing, then
   `Garmin Connect password (not echoed): `. The password is read with
   Python's `getpass`, handed in memory to the unofficial
   [`garminconnect`](https://pypi.org/project/garminconnect/) library, and
   never written to a file by this project.
3. `MFA code: ` if the account uses multi-factor authentication.
4. On success, exactly:

```
OK - token saved to <outside repo>/.garminconnect (TRAIL_FACTORS_GARMIN_TOKENSTORE). Password was not stored.
```

The library stores an OAuth token in the token store, which defaults to
`~/.garminconnect` and must be outside the repo. After a failed login, any
retry within 30 minutes is refused locally without contacting Garmin,
because repeated attempts extend Garmin's rate limit. Do not retry in a
loop.

**Agent check.** None: do not run `garmin-login`, not even with empty input.
Ask the human whether the success line above appeared. If they report
`Email is empty. Nothing sent to Garmin.` they pressed Enter without an
email; if they report `Refusing to contact Garmin: TRAIL_FACTORS_GARMIN_TOKENSTORE
resolves INSIDE the repository ...`, the token store variable points inside
the clone and must be moved out (see step 5).

---

## Step 7. First sync (HUMAN STEP; needs credentials and network)

**Agent: do not run this yourself.** Ask the human to run it in their own
terminal, from the clone root, and to paste the output back:

```sh
uv run trail-factors sync
```

`sync` downloads every activity on the Garmin account that is not already
in `data/raw/garmin/` (the whole account by default; `sync <days>` limits
it), ingests every raw file, fetches weather, validates, and prints a
plain-language summary followed by the post-run report for each new run.
Requests are paced (1.5 s before each list request, 3 s before each file
download), so a first sync of a large account takes a while. It is safe to re-run: files already on disk are
skipped and never overwritten. It does **not** run the terrain steps (step
8).

**Check** (on the output the human pastes back). Not executed for this
document (it needs the human's login). The fixed lines to look for, from `src/trail_factors/cli/report.py` and
`src/trail_factors/cli/garmin.py`:

- `Checking Garmin for new activities...`
- `<N> activities on the account; <M> already on disk; <K> to fetch`
- `downloaded <K>, failed 0, remaining 0`
- either `Downloaded <K> new activities: <ids>` or
  `No new activities on Garmin - everything is already downloaded.`
- `ingested <n>, rejected <r>, failed <f>, warnings <w>`
- a block headed `WHAT THIS MEANS`, containing `Nothing needs your
  confirmation.` (or a `NEEDS YOUR CONFIRMATION` list) and, if any file was
  rejected, `<n> file(s) REJECTED and not ingested:` with the fix (step 9)
- one `POST-RUN REPORT` per new run

If the output ends with
`garminconnect.exceptions.GarminConnectAuthenticationError: Username and password are required`,
there is no token in the token store: step 6 has not been done (or the
token store variable points somewhere else). Go back to step 6.

If Garmin rate-limits a batch, the output says
`RATE LIMITED after <n> files. Stopping — rerun later to resume.` Wait, then
run `sync` again; it resumes.

---

## Step 8. Terrain tiles (needs network; US only)

```sh
uv run trail-factors fetch-tile --missing
```

Terrain (grade, slopes, segments, recovery) needs USGS 3DEP 1 m DEM tiles
covering where the runs are. `fetch-tile --missing` reads the list of
missing 10 km cells from the corpus, resolves each against the 3DEP index,
downloads and verifies the GeoTIFF into `data/dem/`, and then runs `retile`
itself. A bulk pull over 1.5 GB is refused without `--yes`:

```sh
uv run trail-factors fetch-tile --missing --yes
```

To fetch a single cell, copy the `fetch_command` that a run's report prints
for its missing tile (it has the form
`uv run trail-factors fetch-tile <cell> --zone <NN>` when `projection_crs`
is unset). After adding a tile by hand, rebuild only the newly covered
runs:

```sh
uv run trail-factors retile
```

3DEP covers the US only. A cell in a southern-hemisphere zone, or a
northern zone above 23, has no USGS tile grid and gets no fetch command.

**Check.** The download path was not executed for this document. Lines
captured offline:

- With no tiles on disk, `retile` prints:

```
tiles loaded: NONE
nothing to do: data/dem holds no tiles
```

- A cell name without `--zone`, when `projection_crs` is unset, is refused
  before any request is sent. (This command creates an empty
  `data/ultra.duckdb` if none exists yet; see the note in step 10.) `uv run trail-factors fetch-tile x40y500`
  (an example name, the one the message itself uses) prints:

```
  x40y500: REFUSED — projection_crs is not set and no zone was given: a cell name like x40y500 means nothing without the UTM zone it was computed in. Pass the zone the coverage report names (`fetch-tile x40y500 --zone NN`, as its fetch_command prints it), or set projection_crs. No request was sent.
nothing to fetch.
```

- On success (from `src/trail_factors/cli/terrain.py`, not captured) each
  tile prints `OK: <size> MB, <width>x<height> at <pixel> m, <crs>`, then
  `<n> new tile(s) in data/dem. Running retile.`, and `retile` ends with:

```
done. Re-run `report <run>` to see the terrain primitives.
```

---

## Step 9. Handling a rejected file (demonstrated offline)

A file whose tail is a car ride (the watch kept recording), whose GPS is
broken, or whose values are implausible is **rejected** rather than
ingested. This step runs the whole fix loop on a synthetic FIT file written
by `tests/fitgen.py` (about 30 minutes on foot, then a drive at car speed,
at an arbitrary synthetic place and date). Everything goes into a throwaway
directory under `$TMPDIR`; the real `data/` is never touched and nothing
contacts the network.

**9a. Point every data path at a throwaway directory.** The env file is
re-read by each command below, so the steps also work when each command
runs in a fresh shell.

```sh
L="${TMPDIR:-/tmp}/trail-factors-loop"
mkdir -p "$L" && cat > "$L/env" <<EOF
TRAIL_FACTORS_DATA_DIR=$L/data
TRAIL_FACTORS_DB=$L/data/loop.duckdb
TRAIL_FACTORS_RAW_DIR=$L/data/raw
TRAIL_FACTORS_DEM_DIR=$L/data/dem
TRAIL_FACTORS_ATHLETE_DIR=$L/data/athlete
TRAIL_FACTORS_LOOKUP_DIR=$L/data/lookup
TRAIL_FACTORS_JOBS_FILE=$L/data/jobs.json
EOF
```

**9b. Write the synthetic file** under a synthetic Garmin id:

```sh
set -a; . "${TMPDIR:-/tmp}/trail-factors-loop/env"; set +a; uv run python -c "import datetime as dt, os; from pathlib import Path; from tests import fitgen; p = Path(os.environ['TRAIL_FACTORS_RAW_DIR'], 'garmin', '90000000001_ACTIVITY.fit'); fitgen.write_run_then_drive(p, dt.datetime(2024, 5, 4, 6, 0, 0)); print('wrote', p.name, p.stat().st_size, 'bytes')"
```

**Check**

```
wrote 90000000001_ACTIVITY.fit 60021 bytes
```

**9c. Ingest: the file is rejected, with the reason and the exact fix.**

```sh
set -a; . "${TMPDIR:-/tmp}/trail-factors-loop/env"; set +a; uv run trail-factors ingest-all
```

**Check**

```
1 raw files

  REJECT 90000000001_ACTIVITY.fit: speed exceeded 4:00/mi for 306 s continuously; a sustained impossibility is a vehicle (the watch still recording after the run, or before it) or a unit error, not a GPS glitch

ingested 0, rejected 1, failed 0, warnings 0
  REJECTED 90000000001_ACTIVITY.fit
           reason:    it moves faster than any running pace (4:00/mi) for a sustained stretch -- almost always the watch still recording in a vehicle
           next step: confirm a recording window that excludes it -- suggested end 2024-05-04 06:30:00 UTC (trailing vehicle speed). Check it against when you actually stopped (or started); if it is wrong, pass your own --end/--start in UTC. Then run `uv run trail-factors sync` to ingest the trimmed run
           run:       uv run trail-factors confirm-file 90000000001 --end '2024-05-04 06:30:00'
           then:      uv run trail-factors sync
```

In real use, `sync` prints the same advice under
`<n> file(s) REJECTED and not ingested:`. Here the loop uses `ingest-all`
instead of `sync`, because `sync` would first contact Garmin.

**9d. List everything outstanding.**

```sh
set -a; . "${TMPDIR:-/tmp}/trail-factors-loop/env"; set +a; uv run trail-factors rejected-files
```

**Check**

```
1 file(s) in <outside repo>/raw/ with findings and NO activity row

  90000000001      0336c69bb530  pace_sustained_impossible
     file span  2024-05-04 06:00:00 .. 2024-05-04 06:35:30  (2,131 records)
     suggested  start None  end 2024-05-04 06:30:00
     - speed exceeded 4:00/mi for 306 s continuously; a sustained impossibility is a vehicle (the watch still recordi
     next step: confirm a recording window that excludes it -- suggested end 2024-05-04 06:30:00 UTC (trailing vehicle speed). Check it against when you actually stopped (or started); if it is wrong, pass your own --end/--start in UTC. Then run `uv run trail-factors sync` to ingest the trimmed run
     run: uv run trail-factors confirm-file 90000000001 --end '2024-05-04 06:30:00'

confirm a window with `confirm-file` where the problem is a leading or trailing block (a car ride, a forgotten stop), then re-run `sync`. Where the file is genuinely corrupt, or the impossible stretch is mid-run, there is no masking mechanism and it stays here.
```

(`<outside repo>` is literal output: the tool never prints an absolute
path.)

**9e. Run the printed `confirm-file` command.** With a real file, the
human must check the suggested time against when they actually stopped;
times are UTC. Confirming a window is a CLI command only: the MCP server
has no tool for it.

```sh
set -a; . "${TMPDIR:-/tmp}/trail-factors-loop/env"; set +a; uv run trail-factors confirm-file 90000000001 --end '2024-05-04 06:30:00'
```

**Check**

```
confirmed 90000000001_ACTIVITY.fit (revision 0)
  file span      2024-05-04 06:00:00 .. 2024-05-04 06:35:30
  valid_start    None  (no bound)
  valid_end      2024-05-04 06:30:00  (from argument)
  next: re-ingest: run `uv run trail-factors sync` -- it re-ingests every raw file, so this one is ingested at the confirmed window (or `uv run trail-factors ingest <outside repo>/90000000001_ACTIVITY.fit` for this file alone; the terrain steps dem -> silver -> match -> recovery follow in the MCP sync job or as their own commands)
```

The confirmation is written to `window_confirmations.json` in the
athlete-state directory (here the throwaway one; normally `data/athlete/`,
which is gitignored and must be backed up).

**9f. Ingest again: the trimmed run is rescued.**

```sh
set -a; . "${TMPDIR:-/tmp}/trail-factors-loop/env"; set +a; uv run trail-factors ingest-all
set -a; . "${TMPDIR:-/tmp}/trail-factors-loop/env"; set +a; uv run trail-factors rejected-files
```

**Check**

```
1 raw files


ingested 1, rejected 0, failed 0, warnings 0
  RESCUED  90000000001_ACTIVITY.fit: now ingested at the confirmed window 2024-05-04 06:00:00 .. 2024-05-04 06:30:00 UTC: 5.16 km in 30 min (the whole file was 11.27 km in 36 min)
```

and `rejected-files` now starts with
`0 file(s) in <outside repo>/raw/ with findings and NO activity row`.
In real use, `sync` announces the same thing as
`<n> previously rejected file(s) INGESTED:` and prints the run's report.

**9g. Clean up.**

```sh
rm -rf "${TMPDIR:-/tmp}/trail-factors-loop"
```

---

## Step 10. Register the MCP server with Claude Code

The server (`trail-factors-mcp`) uses the stdio transport only and binds no
port. Run this from the clone root; `--directory "$PWD"` pins the clone, and
the relative `TRAIL_FACTORS_DB` resolves against it. Use the human's
database:

Run **one** of the two `claude mcp add` commands below; both register the
name `trail-factors`, so the second fails with
`MCP server trail-factors already exists in local config` while the first is
registered. To switch databases later, run
`claude mcp remove trail-factors -s local` first.

```sh
claude mcp add trail-factors -e TRAIL_FACTORS_DB=data/ultra.duckdb -- uv run --directory "$PWD" trail-factors-mcp
```

or, instead, to try it on the demo from step 3:

```sh
claude mcp add trail-factors -e TRAIL_FACTORS_DB=data/demo.duckdb -- uv run --directory "$PWD" trail-factors-mcp
```

The default scope is `local`: private to the human, in this project
directory. Start Claude Code from the clone root to use it.

**Check** (captured with the demo database):

```
Added stdio MCP server trail-factors with command: uv run --directory <clone> trail-factors-mcp to local config
File modified: <home>/.claude.json [project: <clone>]
```

```sh
claude mcp get trail-factors
claude mcp list
```

`claude mcp get trail-factors` prints:

```
trail-factors:
  Scope: Local config (private to you in this project)
  Status: ✔ Connected
  Type: stdio
  Command: uv
  Args: run --directory <clone> trail-factors-mcp
  Environment:
    TRAIL_FACTORS_DB=data/demo.duckdb

To remove this server, run: claude mcp remove trail-factors -s local
```

and `claude mcp list` includes the line:

```
trail-factors: uv run --directory <clone> trail-factors-mcp - ✔ Connected
```

The server connects even when the database does not exist yet; its query
tools then answer `no corpus at data/ultra.duckdb (...)` and tell the
caller to run `uv run trail-factors ingest-all` in the repo. A database
that exists but holds no runs (for example the empty one step 8's offline
`fetch-tile` check creates) answers normally with an empty selection:
`list_runs` returns `0 activities (running)`. The 29 tools
are listed in `docs/tools.md`.

To remove it (from the same directory):

```sh
claude mcp remove trail-factors -s local
claude mcp get trail-factors
```

**Check**: `Removed MCP server trail-factors from local config`, then
`claude mcp get` prints a line starting
`No MCP server named "trail-factors".` and exits 1.

---

## Step 11. Register the MCP server with Codex (from OpenAI's docs, untested)

Syntax from the Codex MCP documentation
(https://developers.openai.com/codex/mcp, which redirects to
https://learn.chatgpt.com/docs/extend/mcp?surface=cli):
`codex mcp add <server-name> --env VAR1=VALUE1 --env VAR2=VALUE2 -- <stdio server-command>`.
The `get` and `remove` subcommands are from the Codex CLI source
(`codex-rs/cli/src/mcp_cmd.rs` in the openai/codex repository). Run from
the clone root:

```sh
codex mcp add trail-factors --env TRAIL_FACTORS_DB=data/ultra.duckdb -- uv run --directory "$PWD" trail-factors-mcp
codex mcp list
codex mcp get trail-factors
```

**Check**: `codex mcp list` shows a server named `trail-factors`; inside
the `codex` TUI, `/mcp` lists the active servers. Codex was not installed
on the machine where this file was checked, so no output was captured.

Codex stores MCP servers in `~/.codex/config.toml` by default (shared by
the Codex CLI and IDE extension); a project-scoped `.codex/config.toml`
works for trusted projects. The same server written by hand, with the
clone's absolute path substituted:

```toml
[mcp_servers.trail-factors]
command = "uv"
args = ["run", "--directory", "/absolute/path/to/trail-factors", "trail-factors-mcp"]

[mcp_servers.trail-factors.env]
TRAIL_FACTORS_DB = "data/ultra.duckdb"
```

To remove it: `codex mcp remove trail-factors`.

---

## Step 12. Tests

```sh
env -u TRAIL_FACTORS_DB uv run pytest -q
```

With `TRAIL_FACTORS_DB` unset, the corpus tests skip and the rest run (this
is what CI does). The bad-data loop test runs offline in a temporary
directory. To run the corpus tests too, point `TRAIL_FACTORS_DB` at a
**copy** of the database; a path that does not exist fails the run instead
of skipping.

**Check**: the last line reports passes and skips and no `failed` or
`error`. Captured:

```
712 passed, 430 skipped, 14 subtests passed in 42.62s
```

(the counts change as tests are added; a few tests ask git about
`.gitignore` and skip when the directory is not a git checkout).

---

## Step 13. Commit hooks (optional, for contributors)

`pre-commit` is not a project dependency. Check whether it is installed
with `pre-commit --version`; if not, ask the human before installing it
(https://pre-commit.com).

```sh
pre-commit install
pre-commit run --all-files
uv run ruff check . && uv run ruff format --check .
```

The hooks (`.pre-commit-config.yaml`) are gitleaks v8.30.1 (scans what is
staged) and ruff-check + ruff-format v0.17.3.

**Check**

```
pre-commit installed at .git/hooks/pre-commit
```

```
Detect hardcoded secrets.................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
```

```
All checks passed!
136 files already formatted
```

(the file count changes as files are added).

The repository also runs CI on every push and pull request
(`.github/workflows/ci.yml`: `uv sync --locked`, ruff, pytest with
`TRAIL_FACTORS_DB` unset), a TruffleHog scan of the full git history on
every push (`.github/workflows/secrets.yml`), and weekly Dependabot updates
for GitHub Actions and uv (`.github/dependabot.yml`). Separately, the
author's own Claude Code user settings (not part of this repository) run
SonarQube's secrets hooks on prompt submission and before file reads; they
need the `sonar` CLI and do nothing without it.
