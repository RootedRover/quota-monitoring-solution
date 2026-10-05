"""Pure normalisation helpers. No cloud dependencies, so these are the part of
the collector that can be exhaustively unit-tested.

Everything here exists to answer one of three questions:

1. Over what interval is this limit expressed?  (``resolve_interval``)
2. Is this limit a real number at all?           (``classify_limit``)
3. Is this limit a legitimate denominator for the usage we measured?
   (``infer_scope``)

v5 answered (1) with a four-entry regex denylist, never asked (2) beyond an
exact-equality check against INT64_MAX, and never asked (3) at all.
"""

from __future__ import annotations

import re

from .model import (
    UNLIMITED_THRESHOLD,
    DataQualityFlag,
    IntervalSource,
    LimitScope,
    QuotaClass,
)

# Ratios above this are treated as evidence of a collector bug rather than a
# genuine quota emergency. A real quota cannot be meaningfully exceeded by
# 100x; v5 published such rows as fact.
PLAUSIBILITY_CEILING = 2.0

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}

# Cloud Quotas returns refreshInterval as a bare unit ("minute", "day") or as a
# count+unit ("10 seconds", "100s").
_INTERVAL_RE = re.compile(r"^\s*(?:(\d+)\s*)?([a-z]+)\s*$", re.IGNORECASE)

# Ordered longest-first so that "per100seconds" is not shadowed by "persecond".
_LIMIT_NAME_HEURISTICS: tuple[tuple[re.Pattern[str], int | None], ...] = (
    (re.compile(r"per(\d+)seconds?", re.IGNORECASE), None),  # count captured
    (re.compile(r"perhalfminute", re.IGNORECASE), 30),
    (re.compile(r"perminute", re.IGNORECASE), 60),
    (re.compile(r"perhour", re.IGNORECASE), 3600),
    (re.compile(r"perday", re.IGNORECASE), 86400),
    (re.compile(r"persecond", re.IGNORECASE), 1),
    (re.compile(r"perweek", re.IGNORECASE), 604800),
    (re.compile(r"permonth", re.IGNORECASE), 2592000),
)


