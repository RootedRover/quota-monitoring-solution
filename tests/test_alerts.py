"""Unit tests for lightweight zero-GCP-config quota alerting (Decision 4)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

import dashboard.app as app_mod
from collector.alerts import (
    build_console_promql,
    emit_alert_summary,
    evaluate_breaches,
)
from collector.model import (
    DailyRollup,
    DataQualityFlag,
    IntervalSource,
    LimitScope,
    QuotaClass,
    QuotaKey,
)
from dashboard.authz import CallerIdentity, ProjectTarget


def _fake_request(path: str = "/") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


def _rollup(
    *,
    project_id: str = "krishngupt-argolis",
    service: str = "compute.googleapis.com",
    quota_metric: str = "compute.googleapis.com/cpus",
    limit_name: str = "CPUs-per-project-region",
    location: str = "asia-south1",
    quota_class: QuotaClass = QuotaClass.ALLOCATION,
    usage_date: dt.date = dt.date(2026, 10, 3),
    peak_usage: float = 85.0,
    limit_value: int | None = 100,
    is_unlimited: bool = False,
    flags: tuple[DataQualityFlag, ...] = (),
) -> DailyRollup:
    return DailyRollup(
        key=QuotaKey(
            project_id=project_id,
            service=service,
            quota_metric=quota_metric,
            limit_name=limit_name,
            location=location,
        ),
        usage_date_utc=usage_date,
        usage_date_local=usage_date,
        window_boundary="UTC",
        quota_class=quota_class,
        scope=LimitScope.PROJECT,
        interval_seconds=None if quota_class == QuotaClass.ALLOCATION else 60,
        interval_source=IntervalSource.CLOUD_QUOTAS,
        current_usage=peak_usage,
        daily_peak_usage=peak_usage,
        limit_value=limit_value,
        is_unlimited=is_unlimited,
        is_precise=True,
        flags=flags,
    )


def test_evaluate_breaches_filters_latest_comparable_at_or_above_threshold() -> None:
    rows = [
        # Older day had 95% but latest day dropped to 50% -> should NOT breach
        _rollup(
            quota_metric="compute.googleapis.com/cpus",
            usage_date=dt.date(2026, 10, 1),
            peak_usage=95.0,
            limit_value=100,
        ),
        _rollup(
            quota_metric="compute.googleapis.com/cpus",
            usage_date=dt.date(2026, 10, 3),
            peak_usage=50.0,
            limit_value=100,
        ),
        # Latest day at 82% -> SHOULD breach 80% threshold
        _rollup(
            quota_metric="aiplatform.googleapis.com/online_prediction_requests",
            service="aiplatform.googleapis.com",
            quota_class=QuotaClass.RATE,
            usage_date=dt.date(2026, 10, 3),
            peak_usage=82.0,
            limit_value=100,
        ),
        # Non-comparable (per-user limit) -> should NOT breach even if usage > limit
        _rollup(
            quota_metric="dns.googleapis.com/default",
            usage_date=dt.date(2026, 10, 3),
            peak_usage=95.0,
            limit_value=100,
            flags=(DataQualityFlag.LIMIT_SCOPE_NOT_COMPARABLE,),
        ),
    ]

    breaches = evaluate_breaches(rows, threshold=0.80)
    assert len(breaches) == 1
    assert breaches[0].quota_metric == "aiplatform.googleapis.com/online_prediction_requests"
    assert breaches[0].peak_ratio == 0.82


def test_emit_alert_summary_logs_json_and_posts_webhook(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _rollup(
            quota_metric="compute.googleapis.com/cpus",
            usage_date=dt.date(2026, 10, 3),
            peak_usage=91.0,
            limit_value=100,
        )
    ]
    breaches = evaluate_breaches(rows, threshold=0.80)
    posted: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int = 10) -> MagicMock:
        posted.append({"url": url, "json": json, "timeout": timeout})
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr("collector.alerts.requests.post", fake_post)
    summary = emit_alert_summary(
        breaches,
        threshold=0.80,
        webhook_url="https://chat.googleapis.com/v1/spaces/AAA/messages",
        dashboard_url="https://qms-dashboard-114680847754.asia-south1.run.app",
    )
    assert summary["event"] == "QMS_QUOTA_THRESHOLD_SUMMARY"
    assert summary["breach_count"] == 1
    assert summary["severity"] == "WARNING"
    out = capsys.readouterr().out
    assert '"event": "QMS_QUOTA_THRESHOLD_SUMMARY"' in out
    assert len(posted) == 1
    assert "91.0%" in posted[0]["json"]["text"]


def test_build_console_promql_for_allocation_and_rate_quotas() -> None:
    alloc_q = build_console_promql(
        project_id="krishngupt-argolis",
        quota_metric="compute.googleapis.com/cpus",
        location="asia-south1",
        quota_class="ALLOCATION",
        interval_seconds=None,
        limit_value=24,
        threshold=0.80,
    )
    assert "serviceruntime.googleapis.com/quota/allocation/usage" in alloc_q
    assert 'monitored_resource="consumer_quota"' in alloc_q
    assert "/ 24) >= 0.80" in alloc_q

    rate_q = build_console_promql(
        project_id="krishngupt-argolis",
        quota_metric="aiplatform.googleapis.com/online_prediction_requests",
        location="asia-south1",
        quota_class="RATE",
        interval_seconds=60,
        limit_value=600,
        threshold=0.80,
    )
    assert "serviceruntime.googleapis.com/quota/rate/net_usage" in rate_q
    assert "[1m]" in rate_q
    assert "/ 600) >= 0.80" in rate_q


def test_dashboard_api_alerts_and_header_bell_scoped_per_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_repo = MagicMock()
    fake_repo.project = "krishngupt-argolis"
    fake_repo.dataset = "quota_monitoring"
    fake_repo.known_projects.return_value = [
        ProjectTarget(
            project_id="proj-allowed",
            project_number="111",
            folder_id="",
            org_id="957650833838",
        ),
        ProjectTarget(
            project_id="proj-denied",
            project_number="222",
            folder_id="",
            org_id="957650833838",
        ),
    ]

    def fake_snapshot(
        limit: int = 500,
        min_ratio: float = 0.0,
        allowed_projects: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        del limit, min_ratio
        all_rows = [
            {
                "project_id": "proj-allowed",
                "service": "compute.googleapis.com",
                "quota_metric": "compute.googleapis.com/cpus",
                "limit_name": "CPUs",
                "location": "asia-south1",
                "quota_class": "ALLOCATION",
                "interval_seconds": None,
                "limit_value": 100,
                "is_unlimited": False,
                "peak_usage_7d": 85,
                "peak_ratio_7d": 0.85,
                "peak_ratio_30d": 0.85,
                "current_ratio": 0.80,
                "quota_adjuster_enabled": None,
            },
            {
                "project_id": "proj-denied",
                "service": "compute.googleapis.com",
                "quota_metric": "compute.googleapis.com/ssd_total_gb",
                "limit_name": "SSD",
                "location": "asia-south1",
                "quota_class": "ALLOCATION",
                "interval_seconds": None,
                "limit_value": 100,
                "is_unlimited": False,
                "peak_usage_7d": 95,
                "peak_ratio_7d": 0.95,
                "peak_ratio_30d": 0.95,
                "current_ratio": 0.95,
                "quota_adjuster_enabled": None,
            },
        ]
        filtered = [
            r for r in all_rows if not allowed_projects or r["project_id"] in allowed_projects
        ]
        return {
            "summary": {"tracked": len(filtered), "critical": 0, "warning": len(filtered)},
            "risk": filtered,
            "movers": [],
            "hierarchy": [],
            "quality": [],
            "freshness": {
                "collected_at": dt.datetime(2026, 10, 3, 9, 0, tzinfo=dt.UTC),
                "rows_total": 10,
            },
        }

    fake_repo.snapshot.side_effect = fake_snapshot
    fake_auth = MagicMock()
    fake_auth.permission = "cloudquotas.quotaInfos.list"
    fake_auth.allowed_projects.return_value = frozenset({"proj-allowed"})

    monkeypatch.setattr(app_mod, "repo", lambda: fake_repo)
    monkeypatch.setattr(app_mod, "authorizer", lambda: fake_auth)
    monkeypatch.setattr(
        app_mod,
        "authenticate_request",
        lambda _req: CallerIdentity(
            email="admin@krishngupt.altostrat.com",
            sub="123",
            auth_source="iap",
        ),
    )

    payload = app_mod.api_alerts(_fake_request("/api/alerts"), threshold=0.80)
    assert payload["count"] == 1
    assert payload["alerts"][0]["project_id"] == "proj-allowed"
    assert payload["alerts"][0]["worst_ratio"] == 0.85

    r_html = app_mod.index(_fake_request("/"), min_ratio=0.0, limit=500)
    assert r_html.status_code == 200
    body_text = bytes(r_html.body).decode("utf-8")
    assert 'id="btn-alerts"' in body_text
    assert 'id="drawer-console-actions"' in body_text
