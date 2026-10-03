"""Command line entry point.

Three subcommands:

``collect``  -- daily run: gather usage + limits, roll up, write to BigQuery.
``backfill`` -- same, over a wider window, for first-time setup.
``verify``   -- print the full derivation for a sample of quotas so a human can
                reconcile them against the Cloud Console. This exists because
                the previous generation of this tool published wrong numbers
                confidently and nobody could tell where they came from.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from collections import Counter, defaultdict

from .model import DailyRollup, QuotaDefinition
from .rollup import UsageBundle, build_rollups
from .sinks.bigquery import Placement
from .sources.cloud_quotas import CloudQuotasSource
from .sources.hierarchy import HierarchySource
from .sources.monitoring import MonitoringSource

_LOG = logging.getLogger("qms")


def utc_midnight(offset_days: int = 0) -> dt.datetime:
    today = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - dt.timedelta(days=offset_days)


def gather(
    *,
    projects: list[str],
    billing_project: str,
    days: int,
) -> tuple[UsageBundle, dict[tuple[str, str], list[QuotaDefinition]]]:
    end = utc_midnight()
    start = utc_midnight(days)

    quotas = CloudQuotasSource(billing_project=billing_project)
    allocation: list = []
    rate_daily: list = []
    rate_minute: list = []
    definitions: dict[tuple[str, str], list[QuotaDefinition]] = {}

    for project_id in projects:
        _LOG.info("collecting %s", project_id)
        monitoring = MonitoringSource(project_id)
        try:
            allocation.extend(monitoring.allocation_daily_peaks(start=start, end=end))
            rate_daily.extend(monitoring.rate_daily_totals(start=start, end=end))
            rate_minute.extend(monitoring.rate_minute_peaks(start=start, end=end))
        except Exception as exc:  # noqa: BLE001 - see below
            # Intentionally broad. In an org-wide sweep a single project with a
            # transient API error, a missing API, or a permissions gap must not
            # abort collection for every other project.
            _LOG.error("monitoring read failed for %s: %s", project_id, exc)
            continue

    bundle = UsageBundle(allocation, rate_daily, rate_minute)

    # Usage-driven fan-out: ask Cloud Quotas only about services that actually
    # showed usage in the window. Deriving the service list from the samples
    # just collected -- rather than a separate instant query -- avoids both an
    # extra round trip and a silent mismatch: an instant query only looks back
    # five minutes, and these metrics are written sporadically enough that it
    # routinely returns nothing.
    for owner, service in sorted(active_services(bundle)):
        key = (owner, service)
        if key in definitions:
            continue
        definitions[key] = quotas.list_quota_infos(f"projects/{owner}", service)

    _LOG.info(
        "fetched %d quota definitions across %d (project, service) pairs",
        sum(len(v) for v in definitions.values()),
        len(definitions),
    )
    return bundle, definitions


def active_services(bundle: UsageBundle) -> set[tuple[str, str]]:
    """``(project_id, service)`` pairs present anywhere in the collected usage."""
    pairs: set[tuple[str, str]] = set()
    for samples in (
        bundle.allocation_peaks,
        bundle.rate_daily_totals,
        bundle.rate_minute_peaks,
    ):
        for sample in samples:
            pairs.add((sample.key.project_id, sample.key.service))
    return pairs


def cmd_collect(args: argparse.Namespace) -> int:
    projects, placements = resolve_projects(args)
    bundle, definitions = gather(
        projects=projects, billing_project=args.billing_project, days=args.days
    )
    rows = build_rollups(bundle, definitions)
    _LOG.info("built %d rows", len(rows))
    summarise(rows)

    if args.dry_run:
        print("\n-- dry run, nothing written --")
        return 0

    from .sinks.bigquery import BigQuerySink

    sink = BigQuerySink(args.billing_project, args.dataset, location=args.bq_location)
    sink.ensure_table()
    sink.delete_days(sorted({row.usage_date_utc for row in rows}))
    sink.write(rows, collected_at=dt.datetime.now(dt.UTC), placements=placements)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Print the full derivation of each ratio so it can be checked by hand."""
    projects, _ = resolve_projects(args)
    bundle, definitions = gather(
        projects=projects, billing_project=args.billing_project, days=args.days
    )
    rows = build_rollups(bundle, definitions)

    latest: dict[tuple, DailyRollup] = {}
    for row in rows:
        key = row.key.as_tuple()
        if key not in latest or row.usage_date_utc > latest[key].usage_date_utc:
            latest[key] = row

    selected = sorted(
        latest.values(),
        key=lambda r: (r.peak_ratio is None, -(r.peak_ratio or 0)),
    )
    if args.filter:
        needle = args.filter.lower()
        selected = [
            r
            for r in selected
            if needle in r.key.quota_metric.lower() or needle in r.key.limit_name.lower()
        ]
    selected = selected[: args.limit]

    for row in selected:
        print("=" * 78)
        print(f"{row.key.project_id}  {row.key.quota_metric}")
        print(f"  limit_name      : {row.key.limit_name or '(none found)'}")
        print(f"  location        : {row.key.location}")
        print(f"  quota class     : {row.quota_class.value}")
        print(f"  limit scope     : {row.scope.value}")
        print(
            f"  interval        : {row.interval_seconds}s (source: {row.interval_source.value})"
        )
        print(f"  window boundary : {row.window_boundary}")
        print(f"  date (UTC)      : {row.usage_date_utc}")
        print(f"  numerator (peak): {row.daily_peak_usage}")
        print(f"  numerator (curr): {row.current_usage}")
        print(f"  denominator     : {row.limit_value}  unlimited={row.is_unlimited}")
        ratio = row.peak_ratio
        print(f"  peak ratio      : {'n/a' if ratio is None else f'{ratio:.4%}'}")
        if row.flags:
            print(f"  flags           : {', '.join(f.value for f in row.flags)}")
        if not row.comparable:
            print("  -> no ratio published; see flags above")
    print("=" * 78)
    print(f"{len(selected)} shown of {len(latest)} buckets")
    summarise(rows)
    return 0


