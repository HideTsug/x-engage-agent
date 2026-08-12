import json
import os
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from search_quota import (
    DEFAULT_QUOTA,
    JST,
    SEARCH_PATH,
    SearchQuota,
    SearchQuotaExceeded,
    quota_from_env,
    state_path_from_env,
)


def make_quota(tmp_path: Path, quota: int = 5, now: datetime | None = None) -> SearchQuota:
    holder = {"now": now or datetime(2026, 8, 12, 12, 0, tzinfo=JST)}
    q = SearchQuota(tmp_path / "quota.json", quota, now_fn=lambda: holder["now"])
    q._test_time_holder = holder  # let tests move the clock
    return q


def read_state(q: SearchQuota) -> dict:
    return json.loads(q.state_path.read_text(encoding="utf-8"))


# -- basic counting -----------------------------------------------------------


def test_allows_quota_then_rejects(tmp_path):
    q = make_quota(tmp_path, quota=5)
    for i in range(5):
        assert q.consume()["count"] == i + 1
    with pytest.raises(SearchQuotaExceeded) as exc:
        q.consume()
    assert str(exc.value) == (
        "daily search quota exhausted (5/5, resets at JST midnight); do not retry"
    )
    assert read_state(q)["count"] == 5


# -- date rollover (JST, host-TZ independent) ---------------------------------


def test_resets_across_jst_midnight(tmp_path):
    q = make_quota(tmp_path, quota=1, now=datetime(2026, 8, 12, 23, 59, 59, tzinfo=JST))
    q.consume()
    with pytest.raises(SearchQuotaExceeded):
        q.consume()
    q._test_time_holder["now"] = datetime(2026, 8, 13, 0, 0, 0, tzinfo=JST)
    assert q.consume() == {"date": "2026-08-13", "count": 1}


def test_jst_date_derived_from_utc_clock(tmp_path):
    # 2026-08-12 23:30 UTC is already 2026-08-13 in JST, whatever the host TZ is.
    utc_now = datetime(2026, 8, 12, 23, 30, tzinfo=timezone.utc)
    q = SearchQuota(tmp_path / "quota.json", 5, now_fn=lambda: utc_now)
    assert q.initialize()["date"] == "2026-08-13"


# -- initialize() invariants ---------------------------------------------------


def test_initialize_preserves_count_across_restart(tmp_path):
    q = make_quota(tmp_path, quota=5)
    for _ in range(3):
        q.consume()

    fresh = make_quota(tmp_path, quota=5)  # same state file = daemon restart
    state = fresh.initialize()
    assert state == {"date": "2026-08-12", "count": 3}
    assert fresh.initialize() == state  # idempotent, consumes nothing

    for _ in range(2):  # only quota - k remain
        fresh.consume()
    with pytest.raises(SearchQuotaExceeded):
        fresh.consume()


def test_initialize_creates_state_without_consuming(tmp_path):
    q = make_quota(tmp_path, quota=5)
    assert not q.state_path.exists()
    assert q.initialize() == {"date": "2026-08-12", "count": 0}
    assert read_state(q) == {"date": "2026-08-12", "count": 0}


# -- missing / corrupt / write-failure (fail-closed) ---------------------------


def test_missing_state_starts_at_zero(tmp_path):
    q = make_quota(tmp_path, quota=2)
    assert q.consume()["count"] == 1


@pytest.mark.parametrize(
    "corrupt_content",
    [
        "{not json",                                        # syntax error
        json.dumps({"count": 3}),                           # missing key
        json.dumps({"date": "2026-08-12", "count": "3"}),   # wrong type
        json.dumps({"date": "2026-13-45", "count": 3}),     # invalid date
    ],
)
def test_corrupt_state_fails_closed_and_persists(tmp_path, corrupt_content):
    q = make_quota(tmp_path, quota=5)
    q.state_path.parent.mkdir(parents=True, exist_ok=True)
    q.state_path.write_text(corrupt_content, encoding="utf-8")

    with pytest.raises(SearchQuotaExceeded):
        q.consume()

    corrupt_copy = q.state_path.with_name(q.state_path.name + ".corrupt")
    assert corrupt_copy.read_text(encoding="utf-8") == corrupt_content
    # Fail-closed state is persisted: the second call must also be rejected
    # (must not be treated as "missing -> count=0").
    assert read_state(q) == {"date": "2026-08-12", "count": 5}
    with pytest.raises(SearchQuotaExceeded):
        q.consume()


def test_initialize_is_fail_closed_on_corrupt_state(tmp_path):
    q = make_quota(tmp_path, quota=5)
    q.state_path.parent.mkdir(parents=True, exist_ok=True)
    q.state_path.write_text("{not json", encoding="utf-8")
    assert q.initialize() == {"date": "2026-08-12", "count": 5}
    with pytest.raises(SearchQuotaExceeded):
        q.consume()


def test_write_failure_blocks_request(tmp_path, monkeypatch):
    q = make_quota(tmp_path, quota=5)

    def boom(state):
        raise OSError("disk full")

    monkeypatch.setattr(q, "_write_state", boom)
    with pytest.raises(OSError):
        q.consume()


# -- concurrency ---------------------------------------------------------------


def test_thread_exclusion_exactly_one_winner(tmp_path):
    q_path = tmp_path / "quota.json"
    barrier = threading.Barrier(2)
    results = []

    def worker():
        q = SearchQuota(q_path, 1)
        barrier.wait(timeout=10)
        try:
            q.consume()
            results.append("ok")
        except SearchQuotaExceeded:
            results.append("exceeded")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()

    assert sorted(results) == ["exceeded", "ok"]
    assert json.loads(q_path.read_text())["count"] == 1


