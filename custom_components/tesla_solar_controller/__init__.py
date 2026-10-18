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
    # Remove only obsolete entities owned by this config entry, never source entities.
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    removed = {
        "accessory_power",
        "battery_maintenance",
        "solar_power_5_minute_average",
        "grid_power_5_minute_average",
        "charging_power_5_minute_average",
        "solar_surplus_5_minute_average",
    }
    obsolete_ids = {f"{entry.entry_id}_{key}" for key in removed}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id in obsolete_ids:
            registry.async_remove(entity.entity_id)
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


async def async_migrate_entry(
    hass: HomeAssistant, entry: TeslaSolarConfigEntry
) -> bool:
    """Retain selected entities/targets; discard removed features and tuning."""
    from .config_flow import ENTITY_DOMAINS
    from .const import (
        DEFAULT_OPTIONS,
        MAX_CURRENT_A,
        MIN_CURRENT_A,
        OPT_OFFPEAK_CURRENT_A,
    )

    if entry.version > 2:
        return False
    if entry.version == 1:
        options = {
            key: entry.options.get(key, default)
            for key, default in DEFAULT_OPTIONS.items()
        }
        options[OPT_OFFPEAK_CURRENT_A] = max(
            MIN_CURRENT_A, min(MAX_CURRENT_A, options[OPT_OFFPEAK_CURRENT_A])
        )
        hass.config_entries.async_update_entry(
            entry,
            version=2,
            data={k: v for k, v in entry.data.items() if k in ENTITY_DOMAINS},
            options=options,
        )
    return True
