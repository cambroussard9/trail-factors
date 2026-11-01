"""Garmin Connect API client (SPEC.md §10, optional auto-download).

Auth model: you log in ONCE interactively via `trail-factors garmin-login`,
which saves an OAuth token to TOKENSTORE. Everything after that resumes from
the token — no password is stored in this repo, passed on a command line, or
written to any file we control.

WHAT THE LIBRARY WRITES. `Garmin.login(tokenstore=...)` (garminconnect 0.3.x)
persists `Client.dumps()` -- `di_token`, `di_refresh_token`, `di_client_id`
and nothing else -- as a 0o600 file in a 0o700 directory, and drops its own
copy of the password once authentication succeeds. The password reaches the
library in memory, from getpass, and nowhere else.

BOTH CREDENTIAL FILES LIVE OUTSIDE THE REPO, and that is enforced here rather
than left to .gitignore: TOKENSTORE holds a refresh token that grants
persistent account access, and one `git add -A` inside the working tree would
publish it. A path under the repo root is REFUSED before anything is sent
(`check_locations`). Both are set by environment variable (src/trail_factors/paths.py,
.env.example).

The library is unofficial. Requests are deliberately paced (REQUEST_DELAY_S);
aggressive polling has historically drawn temporary account blocks.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trail_factors.paths import (GARMIN_AUTH_STATE, GARMIN_TOKENSTORE, RAW_DIR,
                       display_path, inside_repo)

from garminconnect import Garmin, GarminConnectTooManyRequestsError

# Module attributes, read by every function at CALL time, so a test can patch
# them. Defaults are ~/.garminconnect and ~/.garminconnect_auth_state.json.
TOKENSTORE = str(GARMIN_TOKENSTORE)
REQUEST_DELAY_S = 1.5
# Downloads are heavier than list calls and are the requests most likely to
# draw throttling, so they are paced more conservatively.
DOWNLOAD_DELAY_S = 3.0

# Auth failures are recorded here so a later attempt can refuse locally rather
# than spending another request against Garmin. Defaults to beside the
# tokenstore so no new directory is created. It holds a timestamp and an error
# string, nothing secret -- but it is still kept out of the repo.
AUTH_STATE = Path(GARMIN_AUTH_STATE)
AUTH_BACKOFF_S = 30 * 60


class AuthBackoff(Exception):
    """Raised locally when a previous auth failure is still inside the window."""


class CredentialPathError(Exception):
    """Raised locally when a credential file would sit inside the repo."""


def _describe_auth_state() -> str:
    """Where the auth-state file is, without printing a home directory."""
    return (f"{display_path(AUTH_STATE)} (set by TRAIL_FACTORS_GARMIN_AUTH_STATE; "
            f"default ~/.garminconnect_auth_state.json)")


def check_locations() -> None:
    """Refuse, before ANY request, if a credential path is inside the repo.

    Structural, not a .gitignore rule: an ignore pattern is one edit away
    from publishing a refresh token, and this check is not.
    """
    for env, path in (("TRAIL_FACTORS_GARMIN_TOKENSTORE", Path(TOKENSTORE)),
                      ("TRAIL_FACTORS_GARMIN_AUTH_STATE", Path(AUTH_STATE))):
        if inside_repo(path.expanduser()):
            raise CredentialPathError(
                f"Refusing to contact Garmin: {env} resolves INSIDE the "
                f"repository ({display_path(path.expanduser())}), where a "
                f"commit could publish it. Point it at a directory outside "
                f"the working tree (see .env.example). Nothing was sent.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_backoff() -> None:
    """Refuse to contact Garmin if a recent auth attempt failed.

    Every failed credential attempt counts toward Garmin's lockout and
    rate-limit thresholds, so the cheapest failure is the one we never send.
    """
    check_locations()
    try:
        state = json.loads(AUTH_STATE.read_text())
        last = datetime.fromisoformat(state["last_failure_utc"])
    except (OSError, ValueError, KeyError):
        return
    elapsed = (_now() - last).total_seconds()
    if elapsed >= AUTH_BACKOFF_S:
        return
    remaining = timedelta(seconds=int(AUTH_BACKOFF_S - elapsed))
    raise AuthBackoff(
        f"Refusing to contact Garmin: an auth attempt failed "
        f"{int(elapsed // 60)} min ago ({state.get('error', 'unknown')}).\n"
        f"Wait {remaining} before trying again.\n"
        f"Nothing was sent. To override, delete {_describe_auth_state()}"
    )


def record_failure(error: str) -> None:
    """Record a failed attempt, ATOMICALLY (temp file + os.replace).

    A plain write interrupted or read mid-flight leaves unparseable JSON,
    which `check_backoff` reads as "no recent failure" -- the one wrong
    answer this file exists to prevent. The parent is created if needed, so
    an overridden location works on first use.
    """
    check_locations()
    path = Path(AUTH_STATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(
        {"last_failure_utc": _now().isoformat(), "error": error}, indent=2))
    os.replace(tmp, path)


def clear_failure() -> None:
    AUTH_STATE.unlink(missing_ok=True)


def client() -> Garmin:
    """Resume an existing session. Raises if `garmin-login` has not been run."""
    check_locations()
    api = Garmin()
    api.login(tokenstore=TOKENSTORE)
    return api


def interactive_login(email: str, password: str) -> None:
    """Called only by the CLI, which reads the password via getpass.

    retry_attempts=1: the library defaults to 3, and it tries two transports,
    so one failed login can fire six requests at Garmin's SSO endpoint. When
    that endpoint is already rate-limiting, retrying compounds the block
    rather than clearing it. Fail fast and wait instead.
    """
    # Never send empty credentials. An empty password produced a real 401 from
    # Garmin, which counts toward lockout thresholds in exchange for nothing.
    # Rejected locally, and deliberately NOT recorded as a failure, since no
    # request was made and no backoff is warranted.
    if not (email if email is not None else "").strip():
        raise SystemExit("Email is empty. Nothing sent to Garmin.")
    if not (password if password is not None else "").strip():
        raise SystemExit(
            "Password is empty. Nothing sent to Garmin.\n"
            "getpass shows no characters as you type - that is expected."
        )

    check_backoff()
    api = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("MFA code: "),
        retry_attempts=1,
    )
    try:
        api.login(tokenstore=TOKENSTORE)
        clear_failure()
    except GarminConnectTooManyRequestsError as exc:
        record_failure("429 rate limited")
        raise SystemExit(
            f"Garmin rate-limited the login endpoint (429): {exc}\n"
            "\nThis is Garmin throttling the unofficial client's mobile login\n"
            "path, not your whole IP - connect.garmin.com itself still responds.\n"
            "\nDo NOT retry in a loop; repeated attempts extend the block.\n"
            "Wait at least an hour, then try once. If it persists, use the\n"
            "official export at privacy.garmin.com instead (SPEC.md §10)."
        ) from exc
    except Exception as exc:
        record_failure(f"{type(exc).__name__}: {exc}"[:200])
        raise


def list_activities(api: Garmin, start: str, end: str) -> list[dict]:
    """Activities between two LOCAL dates (YYYY-MM-DD), newest first."""
    time.sleep(REQUEST_DELAY_S)
    return api.get_activities_by_date(start, end)


def list_all_activities(api: Garmin, page_size: int = 200,
                        max_pages: int = 50) -> list[dict]:
    """EVERY activity on the account, paged. No date window, no sport filter.

    §4.1 IS "DOWNLOAD BROADLY, MODEL STRICTLY", and a date window at the door
    is the same mistake as a sport filter at the door: it decides what the
    corpus may ever contain, at the one point where the decision cannot be
    revisited without another network pass. Strictness belongs in the derived
    layers, which can be re-run.
    """
    out, seen = [], set()
    for page in range(max_pages):
        time.sleep(REQUEST_DELAY_S)
        batch = api.get_activities(page * page_size, page_size)
        if not batch:
            break
        for a in batch:
            aid = str(a.get("activityId"))
            if aid not in seen:
                seen.add(aid)
                out.append(a)
        if len(batch) < page_size:
            break
    return out


def download_original(api: Garmin, activity_id: str) -> bytes:
    """The original uploaded file — a .zip containing the .fit (§4: raw is truth)."""
    time.sleep(DOWNLOAD_DELAY_S)
    return api.download_activity(
        str(activity_id), dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
    )


API_CACHE_DIR = RAW_DIR / "garmin_api"


def cache_listing(api, days: int = 400) -> Path:
    """Persist the API listing as a dated, immutable oracle snapshot.

    Chosen over a manual CSV export: regenerable (CLAUDE.md), carries fields
    the CSV lacks (isElevationCorrected, movingDuration, startLatitude,
    timezone), and needs no manual step that could silently go stale. Once
    written it is offline-usable, which is the CSV's only real advantage.
    """
    import json
    from datetime import date, timedelta

    API_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    end = date.today()
    acts = list_activities(api, (end - timedelta(days=days)).isoformat(), end.isoformat())
    out = API_CACHE_DIR / f"activities_{end.isoformat()}.json"
    out.write_text(json.dumps(acts, indent=2, default=str))
    return out


def load_cached_listing() -> dict:
    """Newest cached listing, keyed by activity id. Empty if none exists."""
    import json

    files = sorted(API_CACHE_DIR.glob("activities_*.json"))
    if not files:
        return {}
    return {str(a["activityId"]): a for a in json.loads(files[-1].read_text())}
