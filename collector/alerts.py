"""Lightweight threshold alerting for daily collection runs.

Designed to require **zero additional GCP project configuration** by default:

1. **Structured stdout log event**: After each collection run, emits a single
   JSON log line (``event="QMS_QUOTA_THRESHOLD_SUMMARY"``) listing any
   comparable quotas whose peak utilization reached or exceeded the threshold
   (default 80%). Cloud Run automatically ingests stdout JSON into Cloud
   Logging with zero extra IAM roles or Terraform resources.
2. **Optional webhook digest**: If ``QMS_ALERT_WEBHOOK_URL`` (or
   ``--alert-webhook-url``) is provided (e.g. a Slack or Google Chat incoming
   webhook), posts a single consolidated summary card when one or more quotas
   cross the threshold.
3. **PromQL generator**: Builds the exact per-quota PromQL threshold expression
   for Google Cloud Monitoring so users can paste it directly into the Cloud
   Console Alert Policy creator for any individual mission-critical quota.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import asdict, dataclass

import requests

from .model import DailyRollup, QuotaClass

_LOG = logging.getLogger("qms.alerts")

DEFAULT_THRESHOLD = 0.80


@dataclass(frozen=True)
class ThresholdBreach:
    """A single quota bucket whose utilization reached or crossed the alert threshold."""

    project_id: str
    service: str
    quota_metric: str
    limit_name: str
    location: str
    quota_class: str
    usage_date_utc: str
    daily_peak_usage: float
    limit_value: int
    peak_ratio: float


def evaluate_breaches(
    rows: list[DailyRollup],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[ThresholdBreach]:
    """Return the latest comparable row per quota that meets or exceeds ``threshold``."""
    latest: dict[tuple, DailyRollup] = {}
    for row in rows:
        if not row.comparable or row.peak_ratio is None or row.limit_value is None:
            continue
        key = row.key.as_tuple()
        if key not in latest or row.usage_date_utc > latest[key].usage_date_utc:
            latest[key] = row

    breaches: list[ThresholdBreach] = []
    for row in latest.values():
        if row.peak_ratio is not None and row.peak_ratio >= threshold and row.limit_value:
            breaches.append(
                ThresholdBreach(
                    project_id=row.key.project_id,
                    service=row.key.service,
                    quota_metric=row.key.quota_metric,
                    limit_name=row.key.limit_name,
                    location=row.key.location,
                    quota_class=row.quota_class.value,
                    usage_date_utc=row.usage_date_utc.isoformat(),
                    daily_peak_usage=float(row.daily_peak_usage),
                    limit_value=int(row.limit_value),
                    peak_ratio=round(float(row.peak_ratio), 4),
                )
            )
    breaches.sort(key=lambda b: (-b.peak_ratio, b.project_id, b.quota_metric))
    return breaches


def emit_alert_summary(
    breaches: list[ThresholdBreach],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    webhook_url: str = "",
    dashboard_url: str = "",
) -> dict:
    """Log a structured JSON summary and optionally post to a Slack/Chat webhook."""
    payload = {
        "severity": "WARNING" if breaches else "INFO",
        "event": "QMS_QUOTA_THRESHOLD_SUMMARY",
        "threshold": threshold,
        "breach_count": len(breaches),
        "breaches": [asdict(b) for b in breaches[:50]],
    }
    # Single-line JSON on stdout is parsed automatically as jsonPayload by Cloud Run.
    print(json.dumps(payload, sort_keys=True))

    if breaches and webhook_url:
        _post_webhook_digest(
            breaches,
            threshold=threshold,
            webhook_url=webhook_url,
            dashboard_url=dashboard_url,
        )
    return payload


def _post_webhook_digest(
    breaches: list[ThresholdBreach],
    *,
    threshold: float,
    webhook_url: str,
    dashboard_url: str = "",
) -> bool:
    """Send a concise Markdown digest compatible with both Google Chat and Slack."""
    parsed = urllib.parse.urlparse(webhook_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        _LOG.warning("skipping invalid alert webhook URL scheme: %s", parsed.scheme)
        return False

    pct_label = f"{threshold * 100:.0f}%"
    lines = [
        f"*QMS Quota Alert*: *{len(breaches)}* quota(s) reached >= {pct_label} utilization:"
    ]
    for b in breaches[:15]:
        short_metric = (
            b.quota_metric.split("/", 1)[1] if "/" in b.quota_metric else b.quota_metric
        )
        lines.append(
            f"• `{b.project_id}` | `{short_metric}` (`{b.location}`): "
            f"*{b.peak_ratio * 100:.1f}%* ({b.daily_peak_usage:g} / {b.limit_value:,})"
        )
    if len(breaches) > 15:
        lines.append(f"_…and {len(breaches) - 15} more._")
    if dashboard_url:
        lines.append(f"View details: {dashboard_url}")

    body = {"text": "\n".join(lines)}
    try:
        resp = requests.post(webhook_url, json=body, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        _LOG.warning("webhook notification delivery failed: %s", exc)
        return False


def build_console_promql(
    *,
    project_id: str,
    quota_metric: str,
    location: str,
    quota_class: str,
    interval_seconds: int | None,
    limit_value: float | None,
    threshold: float = DEFAULT_THRESHOLD,
) -> str:
    """Build a ready-to-paste PromQL alert query for Google Cloud Monitoring."""
    loc = location or "global"
    qclass = (quota_class or "RATE").upper()
    if qclass == QuotaClass.ALLOCATION.value:
        metric_name = "serviceruntime.googleapis.com/quota/allocation/usage"
        expr = (
            f"max by (project_id, quota_metric, location) ("
            f'{{"{metric_name}", monitored_resource="consumer_quota", '
            f'project_id="{project_id}", quota_metric="{quota_metric}", location="{loc}"}})'
        )
    else:
        metric_name = "serviceruntime.googleapis.com/quota/rate/net_usage"
        window = "1d" if (interval_seconds and interval_seconds >= 86400) else "1m"
        expr = (
            f"sum by (project_id, quota_metric, location) ("
            f'increase({{"{metric_name}", monitored_resource="consumer_quota", '
            f'project_id="{project_id}", quota_metric="{quota_metric}", location="{loc}"}}[{window}]))'
        )

    if limit_value and float(limit_value) > 0 and float(limit_value) < 1e18:
        cutoff = round(float(limit_value) * threshold, 2)
        return f"({expr} / {int(limit_value)}) >= {threshold:.2f}  # >= {cutoff:g} of {int(limit_value):,}"
    return expr
