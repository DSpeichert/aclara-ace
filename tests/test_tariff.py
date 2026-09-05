"""Tariff parsing and tiered cost math."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.aclara_ace.tariff import Tariff, TariffError, Tier, format_tiers, parse_tiers

TZ = ZoneInfo("America/Chicago")


def test_parse_and_format_roundtrip() -> None:
    tiers = parse_tiers("6000 0.00325\n15,000 $0.00475  # second\n\n+ 0.0065\n")
    assert tiers == (Tier(6000, 0.00325), Tier(15000, 0.00475), Tier(None, 0.0065))
    assert parse_tiers(format_tiers(tiers)) == tiers
    assert parse_tiers("0.005") == (Tier(None, 0.005),)
    assert parse_tiers("") == ()


@pytest.mark.parametrize("text", ["abc 1", "1000 x", "5000 1\n4000 2", "1 2 3", "1000 -1"])
def test_parse_rejects_bad_input(text: str) -> None:
    with pytest.raises(TariffError):
        parse_tiers(text)


def test_cost_splits_across_tiers() -> None:
    t = Tariff(tiers=parse_tiers("100 1\n200 2\n+ 3"))
    assert t.cost(50, 0) == 50
    assert t.cost(100, 50) == 50 * 1 + 50 * 2  # straddles tier 1/2
    assert t.cost(150, 100) == 100 * 2 + 50 * 3  # straddles tier 2/3
    assert t.cost(10, 1000) == 30  # deep in the last tier
    assert t.cost(0, 0) == 0
    # Usage past the last *bounded* tier when no "+" tier is given uses the last price.
    assert Tariff(tiers=parse_tiers("100 1")).cost(150, 0) == 100 * 1 + 50 * 1


def test_period_start_repeats_from_anchor_both_directions() -> None:
    t = Tariff(tiers=(Tier(None, 1),), anchor=date(2026, 3, 15), period_days=30)
    assert t.period_start(date(2026, 3, 15)) == date(2026, 3, 15)
    assert t.period_start(date(2026, 4, 13)) == date(2026, 3, 15)
    assert t.period_start(date(2026, 4, 14)) == date(2026, 4, 14)
    assert t.period_start(date(2026, 1, 1)) == date(2025, 12, 15)  # before the anchor
    assert t.period_start(datetime(2026, 4, 14, 0, 30, tzinfo=TZ)) == date(2026, 4, 14)
    # No anchor: calendar months.
    assert Tariff(tiers=(Tier(None, 1),)).period_start(date(2026, 4, 14)) == date(2026, 4, 1)
