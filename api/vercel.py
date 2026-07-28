"""Legacy root entry point with a deliberately tiny public surface.

Production deployments use the isolated ``control_plane`` project. Keeping
this module independent from :mod:`api.main` ensures an accidentally selected
legacy entry point cannot package or expose private profile, application,
browser, worker, or database functionality.
"""

from fastapi import FastAPI

app = FastAPI(
    title="Job Apply Agent deployment guard",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health/live", include_in_schema=False)
def health_live() -> dict[str, str]:
    """Return only non-sensitive process liveness."""

    return {
        "status": "ok",
        "service": "deployment-guard",
    }


__all__ = ["app"]
