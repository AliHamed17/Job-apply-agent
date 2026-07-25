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

try:
    import pytest_asyncio  # noqa: F401
except ImportError:
    def pytest_pyfunc_call(pyfuncitem):
        """Run coroutine tests when the optional asyncio plugin is absent."""
        if not inspect.iscoroutinefunction(pyfuncitem.obj):
            return None
        kwargs = {
            name: pyfuncitem.funcargs[name]
            for name in inspect.signature(pyfuncitem.obj).parameters
        }
        asyncio.run(pyfuncitem.obj(**kwargs))
        return True
else:
    pytest_plugins = ("pytest_asyncio",)
