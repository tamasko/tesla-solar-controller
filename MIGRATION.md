 # Migration

## v0.1.7 → v0.1.8

No config-entry recreation is needed. After updating, the new
`switch.tesla_solar_controller_enabled` entity is created automatically and is
ON by default. Its value is persisted by the integration.

Charging commands now require the configured Tesla Fleet **Charge cable**
binary sensor to be positively ON. OFF, unknown, unavailable, or missing cable
data blocks charging wakes, starts, current changes, and charging-limit changes.
An active charge may still be stopped without a wake when a safety rule requires
it. Reconfigure the integration if its cable source is no longer valid.

Solar start/wake behavior still uses the existing fresh-plug window and SOC
hysteresis, but every solar start now rechecks both grid export and valid,
positive measured solar production at final command dispatch. Current changes
also use 60-second increase and 30-second decrease holds, with duplicate Tesla
commands suppressed while Fleet state catches up.

## v0.1.5 → v0.1.6

After updating, make sure **Settings → Devices & services → Tesla Solar Controller → Reconfigure** has the Tesla Fleet **Charge cable** binary sensor selected.

New behavior:

- A real OFF→ON plug-in opens a short immediate solar-start window (default 10 minutes).
- During that window, sufficient export can start charging immediately to the configured **Normal target SOC**.
- After the window expires, new solar sessions use the configured SOC hysteresis restart threshold.
- The normal target is not hard-coded at 80%; changing it in the integration options changes all of these decisions.

## v0.1.3 → v0.1.4

After updating, open **Settings → Devices & services → Tesla Solar Controller → Reconfigure** and select the Tesla Fleet **Charge cable** binary sensor. It detects fresh physical plug-in intent and is the primary permission gate for charging commands.


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
- Charge cable: `binary_sensor.tesla_model_y_charge_cable`
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
3. Select **Solar only**. A freshly plugged Tesla below target may start immediately when both configured grid-export and measured solar-production gates qualify; after the fresh-plug window, a new session should start only when SOC is at or below the configured hysteresis restart threshold.
4. With the cable positively connected, vehicle already awake, and both solar gates qualified, solar charging may start at the configured minimum current and regulate upward/downward.
5. If the battery is at or below the maintenance threshold and accessory power is requested, maintenance may wake the car and charge toward the target only while the cable is positively connected.

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
- `switch.tesla_solar_controller_enabled`
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
