# Tesla Solar Controller

![Tesla Solar Controller logo](custom_components/tesla_solar_controller/brand/icon.png)

A Home Assistant custom integration that controls existing Tesla Fleet entities using **SIG P1 grid export/import only**. It does not authenticate to Tesla itself. Control logic stays inside the integration; no YAML charging automations or helpers are required.

## Solar charging

- P1 is in watts: **positive = import, negative = export**. Suggested source: `sensor.sig_p1_meter_grid_net_power`.
- Every solar start requires **30 continuous minutes** of export covering **5 A × L2 voltage + 200 W** (1,350 W at 230 V). A threshold break resets the hold. Plugging in does not bypass it. No start-hour restriction applies.
- Starts use **5 A**, with an enforced **5–10 A** range. Charge ASAP starts at 10 A.
- Current increases require 60 continuous seconds of enough export to cover at least one extra amp plus 200 W. Decreases require 30 continuous seconds of at least 150 W import. Steps are whole amps and may exceed 1 A to follow a steep change.
- Solar current adjustments are at least **five minutes apart**, including retries. Starts, explicit charging-mode changes and corrections of out-of-range current are exempt from this regulation interval.
- At **5 A**, **30 continuous minutes importing at least 300 W** stops solar charging. A break below 300 W or leaving minimum current resets that timer. This deliberately permits some grid energy through clouds.
- After a solar stop, wait **60 minutes**, then qualify a fresh 30-minute export hold. At most **two solar start attempts per local day**, even if the car is already awake. Failed preparation conservatively consumes a session allowance.
- Every solar start also requires SOC at or below the configurable normal target minus the configurable hysteresis (default 3 percentage points). Fresh plug-ins follow the same rule. An active session continues to the normal target.
- Solar production, Forecast.Solar and lagging vehicle-power measurements never participate in surplus control. The **Solar surplus available** sensor now means unconsumed P1 export, `max(0, -P1)`, and excludes power already consumed by the car. Invalid P1 publishes unavailable and returns 0 W internally.

P1 shows current surplus, not future sunshine. The sustained-export hold and session limits reduce short charging sessions but cannot guarantee a sunny half-day. L2 voltage only converts amps to watts; invalid voltage uses 230 V.

## Three charging modes

| Mode | Behavior |
| --- | --- |
| Solar only | Qualified P1 solar charging. If P1 becomes unavailable, wait for scheduled off-peak hours and charge to the normal target. |
| Solar + off-peak | Scheduled grid charging during off-peak; qualified solar charging outside those hours. |
| Charge ASAP | Charge toward **100% at 10 A**, independent of P1. |

Solar and off-peak use the configurable normal target (80% default). Off-peak starts at 5 A and then uses its configured 5–10 A current (8 A default). When a grid-charging window ends or missing P1 recovers in Solar only, stop grid charging before qualifying a new solar start.

The retained default schedule is weekdays before 07:00 and from 22:00; weekends before 17:00 and from 22:00. Hours use Home Assistant's timezone. Change these in Configure if needed. Missing P1 stops an active solar charge outside this schedule; it never triggers immediate unrestricted grid charging. Accessory power, battery maintenance, forecast buffering have been removed. Optional charging notifications remain configurable through a HA notify service.

## Daily Tesla budgets and safety

- **Two wake attempts and 150 command attempts per HA-local calendar day**, across all three modes. Wakes count toward both budgets. Failed service calls and retries count too.
- Counter reservations and retry records are saved **before dispatch**. Reloading, restarting or toggling Enabled does not restore allowance. If storage fails, that command is blocked. The final command is reserved for stopping; reaching 149 attempts stops an active charge instead of continuing regulation. New sessions require at least five commands of headroom.
- Commands to other integrations or the Tesla app are outside this integration's accounting. A HA service invocation is counted as one attempt; internal Fleet polling, automatic wakes and transport retries are outside its visibility. These are controller dispatch limits, not a global Tesla API request meter.
- No forced Tesla refreshes are issued. Wake preparation waits for the existing Fleet entities to report the vehicle awake; it never starts on a wake timeout alone. After a successful wake service call, wait at least 12 minutes before another wake attempt to allow normal Fleet polling to catch up.
- Identical commands are suppressed for at least 120 seconds. Unconfirmed commands have at most two attempts per feedback episode; a confirmed state followed by divergence permits a new episode. Daily rollover also replenishes exhausted retries, except an unconfirmed charging start must first be resolved or cancelled.
- Wakes, starts, current changes and limit changes require the cable sensor **positively ON**. No last-known-cable wake exception. A safety stop can be issued without a connected cable when charging is positively active or this controller has an unconfirmed start attempt. All non-wake commands also require positive awake status, because Fleet entity commands can otherwise auto-wake internally.
- **Enabled OFF sends no command, including no final stop**, and no forced refresh. Existing charging continues untouched. The state persists. Disabling invalidates in-flight preparation; dispatch checks the switch and cable again after saving a reservation.
- Budgets and bounded retries can prevent a stop from succeeding after Tesla errors. Set the vehicle/EVSE's own current and charge limits; cloud commands are not a physical electrical limiter.

## Setup and upgrade

Install through HACS as a custom integration repository, restart Home Assistant, then add **Tesla Solar Controller** in Settings → Devices & services.

Select these existing entities:

- Tesla awake/status binary sensor and wake button
- Battery SOC sensor, charge switch and charge-cable binary sensor
- Charge-current and charge-limit number entities
- P1 net-power sensor and L2 voltage sensor

Version 0.2.0 migrates existing entries automatically, retaining these entity mappings, normal SOC target, hysteresis and off-peak settings. Obsolete production, forecast, accessory and maintenance settings are removed; the new fixed timing/current rules replace legacy tuning. Only obsolete entities belonging to this controller are removed from its entity registry. The real Tesla accessory switch is untouched; turn it off separately if it was previously left on.

Existing Enabled, charging mode, battery/current/limit/status, charging binary sensor, target and surplus entity unique IDs remain stable. Old five-minute power-average entities are removed. Update dashboards that referenced removed entities or the old surplus meaning.

The live Tesla dashboard configuration is saved in `dashboards/tesla.yaml`. It uses the installation's actual Enabled entity ID (`switch.all_house_tesla_solar_controller_enabled`), P1 net-power history and daily allowance cards. Until v0.2.0 is installed, the counters show "Pending update". Existing charging-energy and cost charts are retained.

The Status sensor exposes today's commands, wakes and solar sessions, budget date, session type, preparation state and cooldown deadline. Diagnostics include persisted command retry records. Reload resets continuous qualification timers; unfinished solar preparation restarts conservatively after cooldown. Daily counters and active-session identity survive reloads.

## Branding

The red solar-charging emblem has a transparent background and ships in `custom_components/tesla_solar_controller/brand/` as `icon.png` (256 px) and `icon@2x.png` (512 px), following [Home Assistant's local branding convention](https://contributors.home-assistant.io/blog/2026/02/24/brands-proxy-api/). Install the new integration version and restart Home Assistant to load the packaged assets.

Some HACS versions display "Icon not available" despite valid local branding; see [HACS issue #5402](https://github.com/hacs/integration/issues/5402). Changing these files cannot fix that frontend issue. Artwork generation details are in [the branding notes](docs/branding.md).

## Development

Single Tesla, single-phase L2 charging. Validate locally with:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q custom_components/tesla_solar_controller
python3 -c 'import json, pathlib; [json.loads(p.read_text()) for p in pathlib.Path(".").rglob("*.json") if ".git" not in p.parts]'
git diff --check
```

GitHub Actions runs HACS and Hassfest after push. Do not publish a release until both pass.
