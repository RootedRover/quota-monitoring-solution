"""Unit tests for the pure normalisation layer."""

from __future__ import annotations

import pytest

from collector.model import (
    INT64_MAX,
    DataQualityFlag,
    IntervalSource,
    LimitScope,
    QuotaClass,
)
from collector.normalise import (
    classify_limit,
    classify_quota,
    infer_interval_from_limit_name,
    infer_scope,
    interval_flags,
    is_unlimited,
    parse_refresh_interval,
    plausibility_flags,
    resolve_interval,
    scale_usage_to_interval,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("minute", 60),
        ("Minute", 60),
        ("day", 86400),
        ("second", 1),
        ("hour", 3600),
        ("10 seconds", 10),
        ("100s", 100),
        ("5h", 18000),
        ("1/1min", 60),
        ("1/min", 60),
        # Absent refreshInterval is Cloud Quotas' signal for an allocation
        # quota, not a parse failure.
        (None, None),
        ("", None),
        ("fortnight", None),
        ("0 seconds", None),
    ],
)
def test_parse_refresh_interval(raw, expected):
    assert parse_refresh_interval(raw) == expected


@pytest.mark.parametrize(
    ("limit_name", "expected"),
    [
        ("DefaultPerMinutePerProject", 60),
        ("ReadRequestsPerMinute", 60),
        ("DefaultPerDayPerProject", 86400),
        ("QueriesPerSecond", 1),
        ("RequestsPerHour", 3600),
        # Must not be shadowed by the "persecond" rule.
        ("ReadRequestsPer100Seconds", 100),
        ("ReadRequestsPer10Seconds", 10),
        ("CPUsPerProjectPerRegion", None),
        (None, None),
    ],
)
def test_infer_interval_from_limit_name(limit_name, expected):
    assert infer_interval_from_limit_name(limit_name) == expected


def test_resolve_interval_prefers_cloud_quotas():
    seconds, source = resolve_interval(
        quota_class=QuotaClass.RATE,
        refresh_interval="day",
        # A name that would heuristically resolve to 60 -- the authoritative
        # value must win.
        limit_name="SomethingPerMinutePerProject",
    )
    assert (seconds, source) == (86400, IntervalSource.CLOUD_QUOTAS)


def test_resolve_interval_falls_back_to_heuristic():
    seconds, source = resolve_interval(
        quota_class=QuotaClass.RATE,
        refresh_interval=None,
        limit_name="ReadRequestsPerMinutePerProject",
    )
    assert (seconds, source) == (60, IntervalSource.HEURISTIC)


def test_resolve_interval_unknown_is_flagged_not_guessed():
    seconds, source = resolve_interval(
        quota_class=QuotaClass.RATE, refresh_interval=None, limit_name="MysteryLimit"
    )
    assert seconds is None
    assert source is IntervalSource.UNKNOWN
    assert interval_flags(source) == [DataQualityFlag.UNKNOWN_INTERVAL]


def test_allocation_quotas_have_no_interval():
    seconds, source = resolve_interval(
        quota_class=QuotaClass.ALLOCATION,
        refresh_interval=None,
        limit_name="CPUsPerProjectPerRegion",
    )
    assert seconds is None
    assert source is IntervalSource.NOT_APPLICABLE
    assert interval_flags(source) == []


def test_classify_quota_uses_refresh_interval_presence():
    assert classify_quota("minute", False) is QuotaClass.RATE
    assert classify_quota("day", False) is QuotaClass.RATE
    assert classify_quota(None, True) is QuotaClass.ALLOCATION


class TestIsUnlimited:
    def test_exact_int64_max(self):
        assert is_unlimited(INT64_MAX)

    def test_float64_rounded_int64_max(self):
        # This is what the PromQL endpoint actually returns. v5 compared for
        # equality against INT64_MAX and therefore missed this.
        assert is_unlimited(9223372036854776000.0)

    def test_negative_one_sentinel(self):
        assert is_unlimited(-1)

    @pytest.mark.parametrize("value", [0, 1, 1200, 9e17])
    def test_real_limits(self, value):
        assert not is_unlimited(value)

    def test_none(self):
        assert not is_unlimited(None)


@pytest.mark.parametrize(
    ("dimensions", "limit_name", "expected"),
    [
        (["user"], "DefaultPerMinutePerUser", LimitScope.USER),
        ((), "DefaultPerMinutePerUser", LimitScope.USER),
        ((), "DefaultRequestsPerMinutePerUser", LimitScope.USER),
        (["region"], "A2-CPUS-per-project-region", LimitScope.REGION),
        (["zone"], "A2-CPUS-per-project-zone", LimitScope.ZONE),
        (
            ["regional_location"],
            "SearchRequestsPerMinutePerProjectPerRegion",
            LimitScope.REGION,
        ),
        ((), "DefaultPerDayPerProject", LimitScope.PROJECT),
        ((), "ServiceAccountsPerProject", LimitScope.PROJECT),
    ],
)
def test_infer_scope(dimensions, limit_name, expected):
    assert infer_scope(dimensions=dimensions, limit_name=limit_name) == expected


