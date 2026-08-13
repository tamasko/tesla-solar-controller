"""Tesla Solar Controller integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .controller import TeslaSolarController

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
]

type TeslaSolarConfigEntry = ConfigEntry[TeslaSolarController]


async def async_setup_entry(hass: HomeAssistant, entry: TeslaSolarConfigEntry) -> bool:
    """Set up Tesla Solar Controller from a config entry."""
    controller = TeslaSolarController(hass, entry)
    await controller.async_initialize()
    entry.runtime_data = controller

    async def _async_options_updated(
        hass: HomeAssistant, updated_entry: TeslaSolarConfigEntry
    ) -> None:
        await controller.async_options_updated(updated_entry.options)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TeslaSolarConfigEntry) -> bool:
    """Unload a Tesla Solar Controller config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
