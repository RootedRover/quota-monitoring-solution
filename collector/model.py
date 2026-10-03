"""Core domain types for the quota monitoring collector.

The row grain of everything downstream is:

    (project_id, service, quota_metric, limit_name, location)

Keeping ``limit_name`` in the grain is what prevents the v5 defect where
project-wide usage was divided by an unrelated limit (e.g. a per-user limit),
producing consumption figures in the tens of thousands of percent.

Live verification against org 957650833838 on 2026-09-15 established the
following, which this module encodes:

* ``quota/rate/net_usage`` carries only ``method`` + ``quota_metric``.
* ``quota/allocation/usage`` carries only ``quota_metric``.
* Neither carries ``limit_name``, so a usage series cannot by itself say which
  limit it should be compared against. The mapping has to come from the Cloud
  Quotas API, where ``QuotaInfo.quotaId`` is exactly the monitoring
  ``limit_name``.
* One ``(quota_metric, location)`` genuinely does have several limits -- e.g.
  ``dns.googleapis.com/default`` has both ``DefaultPerDayPerProject`` and
  ``DefaultPerMinutePerUser``. Fanning usage out across all of them is correct
  only if each pairing is separately checked for comparability.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime

# Two distinct "unlimited" encodings are in circulation:
#   * INT64_MAX, written into the serviceruntime quota/limit time series and
#     returned by Cloud Quotas as the exact string "9223372036854775807".
#   * -1, the Cloud Quotas convention for "no limit configured".
# INT64_MAX is not exactly representable as a float64 -- the Monitoring PromQL
# endpoint returns it as 9223372036854776000 -- so callers must compare with a
# threshold rather than for equality.
INT64_MAX = 9223372036854775807
UNLIMITED_THRESHOLD = 9e18


class QuotaClass(enum.Enum):
    """How a quota is enforced, which determines how its ratio is computed."""

    ALLOCATION = "ALLOCATION"
    """A level (e.g. CPUs in use). Usage and limit are directly comparable."""

    RATE = "RATE"
    """Units consumed per enforcement interval. Usage must be accumulated over
    exactly that interval before being compared to the limit."""

    RATEV2 = "RATEV2"
    """Windowed rate quota. The backend already accumulates usage over the
    enforcement window and both series carry limit_name, so no normalisation
    is required. Not emitted in every project."""

    CONCURRENT = "CONCURRENT"
    """A level, like ALLOCATION, but for in-flight operations."""


class LimitScope(enum.Enum):
    """Who or what the limit applies to.

    This is the axis v5 ignored. A limit scoped to a single user is not a
    denominator for project-aggregate usage; dividing by it inflates the ratio
    by roughly the number of active users.
    """

    PROJECT = "PROJECT"
    REGION = "REGION"
    ZONE = "ZONE"
    USER = "USER"
    ORGANIZATION = "ORGANIZATION"
    FOLDER = "FOLDER"
    OTHER = "OTHER"

    @property
    def comparable_to_project_usage(self) -> bool:
        """Whether project-aggregate usage may be divided by this limit."""
        return self in {
            LimitScope.PROJECT,
            LimitScope.REGION,
            LimitScope.ZONE,
            LimitScope.ORGANIZATION,
            LimitScope.FOLDER,
        }


class IntervalSource(enum.Enum):
    """Provenance of the enforcement interval -- surfaced in the data-quality
    panel so that heuristics are never mistaken for authoritative data."""

    CLOUD_QUOTAS = "cloud_quotas"
    """QuotaInfo.refreshInterval. Authoritative."""

    HEURISTIC = "heuristic"
    """Inferred from limit_name. Best effort; may be wrong."""

    NOT_APPLICABLE = "not_applicable"
    """Allocation/concurrent quotas have no interval."""

    UNKNOWN = "unknown"
    """Could not be determined. Rows are flagged and excluded from ratios."""


class DataQualityFlag(enum.Enum):
    UNKNOWN_INTERVAL = "UNKNOWN_INTERVAL"
    HEURISTIC_INTERVAL = "HEURISTIC_INTERVAL"
    LIMIT_MISSING = "LIMIT_MISSING"
    LIMIT_NON_POSITIVE = "LIMIT_NON_POSITIVE"
    LIMIT_UNLIMITED = "LIMIT_UNLIMITED"
    LIMIT_SCOPE_NOT_COMPARABLE = "LIMIT_SCOPE_NOT_COMPARABLE"
    """Usage is aggregated at project level but the limit is per-user (or
    otherwise not a project-level denominator). v5 divided anyway."""

    LIMIT_NOT_PRECISE = "LIMIT_NOT_PRECISE"
    RATIO_IMPLAUSIBLE = "RATIO_IMPLAUSIBLE"
    """Ratio exceeded the plausibility ceiling. Almost always a collector bug,
    not a genuine quota emergency -- v5 shipped these to the dashboard as fact."""

    USAGE_WITHOUT_LIMIT = "USAGE_WITHOUT_LIMIT"
    LIMIT_DISAGREES_WITH_METRIC = "LIMIT_DISAGREES_WITH_METRIC"
    """Cloud Quotas and the quota/limit metric returned different values."""


@dataclass(frozen=True)
class QuotaKey:
    """Identity of a single quota bucket. Immutable and hashable so that
    per-series state can never be shared between buckets by accident."""

    project_id: str
    service: str
    quota_metric: str
    limit_name: str
    location: str

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.project_id,
            self.service,
            self.quota_metric,
            self.limit_name,
            self.location,
        )


@dataclass(frozen=True)
class UsageKey:
    """Identity of a usage series.

    Deliberately *not* the same type as :class:`QuotaKey`: usage has no
    ``limit_name``, and conflating the two is precisely how v5 ended up
    cross-producing usage against limits.
    """

    project_id: str
    service: str
    quota_metric: str
    location: str


@dataclass(frozen=True)
class QuotaDefinition:
    """Interval/units/scope metadata for a quota, sourced from Cloud Quotas.

    These properties belong to the quota *definition*, not to any one project:
    ``ReadRequestsPerMinutePerProject`` refreshes every minute in every
    project. That is what lets the collector cache this per service rather
    than fetching it per (project, service).

    ``quota_id`` is the Cloud Quotas ``quotaId``, verified to be identical to
    the monitoring ``limit_name``.
    """

    service: str
    quota_id: str
    quota_metric: str
    quota_class: QuotaClass
    interval_seconds: int | None
    interval_source: IntervalSource
    scope: LimitScope
    dimensions: tuple[str, ...] = ()
    is_precise: bool = True
    metric_unit: str | None = None
    display_name: str | None = None
    container_type: str = "PROJECT"
    # location -> limit value. "global" is used for non-dimensioned quotas.
    values_by_location: dict[str, int | None] = field(default_factory=dict)

    def value_for(self, location: str) -> int | None:
        if location in self.values_by_location:
            return self.values_by_location[location]
        return self.values_by_location.get("global")


@dataclass(frozen=True)
class UsageSample:
    """One observation of usage for one usage series.

    ``value`` is the number of quota units consumed over ``window_seconds``
    ending at ``observed_at``. For allocation quotas ``window_seconds`` is
    None because the value is a level rather than an accumulation.
    """

    key: UsageKey
    observed_at: datetime
    value: float
    window_seconds: int | None


@dataclass
class DailyRollup:
    """One output row: the daily peak and latest usage for one quota bucket.

    Deliberately a plain per-key record. v5's equivalent logic shared a single
    mutable accumulator across every quota in a project, so one metric's peak
    leaked onto every other metric's row.
    """

    key: QuotaKey
    usage_date_utc: date
    usage_date_local: date | None
    window_boundary: str
    quota_class: QuotaClass
    interval_seconds: int | None
    interval_source: IntervalSource
    scope: LimitScope
    current_usage: float | None
    daily_peak_usage: float | None
    limit_value: int | None
    is_unlimited: bool
    is_precise: bool
    flags: list[DataQualityFlag] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        """Whether a ratio may legitimately be computed for this row."""
        blocking = {
            DataQualityFlag.LIMIT_MISSING,
            DataQualityFlag.LIMIT_NON_POSITIVE,
            DataQualityFlag.LIMIT_UNLIMITED,
            DataQualityFlag.LIMIT_SCOPE_NOT_COMPARABLE,
            DataQualityFlag.UNKNOWN_INTERVAL,
            DataQualityFlag.USAGE_WITHOUT_LIMIT,
        }
        return not blocking.intersection(self.flags)

    @property
    def current_ratio(self) -> float | None:
        if not self.comparable:
            return None
        return _ratio(self.current_usage, self.limit_value, self.is_unlimited)

    @property
    def peak_ratio(self) -> float | None:
        if not self.comparable:
            return None
        return _ratio(self.daily_peak_usage, self.limit_value, self.is_unlimited)


def _ratio(usage: float | None, limit: int | None, is_unlimited: bool) -> float | None:
    if usage is None or limit is None or is_unlimited or limit <= 0:
        return None
    return usage / limit
