"""BigQuery sink.

Writes via load jobs rather than the streaming API. Load jobs are free and
this workload is a once-a-day batch, so streaming would add cost (roughly 2x
the Storage Write API, which is itself not free) for latency nobody asked for.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

from google.cloud import bigquery

from ..model import DailyRollup

_LOG = logging.getLogger(__name__)

TABLE_ID = "quota_daily"

# BigQuery cannot move a dataset after creation, so this is worth getting right
# the first time. Co-locating the dataset with the Cloud Run region avoids
# cross-region reads on every dashboard page load.
DEFAULT_LOCATION = os.environ.get("QMS_BQ_LOCATION", "US")

# `project.dataset.table`, where each part is restricted to the characters
# BigQuery actually permits in an identifier.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2}")

SCHEMA = [
    bigquery.SchemaField("usage_date_utc", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("usage_date_local", "DATE"),
    bigquery.SchemaField("window_boundary", "STRING"),
    bigquery.SchemaField("collected_at", "TIMESTAMP", mode="REQUIRED"),
    # Hierarchy
    bigquery.SchemaField("org_id", "STRING"),
    bigquery.SchemaField("folder_id", "STRING"),
    bigquery.SchemaField("project_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("project_number", "STRING"),
    # Quota identity -- the full grain. v5 keyed on only three of these.
    bigquery.SchemaField("service", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("quota_metric", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("limit_name", "STRING"),
    bigquery.SchemaField("location", "STRING"),
    # Semantics
    bigquery.SchemaField("quota_class", "STRING"),
    bigquery.SchemaField("limit_scope", "STRING"),
    bigquery.SchemaField("interval_seconds", "INT64"),
    bigquery.SchemaField("interval_source", "STRING"),
    # Measurements. Ratios are stored as NULL rather than a misleading number
    # whenever the pairing is not comparable.
    bigquery.SchemaField("current_usage", "FLOAT64"),
    bigquery.SchemaField("daily_peak_usage", "FLOAT64"),
    bigquery.SchemaField("limit_value", "NUMERIC"),
    bigquery.SchemaField("is_unlimited", "BOOL"),
    bigquery.SchemaField("is_precise", "BOOL"),
    bigquery.SchemaField("current_ratio", "FLOAT64"),
    bigquery.SchemaField("peak_ratio", "FLOAT64"),
    bigquery.SchemaField("is_comparable", "BOOL"),
    bigquery.SchemaField("flags", "STRING", mode="REPEATED"),
    bigquery.SchemaField("quota_adjuster_enabled", "BOOL"),
]


@dataclass(frozen=True)
class Placement:
    """Where a project sits in the resource hierarchy.

    Kept as a plain record rather than importing ``ProjectNode`` so the sink
    does not depend on the discovery source; the collector can equally well be
    handed a placement map from configuration.
    """

    org_id: str | None
    folder_id: str | None
    project_number: str | None
    quota_adjuster_enabled: bool | None = None


class BigQuerySink:
    def __init__(
        self,
        project_id: str,
        dataset: str,
        *,
        location: str = DEFAULT_LOCATION,
    ) -> None:
        self.project_id = project_id
        self.dataset = dataset
        self.location = location
        self._client = bigquery.Client(project=project_id, location=location)

    @property
    def table_ref(self) -> str:
        return f"{self.project_id}.{self.dataset}.{TABLE_ID}"

    def ensure_table(self, *, retention_days: int = 400) -> None:
        dataset_ref = bigquery.Dataset(f"{self.project_id}.{self.dataset}")
        dataset_ref.location = self.location
        self._client.create_dataset(dataset_ref, exists_ok=True)

        table = bigquery.Table(self.table_ref, schema=SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="usage_date_utc",
            # v5 set this to one day, which made any 30-day peak impossible.
            expiration_ms=retention_days * 86_400_000,
        )
        table.clustering_fields = ["project_id", "service", "quota_metric", "limit_name"]
        created = self._client.create_table(table, exists_ok=True)

        # Schema evolution: create_table(..., exists_ok=True) returns an
        # existing table untouched, so add any newly introduced NULLABLE
        # columns (e.g. quota_adjuster_enabled) in place.
        existing_names = {field.name for field in created.schema}
        missing = [field for field in SCHEMA if field.name not in existing_names]
        if missing:
            created.schema = [*list(created.schema), *missing]
            self._client.update_table(created, ["schema"])
            _LOG.info(
                "added %d new column(s) to %s: %s",
                len(missing),
                self.table_ref,
                ", ".join(f.name for f in missing),
            )

    def write(
        self,
        rows: Iterable[DailyRollup],
        *,
        collected_at: dt.datetime,
        placements: dict[str, Placement] | None = None,
    ) -> int:
        placements = placements or {}
        payload = [
            _to_json(row, collected_at, placements.get(row.key.project_id)) for row in rows
        ]
        if not payload:
            _LOG.warning("no rows to write")
            return 0

        job_config = bigquery.LoadJobConfig(
            schema=SCHEMA,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            # The job re-collects whole days, so replacing the affected
            # partitions keeps re-runs idempotent.
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        body = "\n".join(json.dumps(record) for record in payload).encode()

        import io

        job = self._client.load_table_from_file(
            io.BytesIO(body), self.table_ref, job_config=job_config
        )
        job.result()
        _LOG.info("loaded %d rows into %s", len(payload), self.table_ref)
        return len(payload)

    def delete_days(self, days: list[dt.date]) -> None:
        """Clear partitions before a re-collection so re-runs stay idempotent."""
        if not days:
            return
        # The only interpolated value is the table reference, which
        # _validated_table_ref restricts to `project.dataset.table`.
        # Identifiers cannot be parameterised in BigQuery; the dates can be,
        # and are.
        table = self._validated_table_ref()
        query = f"DELETE FROM `{table}` WHERE usage_date_utc IN UNNEST(@days)"  # noqa: S608
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("days", "DATE", days)]
        )
        self._client.query(query, job_config=job_config).result()

    def _validated_table_ref(self) -> str:
        """BigQuery cannot parameterise identifiers, so validate instead.

        Project, dataset and table names are restricted to letters, digits,
        underscores and hyphens, which leaves no way to escape the backticks.
        """
        if not _IDENTIFIER_RE.fullmatch(self.table_ref):
            raise ValueError(f"unsafe BigQuery table reference: {self.table_ref!r}")
        return self.table_ref


def _to_json(
    row: DailyRollup,
    collected_at: dt.datetime,
    placement: Placement | None,
) -> dict:
    return {
        "usage_date_utc": row.usage_date_utc.isoformat(),
        "usage_date_local": row.usage_date_local.isoformat() if row.usage_date_local else None,
        "window_boundary": row.window_boundary,
        "collected_at": collected_at.isoformat(),
        "org_id": placement.org_id if placement else None,
        "folder_id": placement.folder_id if placement else None,
        "project_id": row.key.project_id,
        "project_number": placement.project_number if placement else None,
        "service": row.key.service,
        "quota_metric": row.key.quota_metric,
        "limit_name": row.key.limit_name or None,
        "location": row.key.location,
        "quota_class": row.quota_class.value,
        "limit_scope": row.scope.value,
        "interval_seconds": row.interval_seconds,
        "interval_source": row.interval_source.value,
        "current_usage": row.current_usage,
        "daily_peak_usage": row.daily_peak_usage,
        "limit_value": str(row.limit_value) if row.limit_value is not None else None,
        "is_unlimited": row.is_unlimited,
        "is_precise": row.is_precise,
        "current_ratio": row.current_ratio,
        "peak_ratio": row.peak_ratio,
        "is_comparable": row.comparable,
        "flags": [flag.value for flag in row.flags],
        "quota_adjuster_enabled": placement.quota_adjuster_enabled if placement else None,
    }
