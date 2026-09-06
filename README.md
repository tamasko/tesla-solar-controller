# Tesla Solar Controller

A Home Assistant custom integration that coordinates an existing Tesla Fleet / Tesla Fleet Extra setup with local solar and grid sensors.

## Goals

- No Tesla-specific YAML in `configuration.yaml`
- No helper booleans/selects for controller state
- One central state machine instead of several overlapping automations
- Solar-only charging between configurable minimum and maximum currents (defaults 5–10 A)
- SIG-style off-peak mode
- Charge ASAP mode
- Keep Accessory Power control with sleep-aware command handling
- Configurable maintenance restart hysteresis (default 78%)
- Configurable normal target (default 80%)
- Optional poor-next-day-forecast solar buffer (default 83%)
- Fresh plug-in intent: when newly plugged in and both the grid-export and measured-production gates qualify, charge immediately toward the configured normal target
- Long-term SOC hysteresis: after the fresh plug-in window, new solar sessions start only when SOC is sufficiently below target
- Two-sensor solar-start safeguard: a solar start/wake requires both sufficient grid export and minimum measured solar production
- Optional mobile charging notifications

## Important architecture

This integration does **not** authenticate to Tesla and does not implement Tesla's command-signing protocol. It uses the entities already exposed by Home Assistant's Tesla Fleet integration and Tesla Fleet Extra.

## Required source entities

The setup flow asks for:

- Tesla awake/status binary sensor
- Tesla wake button
- Tesla battery level
- Tesla charge switch
- Tesla charge-cable connected binary sensor (required as the charging-command safety gate)
- Tesla charge-current number
- Tesla charge-limit number
- Tesla Keep Accessory Power switch
- grid net power (W; import positive, export negative)
- L2 voltage
- solar power
- optional Forecast.Solar "Estimated energy production - tomorrow" sensor

## Entities created

- `select.tesla_solar_controller_charging_mode`
- `switch.tesla_solar_controller_enabled` (persistent master command permission; default ON)
- `switch.tesla_solar_controller_accessory_power`
- `sensor.tesla_solar_controller_status`
- `sensor.tesla_solar_controller_sleep_status`
- `sensor.tesla_solar_controller_battery_level` (last valid SOC is retained while the car sleeps)
- `sensor.tesla_solar_controller_charge_current` (last valid current is retained while asleep)
- `sensor.tesla_solar_controller_charge_limit` (last valid limit is retained while asleep)
- `sensor.tesla_solar_controller_live_charging_power`
- `sensor.tesla_solar_controller_solar_surplus_available`
- `sensor.tesla_solar_controller_target_soc`
- `binary_sensor.tesla_solar_controller_battery_maintenance`
- `binary_sensor.tesla_solar_controller_charging`

Entity IDs can differ slightly if Home Assistant has existing names; use Developer Tools → States to confirm them.

## Safety and fail-safe behavior

- Turning **Tesla Solar Controller Enabled** OFF immediately makes this integration passive. It sends no wake, charging, charge-limit, accessory, stop, or forced-refresh command and leaves an active charge exactly as it is. The state persists across integration reloads, Home Assistant restarts/updates, and HACS updates.
- Turning the controller back ON performs one normal reevaluation. It does not reset the selected mode, fabricate a fresh plug-in, or wake the vehicle unless the selected mode and all normal gates allow it.
- This switch controls only Tesla Solar Controller. It does **not** disable the underlying Tesla Fleet or Tesla Fleet Extra integration.
- Charging starts, current changes, and charging-limit changes require the configured cable sensor to be positively ON. A sleeping vehicle may be woken for a fully qualified solar start when the cable was last validly ON and has since become unavailable; a valid OFF revokes that eligibility immediately. After wake, live cable ON is required before charging commands proceed. A positively active charge may still receive a no-wake safety stop.
- The configured **maximum current** is enforced independently of the ESP/grid sensor whenever the cable sensor is positively ON and the Tesla is awake or positively reports an active charge.
- Every charge-current command is clamped between the configured minimum and maximum, always preserving the maximum ceiling.
- In **Solar only**, an unavailable grid-net sensor causes charging to stop and prevents a new solar charge from starting.
- Every solar start/wake requires both sufficient valid grid export and valid, strictly positive solar production at/above **Minimum solar production for solar start** (default 300 W). Bogus night-time export with 0 W production cannot wake the Tesla.
- In **Solar + off-peak**, an unavailable grid-net sensor falls back to the configured off-peak current; it does not attempt solar regulation.
- Battery maintenance uses the configured minimum current even if the grid sensor is unavailable.
- **Solar surplus available** is 0 W when grid/solar data is invalid or solar is non-positive, and is capped at measured solar production.
- Identical current, start, stop, limit, wake, and accessory commands are suppressed for at least 120 seconds while Fleet state catches up; any retry is bounded.
- Current rises by exactly 1 A only after 60 continuous seconds of configured export and falls by exactly 1 A only after 30 continuous seconds of configured import. The existing multi-minute stop hold at minimum current remains in place.
- This is still cloud software, not a physical electrical safety limiter. Configure the vehicle/EVSE itself for any current that must never be exceeded.

