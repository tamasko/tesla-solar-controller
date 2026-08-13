# Changelog

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
