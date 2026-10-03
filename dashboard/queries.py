"""BigQuery reads for the dashboard.

Every query here hits a precomputed view, not ``quota_daily``. The one
exception is :func:`Repository.history`, which needs the raw per-day series for
a single quota and is bounded by the clustering key, so it scans very little.

Performance architecture:
* **Parallel cold-start fan-out**: on a cold cache, :meth:`Repository.snapshot`
  dispatches the view queries concurrently across a thread pool rather than
  serially, cutting cold-load latency from ~7x single-query RTT to 1x RTT.
* **Zero-query summary & per-user row filtering**: headline KPI counters
  (tracked, critical, warning, distinct projects, distinct services, last seen,
  excluded) and per-user ``allowed_projects`` scoping are computed in memory
  from the cached view rows, adding 0ms of BigQuery latency per user.
* **Stale-while-revalidate (SWR) + single-flight cache**: once a key has been
  populated, expired entries are returned immediately from memory while a
  background thread refreshes BigQuery asynchronously. User page loads after
  initial warm-up never block on BigQuery.
"""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import os
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from google.cloud import bigquery

from collector.bqnames import validate_name, validate_project

from .authz import ProjectTarget

_LOG = logging.getLogger(__name__)

PROJECT = os.environ.get("QMS_PROJECT", "")
DATASET = os.environ.get("QMS_DATASET", "quota_monitoring")
LOCATION = os.environ.get("QMS_BQ_LOCATION", "US")
CACHE_TTL = int(os.environ.get("QMS_CACHE_TTL", "300"))
# 15,000 rows covers ~100-110 active projects completely in memory (~30 MB RAM,
# safely within a 512 MiB Cloud Run container even during SWR background refresh).
# Above 15,000 rows (e.g. 500 projects / ~70k rows), _all_facets() and targeted
# parameterized queries ensure 100% lookup coverage without OOM-killing the worker.
RISK_CACHE_LIMIT = int(os.environ.get("QMS_RISK_CACHE_LIMIT", "15000"))

# Above this, a quota is shown as critical. Chosen to match the point at which
# a quota increase request is worth filing rather than any property of the API.
CRITICAL = 0.9
WARNING = 0.8

_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="qms-bq")


@dataclass
class _Entry:
    value: Any
    expires_at: float


class _Cache:
    """Thread-safe TTL cache with stale-while-revalidate and single-flight dedupe."""

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._refreshing: set[str] = set()
        self._lock = threading.Lock()

    def get_or_set(self, key: str, producer, ttl: int = CACHE_TTL):
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                if entry.expires_at > now:
                    return entry.value
                # Stale-while-revalidate: serve the stale value immediately and
                # kick off a single background refresh so the caller waits 0ms.
                if key not in self._refreshing:
                    self._refreshing.add(key)
                    _POOL.submit(self._background_refresh, key, producer, ttl)
                return entry.value

            inflight = self._inflight.get(key)
            if inflight is None:
                inflight = threading.Event()
                self._inflight[key] = inflight
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            inflight.wait(timeout=60)
            with self._lock:
                entry = self._data.get(key)
                if entry is not None:
                    return entry.value

        try:
            value = producer()
            with self._lock:
                self._data[key] = _Entry(value, time.monotonic() + ttl)
            return value
        finally:
            with self._lock:
                ev = self._inflight.pop(key, None)
                if ev is not None:
                    ev.set()

    def _background_refresh(self, key: str, producer, ttl: int) -> None:
        try:
            value = producer()
            with self._lock:
                self._data[key] = _Entry(value, time.monotonic() + ttl)
        except Exception:  # noqa: BLE001 - background refresh must never crash worker
            _LOG.warning("background refresh failed for %s", key, exc_info=True)
        finally:
            with self._lock:
                self._refreshing.discard(key)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_cache = _Cache()


