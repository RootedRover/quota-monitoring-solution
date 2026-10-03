"""Unit tests for 100-500+ project scaling, rate-limiting, partition pruning, and lookup completeness."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from collector import cli
from collector.model import UsageKey, UsageSample
from collector.sinks.bigquery import Placement
from collector.sinks.views import _definitions
from collector.sources.cloud_quotas import CloudQuotasSource
from collector.sources.monitoring import MonitoringSource
from dashboard.app import api_risk
from dashboard.authz import Authorizer, CallerIdentity, ProjectTarget
from dashboard.queries import Repository, clear_cache


def _make_request(headers: dict[str, str] | None = None) -> Request:
    raw = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "GET", "path": "/api/risk", "headers": raw})


def test_authorizer_500_projects_single_rpc_when_org_cai_succeeds() -> None:
    """When Org CAI returns HTTP 200 and grants >=1 project, do NOT make 498 per-project RPCs."""
    targets = [
        ProjectTarget(
            project_id=f"proj-{i:03d}",
            project_number=str(100000 + i),
            folder_id=f"folder-{i % 10}",
            org_id="957650833838",
        )
        for i in range(500)
    ]

    session = MagicMock()
    cai_resp = MagicMock(status_code=200)
    cai_resp.json.return_value = {
        "mainAnalysis": {
            "analysisResults": [
                {
                    "attachedResourceFullName": (
                        "//cloudresourcemanager.googleapis.com/projects/proj-007"
                    ),
                },
                {
                    "attachedResourceFullName": (
                        "//cloudresourcemanager.googleapis.com/projects/proj-042"
                    ),
                },
            ]
        }
    }
    session.get.return_value = cai_resp

    auth = Authorizer(session=session)
    allowed = auth.allowed_projects("owner@example.com", targets)
    assert allowed == frozenset({"proj-007", "proj-042"})
    # Must resolve in 1 single Org-level CAI GET call, with 0 CRM POST calls!
    assert session.get.call_count == 1
    assert session.post.call_count == 0


def test_authorizer_bounds_crm_project_fallback_when_cai_returns_empty() -> None:
    """When Org CAI returns 0 grants on 200 projects, CRM project fallback is bounded by max_crm_fallback_projects."""
    targets = [
        ProjectTarget(
            project_id=f"proj-{i:03d}",
            project_number=str(100000 + i),
            folder_id="",
            org_id="957650833838",
        )
        for i in range(200)
    ]

    session = MagicMock()
    empty_cai = MagicMock(status_code=200)
    empty_cai.json.return_value = {"mainAnalysis": {"fullyExplored": True}}
    session.get.return_value = empty_cai

    empty_crm = MagicMock(status_code=200)
    empty_crm.json.return_value = {"bindings": []}
    session.post.return_value = empty_crm

    auth = Authorizer(session=session, max_crm_fallback_projects=15)
    allowed = auth.allowed_projects("no-access@example.com", targets)
    assert allowed == frozenset()
    # 1 Org CAI GET call, 0 project-level CAI GET calls (skipped because Org CAI returned 200),
    # and 1 Org CRM POST + 15 capped project CRM POST calls = 16 total POST calls (not 200!).
    assert session.get.call_count == 1
    assert session.post.call_count == 16


def test_views_include_partition_pruning_on_usage_date_utc() -> None:
    """All views scanning quota_daily must filter on usage_date_utc to prune 400-day partitions."""
    defs = _definitions("host-proj.quota_monitoring.quota_daily")
    assert "usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 35 DAY)" in defs["quota_latest"]
    assert "usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 35 DAY)" in defs["quota_peaks"]
    assert "usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)" in defs["quota_movers"]
    assert (
        "usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)" in defs["quota_quality"]
    )


def test_repository_facets_and_filtered_risk_slice_beyond_cache_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _all_risk hits RISK_CACHE_LIMIT, _all_facets and filtered risk() query BigQuery."""
    clear_cache()
    monkeypatch.setattr("dashboard.queries.bigquery.Client", lambda **_kw: MagicMock())
    monkeypatch.setattr("dashboard.queries.RISK_CACHE_LIMIT", 2)

    r = Repository(project="host-proj", dataset="quota_monitoring", location="asia-south1")

    # Simulate _all_risk hitting the cap of 2 rows (proj-a and proj-b), while proj-c is outside top 2
    monkeypatch.setattr(
        r,
        "_all_risk",
        lambda: [
            {
                "project_id": "proj-a",
                "service": "compute.googleapis.com",
                "quota_metric": "compute.googleapis.com/cpus",
                "peak_ratio_30d": 0.95,
                "peak_ratio_7d": 0.90,
                "last_seen": dt.date(2026, 10, 2),
            },
            {
                "project_id": "proj-b",
                "service": "iam.googleapis.com",
                "quota_metric": "iam.googleapis.com/service_accounts",
                "peak_ratio_30d": 0.85,
                "peak_ratio_7d": 0.80,
                "last_seen": dt.date(2026, 10, 2),
            },
        ],
    )
    monkeypatch.setattr(r, "_all_quality_by_project", list)

    queried_sql: list[str] = []

    def fake_rows(sql: str, params: list | None = None) -> list[dict]:
        queried_sql.append(sql)
        if "GROUP BY project_id, service, quota_metric" in sql:
            return [
                {
                    "project_id": "proj-a",
                    "service": "compute.googleapis.com",
                    "quota_metric": "compute.googleapis.com/cpus",
                    "quota_count": 10,
                    "critical_count": 1,
                    "warning_count": 0,
                    "max_ratio_30d": 0.95,
                    "last_seen": dt.date(2026, 10, 2),
                },
                {
                    "project_id": "proj-c",
                    "service": "storage.googleapis.com",
                    "quota_metric": "storage.googleapis.com/buckets",
                    "quota_count": 5,
                    "critical_count": 0,
                    "warning_count": 0,
                    "max_ratio_30d": 0.12,
                    "last_seen": dt.date(2026, 10, 2),
                },
            ]
        if params and any(getattr(p, "value", "") == "proj-c" for p in params):
            return [
                {
                    "project_id": "proj-c",
                    "service": "storage.googleapis.com",
                    "quota_metric": "storage.googleapis.com/buckets",
                    "limit_name": "BucketsPerProject",
                    "location": "global",
                    "limit_value": 100,
                    "peak_usage_7d": 12,
                    "peak_ratio_7d": 0.12,
                    "peak_ratio_30d": 0.12,
                }
            ]
        return []

    monkeypatch.setattr(r, "_rows", fake_rows)

    summary = r.summary(allowed_projects={"proj-a", "proj-c"})
    assert summary["tracked"] == 15
    assert summary["projects"] == 2
    assert summary["critical"] == 1

    facets = r.facets(allowed_projects={"proj-a", "proj-c"})
    assert {f["p"] for f in facets} == {"proj-a", "proj-c"}

    # Filtering for proj-c (which was outside the top-2 _all_risk cap) must return proj-c's rows!
    proj_c_rows = r.risk(project_id="proj-c", allowed_projects={"proj-a", "proj-c"})
    assert len(proj_c_rows) == 1
    assert proj_c_rows[0]["project_id"] == "proj-c"

    # Filtering for an unauthorized project must return [] without querying BigQuery
    unauth_rows = r.risk(project_id="proj-forbidden", allowed_projects={"proj-a", "proj-c"})
    assert unauth_rows == []


