from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from island_quant.security.readiness import (
    ApprovalEvidence,
    ClockDriftGate,
    ControlPlaneThreatModel,
    EnablementStage,
    ProcessBoundaryPolicy,
    SecretReference,
    TwoStepEnablementPolicy,
)

NOW = datetime(2025, 1, 2, tzinfo=UTC)


def approval(name: str, role: str, offset: int) -> ApprovalEvidence:
    return ApprovalEvidence(name, role, NOW + timedelta(minutes=offset), f"evidence-{name}")


def test_secret_reference_contains_no_secret_value() -> None:
    assert set(SecretReference.__dataclass_fields__) == {
        "provider",
        "reference_name",
        "version_name",
    }


def test_process_boundary_forbids_public_inbound_and_open_permissions() -> None:
    with pytest.raises(ValueError, match="public inbound"):
        ProcessBoundaryPolicy("service", "service", 0o600, frozenset({"fixture.invalid"}), True)
    with pytest.raises(ValueError, match="permissions"):
        ProcessBoundaryPolicy("service", "service", 0o644, frozenset({"fixture.invalid"}), False)


def test_control_plane_threat_model_requires_every_control() -> None:
    assert ControlPlaneThreatModel(True, True, True, True, True, True).complete
    assert not ControlPlaneThreatModel(True, True, True, False, True, True).complete


def test_clock_drift_gate_blocks_excess_drift() -> None:
    within = ClockDriftGate(timedelta(seconds=1), timedelta(milliseconds=500), "fixture")
    beyond = ClockDriftGate(timedelta(seconds=1), timedelta(seconds=2), "fixture")
    assert within.permits_new_risk
    assert not beyond.permits_new_risk


def test_two_step_enablement_requires_independent_approvers() -> None:
    request = approval("operator-a", "requester", 0)
    with pytest.raises(ValueError, match="independent"):
        TwoStepEnablementPolicy(
            "fixture-v1",
            EnablementStage.STAGED_CAPITAL,
            request,
            approval("operator-a", "approver", 1),
            NOW + timedelta(hours=1),
            Decimal("100"),
            Decimal("10"),
            Decimal("5"),
        )
