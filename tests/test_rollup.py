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

    def test_imprecise_limit_is_flagged_but_remains_comparable(self):
        """Decision 1A: ``isPrecise=False`` adds ``LIMIT_NOT_PRECISE`` as an
        informational flag for the Data Quality panel, but does NOT suppress
        the ratio or withhold the row from the main Risk table."""
        definition = QuotaDefinition(
            service="svc.googleapis.com",
            quota_id="ReadsPerMinutePerProject",
            quota_metric="svc.googleapis.com/reads",
            quota_class=QuotaClass.RATE,
            interval_seconds=60,
            interval_source=IntervalSource.CLOUD_QUOTAS,
            scope=LimitScope.PROJECT,
            is_precise=False,
            values_by_location={"global": 100},
        )
        definitions = {(PROJECT, "svc.googleapis.com"): [definition]}
        bundle = empty_bundle(rate_minute_peaks=[usage("svc.googleapis.com/reads", 25.0)])

        row = build_rollups(bundle, definitions)[0]

        assert DataQualityFlag.LIMIT_NOT_PRECISE in row.flags
        assert row.comparable is True
        assert row.peak_ratio == pytest.approx(0.25)


class TestQuotaAdjusterSettings:
    """Decision 2A: read-only QuotaAdjusterSettings collection and schema/view wiring."""

    def _make_source(self, responses: dict[str, dict], monkeypatch):
        from collector.sources.cloud_quotas import CloudQuotasError, CloudQuotasSource

        monkeypatch.setattr(
            "google.auth.default",
            lambda scopes=None: (object(), "proj-billing"),
        )
        src = CloudQuotasSource(billing_project="proj-billing")
        calls: list[str] = []

        def fake_get(url: str, params: dict[str, str]) -> dict:
            calls.append(url)
            payload = responses.get(url, {})
            if "error" in payload:
                raise CloudQuotasError(payload["error"])
            return payload

        monkeypatch.setattr(src, "_get", fake_get)
        return src, calls

    def test_adjuster_enabled_disabled_and_cached(self, monkeypatch):
        base = "https://cloudquotas.googleapis.com/v1beta"
        src, calls = self._make_source(
            {
                f"{base}/projects/proj-on/locations/global/quotaAdjusterSettings": {
                    "name": "projects/proj-on/locations/global/quotaAdjusterSettings",
                    "enablement": "ENABLED",
                },
                f"{base}/projects/proj-off/locations/global/quotaAdjusterSettings": {
                    "name": "projects/proj-off/locations/global/quotaAdjusterSettings",
                    "enablement": "DISABLED",
                },
                f"{base}/projects/proj-err/locations/global/quotaAdjusterSettings": {
                    "error": "permission denied",
                },
            },
            monkeypatch,
        )

        assert src.get_quota_adjuster_enabled("projects/proj-on") is True
        # Second call for the same container hits the cache without another HTTP request.
        assert src.get_quota_adjuster_enabled("projects/proj-on") is True
        assert src.get_quota_adjuster_enabled("projects/proj-off") is False
        assert src.get_quota_adjuster_enabled("projects/proj-err") is None
        assert len(calls) == 3

    def test_bigquery_row_and_views_include_quota_adjuster(self):
        from collector.sinks.bigquery import SCHEMA, Placement, _to_json
        from collector.sinks.views import _definitions

        definitions = {
            (PROJECT, "svc.googleapis.com"): [
                allocation_def("CapPerProject", "svc.googleapis.com/c", value=100)
            ]
        }
        bundle = empty_bundle(allocation_peaks=[usage("svc.googleapis.com/c", 40.0)])
        row = build_rollups(bundle, definitions)[0]

        rec_on = _to_json(
            row,
            DAY,
            Placement(
                org_id="123",
                folder_id=None,
                project_number="456",
                quota_adjuster_enabled=True,
            ),
        )
        rec_none = _to_json(row, DAY, None)

        assert rec_on["quota_adjuster_enabled"] is True
        assert rec_none["quota_adjuster_enabled"] is None
        assert any(
            f.name == "quota_adjuster_enabled" and f.field_type == "BOOL" for f in SCHEMA
        )

        view_sql = _definitions("proj.ds.quota_daily")
        assert "quota_adjuster_enabled" in view_sql["quota_risk"]
        assert "quota_adjuster_enabled" in view_sql["quota_hierarchy"]


