# Trail Factors

Training analysis for ONE trail/ultra runner, built from their own Garmin FIT
files. It ingests every activity into a local DuckDB database, looks up terrain
from USGS 1 m lidar instead of trusting the watch's altitude, identifies hills
as reusable segments, joins hourly weather, and computes a fixed set of
gated primitives (heart-rate drift on matched terrain, recovery after climbs,
pace by grade, run/walk structure, and more). A post-run report and an MCP
server expose those results to a coaching chat, read-only.

It is a single-user tool: there is no web UI, no accounts and no cloud
service. Your data stays on your machine.

- **SPEC.md** is the full design: data model, algorithms, and the reasoning
  (and measured failures) behind each decision.
- **CLAUDE.md** is the condensed rule book: invariants, commands, conventions.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- A Garmin watch and account (FIT files can also be added by hand)
- Optional, for terrain: USGS 3DEP 1 m DEM tiles covering where you run
  (US only; `fetch-tile` downloads them)

## Setup

```sh
uv sync                                              # installs the package editable
cp config/athlete.example.yaml config/athlete.yaml   # then edit it
cp .env.example .env                                 # optional
```

`uv sync` installs the `trail_factors` package from this clone in editable
mode, with two commands: `trail-factors` (the CLI) and `trail-factors-mcp`
(the MCP server). Run them through `uv run`, or activate `.venv`. Data
directories default to folders inside the clone, so run it from a clone
rather than from a copy installed elsewhere.

**`config/athlete.yaml`** holds your static configuration: your training
areas (`clusters`), an optional projected CRS for terrain work
(`projection_crs`; unset, each run's UTM zone is derived from its own
position and the output says so), the run that orders your segment list,
and a few heart-rate thresholds. Everything you supply or decide over time lives in the
athlete-state directory instead (see below). Every config field is optional
and documented in the example file. The file is validated strictly when it
is loaded: an unknown key, a wrong type or an out-of-range value is an error
that names the field and the line. It is gitignored.

**Environment variables** (all optional, documented in `.env.example`) move
the database and data directories. A relative path is resolved against the
repo root, not the current directory. The main one:

| Variable | Default | What |
|---|---|---|
| `TRAIL_FACTORS_DB` | `data/ultra.duckdb` | the database |
| `TRAIL_FACTORS_DATA_DIR` | `data` | root for the directories below |
| `TRAIL_FACTORS_RAW_DIR`, `_DEM_DIR`, `_ATHLETE_DIR`, `_LOOKUP_DIR`, `_JOBS_FILE` | under `data/` | raw files, DEM tiles, your confirmations and names, lookups, job state |
| `TRAIL_FACTORS_CONFIG` | `config/athlete.yaml` | the athlete config |
| `TRAIL_FACTORS_GARMIN_TOKENSTORE` | `~/.garminconnect` | Garmin OAuth token (must be outside the repo) |
| `TRAIL_FACTORS_GARMIN_AUTH_STATE` | `~/.garminconnect_auth_state.json` | failed-login lockout (must be outside the repo) |

Nothing loads `.env` automatically: export the variables in your shell
(`set -a; . ./.env; set +a`) or set them in your MCP client's server config.

### Athlete-state files

These JSON files live in the athlete-state directory (`data/athlete/`,
`TRAIL_FACTORS_ATHLETE_DIR`). The code reads them; none is rebuilt from raw
data, and every one is optional until you use the feature. The SPEC.md
section named holds the design and format; the two hand-edited files are
shown with their shape.

| File | Holds | Written by |
|---|---|---|
| `window_confirmations.json` | confirmed valid start/end per raw file (§4.3) | `confirm-window`, `confirm-file` |
| `segment_names.json` | your names for segments, run-anchored (§12, §19) | `name-segment` |
| `venues.json` | venue name → anchor segments (§19) | `venue-anchor` |
| `notes.json` | free-text run notes | `note` |
| `ride_blocks.json` | stationary-bike resistance/RPM blocks | MCP `save_ride_blocks` |
| `hr_zones.json` | dated HR zone fallback: a list of `{sport, effective_from, zone_floors, max_hr}` (§14e) | by hand |
| `bike_config.json` | measured knob ceiling, `{"resistance_max_turns": ...}` | by hand |

