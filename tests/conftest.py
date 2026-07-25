"""Pytest configuration and fixtures."""

import asyncio
import inspect
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
