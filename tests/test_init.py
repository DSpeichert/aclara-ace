"""Setup + statistics import tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from custom_components.aclara_ace.const import CONF_CLIENT_ID, CONF_PRICE_PER_UNIT, DOMAIN

from .conftest import TZ, FakePortal

STAT_ID = "aclara_ace:water_12345678_consumption"
COST_ID = "aclara_ace:water_12345678_cost"


async def _stats(hass: HomeAssistant, stat_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    start = datetime.now(TZ) - timedelta(days=30)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {stat_id}, "hour", None, {"state", "sum"}
    )
    return rows.get(stat_id, [])


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw", CONF_CLIENT_ID: 42},
        options=options or {},
        unique_id="42_000-0000001-001",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_first_import_backfills_history(recorder_mock, hass: HomeAssistant, mock_client, portal: FakePortal) -> None:
    """A fresh install imports everything the portal holds, with correct running sums."""
    entry = await _setup(hass, options={CONF_PRICE_PER_UNIT: 0.01})
    assert entry.state is ConfigEntryState.LOADED

    # First fetch starts at the portal's oldest AMI date.
    assert portal.calls[0][0] == portal.oldest

    rows = await _stats(hass, STAT_ID)
    expected = portal.readings(portal.oldest, portal.newest.date())
    assert len(rows) == len(expected)
    assert rows[-1]["sum"] == pytest.approx(sum(r.quantity for r in expected))
    # Sum is monotonic and equals the cumulative quantity at every hour.
    running = 0.0
    for row, reading in zip(rows, expected, strict=True):
        running += reading.quantity
        assert row["start"] == reading.start.timestamp()
        assert row["state"] == pytest.approx(reading.quantity)
        assert row["sum"] == pytest.approx(running)

    cost = await _stats(hass, COST_ID)
    assert cost[-1]["sum"] == pytest.approx(rows[-1]["sum"] * 0.01)

    state = hass.states.get("sensor.water_meter_12345678_latest_reading")
    assert state is not None
    assert datetime.fromisoformat(state.state) == portal.newest
    assert state.attributes["consumption_statistic_id"] == STAT_ID
    assert state.attributes["latest_day_total"] == pytest.approx(100.0)  # 06:00 only, newest is 09:00


async def test_incremental_update_overwrites_corrections(
    recorder_mock, hass: HomeAssistant, mock_client, portal: FakePortal
) -> None:
    """Later polls re-import a lookback window and keep sums continuous."""
    entry = await _setup(hass)
    before = await _stats(hass, STAT_ID)
    total_before = before[-1]["sum"]

    # The portal publishes the rest of "yesterday" plus a correction to an old hour,
    # then a new day arrives.
    corrected_ts = (portal.newest - timedelta(days=2)).replace(hour=6)
    portal.overrides[corrected_ts] = 400.0  # was 100
    portal.newest = portal.newest + timedelta(days=1)

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(portal.calls) == 2

    after = await _stats(hass, STAT_ID)
    expected = portal.readings(portal.oldest, portal.newest.date())
    assert len(after) == len(expected)
    assert after[-1]["sum"] == pytest.approx(sum(r.quantity for r in expected))
    assert after[-1]["sum"] == pytest.approx(total_before + 300 + 150)  # +correction, +new day (100+50)
    corrected_row = next(r for r in after if r["start"] == corrected_ts.timestamp())
    assert corrected_row["state"] == pytest.approx(400.0)
    # The second fetch only asked for the lookback window, not full history.
    assert portal.calls[-1][0] > portal.oldest
    running = 0.0
    for row, reading in zip(after, expected, strict=True):
        running += reading.quantity
        assert row["sum"] == pytest.approx(running)


async def test_config_flow_creates_entry(recorder_mock, hass: HomeAssistant, mock_client) -> None:
    """Happy-path config flow."""
    from homeassistant import config_entries
    from homeassistant.data_entry_flow import FlowResultType

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw", CONF_CLIENT_ID: 42},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ACE 000-0000001-001"
    assert result["result"].unique_id == "42_000-0000001-001"
