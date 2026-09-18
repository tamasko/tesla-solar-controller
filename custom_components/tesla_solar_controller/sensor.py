"""Sensor platform for Tesla Solar Controller."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfPower
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TeslaSolarConfigEntry
from .entity import TeslaSolarControllerEntity


async def async_setup_entry(
    hass, entry: TeslaSolarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller = entry.runtime_data
    async_add_entities(
        [
            TeslaStatusSensor(controller),
            TeslaSleepStatusSensor(controller),
            TeslaBatteryLevelSensor(controller),
            TeslaChargeCurrentSensor(controller),
            TeslaChargeLimitSensor(controller),
            TeslaLiveChargingPowerSensor(controller),
            TeslaSolarSurplusSensor(controller),
            TeslaAverageSolarPowerSensor(controller),
            TeslaAverageGridPowerSensor(controller),
            TeslaAverageChargingPowerSensor(controller),
            TeslaAverageSolarSurplusSensor(controller),
            TeslaTargetSocSensor(controller),
        ]
    )


class TeslaStatusSensor(TeslaSolarControllerEntity, SensorEntity):
    _attr_name = "Status"
    _attr_icon = "mdi:car-electric"

    def __init__(self, controller) -> None:
        super().__init__(controller, "status")

    @property
    def native_value(self) -> str:
        return self.controller.display_status

    @property
    def extra_state_attributes(self):
        return {
            "controller_enabled": self.controller.controller_enabled,
            "mode": self.controller.mode,
            "accessory_requested": self.controller.requested_accessory,
            "maintenance_active": self.controller.maintenance_active,
            "last_known_battery": self.controller.last_battery_pct,
            "poor_forecast": self.controller.poor_forecast,
            "tomorrow_forecast_kwh": self.controller.tomorrow_forecast_kwh,
            "off_peak": self.controller.off_peak,
            "charge_cable_connected": self.controller.charge_cable_connected,
            "last_known_charge_cable_connected": (
                self.controller.last_known_charge_cable_connected
            ),
            "solar_wake_delta_soc": self.controller.solar_wake_delta_soc,
            "solar_wake_threshold_soc": self.controller.solar_wake_threshold_soc,
            "recent_plug_active": self.controller.recent_plug_active,
            "recent_plug_window_minutes": self.controller.recent_plug_window_minutes,
        }


class TeslaSleepStatusSensor(TeslaSolarControllerEntity, SensorEntity):
    _attr_name = "Sleep status"
    _attr_icon = "mdi:sleep"

    def __init__(self, controller) -> None:
        super().__init__(controller, "sleep_status")

    @property
    def native_value(self) -> str:
        return self.controller.sleep_status


class TeslaBatteryLevelSensor(TeslaSolarControllerEntity, SensorEntity):
    _attr_name = "Battery level"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery"

    def __init__(self, controller) -> None:
        super().__init__(controller, "battery_level")

    @property
    def native_value(self) -> float | None:
        return self.controller.last_battery_pct


class TeslaChargeCurrentSensor(TeslaSolarControllerEntity, SensorEntity):
    _attr_name = "Charge current"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_icon = "mdi:current-ac"

    def __init__(self, controller) -> None:
        super().__init__(controller, "charge_current")

    @property
    def native_value(self) -> float | None:
        current = self.controller.charge_current_a
        return current if current is not None else self.controller.last_charge_current_a


class TeslaChargeLimitSensor(TeslaSolarControllerEntity, SensorEntity):
    _attr_name = "Charge limit"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-charging"

    def __init__(self, controller) -> None:
        super().__init__(controller, "charge_limit")

    @property
    def native_value(self) -> float | None:
        current = self.controller._float_state(self.controller.data.get("charge_limit"))
        return current if current is not None else self.controller.last_charge_limit_pct


class TeslaLiveChargingPowerSensor(TeslaSolarControllerEntity, SensorEntity):
    _attr_name = "Live charging power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, controller) -> None:
        super().__init__(controller, "live_charging_power")

    @property
    def native_value(self) -> float:
        return round(self.controller.live_charging_power_w)


class TeslaSolarSurplusSensor(TeslaSolarControllerEntity, SensorEntity):
    _attr_name = "Solar surplus available"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:solar-power"

    def __init__(self, controller) -> None:
        super().__init__(controller, "solar_surplus")

    @property
    def available(self) -> bool:
        # Control logic still fails closed to 0 W. Publishing an unavailable
        # measurement prevents a brief source outage from masquerading in
        # history as a real loss of solar surplus.
        return self.controller.solar_surplus_measurement_available

    @property
    def native_value(self) -> float:
        # Do not round upward: the published value must never exceed measured
        # solar production, including when the source has fractional watts.
        return self.controller.solar_surplus_available_w

    @property
    def extra_state_attributes(self):
        return {
            "measurement_reason": self.controller.solar_surplus_measurement_reason,
            "grid_net_power_w": self.controller.grid_net_power_w,
            "solar_production_w": self.controller.solar_power_w,
            "live_charging_power_w": self.controller.live_charging_power_w,
        }


class TeslaAveragePowerSensor(TeslaSolarControllerEntity, SensorEntity):
    """Base class for graphable five-minute power averages."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT


class TeslaAverageSolarPowerSensor(TeslaAveragePowerSensor):
    _attr_name = "Solar production 5-minute average"
    _attr_icon = "mdi:solar-power"

    def __init__(self, controller) -> None:
        super().__init__(controller, "solar_power_5_minute_average")

    @property
    def available(self) -> bool:
        return self.controller.solar_power_w is not None

    @property
    def native_value(self) -> float | None:
        return self.controller.averaged_solar_power_w


class TeslaAverageGridPowerSensor(TeslaAveragePowerSensor):
    _attr_name = "P1 grid net power 5-minute average"
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, controller) -> None:
        super().__init__(controller, "grid_power_5_minute_average")

    @property
    def available(self) -> bool:
        return self.controller.grid_net_power_w is not None

    @property
    def native_value(self) -> float | None:
        return self.controller.averaged_grid_net_power_w


class TeslaAverageChargingPowerSensor(TeslaAveragePowerSensor):
    _attr_name = "Tesla charging power 5-minute average"
    _attr_icon = "mdi:car-electric"

    def __init__(self, controller) -> None:
        super().__init__(controller, "charging_power_5_minute_average")

    @property
    def native_value(self) -> float:
        return self.controller.averaged_tesla_charging_power_w


class TeslaAverageSolarSurplusSensor(TeslaAveragePowerSensor):
    _attr_name = "Solar surplus 5-minute average"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, controller) -> None:
        super().__init__(controller, "solar_surplus_5_minute_average")

    @property
    def available(self) -> bool:
        return self.controller.solar_surplus_measurement_available

    @property
    def native_value(self) -> float:
        return self.controller.averaged_solar_surplus_w


class TeslaTargetSocSensor(TeslaSolarControllerEntity, SensorEntity):
    _attr_name = "Target SOC"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-charging"

    def __init__(self, controller) -> None:
        super().__init__(controller, "target_soc")

    @property
    def native_value(self) -> float:
        return round(self.controller.target_soc, 1)
