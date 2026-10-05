"""Cloud Quotas API source -- the authoritative limit source.

Why this and not the ``serviceruntime.googleapis.com/quota/limit`` metric:

* Coverage. In a sample project the metric exposed 12 limit series against 61
  live usage series. Cloud Quotas returned the full catalogue for every
  service queried.
* Precision. The metric goes through float64 and returns INT64_MAX as
  ``9223372036854776000``; Cloud Quotas returns the exact string
  ``"9223372036854775807"``.
* Semantics. Only Cloud Quotas carries ``refreshInterval`` (the enforcement
  interval) and ``dimensions`` (what the limit is scoped to). Without those
  two fields a correct percentage cannot be computed at all.

``QuotaInfo.quotaId`` was verified to be identical to the monitoring
``limit_name`` label, which is what makes the join possible.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator

import google.auth
import google.auth.transport.requests
import requests
from requests.adapters import HTTPAdapter

from ..model import QuotaDefinition
from ..normalise import (
    CUSTOM_DIMENSION_LABELS,
    CUSTOM_DIMENSION_QUOTA_METRICS,
    classify_quota,
    format_custom_dimension_metric,
    format_custom_dimension_quota_id,
    infer_scope,
    parse_refresh_interval,
)

_LOG = logging.getLogger(__name__)

_BASE = "https://cloudquotas.googleapis.com/v1"
# QuotaAdjusterSettings lives on v1beta; QuotaInfo lives on v1.
_BASE_BETA = "https://cloudquotas.googleapis.com/v1beta"
_PAGE_SIZE = 200
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# Cloud Quotas enforces ReadRequestsPerMinute = 1,200/min (20 RPS) per billing
# project. Defaulting to 14 RPS (840 RPM) keeps parallel collection comfortably
# below the quota ceiling while still achieving ~8x speedup over serial loops.
DEFAULT_MAX_RPS = float(os.environ.get("QMS_CLOUD_QUOTAS_MAX_RPS", "14.0"))


class CloudQuotasError(RuntimeError):
    pass


class CloudQuotasTransientError(CloudQuotasError):
    """Raised when a retryable rate-limit (429) or 5xx error persists after backoff."""


def _build_pooled_session(pool_size: int = 16) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class CloudQuotasSource:
    """Fetches quota definitions, cached per (container, service).

    The cache key deliberately includes the container because limit *values*
    are per-container, but note that the interval/dimensions/class metadata is
    a property of the quota definition itself and is identical everywhere.
    Thread-safe and rate-limited so concurrent workers never exceed the host
    project's ``cloudquotas.googleapis.com/read_requests`` quota (1,200 RPM).
    """

    def __init__(
        self,
        *,
        billing_project: str,
        session: requests.Session | None = None,
        timeout: int = 60,
        max_rps: float | None = None,
        max_retries: int = 4,
    ) -> None:
        self.billing_project = billing_project
        self.timeout = timeout
        self.max_retries = max_retries
        resolved_rps = DEFAULT_MAX_RPS if max_rps is None else max_rps
        self._min_interval = (1.0 / resolved_rps) if resolved_rps > 0 else 0.0
        self._next_allowed_at = 0.0
        self._session = session or _build_pooled_session()
        self._credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        self._cache: dict[tuple[str, str], list[QuotaDefinition]] = {}
        self._adjuster_cache: dict[str, bool | None] = {}
        self._lock = threading.Lock()

    def _token(self) -> str:
        with self._lock:
            if not self._credentials.valid:
                self._credentials.refresh(google.auth.transport.requests.Request())
            return self._credentials.token

    def _throttle(self) -> None:
        if self._min_interval <= 0.0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_at - now
            if wait > 0:
                self._next_allowed_at += self._min_interval
            else:
                wait = 0.0
                self._next_allowed_at = now + self._min_interval
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, params: dict[str, str]) -> dict:
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            self._throttle()
            response = self._session.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    # Required when running on user ADC, otherwise the call is
                    # rejected for having no billing project attached.
                    "x-goog-user-project": self.billing_project,
                },
                timeout=self.timeout,
            )
            status = getattr(response, "status_code", 200)
            if isinstance(status, int) and status in _RETRYABLE_STATUS_CODES:
                if attempt < self.max_retries:
                    _LOG.debug(
                        "Cloud Quotas HTTP %d on %s (attempt %d/%d); backing off %.1fs",
                        status,
                        url,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= 2.0
                    continue
                raise CloudQuotasTransientError(
                    f"HTTP {status} from {url} after {self.max_retries} retries"
                )

            payload = response.json()
            if "error" in payload:
                err = payload["error"]
                code = err.get("code") if isinstance(err, dict) else None
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if code in _RETRYABLE_STATUS_CODES or "RESOURCE_EXHAUSTED" in str(err):
                    if attempt < self.max_retries:
                        time.sleep(delay)
                        delay *= 2.0
                        continue
                    raise CloudQuotasTransientError(msg)
                raise CloudQuotasError(msg)
            return payload

        raise CloudQuotasTransientError(f"Exhausted retries for {url}")

    def list_quota_infos(self, container: str, service: str) -> list[QuotaDefinition]:
        """``container`` is e.g. ``projects/my-proj`` or ``organizations/123``."""
        cache_key = (container, service)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        url = f"{_BASE}/{container}/locations/global/services/{service}/quotaInfos"
        definitions: list[QuotaDefinition] = []
        page_token = ""
        transient_failure = False
        try:
            while True:
                params = {"pageSize": str(_PAGE_SIZE)}
                if page_token:
                    params["pageToken"] = page_token
                payload = self._get(url, params)
                for raw in payload.get("quotaInfos", []):
                    definitions.extend(_to_definitions(raw, service))
                page_token = payload.get("nextPageToken", "")
                if not page_token:
                    break
        except CloudQuotasTransientError as exc:
            # Do not poison the cache with [] on transient 429/5xx exhaustion.
            transient_failure = True
            _LOG.warning(
                "Transient Cloud Quotas failure for %s/%s: %s", container, service, exc
            )
            definitions = []
        except CloudQuotasError as exc:
            # A service with no quota surface, or one the caller cannot read,
            # must not abort the whole collection run.
            _LOG.warning("ListQuotaInfos failed for %s/%s: %s", container, service, exc)
            definitions = []

        if not transient_failure:
            with self._lock:
                self._cache[cache_key] = definitions
        return definitions

    def definitions_by_quota_id(
        self, container: str, service: str
    ) -> dict[str, QuotaDefinition]:
        return {d.quota_id: d for d in self.list_quota_infos(container, service)}

    def iter_all(self, container: str, services: list[str]) -> Iterator[QuotaDefinition]:
        for service in services:
            yield from self.list_quota_infos(container, service)

    def get_quota_adjuster_enabled(self, container: str) -> bool | None:
        """Read-only check of QuotaAdjusterSettings for ``container`` (e.g. ``projects/my-proj``).

        Returns ``True`` if ``enablement == "ENABLED"``, ``False`` if
        ``"DISABLED"``, or ``None`` if unknown / unreachable.
        """
        with self._lock:
            if container in self._adjuster_cache:
                return self._adjuster_cache[container]

        url = f"{_BASE_BETA}/{container}/locations/global/quotaAdjusterSettings"
        result: bool | None = None
        transient_failure = False
        try:
            payload = self._get(url, {})
            enablement = str(payload.get("enablement", "")).upper()
            if enablement == "ENABLED":
                result = True
            elif enablement == "DISABLED":
                result = False
        except CloudQuotasTransientError as exc:
            transient_failure = True
            _LOG.warning(
                "Transient GetQuotaAdjusterSettings failure for %s: %s", container, exc
            )
            result = None
        except (CloudQuotasError, requests.RequestException) as exc:
            _LOG.warning("GetQuotaAdjusterSettings failed for %s: %s", container, exc)
            result = None

        if not transient_failure:
            with self._lock:
                self._adjuster_cache[container] = result
        return result


def _detect_family_dimension(raw: dict) -> str | None:
    metric = str(raw.get("metric") or "")
    if metric in CUSTOM_DIMENSION_QUOTA_METRICS:
        return CUSTOM_DIMENSION_QUOTA_METRICS[metric]
    dims = raw.get("dimensions") or ()
    for label in CUSTOM_DIMENSION_LABELS:
        if label in dims:
            return label
    for info in raw.get("dimensionsInfos", []):
        info_dims = info.get("dimensions") or {}
        for label in CUSTOM_DIMENSION_LABELS:
            if label in info_dims:
                return label
    return None


def _to_definitions(raw: dict, service: str) -> list[QuotaDefinition]:
    """Convert a raw ``QuotaInfo`` payload into one or more ``QuotaDefinition``s.

    For standard quotas, returns a single-element list ``[_to_definition(raw, service)]``.
    For Compute Engine custom-dimension family quotas (``vm_family``, ``gpu_family``,
    ``tpu_family``), returns the base fallback definition plus one specialized
    ``QuotaDefinition`` per hardware family present in ``dimensionsInfos``.
    """
    base = _to_definition(raw, service)
    family_dim = _detect_family_dimension(raw)
    if not family_dim:
        return [base]

    by_family, default_values = _values_by_family_and_location(raw, family_dim)
    if default_values:
        base = QuotaDefinition(
            service=base.service,
            quota_id=base.quota_id,
            quota_metric=base.quota_metric,
            quota_class=base.quota_class,
            interval_seconds=base.interval_seconds,
            interval_source=base.interval_source,
            scope=base.scope,
            dimensions=base.dimensions,
            is_precise=base.is_precise,
            metric_unit=base.metric_unit,
            display_name=base.display_name,
            container_type=base.container_type,
            values_by_location=default_values,
        )

    results: list[QuotaDefinition] = [base]
    base_display = base.display_name or base.quota_id
    for family, family_values in sorted(by_family.items()):
        results.append(
            QuotaDefinition(
                service=base.service,
                quota_id=format_custom_dimension_quota_id(base.quota_id, family),
                quota_metric=format_custom_dimension_metric(base.quota_metric, family),
                quota_class=base.quota_class,
                interval_seconds=base.interval_seconds,
                interval_source=base.interval_source,
                scope=base.scope,
                dimensions=base.dimensions,
                is_precise=base.is_precise,
                metric_unit=base.metric_unit,
                display_name=f"{base_display} ({family})" if base_display else family,
                container_type=base.container_type,
                values_by_location=family_values,
            )
        )
    return results


def _to_definition(raw: dict, service: str) -> QuotaDefinition:
    refresh_interval = raw.get("refreshInterval")
    dimensions = tuple(raw.get("dimensions") or ())
    container_type = raw.get("containerType", "PROJECT")
    quota_id = raw.get("quotaId", "")
    is_precise = bool(raw.get("isPrecise", False))

    quota_class = classify_quota(refresh_interval, is_precise)
    interval_seconds = parse_refresh_interval(refresh_interval)
    scope = infer_scope(
        dimensions=dimensions,
        limit_name=quota_id,
        container_type=container_type,
    )

    from ..model import IntervalSource  # local import avoids a cycle at module load

    if quota_class.name == "ALLOCATION":
        interval_source = IntervalSource.NOT_APPLICABLE
    elif interval_seconds is not None:
        interval_source = IntervalSource.CLOUD_QUOTAS
    else:
        interval_source = IntervalSource.UNKNOWN

    return QuotaDefinition(
        service=raw.get("service", service),
        quota_id=quota_id,
        quota_metric=raw.get("metric", ""),
        quota_class=quota_class,
        interval_seconds=interval_seconds,
        interval_source=interval_source,
        scope=scope,
        dimensions=dimensions,
        is_precise=is_precise,
        metric_unit=raw.get("metricUnit"),
        display_name=raw.get("quotaDisplayName"),
        container_type=container_type,
        values_by_location=_values_by_location(raw),
    )


def _extract_locations(info: dict, dims: dict) -> list[str]:
    applicable = info.get("applicableLocations")
    if applicable:
        return [str(loc) for loc in applicable if loc]
    for loc_key in ("region", "zone", "location"):
        if dims.get(loc_key):
            return [str(dims[loc_key])]
    return ["global"]


def _values_by_family_and_location(
    raw: dict, family_dim: str
) -> tuple[dict[str, dict[str, int | None]], dict[str, int | None]]:
    """Resolve per-family and generic location limits using 4-tier specificity.

    Specificity precedence (highest to lowest):
    4: ``(location + family)`` override
    3: ``(family-only)`` global default across ``applicableLocations``
    2: ``(location-only)`` regional/zonal default (no family dimension)
    1: ``(empty dimensions)`` global fallback default
    """
    infos = raw.get("dimensionsInfos") or []
    families: set[str] = set()
    for info in infos:
        dims = info.get("dimensions") or {}
        fam = str(dims.get(family_dim) or "").strip().upper()
        if fam:
            families.add(fam)

    default_values: dict[str, int | None] = {}
    default_ranks: dict[str, int] = {}
    for info in infos:
        dims = info.get("dimensions") or {}
        fam = str(dims.get(family_dim) or "").strip().upper()
        if fam:
            continue
        has_loc = bool(dims.get("region") or dims.get("zone") or dims.get("location"))
        rank = 2 if has_loc else 1
        value = _parse_value(info.get("details", {}).get("value"))
        for loc in _extract_locations(info, dims):
            if rank > default_ranks.get(loc, 0):
                default_ranks[loc] = rank
                default_values[loc] = value

    by_family: dict[str, dict[str, int | None]] = {}
    for family in sorted(families):
        loc_values: dict[str, int | None] = dict(default_values)
        loc_ranks: dict[str, int] = dict(default_ranks)
        for info in infos:
            dims = info.get("dimensions") or {}
            fam = str(dims.get(family_dim) or "").strip().upper()
            if fam != family:
                continue
            has_loc = bool(dims.get("region") or dims.get("zone") or dims.get("location"))
            rank = 4 if has_loc else 3
            value = _parse_value(info.get("details", {}).get("value"))
            for loc in _extract_locations(info, dims):
                if rank > loc_ranks.get(loc, 0):
                    loc_ranks[loc] = rank
                    loc_values[loc] = value
        by_family[family] = loc_values

    return by_family, default_values


def _values_by_location(raw: dict) -> dict[str, int | None]:
    """Flatten ``dimensionsInfos`` into ``location -> limit value``.

    Values arrive as decimal strings so that INT64_MAX survives intact; they
    are parsed as Python ints, which are arbitrary-precision.
    """
    values: dict[str, int | None] = {}
    for info in raw.get("dimensionsInfos", []):
        value = _parse_value(info.get("details", {}).get("value"))
        locations = info.get("applicableLocations") or ["global"]
        for location in locations:
            # First writer wins: dimensionsInfos is ordered most-specific-first.
            values.setdefault(location, value)
    return values


def _parse_value(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        _LOG.warning("unparseable quota value %r", raw)
        return None
