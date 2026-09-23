"""Provider-neutral production-data contracts and offline conformance checks.

This module performs no I/O and describes no real provider.  The fixture-oriented
validator deliberately fails closed when point-in-time coverage or entitlement is
not demonstrably complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _present(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


class ProductionDataCapability(StrEnum):
    PIT_INSTRUMENTS = "pit_instruments"
    CORPORATE_ACTION_REVISIONS = "corporate_action_revisions"
    TRADABILITY = "tradability"
    OFFICIAL_CALENDAR = "official_calendar"
    BENCHMARK_CONSTITUENTS = "benchmark_constituents"
    MARKET_CAP = "market_cap"
    STREAMING_SEQUENCE = "streaming_sequence"
    CORRECTION_NOTICES = "correction_notices"
    DELETION_NOTICES = "deletion_notices"


class TradabilityStatus(StrEnum):
    TRADABLE = "tradable"
    SUSPENDED = "suspended"
    RESUMED = "resumed"
    PRICE_LIMITED = "price_limited"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class CalendarStatus(StrEnum):
    OPEN = "open"
    HOLIDAY = "holiday"
    UNEXPECTED_CLOSURE = "unexpected_closure"
    DELAYED_OPEN = "delayed_open"
    EARLY_CLOSE = "early_close"


class StreamHealthState(StrEnum):
    HEALTHY = "healthy"
    STALE = "stale"
    GAP = "gap"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    UNKNOWN = "unknown"


class ConformanceCode(StrEnum):
    LATE_ANNOUNCEMENT = "late_announcement"
    SUPERSEDED_REVISION = "superseded_revision"
    INSTRUMENT_OUTSIDE_VALIDITY = "instrument_outside_validity"
    IDENTIFIER_MAPPING_MISSING = "identifier_mapping_missing"
    NOT_TRADABLE = "not_tradable"
    PRICE_LIMIT_EXCEPTION_UNKNOWN = "price_limit_exception_unknown"
    MARKET_CLOSED = "market_closed"
    BENCHMARK_CONSTITUENT_MISSING = "benchmark_constituent_missing"
    STREAM_STALE = "stream_stale"
    STREAM_SEQUENCE_GAP = "stream_sequence_gap"
    STREAM_DUPLICATE = "stream_duplicate"
    STREAM_OUT_OF_ORDER = "stream_out_of_order"
    ENTITLEMENT_EXPIRED = "entitlement_expired"
    RETENTION_VIOLATION = "retention_violation"
    PROVIDER_DISAGREEMENT = "provider_disagreement"
    SILENT_FALLBACK = "silent_fallback"
    INCOMPLETE_PIT_COVERAGE = "incomplete_pit_coverage"


@dataclass(frozen=True, slots=True)
class EntitlementReference:
    """Non-secret entitlement locator; no token, credential, or contract text."""

    reference_name: str
    entitlement_name: str

    def __post_init__(self) -> None:
        _present(self.reference_name, "reference_name")
        _present(self.entitlement_name, "entitlement_name")


@dataclass(frozen=True, slots=True)
class ProductionDataCapabilities:
    provider_name: str
    adapter_version: str
    schema_version: int
    capabilities: frozenset[ProductionDataCapability]
    markets: frozenset[str]
    assessed_at: datetime

    def __post_init__(self) -> None:
        _present(self.provider_name, "provider_name")
        _present(self.adapter_version, "adapter_version")
        if self.schema_version <= 0 or not self.markets:
            raise ValueError("schema_version must be positive and markets must not be empty")
        _aware(self.assessed_at, "assessed_at")

    def require(self, capability: ProductionDataCapability) -> None:
        if capability not in self.capabilities:
            raise ValueError(f"production data capability unavailable: {capability.value}")


@dataclass(frozen=True, slots=True)
class PITInstrumentRecord:
    instrument_id: str
    identifier: str
    identifier_scheme: str
    market: str
    valid_from: date
    valid_to: date | None
    announced_at: datetime
    available_at: datetime
    revision_id: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.instrument_id, "instrument_id"),
            (self.identifier, "identifier"),
            (self.identifier_scheme, "identifier_scheme"),
            (self.market, "market"),
            (self.revision_id, "revision_id"),
        ):
            _present(value, name)
        _aware(self.announced_at, "announced_at")
        _aware(self.available_at, "available_at")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from")
        if self.available_at < self.announced_at:
            raise ValueError("available_at cannot precede announced_at")

    def valid_on(self, day: date) -> bool:
        return day >= self.valid_from and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True, slots=True)
class CorporateActionRevision:
    action_id: str
    instrument_id: str
    action_type: str
    effective_date: date
    announced_at: datetime
    available_at: datetime
    revision: int
    supersedes_revision: int | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.action_id, "action_id"),
            (self.instrument_id, "instrument_id"),
            (self.action_type, "action_type"),
        ):
            _present(value, name)
        _aware(self.announced_at, "announced_at")
        _aware(self.available_at, "available_at")
        if self.revision <= 0 or (
            self.supersedes_revision is not None and self.supersedes_revision >= self.revision
        ):
            raise ValueError("corporate-action revision chain is invalid")
        if self.available_at < self.announced_at:
            raise ValueError("available_at cannot precede announced_at")


@dataclass(frozen=True, slots=True)
class TradabilityRecord:
    instrument_id: str
    session_date: date
    status: TradabilityStatus
    announced_at: datetime
    available_at: datetime
    lower_limit: Decimal | None = None
    upper_limit: Decimal | None = None
    exception_code: str | None = None

    def __post_init__(self) -> None:
        _present(self.instrument_id, "instrument_id")
        _aware(self.announced_at, "announced_at")
        _aware(self.available_at, "available_at")
        if self.available_at < self.announced_at:
            raise ValueError("available_at cannot precede announced_at")
        if (
            self.lower_limit is not None
            and self.upper_limit is not None
            and self.lower_limit > self.upper_limit
        ):
            raise ValueError("lower_limit cannot exceed upper_limit")


@dataclass(frozen=True, slots=True)
class OfficialCalendarRecord:
    market: str
    session_date: date
    status: CalendarStatus
    announced_at: datetime
    available_at: datetime
    revision_id: str

    def __post_init__(self) -> None:
        _present(self.market, "market")
        _present(self.revision_id, "revision_id")
        _aware(self.announced_at, "announced_at")
        _aware(self.available_at, "available_at")
        if self.available_at < self.announced_at:
            raise ValueError("available_at cannot precede announced_at")


@dataclass(frozen=True, slots=True)
class BenchmarkConstituentRecord:
    benchmark_id: str
    instrument_id: str
    effective_from: date
    effective_to: date | None
    weight: Decimal | None
    shares: Decimal | None
    market_cap: Decimal | None
    available_at: datetime
    revision_id: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.benchmark_id, "benchmark_id"),
            (self.instrument_id, "instrument_id"),
            (self.revision_id, "revision_id"),
        ):
            _present(value, name)
        _aware(self.available_at, "available_at")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot precede effective_from")
        for numeric_value in (self.weight, self.shares, self.market_cap):
            if numeric_value is not None and numeric_value < 0:
                raise ValueError("benchmark numeric values cannot be negative")


@dataclass(frozen=True, slots=True)
class StreamingObservation:
    stream_id: str
    event_id: str
    sequence: int
    event_time: datetime
    received_at: datetime
    checksum: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.stream_id, "stream_id"),
            (self.event_id, "event_id"),
            (self.checksum, "checksum"),
        ):
            _present(value, name)
        if self.sequence <= 0:
            raise ValueError("sequence must be positive")
        _aware(self.event_time, "event_time")
        _aware(self.received_at, "received_at")
        if self.received_at < self.event_time:
            raise ValueError("received_at cannot precede event_time")


@dataclass(frozen=True, slots=True)
class StreamingHealth:
    state: StreamHealthState
    observed_at: datetime
    last_event_at: datetime | None
    expected_sequence: int | None
    observed_sequence: int | None
    reason: str

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        if self.last_event_at is not None:
            _aware(self.last_event_at, "last_event_at")
        _present(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class RetentionLicensePolicy:
    policy_version: str
    entitlement: EntitlementReference
    valid_from: datetime
    valid_until: datetime
    maximum_retention: timedelta
    backup_permitted: bool
    derived_use_permitted: bool
    deletion_required: bool

    def __post_init__(self) -> None:
        _present(self.policy_version, "policy_version")
        _aware(self.valid_from, "valid_from")
        _aware(self.valid_until, "valid_until")
        if self.valid_until <= self.valid_from or self.maximum_retention <= timedelta(0):
            raise ValueError("license validity and retention must be positive")


@dataclass(frozen=True, slots=True)
class CoverageRequirement:
    dataset: str
    required_fields: frozenset[str]
    maximum_latency: timedelta
    history_start: date
    minimum_coverage: Decimal
    entitlement: EntitlementReference

    def __post_init__(self) -> None:
        _present(self.dataset, "dataset")
        if not self.required_fields or self.maximum_latency <= timedelta(0):
            raise ValueError("coverage fields and latency are required")
        if not Decimal("0") <= self.minimum_coverage <= Decimal("1"):
            raise ValueError("minimum_coverage must be between zero and one")


@dataclass(frozen=True, slots=True)
class CoverageReport:
    dataset: str
    expected_records: int
    observed_records: int
    missing_fields: frozenset[str]
    maximum_observed_latency: timedelta
    entitlement_valid: bool
    pit_complete: bool

    @property
    def coverage_ratio(self) -> Decimal:
        if self.expected_records <= 0:
            return Decimal("0")
        return Decimal(self.observed_records) / Decimal(self.expected_records)

    def accepts(self, requirement: CoverageRequirement) -> bool:
        return (
            self.dataset == requirement.dataset
            and not self.missing_fields
            and self.maximum_observed_latency <= requirement.maximum_latency
            and self.entitlement_valid
            and self.pit_complete
            and self.coverage_ratio >= requirement.minimum_coverage
        )


@dataclass(frozen=True, slots=True)
class ProviderValue:
    provider_name: str
    record_key: str
    value_checksum: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    record_key: str
    provider_checksums: tuple[ProviderValue, ...]
    agrees: bool
    selected_provider: str | None
    operator_decision_required: bool


def reconcile_provider_values(values: tuple[ProviderValue, ...]) -> ReconciliationReport:
    if not values:
        raise ValueError("provider reconciliation requires observations")
    keys = {value.record_key for value in values}
    if len(keys) != 1:
        raise ValueError("provider observations must share one record_key")
    agrees = len({value.value_checksum for value in values}) == 1
    return ReconciliationReport(
        record_key=values[0].record_key,
        provider_checksums=values,
        agrees=agrees,
        selected_provider=values[0].provider_name if agrees else None,
        operator_decision_required=not agrees,
    )


def assess_stream(
    observations: tuple[StreamingObservation, ...],
    *,
    observed_at: datetime,
    maximum_age: timedelta,
) -> StreamingHealth:
    _aware(observed_at, "observed_at")
    if not observations:
        return StreamingHealth(StreamHealthState.UNKNOWN, observed_at, None, None, None, "empty")
    seen_ids: set[str] = set()
    previous = observations[0].sequence - 1
    for observation in observations:
        if observation.event_id in seen_ids:
            return StreamingHealth(
                StreamHealthState.DUPLICATE,
                observed_at,
                observation.event_time,
                previous + 1,
                observation.sequence,
                "duplicate event identity",
            )
        seen_ids.add(observation.event_id)
        if observation.sequence < previous:
            return StreamingHealth(
                StreamHealthState.OUT_OF_ORDER,
                observed_at,
                observation.event_time,
                previous + 1,
                observation.sequence,
                "sequence moved backwards",
            )
        if observation.sequence > previous + 1:
            return StreamingHealth(
                StreamHealthState.GAP,
                observed_at,
                observation.event_time,
                previous + 1,
                observation.sequence,
                "sequence gap",
            )
        previous = observation.sequence
    last = observations[-1]
    if observed_at - last.event_time > maximum_age:
        return StreamingHealth(
            StreamHealthState.STALE,
            observed_at,
            last.event_time,
            last.sequence + 1,
            last.sequence,
            "freshness threshold exceeded",
        )
    return StreamingHealth(
        StreamHealthState.HEALTHY,
        observed_at,
        last.event_time,
        last.sequence + 1,
        last.sequence,
        "sequence and freshness checks passed",
    )


def effective_corporate_actions(
    actions: tuple[CorporateActionRevision, ...], decision_time: datetime
) -> tuple[CorporateActionRevision, ...]:
    """Return only the latest revision visible at a point in time."""

    _aware(decision_time, "decision_time")
    latest: dict[str, CorporateActionRevision] = {}
    for action in actions:
        if action.available_at > decision_time:
            continue
        previous = latest.get(action.action_id)
        if previous is None or action.revision > previous.revision:
            latest[action.action_id] = action
    return tuple(sorted(latest.values(), key=lambda item: item.action_id))


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    passed: bool
    violations: frozenset[ConformanceCode]
    silent_fallback_used: bool = False


def evaluate_offline_conformance(
    *,
    decision_time: datetime,
    instrument: PITInstrumentRecord | None,
    session_date: date,
    corporate_actions: tuple[CorporateActionRevision, ...],
    tradability: TradabilityRecord | None,
    calendar: OfficialCalendarRecord | None,
    benchmark_required: bool,
    benchmark: BenchmarkConstituentRecord | None,
    stream_health: StreamingHealth,
    license_policy: RetentionLicensePolicy,
    oldest_retained_at: datetime,
    reconciliation: ReconciliationReport,
    coverage: CoverageReport,
    coverage_requirement: CoverageRequirement,
    fallback_provider: str | None = None,
) -> ConformanceResult:
    """Evaluate an intentionally synthetic snapshot with fail-closed semantics."""

    _aware(decision_time, "decision_time")
    _aware(oldest_retained_at, "oldest_retained_at")
    violations: set[ConformanceCode] = set()
    if instrument is None or not instrument.valid_on(session_date):
        violations.add(ConformanceCode.INSTRUMENT_OUTSIDE_VALIDITY)
    elif not instrument.identifier:
        violations.add(ConformanceCode.IDENTIFIER_MAPPING_MISSING)
    if any(
        action.effective_date <= session_date and action.available_at > decision_time
        for action in corporate_actions
    ):
        violations.add(ConformanceCode.LATE_ANNOUNCEMENT)
    if tradability is None or tradability.status is not TradabilityStatus.TRADABLE:
        violations.add(ConformanceCode.NOT_TRADABLE)
    if (
        tradability is not None
        and tradability.status is TradabilityStatus.PRICE_LIMITED
        and tradability.exception_code is None
    ):
        violations.add(ConformanceCode.PRICE_LIMIT_EXCEPTION_UNKNOWN)
    if calendar is None or calendar.status is not CalendarStatus.OPEN:
        violations.add(ConformanceCode.MARKET_CLOSED)
    if benchmark_required and benchmark is None:
        violations.add(ConformanceCode.BENCHMARK_CONSTITUENT_MISSING)
    stream_codes = {
        StreamHealthState.STALE: ConformanceCode.STREAM_STALE,
        StreamHealthState.GAP: ConformanceCode.STREAM_SEQUENCE_GAP,
        StreamHealthState.DUPLICATE: ConformanceCode.STREAM_DUPLICATE,
        StreamHealthState.OUT_OF_ORDER: ConformanceCode.STREAM_OUT_OF_ORDER,
        StreamHealthState.UNKNOWN: ConformanceCode.INCOMPLETE_PIT_COVERAGE,
    }
    if stream_health.state in stream_codes:
        violations.add(stream_codes[stream_health.state])
    if not license_policy.valid_from <= decision_time <= license_policy.valid_until:
        violations.add(ConformanceCode.ENTITLEMENT_EXPIRED)
    if decision_time - oldest_retained_at > license_policy.maximum_retention:
        violations.add(ConformanceCode.RETENTION_VIOLATION)
    if not reconciliation.agrees:
        violations.add(ConformanceCode.PROVIDER_DISAGREEMENT)
    if fallback_provider is not None:
        violations.add(ConformanceCode.SILENT_FALLBACK)
    if not coverage.accepts(coverage_requirement):
        violations.add(ConformanceCode.INCOMPLETE_PIT_COVERAGE)
    return ConformanceResult(not violations, frozenset(violations), fallback_provider is not None)


@runtime_checkable
class ProductionDataProvider(Protocol):
    def capabilities(self) -> ProductionDataCapabilities: ...

    def entitlement(self) -> EntitlementReference: ...

    def coverage(self, requirement: CoverageRequirement) -> CoverageReport: ...