def parse_refresh_interval(raw: str | None) -> int | None:
    """Parse a Cloud Quotas ``QuotaInfo.refreshInterval`` into seconds.

    Returns ``None`` when the field is absent, which Cloud Quotas uses to mean
    "this is an allocation quota, there is no refresh interval" -- a cleaner
    signal than any name-based guess.

    >>> parse_refresh_interval("minute")
    60
    >>> parse_refresh_interval("10 seconds")
    10
    >>> parse_refresh_interval(None) is None
    True
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    # Forms like "1/1min" or "1/min" appear in some service configs.
    if "/" in text:
        text = text.rsplit("/", 1)[-1].strip()
    match = _INTERVAL_RE.match(text)
    if not match:
        return None
    count_raw, unit = match.groups()
    seconds = _UNIT_SECONDS.get(unit)
    if seconds is None:
        return None
    count = int(count_raw) if count_raw else 1
    if count <= 0:
        return None
    return count * seconds


def infer_interval_from_limit_name(limit_name: str | None) -> int | None:
    """Best-effort interval from a limit name. Only used when Cloud Quotas has
    no entry for the quota; always recorded as ``IntervalSource.HEURISTIC``."""
    if not limit_name:
        return None
    collapsed = limit_name.replace("_", "").replace("-", "")
    for pattern, seconds in _LIMIT_NAME_HEURISTICS:
        match = pattern.search(collapsed)
        if not match:
            continue
        if seconds is None:
            count = int(match.group(1))
            return count if count > 0 else None
        return seconds
    return None


def resolve_interval(
    *,
    quota_class: QuotaClass,
    refresh_interval: str | None,
    limit_name: str | None,
) -> tuple[int | None, IntervalSource]:
    """Determine the enforcement interval and record where it came from."""
    if quota_class in (QuotaClass.ALLOCATION, QuotaClass.CONCURRENT):
        return None, IntervalSource.NOT_APPLICABLE

    authoritative = parse_refresh_interval(refresh_interval)
    if authoritative is not None:
        return authoritative, IntervalSource.CLOUD_QUOTAS

    guessed = infer_interval_from_limit_name(limit_name)
    if guessed is not None:
        return guessed, IntervalSource.HEURISTIC

    return None, IntervalSource.UNKNOWN


def classify_quota(refresh_interval: str | None, is_precise: bool) -> QuotaClass:
    """Cloud Quotas distinguishes allocation from rate quotas by the presence
    of ``refreshInterval``. Verified across compute, dns, monitoring and
    discoveryengine: allocation quotas omit it and set ``isPrecise: true``."""
    if parse_refresh_interval(refresh_interval) is not None:
        return QuotaClass.RATE
    return QuotaClass.ALLOCATION


def is_unlimited(limit: float | None) -> bool:
    """Both circulating sentinels, tolerant of float64 rounding.

    The Monitoring PromQL endpoint returns INT64_MAX as 9223372036854776000,
    so an equality test against 9223372036854775807 silently fails -- which is
    how v5 divided by INT64_MAX and produced ratios of ~0.

    >>> is_unlimited(9223372036854775807)
    True
    >>> is_unlimited(9223372036854776000.0)
    True
    >>> is_unlimited(-1)
    True
    >>> is_unlimited(1200)
    False
    """
    if limit is None:
        return False
    return limit < 0 or float(limit) >= UNLIMITED_THRESHOLD


_USER_TOKENS = ("peruser", "per_user")


def infer_scope(
    *,
    dimensions: tuple[str, ...] | list[str] | None,
    limit_name: str | None,
    container_type: str = "PROJECT",
) -> LimitScope:
    """Determine what the limit is scoped to.

    ``dimensions`` from Cloud Quotas is authoritative where present; the limit
    name is the fallback. The case that matters most is ``user``: project-wide
    usage divided by a per-user limit is not a percentage of anything.
    """
    dims = {d.lower() for d in (dimensions or ())}
    if "user" in dims:
        return LimitScope.USER
    if "zone" in dims:
        return LimitScope.ZONE
    if "region" in dims or "regional_location" in dims:
        return LimitScope.REGION

    name = (limit_name or "").lower().replace("_", "").replace("-", "")
    if any(token in name for token in _USER_TOKENS):
        return LimitScope.USER

    container = (container_type or "").upper()
    if container == "ORGANIZATION":
        return LimitScope.ORGANIZATION
    if container == "FOLDER":
        return LimitScope.FOLDER

    if "perzone" in name:
        return LimitScope.ZONE
    if "perregion" in name:
        return LimitScope.REGION
    if "perproject" in name:
        return LimitScope.PROJECT

    if dims:
        return LimitScope.OTHER
    return LimitScope.PROJECT


def scale_usage_to_interval(
    usage: float,
    *,
    measured_over_seconds: int,
    limit_interval_seconds: int,
) -> float:
    """Rescale accumulated usage onto the limit's enforcement interval.

    Only valid for converting between windows over which the rate is assumed
    uniform; the collector avoids it wherever it can instead query the correct
    window directly.
    """
    if measured_over_seconds <= 0 or limit_interval_seconds <= 0:
        raise ValueError("intervals must be positive")
    return usage * (limit_interval_seconds / measured_over_seconds)


def classify_limit(
    limit: float | None,
    *,
    scope: LimitScope,
    is_precise: bool = True,
) -> list[DataQualityFlag]:
    """Flags describing why (or whether) a limit can serve as a denominator."""
    flags: list[DataQualityFlag] = []
    if limit is None:
        flags.append(DataQualityFlag.LIMIT_MISSING)
        return flags
    if is_unlimited(limit):
        flags.append(DataQualityFlag.LIMIT_UNLIMITED)
    elif limit <= 0:
        flags.append(DataQualityFlag.LIMIT_NON_POSITIVE)
    if not scope.comparable_to_project_usage:
        flags.append(DataQualityFlag.LIMIT_SCOPE_NOT_COMPARABLE)
    if not is_precise:
        flags.append(DataQualityFlag.LIMIT_NOT_PRECISE)
    return flags


def interval_flags(source: IntervalSource) -> list[DataQualityFlag]:
    if source is IntervalSource.HEURISTIC:
        return [DataQualityFlag.HEURISTIC_INTERVAL]
    if source is IntervalSource.UNKNOWN:
        return [DataQualityFlag.UNKNOWN_INTERVAL]
    return []


def plausibility_flags(ratio: float | None) -> list[DataQualityFlag]:
    if ratio is not None and ratio > PLAUSIBILITY_CEILING:
        return [DataQualityFlag.RATIO_IMPLAUSIBLE]
    return []


# Compute Engine SuperQuota custom-dimension family metrics.
# Unlike standard serviceruntime.googleapis.com/quota/allocation/usage metrics on
# monitored_resource="consumer_quota", Compute Engine emits these on
# compute.googleapis.com/quota/<suffix>/{usage,limit} with
# monitored_resource="compute.googleapis.com/Location" and a family dimension
# label (vm_family, gpu_family, or tpu_family).
CUSTOM_DIMENSION_QUOTA_METRICS: dict[str, str] = {
    "compute.googleapis.com/cpus_per_vm_family": "vm_family",
    "compute.googleapis.com/gpus_per_gpu_family": "gpu_family",
    "compute.googleapis.com/local_ssd_total_storage_per_vm_family": "vm_family",
    "compute.googleapis.com/tpus_per_tpu_family": "tpu_family",
}

CUSTOM_DIMENSION_LABELS: tuple[str, ...] = ("vm_family", "gpu_family", "tpu_family")


def format_custom_dimension_metric(base_metric: str, family_value: str) -> str:
    """Append the normalized hardware family dimension to a custom-dimension metric."""
    clean_family = (family_value or "").strip().upper()
    if not clean_family:
        return base_metric
    return f"{base_metric}/{clean_family}"


def format_custom_dimension_quota_id(base_quota_id: str, family_value: str) -> str:
    """Append the normalized hardware family dimension to a Cloud Quotas quotaId."""
    clean_family = (family_value or "").strip().upper()
    if not clean_family or not base_quota_id:
        return base_quota_id
    return f"{base_quota_id}/{clean_family}"


def split_custom_dimension_metric(quota_metric: str) -> tuple[str, str, str] | None:
    """If ``quota_metric`` is a custom-dimension family metric (e.g.
    ``compute.googleapis.com/cpus_per_vm_family/C4``), return
    ``(base_metric, dimension_label, family_value)``. Otherwise return ``None``.
    """
    if not quota_metric:
        return None
    for base_metric, dim_label in CUSTOM_DIMENSION_QUOTA_METRICS.items():
        prefix = f"{base_metric}/"
        if quota_metric.startswith(prefix):
            family = quota_metric[len(prefix) :].strip().upper()
            if family and "/" not in family:
                return base_metric, dim_label, family
    return None
