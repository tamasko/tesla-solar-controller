# AGENTS.md

## Project

Tesla Solar Controller is a Home Assistant custom integration. Keep control logic inside `custom_components/tesla_solar_controller/`; do not reintroduce Home Assistant helper booleans/selects or Tesla-specific `configuration.yaml` templates.

## Safety invariants

- When the controller Enabled switch is OFF, issue no Tesla command or forced
  refresh from this integration, including no final stop command.
- Charging starts, wakes, current changes, and charging-related limit changes
  require the configured charge-cable sensor to be positively ON. A no-wake
  safety stop remains allowed with positive evidence of an active charge.
- Never hard-code the normal SOC target as 80%. Use `OPT_NORMAL_TARGET_SOC`; 80% is only the default.
- Clamp every controller-issued charge-current command within the hard
  5–10 A range, always preserving the 10 A maximum ceiling.
- Solar starts/wakes use P1 export only: require 30 continuous minutes covering
  5 A times L2 voltage plus 200 W, and configured normal-target SOC hysteresis.
  No production/forecast gate or fresh-plug bypass remains.
- Solar charging starts at 5 A and stays within a hard 5–10 A range. Charge ASAP
  targets 100% at 10 A. Off-peak uses the configured normal target.
- Missing P1 prevents solar starts and falls back to scheduled off-peak charging
  in both solar modes; outside those hours, stop active solar charging safely.
- At 5 A, stop after 30 continuous minutes importing at least 300 W. Solar stops
  require a 60-minute cooldown followed by fresh start qualification.
- Limit controller activity to two wake attempts, two solar session attempts and
  150 total command attempts per HA-local day, across all modes. Count failures
  and retries; persist reservations before dispatch and reserve the last command
  for stopping. No forced refreshes. Counters survive reload/restart/toggles.
- Remove accessory power, automatic maintenance and forecast buffering entirely.
- Surplus diagnostics report unconsumed P1 export only and fail to 0 W internally
  for invalid P1. Do not use vehicle-power telemetry for solar control.
- Suppress identical Tesla commands for at least 120 seconds and bound retries.
  Current increases require 60 continuous seconds of export; decreases require
  30 seconds of import. Adjust by whole amps (possibly more than 1 A), with at
  least five minutes between solar adjustments.

## Release/versioning

For every release, keep these versions identical:

- `custom_components/tesla_solar_controller/const.py` → `VERSION`
- `custom_components/tesla_solar_controller/manifest.json` → `version`
- `CHANGELOG.md` → add the new release at the top

Update README/strings/translations when behavior or options change. Do not create a GitHub release until HACS validation and Hassfest are green.

## Validation

Before considering work complete:

1. Run `python3 -m compileall -q custom_components/tesla_solar_controller`.
2. Parse every JSON file with Python or `python3 -m json.tool`.
3. Run `git diff --check`.
4. Review the full diff for safety-invariant regressions.
5. Let GitHub Actions run HACS and Hassfest after push.

## Editing workflow

- Prefer complete, coherent file edits over tiny unrelated patches.
- Explain behavioral changes in terms of controller states and failure modes.
- Do not commit or push unless the user explicitly asks. The VS Code workspace is configured to automatically push after a successful manual VS Code commit.