def test_api_risk_endpoint_returns_formatted_filtered_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_cache()
    monkeypatch.setattr(
        "dashboard.app._require_caller",
        lambda _req: CallerIdentity(email="viewer@example.com", auth_source="iap"),
    )
    fake_repo = MagicMock()
    fake_repo.risk.return_value = [
        {
            "project_id": "proj-1",
            "service": "compute.googleapis.com",
            "quota_metric": "compute.googleapis.com/cpus",
            "limit_name": "CPUsPerProject",
            "location": "asia-south1",
            "quota_class": "ALLOCATION",
            "interval_seconds": None,
            "quota_adjuster_enabled": True,
            "is_unlimited": False,
            "limit_value": 200,
            "peak_usage_7d": 180,
            "peak_ratio_7d": 0.90,
            "peak_ratio_30d": 0.95,
        }
    ]
    monkeypatch.setattr("dashboard.app.repo", lambda: fake_repo)
    monkeypatch.setattr(
        "dashboard.app._resolve_authz",
        lambda caller, _r: MagicMock(
            email=caller.email,
            allowed_projects=frozenset({"proj-1"}),
        ),
    )

    res = api_risk(
        _make_request(),
        project_id="proj-1",
        service="compute.googleapis.com",
        quota_metric="",
        min_ratio=0.8,
        limit=100,
    )
    assert res["count"] == 1
    row = res["rows"][0]
    assert row["project_id"] == "proj-1"
    assert row["limit_fmt"] == "200"
    assert row["pct30_fmt"] == "95.0%"
    assert row["sev30"] == "critical"
    assert row["adjuster"] == "ENABLED"


