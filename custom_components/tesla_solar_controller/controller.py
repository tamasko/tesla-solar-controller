"""P1-only state machine with persistent, conservative Tesla command budgets."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from math import ceil, floor, isfinite

from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    COMMAND_DUPLICATE_WINDOW_SECONDS,
    COMMAND_MAX_ATTEMPTS,
    CONF_BATTERY_LEVEL,
    CONF_CHARGE_CABLE,
    CONF_CHARGE_CURRENT,
    CONF_CHARGE_LIMIT,
    CONF_CHARGE_SWITCH,
    CONF_GRID_NET_POWER,
    CONF_L2_VOLTAGE,
    CONF_VEHICLE_STATUS,
    CONF_WAKE_BUTTON,
    CURRENT_ADJUSTMENT_INTERVAL_SECONDS,
    CURRENT_DECREASE_HOLD_SECONDS,
    CURRENT_INCREASE_HOLD_SECONDS,
    DEFAULT_OPTIONS,
    DOMAIN,
    EVALUATION_INTERVAL_SECONDS,
    MAX_CURRENT_A,
    MAX_DAILY_COMMANDS,
    MAX_DAILY_SOLAR_SESSIONS,
    MAX_DAILY_WAKES,
    MIN_CURRENT_A,
    MODE_ASAP,
    MODE_SOLAR_OFFPEAK,
    MODE_SOLAR_ONLY,
    MODES,
    OPT_EVENING_OFFPEAK_START_HOUR,
    OPT_NORMAL_TARGET_SOC,
    OPT_NOTIFY_SERVICE,
    OPT_OFFPEAK_CURRENT_A,
    OPT_SOLAR_WAKE_DELTA_SOC,
    OPT_WEEKDAY_OFFPEAK_END_HOUR,
    OPT_WEEKEND_OFFPEAK_END_HOUR,
    SOLAR_COOLDOWN_SECONDS,
    SOLAR_START_HOLD_SECONDS,
    SOLAR_START_MARGIN_W,
    SOLAR_STOP_HOLD_SECONDS,
    SOLAR_STOP_IMPORT_W,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    WAKE_FEEDBACK_WAIT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class TeslaSolarController:
    """Control existing Fleet entities; never poll or refresh Tesla forcibly."""

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.options = dict(DEFAULT_OPTIONS) | dict(entry.options)
        self._store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}"
        )
        self._listeners = []
        self._unsub_state = self._unsub_interval = None
        self._task = None
        self._dirty = False
        self._active = False
        self._epoch = 0
        self._lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self.controller_enabled = True
        self.mode = MODE_SOLAR_ONLY
        self.last_battery_pct = None
        self.last_charge_current_a = None
        self.last_charge_limit_pct = None
        self.status = "Initializing"
        self.budget_day = ""
        self.daily_commands = self.daily_wakes = self.daily_solar_sessions = 0
        self.session_kind = None
        self.starting = False
        self.stopping = False
        self._evaluation_epoch = None
        self.cooldown_until = None
        self._last_adjustment = None
        self._export_since = self._import_since = None
        self._increase_since = self._decrease_since = None
        self._commands = {}
        self._observed_charging = None

    @property
    def data(self):
        return self.entry.data

    @property
    def device_identifier(self):
        return (DOMAIN, self.entry.entry_id)

    @property
    def source_entity_ids(self):
        return [
            self.data[k]
            for k in (
                CONF_VEHICLE_STATUS,
                CONF_WAKE_BUTTON,
                CONF_BATTERY_LEVEL,
                CONF_CHARGE_SWITCH,
                CONF_CHARGE_CABLE,
                CONF_CHARGE_CURRENT,
                CONF_CHARGE_LIMIT,
                CONF_GRID_NET_POWER,
                CONF_L2_VOLTAGE,
            )
            if self.data.get(k)
        ]

    def _state(self, entity_id):
        state = self.hass.states.get(entity_id) if entity_id else None
        return state.state if state else None

    def _float_state(self, entity_id):
        try:
            value = float(self._state(entity_id))
            return value if isfinite(value) else None
        except (ValueError, TypeError):
            return None

    @property
    def charge_cable_connected(self):
        return {"on": True, "off": False}.get(
            self._state(self.data.get(CONF_CHARGE_CABLE))
        )

    @property
    def is_awake(self):
        return self._state(self.data.get(CONF_VEHICLE_STATUS)) == "on"

    @property
    def is_charging(self):
        return self._state(self.data.get(CONF_CHARGE_SWITCH)) == "on"

    @property
    def sleep_status(self):
        return "Awake" if self.is_awake else "Asleep / unavailable"

    @property
    def charge_current_a(self):
        return self._float_state(self.data.get(CONF_CHARGE_CURRENT))

    @property
    def grid_net_power_w(self):
        return self._float_state(self.data.get(CONF_GRID_NET_POWER))

    @property
    def l2_voltage_v(self):
        value = self._float_state(self.data.get(CONF_L2_VOLTAGE))
        return value if value is not None and 180 <= value <= 260 else 230.0

    @property
    def live_charging_power_w(self):
        return (
            (self.charge_current_a or 0) * self.l2_voltage_v
            if self.is_charging
            else 0.0
        )

    @property
    def solar_surplus_available_w(self):
        """Unconsumed export only; never add lagging vehicle power back into P1."""
        grid = self.grid_net_power_w
        return max(0.0, -grid) if grid is not None else 0.0

    @property
    def target_soc(self):
        return (
            100.0
            if self.mode == MODE_ASAP
            else float(self.options[OPT_NORMAL_TARGET_SOC])
        )

    @property
    def off_peak(self):
        now = dt_util.now()
        key = (
            OPT_WEEKEND_OFFPEAK_END_HOUR
            if now.weekday() >= 5
            else OPT_WEEKDAY_OFFPEAK_END_HOUR
        )
        return (
            now.hour < self.options[key]
            or now.hour >= self.options[OPT_EVENING_OFFPEAK_START_HOUR]
        )

    @property
    def grid_mode(self):
        if self.mode == MODE_ASAP:
            return "asap"
        if self.off_peak and (
            self.mode == MODE_SOLAR_OFFPEAK or self.grid_net_power_w is None
        ):
            return "offpeak"
        return None

    @property
    def display_status(self):
        return (
            self.status if self.controller_enabled else "Disabled — no Tesla commands"
        )

    def _refresh_cache(self):
        for key, attr in (
            (CONF_BATTERY_LEVEL, "last_battery_pct"),
            (CONF_CHARGE_CURRENT, "last_charge_current_a"),
            (CONF_CHARGE_LIMIT, "last_charge_limit_pct"),
        ):
            value = self._float_state(self.data.get(key))
            if value is not None:
                setattr(self, attr, value)

    @staticmethod
    def _date(raw):
        try:
            value = datetime.fromisoformat(raw) if raw else None
            return value if value is None or value.tzinfo is not None else None
        except (TypeError, ValueError):
            return None

    async def async_initialize(self):
        saved = await self._store.async_load() or {}
        self.controller_enabled = saved.get("controller_enabled", True) is True
        self.mode = saved.get("mode") if saved.get("mode") in MODES else MODE_SOLAR_ONLY
        for key in (
            "last_battery_pct",
            "last_charge_current_a",
            "last_charge_limit_pct",
        ):
            setattr(self, key, saved.get(key))
        self.budget_day = saved.get("budget_day", "")
        for key in ("daily_commands", "daily_wakes", "daily_solar_sessions"):
            setattr(self, key, max(0, int(saved.get(key, 0))))
        self.session_kind = saved.get("session_kind")
        self.starting = saved.get("starting", False)
        self.stopping = saved.get("stopping", False)
        self.cooldown_until = self._date(saved.get("cooldown_until"))
        self._last_adjustment = self._date(saved.get("last_adjustment"))
        self._commands = saved.get("commands", {})
        self._refresh_cache()
        self._observed_charging = self._state(self.data.get(CONF_CHARGE_SWITCH))
        # Reload must not recreate a qualified start from incomplete history.
        if self.starting and self.session_kind == "solar" and not self.is_charging:
            self.starting = False
            self.session_kind = None
            self.cooldown_until = dt_util.utcnow() + timedelta(
                seconds=SOLAR_COOLDOWN_SECONDS
            )
        await self._async_save()

    async def async_start(self):
        self._active = True
        self._unsub_state = async_track_state_change_event(
            self.hass, self.source_entity_ids, self._changed
        )
        self._unsub_interval = async_track_time_interval(
            self.hass, self._tick, timedelta(seconds=EVALUATION_INTERVAL_SECONDS)
        )
        self._schedule_evaluate("startup")

    async def async_stop(self):
        self._active = False
        self._epoch += 1
        for unsub in (self._unsub_state, self._unsub_interval):
            if unsub:
                unsub()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._async_save()

    async def async_options_updated(self, options):
        self.options = dict(DEFAULT_OPTIONS) | dict(options)
        self._epoch += 1
        self._reset_timers()
        self._schedule_evaluate("options")

    async def async_set_controller_enabled(self, value):
        if self.controller_enabled != value:
            self.controller_enabled = value
            self._epoch += 1
            self._reset_timers()
            # Invalidate any unfinished start without issuing a final stop.
            self.starting = False
        self._notify()
        await self._async_save()
        if value:
            self._schedule_evaluate("enabled")

    async def async_set_mode(self, mode, **kwargs):
        if mode not in MODES:
            return
        self.mode = mode
        self._epoch += 1
        self.starting = False
        self._reset_timers()
        await self._async_save()
        self._schedule_evaluate("mode")

    @callback
    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self):
        for listener in list(self._listeners):
            listener()

    @callback
    def _changed(self, event):
        self._refresh_cache()
        # Observe threshold breaks immediately, including between evaluations.
        self._update_timers()
        self._schedule_evaluate("source")

    @callback
    def _tick(self, now):
        self._schedule_evaluate("timer")

    def _schedule_evaluate(self, reason):
        if not self._active:
            return
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self._evaluate_loop())

    async def _evaluate_loop(self):
        while self._dirty and self._active:
            self._dirty = False
            try:
                await self.async_evaluate("scheduled")
            except Exception:
                _LOGGER.exception("Controller evaluation failed")
                self.status = "Controller error — see logs"
                self._notify()

    def _reset_timers(self):
        self._export_since = self._import_since = None
        self._increase_since = self._decrease_since = None

    @staticmethod
    def _held(since, seconds):
        return (
            since is not None and (dt_util.utcnow() - since).total_seconds() >= seconds
        )

    def _timer(self, attr, condition, now):
        setattr(self, attr, (getattr(self, attr) or now) if condition else None)

    def _update_timers(self):
        now = dt_util.utcnow()
        grid = self.grid_net_power_w
        enabled = self.controller_enabled and self.charge_cable_connected is True
        cooldown = self.cooldown_until is None or now >= self.cooldown_until
        self._timer(
            "_export_since",
            enabled
            and cooldown
            and grid is not None
            and grid <= -(MIN_CURRENT_A * self.l2_voltage_v + SOLAR_START_MARGIN_W),
            now,
        )
        self._timer(
            "_import_since",
            self.controller_enabled
            and self.is_charging
            and self.session_kind == "solar"
            and self.charge_current_a == MIN_CURRENT_A
            and grid is not None
            and grid >= SOLAR_STOP_IMPORT_W,
            now,
        )
        self._timer(
            "_increase_since",
            enabled
            and self.is_charging
            and grid is not None
            and grid <= -(self.l2_voltage_v + SOLAR_START_MARGIN_W),
            now,
        )
        self._timer(
            "_decrease_since",
            enabled and self.is_charging and grid is not None and grid >= 150,
            now,
        )

    def _roll_day(self):
        day = dt_util.now().date().isoformat()
        # Do not reset on clock rollback; counters survive reloads and updates.
        if day > self.budget_day:
            self.budget_day = day
            self.daily_commands = self.daily_wakes = self.daily_solar_sessions = 0
            for token, record in self._commands.items():
                # Preserve evidence of a possibly accepted start until cancelled.
                if token != "charge:on" or record.get("confirmed"):
                    record["attempts"] = 0

    def _solar_ready(self):
        battery = self.last_battery_pct
        threshold = self.target_soc - float(self.options[OPT_SOLAR_WAKE_DELTA_SOC])
        return (
            self.grid_mode is None
            and self.charge_cable_connected is True
            and battery is not None
            and 0 <= battery <= threshold
            and self._held(self._export_since, SOLAR_START_HOLD_SECONDS)
            and self.grid_net_power_w is not None
            and self.grid_net_power_w
            <= -(MIN_CURRENT_A * self.l2_voltage_v + SOLAR_START_MARGIN_W)
            and (self.starting or self.daily_solar_sessions < MAX_DAILY_SOLAR_SESSIONS)
        )

    def _start_allowed(self, kind):
        if kind not in ("solar", "offpeak", "asap"):
            return False
        battery = self.last_battery_pct
        if battery is None or not 0 <= battery < self.target_soc:
            return False
        if kind == "solar":
            return self._solar_ready()
        return self.grid_mode == kind and self.charge_cable_connected is True

    def _feedback(self, key):
        if key == "wake":
            return "on" if self.is_awake else None
        if key == "charge":
            return self._state(self.data.get(CONF_CHARGE_SWITCH))
        return self._float_state(
            self.data.get(
                CONF_CHARGE_CURRENT if key == "current" else CONF_CHARGE_LIMIT
            )
        )

    async def _dispatch(self, key, desired, *, guard=None):
        """Reserve and persist every attempt BEFORE calling HA, then recheck gates."""
        epoch = (
            self._evaluation_epoch
            if self._evaluation_epoch is not None
            else self._epoch
        )
        async with self._lock:
            stop = key == "charge" and desired == "off"

            def allowed():
                return (
                    self._active
                    and self.controller_enabled
                    and epoch == self._epoch
                    and (
                        self.charge_cable_connected is True
                        if not stop
                        else self.is_charging or self._unconfirmed_start()
                    )
                    # Fleet entity commands can auto-wake internally. Never use
                    # a stale charge switch as permission for an implicit wake.
                    and (key == "wake" or self.is_awake)
                    and (guard is None or guard())
                )

            if not allowed():
                return False
            self._roll_day()
            if self.daily_commands >= MAX_DAILY_COMMANDS - (0 if stop else 1):
                self.status = "Daily command budget reached"
                return False
            if key == "wake" and self.daily_wakes >= MAX_DAILY_WAKES:
                self.status = "Daily wake budget reached"
                return False
            if key == "current":
                desired = max(MIN_CURRENT_A, min(MAX_CURRENT_A, int(desired)))
            if self._feedback(key) == desired and not (
                stop and self._unconfirmed_start()
            ):
                return False
            token = f"{key}:{desired}"
            record = self._commands.get(token, {})
            sent = self._date(record.get("sent"))
            quiet_seconds = (
                WAKE_FEEDBACK_WAIT_SECONDS
                if key == "wake" and record.get("succeeded")
                else COMMAND_DUPLICATE_WINDOW_SECONDS
            )
            if sent and not self._held(sent, quiet_seconds):
                return False
            if record.get("attempts", 0) >= COMMAND_MAX_ATTEMPTS:
                self.status = f"Retry limit reached: {key}"
                return False
            self.daily_commands += 1
            if key == "wake":
                self.daily_wakes += 1
            self._commands[token] = {
                "sent": dt_util.utcnow().isoformat(),
                "attempts": record.get("attempts", 0) + 1,
                "confirmed": False,
            }
            if key == "current":
                self._last_adjustment = dt_util.utcnow()
            try:
                await self._async_save()
            except Exception:
                self.status = "Storage unavailable — command blocked"
                _LOGGER.exception("Cannot persist command reservation")
                return False
            # A reservation from yesterday must not dispatch against today's cap.
            if not allowed() or dt_util.now().date().isoformat() > self.budget_day:
                return False
            if key == "wake":
                domain, service, data = (
                    "button",
                    "press",
                    {"entity_id": self.data[CONF_WAKE_BUTTON]},
                )
            elif key == "charge":
                domain, service, data = (
                    "switch",
                    f"turn_{desired}",
                    {"entity_id": self.data[CONF_CHARGE_SWITCH]},
                )
            else:
                domain, service = "number", "set_value"
                data = {
                    "entity_id": self.data[
                        CONF_CHARGE_CURRENT if key == "current" else CONF_CHARGE_LIMIT
                    ],
                    "value": desired,
                }
            try:
                await self.hass.services.async_call(
                    domain, service, data, blocking=True
                )
            except Exception:
                _LOGGER.exception("Tesla command failed: %s", key)
                return False
            self._commands[token]["succeeded"] = True
            return True

    def _observe_feedback(self):
        for token, record in self._commands.items():
            key, raw = token.split(":", 1)
            desired = raw if key in ("wake", "charge") else float(raw)
            if self._feedback(key) == desired:
                record["confirmed"] = True
            elif record.get("confirmed"):
                # A confirmed command followed by real divergence is a new episode.
                record["attempts"] = 0

    def _unconfirmed_start(self):
        record = self._commands.get("charge:on", {})
        return bool(
            record and not record.get("confirmed") and record.get("attempts", 0)
        )

    async def _stop_charge(self, reason):
        self.status = reason
        if self.is_charging or self._unconfirmed_start():
            self.stopping = True
        sent = await self._dispatch("charge", "off")
        if sent:
            if self.session_kind == "solar":
                self.cooldown_until = dt_util.utcnow() + timedelta(
                    seconds=SOLAR_COOLDOWN_SECONDS
                )
            self.starting = False
            pending = self._commands.get("charge:on")
            if pending:
                pending["confirmed"] = True
                pending["attempts"] = 0
            self._reset_timers()
            await self._async_save()
        return sent

    async def _ensure(self, key, desired, guard):
        if self._feedback(key) == desired:
            return True
        await self._dispatch(key, desired, guard=guard)
        return False

    async def _start_charge(self, kind):
        self.status = f"Preparing {kind} charging"

        def guard():
            return self._start_allowed(kind)

        if not self.is_awake and self.daily_wakes >= MAX_DAILY_WAKES:
            self.status = "Daily wake budget reached"
            if self.starting:
                await self._abort_exhausted_start("wake", "on")
            return
        if not self.starting:
            # Leave enough headroom for wake, current, limit, start, and a stop.
            if self.daily_commands > MAX_DAILY_COMMANDS - 5:
                self.status = "Insufficient daily command budget to start"
                return
            if kind == "solar":
                if self.daily_solar_sessions >= MAX_DAILY_SOLAR_SESSIONS:
                    self.status = "Daily solar session budget reached"
                    return
                self.daily_solar_sessions += 1
            self.starting, self.session_kind = True, kind
            await self._async_save()
        if not guard():
            return
        if not self.is_awake:
            await self._dispatch("wake", "on", guard=guard)
            await self._abort_exhausted_start("wake", "on")
            return
        amps = MAX_CURRENT_A if kind == "asap" else MIN_CURRENT_A
        if not await self._ensure("current", amps, guard):
            await self._abort_exhausted_start("current", amps)
            return
        if not await self._ensure("limit", self.target_soc, guard):
            await self._abort_exhausted_start("limit", self.target_soc)
            return
        await self._dispatch("charge", "on", guard=guard)
        await self._abort_exhausted_start("charge", "on")

    async def _abort_exhausted_start(self, key, desired):
        record = self._commands.get(f"{key}:{desired}", {})
        sent = self._date(record.get("sent"))
        if (
            record.get("attempts", 0) < COMMAND_MAX_ATTEMPTS
            or not self._held(sent, COMMAND_DUPLICATE_WINDOW_SECONDS)
            or self._feedback(key) == desired
        ):
            return
        if self._unconfirmed_start():
            await self._stop_charge("Cancelling unconfirmed start after retry limit")
            return
        if self.session_kind == "solar":
            self.cooldown_until = dt_util.utcnow() + timedelta(
                seconds=SOLAR_COOLDOWN_SECONDS
            )
        self.starting = False
        self.session_kind = None
        self._reset_timers()
        self.status = "Start preparation failed — retry limit reached"
        await self._async_save()

    async def _regulate(self):
        amps, grid = self.charge_current_a, self.grid_net_power_w
        if amps is None or grid is None:
            return
        if self._last_adjustment and not self._held(
            self._last_adjustment, CURRENT_ADJUSTMENT_INTERVAL_SECONDS
        ):
            return
        desired = amps
        if self._held(self._increase_since, CURRENT_INCREASE_HOLD_SECONDS):
            desired += max(1, floor((-grid - SOLAR_START_MARGIN_W) / self.l2_voltage_v))
        elif self._held(self._decrease_since, CURRENT_DECREASE_HOLD_SECONDS):
            desired -= max(1, ceil(grid / self.l2_voltage_v))
        desired = max(MIN_CURRENT_A, min(MAX_CURRENT_A, int(desired)))
        if desired != amps:
            increasing = desired > amps
            await self._dispatch(
                "current",
                desired,
                guard=lambda: (
                    self.is_charging
                    and self.grid_mode is None
                    and self.grid_net_power_w is not None
                    and (
                        (
                            self.charge_current_a is not None
                            and self._held(
                                self._increase_since, CURRENT_INCREASE_HOLD_SECONDS
                            )
                            and self.grid_net_power_w
                            <= -(
                                max(1, desired - self.charge_current_a)
                                * self.l2_voltage_v
                                + SOLAR_START_MARGIN_W
                            )
                        )
                        if increasing
                        else self.grid_net_power_w >= 150
                    )
                ),
            )

    async def async_evaluate(self, reason):
        self._evaluation_epoch = self._epoch
        self._refresh_cache()
        self._observe_feedback()
        self._roll_day()
        self._update_timers()
        try:
            await self._evaluate()
        finally:
            self._evaluation_epoch = None
            await self._async_save()
            await self._notify_charge_transition()
            self._notify()

    async def _notify_charge_transition(self):
        """Keep optional local HA notifications separate from Tesla accounting."""
        state = self._state(self.data.get(CONF_CHARGE_SWITCH))
        if state not in ("on", "off"):
            return
        previous, self._observed_charging = self._observed_charging, state
        service_ref = str(self.options.get(OPT_NOTIFY_SERVICE, "")).strip()
        if (
            not self.controller_enabled
            or previous not in ("on", "off")
            or previous == state
        ):
            return
        domain, _, service = service_ref.partition(".")
        if domain != "notify" or not service:
            return
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {
                    "title": "Tesla charging",
                    "message": f"Charging {'started' if state == 'on' else 'stopped'} · {self.mode} · target {self.target_soc:.0f}%",
                },
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("Charging notification failed")

    async def _evaluate(self):
        if not self.controller_enabled:
            self._reset_timers()
            return
        if self.daily_commands >= MAX_DAILY_COMMANDS - 1:
            await self._stop_charge("Daily command budget reached — stopping")
            return
        if self.stopping:
            if self.is_charging or self._unconfirmed_start():
                await self._stop_charge("Waiting for charging stop confirmation")
                return
            self.stopping = False
            self.starting = False
            self.session_kind = None
        battery = self.last_battery_pct
        if battery is not None and battery >= self.target_soc:
            await self._stop_charge("Target SOC reached")
            return
        if self.charge_cable_connected is not True:
            await self._stop_charge("Waiting for cable positively connected")
            self.starting = False
            return
        if self.is_charging:
            self.starting = False
            kind = self.grid_mode
            if kind:
                self.session_kind = kind
            elif self.grid_net_power_w is None:
                await self._stop_charge("P1 unavailable — waiting for off-peak hours")
                return
            elif self.session_kind != "solar":
                # Switching out of grid charging requires a qualified solar session.
                await self._stop_charge("Grid charging window ended — qualifying solar")
                self.session_kind = None
                return
            self.status = f"Charging: {self.session_kind}"
            amps = self.charge_current_a
            if amps is not None and not MIN_CURRENT_A <= amps <= MAX_CURRENT_A:
                await self._dispatch(
                    "current",
                    max(MIN_CURRENT_A, min(MAX_CURRENT_A, int(amps))),
                    guard=lambda: self.is_charging,
                )
                return
            # A failed/lagging limit update must never postpone a required stop.
            if kind is None and self._held(self._import_since, SOLAR_STOP_HOLD_SECONDS):
                await self._stop_charge("Sustained import at 5 A — solar cooldown")
                return
            if not await self._ensure(
                "limit", self.target_soc, lambda: self.is_charging
            ):
                return
            if kind:
                desired = (
                    MAX_CURRENT_A
                    if kind == "asap"
                    else max(
                        MIN_CURRENT_A,
                        min(MAX_CURRENT_A, int(self.options[OPT_OFFPEAK_CURRENT_A])),
                    )
                )
                await self._ensure(
                    "current",
                    desired,
                    lambda: self.is_charging and self.grid_mode == kind,
                )
            else:
                await self._regulate()
            return
        if self._unconfirmed_start() and not self._start_allowed(self.session_kind):
            await self._stop_charge(
                "Cancelling charging start after conditions changed"
            )
            return
        if self.starting and not self._start_allowed(self.session_kind):
            if self.session_kind == "solar":
                self.cooldown_until = dt_util.utcnow() + timedelta(
                    seconds=SOLAR_COOLDOWN_SECONDS
                )
            self.starting = False
            self._reset_timers()
        kind = self.grid_mode
        if kind and self._start_allowed(kind):
            await self._start_charge(kind)
        elif self._solar_ready():
            await self._start_charge("solar")
        else:
            self.status = (
                "P1 unavailable — waiting for off-peak hours"
                if self.grid_net_power_w is None
                else "Waiting for sustained P1 export / SOC / cooldown / daily allowance"
            )

    async def _async_save(self):
        async with self._save_lock:
            await self._store.async_save(
                {
                    "controller_enabled": self.controller_enabled,
                    "mode": self.mode,
                    "last_battery_pct": self.last_battery_pct,
                    "last_charge_current_a": self.last_charge_current_a,
                    "last_charge_limit_pct": self.last_charge_limit_pct,
                    "budget_day": self.budget_day,
                    "daily_commands": self.daily_commands,
                    "daily_wakes": self.daily_wakes,
                    "daily_solar_sessions": self.daily_solar_sessions,
                    "session_kind": self.session_kind,
                    "starting": self.starting,
                    "stopping": self.stopping,
                    "cooldown_until": self.cooldown_until.isoformat()
                    if self.cooldown_until
                    else None,
                    "last_adjustment": self._last_adjustment.isoformat()
                    if self._last_adjustment
                    else None,
                    "commands": self._commands,
                }
            )
