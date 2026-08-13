"""Base entity for Tesla Solar Controller."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, NAME, VERSION
from .controller import TeslaSolarController


class TeslaSolarControllerEntity(Entity):
    """Base entity backed by the Tesla Solar Controller."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: TeslaSolarController, key: str) -> None:
        self.controller = controller
        self._attr_unique_id = f"{controller.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={controller.device_identifier},
            name=NAME,
            manufacturer="Custom integration",
            model="Tesla solar charging controller",
            sw_version=VERSION,
        )
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_listener = self.controller.async_add_listener(
            self._handle_controller_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None
        await super().async_will_remove_from_hass()

    def _handle_controller_update(self) -> None:
        self.async_write_ha_state()
