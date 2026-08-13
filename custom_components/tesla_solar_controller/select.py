"""Select platform for Tesla Solar Controller."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TeslaSolarConfigEntry
from .const import MODES
from .entity import TeslaSolarControllerEntity


async def async_setup_entry(
    hass, entry: TeslaSolarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([TeslaChargingModeSelect(entry.runtime_data)])


class TeslaChargingModeSelect(TeslaSolarControllerEntity, SelectEntity):
    """Charging mode selector."""

    _attr_name = "Charging mode"
    _attr_icon = "mdi:car-electric"
    _attr_options = MODES

    def __init__(self, controller) -> None:
        super().__init__(controller, "charging_mode")

    @property
    def current_option(self) -> str:
        return self.controller.mode

    async def async_select_option(self, option: str) -> None:
        await self.controller.async_set_mode(option)
