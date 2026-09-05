"""End-to-end test against the real portal. Skipped unless credentials are present.

Put ACE_USERNAME / ACE_PASSWORD / ACE_CLIENT_ID in the environment or in ../.env (next to this repo).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import socket
from unittest.mock import patch

import aiohttp
import pytest_socket

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.aclara_ace.const import CONF_CLIENT_ID, DOMAIN


def _creds() -> tuple[str, str, int] | None:
    for env_file in (Path(__file__).resolve().parents[1] / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    u, p, c = os.environ.get("ACE_USERNAME"), os.environ.get("ACE_PASSWORD"), os.environ.get("ACE_CLIENT_ID")
    return (u, p, int(c)) if u and p and c else None


@pytest.mark.skipif(_creds() is None, reason="no ACE credentials")
async def test_live_setup_imports_statistics(recorder_mock, hass: HomeAssistant, socket_enabled) -> None:
    """Real login, real download, real statistics rows."""
    username, password, client_id = _creds()  # type: ignore[misc]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: username, CONF_PASSWORD: password, CONF_CLIENT_ID: client_id},
    )
    entry.add_to_hass(hass)
    # The harness blocks outbound sockets; allow the portal hosts for this test.
    hosts = {"127.0.0.1"}
    for host in ("acewebsite.silverblaze.com", "acelogin.silverblaze.com"):
        hosts.update(info[4][0] for info in socket.getaddrinfo(host, 443))
    pytest_socket.socket_allow_hosts(sorted(hosts), allow_unix_socket=True)
    # The harness's HA client session can't resolve DNS; use a plain one.
    session = aiohttp.ClientSession()
    try:
        await _run(hass, entry, session)
    finally:
        await session.close()


async def _run(hass: HomeAssistant, entry: MockConfigEntry, session: aiohttp.ClientSession) -> None:
    with patch("custom_components.aclara_ace.coordinator.async_create_clientsession", return_value=session):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await async_wait_recording_done(hass)

    coordinator = entry.runtime_data
    assert coordinator.data, "no meters"
    meter_id, data = next(iter(coordinator.data.items()))
    assert data.latest_reading is not None
    assert data.latest_reading > dt_util.now() - timedelta(days=7)

    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=dt_util.UTC),
        None,
        {data.consumption_statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    rows = rows[data.consumption_statistic_id]
    print(f"\nLIVE: meter {meter_id}: {len(rows)} hourly rows, total {rows[-1]['sum']:.0f} {data.unit}, latest {data.latest_reading}")
    assert len(rows) > 24 * 30
    assert rows[-1]["sum"] > 0
    assert all(b["sum"] >= a["sum"] for a, b in zip(rows, rows[1:], strict=False))

    state = hass.states.get(f"sensor.water_meter_{meter_id}_latest_reading")
    assert state is not None and state.state not in ("unknown", "unavailable")
