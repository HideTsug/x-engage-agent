#!/usr/bin/env python3
"""Fetch following/followers list from X API v2 and maintain local cache.

Used by the Engager pipeline to:
  1) Exclude already-followed accounts from engagement candidates.
  2) Generate daily unfollow candidates (non-mutual + aged).

The cache is the single source of truth for follow-state used by engager.md.
Engager reads it; only this script writes it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values
from oauthlib.oauth1 import Client as OAuth1Client

JST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 2
# Account whose following/followers are tracked. Set X_USERNAME in the
# environment (or xmcp/.env), or pass --username on the fork side.
DEFAULT_USERNAME = os.environ.get("X_USERNAME", "your_x_handle")
DEFAULT_TTL_HOURS = 6
DEFAULT_UNFOLLOW_AGE_DAYS = int(os.environ.get("UNFOLLOW_AGE_DAYS", "7"))
MAX_RETRIES = 3
API_BASE = "https://api.x.com"
USER_AGENT = "xpost-engager-fetch/1.0"

REPO_ROOT = Path(__file__).resolve().parents[3]
IMPROVER_DIR = REPO_ROOT / "improver"
DATA_DIR = IMPROVER_DIR / "data" / "engagement"
CACHE_PATH = DATA_DIR / "following_cache.json"
CACHE_BAK_PATH = DATA_DIR / "following_cache.json.bak"
FOLLOWERS_HISTORY_PATH = DATA_DIR / "followers_history.jsonl"
CHURNED_BLOCKLIST_PATH = DATA_DIR / "churned_blocklist.json"
LOCK_PATH = DATA_DIR / ".fetch_following.lock"
ENV_PATH = REPO_ROOT / "xmcp" / ".env"


@dataclass
class Summary:
    status: str
    following: int = 0
    mutual: int = 0
    unfollow_candidates: int = 0
    reason: str | None = None

    def emit(self) -> None:
        payload = {
            "status": self.status,
            "following": self.following,
            "mutual": self.mutual,
            "unfollow_candidates": self.unfollow_candidates,
        }
        if self.reason:
            payload["reason"] = self.reason
        print(json.dumps(payload, ensure_ascii=False))


def now_jst() -> datetime:
    return datetime.now(JST)


def normalize_username(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    username = value.strip().lstrip("@").lower()
    return username or None


def usernames_from_raw(users: list[dict]) -> list[str]:
    usernames: list[str] = []
    for user in users:
        username = normalize_username(user.get("username"))
        if username:
            usernames.append(username)
    return usernames


def load_bearer() -> str | None:
    env = dotenv_values(ENV_PATH)
    return env.get("X_BEARER_TOKEN") or os.environ.get("X_BEARER_TOKEN")


def env_value(env: dict, key: str) -> str | None:
    value = env.get(key) or os.environ.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def build_oauth1_client() -> tuple[OAuth1Client | None, str | None]:
    env = dotenv_values(ENV_PATH)
    values = {
        "X_OAUTH_CONSUMER_KEY": env_value(env, "X_OAUTH_CONSUMER_KEY"),
        "X_OAUTH_CONSUMER_SECRET": env_value(env, "X_OAUTH_CONSUMER_SECRET"),
        "X_OAUTH_ACCESS_TOKEN": env_value(env, "X_OAUTH_ACCESS_TOKEN"),
        "X_OAUTH_ACCESS_TOKEN_SECRET": env_value(env, "X_OAUTH_ACCESS_TOKEN_SECRET"),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        return None, "OAuth1 credentials missing: " + ", ".join(missing)
    return (
        OAuth1Client(
            client_key=values["X_OAUTH_CONSUMER_KEY"],
            client_secret=values["X_OAUTH_CONSUMER_SECRET"],
            resource_owner_key=values["X_OAUTH_ACCESS_TOKEN"],
            resource_owner_secret=values["X_OAUTH_ACCESS_TOKEN_SECRET"],
            signature_type="AUTH_HEADER",
        ),
        None,
    )


def read_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if CACHE_BAK_PATH.exists():
            try:
                return json.loads(CACHE_BAK_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return None


def cache_is_fresh(cache: dict, ttl_hours: float) -> bool:
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return (now_jst() - ts) < timedelta(hours=ttl_hours)


def write_cache_atomic(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists():
        shutil.copy2(CACHE_PATH, CACHE_BAK_PATH)
    fd, tmp = tempfile.mkstemp(
        prefix="following_cache.", suffix=".tmp", dir=str(DATA_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, CACHE_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def append_followers_history(
    now_iso: str,
    date_str: str,
    follower_usernames: list[str],
    entries: list[dict],
    mutual_count: int,
) -> str | None:
    payload = {
        "timestamp": now_iso,
        "date": date_str,
        "followers_count": len(follower_usernames),
        "following_count": len(entries),
        "mutual": mutual_count,
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(FOLLOWERS_HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        return f"followers history append skipped: {exc}"
    return None


def acquire_lock() -> int | None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def sleep_for_retry(attempt: int, response: httpx.Response | None) -> None:
    if response is not None and response.status_code == 429:
        reset = response.headers.get("x-rate-limit-reset")
        if reset and reset.isdigit():
            wait = max(0.0, int(reset) - time.time())
            time.sleep(min(wait, 90.0) + random.uniform(0.5, 1.5))
            return
    backoff = (2 ** (attempt - 1)) + random.uniform(0.2, 1.0)
    time.sleep(backoff)


def request_with_retry(
    client: httpx.Client, method: str, url: str, **kwargs
) -> httpx.Response:
    """Issue a request with retry on 429 and 5xx. Raises on terminal failure."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt >= MAX_RETRIES:
                raise
            sleep_for_retry(attempt, None)
            continue
        if resp.status_code < 400:
            return resp
        if resp.status_code in (401, 403):
            return resp
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt >= MAX_RETRIES:
                return resp
            sleep_for_retry(attempt, resp)
            continue
        return resp
    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop exited without response")


