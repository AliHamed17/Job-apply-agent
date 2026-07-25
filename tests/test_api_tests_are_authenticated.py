"""Guard: API tests must send an Authorization header.

Five separate test files have now shipped calling authed endpoints with no
header. They pass locally — api/main.py's auth middleware short-circuits when
app_env is development AND secret_key is still the default "change-me" — and
then fail in CI with a bare `assert 401 == 200`, which says nothing about the
cause.

This turns that into an explicit failure at the point of the mistake, naming
the fixture to use. It is a source-level check because the alternative
(actually calling every endpoint) would be far slower and no more accurate.
"""

from __future__ import annotations

import ast
import pathlib

# Endpoints intentionally exempt from auth in api/main.py's middleware.
_EXEMPT_PREFIXES = (
    "/webhook",
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/static",
    "/favicon.ico",
)

_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}


def _literal_path(call: ast.Call) -> str | None:
    """The URL argument, when it is statically readable."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):  # f-string
        parts = [n.value for n in first.values if isinstance(n, ast.Constant)]
        return "".join(str(p) for p in parts)
    return None


def test_every_api_test_call_sends_auth_headers():
    offenders: list[str] = []

    for path in sorted(pathlib.Path("tests").glob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _HTTP_METHODS:
                continue
            # Only calls on something named like a test client.
            target = getattr(func.value, "id", "") or getattr(func.value, "attr", "")
            if "client" not in target.lower():
                continue

            url = _literal_path(node)
            if url is None:
                continue  # not statically known — leave it alone
            if not url.startswith("/") or url.startswith(_EXEMPT_PREFIXES):
                continue
            if any(k.arg == "headers" for k in node.keywords):
                continue
            offenders.append(f"{path.name}:{node.lineno} -> {url}")

    assert not offenders, (
        "These test calls hit authed endpoints with no Authorization header. "
        "They pass locally (the middleware skips auth on the default dev "
        "secret) but return 401 in CI.\n\nAdd the shared fixture:\n"
        "    def test_x(client, auth_headers):\n"
        "        client.get('/api/...', headers=auth_headers)\n\n"
        + "\n".join(offenders)
    )
