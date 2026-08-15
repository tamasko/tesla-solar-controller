# Changelog

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
