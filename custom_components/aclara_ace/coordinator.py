"""Coordinator: polls the ACE portal and inserts long-term statistics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import (
    AclaraAceClient,
    AclaraAceError,
    Account,
    ApiError,
    AuthError,
    Meter,
    MeterSpan,
    Reading,
)
from .const import (
    CONF_BILLING_PERIOD_DAYS,
    CONF_BILLING_START,
    CONF_CLIENT_ID,
    CONF_PRICE_PER_UNIT,
    CONF_TIERS,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_BILLING_PERIOD_DAYS,
    DEFAULT_PRICE_PER_UNIT,
    DOMAIN,
    LOOKBACK_DAYS,
    UPDATE_INTERVAL,
)
from .tariff import Tariff, TariffError, Tier, parse_tiers

_LOGGER = logging.getLogger(__name__)

type AclaraAceConfigEntry = ConfigEntry[AclaraAceCoordinator]

_UNIT_MAP: dict[str, str] = {
    "gal": UnitOfVolume.GALLONS,
    "gallon": UnitOfVolume.GALLONS,
    "gallons": UnitOfVolume.GALLONS,
    "ccf": UnitOfVolume.CENTUM_CUBIC_FEET,
    "cf": UnitOfVolume.CUBIC_FEET,
    "ft3": UnitOfVolume.CUBIC_FEET,
    "m3": UnitOfVolume.CUBIC_METERS,
    "l": UnitOfVolume.LITERS,
    "liter": UnitOfVolume.LITERS,
    "litre": UnitOfVolume.LITERS,
}


@dataclass
class MeterData:
    """Per-meter snapshot exposed to entities."""

    meter: Meter
    account: Account
    unit: str
    latest_reading: datetime | None
    latest_day: date | None
    latest_day_total: float | None
    consumption_statistic_id: str
    cost_statistic_id: str


class AclaraAceCoordinator(DataUpdateCoordinator[dict[str, MeterData]]):
    """Fetch usage from the portal and push it into recorder statistics."""

    config_entry: AclaraAceConfigEntry

    def __init__(self, hass: HomeAssistant, entry: AclaraAceConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = AclaraAceClient(
            async_create_clientsession(hass),
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            client_id=int(entry.data[CONF_CLIENT_ID]),
        )

        @callback
        def _dummy_listener() -> None:
            pass

        # Keep periodic refreshes running even if no entity subscribes.
        self.async_add_listener(_dummy_listener)
        # Meters whose statistics must be rebuilt from the oldest reading.
        self._rebuild: set[str] = set()

    async def async_rebuild_statistics(self, meter_id: str) -> None:
        """Re-import every reading for ``meter_id`` and re-price it with the current tariff.

        Existing statistic rows are overwritten in place (same start times),
        so both the consumption and cost sums are recomputed from zero.
        """
        self._rebuild.add(meter_id)
        _LOGGER.info("Rebuilding statistics for meter %s from the oldest reading", meter_id)
        await self.async_refresh()

    @property
    def tariff(self) -> Tariff:
        """Tiered tariff from the options flow (falls back to the legacy flat price)."""
        return tariff_from_options(self.config_entry.options)

    @property
    def price_per_unit(self) -> float:
        """Price of the first tier, for display."""
        tariff = self.tariff
        return tariff.tiers[0].price if tariff.tiers else 0.0

    async def _async_update_data(self) -> dict[str, MeterData]:
        """Log in, pull new readings and insert statistics."""
        try:
            # Portal sessions do not survive between 6-hourly polls; always re-login.
            await self.api.async_login()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (aiohttp.ClientError, TimeoutError, AclaraAceError) as err:
            raise UpdateFailed(f"Error logging in to ACE portal: {err}") from err

        try:
            account = await self.api.async_get_account()
            spans = await self.api.async_get_meter_spans()
        except (aiohttp.ClientError, TimeoutError, AclaraAceError) as err:
            raise UpdateFailed(f"Error reading account from ACE portal: {err}") from err

        tz = await dt_util.async_get_time_zone(account.timezone) or dt_util.get_default_time_zone()
        result: dict[str, MeterData] = {}
        for meter in account.meters:
            span = _pick_span(spans, meter.meter_id)
            try:
                result[meter.meter_id] = await self._async_insert_statistics(
                    account, meter, span, tz
                )
            except (aiohttp.ClientError, TimeoutError, ApiError) as err:
                raise UpdateFailed(
                    f"Error reading usage for meter {meter.meter_id}: {err}"
                ) from err
        return result

    async def _async_insert_statistics(
        self, account: Account, meter: Meter, span: MeterSpan | None, tz: Any
    ) -> MeterData:
        """Import hourly readings for one meter as external statistics."""
        id_prefix = f"{meter.commodity}_{meter.meter_id}".replace("-", "_").lower()
        consumption_id = f"{DOMAIN}:{id_prefix}_consumption"
        cost_id = f"{DOMAIN}:{id_prefix}_cost"
        name_prefix = f"ACE {meter.commodity} {meter.meter_id}"

        today = dt_util.now(tz).date()
        rebuild = meter.meter_id in self._rebuild
        last_stat = None
        if not rebuild:
            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, consumption_id, True, set()
            )
        if not last_stat:
            if span is not None:
                start_date = span.oldest.astimezone(tz).date()
            else:
                start_date = today - timedelta(days=DEFAULT_BACKFILL_DAYS)
            _LOGGER.debug("%s: %s from %s", consumption_id, "rebuilding" if rebuild else "first import, backfilling", start_date)
        else:
            last_start = dt_util.utc_from_timestamp(last_stat[consumption_id][0]["start"])
            start_date = (last_start.astimezone(tz) - timedelta(days=LOOKBACK_DAYS)).date()

        tariff = self.tariff
        if last_stat and tariff.enabled and not tariff.is_flat:
            # Tier position depends on everything used earlier in the billing
            # period, so start the fetch at that period's first day.
            start_date = min(start_date, tariff.period_start(start_date))

        readings = await self.api.async_get_hourly_usage(meter, start_date, today, timezone=tz)
        unit_raw = readings[0].unit if readings else (span.unit if span else "gal")
        unit = _UNIT_MAP.get(unit_raw.lower(), unit_raw)

        consumption_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name_prefix} consumption",
            source=DOMAIN,
            statistic_id=consumption_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=unit,
        )
        cost_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name_prefix} cost",
            source=DOMAIN,
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency,
        )

        latest_reading: datetime | None = None
        latest_day: date | None = None
        latest_day_total: float | None = None

        if readings:
            first_start = readings[0].start
            consumption_sum, cost_sum = await self._async_base_sums(
                consumption_id, cost_id, first_start
            )
            consumption_stats: list[StatisticData] = []
            cost_stats: list[StatisticData] = []
            period_start: date | None = None
            period_used = 0.0
            for reading in readings:
                consumption_sum += reading.quantity
                this_period = tariff.period_start(reading.start)
                if this_period != period_start:
                    period_start, period_used = this_period, 0.0
                cost = tariff.cost(reading.quantity, period_used)
                period_used += reading.quantity
                cost_sum += cost
                consumption_stats.append(
                    StatisticData(start=reading.start, state=reading.quantity, sum=consumption_sum)
                )
                cost_stats.append(StatisticData(start=reading.start, state=cost, sum=cost_sum))
            async_add_external_statistics(self.hass, consumption_meta, consumption_stats)
            async_add_external_statistics(self.hass, cost_meta, cost_stats)
            _LOGGER.debug(
                "%s: imported %d hourly rows from %s to %s",
                consumption_id,
                len(readings),
                readings[0].start,
                readings[-1].start,
            )
            self._rebuild.discard(meter.meter_id)
            latest_reading = readings[-1].start
            latest_day = latest_reading.date()
            latest_day_total = sum(r.quantity for r in readings if r.start.date() == latest_day)
        else:
            _LOGGER.debug("%s: no readings returned since %s", consumption_id, start_date)
            if last_stat:
                latest_reading = dt_util.utc_from_timestamp(
                    last_stat[consumption_id][0]["start"]
                ).astimezone(tz)

        return MeterData(
            meter=meter,
            account=account,
            unit=unit,
            latest_reading=latest_reading,
            latest_day=latest_day,
            latest_day_total=latest_day_total,
            consumption_statistic_id=consumption_id,
            cost_statistic_id=cost_id,
        )

    async def _async_base_sums(
        self, consumption_id: str, cost_id: str, first_start: datetime
    ) -> tuple[float, float]:
        """Return the running sums stored just before ``first_start``.

        Everything from ``first_start`` onwards is rewritten, so the base is the
        most recent statistic row strictly before it (or zero if none exists).
        """
        window_start = first_start - timedelta(days=LOOKBACK_DAYS * 4)
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start,
            first_start,
            {consumption_id, cost_id},
            "hour",
            None,
            {"sum"},
        )

        def _last_sum(rows: list[dict[str, Any]]) -> float:
            for row in reversed(rows):
                if row.get("sum") is not None:
                    return float(row["sum"])
            return 0.0

        return _last_sum(stats.get(consumption_id, [])), _last_sum(stats.get(cost_id, []))


def tariff_from_options(options: Mapping[str, Any]) -> Tariff:
    """Build the tariff from config entry options."""
    tiers: tuple[Tier, ...] = ()
    text = options.get(CONF_TIERS) or ""
    if text.strip():
        try:
            tiers = parse_tiers(text)
        except TariffError as err:
            _LOGGER.error("Ignoring invalid tariff tiers in options: %s", err)
    elif (flat := float(options.get(CONF_PRICE_PER_UNIT, DEFAULT_PRICE_PER_UNIT))) > 0:
        tiers = (Tier(None, flat),)
    anchor: date | None = None
    if raw := options.get(CONF_BILLING_START):
        try:
            anchor = date.fromisoformat(str(raw))
        except ValueError:
            _LOGGER.error("Ignoring invalid billing start date in options: %r", raw)
    period_days = int(options.get(CONF_BILLING_PERIOD_DAYS, DEFAULT_BILLING_PERIOD_DAYS) or DEFAULT_BILLING_PERIOD_DAYS)
    return Tariff(tiers=tiers, anchor=anchor, period_days=max(1, period_days))


def _pick_span(spans: list[MeterSpan], meter_id: str) -> MeterSpan | None:
    """Prefer the hourly AMI span for a meter."""
    mine = [s for s in spans if s.meter_id == meter_id]
    for s in mine:
        if s.interval.lower() in ("60min", "hour", "hourly"):
            return s
    return mine[0] if mine else None


__all__ = ["AclaraAceConfigEntry", "AclaraAceCoordinator", "MeterData", "Reading"]
