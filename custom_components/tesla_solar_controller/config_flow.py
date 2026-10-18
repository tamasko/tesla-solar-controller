"""Entity selection and options for P1-only charging."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_BATTERY_LEVEL,
    CONF_CHARGE_CABLE,
    CONF_CHARGE_CURRENT,
    CONF_CHARGE_LIMIT,
    CONF_CHARGE_SWITCH,
    CONF_GRID_NET_POWER,
    CONF_L2_VOLTAGE,
    CONF_VEHICLE_STATUS,
    CONF_WAKE_BUTTON,
    DEFAULT_OPTIONS,
    DOMAIN,
    OPT_EVENING_OFFPEAK_START_HOUR,
    OPT_NORMAL_TARGET_SOC,
    OPT_NOTIFY_SERVICE,
    OPT_OFFPEAK_CURRENT_A,
    OPT_SOLAR_WAKE_DELTA_SOC,
    OPT_WEEKDAY_OFFPEAK_END_HOUR,
    OPT_WEEKEND_OFFPEAK_END_HOUR,
)

ENTITY_DOMAINS = {
    CONF_VEHICLE_STATUS: "binary_sensor",
    CONF_WAKE_BUTTON: "button",
    CONF_BATTERY_LEVEL: "sensor",
    CONF_CHARGE_SWITCH: "switch",
    CONF_CHARGE_CABLE: "binary_sensor",
    CONF_CHARGE_CURRENT: "number",
    CONF_CHARGE_LIMIT: "number",
    CONF_GRID_NET_POWER: "sensor",
    CONF_L2_VOLTAGE: "sensor",
}


def _entity_schema(suggested):
    return vol.Schema(
        {
            vol.Required(
                key,
                description={"suggested_value": suggested[key]}
                if key in suggested
                else {},
            ): EntitySelector(EntitySelectorConfig(domain=domain))
            for key, domain in ENTITY_DOMAINS.items()
        }
    )


def _suggested_entities(hass):
    known = {
        CONF_VEHICLE_STATUS: "binary_sensor.tesla_model_y_status",
        CONF_WAKE_BUTTON: "button.tesla_model_y_wake",
        CONF_BATTERY_LEVEL: "sensor.tesla_model_y_battery_level",
        CONF_CHARGE_SWITCH: "switch.tesla_model_y_charge",
        CONF_CHARGE_CABLE: "binary_sensor.tesla_model_y_charge_cable",
        CONF_CHARGE_CURRENT: "number.tesla_model_y_charge_current",
        CONF_CHARGE_LIMIT: "number.tesla_model_y_charge_limit",
        CONF_GRID_NET_POWER: "sensor.sig_p1_meter_grid_net_power",
        CONF_L2_VOLTAGE: "sensor.sig_p1_meter_voltage_l2",
    }
    return {k: v for k, v in known.items() if hass.states.get(v)}


class TeslaSolarControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure existing Fleet and P1 entities."""

    VERSION = 2

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title="Tesla Solar Controller",
                data=user_input,
                options=dict(DEFAULT_OPTIONS),
            )
        return self.async_show_form(
            step_id="user", data_schema=_entity_schema(_suggested_entities(self.hass))
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data=user_input)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_entity_schema(dict(entry.data))
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TeslaSolarControllerOptionsFlow()


class TeslaSolarControllerOptionsFlow(config_entries.OptionsFlow):
    """Expose only settings still used by the controller."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = dict(DEFAULT_OPTIONS) | dict(self.config_entry.options)
        limits = {
            OPT_NORMAL_TARGET_SOC: (50, 95),
            OPT_SOLAR_WAKE_DELTA_SOC: (1, 20),
            OPT_OFFPEAK_CURRENT_A: (5, 10),
            OPT_WEEKDAY_OFFPEAK_END_HOUR: (0, 23),
            OPT_WEEKEND_OFFPEAK_END_HOUR: (0, 23),
            OPT_EVENING_OFFPEAK_START_HOUR: (0, 23),
        }
        fields = {
            vol.Required(
                key, description={"suggested_value": options[key]}
            ): NumberSelector(
                NumberSelectorConfig(
                    min=low, max=high, step=1, mode=NumberSelectorMode.BOX
                )
            )
            for key, (low, high) in limits.items()
        }
        fields[
            vol.Optional(
                OPT_NOTIFY_SERVICE,
                description={"suggested_value": options[OPT_NOTIFY_SERVICE]},
            )
        ] = TextSelector()
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))