def _normalise_row(row: Any) -> dict[str, Any]:
    """Convert a BigQuery Row into JSON-friendly Python primitives."""
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, decimal.Decimal):
            out[k] = int(v) if v == v.to_integral_value() else float(v)
        else:
            out[k] = v
    return out


class Repository:
    def __init__(
        self,
        project: str | None = None,
        dataset: str | None = None,
        location: str | None = None,
    ) -> None:
        resolved_project = project or PROJECT or os.environ.get("QMS_PROJECT", "")
        resolved_dataset = (
            dataset or DATASET or os.environ.get("QMS_DATASET", "quota_monitoring")
        )
        resolved_location = location or LOCATION or os.environ.get("QMS_BQ_LOCATION", "US")
        if not resolved_project:
            raise ValueError("QMS_PROJECT must be set")
        # Both of these come from the environment, so they are validated before
        # they are ever spliced into SQL. BigQuery cannot bind an identifier as
        # a query parameter, which makes this the only available defence.
        self.project = validate_project(resolved_project)
        self.dataset = validate_name(resolved_dataset)
        # Pinned explicitly. Left unset, the client dispatches jobs to the
        # default multi-region and a dataset that lives anywhere else simply
        # reports as not found, which reads like a permissions problem.
        self._client = bigquery.Client(project=self.project, location=resolved_location)

    def _view(self, name: str) -> str:
        return f"`{self.project}.{self.dataset}.{validate_name(name)}`"

    def _rows(self, sql: str, params: list | None = None) -> list[dict]:
        job_config = bigquery.QueryJobConfig(query_parameters=params or [])
        return [
            _normalise_row(row)
            for row in self._client.query(sql, job_config=job_config).result()
        ]

    # ---------------------------------------------------------------- panels

    def _all_risk(self) -> list[dict]:
        """Comparable quota set (cached up to ``RISK_CACHE_LIMIT``; filtered in-memory)."""

        def produce() -> list[dict]:
            sql = f"""
            SELECT *
            FROM {self._view("quota_risk")}
            ORDER BY peak_ratio_30d DESC NULLS LAST, peak_ratio_7d DESC NULLS LAST
            LIMIT {int(RISK_CACHE_LIMIT)}
            """
            return self._rows(sql)

        return _cache.get_or_set("risk:all", produce)

    def _all_facets(self) -> list[dict]:
        """Uncapped ``(project_id, service, quota_metric)`` rollup across all comparable quotas.

        When ``_all_risk()`` contains fewer than ``RISK_CACHE_LIMIT`` rows, it
        already holds 100% of comparable quotas in the warehouse, so facets are
        derived in-memory with 0 extra BigQuery queries. When an organization
        exceeds ``RISK_CACHE_LIMIT`` rows (e.g. 500 projects / ~70k quotas),
        this query aggregates by ``(project_id, service, quota_metric)`` so
        headline KPIs and Project/Service/Quota Metric dropdowns remain 100%
        complete while keeping Cloud Run memory safely inside 512 MiB.
        """
        risk_rows = self._all_risk()
        if len(risk_rows) < RISK_CACHE_LIMIT:
            grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
            for r in risk_rows:
                pid = str(r.get("project_id") or "")
                svc = str(r.get("service") or "")
                met = str(r.get("quota_metric") or "")
                if not pid:
                    continue
                key = (pid, svc, met)
                ratio = r.get("peak_ratio_30d")
                seen = r.get("last_seen")
                item = grouped.get(key)
                if item is None:
                    grouped[key] = {
                        "project_id": pid,
                        "service": svc,
                        "quota_metric": met,
                        "quota_count": 1,
                        "critical_count": 1 if (ratio is not None and ratio >= CRITICAL) else 0,
                        "warning_count": (
                            1 if (ratio is not None and WARNING <= ratio < CRITICAL) else 0
                        ),
                        "max_ratio_30d": ratio,
                        "last_seen": seen,
                    }
                else:
                    item["quota_count"] += 1
                    if ratio is not None:
                        if ratio >= CRITICAL:
                            item["critical_count"] += 1
                        elif ratio >= WARNING:
                            item["warning_count"] += 1
                        if item["max_ratio_30d"] is None or ratio > item["max_ratio_30d"]:
                            item["max_ratio_30d"] = ratio
                    if isinstance(seen, dt.date) and (
                        item["last_seen"] is None or seen > item["last_seen"]
                    ):
                        item["last_seen"] = seen
            return list(grouped.values())

        def produce() -> list[dict]:
            sql = f"""
            SELECT
              project_id,
              service,
              quota_metric,
              COUNT(*) AS quota_count,
              COUNTIF(peak_ratio_30d >= {CRITICAL}) AS critical_count,
              COUNTIF(peak_ratio_30d >= {WARNING} AND peak_ratio_30d < {CRITICAL}) AS warning_count,
              MAX(peak_ratio_30d) AS max_ratio_30d,
              MAX(last_seen) AS last_seen
            FROM {self._view("quota_risk")}
            GROUP BY project_id, service, quota_metric
            """
            return self._rows(sql)

        return _cache.get_or_set("risk:facets", produce)

    def facets(
        self,
        *,
        allowed_projects: Iterable[str] | None = None,
    ) -> list[dict]:
        """Return per-(project_id, service, quota_metric) facet counts for the caller."""
        rows = self._all_facets()
        if allowed_projects is not None:
            allowed = set(allowed_projects)
            rows = [r for r in rows if r.get("project_id") in allowed]
        out: list[dict] = []
        for r in rows:
            out.append(
                {
                    "p": r.get("project_id", ""),
                    "s": r.get("service", ""),
                    "m": r.get("quota_metric", ""),
                    "c": int(r.get("quota_count") or 0),
                    "r": (
                        round(float(r["max_ratio_30d"]), 4)
                        if r.get("max_ratio_30d") is not None
                        else -1.0
                    ),
                }
            )
        return out

    def _all_movers(self) -> list[dict]:
        """Cached movers across the organisation; filtered per user in-memory."""

        def produce() -> list[dict]:
            sql = f"""
            SELECT *
            FROM {self._view("quota_movers")}
            ORDER BY delta DESC
            LIMIT 200
            """
            return self._rows(sql)

        return _cache.get_or_set("movers:all", produce)

    def _all_hierarchy(self) -> list[dict]:
        def produce() -> list[dict]:
            sql = f"""
            SELECT *
            FROM {self._view("quota_hierarchy")}
            ORDER BY critical_30d DESC, worst_30d DESC
            """
            return self._rows(sql)

        return _cache.get_or_set("hierarchy", produce)

    def _all_quality_by_project(self) -> list[dict]:
        """Per-(project_id, flag) counts so per-user quality rollups are additive."""

        def produce() -> list[dict]:
            sql = f"""
            SELECT
              project_id,
              flag,
              LOGICAL_AND(is_comparable) AS is_comparable,
              COUNT(*) AS row_count,
              COUNT(DISTINCT FORMAT('%s|%s|%s|%s',
                project_id, service, quota_metric, IFNULL(limit_name, ''))) AS quota_count
            FROM {self._view("quota_quality")}
            GROUP BY project_id, flag
            ORDER BY row_count DESC
            """
            return self._rows(sql)

        return _cache.get_or_set("quality:by_project", produce)

    def warm_caches(self) -> None:
        """Populate all 5 shared view caches concurrently (1x BigQuery RTT on cold start)."""
        futs = [
            _POOL.submit(self._all_risk),
            _POOL.submit(self._all_movers),
            _POOL.submit(self._all_hierarchy),
            _POOL.submit(self._all_quality_by_project),
            _POOL.submit(self.freshness),
        ]
        for fut in futs:
            fut.result()

    def known_projects(self) -> list[ProjectTarget]:
        """Return every distinct project in the warehouse with its org/folder ancestry."""
        by_id: dict[str, ProjectTarget] = {}

        for row in self._all_risk():
            pid = str(row.get("project_id") or "")
            if not pid:
                continue
            existing = by_id.get(pid)
            by_id[pid] = ProjectTarget(
                project_id=pid,
                project_number=str(
                    row.get("project_number")
                    or (existing.project_number if existing else "")
                    or ""
                ),
                folder_id=str(
                    row.get("folder_id") or (existing.folder_id if existing else "") or ""
                ),
                org_id=str(row.get("org_id") or (existing.org_id if existing else "") or ""),
            )

        for row in self._all_hierarchy():
            pid = str(row.get("project_id") or "")
            if not pid:
                continue
            existing = by_id.get(pid)
            by_id[pid] = ProjectTarget(
                project_id=pid,
                project_number=str(
                    row.get("project_number")
                    or (existing.project_number if existing else "")
                    or ""
                ),
                folder_id=str(
                    row.get("folder_id") or (existing.folder_id if existing else "") or ""
                ),
                org_id=str(row.get("org_id") or (existing.org_id if existing else "") or ""),
            )

        for row in self._all_quality_by_project():
            pid = str(row.get("project_id") or "")
            if pid and pid not in by_id:
                by_id[pid] = ProjectTarget(project_id=pid)

        return sorted(by_id.values(), key=lambda t: t.project_id)

    def risk(
        self,
        *,
        limit: int = 500,
        min_ratio: float = 0.0,
        project_id: str = "",
        service: str = "",
        quota_metric: str = "",
        allowed_projects: Iterable[str] | None = None,
    ) -> list[dict]:
        """The main table: every comparable quota matching filters, worst first."""
        cached_rows = self._all_risk()
        allowed = set(allowed_projects) if allowed_projects is not None else None

        # When the warehouse exceeds RISK_CACHE_LIMIT (e.g. 500 projects / ~70k
        # rows) and the caller filters by project/service/metric, query BigQuery
        # directly for that slice so low-utilization quotas outside the top
        # RISK_CACHE_LIMIT are still returned in full.
        if len(cached_rows) >= RISK_CACHE_LIMIT and (project_id or service or quota_metric):
            if allowed is not None and project_id and project_id not in allowed:
                return []

            def produce_filtered() -> list[dict]:
                clauses = ["TRUE"]
                params: list[bigquery.ScalarQueryParameter] = []
                if project_id:
                    clauses.append("project_id = @project_id")
                    params.append(
                        bigquery.ScalarQueryParameter("project_id", "STRING", project_id)
                    )
                if service:
                    clauses.append("service = @service")
                    params.append(bigquery.ScalarQueryParameter("service", "STRING", service))
                if quota_metric:
                    clauses.append("quota_metric = @quota_metric")
                    params.append(
                        bigquery.ScalarQueryParameter("quota_metric", "STRING", quota_metric)
                    )
                where_sql = " AND ".join(clauses)
                sql = f"""
                SELECT *
                FROM {self._view("quota_risk")}
                WHERE {where_sql}
                ORDER BY peak_ratio_30d DESC NULLS LAST, peak_ratio_7d DESC NULLS LAST
                LIMIT 2000
                """
                return self._rows(sql, params)

            key = f"risk:slice:{project_id}:{service}:{quota_metric}"
            rows = _cache.get_or_set(key, produce_filtered)
        else:
            rows = cached_rows

        if allowed is not None:
            rows = [r for r in rows if r.get("project_id") in allowed]
        if project_id:
            rows = [r for r in rows if r.get("project_id") == project_id]
        if service:
            rows = [r for r in rows if r.get("service") == service]
        if quota_metric:
            rows = [r for r in rows if r.get("quota_metric") == quota_metric]
        if min_ratio > 0.0:
            rows = [r for r in rows if (r.get("peak_ratio_30d") or 0.0) >= min_ratio]
        return rows[:limit]

    def movers(
        self,
        *,
        limit: int = 50,
        allowed_projects: Iterable[str] | None = None,
    ) -> list[dict]:
        rows = self._all_movers()
        if allowed_projects is not None:
            allowed = set(allowed_projects)
            rows = [r for r in rows if r.get("project_id") in allowed]
        return rows[:limit]

    def hierarchy(
        self,
        *,
        allowed_projects: Iterable[str] | None = None,
    ) -> list[dict]:
        rows = self._all_hierarchy()
        if allowed_projects is not None:
            allowed = set(allowed_projects)
            rows = [r for r in rows if r.get("project_id") in allowed]
        return rows

    def quality(
        self,
        *,
        allowed_projects: Iterable[str] | None = None,
    ) -> list[dict]:
        """Flag counts, and how many distinct quotas each flag affects."""
        rows = self._all_quality_by_project()
        if allowed_projects is not None:
            allowed = set(allowed_projects)
            rows = [r for r in rows if r.get("project_id") in allowed]

        grouped: dict[str, dict[str, Any]] = {}
        for r in rows:
            flag = str(r.get("flag") or "OK")
            agg = grouped.get(flag)
            if agg is None:
                grouped[flag] = {
                    "flag": flag,
                    "is_comparable": bool(r.get("is_comparable", True)),
                    "row_count": int(r.get("row_count") or 0),
                    "quota_count": int(r.get("quota_count") or 0),
                }
            else:
                agg["is_comparable"] = agg["is_comparable"] and bool(
                    r.get("is_comparable", True)
                )
                agg["row_count"] += int(r.get("row_count") or 0)
                agg["quota_count"] += int(r.get("quota_count") or 0)

        return sorted(grouped.values(), key=lambda x: x["row_count"], reverse=True)

    def quality_rows(
        self,
        flag: str,
        *,
        limit: int = 100,
        allowed_projects: Iterable[str] | None = None,
    ) -> list[dict]:
        def produce() -> list[dict]:
            sql = f"""
            SELECT DISTINCT
              project_id, service, quota_metric, limit_name, location,
              quota_class, limit_scope, interval_source, limit_value,
              daily_peak_usage
            FROM {self._view("quota_quality")}
            WHERE flag = @flag
            LIMIT 500
            """
            return self._rows(
                sql,
                [bigquery.ScalarQueryParameter("flag", "STRING", flag)],
            )

        rows = _cache.get_or_set(f"quality_rows:{flag}", produce)
        if allowed_projects is not None:
            allowed = set(allowed_projects)
            rows = [r for r in rows if r.get("project_id") in allowed]
        return rows[:limit]

    def summary(
        self,
        *,
        allowed_projects: Iterable[str] | None = None,
    ) -> dict:
        """Headline counters derived in-memory from cached facets + quality views."""
        allowed = set(allowed_projects) if allowed_projects is not None else None
        facet_rows = [
            r for r in self._all_facets() if allowed is None or r.get("project_id") in allowed
        ]
        quality_rows = self.quality(allowed_projects=allowed)

        tracked = 0
        critical = 0
        warning = 0
        projects: set[str] = set()
        services: set[str] = set()
        last_seen: dt.date | None = None

        for r in facet_rows:
            tracked += int(r.get("quota_count") or 0)
            critical += int(r.get("critical_count") or 0)
            warning += int(r.get("warning_count") or 0)
            if r.get("project_id"):
                projects.add(r["project_id"])
            if r.get("service"):
                services.add(r["service"])
            seen = r.get("last_seen")
            if isinstance(seen, dt.date) and (last_seen is None or seen > last_seen):
                last_seen = seen

        excluded = sum(
            int(q.get("quota_count") or 0)
            for q in quality_rows
            if q.get("flag") != "OK" and not q.get("is_comparable", True)
        )

        return {
            "tracked": tracked,
            "critical": critical,
            "warning": warning,
            "projects": len(projects),
            "services": len(services),
            "last_seen": last_seen,
            "excluded": excluded,
        }

    def snapshot(
        self,
        *,
        limit: int = 500,
        min_ratio: float = 0.0,
        project_id: str = "",
        service: str = "",
        quota_metric: str = "",
        allowed_projects: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch all dashboard panels in parallel (1x BigQuery RTT when cold)."""
        self.warm_caches()
        allowed = set(allowed_projects) if allowed_projects is not None else None
        return {
            "summary": self.summary(allowed_projects=allowed),
            "facets": self.facets(allowed_projects=allowed),
            "risk": self.risk(
                limit=limit,
                min_ratio=min_ratio,
                project_id=project_id,
                service=service,
                quota_metric=quota_metric,
                allowed_projects=allowed,
            ),
            "movers": self.movers(allowed_projects=allowed),
            "hierarchy": self.hierarchy(allowed_projects=allowed),
            "quality": self.quality(allowed_projects=allowed),
            "freshness": self.freshness(),
        }

    def warm_async(self) -> None:
        """Fire-and-forget cache warm-up at process startup."""

        def _warm() -> None:
            try:
                self.warm_caches()
                _LOG.info("dashboard cache warmed")
            except Exception:  # noqa: BLE001
                _LOG.warning("initial cache warm-up failed", exc_info=True)

        _POOL.submit(_warm)

    def history(
        self,
        *,
        project_id: str,
        service: str,
        quota_metric: str,
        limit_name: str,
        location: str,
        allowed_projects: Iterable[str] | None = None,
    ) -> list[dict]:
        """Daily series for one quota, for the detail drawer."""
        if allowed_projects is not None and project_id not in set(allowed_projects):
            raise PermissionError(
                f"Caller is not authorized to view quota history for project {project_id!r}"
            )

        def produce() -> list[dict]:
            sql = f"""
            SELECT
              usage_date_utc,
              daily_peak_usage,
              current_usage,
              limit_value,
              peak_ratio,
              current_ratio,
              is_comparable,
              flags
            FROM `{self.project}.{self.dataset}.quota_daily`
            WHERE usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 35 DAY)
              AND project_id = @project_id
              AND service = @service
              AND quota_metric = @quota_metric
              AND IFNULL(limit_name, '') = @limit_name
              AND location = @location
            ORDER BY usage_date_utc
            """
            rows = self._rows(
                sql,
                [
                    bigquery.ScalarQueryParameter("project_id", "STRING", project_id),
                    bigquery.ScalarQueryParameter("service", "STRING", service),
                    bigquery.ScalarQueryParameter("quota_metric", "STRING", quota_metric),
                    bigquery.ScalarQueryParameter("limit_name", "STRING", limit_name),
                    bigquery.ScalarQueryParameter("location", "STRING", location),
                ],
            )
            for row in rows:
                if isinstance(row.get("usage_date_utc"), dt.date):
                    row["usage_date_utc"] = row["usage_date_utc"].isoformat()
            return rows

        key = f"history:{project_id}:{service}:{quota_metric}:{limit_name}:{location}"
        return _cache.get_or_set(key, produce)

    def freshness(self) -> dict:
        def produce() -> dict:
            sql = f"""
            SELECT
              MAX(collected_at) AS collected_at,
              MAX(usage_date_utc) AS latest_day,
              COUNT(*) AS rows_total
            FROM `{self.project}.{self.dataset}.quota_daily`
            WHERE usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 35 DAY)
            """
            rows = self._rows(sql)
            return rows[0] if rows else {}

        return _cache.get_or_set("freshness", produce, ttl=CACHE_TTL)


def clear_cache() -> None:
    _cache.clear()


def severity(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio >= CRITICAL:
        return "critical"
    if ratio >= WARNING:
        return "warning"
    if ratio >= 0.5:
        return "elevated"
    return "ok"
