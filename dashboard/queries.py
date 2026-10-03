"""BigQuery reads for the dashboard.

Every query here hits a precomputed view, not ``quota_daily``. The one
exception is :func:`history`, which needs the raw per-day series for a single
quota and is bounded by the clustering key, so it scans very little.

Results are cached for a few minutes. The underlying data changes once a day,
so anything shorter is spending money to re-read an identical answer; anything
longer makes the "refresh now" path feel broken after a manual collect.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
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


@dataclass
class _Entry:
    value: Any
    expires_at: float


class _Cache:
    """Tiny TTL cache.

    A dict plus a lock rather than a library: there is one process, a handful
    of keys, and no eviction policy worth configuring.
    """

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: str, producer, ttl: int = CACHE_TTL):
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry and entry.expires_at > now:
                return entry.value
        # Produced outside the lock: a slow BigQuery call must not block
        # readers of unrelated keys. Two concurrent misses on the same key will
        # both query, which is acceptable and much cheaper than serialising.
        value = producer()
        with self._lock:
            self._data[key] = _Entry(value, now + ttl)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_cache = _Cache()


class Repository:
    def __init__(self, project: str = PROJECT, dataset: str = DATASET) -> None:
        if not project:
            raise ValueError("QMS_PROJECT must be set")
        # Both of these come from the environment, so they are validated before
        # they are ever spliced into SQL. BigQuery cannot bind an identifier as
        # a query parameter, which makes this the only available defence.
        self.project = validate_project(project)
        self.dataset = validate_name(dataset)
        # Pinned explicitly. Left unset, the client dispatches jobs to the
        # default multi-region and a dataset that lives anywhere else simply
        # reports as not found, which reads like a permissions problem.
        self._client = bigquery.Client(project=self.project, location=LOCATION)

    def _view(self, name: str) -> str:
        return f"`{self.project}.{self.dataset}.{validate_name(name)}`"

    def _rows(self, sql: str, params: list | None = None) -> list[dict]:
        job_config = bigquery.QueryJobConfig(query_parameters=params or [])
        return [dict(row) for row in self._client.query(sql, job_config=job_config).result()]

    # ---------------------------------------------------------------- panels

    def risk(self, *, limit: int = 200, min_ratio: float = 0.0) -> list[dict]:
        """The main table: every comparable quota, worst first."""

        def produce() -> list[dict]:
            sql = f"""
            SELECT *
            FROM {self._view("quota_risk")}
            WHERE COALESCE(peak_ratio_30d, 0) >= @min_ratio
            ORDER BY peak_ratio_30d DESC NULLS LAST
            LIMIT @limit
            """
            return self._rows(
                sql,
                [
                    bigquery.ScalarQueryParameter("min_ratio", "FLOAT64", min_ratio),
                    bigquery.ScalarQueryParameter("limit", "INT64", limit),
                ],
            )

        return _cache.get_or_set(f"risk:{limit}:{min_ratio}", produce)

    def movers(self, *, limit: int = 25) -> list[dict]:
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
              COUNT(*) AS row_count,
              COUNT(DISTINCT FORMAT('%s|%s|%s|%s',
                project_id, service, quota_metric, limit_name)) AS quota_count
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
        """Headline counters."""

        def produce() -> dict:
            sql = f"""
            SELECT
              COUNT(*) AS tracked,
              COUNTIF(peak_ratio_30d >= {CRITICAL}) AS critical,
              COUNTIF(peak_ratio_30d >= {WARNING} AND peak_ratio_30d < {CRITICAL}) AS warning,
              COUNT(DISTINCT project_id) AS projects,
              COUNT(DISTINCT service) AS services,
              MAX(last_seen) AS last_seen
            FROM {self._view("quota_risk")}
            """
            rows = self._rows(sql)
            base = rows[0] if rows else {}

            # Not-comparable rows are counted separately: they are excluded
            # from quota_risk by design, so the headline "tracked" number would
            # otherwise quietly understate what we looked at.
            excluded = self._rows(f"""
            SELECT COUNT(DISTINCT FORMAT('%s|%s|%s|%s',
              project_id, service, quota_metric, limit_name)) AS excluded
            FROM {self._view("quota_quality")}
            WHERE NOT is_comparable
            """)
            base["excluded"] = excluded[0]["excluded"] if excluded else 0
            return base

        return _cache.get_or_set("summary", produce)

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

        return _cache.get_or_set("freshness", produce, ttl=60)


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
