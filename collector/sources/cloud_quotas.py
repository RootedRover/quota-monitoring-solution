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
from collections.abc import Iterator

import google.auth
import google.auth.transport.requests
import requests

from ..model import QuotaDefinition
from ..normalise import classify_quota, infer_scope, parse_refresh_interval

_LOG = logging.getLogger(__name__)

_BASE = "https://cloudquotas.googleapis.com/v1"
_PAGE_SIZE = 200


class CloudQuotasError(RuntimeError):
    pass


class CloudQuotasSource:
    """Fetches quota definitions, cached per (container, service).

    The cache key deliberately includes the container because limit *values*
    are per-container, but note that the interval/dimensions/class metadata is
    a property of the quota definition itself and is identical everywhere.
    """

    def __init__(
        self,
        *,
        billing_project: str,
        session: requests.Session | None = None,
        timeout: int = 60,
    ) -> None:
        self.billing_project = billing_project
        self.timeout = timeout
        self._session = session or requests.Session()
        self._credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        self._cache: dict[tuple[str, str], list[QuotaDefinition]] = {}

    def _token(self) -> str:
        if not self._credentials.valid:
            self._credentials.refresh(google.auth.transport.requests.Request())
        return self._credentials.token

    def _get(self, url: str, params: dict[str, str]) -> dict:
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
        payload = response.json()
        if "error" in payload:
            raise CloudQuotasError(payload["error"].get("message", str(payload["error"])))
        return payload

    def list_quota_infos(self, container: str, service: str) -> list[QuotaDefinition]:
        """``container`` is e.g. ``projects/my-proj`` or ``organizations/123``."""
        cache_key = (container, service)
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = f"{_BASE}/{container}/locations/global/services/{service}/quotaInfos"
        definitions: list[QuotaDefinition] = []
        page_token = ""
        try:
            while True:
                params = {"pageSize": str(_PAGE_SIZE)}
                if page_token:
                    params["pageToken"] = page_token
                payload = self._get(url, params)
                for raw in payload.get("quotaInfos", []):
                    definitions.append(_to_definition(raw, service))
                page_token = payload.get("nextPageToken", "")
                if not page_token:
                    break
        except CloudQuotasError as exc:
            # A service with no quota surface, or one the caller cannot read,
            # must not abort the whole collection run.
            _LOG.warning("ListQuotaInfos failed for %s/%s: %s", container, service, exc)
            definitions = []

        self._cache[cache_key] = definitions
        return definitions

    def definitions_by_quota_id(
        self, container: str, service: str
    ) -> dict[str, QuotaDefinition]:
        return {d.quota_id: d for d in self.list_quota_infos(container, service)}

    def iter_all(
        self, container: str, services: list[str]
    ) -> Iterator[QuotaDefinition]:
        for service in services:
            yield from self.list_quota_infos(container, service)


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