def request_oauth1_with_retry(
    client: httpx.Client,
    oauth1_client: OAuth1Client,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """Issue an OAuth1-signed request with retry on 429 and 5xx."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            request = client.build_request(method, url, **kwargs)
            signed_url, signed_headers, _ = oauth1_client.sign(
                str(request.url),
                http_method=method,
                headers={},
            )
            resp = client.request(method, signed_url, headers=signed_headers)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt >= MAX_RETRIES:
                raise
            sleep_for_retry(attempt, None)
            continue
        if resp.status_code < 400:
            return resp
        if resp.status_code in (401, 403):
            return resp
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt >= MAX_RETRIES:
                return resp
            sleep_for_retry(attempt, resp)
            continue
        return resp
    if last_exc:
        raise last_exc
    raise RuntimeError("OAuth1 retry loop exited without response")


def fetch_user_id(client: httpx.Client, username: str) -> str:
    resp = request_with_retry(
        client, "GET", f"{API_BASE}/2/users/by/username/{username}"
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"users/by/username lookup failed: {resp.status_code} {resp.text[:200]}"
        )
    return resp.json()["data"]["id"]


def capability_check(client: httpx.Client, user_id: str) -> tuple[bool, str | None]:
    """Probe with max_results=1 to detect auth/permission issues early."""
    try:
        resp = request_with_retry(
            client,
            "GET",
            f"{API_BASE}/2/users/{user_id}/following",
            params={"max_results": 1},
        )
    except httpx.HTTPError as exc:
        return False, f"capability check network error: {exc}"
    if resp.status_code >= 400:
        return False, f"capability check returned {resp.status_code}: {resp.text[:200]}"
    return True, None


MAX_PAGES = 50  # safety cap: 50 * 1000 = 50k accounts


def fetch_paginated(
    client: httpx.Client, user_id: str, endpoint: str
) -> list[dict]:
    """Fetch all pages of /following or /followers."""
    out: list[dict] = []
    pagination_token: str | None = None
    seen_tokens: set[str] = set()
    for page in range(MAX_PAGES):
        params: dict[str, str | int] = {"max_results": 1000}
        if pagination_token:
            params["pagination_token"] = pagination_token
        resp = request_with_retry(
            client, "GET", f"{API_BASE}/2/users/{user_id}/{endpoint}", params=params
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{endpoint} fetch failed: {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        out.extend(body.get("data") or [])
        pagination_token = (body.get("meta") or {}).get("next_token")
        if not pagination_token:
            break
        if pagination_token in seen_tokens:
            raise RuntimeError(
                f"{endpoint} pagination loop detected (token reused at page {page})"
            )
        seen_tokens.add(pagination_token)
    else:
        raise RuntimeError(f"{endpoint} pagination exceeded {MAX_PAGES} pages")
    return out


def fetch_oauth1_paginated(
    client: httpx.Client,
    oauth1_client: OAuth1Client,
    user_id: str,
    endpoint: str,
) -> list[dict]:
    """Fetch all pages of OAuth1 user-context endpoints."""
    out: list[dict] = []
    pagination_token: str | None = None
    seen_tokens: set[str] = set()
    for page in range(MAX_PAGES):
        params: dict[str, str | int] = {"max_results": 1000}
        if pagination_token:
            params["pagination_token"] = pagination_token
        resp = request_oauth1_with_retry(
            client,
            oauth1_client,
            "GET",
            f"{API_BASE}/2/users/{user_id}/{endpoint}",
            params=params,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{endpoint} fetch failed: {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        out.extend(body.get("data") or [])
        pagination_token = (body.get("meta") or {}).get("next_token")
        if not pagination_token:
            break
        if pagination_token in seen_tokens:
            raise RuntimeError(
                f"{endpoint} pagination loop detected (token reused at page {page})"
            )
        seen_tokens.add(pagination_token)
    else:
        raise RuntimeError(f"{endpoint} pagination exceeded {MAX_PAGES} pages")
    return out


def fetch_private_usernames(
    oauth1_client: OAuth1Client, user_id: str, endpoint: str
) -> tuple[list[str], str | None]:
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
            users = fetch_oauth1_paginated(client, oauth1_client, user_id, endpoint)
    except Exception as exc:
        return [], f"{endpoint}: {exc}"
    return usernames_from_raw(users), None


def build_following_entries(
    following_raw: list[dict],
    follower_raw: list[dict],
    prior_cache: dict | None,
    now_iso: str,
) -> list[dict]:
    follower_ids = {u["id"] for u in follower_raw}
    prior_by_id: dict[str, dict] = {}
    if prior_cache:
        for entry in prior_cache.get("following", []) or []:
            if "id" in entry:
                prior_by_id[entry["id"]] = entry
    entries: list[dict] = []
    for u in following_raw:
        uid = u["id"]
        prior = prior_by_id.get(uid)
        first_seen = (prior or {}).get("first_seen_following") or now_iso
        entries.append(
            {
                "id": uid,
                "username": u.get("username"),
                "first_seen_following": first_seen,
                "is_mutual": uid in follower_ids,
            }
        )
    return entries


def write_unfollow_jsonl(
    entries: list[dict], date_str: str, age_days: int, now_iso: str
) -> int:
    cutoff = datetime.fromisoformat(date_str + "T00:00:00+09:00") - timedelta(
        days=age_days
    )
    candidates: list[dict] = []
    for entry in entries:
        if entry.get("is_mutual"):
            continue
        try:
            first_seen = datetime.fromisoformat(entry["first_seen_following"])
        except (KeyError, ValueError):
            continue
        if first_seen > cutoff:
            continue
        age = (now_jst() - first_seen).days
        candidates.append(
            {
                "type": "unfollow_candidate",
                "id": entry["id"],
                "username": entry.get("username"),
                "first_seen_following": entry["first_seen_following"],
                "age_days": age,
                "timestamp": now_iso,
            }
        )
    out_path = DATA_DIR / f"{date_str}_unfollow.jsonl"
    fd, tmp = tempfile.mkstemp(
        prefix=f"{date_str}_unfollow.", suffix=".tmp", dir=str(DATA_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for c in candidates:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        os.replace(tmp, out_path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(candidates)


def load_churned_usernames() -> set[str]:
    if not CHURNED_BLOCKLIST_PATH.exists():
        return set()
    try:
        body = json.loads(CHURNED_BLOCKLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    usernames: set[str] = set()
    for value in body.get("usernames", []) or []:
        username = normalize_username(value)
        if username:
            usernames.add(username)
    return usernames


def iter_unfollow_usernames() -> set[str]:
    usernames: set[str] = set()
    for path in DATA_DIR.glob("*_unfollow.jsonl"):
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    username = normalize_username(item.get("username"))
                    if username:
                        usernames.add(username)
        except OSError:
            continue
    return usernames


def write_churned_blocklist(now_iso: str) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    usernames = load_churned_usernames() | iter_unfollow_usernames()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso,
        "usernames": sorted(usernames),
    }
    fd, tmp = tempfile.mkstemp(
        prefix="churned_blocklist.", suffix=".tmp", dir=str(DATA_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, CHURNED_BLOCKLIST_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(usernames)


def _date_arg(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--date must be YYYY-MM-DD: {value}") from exc
    return value


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch X following/followers cache")
    p.add_argument("--username", default=DEFAULT_USERNAME)
    p.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    p.add_argument("--force", action="store_true")
    p.add_argument("--date", default=None, type=_date_arg, help="YYYY-MM-DD (Asia/Tokyo)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    lock_fd = acquire_lock()
    if lock_fd is None:
        Summary(status="skipped", reason="another instance running").emit()
        return 0

    try:
        return _run(args)
    finally:
        release_lock(lock_fd)


def _run(args: argparse.Namespace) -> int:
    bearer = load_bearer()
    if not bearer:
        Summary(status="failed", reason="X_BEARER_TOKEN not configured").emit()
        return 1

    prior_cache = read_cache()
    today = args.date or now_jst().strftime("%Y-%m-%d")
    now_iso = now_jst().isoformat(timespec="seconds")
    if prior_cache and not args.force and cache_is_fresh(prior_cache, args.ttl_hours):
        write_churned_blocklist(now_iso)
        Summary(
            status="skipped",
            reason="cache fresh",
            following=len(prior_cache.get("following", []) or []),
        ).emit()
        return 0

    headers = {"Authorization": f"Bearer {bearer}", "User-Agent": USER_AGENT}

    with httpx.Client(headers=headers, timeout=30.0) as client:
        user_id = (prior_cache or {}).get("user_id")
        if not user_id or (prior_cache or {}).get("username") != args.username:
            try:
                user_id = fetch_user_id(client, args.username)
            except Exception as exc:
                Summary(status="failed", reason=f"user_id lookup: {exc}").emit()
                return 1

        ok, reason = capability_check(client, user_id)
        if not ok:
            Summary(status="degraded", reason=reason or "capability check failed").emit()
            return 2

        try:
            following_raw = fetch_paginated(client, user_id, "following")
            follower_raw = fetch_paginated(client, user_id, "followers")
        except Exception as exc:
            Summary(status="failed", reason=f"fetch error: {exc}").emit()
            return 1

    oauth_reasons: list[str] = []
    oauth1_client, oauth_reason = build_oauth1_client()
    if oauth1_client is None:
        muting_usernames: list[str] = []
        blocking_usernames: list[str] = []
        oauth_reasons.append(f"muting skipped: {oauth_reason}")
        oauth_reasons.append(f"blocking skipped: {oauth_reason}")
    else:
        muting_usernames, reason = fetch_private_usernames(
            oauth1_client, user_id, "muting"
        )
        if reason:
            oauth_reasons.append(reason)
        blocking_usernames, reason = fetch_private_usernames(
            oauth1_client, user_id, "blocking"
        )
        if reason:
            oauth_reasons.append(reason)

    entries = build_following_entries(following_raw, follower_raw, prior_cache, now_iso)
    follower_usernames = usernames_from_raw(follower_raw)
    mutual_count = sum(1 for e in entries if e["is_mutual"])
    cache = {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": now_iso,
        "username": args.username,
        "user_id": user_id,
        "following_count": len(entries),
        "followers_count": len(follower_usernames),
        "following": entries,
        "followers": follower_usernames,
        "muting": muting_usernames,
        "blocking": blocking_usernames,
    }
    write_cache_atomic(cache)
    history_reason = append_followers_history(
        now_iso, today, follower_usernames, entries, mutual_count
    )
    if history_reason:
        oauth_reasons.append(history_reason)
    unfollow_n = write_unfollow_jsonl(
        entries, today, DEFAULT_UNFOLLOW_AGE_DAYS, now_iso
    )
    write_churned_blocklist(now_iso)

    Summary(
        status="degraded" if oauth_reasons else "ok",
        following=len(entries),
        mutual=mutual_count,
        unfollow_candidates=unfollow_n,
        reason="; ".join(oauth_reasons) if oauth_reasons else None,
    ).emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