class TestCustomDimensionFamilyQuotas:
    """Compute Engine Generation 2 custom-dimension family quotas
    (cpus_per_vm_family, gpus_per_gpu_family, local_ssd_total_storage_per_vm_family,
    tpus_per_tpu_family) and GCS egress per-second rate quotas."""

    def test_four_tier_specificity_and_per_family_rollup(self):
        from collector.sources.cloud_quotas import _to_definitions

        raw_quota_info = {
            "quotaId": "CPUS-PER-VM-FAMILY-per-project-region",
            "metric": "compute.googleapis.com/cpus_per_vm_family",
            "service": "compute.googleapis.com",
            "isPrecise": True,
            "refreshInterval": None,
            "containerType": "PROJECT",
            "dimensions": ["region", "vm_family"],
            "quotaDisplayName": "CPUs per VM family",
            # Deliberately put generic regional default FIRST to verify that
            # rank-based specificity beats raw array order.
            "dimensionsInfos": [
                {
                    "dimensions": {"region": "asia-east1"},
                    "details": {"value": "0"},
                    "applicableLocations": ["asia-east1"],
                },
                {
                    "dimensions": {"vm_family": "C3D"},
                    "details": {"value": "8"},
                    "applicableLocations": ["asia-east1", "us-central1"],
                },
                {
                    "dimensions": {"region": "asia-east1", "vm_family": "C3D"},
                    "details": {"value": "64"},
                    "applicableLocations": ["asia-east1"],
                },
                {
                    "dimensions": {"region": "us-central1", "vm_family": "C4"},
                    "details": {"value": "128"},
                    "applicableLocations": ["us-central1"],
                },
                {
                    "dimensions": {},
                    "details": {"value": "0"},
                    "applicableLocations": ["us-central1", "europe-west1"],
                },
            ],
        }

        defs = _to_definitions(raw_quota_info, "compute.googleapis.com")
        by_metric = {d.quota_metric: d for d in defs}

        assert "compute.googleapis.com/cpus_per_vm_family/C3D" in by_metric
        assert "compute.googleapis.com/cpus_per_vm_family/C4" in by_metric

        c3d = by_metric["compute.googleapis.com/cpus_per_vm_family/C3D"]
        # Rank 4 (region + vm_family) beats Rank 3 (vm_family default) and Rank 2 (region default)
        assert c3d.value_for("asia-east1") == 64
        # Rank 3 (vm_family default = 8) beats Rank 1 (empty dims default = 0)
        assert c3d.value_for("us-central1") == 8

        c4 = by_metric["compute.googleapis.com/cpus_per_vm_family/C4"]
        assert c4.value_for("us-central1") == 128
        # In asia-east1, C4 has no family entry so it inherits Rank 2 (region default = 0)
        assert c4.value_for("asia-east1") == 0

        definitions = {(PROJECT, "compute.googleapis.com"): defs}
        bundle = empty_bundle(
            allocation_peaks=[
                usage(
                    "compute.googleapis.com/cpus_per_vm_family/C3D",
                    32.0,
                    service="compute.googleapis.com",
                    location="asia-east1",
                ),
                usage(
                    "compute.googleapis.com/cpus_per_vm_family/C4",
                    96.0,
                    service="compute.googleapis.com",
                    location="us-central1",
                ),
                # N4 is not explicitly in dimensionsInfos -> falls back to base definition
                usage(
                    "compute.googleapis.com/cpus_per_vm_family/N4",
                    4.0,
                    service="compute.googleapis.com",
                    location="europe-west1",
                ),
            ]
        )

        rows = {
            (r.key.quota_metric, r.key.location): r for r in build_rollups(bundle, definitions)
        }

        c3d_row = rows[("compute.googleapis.com/cpus_per_vm_family/C3D", "asia-east1")]
        assert c3d_row.key.limit_name == "CPUS-PER-VM-FAMILY-per-project-region/C3D"
        assert c3d_row.limit_value == 64
        assert c3d_row.peak_ratio == pytest.approx(0.5)

        c4_row = rows[("compute.googleapis.com/cpus_per_vm_family/C4", "us-central1")]
        assert c4_row.key.limit_name == "CPUS-PER-VM-FAMILY-per-project-region/C4"
        assert c4_row.limit_value == 128
        assert c4_row.peak_ratio == pytest.approx(0.75)

        n4_row = rows[("compute.googleapis.com/cpus_per_vm_family/N4", "europe-west1")]
        assert n4_row.key.limit_name == "CPUS-PER-VM-FAMILY-per-project-region/N4"
        assert n4_row.limit_value == 0
        assert DataQualityFlag.LIMIT_NON_POSITIVE in n4_row.flags

    def test_monitoring_source_parses_custom_dimension_and_isolates_failures(self, monkeypatch):
        from collector.sources.monitoring import (
            Q_ALLOCATION_PEAK,
            Q_COMPUTE_CUSTOM_ALLOCATION_PEAK,
            MonitoringSource,
            RangePoint,
        )

        monkeypatch.setattr(
            "collector.sources.monitoring.google.auth.default",
            lambda **_kw: (object(), PROJECT),
        )
        mon = MonitoringSource(PROJECT)

        def fake_query_range(query: str, *, start: dt.datetime, end: dt.datetime):
            del start, end
            if query == Q_ALLOCATION_PEAK:
                yield (
                    {
                        "project_id": PROJECT,
                        "service": "compute.googleapis.com",
                        "quota_metric": "compute.googleapis.com/cpus",
                        "location": "us-central1",
                    },
                    [RangePoint(at=DAY, value=16.0)],
                )
            elif query == Q_COMPUTE_CUSTOM_ALLOCATION_PEAK:
                yield (
                    {
                        "project_id": PROJECT,
                        "location": "us-central1",
                        "limit_name": "GPUS-PER-GPU-FAMILY-per-project-region",
                        "gpu_family": "NVIDIA_H100",
                    },
                    [RangePoint(at=DAY, value=8.0)],
                )

        monkeypatch.setattr(mon, "query_range_daily", fake_query_range)
        samples = mon.allocation_daily_peaks(start=DAY, end=DAY)
        by_metric = {s.key.quota_metric: s for s in samples}
        assert by_metric["compute.googleapis.com/cpus"].value == 16.0
        assert by_metric["compute.googleapis.com/gpus_per_gpu_family/NVIDIA_H100"].value == 8.0

        # Now verify safeguard: if custom_dimension_daily_peaks raises, standard
        # consumer_quota samples are still returned intact.
        def broken_custom(**_kw):
            raise RuntimeError("simulated Location query failure")

        monkeypatch.setattr(mon, "custom_dimension_daily_peaks", broken_custom)
        safe_samples = mon.allocation_daily_peaks(start=DAY, end=DAY)
        assert len(safe_samples) == 1
        assert safe_samples[0].key.quota_metric == "compute.googleapis.com/cpus"

    def test_gcs_egress_per_second_rate_quota_rescaled_accurately(self):
        """GCS egress bandwidth quotas (refreshInterval='second', interval=1)
        must rescale 60-second peak usage onto a 1-second window so ratios are
        never inflated 60x."""
        definitions = {
            (PROJECT, "storage.googleapis.com"): [
                rate_def(
                    "GoogleEgressBandwidthPerSecondPerRegion",
                    "storage.googleapis.com/google_egress_bandwidth",
                    value=25_000_000_000,  # 25 GB/s
                    interval=1,
                    service="storage.googleapis.com",
                    scope=LimitScope.REGION,
                )
            ]
        }
        # 600 GB transferred in the peak minute == 10 GB/s average over that minute (40% of 25 GB/s)
        bundle = empty_bundle(
            rate_minute_peaks=[
                usage(
                    "storage.googleapis.com/google_egress_bandwidth",
                    600_000_000_000.0,
                    service="storage.googleapis.com",
                    location="us-central1",
                )
            ]
        )

        row = build_rollups(bundle, definitions)[0]
        assert row.daily_peak_usage == pytest.approx(10_000_000_000.0)
        assert row.peak_ratio == pytest.approx(0.40)
        assert DataQualityFlag.RATIO_IMPLAUSIBLE not in row.flags
