"""BigQuery reads for the dashboard.

Every query here hits a precomputed view, not ``quota_daily``. The one
exception is :func:`Repository.history`, which needs the raw per-day series for
a single quota and is bounded by the clustering key, so it scans very little.

Performance architecture:
* **Parallel cold-start fan-out**: on a cold cache, :meth:`Repository.snapshot`
  dispatches the view queries concurrently across a thread pool rather than
  serially, cutting cold-load latency from ~7x single-query RTT to 1x RTT.
* **Zero-query summary derivation**: headline KPI counters (tracked, critical,
  warning, distinct projects, distinct services, last seen) are computed in
  memory from the cached ``quota_risk`` rows, eliminating two redundant BigQuery
  round-trips per page load.
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from google.cloud import bigquery

from collector.bqnames import validate_name, validate_project

_LOG = logging.getLogger(__name__)

PROJECT = os.environ.get("QMS_PROJECT", "")
DATASET = os.environ.get("QMS_DATASET", "quota_monitoring")
LOCATION = os.environ.get("QMS_BQ_LOCATION", "US")
CACHE_TTL = int(os.environ.get("QMS_CACHE_TTL", "300"))

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
        """Full comparable quota set (cached once; filtered in-memory)."""

        def produce() -> list[dict]:
            sql = f"""
            SELECT *
            FROM {self._view("quota_risk")}
            ORDER BY peak_ratio_30d DESC NULLS LAST, peak_ratio_7d DESC NULLS LAST
            LIMIT 2000
            """
            return self._rows(sql)

        return _cache.get_or_set("risk:all", produce)

    def risk(self, *, limit: int = 500, min_ratio: float = 0.0) -> list[dict]:
        """The main table: every comparable quota, worst first."""
        rows = self._all_risk()
        if min_ratio > 0.0:
            rows = [r for r in rows if (r.get("peak_ratio_30d") or 0.0) >= min_ratio]
        return rows[:limit]

    def movers(self, *, limit: int = 50) -> list[dict]:
        def produce() -> list[dict]:
            sql = f"""
            SELECT *
            FROM {self._view("quota_movers")}
            ORDER BY delta DESC
            LIMIT @limit
            """
            return self._rows(sql, [bigquery.ScalarQueryParameter("limit", "INT64", limit)])

        return _cache.get_or_set(f"movers:{limit}", produce)

    def hierarchy(self) -> list[dict]:
        def produce() -> list[dict]:
            sql = f"""
            SELECT *
            FROM {self._view("quota_hierarchy")}
            ORDER BY critical_30d DESC, worst_30d DESC
            """
            return self._rows(sql)

        return _cache.get_or_set("hierarchy", produce)

    def quality(self) -> list[dict]:
        """Flag counts, and how many distinct quotas each flag affects."""

        def produce() -> list[dict]:
            sql = f"""
            SELECT
              flag,
              LOGICAL_AND(is_comparable) AS is_comparable,
              COUNT(*) AS row_count,
              COUNT(DISTINCT FORMAT('%s|%s|%s|%s',
                project_id, service, quota_metric, IFNULL(limit_name, ''))) AS quota_count
            FROM {self._view("quota_quality")}
            GROUP BY flag
            ORDER BY row_count DESC
            """
            return self._rows(sql)

        return _cache.get_or_set("quality", produce)

    def quality_rows(self, flag: str, *, limit: int = 100) -> list[dict]:
        def produce() -> list[dict]:
            sql = f"""
            SELECT DISTINCT
              project_id, service, quota_metric, limit_name, location,
              quota_class, limit_scope, interval_source, limit_value,
              daily_peak_usage
            FROM {self._view("quota_quality")}
            WHERE flag = @flag
            LIMIT @limit
            """
            return self._rows(
                sql,
                [
                    bigquery.ScalarQueryParameter("flag", "STRING", flag),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )

        return _cache.get_or_set(f"quality_rows:{flag}:{limit}", produce)

    def summary(self) -> dict:
        """Headline counters derived in-memory from cached risk + quality views."""
        risk_rows = self._all_risk()
        quality_rows = self.quality()

        critical = 0
        warning = 0
        projects: set[str] = set()
        services: set[str] = set()
        last_seen: dt.date | None = None

        for r in risk_rows:
            ratio = r.get("peak_ratio_30d")
            if ratio is not None:
                if ratio >= CRITICAL:
                    critical += 1
                elif ratio >= WARNING:
                    warning += 1
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
            "tracked": len(risk_rows),
            "critical": critical,
            "warning": warning,
            "projects": len(projects),
            "services": len(services),
            "last_seen": last_seen,
            "excluded": excluded,
        }

    def snapshot(self, *, limit: int = 500, min_ratio: float = 0.0) -> dict[str, Any]:
        """Fetch all dashboard panels in parallel (1x BigQuery RTT when cold)."""
        fut_risk = _POOL.submit(self._all_risk)
        fut_movers = _POOL.submit(self.movers)
        fut_hierarchy = _POOL.submit(self.hierarchy)
        fut_quality = _POOL.submit(self.quality)
        fut_freshness = _POOL.submit(self.freshness)

        # Wait for the core datasets in parallel.
        fut_risk.result()
        fut_quality.result()

        return {
            "summary": self.summary(),
            "risk": self.risk(limit=limit, min_ratio=min_ratio),
            "movers": fut_movers.result(),
            "hierarchy": fut_hierarchy.result(),
            "quality": fut_quality.result(),
            "freshness": fut_freshness.result(),
        }

    def warm_async(self) -> None:
        """Fire-and-forget cache warm-up at process startup."""

        def _warm() -> None:
            try:
                self.snapshot()
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
    ) -> list[dict]:
        """Daily series for one quota, for the detail drawer."""

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
            WHERE project_id = @project_id
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
