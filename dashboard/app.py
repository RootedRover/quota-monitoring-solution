"""Dashboard service.

Replaces the Looker Studio report the previous generation depended on. That
report had to be copied by hand, pointed at a data source by hand, and could
not express the one thing this data needs -- a filter on whether a ratio is
meaningful at all. Serving it ourselves costs one small Cloud Run service and
makes the data-quality view possible.

Read-only by design: it issues SELECTs against precomputed views and has no
write path to anything.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .queries import Repository, clear_cache, severity

_LOG = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Quota Monitoring", docs_url=None, redoc_url=None)

_repo: Repository | None = None


def repo() -> Repository:
    """Built lazily so the container starts even if BigQuery is unreachable.

    Cloud Run treats a failed startup as a failed revision. A dashboard that
    boots and shows an error is far easier to debug than a revision that never
    goes live.
    """
    global _repo
    if _repo is None:
        _repo = Repository()
    return _repo


def _pct(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value * 100:.1f}%"


def _num(value) -> str:
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1e18:
        return "unlimited"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}"


templates.env.filters["pct"] = _pct
templates.env.filters["num"] = _num
templates.env.globals["severity"] = severity


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only -- deliberately does not touch BigQuery.

    Cloud Run's health check should answer "is the process serving?", not "is
    the warehouse up?". Conflating the two turns a BigQuery blip into a restart
    loop.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    try:
        repo().freshness()
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller verbatim
        return JSONResponse({"status": "degraded", "error": str(exc)}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    min_ratio: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(200, ge=1, le=1000),
) -> HTMLResponse:
    r = repo()
    try:
        context = {
            "summary": r.summary(),
            "risk": r.risk(limit=limit, min_ratio=min_ratio),
            "movers": r.movers(),
            "hierarchy": r.hierarchy(),
            "quality": r.quality(),
            "freshness": r.freshness(),
            "min_ratio": min_ratio,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - render the failure, don't 500
        _LOG.exception("dashboard query failed")
        context = {
            "summary": {},
            "risk": [],
            "movers": [],
            "hierarchy": [],
            "quality": [],
            "freshness": {},
            "min_ratio": min_ratio,
            "error": str(exc),
        }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/api/history")
def history(
    project_id: str,
    service: str,
    quota_metric: str,
    location: str,
    limit_name: str = "",
) -> dict:
    try:
        rows = repo().history(
            project_id=project_id,
            service=service,
            quota_metric=quota_metric,
            limit_name=limit_name,
            location=location,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"series": rows}


@app.get("/api/quality/{flag}")
def quality_detail(flag: str) -> dict:
    try:
        return {"rows": repo().quality_rows(flag)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/refresh")
def refresh() -> dict:
    """Drop the cache. Does not re-collect -- that is the job's business."""
    clear_cache()
    return {"status": "cleared"}


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - Cloud Run requires binding all interfaces
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("QMS_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
