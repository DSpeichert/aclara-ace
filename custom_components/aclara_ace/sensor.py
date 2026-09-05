"""Diagnostic sensors for the Aclara ACE integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AclaraAceConfigEntry, AclaraAceCoordinator, MeterData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AclaraAceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one "latest reading" sensor per meter."""
    coordinator = entry.runtime_data
    async_add_entities(
        AclaraAceLatestReadingSensor(coordinator, meter_id)
        for meter_id in coordinator.data
    )


class AclaraAceLatestReadingSensor(CoordinatorEntity[AclaraAceCoordinator], SensorEntity):
    """Timestamp of the newest hourly read the portal has published."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = SensorEntityDescription(
        key="latest_reading",
        translation_key="latest_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
    )

    def __init__(self, coordinator: AclaraAceCoordinator, meter_id: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._meter_id = meter_id
        data = coordinator.data[meter_id]
        self._attr_unique_id = f"{meter_id}_latest_reading"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter_id)},
            name=f"{data.meter.commodity.capitalize()} meter {meter_id}",
            manufacturer="Aclara",
            model=data.meter.meter_type or "AMI",
            configuration_url="https://acewebsite.silverblaze.com/",
        )

    @property
    def _data(self) -> MeterData | None:
        return self.coordinator.data.get(self._meter_id)

    @property
    def available(self) -> bool:
        """Available when the meter is still reported by the portal."""
        return super().available and self._data is not None

    @property
    def native_value(self) -> datetime | None:
        """Return the start of the newest hourly interval."""
        return self._data.latest_reading if self._data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the statistic ids so they're easy to find in the Energy config."""
        if not (data := self._data):
            return {}
        return {
            "consumption_statistic_id": data.consumption_statistic_id,
            "cost_statistic_id": data.cost_statistic_id,
            "latest_day": data.latest_day.isoformat() if data.latest_day else None,
            "latest_day_total": data.latest_day_total,
            "unit": data.unit,
            "account_id": data.meter.account_id,
            "address": data.meter.address,
            "rate_class": data.meter.rate_class,
        }