_PROC_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from search_quota import SearchQuota, SearchQuotaExceeded

    state = Path(sys.argv[2]); ready = Path(sys.argv[3]); go = Path(sys.argv[4])
    q = SearchQuota(state, 1)
    ready.touch()
    deadline = time.time() + 10
    while not go.exists():
        if time.time() > deadline:
            print("timeout"); sys.exit(1)
        time.sleep(0.005)
    try:
        q.consume()
        print("ok")
    except SearchQuotaExceeded:
        print("exceeded")
    """
)


def test_process_exclusion_exactly_one_winner(tmp_path):
    repo_root = str(Path(__file__).resolve().parent.parent)
    state = tmp_path / "quota.json"
    go = tmp_path / "go"
    procs = []
    ready_files = []
    for i in range(2):
        ready = tmp_path / f"ready{i}"
        ready_files.append(ready)
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", _PROC_SCRIPT, repo_root, str(state), str(ready), str(go)],
                stdout=subprocess.PIPE,
                text=True,
            )
        )

    deadline = __import__("time").time() + 10
    while not all(r.exists() for r in ready_files):
        assert __import__("time").time() < deadline, "workers never became ready"
    go.touch()

    outputs = []
    for p in procs:
        out, _ = p.communicate(timeout=20)
        assert p.returncode == 0
        outputs.append(out.strip())

    assert sorted(outputs) == ["exceeded", "ok"]
    assert json.loads(state.read_text())["count"] == 1


# -- env parsing ---------------------------------------------------------------


def test_quota_env_default_and_valid():
    assert quota_from_env({}) == DEFAULT_QUOTA == 12
    assert quota_from_env({"X_SEARCH_DAILY_QUOTA": "1"}) == 1


@pytest.mark.parametrize("bad", ["0", "-3", "2.5", "five", " "])
def test_quota_env_rejects_invalid(bad):
    if bad.strip() == "":
        assert quota_from_env({"X_SEARCH_DAILY_QUOTA": bad}) == DEFAULT_QUOTA
    else:
        with pytest.raises(ValueError):
            quota_from_env({"X_SEARCH_DAILY_QUOTA": bad})


def test_state_path_env_override(tmp_path):
    p = tmp_path / "s.json"
    assert state_path_from_env({"X_SEARCH_QUOTA_STATE": str(p)}) == p
    default = state_path_from_env({})
    assert default.name == "search_quota.json"
    assert default.parent.name == "data"


# -- create_mcp() wiring (integration, AC1) -------------------------------------


def _minimal_spec():
    return {
        "openapi": "3.0.0",
        "info": {"title": "X", "version": "1"},
        "paths": {
            "/2/tweets/search/recent": {
                "get": {
                    "operationId": "searchPostsRecent",
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/2/users/me": {
                "get": {
                    "operationId": "getUsersMe",
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }


@pytest.fixture
def wired_client(monkeypatch, tmp_path):
    import httpx

    import server

    monkeypatch.setattr(server, "load_env", lambda: None)
    monkeypatch.setattr(server, "load_openapi_spec", _minimal_spec)
    for key, value in {
        "X_OAUTH_CONSUMER_KEY": "ck",
        "X_OAUTH_CONSUMER_SECRET": "cs",
        "X_OAUTH_ACCESS_TOKEN": "at",
        "X_OAUTH_ACCESS_TOKEN_SECRET": "as",
        "X_SEARCH_QUOTA_STATE": str(tmp_path / "quota.json"),
        "X_API_TOOL_ALLOWLIST": "",
        "X_API_DEBUG": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("X_SEARCH_DAILY_QUOTA", raising=False)

    reached = []

    def handler(request):
        reached.append(request.url.path)
        return httpx.Response(200, json={})

    created = []
    real_client = httpx.AsyncClient

    class PatchedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(server.httpx, "AsyncClient", PatchedClient)
    server.create_mcp()
    assert created, "create_mcp() did not construct an httpx.AsyncClient"
    return created[0], reached


def test_quota_hook_is_first_request_hook(wired_client):
    client, _ = wired_client
    hooks = client.event_hooks["request"]
    assert hooks and hooks[0].__name__ == "enforce_search_quota"


@pytest.mark.anyio
async def test_thirteenth_search_never_reaches_transport(wired_client):
    client, reached = wired_client
    for _ in range(12):
        response = await client.get(SEARCH_PATH)
        assert response.status_code == 200
    assert reached.count(SEARCH_PATH) == 12

    with pytest.raises(SearchQuotaExceeded) as exc:
        await client.get(SEARCH_PATH)
    assert str(exc.value) == (
        "daily search quota exhausted (12/12, resets at JST midnight); do not retry"
    )
    assert reached.count(SEARCH_PATH) == 12  # transport never saw the 13th call


@pytest.mark.anyio
async def test_non_search_paths_bypass_quota(wired_client):
    client, reached = wired_client
    for _ in range(7):  # more than the quota, all must pass through
        response = await client.get("/2/users/me")
        assert response.status_code == 200
    assert reached.count("/2/users/me") == 7


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_create_mcp_fails_on_invalid_quota(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "load_env", lambda: None)
    monkeypatch.setattr(server, "load_openapi_spec", _minimal_spec)
    for key, value in {
        "X_OAUTH_CONSUMER_KEY": "ck",
        "X_OAUTH_CONSUMER_SECRET": "cs",
        "X_OAUTH_ACCESS_TOKEN": "at",
        "X_OAUTH_ACCESS_TOKEN_SECRET": "as",
        "X_SEARCH_QUOTA_STATE": str(tmp_path / "quota.json"),
        "X_SEARCH_DAILY_QUOTA": "0",
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        server.create_mcp()
