"""Switch platform for Tesla Solar Controller."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TeslaSolarConfigEntry
from .entity import TeslaSolarControllerEntity


async def async_setup_entry(
    hass, entry: TeslaSolarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        [
            TeslaControllerEnabledSwitch(entry.runtime_data),
        ]
    )


class TeslaControllerEnabledSwitch(TeslaSolarControllerEntity, SwitchEntity):
    """Persistent master permission for all controller vehicle commands."""

    _attr_name = "Enabled"
    _attr_translation_key = "enabled"
    _attr_icon = "mdi:power"

    def __init__(self, controller) -> None:
        super().__init__(controller, "enabled")

    @property
    def is_on(self) -> bool:
        return self.controller.controller_enabled

    async def async_turn_on(self, **kwargs) -> None:
        try:
            await self.controller.async_set_controller_enabled(True)
        finally:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        try:
            await self.controller.async_set_controller_enabled(False)
        finally:
            self.async_write_ha_state()