def test_infer_scope_honours_container_type():
    assert (
        infer_scope(
            dimensions=(),
            limit_name="GCE-FIREWALL-PROGRAMMED-SECURE-TAG-VALUES-per-organization",
            container_type="ORGANIZATION",
        )
        is LimitScope.ORGANIZATION
    )


def test_per_user_limits_are_not_valid_denominators():
    assert not LimitScope.USER.comparable_to_project_usage
    assert LimitScope.PROJECT.comparable_to_project_usage
    assert LimitScope.REGION.comparable_to_project_usage


@pytest.mark.parametrize(
    ("usage", "measured", "target", "expected"),
    [
        (10.0, 1, 60, 600.0),
        (600.0, 60, 1, 10.0),
        (100.0, 60, 86400, 144000.0),
        (100.0, 60, 60, 100.0),
    ],
)
def test_scale_usage_to_interval(usage, measured, target, expected):
    result = scale_usage_to_interval(
        usage, measured_over_seconds=measured, limit_interval_seconds=target
    )
    assert result == pytest.approx(expected)


def test_scale_usage_rejects_non_positive_intervals():
    with pytest.raises(ValueError):
        scale_usage_to_interval(1.0, measured_over_seconds=0, limit_interval_seconds=60)


class TestClassifyLimit:
    def test_healthy_limit(self):
        assert classify_limit(1200, scope=LimitScope.PROJECT) == []

    def test_missing(self):
        assert classify_limit(None, scope=LimitScope.PROJECT) == [DataQualityFlag.LIMIT_MISSING]

    def test_unlimited(self):
        assert DataQualityFlag.LIMIT_UNLIMITED in classify_limit(
            INT64_MAX, scope=LimitScope.PROJECT
        )

    def test_negative_sentinel_is_unlimited_not_negative(self):
        flags = classify_limit(-1, scope=LimitScope.PROJECT)
        assert DataQualityFlag.LIMIT_UNLIMITED in flags
        assert DataQualityFlag.LIMIT_NON_POSITIVE not in flags

    def test_zero(self):
        assert DataQualityFlag.LIMIT_NON_POSITIVE in classify_limit(0, scope=LimitScope.PROJECT)

    def test_per_user_scope_flagged(self):
        assert DataQualityFlag.LIMIT_SCOPE_NOT_COMPARABLE in classify_limit(
            1200, scope=LimitScope.USER
        )

    def test_imprecise_flagged(self):
        assert DataQualityFlag.LIMIT_NOT_PRECISE in classify_limit(
            1200, scope=LimitScope.PROJECT, is_precise=False
        )


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (None, []),
        (0.5, []),
        (1.0, []),
        (1.5, []),
        (2.0, []),
        (2.5, [DataQualityFlag.RATIO_IMPLAUSIBLE]),
        (1000.0, [DataQualityFlag.RATIO_IMPLAUSIBLE]),
    ],
)
def test_plausibility_flags(ratio, expected):
    assert plausibility_flags(ratio) == expected


def test_custom_dimension_metric_helpers():
    from collector.normalise import (
        format_custom_dimension_metric,
        format_custom_dimension_quota_id,
        split_custom_dimension_metric,
    )

    metric = format_custom_dimension_metric("compute.googleapis.com/cpus_per_vm_family", " c4 ")
    assert metric == "compute.googleapis.com/cpus_per_vm_family/C4"

    qid = format_custom_dimension_quota_id("CPUS-PER-VM-FAMILY-per-project-region", "c4")
    assert qid == "CPUS-PER-VM-FAMILY-per-project-region/C4"

    assert split_custom_dimension_metric(metric) == (
        "compute.googleapis.com/cpus_per_vm_family",
        "vm_family",
        "C4",
    )
    assert split_custom_dimension_metric(
        "compute.googleapis.com/gpus_per_gpu_family/NVIDIA_H100"
    ) == (
        "compute.googleapis.com/gpus_per_gpu_family",
        "gpu_family",
        "NVIDIA_H100",
    )
    assert split_custom_dimension_metric("compute.googleapis.com/cpus") is None
    assert split_custom_dimension_metric("compute.googleapis.com/cpus_per_vm_family") is None
