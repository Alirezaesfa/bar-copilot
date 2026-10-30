"""Unit tests for pure calculation functions in tools.bar_tools."""

from decimal import Decimal
from tools.bar_tools import (
    calc_rate_error,
    calc_sla_availability,
    calc_sla_credit_pct,
    calc_sla_credit,
    calc_etf,
    calc_approval_level,
)


def test_pol_101_rate_error_worked_example():
    """POL-101 §3 worked example: (110-100) × 20 × 0.90 × 3 periods = 540.00."""
    per_period = calc_rate_error(
        invoiced_price=Decimal("110"),
        contract_price=Decimal("100"),
        quantity=Decimal("20"),
        discount_pct=Decimal("10"),
    )
    total = per_period * 3
    assert total == Decimal("540.00")


def test_pol_102_sla_credit_worked_example():
    """POL-102 §7 worked example: shortfall 0.594 → 10% tier → 22.00 × 40 × 10% = 88.00."""
    shortfall = Decimal("0.594")
    credit_pct = calc_sla_credit_pct(shortfall)
    assert credit_pct == Decimal("10")

    credit = calc_sla_credit(
        unit_price=Decimal("22.00"),
        affected_units=Decimal("40"),
        discount_pct=Decimal("0"),
        credit_pct=credit_pct,
    )
    assert credit == Decimal("88.00")


def test_pol_105_etf_worked_example():
    """POL-105 §1 worked example: 50% × 400.00 × 6 = 1200.00."""
    result = calc_etf(net_mrc=Decimal("400.00"), remaining_months=Decimal("6"))
    assert result == Decimal("1200.00")


def test_pol_001_approval_level_thresholds():
    """POL-001 §1: ≤20000 → Reviewer, 20000.01 → Director, 100000.01 → Director and VP."""
    assert calc_approval_level(Decimal("20000.00")) == "Reviewer"
    assert calc_approval_level(Decimal("20000.01")) == "Director"
    assert calc_approval_level(Decimal("100000.01")) == "Director and VP"
