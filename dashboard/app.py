"""Dashboard service.

Replaces the Looker Studio report the previous generation depended on. That
report had to be copied by hand, pointed at a data source by hand, and could
not express the one thing this data needs -- a filter on whether a ratio is
meaningful at all. Serving it ourselves costs one small Cloud Run service and
makes the data-quality view possible.

Read-only by design: it issues SELECTs against precomputed views and has no
write path to anything. Every user-facing route enforces both cryptographic
caller authentication (Direct Cloud Run IAP JWT or Google OIDC Bearer token)
and per-user project authorization (`cloudquotas.quotaInfos.list` evaluated
across Org / Folder / Project hierarchy via Cloud Asset Inventory).
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .authz import (
    AuthzContext,
    CallerIdentity,
    UnauthenticatedError,
    authenticate_request,
    authorizer,
    clear_authz_cache,
)
from .queries import Repository, clear_cache, severity

_LOG = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

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


def _require_caller(request: Request) -> CallerIdentity:
    try:
        return authenticate_request(request)
    except UnauthenticatedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _resolve_authz(caller: CallerIdentity, r: Repository) -> AuthzContext:
    r.warm_caches()
    targets = r.known_projects()
    auth = authorizer()
    allowed = auth.allowed_projects(caller.email, targets)
    return AuthzContext(
        email=caller.email,
        auth_source=caller.auth_source,
        permission=auth.permission,
        allowed_projects=allowed,
        total_projects=len(targets),
    )


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Warm the BigQuery cache in a background thread as soon as the worker starts."""
    if os.environ.get("QMS_PROJECT"):
        try:
            repo().warm_async()
        except Exception:  # noqa: BLE001
            _LOG.warning("could not schedule startup cache warm-up", exc_info=True)
    yield


app = FastAPI(
    title="Quota Monitoring",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=500)


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _num(value) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1e18:
        return "Unlimited"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:.2f}"


templates.env.filters["pct"] = _pct
templates.env.filters["num"] = _num
templates.env.globals["severity"] = severity

_STATIC_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "favicon.ico",
        media_type="image/x-icon",
        headers=_STATIC_CACHE_HEADERS,
    )


@app.get("/favicon.png", include_in_schema=False)
def favicon_png() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "favicon.png",
        media_type="image/png",
        headers=_STATIC_CACHE_HEADERS,
    )


@app.get("/static/logo.png", include_in_schema=False)
def static_logo_png() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "logo.png",
        media_type="image/png",
        headers=_STATIC_CACHE_HEADERS,
    )


@app.get("/livez")
@app.get("/healthz")
def healthz() -> dict:
    """Liveness only -- deliberately does not touch BigQuery.

    Cloud Run's health check should answer "is the process serving?", not "is
    the warehouse up?". Conflating the two turns a BigQuery blip into a restart
    loop. ``/livez`` is the primary route because Google's ``*.run.app`` edge
    intercepts ``/healthz`` on external requests.
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
    limit: int = Query(500, ge=1, le=2000),
) -> HTMLResponse:
    caller = _require_caller(request)
    r = repo()
    try:
        authz_ctx = _resolve_authz(caller, r)
        snap = r.snapshot(
            limit=limit,
            min_ratio=min_ratio,
            allowed_projects=authz_ctx.allowed_projects,
        )
        context = {
            **snap,
            "authz": authz_ctx,
            "min_ratio": min_ratio,
            "host_project": r.project,
            "dataset": r.dataset,
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
            "authz": AuthzContext(
                email=caller.email,
                auth_source=caller.auth_source,
                permission=authorizer().permission,
                allowed_projects=frozenset(),
                total_projects=0,
            ),
            "min_ratio": min_ratio,
            "host_project": os.environ.get("QMS_PROJECT", ""),
            "dataset": os.environ.get("QMS_DATASET", "quota_monitoring"),
            "error": str(exc),
        }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/api/history")
def history(
    request: Request,
    project_id: str,
    service: str,
    quota_metric: str,
    location: str,
    limit_name: str = "",
) -> dict:
    caller = _require_caller(request)
    r = repo()
    authz_ctx = _resolve_authz(caller, r)
    if project_id not in authz_ctx.allowed_projects:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Principal {caller.email} lacks {authz_ctx.permission} on project {project_id}"
            ),
        )
    try:
        rows = r.history(
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
def quality_detail(request: Request, flag: str) -> dict:
    caller = _require_caller(request)
    r = repo()
    authz_ctx = _resolve_authz(caller, r)
    try:
        return {"rows": r.quality_rows(flag, allowed_projects=authz_ctx.allowed_projects)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/refresh")
def refresh(request: Request) -> dict:
    """Drop both data and IAM authorization caches and trigger a fresh snapshot."""
    _require_caller(request)
    clear_cache()
    clear_authz_cache()
    if os.environ.get("QMS_PROJECT"):
        repo().warm_async()
    return {"status": "cleared"}


def main() -> None:
    import argparse

    import uvicorn

    from . import queries as qmod

    parser = argparse.ArgumentParser(description="Run the QMS dashboard service.")
    parser.add_argument("--project", default=os.environ.get("QMS_PROJECT", ""))
    parser.add_argument("--dataset", default=os.environ.get("QMS_DATASET", "quota_monitoring"))
    parser.add_argument("--bq-location", default=os.environ.get("QMS_BQ_LOCATION", "US"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--dev-user",
        default="",
        help="Local development only: set QMS_AUTHZ_MODE=dev and QMS_DEV_USER_EMAIL.",
    )
    args = parser.parse_args()

    if args.project:
        os.environ["QMS_PROJECT"] = args.project
        qmod.PROJECT = args.project
    if args.dataset:
        os.environ["QMS_DATASET"] = args.dataset
        qmod.DATASET = args.dataset
    if args.bq_location:
        os.environ["QMS_BQ_LOCATION"] = args.bq_location
        qmod.LOCATION = args.bq_location
    if args.dev_user and not os.environ.get("K_SERVICE"):
        os.environ["QMS_AUTHZ_MODE"] = "dev"
        os.environ["QMS_DEV_USER_EMAIL"] = args.dev_user

    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - Cloud Run requires binding all interfaces
        port=args.port,
        log_level=os.environ.get("QMS_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
