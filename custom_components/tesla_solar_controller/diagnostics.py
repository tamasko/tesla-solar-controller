"""Diagnostics for the P1-only controller."""

from __future__ import annotations

from . import TeslaSolarConfigEntry


async def async_get_config_entry_diagnostics(
    hass, entry: TeslaSolarConfigEntry
) -> dict:
    c = entry.runtime_data
    return {
        "controller_enabled": c.controller_enabled,
        "mode": c.mode,
        "status": c.display_status,
        "session_kind": c.session_kind,
        "starting": c.starting,
        "is_awake": c.is_awake,
        "is_charging": c.is_charging,
        "charge_cable_connected": c.charge_cable_connected,
        "last_battery_pct": c.last_battery_pct,
        "target_soc": c.target_soc,
        "grid_net_power_w": c.grid_net_power_w,
        "budget_day": c.budget_day,
        "commands_today": c.daily_commands,
        "wakes_today": c.daily_wakes,
        "solar_sessions_today": c.daily_solar_sessions,
        "cooldown_until": c.cooldown_until.isoformat() if c.cooldown_until else None,
        "commands": c._commands,
        "source_entities": dict(entry.data),
        "options": dict(entry.options),
    }
