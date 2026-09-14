"""Config flow for Tesla Solar Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ACCESSORY_SWITCH,
    CONF_BATTERY_LEVEL,
    CONF_CHARGE_CABLE,
    CONF_CHARGE_CURRENT,
    CONF_CHARGE_LIMIT,
    CONF_CHARGE_SWITCH,
    CONF_FORECAST_TOMORROW,
    CONF_GRID_NET_POWER,
    CONF_L2_VOLTAGE,
    CONF_SOLAR_POWER,
    CONF_VEHICLE_STATUS,
    CONF_WAKE_BUTTON,
    DEFAULT_OPTIONS,
    DOMAIN,
    OPT_ADAPTIVE_BUFFER_ENABLED,
    OPT_BUFFER_TARGET_SOC,
    OPT_EVENING_OFFPEAK_START_HOUR,
    OPT_GRID_STEP_DOWN_IMPORT_W,
    OPT_MAINTENANCE_START_SOC,
    OPT_MIN_SOLAR_PRODUCTION_W,
    OPT_MAX_CURRENT_A,
    OPT_MIN_CURRENT_A,
    OPT_NORMAL_TARGET_SOC,
    OPT_NOTIFY_SERVICE,
    OPT_OFFPEAK_CURRENT_A,
    OPT_POOR_FORECAST_THRESHOLD_KWH,
    OPT_RECENT_PLUG_WINDOW_MINUTES,
    OPT_SOLAR_START_EXPORT_W,
    OPT_SOLAR_WAKE_DELTA_SOC,
    OPT_SOLAR_START_MINUTES,
    OPT_SOLAR_STEP_UP_EXPORT_W,
    OPT_SOLAR_STOP_IMPORT_W,
    OPT_SOLAR_STOP_MINUTES,
    OPT_WEEKDAY_OFFPEAK_END_HOUR,
    OPT_WEEKEND_OFFPEAK_END_HOUR,
)


def _required_entity(
    key: str, domain: str, suggested: dict[str, str]
) -> tuple[vol.Marker, EntitySelector]:
    if value := suggested.get(key):
        marker = vol.Required(key, description={"suggested_value": value})
    else:
        marker = vol.Required(key)
    return marker, EntitySelector(EntitySelectorConfig(domain=domain))


def _optional_entity(
    key: str, domain: str, suggested: dict[str, str]
) -> tuple[vol.Marker, EntitySelector]:
    if value := suggested.get(key):
        marker = vol.Optional(key, description={"suggested_value": value})
    else:
        marker = vol.Optional(key)
    return marker, EntitySelector(EntitySelectorConfig(domain=domain))


def _entity_schema(suggested: dict[str, str] | None = None) -> vol.Schema:
    suggested = suggested or {}
    fields = [
        _required_entity(CONF_VEHICLE_STATUS, "binary_sensor", suggested),
        _required_entity(CONF_WAKE_BUTTON, "button", suggested),
        _required_entity(CONF_BATTERY_LEVEL, "sensor", suggested),
        _required_entity(CONF_CHARGE_SWITCH, "switch", suggested),
        _required_entity(CONF_CHARGE_CABLE, "binary_sensor", suggested),
        _required_entity(CONF_CHARGE_CURRENT, "number", suggested),
        _required_entity(CONF_CHARGE_LIMIT, "number", suggested),
        _required_entity(CONF_ACCESSORY_SWITCH, "switch", suggested),
        _required_entity(CONF_GRID_NET_POWER, "sensor", suggested),
        _required_entity(CONF_L2_VOLTAGE, "sensor", suggested),
        _required_entity(CONF_SOLAR_POWER, "sensor", suggested),
        _optional_entity(CONF_FORECAST_TOMORROW, "sensor", suggested),
    ]
    return vol.Schema({marker: selector for marker, selector in fields})


def _suggested_entities(hass) -> dict[str, str]:
    known = {
        CONF_VEHICLE_STATUS: "binary_sensor.tesla_model_y_status",
        CONF_WAKE_BUTTON: "button.tesla_model_y_wake",
        CONF_BATTERY_LEVEL: "sensor.tesla_model_y_battery_level",
        CONF_CHARGE_SWITCH: "switch.tesla_model_y_charge",
        CONF_CHARGE_CABLE: "binary_sensor.tesla_model_y_charge_cable",
        CONF_CHARGE_CURRENT: "number.tesla_model_y_charge_current",
        CONF_CHARGE_LIMIT: "number.tesla_model_y_charge_limit",
        CONF_ACCESSORY_SWITCH: "switch.all_house_tesla_model_y_keep_accessory_power_on",
        CONF_GRID_NET_POWER: "sensor.sig_p1_meter_grid_net_power",
        CONF_L2_VOLTAGE: "sensor.sig_p1_meter_voltage_l2",
        CONF_SOLAR_POWER: "sensor.sma_webbox_192_168_1_254_0_gripwr",
    }
    return {key: value for key, value in known.items() if hass.states.get(value)}


def _num(min_value: float, max_value: float, step: float = 1.0) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=min_value,
            max=max_value,
            step=step,
            mode=NumberSelectorMode.BOX,
        )
    )


class TeslaSolarControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Tesla Solar Controller configuration."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title="Tesla Solar Controller",
                data=user_input,
                options=dict(DEFAULT_OPTIONS),
            )
        return self.async_show_form(
            step_id="user",
            data_schema=_entity_schema(_suggested_entities(self.hass)),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data=user_input)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_entity_schema(dict(entry.data)),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Create the options flow."""
        return TeslaSolarControllerOptionsFlow()


