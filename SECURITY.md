# Security

Trail Factors is a local, single-user tool. It handles three kinds of
sensitive material:

- **GPS positions.** Raw activity files, and the database built from them,
  show where you run and where you start from.
- **Health measurements.** Heart rate, pace and the rest.
- **A Garmin Connect OAuth token.** It includes a refresh token that grants
  ongoing access to your account.

This page lists where each of these is stored, what leaves your machine, and
how to report a problem.

## Reporting a vulnerability

Please report it privately through GitHub's private vulnerability reporting:
open the **Security** tab of
[the repository](https://github.com/cambroussard9/trail-factors/security) and
choose **Report a vulnerability**. **Do not open a public issue** for a
security problem.

This is a single-maintainer hobby project. Reports are handled on a
best-effort basis, with no guaranteed response time. Please do not attach real
activity files, tokens or other personal data to a report. A synthetic FIT
file (see `tests/fitgen.py`) or the demo database is enough to reproduce most
problems.

Things worth reporting include:

- a way for a tool or command to emit a GPS position, an absolute file path
  or a credential;
- a credential that is written inside the repository;
- a write, through the MCP server, to anything other than the places listed
  below;
- a network request to a host not listed below.

## What is stored where

Every path below is a default. Each one can be moved with its
`TRAIL_FACTORS_*` environment variable (see `.env.example`). A relative value
resolves against the repository root. **Everything under the root `data/`
directory is gitignored** (`/data/` in `.gitignore`), and so is every
`*.duckdb` file.

| Location | What it holds | Committed? | How it is written |
|---|---|---|---|
| `data/raw/` | Activity `.fit` files, downloaded or added by hand, with GPS positions, heart rate and device data. Also `garmin_api/activities_<date>.json`, the cached Garmin activity listings, which include start coordinates, and `weather/`, the cached Open-Meteo responses, which hold the rounded coordinate that was sent. | No | `.fit` downloads only append, and refuse to overwrite an existing file. Nothing in the code modifies, moves or deletes a `.fit` file. The weather cache is rewritten (atomically) when the ERA5 archive replaces a forecast, and the API listing is rewritten by a second `validate --refresh` on the same day. |
| `data/ultra.duckdb` (`TRAIL_FACTORS_DB`) | Everything derived from the raw files: per-record positions, segment endpoints, stop locations, and `ingest_ledger`, which holds the device serial and file paths. | No | CLI commands, one writer at a time. The MCP server opens it read-only. It can be rebuilt from `data/raw/`. |
| `data/demo.duckdb` | The demo database. | No | `uv run trail-factors demo`, rebuilt from `demo/`. |
| `data/athlete/` | Your own input: `window_confirmations.json` (file hashes and timestamps), `segment_names.json` and `venues.json` (run-anchored, with no coordinates, and both stores refuse a position key), `notes.json`, `ride_blocks.json`, plus the hand-edited `hr_zones.json` and `bike_config.json`. | No. **It cannot be rebuilt from anything, so back it up yourself.** | Atomically (temporary file, then `os.replace`), with earlier revisions kept. |
| `data/lookup/` | `garmin_activity_summaries.json`: seven fields per activity (id, local start, type, duration, distance, max HR, average HR). It has no coordinates by construction. | No | `garmin-list history`, atomically. Never ingested. |
| `data/dem/` | USGS 3DEP elevation tiles (`*.tif`). The data is public, but the set of cells on disk shows the regions where you run. | No (`*.tif` is also ignored everywhere) | `fetch-tile` writes `.part` and moves the file into place only after verification. |
| `data/jobs.json` | State of MCP `sync` jobs. | No | Atomically. Safe to delete. |
| `data/golden/` | Private golden outputs for the refactoring guard (tests only). | No | `tests/golden/capture.py` |
| `config/athlete.yaml` | Your configuration, including the centre coordinates of your training clusters. | No (gitignored). The template `config/athlete.example.yaml` is committed. | By hand. Validated strictly on load. |
| `.env` | Your environment variables, if you keep them in a file. | No (gitignored). `.env.example` is committed. | By hand. Nothing loads it automatically. |
| Garmin token store, `~/.garminconnect` (`TRAIL_FACTORS_GARMIN_TOKENSTORE`) | The OAuth token written by the `garminconnect` library. The library writes it as a `0600` file in a `0700` directory. | Outside the repository. A path inside it is refused before anything is sent. | `uv run trail-factors garmin-login` |
| Auth-state file, `~/.garminconnect_auth_state.json` (`TRAIL_FACTORS_GARMIN_AUTH_STATE`) | The time and error message of the last failed login. No secret. | Outside the repository. A path inside it is refused. | Written after a failed login. |
| `demo/data/*.parquet` | The demo dataset: 17 tables for **11 real runs**, with their real dates, times, **positions** and measurements. Garmin activity ids are replaced by synthetic ones (`90000000001`, ...). No raw FIT files, no `ingest_ledger`, no athlete state, and no run with a confirmed recording window. | **Yes** | `scripts/export_demo.py`, a maintainer tool that reads a private database. |

To check that your private files are ignored before a commit:

```sh
git check-ignore -v data/ultra.duckdb config/athlete.yaml .env
```

## Network calls

Only the following leave the machine.

**Garmin Connect** (`garmin-login`, `sync`, `garmin-download`,
`garmin-download-all`, `garmin-list history`, `coverage-report`, which lists
the last 400 days of activities to compare start positions,
`validate --refresh`, which caches that listing, and the MCP `sync` job).
These calls go through [`garminconnect`](https://pypi.org/project/garminconnect/), an **unofficial**
library. Garmin can change its API, which may break it, and Garmin
rate-limits and may temporarily block clients that poll aggressively. Requests
are deliberately paced.

- Credentials enter only through the interactive
  `uv run trail-factors garmin-login`. It asks for your email at a prompt and
  reads your password with `getpass`, so the password is never echoed. The
  password goes to the library in memory. **This project never writes it to
  a file**, and it is never passed on a command line or through an
  environment variable. After that, everything resumes from the token store.
- Both credential paths are checked before any request. If either one
  resolves inside the repository, the command refuses and sends nothing.
- After a failed login, further attempts are refused locally for 30 minutes,
  because repeated failures extend Garmin's own rate limit. The message names
  the auth-state file to delete if you need to override it.

**Open-Meteo** (`weather`, the CLI `sync`, and the MCP `sync` job). Each
positioned activity sends **one coordinate, its first recorded position
rounded to 2 decimal places (about 1.1 km)**, with UTC times, to
`archive-api.open-meteo.com` or `api.open-meteo.com`. The fetch function
rounds the coordinate itself, and no API key is used. The value actually sent
is stored in the cached response. An activity with no position sends nothing
and gets a labelled indoor assumption instead.

**USGS 3DEP** (`fetch-tile`, which you run yourself). This sends one 10 km grid
cell's bounding box to the 3DEP product index at `tnmaccess.nationalmap.gov`,
then downloads the tile over HTTPS from an allowlisted host
(`prd-tnm.s3.amazonaws.com`).

- A redirect that leaves the allowlist or drops HTTPS is refused.
- A final URL off the allowlist is refused.
- A cell outside the United States is refused before any request is sent.
- `--missing` fetches only the cells that a coverage report has already
  named.
- A pull larger than 1.5 GB needs `--yes`.

The elevation **lookup** is local. `src/trail_factors/transform/dem.py` opens
no socket, so no position is ever sent to an elevation service.

## The MCP server

- **No port.** `trail-factors-mcp` uses the stdio transport only. It runs as
  a child process of the client that started it and binds no host and no
  port.
- **Read-only database.** Every connection the server opens is
  `read_only=True`.
- **Two things write.**
  - `save_ride_blocks` validates a stationary-ride log and writes it to the
    athlete-state directory.
  - `sync` starts a background job. The job appends new `.fit` files to
    `data/raw/`, runs the processing pipeline (which writes the database) and
    records its state in the job file. While it runs, it holds the database's
    write lock.

  Apart from the `sync` job's weather step, which rewrites a cached
  Open-Meteo response in `data/raw/weather/` when the ERA5 archive replaces a
  forecast, nothing reachable from a tool modifies an existing raw file.
  `tests/test_mcp.py` fingerprints a raw tree, calls every tool except
  `sync`, and checks that the tree is unchanged.
- **No positions or paths in output.** Results never include a GPS position
  or an absolute path. The finest location they carry is the 10 km grid cell
  of a missing DEM tile. Every message that names a file shows it relative to
  the repository, or by file name alone if it lies outside. As a backstop,
  the envelope replaces any absolute path it still contains.
- **What the server cannot control.** If your MCP client also has a shell or
  filesystem access to the clone, it can open the database or the raw files
  directly. Nothing in the server can prevent that. Whether to grant a client
  access to this folder is your decision (SPEC.md §14m-bis).

## Privacy defaults

- The whole root `data/` directory is gitignored. That covers raw files, the
  database, DEM tiles, lookups, job state and the athlete-state directory.
- `*.fit` files are ignored everywhere except `demo/` and `tests/fixtures/`,
  which are for synthetic files only. The demo itself contains no FIT files.
- `*.tif` DEM tiles, `config/athlete.yaml` and `.env` are ignored.
- Garmin credential files must sit outside the repository, and the code
  enforces this.
- Weather requests send one start coordinate per activity, rounded to about
  1.1 km. DEM fetches send a 10 km grid cell. Nothing else leaves the machine
  except Garmin's own API calls.
- **Exception: `demo/` is committed and contains real positions** for the 11
  demo runs, with synthetic Garmin ids. If you fork this project to publish
  your own demo, `scripts/export_demo.py` is where to decide what leaves your
  corpus.

## Secret scanning

- **Before each commit:** `.pre-commit-config.yaml` runs
  [gitleaks](https://github.com/gitleaks/gitleaks) v8.30.1 on staged changes,
  and ruff for lint and format.

  ```sh
  pre-commit install            # once
  pre-commit run --all-files    # everything (gitleaks scans what is staged)
  ```

- **On every push:** `.github/workflows/secrets.yml` runs
  [TruffleHog](https://github.com/trufflesecurity/trufflehog) v3.99.7 over the
  **full git history**, with `--results=verified,unknown`. Any finding fails
  the job.
- **CI:** `.github/workflows/ci.yml` runs ruff and pytest on every push and
  pull request. It runs with `contents: read` permissions and without
  persisting checkout credentials, and the secret-scanning workflow is set up
  the same way. Dependabot (`.github/dependabot.yml`) proposes weekly updates
  for the GitHub Actions and the Python dependencies.
- **The author's local setup (not part of this repository):** the author's
  own Claude Code user settings also run SonarQube's secrets hooks. One is a
  `UserPromptSubmit` hook (`sonar hook claude-prompt-submit`) and the other a
  `PreToolUse` hook on file reads (`sonar hook claude-pre-tool-use`). They need
  the `sonar` CLI and do nothing without it. Nothing in this repository
  installs or depends on them.
