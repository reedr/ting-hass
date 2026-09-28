"""Alerts event entity for Ting."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TingRuntimeData
from .api import TingDevice
from .const import ALERT_EVENT_TYPES, DOMAIN
from .coordinator import TingProfileCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ting alerts event entities."""
    runtime: TingRuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        TingAlertsEvent(runtime.profile_coordinator, device) for device in runtime.devices
    )


class TingAlertsEvent(CoordinatorEntity[TingProfileCoordinator], EventEntity):
    """Fires once for each new notification Ting sends about this device.

    These are the alerts the Ting app shows: fire hazard, frozen pipe, power
    outage / restored, sag / swell and weather.  An unrecognised type fires
    as "unknown" with Ting's own value in raw_event_type.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "alerts"
    _attr_event_types = ALERT_EVENT_TYPES

    def __init__(self, coordinator: TingProfileCoordinator, device: TingDevice) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.serial_number}_alerts"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial_number)},
            manufacturer="Whisker Labs",
            name=device.name,
            model=device.model or "Ting",
            sw_version=device.firmware,
            suggested_area=device.site_name,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        for note in self.coordinator.new_alerts.get(self._device.serial_number, []):
            raw_type = note["event_type"]
            self._trigger_event(
                raw_type if raw_type in ALERT_EVENT_TYPES else "unknown",
                {
                    "title": note["title"],
                    "subtitle": note["subtitle"],
                    "message": note["message"],
                    "category": note["category"],
                    "raw_event_type": raw_type,
                    "timestamp": note["timestamp"].isoformat() if note["timestamp"] else None,
                    "notification_id": note["id"],
                    "acknowledged": note["acknowledged"],
                    "cleared": note["cleared"],
                },
            )
            # note: write each one; the entity's state holds only the latest
            #       event, so batching would hide all but the last alert from
            #       automations.
            self.async_write_ha_state()
        super()._handle_coordinator_update()