## Getting data in

```sh
uv run trail-factors garmin-login        # once: email + password, MFA if asked
uv run trail-factors sync                # download new activities, ingest,
                                             # fetch weather, validate, report
uv run trail-factors retile              # terrain, segments and recovery
                                             # for runs that lack them
uv run trail-factors report              # the report for the latest run
```

The CLI `sync` downloads from the whole account by default (`sync <days>`
limits it) and does NOT run the terrain steps (dem, silver, match, recovery):
run `retile`, or those commands individually, afterwards. Nothing in `sync`
or the report asks you for answers about a run: effort comes from Garmin's
own post-activity rating (see *Session load* below).

### When a file is rejected

A file whose tail is a car ride, whose GPS is broken, or whose values are
implausible is rejected rather than ingested. The loop to fix it:

```sh
uv run trail-factors sync              # the rejection names the failing
                                           # check and the command to run next
uv run trail-factors rejected-files    # every rejected file: findings,
                                           # span, a suggested window, and the
                                           # confirm-file command for it
uv run trail-factors confirm-file <garmin-id> --end 'YYYY-MM-DD HH:MM:SS'
                                           # UTC; omit --end/--start to accept
                                           # the suggestion; --note '...' optional
uv run trail-factors sync              # retries the file with the window
                                           # applied; it now ingests, trimmed,
                                           # and the sync says so
```

If the impossible stretch is in the middle of the run, with running on both
sides, no start/end window can remove it and the advice says so rather than
offering one. A car ride at the end followed by a long walk from the car is
still an edge: the advice gives the window and states how much walking it
would cut. Confirming only `--start` (or only `--end`) keeps the other bound
you confirmed before. `confirm-window <garmin-id>`, for a file that did
ingest, also only records the window; run `sync` to apply it.

The window is applied before validation, so the trimmed run is what gets
checked. Splits wholly outside it are dropped, a straddling split is
truncated, and session totals are recomputed (Garmin's whole-file totals are
kept alongside). Confirmations are stored in `window_confirmations.json` in
the athlete-state directory, which is per-user and gitignored. Confirming a
window is a CLI command only; the MCP server has no tool for it, and its
`sync` job's `problems` name the CLI command instead.