class TeslaSolarControllerOptionsFlow(config_entries.OptionsFlow):
    """Controller tuning options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = dict(DEFAULT_OPTIONS)
        options.update(self.config_entry.options)
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_MAINTENANCE_START_SOC,
                    description={"suggested_value": options[OPT_MAINTENANCE_START_SOC]},
                ): _num(50, 95, 1),
                vol.Required(
                    OPT_NORMAL_TARGET_SOC,
                    description={"suggested_value": options[OPT_NORMAL_TARGET_SOC]},
                ): _num(50, 95, 1),
                vol.Required(
                    OPT_BUFFER_TARGET_SOC,
                    description={"suggested_value": options[OPT_BUFFER_TARGET_SOC]},
                ): _num(50, 95, 1),
                vol.Required(
                    OPT_ADAPTIVE_BUFFER_ENABLED,
                    default=options[OPT_ADAPTIVE_BUFFER_ENABLED],
                ): BooleanSelector(),
                vol.Required(
                    OPT_POOR_FORECAST_THRESHOLD_KWH,
                    description={
                        "suggested_value": options[OPT_POOR_FORECAST_THRESHOLD_KWH]
                    },
                ): _num(0, 50, 0.5),
                vol.Required(
                    OPT_MIN_CURRENT_A,
                    description={"suggested_value": options[OPT_MIN_CURRENT_A]},
                ): _num(1, 32, 1),
                vol.Required(
                    OPT_OFFPEAK_CURRENT_A,
                    description={"suggested_value": options[OPT_OFFPEAK_CURRENT_A]},
                ): _num(1, 32, 1),
                vol.Required(
                    OPT_MAX_CURRENT_A,
                    description={"suggested_value": options[OPT_MAX_CURRENT_A]},
                ): _num(1, 32, 1),
                vol.Required(
                    OPT_SOLAR_START_EXPORT_W,
                    description={"suggested_value": options[OPT_SOLAR_START_EXPORT_W]},
                ): _num(0, 10000, 50),
                vol.Required(
                    OPT_MIN_SOLAR_PRODUCTION_W,
                    description={
                        "suggested_value": options[OPT_MIN_SOLAR_PRODUCTION_W]
                    },
                ): _num(1, 10000, 1),
                vol.Required(
                    OPT_SOLAR_WAKE_DELTA_SOC,
                    description={"suggested_value": options[OPT_SOLAR_WAKE_DELTA_SOC]},
                ): _num(0, 20, 1),
                vol.Required(
                    OPT_RECENT_PLUG_WINDOW_MINUTES,
                    description={
                        "suggested_value": options[OPT_RECENT_PLUG_WINDOW_MINUTES]
                    },
                ): _num(0, 60, 1),
                vol.Required(
                    OPT_SOLAR_START_MINUTES,
                    description={"suggested_value": options[OPT_SOLAR_START_MINUTES]},
                ): _num(0, 30, 0.5),
                vol.Required(
                    OPT_SOLAR_STEP_UP_EXPORT_W,
                    description={
                        "suggested_value": options[OPT_SOLAR_STEP_UP_EXPORT_W]
                    },
                ): _num(0, 5000, 50),
                vol.Required(
                    OPT_GRID_STEP_DOWN_IMPORT_W,
                    description={
                        "suggested_value": options[OPT_GRID_STEP_DOWN_IMPORT_W]
                    },
                ): _num(0, 5000, 50),
                vol.Required(
                    OPT_SOLAR_STOP_IMPORT_W,
                    description={"suggested_value": options[OPT_SOLAR_STOP_IMPORT_W]},
                ): _num(0, 5000, 50),
                vol.Required(
                    OPT_SOLAR_STOP_MINUTES,
                    description={
                        "suggested_value": max(10, options[OPT_SOLAR_STOP_MINUTES])
                    },
                ): _num(10, 30, 0.5),
                vol.Required(
                    OPT_WEEKDAY_OFFPEAK_END_HOUR,
                    description={
                        "suggested_value": options[OPT_WEEKDAY_OFFPEAK_END_HOUR]
                    },
                ): _num(0, 23, 1),
                vol.Required(
                    OPT_WEEKEND_OFFPEAK_END_HOUR,
                    description={
                        "suggested_value": options[OPT_WEEKEND_OFFPEAK_END_HOUR]
                    },
                ): _num(0, 23, 1),
                vol.Required(
                    OPT_EVENING_OFFPEAK_START_HOUR,
                    description={
                        "suggested_value": options[OPT_EVENING_OFFPEAK_START_HOUR]
                    },
                ): _num(0, 23, 1),
                vol.Optional(
                    OPT_NOTIFY_SERVICE,
                    description={"suggested_value": options[OPT_NOTIFY_SERVICE]},
                ): TextSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