def test_cloud_quotas_retries_on_429_and_does_not_cache_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CloudQuotasSource must back off on HTTP 429 and never poison _cache with [] on transient errors."""
    fake_creds = MagicMock(valid=True)
    fake_creds.token = "fake-oauth-token"  # noqa: S105
    monkeypatch.setattr(
        "collector.sources.cloud_quotas.google.auth.default",
        lambda **_kw: (fake_creds, "proj"),
    )
    monkeypatch.setattr("collector.sources.cloud_quotas.time.sleep", lambda _s: None)

    session = MagicMock()
    resp_429 = MagicMock(status_code=429)
    resp_200 = MagicMock(status_code=200)
    resp_200.json.return_value = {
        "quotaInfos": [
            {
                "quotaId": "ReadRequestsPerMinute",
                "metric": "cloudquotas.googleapis.com/read_requests",
                "service": "cloudquotas.googleapis.com",
                "refreshInterval": "minute",
                "isPrecise": True,
                "dimensionsInfos": [
                    {"applicableLocations": ["global"], "details": {"value": "1200"}}
                ],
            }
        ]
    }

    # Case 1: 429 followed by 200 succeeds and caches the definition.
    session.get.side_effect = [resp_429, resp_200]
    src = CloudQuotasSource(
        billing_project="host-proj", session=session, max_rps=0, max_retries=2
    )
    defs = src.list_quota_infos("projects/proj-a", "cloudquotas.googleapis.com")
    assert len(defs) == 1
    assert defs[0].quota_id == "ReadRequestsPerMinute"
    assert ("projects/proj-a", "cloudquotas.googleapis.com") in src._cache

    # Case 2: Persistent 429 across all retries returns [] for that call so the
    # collector doesn't crash, but does NOT cache [] in src._cache!
    session.get.side_effect = [resp_429, resp_429, resp_429]
    defs_fail = src.list_quota_infos("projects/proj-b", "compute.googleapis.com")
    assert defs_fail == []
    assert ("projects/proj-b", "compute.googleapis.com") not in src._cache


def test_monitoring_source_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_creds = MagicMock(valid=True)
    fake_creds.token = "fake-oauth-token"  # noqa: S105
    monkeypatch.setattr(
        "collector.sources.monitoring.google.auth.default",
        lambda **_kw: (fake_creds, "proj"),
    )
    monkeypatch.setattr("collector.sources.monitoring.time.sleep", lambda _s: None)

    session = MagicMock()
    resp_429 = MagicMock(status_code=429)
    resp_200 = MagicMock(status_code=200)
    resp_200.json.return_value = {"status": "success", "data": {"result": []}}
    session.post.side_effect = [resp_429, resp_200]

    mon = MonitoringSource("proj-a", session=session, max_retries=2)
    samples = mon.rate_minute_peaks(
        start=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
    )
    assert samples == []
    assert session.post.call_count == 2


def test_collector_parallel_gather_and_enrich_placements_preserve_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel gather() and enrich_placements() must preserve deterministic project order."""

    class FakeMonitoring:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def allocation_daily_peaks(self, **_kw: Any) -> list[UsageSample]:
            return [
                UsageSample(
                    key=UsageKey(
                        project_id=self.project_id,
                        service="compute.googleapis.com",
                        quota_metric="compute.googleapis.com/cpus",
                        location="global",
                    ),
                    observed_at=dt.datetime(2026, 10, 2, tzinfo=dt.UTC),
                    value=10.0,
                    window_seconds=None,
                )
            ]

        def rate_daily_totals(self, **_kw: Any) -> list[UsageSample]:
            return []

        def rate_minute_peaks(self, **_kw: Any) -> list[UsageSample]:
            return []

    class FakeQuotas:
        def __init__(self, *, billing_project: str) -> None:
            self.billing_project = billing_project

        def list_quota_infos(self, container: str, service: str) -> list:
            return []

        def get_quota_adjuster_enabled(self, container: str) -> bool:
            return container.endswith("proj-002")

    monkeypatch.setattr(cli, "MonitoringSource", FakeMonitoring)
    monkeypatch.setattr(cli, "CloudQuotasSource", FakeQuotas)

    projects = ["proj-001", "proj-002", "proj-003", "proj-004"]
    placements = {
        pid: Placement(org_id="900", folder_id="500", project_number=str(i))
        for i, pid in enumerate(projects, 1)
    }

    enriched = cli.enrich_placements(
        projects, placements, billing_project="host-proj", max_workers=4
    )
    assert list(enriched.keys()) == projects
    assert enriched["proj-001"].quota_adjuster_enabled is False
    assert enriched["proj-002"].quota_adjuster_enabled is True

    bundle, defs = cli.gather(
        projects=projects, billing_project="host-proj", days=1, max_workers=4
    )
    assert [s.key.project_id for s in bundle.allocation_peaks] == projects
    assert len(defs) == 4
