# Migration from the YAML/automation controller

Do **not** delete the old controller until the custom integration has loaded and the GitHub validation checks have passed.

## Phase 1 — Freeze the old controller

Before adding the new Tesla Solar Controller config entry, disable these old automations if they still exist:

- Tesla - Charging mode controller
- Tesla - Solar charging controller
- Tesla - Accessory power controller
- Tesla - Accessory state sync
- Tesla - Refresh after intentional wake
- Tesla - Charging notifications
- any older Tesla - Refresh after accessory wake automation

Do not delete them yet. Disabling avoids two controllers sending conflicting commands to the Tesla.

## Phase 2 — Add and test the custom integration

Install through HACS, restart Home Assistant, then add **Tesla Solar Controller** from Settings → Devices & services.

For the current installation, the expected source entities are:

- Vehicle status: `binary_sensor.tesla_model_y_status`
- Wake button: `button.tesla_model_y_wake`
- Battery: `sensor.tesla_model_y_battery_level`
- Charge switch: `switch.tesla_model_y_charge`
- Charge current: `number.tesla_model_y_charge_current`
- Charge limit: `number.tesla_model_y_charge_limit`
- Keep Accessory Power: `switch.all_house_tesla_model_y_keep_accessory_power_on`
- Grid net power: `sensor.sig_p1_meter_grid_net_power`
- L2 voltage: `sensor.sig_p1_meter_voltage_l2`
- Solar power: `sensor.sma_webbox_192_168_1_254_0_gripwr`
- Forecast tomorrow: optional; select the Forecast.Solar "Estimated energy production - tomorrow" sensor if configured

Test these actions before deleting anything old:

1. Toggle the new **Accessory power** switch ON once while the car is sleeping. It should remain requested ON, wake the car, and eventually confirm the real Tesla setting.
2. Toggle it OFF once. It must stay OFF and must not bounce back ON.
3. Select **Solar only**. A sleeping Tesla must remain asleep even if grid export exceeds the solar-start threshold.
4. With the vehicle already awake and sufficient export, solar charging may start at the configured minimum current and regulate upward/downward.
5. If the battery is at or below the maintenance threshold and accessory power is requested, maintenance may wake the car and charge toward the target.

## Phase 3 — Delete the old helpers

Once the custom integration is verified, delete these helpers from Settings → Devices & services → Helpers:

- `input_select.tesla_charging_mode`
- `input_boolean.tesla_accessory_power`
- `input_boolean.tesla_battery_maintenance`

If an accidental duplicate helper with a similar name still exists, remove it as well after confirming nothing references it.

## Phase 4 — Delete the old automations

Delete the disabled automations listed in Phase 1.

## Phase 5 — Clean configuration.yaml

Remove only the old Tesla-specific template sensors from the `template:` section:

- Tesla Live Charging Power
- Solar Surplus Available
- Tesla Sleep Status
- Tesla Charging Status
- House Power Excluding Tesla

Keep unrelated template sensors such as `ECS_charge_level`, and keep unrelated configuration such as frontend themes, automation/script/scene includes and InfluxDB.

Because `configuration.yaml` may have changed since the original controller was built, use the **current live file** for this cleanup. Do not paste an old copy over it. After editing, run Home Assistant's configuration check and restart.

## Phase 6 — Update the dashboard

Replace references to the old helpers/template sensors with the new integration entities. Expected entity IDs are normally:

- `select.tesla_solar_controller_charging_mode`
- `switch.tesla_solar_controller_accessory_power`
- `sensor.tesla_solar_controller_status`
- `sensor.tesla_solar_controller_sleep_status`
- `sensor.tesla_solar_controller_battery_level`
- `sensor.tesla_solar_controller_charge_current`
- `sensor.tesla_solar_controller_charge_limit`
- `sensor.tesla_solar_controller_live_charging_power`
- `sensor.tesla_solar_controller_solar_surplus_available`
- `sensor.tesla_solar_controller_target_soc`
- `binary_sensor.tesla_solar_controller_battery_maintenance`
- `binary_sensor.tesla_solar_controller_charging`

Home Assistant may choose slightly different IDs if a name is already reserved. Confirm the actual IDs under Developer Tools → States before editing the dashboard.
