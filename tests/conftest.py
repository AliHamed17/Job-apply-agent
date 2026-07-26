"""Pytest configuration and fixtures."""

import asyncio
import inspect
import os
import sys

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """Ensure the shared DB has its schema before any test touches it.

    Tests that hit the API via TestClient(app) as a plain fixture never run
    the app's lifespan, so nothing calls init_db(). They only passed because
    some other test file happened to run first and create the tables — which
    made the suite order-dependent: whichever module sorted first against an
    empty DB failed with "no such table: jobs". create_all is idempotent, so
    doing it once up front is safe and removes the ordering coupling.
    """
    from db.session import init_db

    init_db()


@pytest.fixture
def auth_headers():
    """Bearer token for authenticated API calls.

    Locally the middleware short-circuits on the default dev secret, so a
    missing header goes unnoticed; CI sets a real SECRET_KEY and returns
    401. Shared here so new test files get it by default instead of each
    rediscovering the convention.
    """
    from core.config import get_settings

    return {"Authorization": f"Bearer {get_settings().secret_key}"}


@pytest.fixture(scope="session")
def _rate_limit_redis():
    """A reachable Redis for rate-limit resets, or None.

    Probed once per session: without Redis (a typical dev box) every
    connection attempt blocks on the socket timeout, and paying that on all
    ~250 tests stalls the suite.
    """
    try:
        import redis

        from core.config import get_settings

        client = redis.Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        return client
    except Exception:
        return None


@pytest.fixture(autouse=True)
def _reset_rate_limit_bucket(_rate_limit_redis):
    """Clear this minute's rate-limit counter before each test.

    The limiter keys on client IP and Starlette's TestClient reports its
    host as "testclient", so every test in the suite shares one bucket.
    They accumulate against a 10/min cap until it trips, failing whichever
    tests happen to run last with 429. It only shows up where Redis is
    actually reachable (CI); locally the middleware swallows the connection
    error and allows the request, which is why the suite passes on a dev box.

    Resetting per test isolates them without weakening the limiter: it stays
    fully active in production and still enforces the cap within any single
    test (no test here makes more than 6 requests). core/operations.py
    buckets by wall-clock minute, so both the current and next bucket are
    cleared to cover a test that straddles the boundary.
    """
    if _rate_limit_redis is not None:
        import time

        bucket = int(time.time() // 60)
        try:
            _rate_limit_redis.delete(
                f"job-agent:rate:testclient:{bucket}",
                f"job-agent:rate:testclient:{bucket + 1}",
            )
        except Exception:
            pass
    yield


try:
    import pytest_asyncio  # noqa: F401
except ImportError:

    def pytest_pyfunc_call(pyfuncitem):
        """Run coroutine tests when the optional asyncio plugin is absent."""
        if not inspect.iscoroutinefunction(pyfuncitem.obj):
            return None
        kwargs = {
            name: pyfuncitem.funcargs[name] for name in inspect.signature(pyfuncitem.obj).parameters
        }
        asyncio.run(pyfuncitem.obj(**kwargs))
        return True
else:
    pytest_plugins = ("pytest_asyncio",)
