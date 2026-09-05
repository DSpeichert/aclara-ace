"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.aclara_ace.api import Account, Meter, MeterSpan, Reading

TZ = ZoneInfo("America/Chicago")
METER = Meter(
    meter_id="12345678",
    service_id="000-0000001-001_12345678",
    commodity="water",
    account_id="000-0000001-001",
    premise_id="0000000001",
    address="1 EXAMPLE ST, SPRINGFIELD, XX, 00000",
    rate_class="RW-1",
    meter_type="AMI",
)
ACCOUNT = Account(
    customer_id="1",
    account_id="000-0000001-001",
    name="TEST CUSTOMER",
    meters=[METER],
    timezone="America/Chicago",
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations: None) -> None:
    """Enable loading custom_components (recorder must be requested before hass)."""


class FakePortal:
    """Deterministic stand-in for the portal: 100 gal at 06:00 and 50 gal at 19:00 every day."""

    def __init__(self, oldest: date, newest: datetime) -> None:
        self.oldest = oldest
        self.newest = newest  # aware, last hour that has data
        self.overrides: dict[datetime, float] = {}
        self.calls: list[tuple[date, date]] = []

    def readings(self, start: date, end: date) -> list[Reading]:
        out: list[Reading] = []
        day = max(start, self.oldest)
        while day <= end:
            for hour in range(24):
                ts = datetime(day.year, day.month, day.day, hour, tzinfo=TZ)
                if ts > self.newest:
                    break
                qty = {6: 100.0, 19: 50.0}.get(hour, 0.0)
                qty = self.overrides.get(ts, qty)
                out.append(Reading(start=ts, quantity=qty, unit="gal", interval="60min"))
            day += timedelta(days=1)
        return out


@pytest.fixture
def portal() -> FakePortal:
    """Portal with 10 days of history ending yesterday at 09:00 local."""
    now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    newest = (now - timedelta(days=1)).replace(hour=9)
    return FakePortal(oldest=(now - timedelta(days=10)).date(), newest=newest)


@pytest.fixture
def mock_client(portal: FakePortal) -> Generator[AsyncMock]:
    """Patch the API client everywhere it is constructed."""

    async def get_hourly(meter: Meter, start: date, end: date, *, timezone=None) -> list[Reading]:
        portal.calls.append((start, end))
        return portal.readings(start, end)

    async def spans() -> list[MeterSpan]:
        return [
            MeterSpan(
                meter_id=METER.meter_id,
                interval="60min",
                unit="gal",
                oldest=datetime(portal.oldest.year, portal.oldest.month, portal.oldest.day, tzinfo=TZ),
                newest=portal.newest,
            )
        ]

    with (
        patch("custom_components.aclara_ace.coordinator.AclaraAceClient", autospec=True) as coord_cls,
        patch("custom_components.aclara_ace.config_flow.AclaraAceClient", autospec=True) as flow_cls,
    ):
        for cls in (coord_cls, flow_cls):
            inst = cls.return_value
            inst.async_login = AsyncMock(return_value=None)
            inst.async_get_account = AsyncMock(return_value=ACCOUNT)
            inst.async_get_meter_spans = AsyncMock(side_effect=spans)
            inst.async_get_hourly_usage = AsyncMock(side_effect=get_hourly)
        yield coord_cls.return_value