`garmin-login` reads your password with `getpass` and hands it to the
[`garminconnect`](https://pypi.org/project/garminconnect/) library in memory.
This project never writes the password to disk; the library stores an OAuth
token in the token store, outside the repo. After a failed login, retries are
refused locally for 30 minutes, because repeated attempts extend Garmin's
rate limit.

Terrain needs DEM tiles. The projection is derived per run from its own
position unless `projection_crs` is set; segments in different UTM zones
never match.

```sh
uv run trail-factors fetch-tile --missing   # resolve, download, verify, retile
uv run trail-factors fetch-tile <cell> [--zone NN]   # one cell
```

A report on a run outside every tile names the missing 10 km cell and the
exact `fetch_command` to run; copy that rather than building one. With
`projection_crs` unset, cells are named in the run's own derived UTM zone,
so the command carries `--zone NN` and a bare cell name is refused. 3DEP
covers the US only: a cell in a southern-hemisphere zone, or a northern zone
above 23, has no USGS tile grid and gets no fetch command.

Run `uv run trail-factors` for the full command list.

## MCP server

```sh
TRAIL_FACTORS_DB=/path/to/ultra.duckdb uv run trail-factors-mcp
```

The server uses the **stdio transport only**: it binds no port. Point your MCP
client at that command (server name `trail-factors`; for a client that does
not run `uv`, use the absolute path of `.venv/bin/trail-factors-mcp`), and set
any environment variables in the client's `env` block. Every query tool opens the database read-only. Only two things
write: `save_ride_blocks`, which puts a validated stationary-bike ride log in
the athlete-state directory, and the `sync` tool. Window confirmations are
written by the CLI (`confirm-file`), not the server. `sync`
starts a background job that appends new files to the raw directory (a
400-day download window) and then runs the full pipeline: ingest, dem,
silver, weather, match, recovery and validate. That is more than the CLI
`sync`, which stops before the terrain steps. With no DEM tiles yet, the job
skips the terrain steps with a note instead of failing, and it always reports
any rejected file with its fix. Results never include a GPS
position or an absolute path. The finest location they carry is the 10 km
grid cell of a missing DEM tile.

**`run_type` is not collected in this build.** Every selection tool keeps
its `run_type` parameter, but a non-empty value is rejected by name ("no
run_type source in this build"), and every per-run result reports
`run_type: null`, labelled "not collected". A later version re-enables it
through user-defined run types.

**Session load.** The `session_load` tool and the report carry Foster load =
Garmin's post-activity RPE × timer minutes. An activity without a Garmin
self-evaluation says so instead of reporting a number. Garmin's "feel"
rating is shown but never used in the load. Over a date range or `last_n`,
the load covers every sport (rides included), and says how many activities
were unrated or could not be priced; only then is the total a lower bound.

**Known limitations.** Date filters use the UTC date of a run's start, while
`list_runs` shows the local date, so an evening run close to midnight can
fall on the next day in a filter. With `projection_crs` unset, two runs over
the same hill whose positions sit mostly either side of a UTM zone line form
two segments instead of one; setting `projection_crs` avoids it.

## Tests

```sh
uv run pytest                                     # unit tests; corpus tests skip
TRAIL_FACTORS_DB=data/copy.duckdb uv run pytest   # everything
```

The suite is plain `unittest`; pytest runs it, and
`uv run python -m unittest discover tests` works too.

Corpus tests read an ingested database and run only when `TRAIL_FACTORS_DB` is
set. Point it at a **copy** of your database. The tests open it read-only and
look for runs by their properties, so they work on anyone's corpus, and they
skip with a stated reason when your corpus has no qualifying run. If the
variable is set to a path that does not exist, the run fails rather than
skipping everything, and no test may create a database file.
`tests/golden/capture.py` pins three query outputs as a private refactoring
guard; they are stored in `data/golden/` (`TRAIL_FACTORS_GOLDEN_DIR`), never in
the repo. A fresh checkout has none. Run it with `--pin` once to choose the
pinned activity list, then with no flag to capture the outputs, then with
`--check` after every refactor (it exits non-zero on a diff).

## Development

```sh
pre-commit install            # once: gitleaks + ruff on every commit
pre-commit run --all-files    # everything (gitleaks scans what is staged)
uv run ruff check . && uv run ruff format --check .
```

CI (`.github/workflows/ci.yml`) runs ruff and pytest on every push and pull
request, with `TRAIL_FACTORS_DB` unset, so corpus tests skip. A second
workflow scans the full git history with TruffleHog on every push and fails
on any verified or unknown finding. Dependabot proposes weekly updates for the
GitHub Actions and for the Python dependencies (Dependabot's `uv`
ecosystem).

## Privacy defaults

- The entire `data/` directory is gitignored. That includes your raw files
  (which carry GPS positions), the database, DEM tiles, lookups and
  `data/athlete/`. `data/athlete/` holds things you entered (window
  confirmations, segment and venue names, notes, ride logs) that cannot be
  rebuilt from anything else, so **back it up yourself**.
- `*.fit` files are ignored everywhere except `demo/` and `tests/fixtures/`,
  which are for synthetic files only.
- Weather requests send one start coordinate per run, rounded to about
  1.1 km. DEM fetches send a 10 km grid cell. Nothing else leaves the
  machine except Garmin's own API calls.

## License

MIT. See [LICENSE](LICENSE).
