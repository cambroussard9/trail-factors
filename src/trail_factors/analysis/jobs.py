"""Background jobs for long operations (SPEC.md §12.6).

WHY SYNC CANNOT BE SYNCHRONOUS. `sync` downloads from Garmin, ingests, runs the
DEM lookup, the silver layer, the matcher, weather and validation — minutes of
work against an MCP timeout. Worse than timing out itself, it holds DuckDB's
EXCLUSIVE WRITE LOCK for the duration, and a reader cannot open the file while
a writer holds it (measured, cross-process). So a synchronous sync tool would
make every other tool call in the conversation fail while it ran: it fails the
conversation, not just itself.

So: return a job id immediately, and poll with a separate tool. The job runs in
a thread and writes its state to a JSON file, `trail_factors.paths.JOBS_FILE`
(data/jobs.json by default, overridable by TRAIL_FACTORS_JOBS_FILE) — NOT under
data/raw/, which nothing reachable from the server may touch, and NOT beside
the athlete-supplied state, which cannot be rebuilt.

STATE IS A FILE, NOT MEMORY, on purpose. An MCP server can be restarted between
a chat's calls, and a job id that silently stopped meaning anything would be
worse than no job id at all.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import UTC, datetime

from trail_factors.paths import JOBS_FILE, REPO_ROOT, display_path

# KEPT OUT OF the athlete directory on purpose. It is operational and
# regenerable; that directory is for state that cannot be rebuilt from
# anything. See src/trail_factors/paths.py for the reasoning. The atomic-write rule
# below still applies --
# a poll landing mid-write is what made this a file in the first place.

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _load() -> dict:
    if not JOBS_FILE.exists():
        return {}
    try:
        return json.loads(JOBS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(jobs: dict) -> None:
    """Write the job file ATOMICALLY: temp file, then os.replace.

    A plain write_text is not atomic, and this file is polled while it is being
    written. `status()` reads it without the lock -- deliberately, because the
    whole point of keeping job state in a FILE is that a restarted server, or a
    different process, can still read it, and a threading lock does not reach
    across processes. A reader that caught a half-written file fell into
    _load()'s except branch, got {}, and reported "no job <id>" for a job that
    was running perfectly well.

    That is precisely the failure this module exists to prevent -- "a job id
    that silently stopped meaning anything would be worse than no job id at
    all" -- and it surfaced as a test that failed about one run in eight.
    os.replace is atomic on POSIX, so a reader now sees either the old file or
    the new one and never a partial one.
    """
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file bounded: the last 20 jobs is far more history than anyone
    # needs and stops an unattended server growing a file forever.
    trimmed = dict(
        sorted(jobs.items(), key=lambda kv: kv[1].get("started_at", ""))[-20:]
    )
    tmp = JOBS_FILE.with_name(JOBS_FILE.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(trimmed, indent=2))
    os.replace(tmp, JOBS_FILE)


def _update(job_id: str, **fields) -> None:
    with _lock:
        jobs = _load()
        jobs.setdefault(job_id, {})
        jobs[job_id].update(fields)
        _save(jobs)


def _scrub_paths(text: str) -> str:
    """`text` with the repo root and the home directory taken out.

    Job state is read back through `sync_status`, so an error message or a
    traceback here is OUTPUT: a raw traceback names every source file by its
    absolute path, home directory included. Repo paths become repo-relative
    and anything else under the home directory becomes `~`.
    """
    out = text.replace(str(REPO_ROOT) + os.sep, "")
    home = os.path.expanduser("~")
    if home and home != os.sep:
        out = out.replace(home, "~")
    return out


def new_job_id(jobs: dict | None = None) -> str:
    """Sequential and human-quotable — a chat has to say this back."""
    jobs = _load() if jobs is None else jobs
    return f"sync-{len(jobs) + 1:04d}"


def status(job_id: str | None = None) -> dict:
    jobs = _load()
    if job_id is None:
        if not jobs:
            return {"jobs": [], "latest": None}
        latest = max(jobs.items(), key=lambda kv: kv[1].get("started_at", ""))
        return {"jobs": sorted(jobs), "latest": {"job_id": latest[0], **latest[1]}}
    if job_id not in jobs:
        raise LookupError(f"no job {job_id}")
    return {"job_id": job_id, **jobs[job_id]}


def start(fn, label: str = "sync") -> str:
    """Run `fn(progress)` on a thread and return its id immediately.

    `fn` is handed a callback so it can record where it got to; a job that
    fails halfway must say WHICH step failed, or the athlete cannot tell a
    Garmin outage from a corrupt file.
    """
    with _lock:
        jobs = _load()
        job_id = new_job_id(jobs)
        jobs[job_id] = {
            "label": label,
            "state": QUEUED,
            "step": None,
            "started_at": _now(),
            "finished_at": None,
            "steps_done": [],
            "error": None,
        }
        _save(jobs)

    def progress(step: str) -> None:
        with _lock:
            jobs = _load()
            done = jobs.get(job_id, {}).get("steps_done", [])
            jobs.setdefault(job_id, {})
            jobs[job_id].update(
                {"state": RUNNING, "step": step, "steps_done": done + [step]}
            )
            _save(jobs)

    def run() -> None:
        try:
            result = fn(progress)
            _update(job_id, state=DONE, step=None, finished_at=_now(), result=result)
        except Exception as exc:
            _update(
                job_id,
                state=FAILED,
                finished_at=_now(),
                error=_scrub_paths(f"{type(exc).__name__}: {exc}"),
                traceback=_scrub_paths(traceback.format_exc())[-1500:],
            )

    threading.Thread(target=run, name=f"job-{job_id}", daemon=True).start()
    return job_id


# THE LOCK PROBE RUNS IN A CHILD PROCESS, AND THAT IS LOAD-BEARING. A sync
# started from this server runs on a THREAD of this process (jobs.start), so
# its write lock belongs to us. POSIX record locks are per process and are
# released when the process closes ANY descriptor on the file -- so opening
# the database here to ask about the lock, and closing it, would silently drop
# the running sync's lock. A child holds no lock of ours, and F_GETLK only
# ASKS: it acquires nothing, so it cannot collide with a writer starting at
# that instant. It asks for a READ lock, which conflicts only with a writer:
# a reader is not a lock that stops a read. Measured on macOS:
# free -> False, writer held -> True with its pid, reader only -> False.
_LOCK_PROBE = r"""
import fcntl, os, struct, sys
if sys.platform == "darwin":
    fmt, order = "qqihh", ("start", "len", "pid", "type", "whence")
