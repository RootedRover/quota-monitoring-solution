"""Precomputed BigQuery views that the dashboard reads.

The dashboard never touches ``quota_daily`` directly. Two reasons:

* Cost and latency. The risk table needs a 30-day peak per quota, which is a
  scan-and-group-by over the whole retention window. Expressing it once here
  means the UI issues a small, predictable query instead of re-deriving the
  same aggregate in five different handlers.
* Correctness. Every one of these views filters on ``is_comparable``. The
  single largest failure of the previous generation was publishing a ratio for
  rows where the numerator and denominator were not measuring the same thing.
  Making that filter a property of the view, rather than something each caller
  remembers to add, is the difference between a bug and an impossibility.

``quota_quality`` is the deliberate exception: it exists precisely to show the
rows the others exclude.
"""

from __future__ import annotations

import logging

from google.cloud import bigquery

from ..bqnames import qualified

_LOG = logging.getLogger(__name__)

# A quota row's identity. Used as the PARTITION BY throughout. v5 grouped on
# only three of these, which merged unrelated limits into one number.
_GRAIN = "project_id, service, quota_metric, limit_name, location"


def _definitions(table: str) -> dict[str, str]:
    return {
        # Most recent observation per quota, whatever day that happens to be.
        # Quota metrics are written sporadically, so "yesterday" is not a
        # reliable filter -- a quota can legitimately have no sample for days.
        "quota_latest": f"""
SELECT * EXCEPT(rn)
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY {_GRAIN}
      ORDER BY usage_date_utc DESC
    ) AS rn
  FROM `{table}`
)
WHERE rn = 1
""",
        # 7- and 30-day peaks. MAX over stored daily peaks: each daily row
        # already holds the worst moment of that day, computed against the
        # right window for its limit, so the peak-of-peaks is the true peak.
        "quota_peaks": f"""
SELECT
  {_GRAIN},
  MAX(IF(usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY),
         peak_ratio, NULL)) AS peak_ratio_7d,
  MAX(IF(usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY),
         peak_ratio, NULL)) AS peak_ratio_30d,
  MAX(IF(usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY),
         daily_peak_usage, NULL)) AS peak_usage_7d,
  MAX(IF(usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY),
         daily_peak_usage, NULL)) AS peak_usage_30d,
  COUNT(*) AS observed_days
FROM `{table}`
WHERE is_comparable
GROUP BY {_GRAIN}
""",
        # The main dashboard table: current state joined to the peaks.
        "quota_risk": f"""
SELECT
  l.org_id,
  l.folder_id,
  l.project_id,
  l.project_number,
  l.quota_adjuster_enabled,
  l.service,
  l.quota_metric,
  l.limit_name,
  l.location,
  l.quota_class,
  l.limit_scope,
  l.interval_seconds,
  l.interval_source,
  l.usage_date_utc AS last_seen,
  l.current_usage,
  l.daily_peak_usage,
  l.limit_value,
  l.is_unlimited,
  l.is_precise,
  l.current_ratio,
  p.peak_ratio_7d,
  p.peak_ratio_30d,
  p.peak_usage_7d,
  p.peak_usage_30d,
  p.observed_days,
  -- Headroom in absolute units, which is what you actually need to decide
  -- whether to file a quota increase. A percentage alone does not tell you
  -- whether 90% of 10 is a problem.
  SAFE_SUBTRACT(CAST(l.limit_value AS INT64), p.peak_usage_30d) AS headroom_30d
FROM `{table.rsplit(".", 1)[0]}.quota_latest` AS l
JOIN `{table.rsplit(".", 1)[0]}.quota_peaks` AS p
  USING (project_id, service, quota_metric, limit_name, location)
WHERE l.is_comparable
""",
        # Quotas whose recent week is materially worse than the three before
        # it. Ranking by ratio alone surfaces the same permanently-busy quotas
        # every day; ranking by change surfaces the ones that just started
        # moving, which is the actionable set.
        "quota_movers": f"""
WITH windowed AS (
  SELECT
    {_GRAIN},
    MAX(IF(usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY),
           peak_ratio, NULL)) AS recent,
    MAX(IF(usage_date_utc <  DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
       AND usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 28 DAY),
           peak_ratio, NULL)) AS baseline
  FROM `{table}`
  WHERE is_comparable
  GROUP BY {_GRAIN}
)
SELECT
  {_GRAIN},
  recent,
  baseline,
  recent - baseline AS delta,
  SAFE_DIVIDE(recent - baseline, baseline) AS relative_change
FROM windowed
WHERE recent IS NOT NULL
  AND baseline IS NOT NULL
  -- Ignore noise off a tiny base: going from 0.01% to 0.02% is a 100% rise
  -- and means nothing.
  AND recent >= 0.05
""",
        # Rollup for the hierarchy panel. Counts rather than averages: the mean
        # of a set of quota ratios is not a meaningful quantity, but "how many
        # quotas in this folder are above 80%" is.
        "quota_hierarchy": f"""
SELECT
  org_id,
  folder_id,
  project_id,
  MAX(quota_adjuster_enabled) AS quota_adjuster_enabled,
  COUNT(*) AS quotas_tracked,
  COUNTIF(peak_ratio_30d >= 0.9) AS critical_30d,
  COUNTIF(peak_ratio_30d >= 0.8 AND peak_ratio_30d < 0.9) AS warning_30d,
  MAX(peak_ratio_30d) AS worst_30d
FROM `{table.rsplit(".", 1)[0]}.quota_risk`
GROUP BY org_id, folder_id, project_id
""",
        # Everything the other views hide, and why. A dashboard that silently
        # drops 5% of its input is worse than one that shows you the 5%.
        "quota_quality": f"""
SELECT
  usage_date_utc,
  {_GRAIN},
  quota_class,
  limit_scope,
  interval_source,
  is_comparable,
  is_precise,
  is_unlimited,
  current_usage,
  daily_peak_usage,
  limit_value,
  flag
FROM `{table}`, UNNEST(IF(ARRAY_LENGTH(flags) = 0, ['OK'], flags)) AS flag
WHERE usage_date_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
""",
    }


# Order matters: quota_risk reads quota_latest and quota_peaks, and
# quota_hierarchy reads quota_risk.
ORDER = [
    "quota_latest",
    "quota_peaks",
    "quota_risk",
    "quota_movers",
    "quota_hierarchy",
    "quota_quality",
]


def ensure_views(client: bigquery.Client, project_id: str, dataset: str) -> list[str]:
    """Create or replace every view. Idempotent, and safe to run each deploy."""
    table = qualified(project_id, dataset, "quota_daily")
    definitions = _definitions(table)
    created = []
    for name in ORDER:
        view_id = qualified(project_id, dataset, name)
        view = bigquery.Table(view_id)
        view.view_query = definitions[name].strip()
        client.delete_table(view_id, not_found_ok=True)
        client.create_table(view)
        _LOG.info("created view %s", view_id)
        created.append(name)
    return created
