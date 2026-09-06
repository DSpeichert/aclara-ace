"""Setup + statistics import tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta

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


async def test_options_flow_sets_tariff(recorder_mock, hass: HomeAssistant, mock_client) -> None:
    """The options form renders, validates, and stores the tariff."""
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.aclara_ace.const import CONF_BILLING_PERIOD_DAYS, CONF_BILLING_START, CONF_TIERS

    entry = await _setup(hass, options={CONF_PRICE_PER_UNIT: 0.005})  # legacy flat price
    assert entry.runtime_data.tariff.is_flat
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    # Multiple tiers without a billing date is rejected.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TIERS: "6000 0.003\n+ 0.005", CONF_BILLING_PERIOD_DAYS: 30}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BILLING_START: "billing_start_required"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TIERS: "garbage here now", CONF_BILLING_PERIOD_DAYS: 30}
    )
    assert result["errors"] == {CONF_TIERS: "invalid_tiers"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_TIERS: "6000 0.003\n+ 0.005", CONF_BILLING_START: "2026-03-15", CONF_BILLING_PERIOD_DAYS: 30},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    tariff = entry.runtime_data.tariff
    assert [t.upper for t in tariff.tiers] == [6000, None]
    assert tariff.anchor == date(2026, 3, 15)
    assert tariff.period_days == 30


async def test_tiered_cost_resets_each_billing_period(
    recorder_mock, hass: HomeAssistant, mock_client, portal: FakePortal
) -> None:
    """Cost uses cumulative-per-period tiers and starts over on the billing date."""
    from custom_components.aclara_ace.const import CONF_BILLING_PERIOD_DAYS, CONF_BILLING_START, CONF_TIERS

    # 150 gal/day. A 4-day period holds 600 gal: 250 @ $1, 250 @ $2, rest @ $4.
    anchor = portal.oldest + timedelta(days=3)  # first period is short (3 days = 450 gal)
    entry = await _setup(
        hass,
        options={
            CONF_TIERS: "250 1\n500 2\n+ 4",
            CONF_BILLING_START: anchor.isoformat(),
            CONF_BILLING_PERIOD_DAYS: 4,
        },
    )
    tariff = entry.runtime_data.tariff
    readings = portal.readings(portal.oldest, portal.newest.date())

    expected_total = 0.0
    period, used = None, 0.0
    for r in readings:
        p = tariff.period_start(r.start)
        if p != period:
            period, used = p, 0.0
        expected_total += tariff.cost(r.quantity, used)
        used += r.quantity

    cost = await _stats(hass, COST_ID)
    assert cost[-1]["sum"] == pytest.approx(expected_total)

    # Sanity-check the model by hand on the first two periods.
    # Period 0 (3 days, 450 gal): 250*1 + 200*2 = 650. Period 1 (4 days, 600 gal): 250 + 500 + 100*4 = 1150.
    by_period: dict[date, float] = {}
    for row in cost:
        d = datetime.fromtimestamp(row["start"], TZ)
        by_period[tariff.period_start(d)] = by_period.get(tariff.period_start(d), 0.0) + row["state"]
    periods = sorted(by_period)
    assert by_period[periods[0]] == pytest.approx(650)
    assert by_period[periods[1]] == pytest.approx(1150)

    # Incremental refresh re-fetches from the period start so tier position is right.
    portal.newest = portal.newest + timedelta(days=1)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert portal.calls[-1][0] <= tariff.period_start(portal.newest.date() - timedelta(days=7))
    cost2 = await _stats(hass, COST_ID)
    readings2 = portal.readings(portal.oldest, portal.newest.date())
    expected2, period, used = 0.0, None, 0.0
    for r in readings2:
        p = tariff.period_start(r.start)
        if p != period:
            period, used = p, 0.0
        expected2 += tariff.cost(r.quantity, used)
        used += r.quantity
    assert cost2[-1]["sum"] == pytest.approx(expected2)


async def test_rebuild_button_reprices_history(
    recorder_mock, hass: HomeAssistant, mock_client, portal: FakePortal
) -> None:
    """Pressing the button re-imports everything with the tariff set since."""
    from custom_components.aclara_ace.const import CONF_TIERS

    entry = await _setup(hass)  # no tariff: cost stays 0
    cost = await _stats(hass, COST_ID)
    assert cost[-1]["sum"] == 0

    # The coordinator reads options live, so no reload is needed for the test.
    hass.config_entries.async_update_entry(entry, options={CONF_TIERS: "+ 0.01"})
    await hass.async_block_till_done()

    button = "button.water_meter_12345678_rebuild_statistics"
    assert hass.states.get(button) is not None
    calls_before = len(portal.calls)
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()

    assert len(portal.calls) == calls_before + 1
    assert portal.calls[-1][0] == portal.oldest  # full history, not the lookback window
    usage = await _stats(hass, STAT_ID)
    cost = await _stats(hass, COST_ID)
    assert len(cost) == len(usage)
    assert cost[-1]["sum"] == pytest.approx(usage[-1]["sum"] * 0.01)
    assert usage[-1]["sum"] == pytest.approx(sum(r.quantity for r in portal.readings(portal.oldest, portal.newest.date())))
