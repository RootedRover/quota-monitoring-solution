"""Cloud Monitoring PromQL source.

Uses the REST endpoint directly rather than a generated client, because the
query shapes matter more than the transport and they are easier to review and
golden-test as strings.

Two hard-won constraints are baked in here:

* ``monitored_resource="consumer_quota"`` is **mandatory**. Without it the API
  rejects the query outright, because ``serviceruntime.googleapis.com/quota/*``
  is emitted against both ``consumer_quota`` and ``producer_quota``.
* There is **no organization-scoped PromQL endpoint**. The v1 discovery
  document exposes ``prometheus/api/v1/*`` only beneath
  ``projects/{project}/location/{location}``. Org-wide collection is therefore
  a fan-out over projects (optionally collapsed via a Metrics Scope, which
  tops out at 375 monitored projects).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import google.auth
import google.auth.transport.requests
import requests
from requests.adapters import HTTPAdapter

from ..model import UsageKey, UsageSample

_LOG = logging.getLogger(__name__)

_BASE = "https://monitoring.googleapis.com/v1"
_CONSUMER = 'monitored_resource="consumer_quota"'
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

SERVICERUNTIME = "serviceruntime.googleapis.com"

# Daily peak of the *level* held by an allocation quota.
Q_ALLOCATION_PEAK = (
    f'max_over_time({{__name__="{SERVICERUNTIME}/quota/allocation/usage",{_CONSUMER}}}[1d])'
)

# Total rate-quota consumption within each day. This is the numerator for
# limits enforced per day.
Q_RATE_DAILY_TOTAL = (
    "sum by (project_id,service,quota_metric,location) ("
    f'increase({{__name__="{SERVICERUNTIME}/quota/rate/net_usage",{_CONSUMER}}}[1d])'
    ")"
)

# Peak one-minute consumption within each day. This is the numerator for
# limits enforced per minute.
#
# The inner window is 1m because net_usage is a DELTA sampled every 60s, so
# increase(...[1m]) recovers the raw per-minute delta. The subquery resolution
# must also be 1m: at 5m the API silently returns fewer series and drops
# short bursts entirely (measured: 98 series vs 113).
Q_RATE_MINUTE_PEAK = (
    "max_over_time("
    "sum by (project_id,service,quota_metric,location) ("
    f'increase({{__name__="{SERVICERUNTIME}/quota/rate/net_usage",{_CONSUMER}}}[1m])'
    ")[1d:1m])"
)

# The quota/limit metric, used only to cross-check Cloud Quotas. Its coverage
# is sparse (12 live series against 61 usage series in a sample project), which
# is why it is not the primary limit source.
Q_LIMIT_DAILY_MIN = (
    f'min_over_time({{__name__="{SERVICERUNTIME}/quota/limit",{_CONSUMER}}}[1d])'
)


class PromQLError(RuntimeError):
    pass


@dataclass(frozen=True)
class RangePoint:
    at: dt.datetime
    value: float


def _build_pooled_session(pool_size: int = 16) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class MonitoringSource:
    """Reads quota usage series for one scoping project."""

    def __init__(
        self,
        project_id: str,
        *,
        session: requests.Session | None = None,
        timeout: int = 120,
        max_retries: int = 3,
    ) -> None:
        self.project_id = project_id
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or _build_pooled_session()
        self._credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/monitoring.read"]
        )
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _token(self) -> str:
        with self._lock:
            if not self._credentials.valid:
                self._credentials.refresh(google.auth.transport.requests.Request())
            return self._credentials.token

    def _post(self, path: str, data: dict[str, str]) -> dict:
        url = f"{_BASE}/projects/{self.project_id}/location/global/prometheus/api/v1/{path}"
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            response = self._session.post(
                url,
                data=data,
                headers={"Authorization": f"Bearer {self._token()}"},
                timeout=self.timeout,
            )
            status = getattr(response, "status_code", 200)
            if (
                isinstance(status, int)
                and status in _RETRYABLE_STATUS_CODES
                and attempt < self.max_retries
            ):
                _LOG.debug(
                    "PromQL HTTP %d for %s (attempt %d/%d); backing off %.1fs",
                    status,
                    self.project_id,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)
                delay *= 2.0
                continue
            try:
                payload = response.json()
            except ValueError as exc:  # pragma: no cover - transport failure
                raise PromQLError(
                    f"non-JSON response ({response.status_code}) from {url}: {response.text[:300]}"
                ) from exc
            if payload.get("status") != "success":
                err_text = str(payload.get("error", payload))
                if (
                    "429" in err_text or "RESOURCE_EXHAUSTED" in err_text
                ) and attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2.0
                    continue
                raise PromQLError(f"{path} failed for {self.project_id}: {err_text}")
            return payload["data"]

        raise PromQLError(f"{path} exhausted retries for {self.project_id}")

    # -- queries -----------------------------------------------------------

    def query_range_daily(
        self,
        query: str,
        *,
        start: dt.datetime,
        end: dt.datetime,
    ) -> Iterator[tuple[dict[str, str], list[RangePoint]]]:
        """Run ``query`` at one-day resolution.

        ``start`` and ``end`` should be UTC midnight boundaries so that each
        returned point corresponds to exactly one calendar day.
        """
        data = self._post(
            "query_range",
            {
                "query": query,
                "start": _rfc3339(start),
                "end": _rfc3339(end),
                "step": "86400s",
            },
        )
        for series in data.get("result", []):
            points = [
                RangePoint(
                    at=dt.datetime.fromtimestamp(float(ts), tz=dt.UTC),
                    value=float(value),
                )
                for ts, value in series.get("values", [])
            ]
            yield series.get("metric", {}), points

    def query_instant(self, query: str, *, at: dt.datetime) -> Iterator[tuple[dict, float]]:
        data = self._post("query", {"query": query, "time": _rfc3339(at)})
        for series in data.get("result", []):
            yield series.get("metric", {}), float(series["value"][1])

    # -- typed reads -------------------------------------------------------

    def allocation_daily_peaks(
        self, *, start: dt.datetime, end: dt.datetime
    ) -> list[UsageSample]:
        return list(
            self._as_samples(
                self.query_range_daily(Q_ALLOCATION_PEAK, start=start, end=end),
                window_seconds=None,
            )
        )

    def rate_daily_totals(self, *, start: dt.datetime, end: dt.datetime) -> list[UsageSample]:
        return list(
            self._as_samples(
                self.query_range_daily(Q_RATE_DAILY_TOTAL, start=start, end=end),
                window_seconds=86400,
            )
        )

    def rate_minute_peaks(self, *, start: dt.datetime, end: dt.datetime) -> list[UsageSample]:
        return list(
            self._as_samples(
                self.query_range_daily(Q_RATE_MINUTE_PEAK, start=start, end=end),
                window_seconds=60,
            )
        )

    def _as_samples(
        self,
        series: Iterable[tuple[dict[str, str], list[RangePoint]]],
        *,
        window_seconds: int | None,
    ) -> Iterator[UsageSample]:
        for labels, points in series:
            key = _usage_key(labels, fallback_project=self.project_id)
            if key is None:
                _LOG.warning("skipping series with unusable labels: %s", labels)
                continue
            for point in points:
                yield UsageSample(
                    key=key,
                    observed_at=point.at,
                    value=point.value,
                    window_seconds=window_seconds,
                )

    def active_services(self, *, at: dt.datetime) -> set[tuple[str, str]]:
        """``(project_id, service)`` pairs with any quota activity.

        This drives the fan-out into Cloud Quotas, so that ``ListQuotaInfos``
        is called only for services actually in use.
        """
        pairs: set[tuple[str, str]] = set()
        for query in (
            f'count by (project_id,service) ({{__name__="{SERVICERUNTIME}/quota/allocation/usage",{_CONSUMER}}})',
            f'count by (project_id,service) ({{__name__="{SERVICERUNTIME}/quota/rate/net_usage",{_CONSUMER}}})',
            f'count by (project_id,service) ({{__name__="{SERVICERUNTIME}/quota/limit",{_CONSUMER}}})',
        ):
            for labels, _ in self.query_instant(query, at=at):
                project = labels.get("project_id") or self.project_id
                service = labels.get("service")
                if service:
                    pairs.add((project, service))
        return pairs


def _usage_key(labels: dict[str, str], *, fallback_project: str) -> UsageKey | None:
    quota_metric = labels.get("quota_metric")
    service = labels.get("service")
    if not quota_metric or not service:
        return None
    return UsageKey(
        project_id=labels.get("project_id") or fallback_project,
        service=service,
        quota_metric=quota_metric,
        location=labels.get("location") or "global",
    )


def _rfc3339(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
