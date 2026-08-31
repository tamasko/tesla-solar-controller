# Changelog

## 0.1.8 — 2026-08-30

- Add the persistent, default-ON **Tesla Solar Controller Enabled** switch. OFF
  makes the controller passive immediately and sends no final stop, Tesla
  command, wake, or forced refresh.
- Require the configured charge-cable sensor to be positively ON for every
  charging start, charging wake, current change, and charging-related limit
  change. Preserve no-wake safety stops for positively active charges.
- Recheck cable plus both live solar-start gates after wake delays and at the
  final service dispatcher so stale state cannot leak a start command.
- Keep fresh physical plug-ins immediate when both power gates qualify, while
  retaining configured normal-target SOC and post-window SOC hysteresis.
- Hold export continuously for 60 seconds before each 1 A increase and import
  continuously for 30 seconds before each 1 A decrease. Maximum-current safety
  corrections remain immediate.
- Suppress identical current, limit, start, stop, wake, and accessory commands
  for 120 seconds, allow only one bounded retry, and clear pending commands when
  Tesla Fleet state confirms them.
- Remove accessory command retry/refresh loops; confirmation now arrives through
  normal Tesla Fleet entity updates.
- Cap **Solar surplus available** at measured solar production and report 0 W
  whenever grid or solar data is invalid or solar production is non-positive.
- Clarify disabled, unplugged, fresh-plug, sustained-hold, automatic-wake, and
  SOC-hysteresis controller status messages.

## 0.1.7 — 2026-08-16

- Add a two-sensor safeguard for every solar start/wake decision.
- Require both configured grid export and measured solar production before fresh-plug or SOC-hysteresis solar charging may start.
- Add **Minimum solar production for solar start** option (default 300 W).
- If the solar-production sensor is unavailable or below the configured minimum, solar-only logic cannot wake/start the Tesla even if the grid sensor reports export.
- Keep solar-stop protection grid-based so sustained import can still stop an active minimum-current charge if the solar sensor becomes unavailable.
- Add VS Code project settings so a successful VS Code commit automatically pushes to the configured Git remote.
- Add repository-level `AGENTS.md` instructions so Codex consistently preserves charging safety invariants and the release workflow.

## 0.1.6 — 2026-08-16

- Combine fresh plug-in intent with long-term SOC hysteresis.
- A real charge-cable OFF→ON transition opens a configurable **Fresh plug-in solar-start window** (default 10 minutes).
- During that window, if SOC is below the configured **Normal target SOC** and grid export already exceeds the configured solar-start threshold, charging starts immediately with no solar hold delay, even if the Tesla has already fallen asleep.
- The plug-in target is the HACS-configured **Normal target SOC**; no 80% value is hard-coded.
- After the fresh plug-in window expires, any new solar session requires SOC to be at or below the configured hysteresis restart threshold and sustained solar export.
- The same hysteresis now applies to new solar starts even if the Tesla happens to be awake, preventing nuisance 79→80% restarts long after plug-in.
- Keep partially completed sessions resumable on later sunny periods/days when SOC is meaningfully below target.

## 0.1.5 — 2026-08-16

- Replace the one-time recent-plug solar wake rule with configurable SOC hysteresis.
- Add **Solar wake SOC hysteresis** option (default 3 percentage points).
- With the default 80% target, a sleeping Tesla stays asleep at 78-79% but may wake at 77% or lower once the solar-start threshold has been sustained.
- Allow partially completed solar sessions to resume automatically on a later sunny period/day, e.g. 65% -> 70% on a cloudy day and 70% -> 80% the next day.
- Keep the charge-cable sensor as optional diagnostic telemetry; it no longer gates solar wake behavior.
- Expose `solar_wake_delta_soc` and `solar_wake_threshold_soc` as status-sensor attributes for diagnostics.

## 0.1.4

- Add optional Tesla charge-cable binary sensor to the setup/reconfigure flow.
- Treat a fresh physical plug-in as explicit user intent for one 10-minute solar-start wake window.
- Fix the case where a newly plugged Tesla could fall asleep during the 2-minute solar hold time and therefore never start charging.
- Preserve the core rule that ordinary solar surplus does not wake an already sleeping vehicle.
- Remove duplicate `Sleeping` wording from the controller status.
- Fix source-state cache refresh to use the existing `_refresh_cached_values()` method.

## 0.1.3

- Restore HACS brand assets to `custom_components/tesla_solar_controller/brand/`.
- No controller logic changes from 0.1.2.

## 0.1.2

- Fix Home Assistant manifest key ordering for Hassfest.
- Move HACS brand assets to the repository-level `brand/` directory.
- Remove the daily validation schedule to avoid repeated failure emails.
- Update `actions/checkout` to v5 (Node.js 24 runtime).
- Remove Python bytecode/cache files from the distribution.

## 0.1.1 — 2026-08-14

Safety/fail-safe update after testing an ESP/grid-meter outage.

- Enforce configured maximum charge current independently of grid/solar sensor availability
- Clamp every controller-issued charge-current command to the configured maximum
- Fix ASAP mode so an existing current above the configured maximum is reduced instead of accepted
- Solar-only mode now fails closed: if grid-net power is unavailable, active solar charging is stopped and no new solar charge is started
- Solar + off-peak falls back to the configured off-peak current when grid-net power is unavailable
- Battery maintenance holds the configured minimum current even when the grid meter is unavailable
- Preserve the existing rule that solar surplus alone never wakes a sleeping Tesla

## 0.1.0 — 2026-08-13

Initial project version.

- UI config flow: no Tesla controller YAML required
- Central sleep-aware controller/state machine
- Solar only / Solar + off-peak / Charge ASAP modes
- Accessory-power wake and confirmation handling
- 78% maintenance restart and 80% normal target defaults
- Optional poor-forecast solar buffer to 83%
- Solar surplus alone never wakes a sleeping vehicle
- Cached battery/current/limit sensors while Tesla Fleet entities are unavailable during sleep
- Optional mobile notify service
- HACS and hassfest GitHub validation workflows
