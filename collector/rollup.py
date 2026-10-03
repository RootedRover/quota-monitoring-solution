"""Join usage series to quota definitions and produce one row per bucket per day.

This module is where v5's headline defect lived. Its Java equivalent allocated
a single mutable accumulator outside the per-series loop, so the running max
of one quota metric leaked onto every other metric in the same project. The
structural defences here are:

* every accumulator is created inside :func:`_accumulate`, keyed by
  ``(QuotaKey, date)``, and is never shared;
* :class:`~collector.model.UsageKey` and :class:`~collector.model.QuotaKey` are
  different types, so a usage series cannot be used as an output key by
  accident;
* a usage series that matches several limits is fanned out explicitly, and
  each pairing is independently checked for comparability.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass

from .model import (
    DailyRollup,
    DataQualityFlag,
    IntervalSource,
    LimitScope,
    QuotaClass,
    QuotaDefinition,
    QuotaKey,
    UsageSample,
)
from .normalise import (
    classify_limit,
    interval_flags,
    is_unlimited,
    plausibility_flags,
    scale_usage_to_interval,
)

_LOG = logging.getLogger(__name__)

# GCP per-day quotas reset at midnight US/Pacific, not UTC. Rows carry both so
# the dashboard can be explicit about which boundary a peak was computed on.
PACIFIC = dt.timezone(dt.timedelta(hours=-8))


@dataclass
class _Accumulator:
    """Per-bucket, per-day state. One instance per (QuotaKey, date)."""

    peak: float | None = None
    current: float | None = None
    current_at: dt.datetime | None = None

    def observe(self, value: float, at: dt.datetime) -> None:
        self.peak = value if self.peak is None else max(self.peak, value)
        if self.current_at is None or at >= self.current_at:
            self.current = value
            self.current_at = at


@dataclass(frozen=True)
class UsageBundle:
    """The three numerators, kept separate because they are not interchangeable.

    ``allocation_peaks`` is a level; ``rate_daily_totals`` is consumption over
    a whole day; ``rate_minute_peaks`` is the largest single minute in a day.
    Which one is correct depends entirely on the limit's enforcement interval.
    """

    allocation_peaks: list[UsageSample]
    rate_daily_totals: list[UsageSample]
    rate_minute_peaks: list[UsageSample]


def build_rollups(
    usage: UsageBundle,
    definitions_by_service: dict[tuple[str, str], list[QuotaDefinition]],
) -> list[DailyRollup]:
    """Produce one :class:`DailyRollup` per (quota bucket, day).

    ``definitions_by_service`` is keyed by ``(project_id, service)``.
    """
    index = _index_definitions(definitions_by_service)

    accumulators: dict[tuple[QuotaKey, dt.date], _Accumulator] = {}
    context: dict[tuple[QuotaKey, dt.date], _RowContext] = {}

    _accumulate(
        usage.allocation_peaks,
        index,
        accumulators,
        context,
        accepts=lambda d: d.quota_class in (QuotaClass.ALLOCATION, QuotaClass.CONCURRENT),
        measured_over=None,
    )
    _accumulate(
        usage.rate_daily_totals,
        index,
        accumulators,
        context,
        accepts=lambda d: d.quota_class is QuotaClass.RATE
        and d.interval_seconds is not None
        and d.interval_seconds >= 3600,
        measured_over=86400,
    )
    _accumulate(
        usage.rate_minute_peaks,
        index,
        accumulators,
        context,
        accepts=lambda d: d.quota_class is QuotaClass.RATE
        and d.interval_seconds is not None
        and d.interval_seconds < 3600,
        measured_over=60,
    )

    rows: list[DailyRollup] = []
    for (key, day), accumulator in accumulators.items():
        rows.append(_finalise(key, day, accumulator, context[(key, day)]))
    return rows


@dataclass(frozen=True)
class _RowContext:
    definition: QuotaDefinition
    limit_value: int | None


def _index_definitions(
    definitions_by_service: dict[tuple[str, str], list[QuotaDefinition]],
) -> dict[tuple[str, str, str], list[QuotaDefinition]]:
    """``(project_id, service, quota_metric) -> definitions``.

    A quota metric legitimately maps to several definitions -- e.g.
    ``dns.googleapis.com/default`` has both a per-day-per-project limit and a
    per-minute-per-user one.
    """
    index: dict[tuple[str, str, str], list[QuotaDefinition]] = defaultdict(list)
    for (project_id, service), definitions in definitions_by_service.items():
        for definition in definitions:
            if not definition.quota_metric:
                continue
            index[(project_id, service, definition.quota_metric)].append(definition)
    return index


def _accumulate(
    samples: list[UsageSample],
    index: dict[tuple[str, str, str], list[QuotaDefinition]],
    accumulators: dict[tuple[QuotaKey, dt.date], _Accumulator],
    context: dict[tuple[QuotaKey, dt.date], _RowContext],
    *,
    accepts,
    measured_over: int | None,
) -> None:
    for sample in samples:
        usage_key = sample.key
        candidates = index.get(
            (usage_key.project_id, usage_key.service, usage_key.quota_metric), []
        )
        matched = [d for d in candidates if accepts(d)]

        if not matched:
            if candidates:
                # The metric is known but no definition matches this numerator
                # shape; another call in this run will handle it.
                continue
            _record_unmatched(sample, accumulators, context)
            continue

        for definition in matched:
            value = _rescale(sample.value, measured_over, definition)
            if value is None:
                continue
            key = QuotaKey(
                project_id=usage_key.project_id,
                service=usage_key.service,
                quota_metric=usage_key.quota_metric,
                limit_name=definition.quota_id,
                location=usage_key.location,
            )
            day = sample.observed_at.astimezone(dt.UTC).date()
            slot = (key, day)
            # Fresh accumulator per bucket per day -- never shared.
            accumulators.setdefault(slot, _Accumulator()).observe(
                value, sample.observed_at
            )
            context.setdefault(
                slot,
                _RowContext(
                    definition=definition,
                    limit_value=definition.value_for(usage_key.location),
                ),
            )


def _rescale(
    value: float, measured_over: int | None, definition: QuotaDefinition
) -> float | None:
    """Put the numerator onto the limit's enforcement interval.

    Returns the value unchanged for allocation quotas and for the common cases
    where the query already used the right window (per-minute and per-day).
    """
    if measured_over is None or definition.interval_seconds is None:
        return value
    if measured_over == definition.interval_seconds:
        return value
    return scale_usage_to_interval(
        value,
        measured_over_seconds=measured_over,
        limit_interval_seconds=definition.interval_seconds,
    )


def _record_unmatched(
    sample: UsageSample,
    accumulators: dict[tuple[QuotaKey, dt.date], _Accumulator],
    context: dict[tuple[QuotaKey, dt.date], _RowContext],
) -> None:
    """Keep usage that has no known limit, flagged rather than silently dropped.

    These rows are what make the data-quality panel honest: v5 simply lost them.
    """
    key = QuotaKey(
        project_id=sample.key.project_id,
        service=sample.key.service,
        quota_metric=sample.key.quota_metric,
        limit_name="",
        location=sample.key.location,
    )
    day = sample.observed_at.astimezone(dt.UTC).date()
    slot = (key, day)
    accumulators.setdefault(slot, _Accumulator()).observe(
        sample.value, sample.observed_at
    )
    context.setdefault(
        slot,
        _RowContext(
            definition=QuotaDefinition(
                service=sample.key.service,
                quota_id="",
                quota_metric=sample.key.quota_metric,
                quota_class=QuotaClass.ALLOCATION,
                interval_seconds=None,
                interval_source=IntervalSource.UNKNOWN,
                scope=LimitScope.OTHER,
            ),
            limit_value=None,
        ),
    )


def _finalise(
    key: QuotaKey,
    day: dt.date,
    accumulator: _Accumulator,
    row_context: _RowContext,
) -> DailyRollup:
    definition = row_context.definition
    limit_value = row_context.limit_value

    flags: list[DataQualityFlag] = []
    if not definition.quota_id:
        flags.append(DataQualityFlag.USAGE_WITHOUT_LIMIT)
    flags.extend(interval_flags(definition.interval_source))
    flags.extend(
        classify_limit(
            limit_value,
            scope=definition.scope,
            is_precise=definition.is_precise,
        )
    )

    row = DailyRollup(
        key=key,
        usage_date_utc=day,
        usage_date_local=_pacific_date(day, definition),
        window_boundary="US/Pacific" if _is_daily(definition) else "UTC",
        quota_class=definition.quota_class,
        interval_seconds=definition.interval_seconds,
        interval_source=definition.interval_source,
        scope=definition.scope,
        current_usage=accumulator.current,
        daily_peak_usage=accumulator.peak,
        limit_value=limit_value,
        is_unlimited=is_unlimited(limit_value),
        is_precise=definition.is_precise,
        flags=flags,
    )
    # Computed last: peak_ratio consults the flags set above.
    row.flags.extend(plausibility_flags(row.peak_ratio))
    return row


def _is_daily(definition: QuotaDefinition) -> bool:
    return definition.interval_seconds == 86400


def _pacific_date(day: dt.date, definition: QuotaDefinition) -> dt.date | None:
    if not _is_daily(definition):
        return None
    return day
