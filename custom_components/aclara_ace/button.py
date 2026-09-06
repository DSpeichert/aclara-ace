"""Buttons for the Aclara ACE integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AclaraAceConfigEntry, AclaraAceCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AclaraAceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one rebuild button per meter."""
    coordinator = entry.runtime_data
    async_add_entities(
        AclaraAceRebuildButton(coordinator, meter_id) for meter_id in coordinator.data
    )


class AclaraAceRebuildButton(CoordinatorEntity[AclaraAceCoordinator], ButtonEntity):
    """Re-import all history and re-price it with the current tariff."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    entity_description = ButtonEntityDescription(
        key="rebuild_statistics",
        translation_key="rebuild_statistics",
        icon="mdi:database-refresh",
    )

    def __init__(self, coordinator: AclaraAceCoordinator, meter_id: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._attr_unique_id = f"{meter_id}_rebuild_statistics"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, meter_id)})

    async def async_press(self) -> None:
        """Trigger a full rebuild on the next refresh and run it now."""
        await self.coordinator.async_rebuild_statistics(self._meter_id)