elif sys.platform.startswith("linux"):
    fmt, order = "@hhqqi4x", ("type", "whence", "start", "len", "pid")
else:
    print("unsupported"); sys.exit(0)
v = {"start": 0, "len": 0, "pid": 0, "type": fcntl.F_RDLCK, "whence": os.SEEK_SET}
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    out = dict(zip(order, struct.unpack(fmt, fcntl.fcntl(
        fd, fcntl.F_GETLK, struct.pack(fmt, *[v[k] for k in order])))))
finally:
    os.close(fd)
print("locked %d" % out["pid"] if out["type"] != fcntl.F_UNLCK else "free")
"""
LOCK_PROBE_TIMEOUT_S = 2.0


def database_lock_state(db_file) -> dict:
    """Whether a WRITER holds `db_file`, determined without blocking.

    `write_locked` is True / False, or None when the probe could not say --
    never a guess in either direction.
    """
    import subprocess
    import sys as _sys

    if not db_file.exists():
        return {
            "write_locked": False,
            "holder_pid": None,
            "basis": f"no database file at {display_path(db_file)}",
        }
    try:
        out = subprocess.run(
            [_sys.executable, "-I", "-c", _LOCK_PROBE, str(db_file)],
            capture_output=True,
            text=True,
            timeout=LOCK_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {
            "write_locked": None,
            "holder_pid": None,
            "basis": f"lock probe did not answer within {LOCK_PROBE_TIMEOUT_S:.0f}s",
        }
    line = out.stdout.strip()
    if out.returncode != 0 or not line or line == "unsupported":
        # The probe's stderr can quote the file it was handed; that is an
        # absolute path, so it goes out as a display path.
        err = out.stderr.strip().replace(str(db_file), display_path(db_file))
        detail = _scrub_paths(err if err else line)
        return {
            "write_locked": None,
            "holder_pid": None,
            "basis": f"lock probe could not run: {detail[:200]}",
        }
    if line == "free":
        return {
            "write_locked": False,
            "holder_pid": None,
            "basis": "F_GETLK from a child process: no writer",
        }
    pid = int(line.split()[1])
    return {
        "write_locked": True,
        "holder_pid": pid,
        "basis": (
            "F_GETLK from a child process: a writer holds it"
            + (" -- THIS server (a sync job is writing)" if pid == os.getpid() else "")
        ),
    }
