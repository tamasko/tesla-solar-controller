# Wake-budget audit

Audited baseline: commit `78b275b`. The baseline references below identify the
original defect; the current references identify the fix. This is a repository
and deterministic state-machine audit. No live HA storage, installation timezone,
vehicle logs, or Tesla API calls were accessed.

## Root cause at the baseline

Solar preparation and scheduled off-peak preparation share `daily_wakes` and the
same `wake:on` retry record. Solar can spend both daily wakes before 22:00. The
scheduled start then fails the same daily check, even though scheduled charging
has priority over discretionary solar charging. There is no midnight timezone
bug in the ordinary forward-clock path and no unbounded wake dispatch loop.

Exact original locations in `custom_components/tesla_solar_controller/`:

| Baseline location | Finding |
| --- | --- |
| `const.py:46–50` | Successful-wake feedback wait: 720 seconds. Retry maximum: two. Daily wake maximum: two, commands: 150, solar sessions: two. |
| `controller.py:602–606` | `_start_charge()` rejects every sleeping-vehicle start at two wakes, including `offpeak`. |
| `controller.py:621–623` | Each eligible evaluation of sleeping-vehicle preparation requests another wake; no discretionary/scheduled distinction. |
| `controller.py:484–486` | Dispatch applies the same global wake rejection in every mode. |
| `controller.py:495–508` | Retry after 120 seconds for a failed call or 720 seconds for a successful call without awake feedback. Each reserved wake increments both daily counters. Failures legitimately exhaust the two-wake allowance. |
| `controller.py:249–257,267` | Initialization restores yesterday's date/counters/retry records and immediately saves them without rollover. Entities briefly expose old counters until startup evaluation. |
| `controller.py:405–414` | Rollover compares ISO HA-local dates using `>`; resets counters and eligible retry attempts, but does not clear the in-memory budget status. |
| `controller.py:699–711` | Normal evaluation rolls the date, evaluates fresh status, saves, and notifies listeners. This ordinarily clears a previous budget message; no permanent stale-status defect was reproduced after a successful evaluation. |
| `sensor.py:44–56` | Status and attributes directly expose controller values. The entity does not restore a persisted status string. |

A weekday 19:00 plug-in alone does not authorize a wake. With insufficient P1
export, the baseline already waits without Tesla commands. With qualified export,
solar preparation may wake and use both attempts before 22:00. The defect is
priority starvation of the later scheduled wake, rather than a plug-in bypass.

## Complete counter and time trace after the fix

All locations in this table are in `controller.py` unless another file is named.
There are no additional wake dispatchers or counter writers elsewhere in the
repository. Dashboard templates read attributes only. Configuration, migrations,
and historical changelogs do not dispatch commands or reset daily budgets.