def summarise(rows: list[DailyRollup]) -> None:
    flag_counts: Counter[str] = Counter()
    for row in rows:
        for flag in row.flags:
            flag_counts[flag.value] += 1
    comparable = sum(1 for r in rows if r.comparable)

    print("\n-- data quality --")
    print(f"  rows            : {len(rows)}")
    print(f"  comparable      : {comparable}")
    print(f"  not comparable  : {len(rows) - comparable}")
    for flag, count in flag_counts.most_common():
        print(f"    {flag:<32} {count}")

    by_project: dict[str, int] = defaultdict(int)
    for row in rows:
        by_project[row.key.project_id] += 1
    print("  rows per project:")
    for project, count in sorted(by_project.items()):
        print(f"    {project:<40} {count}")


def cmd_views(args: argparse.Namespace) -> int:
    """(Re)create the views the dashboard reads.

    Separate from ``collect`` because the two change on different schedules:
    the data is refreshed daily, the view definitions only when we change the
    SQL. Running it is cheap and idempotent, so the deploy does it every time.
    """
    from google.cloud import bigquery

    from .sinks.views import ensure_views

    client = bigquery.Client(project=args.billing_project, location=args.bq_location)
    created = ensure_views(client, args.billing_project, args.dataset)
    for name in created:
        print(f"  {args.billing_project}.{args.dataset}.{name}")
    print(f"{len(created)} views ready")
    return 0


def resolve_projects(args: argparse.Namespace) -> tuple[list[str], dict[str, Placement]]:
    """Return the projects to scan, plus where each one sits in the hierarchy.

    The placement map is what lets the dashboard roll up by folder and org. It
    is only available when we discovered the projects ourselves; an explicit
    ``--projects`` list tells us nothing about the tree, and walking it just to
    label two projects is not worth the API calls.
    """
    if args.projects:
        return args.projects, {}
    if args.organization:
        hierarchy = HierarchySource(billing_project=args.billing_project)
        nodes = hierarchy.walk(f"organizations/{args.organization}")
        _LOG.info("discovered %d active projects in org %s", len(nodes), args.organization)
        placements = {
            node.project_id: Placement(
                org_id=node.org_id,
                folder_id=node.folder_id,
                project_number=node.project_number,
            )
            for node in nodes
        }
        return [node.project_id for node in nodes], placements
    return [args.billing_project], {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qms", description=__doc__)
    parser.add_argument(
        "--billing-project",
        default=os.environ.get("QMS_PROJECT", ""),
        help="project hosting the dataset and billing the API calls",
    )
    parser.add_argument("--organization", default=os.environ.get("QMS_ORG", ""))
    parser.add_argument("--projects", nargs="*", default=None)
    parser.add_argument("--dataset", default=os.environ.get("QMS_DATASET", "quota_monitoring"))
    parser.add_argument(
        "--bq-location",
        default=os.environ.get("QMS_BQ_LOCATION", "US"),
        help="dataset location; cannot be changed after the dataset is created",
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="collect and write to BigQuery")
    collect.add_argument("--dry-run", action="store_true")
    collect.set_defaults(func=cmd_collect, days=2)

    backfill = sub.add_parser("backfill", help="collect a wide window")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.set_defaults(func=cmd_collect)

    verify = sub.add_parser("verify", help="print ratio derivations for reconciliation")
    verify.add_argument("--limit", type=int, default=20)
    verify.add_argument("--filter", default="")
    verify.set_defaults(func=cmd_verify)

    views = sub.add_parser("views", help="create or replace the dashboard's BigQuery views")
    views.set_defaults(func=cmd_views)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.billing_project:
        parser.error("--billing-project (or QMS_PROJECT) is required")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
