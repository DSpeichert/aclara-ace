"""Tiered water tariff with billing-period resets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import math
import re

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")

DEFAULT_PERIOD_DAYS = 30


class TariffError(ValueError):
    """Raised when the tier text can't be parsed."""


@dataclass(frozen=True)
class Tier:
    """Price applying up to ``upper`` cumulative units in a period (None = no limit)."""

    upper: float | None
    price: float


@dataclass(frozen=True)
class Tariff:
    """Ordered tiers plus the billing cycle that resets them."""

    tiers: tuple[Tier, ...] = field(default_factory=tuple)
    anchor: date | None = None
    period_days: int = DEFAULT_PERIOD_DAYS

    @property
    def enabled(self) -> bool:
        """True when any pricing was configured."""
        return bool(self.tiers)

    @property
    def is_flat(self) -> bool:
        """True for a single unbounded tier."""
        return len(self.tiers) == 1 and self.tiers[0].upper is None

    def period_start(self, when: date | datetime) -> date:
        """Return the first day of the billing period containing ``when``.

        Periods are ``period_days`` long and repeat from ``anchor`` in both
        directions. Without an anchor, calendar months are used.
        """
        day = when.date() if isinstance(when, datetime) else when
        if self.anchor is None:
            return day.replace(day=1)
        k = math.floor((day - self.anchor).days / self.period_days)
        return self.anchor + timedelta(days=k * self.period_days)

    def cost(self, quantity: float, used_before: float) -> float:
        """Cost of ``quantity`` units when ``used_before`` were already used this period."""
        if not self.tiers or quantity <= 0:
            return 0.0
        remaining = quantity
        position = used_before
        total = 0.0
        for tier in self.tiers:
            if tier.upper is None:
                total += remaining * tier.price
                remaining = 0.0
                break
            room = tier.upper - position
            if room <= 0:
                continue
            take = min(room, remaining)
            total += take * tier.price
            remaining -= take
            position += take
            if remaining <= 0:
                break
        if remaining > 0:
            # Usage beyond the last bounded tier: charge at the last tier's price.
            total += remaining * self.tiers[-1].price
        return total


def parse_tiers(text: str) -> tuple[Tier, ...]:
    """Parse one tier per line: ``<upper units or +> <price>``.

    Example::

        6000 0.00325
        15000 0.00475
        + 0.00650

    ``+`` (or ``*``/blank upper) marks the final, unbounded tier. Uppers are
    cumulative per billing period and must increase. Commas and a leading
    ``$`` are ignored.
    """
    tiers: list[Tier] = []
    last_upper = 0.0
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        line = _THOUSANDS.sub("", line)  # 15,000 -> 15000
        parts = line.replace(",", " ").replace(":", " ").split()
        if len(parts) == 1:
            upper_s, price_s = "+", parts[0]
        elif len(parts) == 2:
            upper_s, price_s = parts
        else:
            raise TariffError(f"Expected '<upper> <price>' on line: {raw_line!r}")
        try:
            price = float(price_s.lstrip("$"))
        except ValueError as err:
            raise TariffError(f"Bad price on line: {raw_line!r}") from err
        if price < 0:
            raise TariffError(f"Negative price on line: {raw_line!r}")
        if upper_s in ("+", "*", "inf", "∞"):
            tiers.append(Tier(None, price))
            break
        try:
            upper = float(upper_s)
        except ValueError as err:
            raise TariffError(f"Bad upper bound on line: {raw_line!r}") from err
        if upper <= last_upper:
            raise TariffError(f"Tier bounds must increase: {raw_line!r}")
        tiers.append(Tier(upper, price))
        last_upper = upper
    return tuple(tiers)


def format_tiers(tiers: tuple[Tier, ...]) -> str:
    """Inverse of :func:`parse_tiers` for showing current options."""
    lines = []
    for t in tiers:
        upper = "+" if t.upper is None else f"{t.upper:g}"
        lines.append(f"{upper} {t.price:g}")
    return "\n".join(lines)
