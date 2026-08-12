"""Daily quota enforcement for X API recent-search calls.

X API bills $0.005 per post read on /2/tweets/search/recent, so an
unbounded agent loop can dominate the monthly bill. This module keeps a
small JSON state file ({"date": "YYYY-MM-DD", "count": N}, JST calendar
day) and refuses further searches once the daily budget is spent.

Concurrency and failure semantics:
- The whole read -> reset -> check -> increment -> write section runs
  under an OS-level lock on a dedicated lock file, so threads and separate
  processes cannot exceed the budget together.
- Fail-closed everywhere: a corrupt state file is quarantined to
  *.corrupt and the day is marked exhausted; a failed write refuses the
  call rather than letting an uncounted request through.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

try:
    import fcntl
except ImportError:
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

LOGGER = logging.getLogger("xmcp.search_quota")

JST = timezone(timedelta(hours=9))
SEARCH_PATH = "/2/tweets/search/recent"
# 12 matches the engager's segment allocation (client 6 / peer 3 / recruit 3)
DEFAULT_QUOTA = 12
QUOTA_ENV = "X_SEARCH_DAILY_QUOTA"
STATE_ENV = "X_SEARCH_QUOTA_STATE"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SearchQuotaExceeded(Exception):
    """Raised before the HTTP request is sent when the daily budget is spent."""


def quota_from_env(env=os.environ) -> int:
    """Parse the daily quota from the environment, failing hard on bad values."""
    raw = env.get(QUOTA_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_QUOTA
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(
            f"{QUOTA_ENV} must be a positive integer, got {raw!r}"
        ) from None
    if value < 1:
        raise ValueError(f"{QUOTA_ENV} must be a positive integer, got {raw!r}")
    return value


def state_path_from_env(env=os.environ) -> Path:
    raw = env.get(STATE_ENV)
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent / "data" / "search_quota.json"


def _is_valid_state(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    date = data.get("date")
    count = data.get("count")
    if not isinstance(date, str) or not _DATE_RE.match(date):
        return False
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return False
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return False
    return True


class SearchQuota:
    def __init__(
        self,
        state_path: Path,
        quota: int,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if isinstance(quota, bool) or not isinstance(quota, int) or quota < 1:
            raise ValueError(f"quota must be a positive integer, got {quota!r}")
        self.state_path = Path(state_path)
        self.quota = quota
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self._now_fn = now_fn or (lambda: datetime.now(JST))

    # -- time ---------------------------------------------------------------

    def _today(self) -> str:
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=JST)
        return now.astimezone(JST).strftime("%Y-%m-%d")

    # -- locking / persistence ----------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock_file:
            fd = lock_file.fileno()
            _lock(fd)
            try:
                yield
            finally:
                _unlock(fd)

    def _read_state(self) -> tuple[str, Optional[dict]]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "corrupt", None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "corrupt", None
        if not _is_valid_state(data):
            return "corrupt", None
        return "ok", data

    def _write_state(self, state: dict) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.state_path.parent),
            prefix=self.state_path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(state, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.state_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _resolve_state_locked(self) -> dict:
        """Return today's state; quarantine corrupt files as spent (fail-closed)."""
        today = self._today()
        status, data = self._read_state()
        if status == "corrupt":
            corrupt_path = self.state_path.with_name(self.state_path.name + ".corrupt")
            os.replace(self.state_path, corrupt_path)
            state = {"date": today, "count": self.quota}
            self._write_state(state)
            LOGGER.warning(
                "search quota state was corrupt; quarantined to %s and "
                "marked today's budget exhausted (fail-closed)",
                corrupt_path,
            )
            return state
        if status == "missing" or data["date"] != today:
            state = {"date": today, "count": 0}
            self._write_state(state)
            return state
        return data

    # -- public API -----------------------------------------------------------

    def initialize(self) -> dict:
        """Normalize state without consuming; idempotent, for startup logging."""
        with self._locked():
            return dict(self._resolve_state_locked())

    def consume(self) -> dict:
        with self._locked():
            state = self._resolve_state_locked()
            if state["count"] >= self.quota:
                raise SearchQuotaExceeded(self.exhausted_message())
            state = {"date": state["date"], "count": state["count"] + 1}
            self._write_state(state)
            return dict(state)

    def exhausted_message(self) -> str:
        return (
            f"daily search quota exhausted ({self.quota}/{self.quota}, "
            "resets at JST midnight); do not retry"
        )


def make_request_hook(quota: SearchQuota):
    """Build the httpx request event hook enforcing the daily search quota."""

    async def enforce_search_quota(request) -> None:
        if request.url.path != SEARCH_PATH:
            return
        state = quota.consume()
        LOGGER.info("search quota consumed: %d/%d", state["count"], quota.quota)

    return enforce_search_quota