## Charging modes

### Solar only

- Uses solar surplus and regulates current between the configured minimum and maximum.
- A real charge-cable OFF→ON transition opens the configurable **Fresh plug-in solar-start window** (default 10 minutes).
- During that fresh window, if SOC is below the configured **Normal target SOC**, export meets the solar-start threshold **and** measured solar production meets or exceeds its configured minimum, charging starts immediately with no hold delay, even if the car has already fallen asleep.
- The target is always the configured **Normal target SOC** (80% is only the default, not a hard-coded value).
- If either solar gate is short at plug-in, the controller waits during the fresh window and starts immediately if both sufficient export and measured production appear before the window expires.
- After the fresh window, new solar sessions use SOC hysteresis: the restart threshold is normal target minus **Solar wake SOC hysteresis**.
- Default example: normal target 80%, hysteresis 3 points -> a long-plugged car at 79/78% does not restart; 77% or lower can restart after the normal sustained-solar hold.
- If a cloudy day only charges 65% -> 70%, the next sufficiently sunny period can automatically wake the car and continue toward the configured normal target.
- Once a solar session is already running, it can continue toward target; hysteresis is for starting/restarting sessions, not for prematurely stopping an active one.

### Solar + off-peak

- Same solar behavior outside off-peak hours.
- During configured off-peak periods, the controller may wake the Tesla if the cable is positively connected and the last known SOC is below the normal target.
- Default SIG schedule used by this project:
  - weekdays: before 07:00 and from 22:00
  - weekends: before 17:00 and from 22:00

### Charge ASAP

- Remembers the requested mode while unplugged; with the cable positively connected, it explicitly wakes the vehicle.
- Sets charge limit to 100%.
- Uses configured maximum current.

## Accessory / battery maintenance

Default behavior:

- Accessory requested ON controls the real Tesla Keep Accessory Power switch and may wake the vehicle while unplugged.
- At or below the configured maintenance threshold (default 78%), battery maintenance is latched ON.
- Maintenance charging waits for a positively connected cable and charges toward the configured normal target.
- If tomorrow's solar forecast is poor **and** enough surplus is available while the Tesla is already awake, the target can extend to the configured buffer target (default 83%).
- Above the configured normal target, the controller will not buy grid power merely to reach the buffer target, and the buffer alone never wakes a sleeping vehicle.
- Once the target is reached, charging stops and the Tesla is allowed to sleep.
- A fresh physical plug-in can start an immediate solar session below the configured normal target; later restarts use the configured SOC hysteresis band.

## Installation via HACS custom repository

1. Put this repository on public GitHub.
2. HACS → Integrations → three-dot menu → Custom repositories.
3. Add the repository URL and choose category **Integration**.
4. Install **Tesla Solar Controller**.
5. Restart Home Assistant.
6. Settings → Devices & services → Add integration → **Tesla Solar Controller**.
7. Select the source entities.
8. Open the integration's **Configure** dialog to tune thresholds and optionally enter a mobile notification service such as `notify.mobile_app_tam14p`.

## Development status

This is a beta integration tailored to a single Tesla and a single-phase AC charging setup. Test carefully before relying on it unattended.
