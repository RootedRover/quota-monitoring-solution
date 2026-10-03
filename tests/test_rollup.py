"""Tests for the usage/limit join.

The first test class is the regression guard for the defect that motivated
this rewrite: in v5, one quota metric's peak could appear on every other quota
metric's row in the same project.
"""

from __future__ import annotations

import datetime as dt
import typing

import pytest

from collector.model import (
    INT64_MAX,
    DataQualityFlag,
    IntervalSource,
    LimitScope,
    QuotaClass,
    QuotaDefinition,
    UsageKey,
    UsageSample,
)
from collector.rollup import UsageBundle, build_rollups

PROJECT = "proj-a"
DAY = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def allocation_def(
    quota_id, metric, *, value, service="svc.googleapis.com", scope=LimitScope.PROJECT
):
    return QuotaDefinition(
        service=service,
        quota_id=quota_id,
        quota_metric=metric,
        quota_class=QuotaClass.ALLOCATION,
        interval_seconds=None,
        interval_source=IntervalSource.NOT_APPLICABLE,
        scope=scope,
        values_by_location={"global": value},
    )


def rate_def(
    quota_id,
    metric,
    *,
    value,
    interval,
    service="svc.googleapis.com",
    scope=LimitScope.PROJECT,
):
    return QuotaDefinition(
        service=service,
        quota_id=quota_id,
        quota_metric=metric,
        quota_class=QuotaClass.RATE,
        interval_seconds=interval,
        interval_source=IntervalSource.CLOUD_QUOTAS,
        scope=scope,
        values_by_location={"global": value},
    )


def usage(metric, value, *, at=DAY, service="svc.googleapis.com", location="global"):
    return UsageSample(
        key=UsageKey(
            project_id=PROJECT, service=service, quota_metric=metric, location=location
        ),
        observed_at=at,
        value=value,
        window_seconds=None,
    )


def empty_bundle(**kwargs):
    return UsageBundle(
        allocation_peaks=kwargs.get("allocation_peaks", []),
        rate_daily_totals=kwargs.get("rate_daily_totals", []),
        rate_minute_peaks=kwargs.get("rate_minute_peaks", []),
    )


class TestNoCrossMetricLeakage:
    """Regression guard for the v5 shared-accumulator defect.

    v5 allocated its running-max map outside the per-time-series loop, so a
    single large metric contaminated every other metric in the project. The
    signature was several unrelated rows sharing one implausibly large peak.
    """

    def test_large_metric_does_not_contaminate_small_ones(self):
        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                allocation_def("BigPerProject", "svc.googleapis.com/big", value=10_000_000),
                allocation_def("SmallPerProject", "svc.googleapis.com/small", value=100),
                allocation_def("TinyPerProject", "svc.googleapis.com/tiny", value=10),
            ]
        }
        bundle = empty_bundle(
            allocation_peaks=[
                usage("svc.googleapis.com/big", 9_000_000.0),
                usage("svc.googleapis.com/small", 3.0),
                usage("svc.googleapis.com/tiny", 1.0),
            ]
        )

        rows = {r.key.quota_metric: r for r in build_rollups(bundle, definitions)}

        assert rows["svc.googleapis.com/big"].daily_peak_usage == 9_000_000.0
        assert rows["svc.googleapis.com/small"].daily_peak_usage == 3.0
        assert rows["svc.googleapis.com/tiny"].daily_peak_usage == 1.0
        # And crucially the ratios stay sane.
        assert rows["svc.googleapis.com/small"].peak_ratio == pytest.approx(0.03)
        assert rows["svc.googleapis.com/tiny"].peak_ratio == pytest.approx(0.1)

    def test_peaks_are_per_day_not_cumulative(self):
        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                allocation_def("XPerProject", "svc.googleapis.com/x", value=100)
            ]
        }
        day1 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
        day2 = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
        bundle = empty_bundle(
            allocation_peaks=[
                usage("svc.googleapis.com/x", 90.0, at=day1),
                usage("svc.googleapis.com/x", 5.0, at=day2),
            ]
        )

        rows = {r.usage_date_utc: r for r in build_rollups(bundle, definitions)}

        assert rows[day1.date()].daily_peak_usage == 90.0
        # The quiet day must not inherit the busy day's peak.
        assert rows[day2.date()].daily_peak_usage == 5.0

    def test_locations_are_not_merged(self):
        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                allocation_def("XPerRegion", "svc.googleapis.com/x", value=100)
            ]
        }
        bundle = empty_bundle(
            allocation_peaks=[
                usage("svc.googleapis.com/x", 90.0, location="us-central1"),
                usage("svc.googleapis.com/x", 2.0, location="us-west1"),
            ]
        )

        rows = {r.key.location: r for r in build_rollups(bundle, definitions)}

        assert rows["us-central1"].daily_peak_usage == 90.0
        assert rows["us-west1"].daily_peak_usage == 2.0


