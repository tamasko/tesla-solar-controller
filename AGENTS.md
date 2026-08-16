# AGENTS.md

## Project

Tesla Solar Controller is a Home Assistant custom integration. Keep control logic inside `custom_components/tesla_solar_controller/`; do not reintroduce Home Assistant helper booleans/selects or Tesla-specific `configuration.yaml` templates.

## Safety invariants

- Never hard-code the normal SOC target as 80%. Use `OPT_NORMAL_TARGET_SOC`; 80% is only the default.
- Clamp every controller-issued charge-current command to `OPT_MAX_CURRENT_A`.
- In Solar only mode, missing grid-net data must fail closed: do not start solar charging and stop an active solar charge when safe to do so.
- Every new solar start/wake requires BOTH:
  1. grid export at least `OPT_SOLAR_START_EXPORT_W`, and
  2. measured solar production at least `OPT_MIN_SOLAR_PRODUCTION_W`.
- Missing/low solar production must never cause a solar wake, including during the fresh plug-in window.
- A fresh physical cable OFF→ON transition may bypass the normal solar hold time only during `OPT_RECENT_PLUG_WINDOW_MINUTES`; it must not bypass the two-sensor power thresholds or configured normal target.
- After the fresh plug window, new solar sessions use SOC hysteresis via `OPT_SOLAR_WAKE_DELTA_SOC`.
- Solar surplus may resume a meaningfully incomplete charge on a later sunny period/day, but should not cause nuisance restarts a point or two below target.
- The optional poor-forecast buffer may extend an already-awake/active solar session but must not wake a sleeping vehicle solely to add buffer SOC.
- Accessory power and Charge ASAP are explicit user intents and may wake the car.

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
