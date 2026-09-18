"""Diagnostics support for Tesla Solar Controller."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from . import TeslaSolarConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TeslaSolarConfigEntry
) -> dict:
    controller = entry.runtime_data
    return {
        "controller_enabled": controller.controller_enabled,
        "mode": controller.mode,
        "requested_accessory": controller.requested_accessory,
        "maintenance_active": controller.maintenance_active,
        "last_battery_pct": controller.last_battery_pct,
        "last_charge_current_a": controller.last_charge_current_a,
        "last_charge_limit_pct": controller.last_charge_limit_pct,
        "status": controller.status,
        "sleep_status": controller.sleep_status,
        "target_soc": controller.target_soc,
        "is_awake": controller.is_awake,
        "is_charging": controller.is_charging,
        "live_charging_power_w": controller.live_charging_power_w,
        "solar_surplus_available_w": controller.solar_surplus_available_w,
        "averaged_solar_power_w": controller.averaged_solar_power_w,
        "averaged_grid_net_power_w": controller.averaged_grid_net_power_w,
        "averaged_tesla_charging_power_w": controller.averaged_tesla_charging_power_w,
        "averaged_solar_surplus_w": controller.averaged_solar_surplus_w,
        "tomorrow_forecast_kwh": controller.tomorrow_forecast_kwh,
        "poor_forecast": controller.poor_forecast,
        "off_peak": controller.off_peak,
        "source_entities": dict(entry.data),
        "options": dict(entry.options),
    }