class TestMultiLimitFanOut:
    """A quota metric with several limits, as seen live with
    ``dns.googleapis.com/default``."""

    definitions: typing.ClassVar[dict] = {
        (PROJECT, "dns.googleapis.com"): [
            rate_def(
                "DefaultPerDayPerProject",
                "dns.googleapis.com/default",
                value=INT64_MAX,
                interval=86400,
                service="dns.googleapis.com",
            ),
            rate_def(
                "DefaultPerMinutePerUser",
                "dns.googleapis.com/default",
                value=1200,
                interval=60,
                service="dns.googleapis.com",
                scope=LimitScope.USER,
            ),
        ]
    }

    def _rows(self):
        bundle = empty_bundle(
            rate_daily_totals=[
                usage("dns.googleapis.com/default", 5000.0, service="dns.googleapis.com")
            ],
            rate_minute_peaks=[
                usage("dns.googleapis.com/default", 40.0, service="dns.googleapis.com")
            ],
        )
        return {r.key.limit_name: r for r in build_rollups(bundle, self.definitions)}

    def test_both_limits_produce_rows(self):
        assert set(self._rows()) == {
            "DefaultPerDayPerProject",
            "DefaultPerMinutePerUser",
        }

    def test_each_limit_gets_its_own_interval_numerator(self):
        rows = self._rows()
        # Per-day limit is compared against the whole day's consumption.
        assert rows["DefaultPerDayPerProject"].daily_peak_usage == 5000.0
        # Per-minute limit is compared against the busiest single minute.
        assert rows["DefaultPerMinutePerUser"].daily_peak_usage == 40.0

    def test_unlimited_limit_yields_no_ratio(self):
        row = self._rows()["DefaultPerDayPerProject"]
        assert row.is_unlimited
        assert DataQualityFlag.LIMIT_UNLIMITED in row.flags
        assert row.peak_ratio is None

    def test_per_user_limit_is_not_divided_into_project_usage(self):
        """The v5 cross-product bug. Project-aggregate usage over a per-user
        limit is meaningless, so no ratio may be published."""
        row = self._rows()["DefaultPerMinutePerUser"]
        assert DataQualityFlag.LIMIT_SCOPE_NOT_COMPARABLE in row.flags
        assert row.peak_ratio is None
        assert row.current_ratio is None


class TestIntervalHandling:
    def test_per_minute_limit_uses_minute_peak_not_daily_total(self):
        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                rate_def("ReadsPerMinute", "svc.googleapis.com/reads", value=100, interval=60)
            ]
        }
        bundle = empty_bundle(
            # 43,200 requests spread over a day is only 30/min -- well within
            # a 100/min limit. v5 would have divided the daily total by the
            # per-minute limit and reported 43,200%.
            rate_daily_totals=[usage("svc.googleapis.com/reads", 43_200.0)],
            rate_minute_peaks=[usage("svc.googleapis.com/reads", 30.0)],
        )

        rows = build_rollups(bundle, definitions)

        assert len(rows) == 1
        assert rows[0].daily_peak_usage == 30.0
        assert rows[0].peak_ratio == pytest.approx(0.3)
        assert DataQualityFlag.RATIO_IMPLAUSIBLE not in rows[0].flags

    def test_hourly_limit_is_rescaled_from_daily_total(self):
        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                rate_def("ReadsPerHour", "svc.googleapis.com/reads", value=1000, interval=3600)
            ]
        }
        bundle = empty_bundle(rate_daily_totals=[usage("svc.googleapis.com/reads", 2400.0)])

        rows = build_rollups(bundle, definitions)

        # 2400/day rescaled onto an hour == 100/hour.
        assert rows[0].daily_peak_usage == pytest.approx(100.0)
        assert rows[0].peak_ratio == pytest.approx(0.1)


class TestDataQuality:
    def test_usage_without_a_known_limit_is_retained_and_flagged(self):
        bundle = empty_bundle(allocation_peaks=[usage("svc.googleapis.com/orphan", 7.0)])

        rows = build_rollups(bundle, {})

        assert len(rows) == 1
        assert rows[0].key.quota_metric == "svc.googleapis.com/orphan"
        assert rows[0].daily_peak_usage == 7.0
        assert DataQualityFlag.USAGE_WITHOUT_LIMIT in rows[0].flags
        assert rows[0].peak_ratio is None

    def test_zero_limit_produces_no_ratio(self):
        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                allocation_def("ZeroPerProject", "svc.googleapis.com/z", value=0)
            ]
        }
        bundle = empty_bundle(allocation_peaks=[usage("svc.googleapis.com/z", 5.0)])

        row = build_rollups(bundle, definitions)[0]

        assert DataQualityFlag.LIMIT_NON_POSITIVE in row.flags
        assert row.peak_ratio is None

    def test_genuine_overage_is_reported_not_suppressed(self):
        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                allocation_def("CapPerProject", "svc.googleapis.com/c", value=100)
            ]
        }
        bundle = empty_bundle(allocation_peaks=[usage("svc.googleapis.com/c", 105.0)])

        row = build_rollups(bundle, definitions)[0]

        assert row.peak_ratio == pytest.approx(1.05)
        assert DataQualityFlag.RATIO_IMPLAUSIBLE not in row.flags

    def test_absurd_ratio_is_flagged_as_a_bug(self):
        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                allocation_def("CapPerProject", "svc.googleapis.com/c", value=100)
            ]
        }
        bundle = empty_bundle(allocation_peaks=[usage("svc.googleapis.com/c", 100_000.0)])

        row = build_rollups(bundle, definitions)[0]

        assert DataQualityFlag.RATIO_IMPLAUSIBLE in row.flags

    def test_location_specific_limit_value_is_used(self):
        definition = QuotaDefinition(
            service="compute.googleapis.com",
            quota_id="CPUS-per-project-region",
            quota_metric="compute.googleapis.com/cpus",
            quota_class=QuotaClass.ALLOCATION,
            interval_seconds=None,
            interval_source=IntervalSource.NOT_APPLICABLE,
            scope=LimitScope.REGION,
            values_by_location={"us-central1": 500, "us-west1": 24, "global": 8},
        )
        definitions = {(PROJECT, "compute.googleapis.com"): [definition]}
        bundle = empty_bundle(
            allocation_peaks=[
                usage(
                    "compute.googleapis.com/cpus",
                    12.0,
                    service="compute.googleapis.com",
                    location="us-west1",
                )
            ]
        )

        row = build_rollups(bundle, definitions)[0]

        assert row.limit_value == 24
        assert row.peak_ratio == pytest.approx(0.5)
