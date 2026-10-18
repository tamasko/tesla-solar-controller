"""Binary sensor platform for Tesla Solar Controller."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TeslaSolarConfigEntry
from .entity import TeslaSolarControllerEntity


async def async_setup_entry(
    hass, entry: TeslaSolarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller = entry.runtime_data
    async_add_entities(
        [
            TeslaChargingBinarySensor(controller),
        ]
    )


class TeslaChargingBinarySensor(TeslaSolarControllerEntity, BinarySensorEntity):
    _attr_name = "Charging"
    _attr_icon = "mdi:ev-station"

    def __init__(self, controller) -> None:
        super().__init__(controller, "charging")

    @property
    def is_on(self) -> bool:
        return self.controller.is_charging