| Path | Current location | Behavior |
| --- | --- | --- |
| Construction | 90–102 | Status starts as `Initializing`; budget date is empty; all three counters start at zero. |
| Load/reload | 302–333 | Restore enabled/mode, budget date/counters, sessions, cooldown and retry records. Roll forward before saving and before entities are set up. Same-day reload retains used allowances. Unfinished solar preparation retains the existing conservative cooldown rule. |
| HA lifecycle | `__init__.py:47–70`; 335–344; 346–356 | Initialize before forwarding entity platforms; evaluate at startup, on state changes, and every 30 seconds. Unload cancels pending evaluation and saves; it never resets counters. |
| Day rollover | 496–521 | Compare `dt_util.now().date().isoformat()` with `budget_day`. A newer local date resets commands, wakes and solar sessions together. Reset retry attempts except an unconfirmed charge start. Preserve last command timestamp, reason, feedback/success flags, cooldown, active session and pending start evidence. Clear obsolete budget/allowance status. |
| Rollover callers | 321,621,891 | Initialization, gated dispatch, and every evaluation, including evaluations while disabled. These operations alone issue no Tesla service call. Evaluation and initialization persist rollover; dispatch persists its reservation before service dispatch. |
| Clock rollback | 498–499 | A future saved date is intentionally retained. No replenishment until local date exceeds that saved date; the diagnostic reset timestamp reflects this deferral. DST does not reverse the local calendar date. |
| Off-peak schedule | 236–256 | HA-local weekday/hour comparisons: default weekday before 07:00 or from 22:00; weekend before 17:00 or from 22:00. `Solar + off-peak` selects scheduled grid charging; missing P1 enables the same fallback in `Solar only`. |
| Wake policy | 559–566; 782 | All modes share a hard maximum of two reserved wakes/day. Solar may use only the first global allowance, leaving the final one for off-peak/ASAP. Awake starts need no wake allowance. Solar session limit remains two, including already-awake starts. |
| Dispatch gates | 581–620 | Active integration, Enabled ON, unchanged control epoch, cable positively ON for wakes/starts/changes, and live preparation guard. Non-wake commands also need positive awake feedback. Only qualified no-wake stops may relax the cable gate. |
| Command budget | 622; 789; 941 | Preserve final command for stopping; new sessions need five commands of headroom. Scheduled charging still obeys the global command limit. |
| Duplicate/retry checks | 632–652 | Do nothing if feedback already matches. Honor 120-second duplicate suppression or 720-second successful-wake feedback wait. Bound retries to two per feedback episode. Suppressed calls do not increment counters. |
| Only increment | 653–677 | Add one command and, for wakes, one wake; write `wake:on` sent UTC timestamp, reason and attempt count. Persist before dispatch. Storage failure blocks the service call. Reservations remain consumed if persistence or later safety checks prevent dispatch. |
| Save/dispatch race | 679–726 | Recheck Enabled, cable, epoch and live guard after saving. Block yesterday's reservation if local date advanced during save. Call the configured HA wake button, never a forced-refresh service. Service errors retain the reservation; service success sets the success flag, saved by evaluation's final save. |
| Feedback episode | 733–743 | Positive awake feedback confirms wake. A later divergence reopens retry attempts, but cannot replenish the daily counter. |
| Preparation failure | 815–843 | Exhausted solar allowance also ends preparation after the appropriate feedback wait and applies solar cooldown. A successful wake keeps its full 12-minute opportunity to report awake. |
| Evaluation/status | 887–900; 937–1032 | Roll before evaluating; disabled exits before commands; confirmed unplugged/unknown blocks starts. Scheduled path precedes solar. Standby explicitly mentions off-peak. Finally save and notify entities. Actual global exhaustion can legitimately set `Daily wake budget reached` again. |
| Other updates | 358–393 | Options/mode/Enabled changes invalidate preparation or continuous timers; they do not reset daily counters or wake retry records. |
| Persistence | 1034–1058 | Save date, counters and command records in the existing per-entry HA Store (storage version unchanged). Status text itself is not saved. |
| Diagnostics | 265–282; `sensor.py:55`; `diagnostics.py:26` | Share wake count, limit, last reservation reason/time, HA timezone, and next reset timestamp without any Tesla command or refresh. Legacy stored wake records report an unknown reason until the next reservation. |

`dt_util.now()` uses HA's default timezone, whereas command `sent` times,
cooldown, adjustment and continuous-duration checks use UTC. See Home Assistant's
[datetime implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/util/dt.py).
Configure HA as `Europe/Zurich` to obtain Zurich schedule boundaries and resets.
Rollover occurs at the next evaluation, normally within 30 seconds of midnight,
rather than via a dedicated exact-midnight callback. Diagnostics calculate the
next calendar midnight with its actual timezone offset, including DST changes.

## Scope and remaining limits

No extra wake allowance was introduced. Two prior scheduled/ASAP wakes can still
block another scheduled wake that day. An installation already at two used wakes
when this fix is loaded keeps that count until its next legitimate daily reset.
Reloading or changing modes must not reclaim already-used allowance. The final
reservation also remains subject to duplicate suppression and awake feedback;
this fix never sends a forced refresh to accelerate polling.

All tests use fake HA services/storage and an injectable UTC clock with a separate
HA-local timezone. They cover winter/summer Zurich midnight, UTC midnight,
DST reset offsets, date rollback, same-day reload, restart after midnight,
status clearing, persisted reservations before a failed service call, solar
feedback delays, repeated failed wakes, reserved-wake preservation across reload
and mode changes, 19:00 standby/22:00 start, missing-P1 fallback, configurable SOC,
and disabled/cable gates before and after reservation. Existing tests retain
current clamps, solar qualification, cooldown, command ceilings and stop safety.
