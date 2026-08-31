"""Controller/state machine for Tesla Solar Controller."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from math import ceil, isfinite
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ACCESSORY_SYNC_DELAY_SECONDS,
    COMMAND_DUPLICATE_WINDOW_SECONDS,
    COMMAND_MAX_ATTEMPTS,
    CONF_ACCESSORY_SWITCH,
    CONF_BATTERY_LEVEL,
    CONF_CHARGE_CABLE,
    CONF_CHARGE_CURRENT,
    CONF_CHARGE_LIMIT,
    CONF_CHARGE_SWITCH,
    CONF_FORECAST_TOMORROW,
    CONF_GRID_NET_POWER,
    CONF_L2_VOLTAGE,
    CONF_SOLAR_POWER,
    CONF_VEHICLE_STATUS,
    CONF_WAKE_BUTTON,
    CURRENT_DECREASE_HOLD_SECONDS,
    CURRENT_INCREASE_HOLD_SECONDS,
    DEFAULT_OPTIONS,
    DOMAIN,
    EVALUATION_INTERVAL_SECONDS,
    MODE_ASAP,
    MODE_SOLAR_OFFPEAK,
    MODE_SOLAR_ONLY,
    MODES,
    OPT_ADAPTIVE_BUFFER_ENABLED,
    OPT_BUFFER_TARGET_SOC,
    OPT_EVENING_OFFPEAK_START_HOUR,
    OPT_GRID_STEP_DOWN_IMPORT_W,
    OPT_MAINTENANCE_START_SOC,
    OPT_MIN_SOLAR_PRODUCTION_W,
    OPT_MAX_CURRENT_A,
    OPT_MIN_CURRENT_A,
    OPT_NORMAL_TARGET_SOC,
    OPT_NOTIFY_SERVICE,
    OPT_OFFPEAK_CURRENT_A,
    OPT_POOR_FORECAST_THRESHOLD_KWH,
    OPT_RECENT_PLUG_WINDOW_MINUTES,
    OPT_SOLAR_START_EXPORT_W,
    OPT_SOLAR_WAKE_DELTA_SOC,
    OPT_SOLAR_START_MINUTES,
    OPT_SOLAR_STEP_UP_EXPORT_W,
    OPT_SOLAR_STOP_IMPORT_W,
    OPT_SOLAR_STOP_MINUTES,
    OPT_WEEKDAY_OFFPEAK_END_HOUR,
    OPT_WEEKEND_OFFPEAK_END_HOUR,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PendingVehicleCommand:
    """A bounded command awaiting confirmation from a source entity."""

    desired: str | float
    last_sent_at: datetime
    attempts: int = 1
    confirmation_requires_divergence: bool = False
    divergence_observed: bool = False


class TeslaSolarController:
    """Central controller for Tesla charging, accessory power, and sleep protection."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}"
        )
        self._listeners: list[Callable[[], None]] = []
        self._unsub_state = None
        self._unsub_interval = None
        self._command_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._accessory_task: asyncio.Task | None = None
        self._evaluate_task: asyncio.Task | None = None
        self._cache_save_task: asyncio.Task | None = None
        self._cache_save_pending = False
        self._evaluation_pending = False
        self._pending_evaluation_reason = "coalesced"
        self._runtime_active = False
        self._command_epoch = 0
        self._task_command_epochs: dict[asyncio.Task, int] = {}

        self.controller_enabled = True
        self.mode = MODE_SOLAR_ONLY
        self.requested_accessory = False
        self._accessory_command_needed = False
        self.maintenance_active = False
        self.last_battery_pct: float | None = None
        self.last_charge_current_a: float | None = None
        self.last_charge_limit_pct: float | None = None
        self.status = "Initializing"
        self.target_soc = float(DEFAULT_OPTIONS[OPT_NORMAL_TARGET_SOC])

        self._solar_export_since: datetime | None = None
        self._solar_import_since: datetime | None = None
        self._current_increase_since: datetime | None = None
        self._current_decrease_since: datetime | None = None
        self._actual_accessory_candidate: str | None = None
        self._actual_accessory_candidate_since: datetime | None = None
        self._recent_plug_at: datetime | None = None
        self._pending_vehicle_commands: dict[str, PendingVehicleCommand] = {}
        self._last_vehicle_commands: dict[
            tuple[str, str | float], datetime
        ] = {}
        self._confirmed_vehicle_commands: dict[str, str | float] = {}
        self._pending_charge_start_guard: Callable[[], bool] | None = None

        self._charging_notified = False
        self._charging_start_candidate: datetime | None = None
        self._charging_stop_candidate: datetime | None = None

        self.options = dict(DEFAULT_OPTIONS)
        self.options.update(entry.options)

    @property
    def data(self) -> dict[str, Any]:
        return dict(self.entry.data)

    @property
    def device_identifier(self) -> tuple[str, str]:
        return (DOMAIN, self.entry.entry_id)

    @property
    def source_entity_ids(self) -> list[str]:
        keys = [
            CONF_VEHICLE_STATUS,
            CONF_BATTERY_LEVEL,
            CONF_CHARGE_SWITCH,
            CONF_CHARGE_CURRENT,
            CONF_CHARGE_LIMIT,
            CONF_ACCESSORY_SWITCH,
            CONF_GRID_NET_POWER,
            CONF_L2_VOLTAGE,
            CONF_SOLAR_POWER,
        ]
        if self.data.get(CONF_CHARGE_CABLE):
            keys.append(CONF_CHARGE_CABLE)
        if self.data.get(CONF_FORECAST_TOMORROW):
            keys.append(CONF_FORECAST_TOMORROW)
        return [self.data[key] for key in keys if self.data.get(key)]

    async def async_initialize(self) -> None:
        """Restore persistent controller state before entities are created."""
        saved = await self._store.async_load() or {}
        saved_enabled = saved.get("controller_enabled", True)
        self.controller_enabled = (
            saved_enabled if isinstance(saved_enabled, bool) else True
        )
        mode = saved.get("mode")
        if mode in MODES:
            self.mode = mode
        self.requested_accessory = bool(saved.get("requested_accessory", False))
        self._accessory_command_needed = bool(
            saved.get("accessory_command_needed", False)
        )
        self.maintenance_active = bool(saved.get("maintenance_active", False))
        battery = saved.get("last_battery_pct")
        if isinstance(battery, (int, float)):
            self.last_battery_pct = float(battery)
        current = saved.get("last_charge_current_a")
        if isinstance(current, (int, float)):
            self.last_charge_current_a = float(current)
        limit = saved.get("last_charge_limit_pct")
        if isinstance(limit, (int, float)):
            self.last_charge_limit_pct = float(limit)
        self._refresh_cached_values()
        self._update_status()

    async def async_start(self) -> None:
        """Start listeners and local periodic evaluation."""
        self._runtime_active = True
        if self._unsub_state is None:
            self._unsub_state = async_track_state_change_event(
                self.hass, self.source_entity_ids, self._async_source_state_changed
            )
        if self._unsub_interval is None:
            self._unsub_interval = async_track_time_interval(
                self.hass,
                self._async_interval,
                timedelta(seconds=EVALUATION_INTERVAL_SECONDS),
            )
        self._schedule_evaluate("startup")

    async def async_stop(self) -> None:
        """Stop listeners and background tasks."""
        self._runtime_active = False
        self._command_epoch += 1
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        tasks = [
            task
            for task in (
                self._accessory_task,
                self._evaluate_task,
                self._cache_save_task,
            )
            if task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._accessory_task = None
        self._evaluate_task = None
        self._cache_save_task = None
        self._cache_save_pending = False
        await self._async_save()

    async def async_options_updated(self, options: dict[str, Any]) -> None:
        """Apply options without reloading the integration."""
        self._command_epoch += 1
        self.options = dict(DEFAULT_OPTIONS)
        self.options.update(options)
        # A hold accumulated under old thresholds cannot prove continuity under
        # the newly configured values.
        self._reset_control_timers()
        self._update_status()
        self._notify()
        self._schedule_evaluate("options_updated")

    async def async_set_controller_enabled(self, value: bool) -> None:
        """Persist the master command permission and reevaluate when re-enabled."""
        value = bool(value)
        if value == self.controller_enabled:
            return

        # Set the in-memory gate before the first await so any in-flight command
        # sequence sees OFF immediately at its final dispatch check.
        self.controller_enabled = value
        self._command_epoch += 1
        cancelled_tasks: list[asyncio.Task] = []
        if not value:
            self._reset_control_timers()
            if self._accessory_task and not self._accessory_task.done():
                self._accessory_task.cancel()
                cancelled_tasks.append(self._accessory_task)
                self._accessory_task = None
            if self._evaluate_task and not self._evaluate_task.done():
                self._evaluate_task.cancel()
                cancelled_tasks.append(self._evaluate_task)
                self._evaluate_task = None
                self._evaluation_pending = False

        if cancelled_tasks:
            await asyncio.gather(*cancelled_tasks, return_exceptions=True)

        await self._async_save()
        self._update_status()
        self._notify()

        if value:
            # This is a normal, coalesced evaluation. It neither resets the mode
            # nor creates a synthetic fresh-plug event or unconditional wake.
            self._schedule_evaluate("controller_enabled")

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register an entity update listener."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_set_mode(self, mode: str, *, restored: bool = False) -> None:
        """Set the requested charging mode."""
        if mode not in MODES:
            return
        changed = mode != self.mode
        if changed:
            self._command_epoch += 1
            self._reset_current_regulation_timers()
        self.mode = mode
        await self._async_save()
        self._update_status()
        self._notify()
        if restored:
            return
        if changed and mode == MODE_ASAP:
            self._schedule_evaluate("mode_asap")
        else:
            self._schedule_evaluate("mode_changed")

    async def async_set_accessory_requested(
        self, value: bool, *, restored: bool = False, vehicle_sync: bool = False
    ) -> None:
        """Set desired accessory-power state and optionally command the vehicle."""
        value = bool(value)
        if value != self.requested_accessory:
            # Invalidate in-flight work while retaining the opposite pending
            # command so the new intent can explicitly supersede it.
            self._command_epoch += 1
        self.requested_accessory = value
        if not value:
            self.maintenance_active = False
        if not restored and not vehicle_sync:
            self._accessory_command_needed = True
        await self._async_save()
        self._actual_accessory_candidate = None
        self._actual_accessory_candidate_since = None
        self._update_status()
        self._notify()

        if restored or vehicle_sync:
            self._schedule_evaluate("accessory_state_restored")
            return

        if not self.controller_enabled:
            return

        if self._accessory_task and not self._accessory_task.done():
            self._accessory_task.cancel()
        self._accessory_task = self.hass.async_create_task(
            self._async_command_accessory(value, self._command_epoch),
            "tesla_solar_controller_accessory_command",
        )

    @callback
    def _async_source_state_changed(self, event: Event) -> None:
        self._refresh_cached_values()
        self._confirm_pending_vehicle_commands()

        entity_id = event.data.get("entity_id")
        if entity_id == self.data.get(CONF_CHARGE_CABLE):
            old_state_obj = event.data.get("old_state")
            new_state_obj = event.data.get("new_state")
            old_state = old_state_obj.state if old_state_obj is not None else None
            new_state = new_state_obj.state if new_state_obj is not None else None
            self._handle_charge_cable_change(old_state, new_state)

        # Observe every source transition synchronously so a brief invalid or
        # below-threshold value cannot be hidden by a long in-flight evaluation.
        if self.controller_enabled:
            self._update_solar_timers()
            self._reset_current_timers_for_live_conditions()
        else:
            self._reset_control_timers()

        self._schedule_evaluate("source_state_changed")

    @callback
    def _async_interval(self, now: datetime) -> None:
        self._schedule_evaluate("interval")

    @callback
    def _schedule_evaluate(self, reason: str) -> None:
        if not self._runtime_active:
            return
        if self._evaluate_task and not self._evaluate_task.done():
            self._evaluation_pending = True
            self._pending_evaluation_reason = reason
            return
        self._evaluate_task = self.hass.async_create_task(
            self._async_evaluation_loop(reason), "tesla_solar_controller_evaluate"
        )

    async def _async_evaluation_loop(self, reason: str) -> None:
        """Run evaluations serially while coalescing callbacks received in flight."""
        task = asyncio.current_task()
        assert task is not None
        next_reason = reason
        try:
            while True:
                self._task_command_epochs[task] = self._command_epoch
                self._evaluation_pending = False
                await self.async_evaluate(next_reason)
                if not self._evaluation_pending:
                    return
                next_reason = self._pending_evaluation_reason
        finally:
            self._task_command_epochs.pop(task, None)

    @callback
    def _handle_charge_cable_change(
        self, old_state: str | None, new_state: str | None
    ) -> None:
        """Track physical cable connection and a short explicit-intent window.

        Only a real OFF -> ON transition counts as a fresh plug-in. An existing
        plugged-in car seen during Home Assistant startup/reload does not receive
        a new plug-in window.
        """
        if new_state == "on":
            if old_state == "off":
                self._recent_plug_at = dt_util.utcnow()
        else:
            # OFF and every non-authoritative state fail closed. If the sensor
            # later recovers directly to ON, that is not a proven OFF -> ON
            # physical transition and therefore does not fabricate plug intent.
            self._recent_plug_at = None

    @property
    def charge_cable_connected(self) -> bool | None:
        """Return a live, tri-state cable value for display and command gates."""
        state = self._state(self.data.get(CONF_CHARGE_CABLE))
        if state == "on":
            return True
        if state == "off":
            return False
        return None

    @property
    def charging_commands_allowed(self) -> bool:
        """Whether charging may be initiated or modified right now."""
        return self.controller_enabled and self.charge_cable_connected is True

    @property
    def recent_plug_window_minutes(self) -> float:
        """Configured duration of the fresh plug-in solar-start window."""
        return max(0.0, float(self.options[OPT_RECENT_PLUG_WINDOW_MINUTES]))

    @property
    def recent_plug_active(self) -> bool:
        """Whether a physical plug-in happened recently enough to count as intent."""
        if self.charge_cable_connected is not True or self._recent_plug_at is None:
            return False
        elapsed = (dt_util.utcnow() - self._recent_plug_at).total_seconds()
        return elapsed <= self.recent_plug_window_minutes * 60

    def _instant_solar_start_ready(self) -> bool:
        """Return whether export is already enough to start at minimum current.

        Unlike the normal solar-start rule, this intentionally has no hold time.
        It is used only during the short fresh plug-in window.
        """
        grid = self.grid_net_power_w
        if grid is None or not self._solar_production_ready():
            return False
        return grid <= -float(self.options[OPT_SOLAR_START_EXPORT_W])

    def _solar_production_ready(self) -> bool:
        """Return whether measured solar is positive and meets the start minimum."""
        solar = self.solar_power_w
        minimum = max(1.0, float(self.options[OPT_MIN_SOLAR_PRODUCTION_W]))
        return solar is not None and solar > 0 and solar >= minimum

    def _solar_start_deficit_w(self) -> float | None:
        """Return the additional watts needed to satisfy both solar start gates."""
        grid = self.grid_net_power_w
        solar = self.solar_power_w
        if grid is None or solar is None:
            return None
        export_deficit = max(
            0.0, float(self.options[OPT_SOLAR_START_EXPORT_W]) + grid
        )
        production_deficit = max(
            0.0,
            max(1.0, float(self.options[OPT_MIN_SOLAR_PRODUCTION_W])) - solar,
        )
        return max(export_deficit, production_deficit)

    @property
    def solar_wake_delta_soc(self) -> float:
        """Configured SOC gap below normal target required for a solar wake."""
        return max(0.0, float(self.options[OPT_SOLAR_WAKE_DELTA_SOC]))

    @property
    def solar_wake_threshold_soc(self) -> float:
        """SOC at or below which solar is allowed to wake a sleeping Tesla."""
        normal_target = float(self.options[OPT_NORMAL_TARGET_SOC])
        return max(0.0, normal_target - self.solar_wake_delta_soc)

    def _state(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        return state.state if state else None

    def _float_state(self, entity_id: str | None) -> float | None:
        raw = self._state(entity_id)
        if raw in (None, "unknown", "unavailable", "none", ""):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if isfinite(value) else None

    def _refresh_cached_values(self) -> None:
        changed = False
        battery = self._float_state(self.data.get(CONF_BATTERY_LEVEL))
        if (
            battery is not None
            and 0 <= battery <= 100
            and self.last_battery_pct != battery
        ):
            self.last_battery_pct = battery
            changed = True

        current = self._float_state(self.data.get(CONF_CHARGE_CURRENT))
        if (
            current is not None
            and current >= 0
            and self.last_charge_current_a != current
        ):
            self.last_charge_current_a = current
            changed = True

        limit = self._float_state(self.data.get(CONF_CHARGE_LIMIT))
        if (
            limit is not None
            and 0 <= limit <= 100
            and self.last_charge_limit_pct != limit
        ):
            self.last_charge_limit_pct = limit
            changed = True

        if changed:
            self._schedule_cache_save()

    def _schedule_cache_save(self) -> None:
        """Coalesce read-only cache persistence into one tracked task."""
        self._cache_save_pending = True
        if self._cache_save_task and not self._cache_save_task.done():
            return
        self._cache_save_task = self.hass.async_create_task(
            self._async_cache_save_loop(),
            "tesla_solar_controller_cache_save",
        )

    async def _async_cache_save_loop(self) -> None:
        while self._cache_save_pending:
            self._cache_save_pending = False
            await self._async_save()

    @property
    def is_awake(self) -> bool:
        return self._state(self.data.get(CONF_VEHICLE_STATUS)) == "on"

    @property
    def sleep_status(self) -> str:
        status = self._state(self.data.get(CONF_VEHICLE_STATUS))
        if status == "on":
            return "Awake"
        if status in ("off", "unknown", "unavailable", None):
            return "Sleeping"
        return "Unknown"

    @property
    def is_charging(self) -> bool:
        return self._state(self.data.get(CONF_CHARGE_SWITCH)) == "on"

    @property
    def charge_current_a(self) -> float | None:
        return self._float_state(self.data.get(CONF_CHARGE_CURRENT))

    @property
    def l2_voltage_v(self) -> float:
        return self._float_state(self.data.get(CONF_L2_VOLTAGE)) or 230.0

    @property
    def live_charging_power_w(self) -> float:
        if not self.is_charging:
            return 0.0
        amps = self.charge_current_a
        if amps is None:
            return 0.0
        return max(0.0, amps * self.l2_voltage_v)

    @property
    def grid_net_power_w(self) -> float | None:
        return self._float_state(self.data.get(CONF_GRID_NET_POWER))

    @property
    def solar_power_w(self) -> float | None:
        return self._float_state(self.data.get(CONF_SOLAR_POWER))

    @property
    def solar_surplus_available_w(self) -> float:
        grid = self.grid_net_power_w
        solar = self.solar_power_w
        if grid is None or solar is None or solar <= 0:
            return 0.0
        raw_available = max(0.0, self.live_charging_power_w - grid)
        return min(raw_available, solar)

    @property
    def minimum_current_a(self) -> float:
        """Configured minimum normalized against the maximum ceiling."""
        maximum = max(0.0, float(self.options[OPT_MAX_CURRENT_A]))
        return min(max(0.0, float(self.options[OPT_MIN_CURRENT_A])), maximum)

    @property
    def maximum_current_a(self) -> float:
        """Configured maximum controller charge current."""
        return max(0.0, float(self.options[OPT_MAX_CURRENT_A]))

    def _clamp_charge_current(self, amps: float) -> float:
        """Clamp every requested current to the configured controller range."""
        return min(max(float(amps), self.minimum_current_a), self.maximum_current_a)

    def _maximum_current_correction_required(self) -> bool:
        actual = self.charge_current_a
        return (
            self.charging_commands_allowed
            and (self.is_awake or self.is_charging)
            and actual is not None
            and actual > self.maximum_current_a
        )

    @property
    def tomorrow_forecast_kwh(self) -> float | None:
        entity_id = self.data.get(CONF_FORECAST_TOMORROW)
        return self._float_state(entity_id) if entity_id else None

    @property
    def poor_forecast(self) -> bool:
        if not bool(self.options[OPT_ADAPTIVE_BUFFER_ENABLED]):
            return False
        forecast = self.tomorrow_forecast_kwh
        if forecast is None:
            return False
        return forecast < float(self.options[OPT_POOR_FORECAST_THRESHOLD_KWH])

    @property
    def off_peak(self) -> bool:
        now = dt_util.now()
        hour = now.hour
        evening_start = int(self.options[OPT_EVENING_OFFPEAK_START_HOUR])
        if now.weekday() < 5:
            morning_end = int(self.options[OPT_WEEKDAY_OFFPEAK_END_HOUR])
        else:
            morning_end = int(self.options[OPT_WEEKEND_OFFPEAK_END_HOUR])
        return hour < morning_end or hour >= evening_start

    def _buffer_eligible(self) -> bool:
        if not self.requested_accessory or not self.poor_forecast or not self.is_awake:
            return False
        required = self.minimum_current_a * self.l2_voltage_v + 150.0
        return self.solar_surplus_available_w >= required

    def _desired_target_soc(self) -> float:
        if self.mode == MODE_ASAP:
            return 100.0
        normal_target = float(self.options[OPT_NORMAL_TARGET_SOC])
        if (
            self.last_battery_pct is not None
            and self.last_battery_pct >= normal_target
            and self._buffer_eligible()
        ):
            return float(self.options[OPT_BUFFER_TARGET_SOC])
        return normal_target

    @property
    def _solar_control_active(self) -> bool:
        """Whether the selected mode is currently governed by solar-only rules."""
        return self.mode == MODE_SOLAR_ONLY or (
            self.mode == MODE_SOLAR_OFFPEAK and not self.off_peak
        )

    def _fresh_solar_start_allowed(self) -> bool:
        battery = self.last_battery_pct
        normal_target = float(self.options[OPT_NORMAL_TARGET_SOC])
        return (
            self._solar_control_active
            and battery is not None
            and battery < normal_target
            and (
                (
                    self.recent_plug_active
                    and self._instant_solar_start_ready()
                )
                or self._held_solar_start_allowed()
            )
        )

    def _held_solar_start_allowed(self) -> bool:
        battery = self.last_battery_pct
        return (
            self._solar_control_active
            and battery is not None
            and battery <= self.solar_wake_threshold_soc
            and self._solar_start_ready()
        )

    def _asap_start_allowed(self) -> bool:
        battery = self.last_battery_pct
        return self.mode == MODE_ASAP and (battery is None or battery < 100.0)

    def _offpeak_start_allowed(self) -> bool:
        battery = self.last_battery_pct
        return (
            self.mode == MODE_SOLAR_OFFPEAK
            and self.off_peak
            and battery is not None
            and battery < float(self.options[OPT_NORMAL_TARGET_SOC])
        )

    def _solar_buffer_start_allowed(self) -> bool:
        battery = self.last_battery_pct
        if battery is None:
            return False
        return (
            self._solar_control_active
            and float(self.options[OPT_NORMAL_TARGET_SOC]) <= battery
            < float(self.options[OPT_BUFFER_TARGET_SOC])
            and self._buffer_eligible()
            and self._solar_start_ready()
        )

    def _offpeak_buffer_start_allowed(self) -> bool:
        battery = self.last_battery_pct
        if battery is None:
            return False
        return (
            self.mode == MODE_SOLAR_OFFPEAK
            and self.off_peak
            and float(self.options[OPT_NORMAL_TARGET_SOC]) <= battery
            < float(self.options[OPT_BUFFER_TARGET_SOC])
            and self._buffer_eligible()
            and self._solar_start_ready()
        )

    def _maintenance_start_allowed(self) -> bool:
        battery = self.last_battery_pct
        return (
            self.mode != MODE_ASAP
            and self.requested_accessory
            and self.maintenance_active
            and (battery is None or battery < self._desired_target_soc())
        )

    def _solar_active_session_allowed(self) -> bool:
        """Whether the current solar-governed session may still be adjusted."""
        battery = self.last_battery_pct
        normal_target = float(self.options[OPT_NORMAL_TARGET_SOC])
        buffer_target = float(self.options[OPT_BUFFER_TARGET_SOC])
        return self._solar_control_active and (
            battery is None
            or battery < normal_target
            or (
                battery < buffer_target
                and self._buffer_eligible()
            )
        )

    def _offpeak_active_session_allowed(self) -> bool:
        """Whether the current off-peak session may still be adjusted."""
        battery = self.last_battery_pct
        normal_target = float(self.options[OPT_NORMAL_TARGET_SOC])
        buffer_target = float(self.options[OPT_BUFFER_TARGET_SOC])
        return (
            self.mode == MODE_SOLAR_OFFPEAK
            and self.off_peak
            and (
                battery is None
                or battery < normal_target
                or (
                    battery < buffer_target
                    and self._buffer_eligible()
                )
            )
        )

    def _asap_active_session_allowed(self) -> bool:
        battery = self.last_battery_pct
        return (
            self.mode == MODE_ASAP
            and self.is_charging
            and self.is_awake
            and self.charging_commands_allowed
            and (battery is None or battery < 100.0)
        )

    def _maintenance_active_session_allowed(self) -> bool:
        return (
            self.is_charging
            and self.is_awake
            and self.charging_commands_allowed
            and self._maintenance_start_allowed()
        )

    def _target_stop_required(self) -> bool:
        """Whether current SOC still requires a normal/buffer target stop."""
        battery = self.last_battery_pct
        return (
            self.mode != MODE_ASAP
            and self.is_charging
            and battery is not None
            and battery >= self._desired_target_soc()
        )

    def _solar_dispatch_ready(self, bypass_hold: bool) -> bool:
        return (
            self._instant_solar_start_ready()
            if bypass_hold
            else self._solar_start_ready()
        )

    def _charge_start_pending(self) -> bool:
        pending = self._pending_vehicle_commands.get("charge_state")
        return pending is not None and pending.desired == "on"

    def _pending_charge_start_must_be_cancelled(self) -> bool:
        return (
            self._charge_start_pending()
            and self._pending_charge_start_guard is not None
            and not self._pending_charge_start_guard()
        )

    def _grid_loss_stop_required(self) -> bool:
        return (
            self._solar_control_active
            and not self.maintenance_active
            and (self.is_charging or self._charge_start_pending())
            and self.grid_net_power_w is None
        )

    def _minimum_solar_stop_required(self) -> bool:
        amps = self.charge_current_a
        return (
            self._solar_control_active
            and not self.maintenance_active
            and self.is_charging
            and amps is not None
            and amps <= self.minimum_current_a
            and self._solar_stop_ready()
        )

    async def async_evaluate(self, reason: str) -> None:
        """Evaluate control logic using cached/local HA state only."""
        try:
            self._refresh_cached_values()
            self._confirm_pending_vehicle_commands()
            self.target_soc = self._desired_target_soc()

            if not self.controller_enabled:
                self._reset_control_timers()
                self._update_status()
                self._notify()
                return

            self._update_solar_timers()

            battery = self.last_battery_pct
            normal_target = float(self.options[OPT_NORMAL_TARGET_SOC])
            maintenance_start = float(self.options[OPT_MAINTENANCE_START_SOC])

            if self.requested_accessory and battery is not None:
                if battery <= maintenance_start and not self.maintenance_active:
                    self.maintenance_active = True
                    await self._async_save()
                elif self.maintenance_active and battery >= self.target_soc:
                    self.maintenance_active = False
                    await self._async_save()

            if not self.requested_accessory and self.maintenance_active:
                self.maintenance_active = False
                await self._async_save()

            # Mandatory active-session safety stops precede accessory work,
            # command-lock shortcuts, limit alignment, and current correction.
            # They never wake and intentionally tolerate missing cable/status
            # telemetry when the charge switch positively reports ON.
            if self._pending_charge_start_must_be_cancelled():
                await self._async_stop_charge_if_active(
                    allow_pending_start=True,
                    final_guard=self._pending_charge_start_must_be_cancelled,
                )
                self._update_status()
                self._notify()
                return

            if self._grid_loss_stop_required():
                await self._async_stop_charge_if_active(
                    allow_pending_start=True,
                    final_guard=self._grid_loss_stop_required,
                )
                self._update_status()
                self._notify()
                return

            if self._minimum_solar_stop_required():
                await self._async_stop_charge_if_active(
                    final_guard=self._minimum_solar_stop_required
                )
                self._update_status()
                self._notify()
                return

            # Safety invariant: whenever the Tesla exposes a valid charge-current
            # value while available for an awake/active vehicle, never allow it
            # to remain above the configured controller maximum. This precedes
            # accessory work and is independent of the ESP/grid meter.
            if self._maximum_current_correction_required():
                max_current = self.maximum_current_a
                actual_current = self.charge_current_a
                _LOGGER.warning(
                    "Tesla charge current %.1f A exceeds configured maximum "
                    "%.1f A; clamping",
                    actual_current,
                    max_current,
                )
                await self._async_set_charge_current(
                    max_current,
                    require_awake=False,
                    final_guard=self._maximum_current_correction_required,
                )
                self._update_status()
                self._notify()
                return

            await self._async_sync_accessory_from_vehicle()
            if self._evaluation_pending:
                self._update_status()
                self._notify()
                return

            if self._accessory_command_needed:
                if not self._accessory_task or self._accessory_task.done():
                    self._accessory_task = self.hass.async_create_task(
                        self._async_command_accessory(
                            self.requested_accessory, self._command_epoch
                        ),
                        "tesla_solar_controller_accessory_command",
                    )
                self._update_status()
                self._notify()
                return

            # Keep the Tesla's actual charge limit aligned with the controller's
            # current target whenever the vehicle is already awake. This never
            # wakes a sleeping vehicle merely to change the limit.
            if self.is_awake and self.charging_commands_allowed:
                aligned_target = self.target_soc

                def target_alignment_still_allowed() -> bool:
                    return (
                        self.is_awake
                        and self.charging_commands_allowed
                        and self._command_values_match(
                            self._desired_target_soc(), aligned_target, 0.5
                        )
                    )

                await self._async_ensure_charge_limit(
                    aligned_target,
                    final_guard=target_alignment_still_allowed,
                )

            if self.mode == MODE_ASAP:
                await self._async_handle_asap(battery)
            elif self.maintenance_active:
                await self._async_handle_maintenance(battery, normal_target)
            elif self.mode == MODE_SOLAR_OFFPEAK and self.off_peak:
                await self._async_handle_offpeak(battery, normal_target)
            else:
                await self._async_handle_solar_only(battery, normal_target)

            await self._async_handle_notifications()
            self._update_status()
            self._notify()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Error evaluating Tesla Solar Controller (%s)", reason)

    def _update_solar_timers(self) -> None:
        now = dt_util.utcnow()
        grid = self.grid_net_power_w

        # Solar starts/wakes use two independent measurements. A negative grid
        # reading alone is never enough: the configured solar-production sensor
        # must also report meaningful generation. This prevents an erroneous
        # nighttime export reading from waking the Tesla.
        if grid is None:
            self._solar_export_since = None
            self._solar_import_since = None
            return

        if (
            self._solar_production_ready()
            and grid <= -float(self.options[OPT_SOLAR_START_EXPORT_W])
        ):
            self._solar_export_since = self._solar_export_since or now
        else:
            self._solar_export_since = None

        # Stopping an active solar charge remains grid-based. Even if the solar
        # sensor is temporarily unavailable, sustained grid import can still
        # safely stop a minimum-current session.
        if grid >= float(self.options[OPT_SOLAR_STOP_IMPORT_W]):
            self._solar_import_since = self._solar_import_since or now
        else:
            self._solar_import_since = None

    def _solar_start_ready(self) -> bool:
        if self._solar_export_since is None or not self._instant_solar_start_ready():
            return False
        elapsed = (dt_util.utcnow() - self._solar_export_since).total_seconds()
        return elapsed >= float(self.options[OPT_SOLAR_START_MINUTES]) * 60

    def _solar_stop_ready(self) -> bool:
        if self._solar_import_since is None:
            return False
        elapsed = (dt_util.utcnow() - self._solar_import_since).total_seconds()
        return elapsed >= float(self.options[OPT_SOLAR_STOP_MINUTES]) * 60

    def _reset_current_regulation_timers(self) -> None:
        self._current_increase_since = None
        self._current_decrease_since = None

    def _reset_control_timers(self) -> None:
        self._solar_export_since = None
        self._solar_import_since = None
        self._reset_current_regulation_timers()

    async def _async_handle_asap(self, battery: float | None) -> None:
        if battery is not None and battery >= 100:
            self.status = "ASAP target reached"
            return

        if not self.charging_commands_allowed:
            self._reset_current_regulation_timers()
            return

        max_current = self.maximum_current_a
        if self.is_charging:
            amps = self.charge_current_a
            if self.is_awake and (
                amps is None
                or abs(amps - max_current) >= 0.1
                or self._pending_command_opposes(
                    "charge_current", max_current
                )
            ):
                await self._async_set_charge_current(
                    max_current,
                    final_guard=self._asap_active_session_allowed,
                )
            shown = self.charge_current_a
            self.status = (
                f"ASAP · Charging at {shown:.0f} A"
                if shown is not None
                else "ASAP · Charging"
            )
            return

        await self._async_start_charge(
            limit=100.0,
            amps=max_current,
            allow_wake=True,
            reason="ASAP",
            start_allowed=self._asap_start_allowed,
        )

    async def _async_handle_maintenance(
        self, battery: float | None, normal_target: float
    ) -> None:
        if battery is not None and battery >= self.target_soc:
            self.maintenance_active = False
            await self._async_save()
            await self._async_stop_charge_if_active(
                final_guard=self._target_stop_required
            )
            return

        # From the normal target upward, only continue the solar buffer while
        # surplus remains genuinely available. Never buy grid energy solely for
        # the configured buffer extension.
        if battery is not None and battery >= normal_target:
            if not self._buffer_eligible():
                self.maintenance_active = False
                await self._async_save()
                await self._async_stop_charge_if_active(
                    final_guard=self._target_stop_required
                )
                return

        if not self.charging_commands_allowed:
            self._reset_current_regulation_timers()
            return

        maintenance_current = self.minimum_current_a
        if not self.is_charging:
            await self._async_start_charge(
                limit=self.target_soc,
                amps=maintenance_current,
                allow_wake=True,
                reason="battery maintenance",
                solar_start=battery is not None and battery >= normal_target,
                start_allowed=self._maintenance_start_allowed,
            )
            return

        # Battery maintenance is intentionally conservative and deterministic:
        # hold the configured minimum current regardless of grid-meter state.
        # This prevents a missing ESP/grid sensor from leaving the Tesla at a
        # previously higher current.
        amps = self.charge_current_a
        if self.is_awake and (
            amps is None
            or abs(amps - maintenance_current) >= 0.1
            or self._pending_command_opposes(
                "charge_current", maintenance_current
            )
        ):
            await self._async_set_charge_current(
                maintenance_current,
                final_guard=self._maintenance_active_session_allowed,
            )

    async def _async_handle_offpeak(
        self, battery: float | None, normal_target: float
    ) -> None:
        buffer_target = float(self.options[OPT_BUFFER_TARGET_SOC])

        # Once the normal target is reached, an optional configured buffer is
        # allowed only from real surplus while the car is already awake. The
        # cheap tariff is not used as a reason to buy grid energy for the buffer.
        if battery is not None and battery >= normal_target:
            if (
                battery < buffer_target
                and self._buffer_eligible()
                and self.is_awake
            ):
                if not self.is_charging:
                    await self._async_start_charge(
                        limit=buffer_target,
                        amps=self.minimum_current_a,
                        allow_wake=False,
                        reason="poor-forecast solar buffer",
                        solar_start=True,
                        start_allowed=self._offpeak_buffer_start_allowed,
                    )
                else:
                    await self._async_regulate_current(
                        minimum=self.minimum_current_a,
                        allow_stop=True,
                        session_guard=self._offpeak_active_session_allowed,
                    )
                return
            await self._async_stop_charge_if_active(
                final_guard=self._target_stop_required
            )
            return

        if not self.charging_commands_allowed:
            self._reset_current_regulation_timers()
            return

        if not self.is_charging:
            # Off-peak mode is allowed to wake the car, but only when we have a
            # valid last-known SOC below target. If SOC is unknown, do not wake
            # just to query it.
            if battery is None or battery >= normal_target:
                return
            await self._async_start_charge(
                limit=normal_target,
                amps=float(self.options[OPT_OFFPEAK_CURRENT_A]),
                allow_wake=True,
                reason="SIG off-peak",
                start_allowed=self._offpeak_start_allowed,
            )
            return

        offpeak_current = self._clamp_charge_current(
            float(self.options[OPT_OFFPEAK_CURRENT_A])
        )

        # If the grid meter is unavailable, off-peak charging may continue, but
        # only at the known-safe off-peak current. Do not leave a stale/higher
        # current in place when solar contribution cannot be measured.
        if self.grid_net_power_w is None:
            amps = self.charge_current_a
            if self.is_awake and (
                amps is None
                or abs(amps - offpeak_current) >= 0.1
                or self._pending_command_opposes(
                    "charge_current", offpeak_current
                )
            ):
                await self._async_set_charge_current(
                    offpeak_current,
                    final_guard=lambda: (
                        self._offpeak_active_session_allowed()
                        and self.grid_net_power_w is None
                    ),
                )
            return

        await self._async_regulate_current(
            minimum=offpeak_current,
            allow_stop=False,
            session_guard=self._offpeak_active_session_allowed,
        )

    async def _async_handle_solar_only(
        self, battery: float | None, normal_target: float
    ) -> None:
        # Missing grid-net data fails closed even when vehicle status is stale.
        # A positively ON charge switch is sufficient evidence for a no-wake
        # safety stop; cable state is deliberately not required for this stop.
        if self.grid_net_power_w is None:
            if self.is_charging:
                await self._async_stop_charge_if_active(
                    final_guard=self._grid_loss_stop_required
                )
            self.status = "Grid meter unavailable · Solar charging stopped"
            return

        buffer_target = float(self.options[OPT_BUFFER_TARGET_SOC])

        # Target completion and loss of buffer eligibility can safely stop a
        # known active charge without waking, even if cable telemetry is stale.
        if battery is not None and battery >= normal_target:
            if battery < buffer_target and self._buffer_eligible():
                if not self.charging_commands_allowed:
                    self._reset_current_regulation_timers()
                    return
                if not self.is_charging:
                    await self._async_start_charge(
                        limit=buffer_target,
                        amps=self.minimum_current_a,
                        allow_wake=False,
                        reason="poor-forecast solar buffer",
                        solar_start=True,
                        start_allowed=self._solar_buffer_start_allowed,
                    )
                else:
                    await self._async_regulate_current(
                        minimum=self.minimum_current_a,
                        allow_stop=True,
                        session_guard=self._solar_active_session_allowed,
                    )
                return
            await self._async_stop_charge_if_active(
                final_guard=self._target_stop_required
            )
            return

        if self.is_charging:
            await self._async_regulate_current(
                minimum=self.minimum_current_a,
                allow_stop=True,
                session_guard=self._solar_active_session_allowed,
            )
            return

        if not self.charging_commands_allowed:
            self._reset_current_regulation_timers()
            return

        # A fresh physical plug-in is explicit user intent. During the configured
        # short plug window, if the battery is below the configured normal target
        # and export is already sufficient, start immediately with no 2-minute
        # solar hold. This applies even if the Tesla has already fallen asleep.
        # The target here is never hard-coded: it is OPT_NORMAL_TARGET_SOC.
        if (
            not self.is_charging
            and self.recent_plug_active
            and battery is not None
            and battery < normal_target
            and self._instant_solar_start_ready()
        ):
            await self._async_start_charge(
                limit=normal_target,
                amps=self.minimum_current_a,
                allow_wake=True,
                reason=f"fresh plug-in solar to {normal_target:.0f}%",
                solar_start=True,
                bypass_solar_hold=True,
                start_allowed=self._fresh_solar_start_allowed,
            )
            return

        # Once the fresh plug-in window has expired, new solar sessions use SOC
        # hysteresis. This applies whether the Tesla happens to be awake or asleep:
        # being merely 1-2 points below target must not repeatedly restart charging.
        # Example with target 80% and delta 3%: 79/78% -> no new solar session;
        # 77% or lower + sustained export -> resume charging automatically.
        if not self.is_awake:
            if (
                battery is not None
                and battery <= self.solar_wake_threshold_soc
                and self._solar_start_ready()
            ):
                await self._async_start_charge(
                    limit=normal_target,
                    amps=self.minimum_current_a,
                    allow_wake=True,
                    reason=(
                        f"solar wake at {battery:.0f}% "
                        f"(threshold {self.solar_wake_threshold_soc:.0f}%)"
                    ),
                    solar_start=True,
                    start_allowed=self._held_solar_start_allowed,
                )
            return

        # After the recent plug window, do not start a new session merely because
        # the vehicle happens to be awake. Require the same SOC hysteresis used for
        # waking a sleeping car.
        if battery is None or battery > self.solar_wake_threshold_soc:
            return

        if self._solar_start_ready():
            await self._async_start_charge(
                limit=normal_target,
                amps=self.minimum_current_a,
                allow_wake=False,
                reason="solar surplus below restart threshold",
                solar_start=True,
                start_allowed=self._held_solar_start_allowed,
            )

    async def _async_regulate_current(
        self,
        minimum: float,
        allow_stop: bool,
        *,
        session_guard: Callable[[], bool] | None = None,
    ) -> None:
        minimum = self._clamp_charge_current(minimum)
        grid = self.grid_net_power_w
        amps = self.charge_current_a
        if grid is None or amps is None:
            self._reset_current_regulation_timers()
            return

        def minimum_stop_still_required() -> bool:
            current = self.charge_current_a
            return (
                allow_stop
                and self.is_charging
                and current is not None
                and current <= minimum
                and self._solar_stop_ready()
            )

        def increase_still_ready() -> bool:
            current_grid = self.grid_net_power_w
            current_amps = self.charge_current_a
            return (
                self.is_charging
                and self.is_awake
                and self.charging_commands_allowed
                and (session_guard is None or session_guard())
                and current_grid is not None
                and current_amps is not None
                and self._command_values_match(current_amps, amps, 0.1)
                and self._solar_production_ready()
                and current_grid
                <= -float(self.options[OPT_SOLAR_STEP_UP_EXPORT_W])
                and current_amps < self.maximum_current_a
            )

        def decrease_still_ready() -> bool:
            current_grid = self.grid_net_power_w
            current_amps = self.charge_current_a
            return (
                self.is_charging
                and self.is_awake
                and self.charging_commands_allowed
                and (session_guard is None or session_guard())
                and current_grid is not None
                and current_amps is not None
                and self._command_values_match(current_amps, amps, 0.1)
                and current_grid
                >= float(self.options[OPT_GRID_STEP_DOWN_IMPORT_W])
                and current_amps > minimum
            )

        # A no-wake stop remains permitted with positive charging evidence even
        # when cable/vehicle-status telemetry is unavailable.
        if minimum_stop_still_required():
            self._reset_current_regulation_timers()
            await self._async_stop_charge_if_active(
                final_guard=minimum_stop_still_required
            )
            return

        if not self.is_awake or not self.charging_commands_allowed:
            self._reset_current_regulation_timers()
            return

        now = dt_util.utcnow()
        increase_ready = increase_still_ready()
        decrease_ready = decrease_still_ready()

        if increase_ready:
            self._current_decrease_since = None
            self._current_increase_since = self._current_increase_since or now
            if (
                now - self._current_increase_since
            ).total_seconds() >= CURRENT_INCREASE_HOLD_SECONDS:
                sent = await self._async_set_charge_current(
                    min(amps + 1.0, self.maximum_current_a),
                    final_guard=increase_still_ready,
                )
                if sent:
                    self._current_increase_since = None
            return

        self._current_increase_since = None
        if decrease_ready:
            self._current_decrease_since = self._current_decrease_since or now
            if (
                now - self._current_decrease_since
            ).total_seconds() >= CURRENT_DECREASE_HOLD_SECONDS:
                sent = await self._async_set_charge_current(
                    max(amps - 1.0, minimum),
                    final_guard=decrease_still_ready,
                )
                if sent:
                    self._current_decrease_since = None
            return

        self._current_decrease_since = None

    def _reset_current_timers_for_live_conditions(self) -> None:
        """Reset regulation holds immediately when their live condition breaks."""
        if (
            not self.is_charging
            or not self.is_awake
            or not self.charging_commands_allowed
        ):
            self._reset_current_regulation_timers()
            return

        grid = self.grid_net_power_w
        if (
            grid is None
            or not self._solar_production_ready()
            or grid > -float(self.options[OPT_SOLAR_STEP_UP_EXPORT_W])
        ):
            self._current_increase_since = None
        if grid is None or grid < float(self.options[OPT_GRID_STEP_DOWN_IMPORT_W]):
            self._current_decrease_since = None

    async def _async_command_accessory(
        self, turn_on: bool, command_epoch: int
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._task_command_epochs[task] = command_epoch
        try:
            expected = "on" if turn_on else "off"
            actual = self._state(self.data.get(CONF_ACCESSORY_SWITCH))
            opposite_pending = self._pending_command_opposes(
                "accessory_power", expected
            )
            if (actual != expected or opposite_pending) and self.controller_enabled:
                request_still_current = lambda: self.requested_accessory == turn_on
                if not self.is_awake:
                    await self._async_wake_vehicle(
                        charging=False, final_guard=request_still_current
                    )
                await self._async_vehicle_command(
                    command_key="accessory_power",
                    desired=expected,
                    domain="switch",
                    service="turn_on" if turn_on else "turn_off",
                    data={"entity_id": self.data[CONF_ACCESSORY_SWITCH]},
                    final_guard=request_still_current,
                )
            elif actual == expected:
                self._observe_command_feedback("accessory_power", actual)

            if (
                self.controller_enabled
                and command_epoch == self._command_epoch
                and self.requested_accessory == turn_on
            ):
                actual_after = self._state(self.data.get(CONF_ACCESSORY_SWITCH))
                pending = self._pending_vehicle_commands.get("accessory_power")
                if (
                    actual_after == expected
                    and not self._pending_command_opposes(
                        "accessory_power", expected
                    )
                ):
                    self._accessory_command_needed = False
                else:
                    self._accessory_command_needed = (
                        pending is not None
                        and self._command_values_match(pending.desired, expected)
                        and pending.attempts < COMMAND_MAX_ATTEMPTS
                    )
                await self._async_save()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Accessory power command failed")
        finally:
            self._task_command_epochs.pop(task, None)
            self._actual_accessory_candidate = None
            self._actual_accessory_candidate_since = None
            if self.controller_enabled and self._runtime_active:
                self._schedule_evaluate("accessory_command_complete")

    async def _async_sync_accessory_from_vehicle(self) -> None:
        """Mirror stable Tesla-app changes back into the custom switch."""
        actual = self._state(self.data.get(CONF_ACCESSORY_SWITCH))
        if actual not in ("on", "off"):
            self._actual_accessory_candidate = None
            self._actual_accessory_candidate_since = None
            return

        desired = "on" if self.requested_accessory else "off"
        pending = self._pending_vehicle_commands.get("accessory_power")
        if (
            pending is not None
            and pending.attempts >= COMMAND_MAX_ATTEMPTS
            and (dt_util.utcnow() - pending.last_sent_at).total_seconds()
            >= COMMAND_DUPLICATE_WINDOW_SECONDS
            and actual != pending.desired
        ):
            _LOGGER.warning(
                "Accessory power did not confirm after %d attempts; "
                "returning to normal vehicle-state synchronization",
                pending.attempts,
            )
            self._pending_vehicle_commands.pop("accessory_power", None)
            pending = None

        if actual == desired:
            # If the opposite command is still pending, matching live state is
            # the pre-command state, not confirmation of the user's reversal.
            # Keep it pending so the accessory task can explicitly supersede it.
            if not self._pending_command_opposes("accessory_power", desired):
                self._observe_command_feedback("accessory_power", actual)
                if self._accessory_command_needed:
                    self._accessory_command_needed = False
                    await self._async_save()
            self._actual_accessory_candidate = None
            self._actual_accessory_candidate_since = None
            return

        # Do not reinterpret slow Fleet feedback for a local command as a Tesla
        # app change. The pending command is cleared when its source confirms.
        if self._accessory_command_needed or pending is not None:
            self._actual_accessory_candidate = None
            self._actual_accessory_candidate_since = None
            return

        now = dt_util.utcnow()
        if self._actual_accessory_candidate != actual:
            self._actual_accessory_candidate = actual
            self._actual_accessory_candidate_since = now
            return

        if self._actual_accessory_candidate_since is None:
            self._actual_accessory_candidate_since = now
            return

        if (
            now - self._actual_accessory_candidate_since
        ).total_seconds() < ACCESSORY_SYNC_DELAY_SECONDS:
            return

        await self.async_set_accessory_requested(
            actual == "on", vehicle_sync=True
        )
        self._actual_accessory_candidate = None
        self._actual_accessory_candidate_since = None

    async def _async_start_charge(
        self,
        *,
        limit: float,
        amps: float,
        allow_wake: bool,
        reason: str,
        solar_start: bool = False,
        bypass_solar_hold: bool = False,
        start_allowed: Callable[[], bool] | None = None,
    ) -> bool:
        def policy_still_allows_start() -> bool:
            return (
                self.charging_commands_allowed
                and (start_allowed is None or start_allowed())
                and (
                    not solar_start
                    or self._solar_dispatch_ready(bypass_solar_hold)
                )
            )

        def safe_to_turn_on() -> bool:
            actual_current = self.charge_current_a
            return policy_still_allows_start() and (
                actual_current is None
                or actual_current <= self.maximum_current_a
            )

        def maximum_correction_still_required() -> bool:
            actual_current = self.charge_current_a
            return (
                policy_still_allows_start()
                and actual_current is not None
                and actual_current > self.maximum_current_a
            )

        if not self.charging_commands_allowed or self.is_charging:
            return False
        if not policy_still_allows_start():
            return False

        if not self.is_awake:
            if not allow_wake:
                return False
            await self._async_wake_vehicle(
                charging=True,
                solar_start=solar_start,
                bypass_solar_hold=bypass_solar_hold,
                final_guard=policy_still_allows_start,
            )

        # Wake/refresh delays create a race window. Recheck every start gate
        # before setting charging parameters or turning on the charge switch.
        if self.is_charging:
            return False
        if not policy_still_allows_start():
            return False
        if not self.is_awake and allow_wake:
            # Still send commands after a deliberate wake attempt; Tesla Fleet may
            # lag behind the app even when the car is already awake.
            _LOGGER.debug("Vehicle status still stale after wake for %s", reason)

        safe_amps = self._clamp_charge_current(amps)
        await self._async_set_charge_limit(
            limit,
            set_when_unknown=True,
            solar_start=solar_start,
            bypass_solar_hold=bypass_solar_hold,
            final_guard=policy_still_allows_start,
        )

        actual_current = self.charge_current_a
        if actual_current is not None and actual_current > self.maximum_current_a:
            await self._async_set_charge_current(
                self.maximum_current_a,
                solar_start=solar_start,
                require_awake=False,
                bypass_solar_hold=bypass_solar_hold,
                final_guard=maximum_correction_still_required,
            )
            return False

        await self._async_set_charge_current(
            safe_amps,
            solar_start=solar_start,
            require_awake=False,
            bypass_solar_hold=bypass_solar_hold,
            final_guard=policy_still_allows_start,
        )
        if not safe_to_turn_on():
            return False
        self._pending_charge_start_guard = safe_to_turn_on
        started = await self._async_vehicle_command(
            command_key="charge_state",
            desired="on",
            domain="switch",
            service="turn_on",
            data={"entity_id": self.data[CONF_CHARGE_SWITCH]},
            requires_cable=True,
            requires_solar_start=solar_start,
            bypass_solar_hold=bypass_solar_hold,
            final_guard=safe_to_turn_on,
        )
        if not self._charge_start_pending():
            self._pending_charge_start_guard = None
        if started:
            self.status = f"Starting charge · {reason}"
        return started

    async def _async_stop_charge_if_active(
        self,
        *,
        allow_pending_start: bool = False,
        final_guard: Callable[[], bool] | None = None,
    ) -> bool:
        """Stop a positively active charge without waking or requiring cable data."""
        if not self.is_charging and not (
            allow_pending_start
            and self._pending_command_opposes("charge_state", "off")
        ):
            return False
        return await self._async_vehicle_command(
            command_key="charge_state",
            desired="off",
            domain="switch",
            service="turn_off",
            data={"entity_id": self.data[CONF_CHARGE_SWITCH]},
            final_guard=final_guard,
        )

    async def _async_ensure_charge_limit(
        self,
        limit: float,
        *,
        final_guard: Callable[[], bool] | None = None,
    ) -> bool:
        return await self._async_set_charge_limit(
            limit,
            set_when_unknown=False,
            final_guard=final_guard,
        )

    async def _async_set_charge_limit(
        self,
        limit: float,
        *,
        set_when_unknown: bool,
        solar_start: bool = False,
        bypass_solar_hold: bool = False,
        final_guard: Callable[[], bool] | None = None,
    ) -> bool:
        if not self.charging_commands_allowed:
            return False
        safe_limit = min(max(float(limit), 0.0), 100.0)
        current = self._float_state(self.data.get(CONF_CHARGE_LIMIT))
        if current is None and not set_when_unknown:
            return False
        return await self._async_vehicle_command(
            command_key="charge_limit",
            desired=safe_limit,
            domain="number",
            service="set_value",
            data={
                "entity_id": self.data[CONF_CHARGE_LIMIT],
                "value": safe_limit,
            },
            requires_cable=True,
            requires_solar_start=solar_start,
            bypass_solar_hold=bypass_solar_hold,
            final_guard=final_guard,
        )

    async def _async_set_charge_current(
        self,
        amps: float,
        *,
        solar_start: bool = False,
        require_awake: bool = True,
        bypass_solar_hold: bool = False,
        final_guard: Callable[[], bool] | None = None,
    ) -> bool:
        if (require_awake and not self.is_awake) or not self.charging_commands_allowed:
            return False
        safe_amps = self._clamp_charge_current(amps)
        sent = await self._async_vehicle_command(
            command_key="charge_current",
            desired=safe_amps,
            domain="number",
            service="set_value",
            data={
                "entity_id": self.data[CONF_CHARGE_CURRENT],
                "value": safe_amps,
            },
            requires_cable=True,
            requires_solar_start=solar_start,
            bypass_solar_hold=bypass_solar_hold,
            final_guard=final_guard,
        )
        return sent

    async def _async_wake_vehicle(
        self,
        *,
        charging: bool,
        solar_start: bool = False,
        bypass_solar_hold: bool = False,
        final_guard: Callable[[], bool] | None = None,
    ) -> bool:
        if self.is_awake:
            return True
        sent = await self._async_vehicle_command(
            command_key="wake",
            desired="on",
            domain="button",
            service="press",
            data={"entity_id": self.data[CONF_WAKE_BUTTON]},
            requires_cable=charging,
            requires_solar_start=solar_start,
            bypass_solar_hold=bypass_solar_hold,
            final_guard=final_guard,
        )
        if not sent:
            return (
                self.controller_enabled
                and (not charging or self.charge_cable_connected is True)
                and (final_guard is None or final_guard())
            )
        await asyncio.sleep(10)
        await self._async_vehicle_command(
            command_key=None,
            desired=None,
            domain="homeassistant",
            service="update_entity",
            data={"entity_id": self.data[CONF_VEHICLE_STATUS]},
            requires_cable=charging,
            requires_solar_start=solar_start,
            bypass_solar_hold=bypass_solar_hold,
            final_guard=final_guard,
        )
        for _ in range(4):
            if self.is_awake:
                return True
            if not self.controller_enabled:
                return False
            if charging and self.charge_cable_connected is not True:
                return False
            if final_guard is not None and not final_guard():
                return False
            await asyncio.sleep(5)
        _LOGGER.debug("Tesla wake timeout; continuing with command")
        return (
            self.controller_enabled
            and (not charging or self.charge_cable_connected is True)
            and (final_guard is None or final_guard())
        )

    @staticmethod
    def _command_values_match(
        left: str | float, right: str | float, tolerance: float = 0.01
    ) -> bool:
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return abs(float(left) - float(right)) < tolerance
        return left == right

    def _pending_command_opposes(
        self, command_key: str, desired: str | float
    ) -> bool:
        pending = self._pending_vehicle_commands.get(command_key)
        return pending is not None and not self._command_values_match(
            pending.desired, desired
        )

    def _command_should_dispatch(
        self, command_key: str, desired: str | float, now: datetime
    ) -> bool:
        pending = self._pending_vehicle_commands.get(command_key)
        if (
            command_key in {"accessory_power", "charge_state"}
            and pending is not None
            and not self._command_values_match(pending.desired, desired)
        ):
            # Binary user intent and charge safety reversals must supersede a
            # late opposite command. Numeric regulation keeps the full history
            # window to prevent 5/6/5/6 A chatter.
            return True

        last = self._last_command_sent_at(command_key, desired)
        if (
            last is not None
            and (now - last).total_seconds() < COMMAND_DUPLICATE_WINDOW_SECONDS
        ):
            return False

        if pending is None or not self._command_values_match(pending.desired, desired):
            return True
        if (
            now - pending.last_sent_at
        ).total_seconds() < COMMAND_DUPLICATE_WINDOW_SECONDS:
            return False
        return pending.attempts < COMMAND_MAX_ATTEMPTS

    def _last_command_sent_at(
        self, command_key: str, desired: str | float
    ) -> datetime | None:
        matching = [
            sent_at
            for (
                stored_key,
                stored_desired,
            ), sent_at in self._last_vehicle_commands.items()
            if stored_key == command_key
            and self._command_values_match(stored_desired, desired)
        ]
        return max(matching, default=None)

    def _forget_last_vehicle_command(
        self,
        command_key: str,
        desired: str | float,
        *,
        tolerance: float = 0.01,
    ) -> None:
        for stored in list(self._last_vehicle_commands):
            stored_key, stored_desired = stored
            if stored_key == command_key and self._command_values_match(
                stored_desired, desired, tolerance
            ):
                self._last_vehicle_commands.pop(stored, None)

    def _record_vehicle_command(
        self, command_key: str, desired: str | float, now: datetime
    ) -> None:
        self._forget_last_vehicle_command(command_key, desired)
        self._last_vehicle_commands[(command_key, desired)] = now
        confirmed = self._confirmed_vehicle_commands.get(command_key)
        if confirmed is not None and not self._command_values_match(confirmed, desired):
            self._confirmed_vehicle_commands.pop(command_key, None)
        pending = self._pending_vehicle_commands.get(command_key)
        if pending is not None and self._command_values_match(pending.desired, desired):
            pending.last_sent_at = now
            pending.attempts += 1
            return

        needs_divergence = (
            pending is not None
            and not self._command_values_match(pending.desired, desired)
            and self._command_feedback_matches(command_key, desired)
        )
        self._pending_vehicle_commands[command_key] = PendingVehicleCommand(
            desired=desired,
            last_sent_at=now,
            confirmation_requires_divergence=needs_divergence,
        )

        if command_key == "charge_state" and desired == "off":
            self._pending_charge_start_guard = None

    def _observe_command_feedback(
        self,
        command_key: str,
        actual: str | float | None,
        *,
        tolerance: float = 0.01,
    ) -> bool:
        """Confirm pending state and reopen a command episode after divergence."""
        if actual is None:
            return False

        pending = self._pending_vehicle_commands.get(command_key)
        if pending is not None and self._command_values_match(
            actual, pending.desired, tolerance
        ):
            if (
                pending.confirmation_requires_divergence
                and not pending.divergence_observed
            ):
                return False
            self._pending_vehicle_commands.pop(command_key, None)
            self._confirmed_vehicle_commands[command_key] = pending.desired
            return True

        if pending is not None and pending.confirmation_requires_divergence:
            pending.divergence_observed = True
            if command_key == "accessory_power":
                self._accessory_command_needed = (
                    pending.attempts < COMMAND_MAX_ATTEMPTS
                )

        confirmed = self._confirmed_vehicle_commands.get(command_key)
        if confirmed is not None and not self._command_values_match(
            actual, confirmed, tolerance
        ):
            self._confirmed_vehicle_commands.pop(command_key, None)
            self._forget_last_vehicle_command(
                command_key, confirmed, tolerance=tolerance
            )
        return False

    def _confirm_pending_vehicle_commands(self) -> None:
        """Clear pending commands once normal Fleet entities confirm them."""
        self._observe_command_feedback(
            "charge_current", self.charge_current_a, tolerance=0.1
        )
        self._observe_command_feedback(
            "charge_limit",
            self._float_state(self.data.get(CONF_CHARGE_LIMIT)),
            tolerance=0.5,
        )
        charge_state = self._state(self.data.get(CONF_CHARGE_SWITCH))
        charge_confirmed = self._observe_command_feedback(
            "charge_state",
            charge_state if charge_state in ("on", "off") else None,
        )
        if charge_confirmed and charge_state == "on":
            self._pending_charge_start_guard = None

        accessory_state = self._state(self.data.get(CONF_ACCESSORY_SWITCH))
        accessory_confirmed = self._observe_command_feedback(
            "accessory_power",
            accessory_state if accessory_state in ("on", "off") else None,
        )

        # Any positive response from an actuation command proves that the wake
        # was effective even if the vehicle-status entity itself lags.
        if self.is_awake or self.is_charging or accessory_confirmed:
            pending_wake = self._pending_vehicle_commands.pop("wake", None)
            if pending_wake is not None:
                self._confirmed_vehicle_commands["wake"] = "on"
        else:
            vehicle_state = self._state(self.data.get(CONF_VEHICLE_STATUS))
            self._observe_command_feedback(
                "wake", vehicle_state if vehicle_state in ("on", "off") else None
            )

    def _command_already_reflected(
        self, command_key: str, desired: str | float
    ) -> bool:
        """Check live feedback under the dispatch lock before sending."""
        if self._pending_command_opposes(command_key, desired):
            return False
        return self._command_feedback_matches(command_key, desired)

    def _command_feedback_matches(
        self, command_key: str, desired: str | float
    ) -> bool:
        """Compare a desired command with its current source entity."""
        if command_key == "charge_current":
            actual = self.charge_current_a
            return actual is not None and self._command_values_match(
                actual, desired, 0.1
            )
        if command_key == "charge_limit":
            actual = self._float_state(self.data.get(CONF_CHARGE_LIMIT))
            return actual is not None and self._command_values_match(
                actual, desired, 0.5
            )
        if command_key == "charge_state":
            return self._state(self.data.get(CONF_CHARGE_SWITCH)) == desired
        if command_key == "accessory_power":
            return self._state(self.data.get(CONF_ACCESSORY_SWITCH)) == desired
        if command_key == "wake":
            return desired == "on" and self.is_awake
        return False

    async def _async_vehicle_command(
        self,
        *,
        command_key: str | None,
        desired: str | float | None,
        domain: str,
        service: str,
        data: dict[str, Any],
        requires_cable: bool = False,
        requires_solar_start: bool = False,
        bypass_solar_hold: bool = False,
        final_guard: Callable[[], bool] | None = None,
    ) -> bool:
        """Apply final master/safety/dedup gates and dispatch one vehicle call."""
        async with self._command_lock:
            # These checks deliberately live immediately beside async_call. They
            # protect against missed caller guards and state changes during wake.
            task = asyncio.current_task()
            task_epoch = self._task_command_epochs.get(task, self._command_epoch)
            if (
                not self._runtime_active
                or not self.controller_enabled
                or task_epoch != self._command_epoch
            ):
                _LOGGER.debug(
                    "Suppressing %s.%s while controller is disabled", domain, service
                )
                return False
            if requires_cable and self.charge_cable_connected is not True:
                _LOGGER.debug(
                    "Suppressing %s.%s without positive cable state", domain, service
                )
                return False
            if requires_solar_start and not self._solar_dispatch_ready(
                bypass_solar_hold
            ):
                _LOGGER.debug(
                    "Suppressing %s.%s without both solar start gates", domain, service
                )
                return False
            if final_guard is not None and not final_guard():
                _LOGGER.debug(
                    "Suppressing %s.%s after its live condition ended", domain, service
                )
                return False

            now = dt_util.utcnow()
            self._confirm_pending_vehicle_commands()
            if (
                command_key is not None
                and desired is not None
                and self._command_already_reflected(command_key, desired)
            ):
                return False
            if (
                command_key is not None
                and desired is not None
                and not self._command_should_dispatch(command_key, desired, now)
            ):
                return False

            if command_key is not None and desired is not None:
                # Record the dispatch attempt before yielding. Even a service
                # exception may occur after Tesla accepted a command, so an
                # immediate evaluation must not create command chatter.
                self._record_vehicle_command(command_key, desired, now)
            await self.hass.services.async_call(domain, service, data, blocking=True)
            return True

    async def _async_handle_notifications(self) -> None:
        service_ref = str(self.options.get(OPT_NOTIFY_SERVICE, "")).strip()
        if not service_ref:
            return

        now = dt_util.utcnow()
        power = self.live_charging_power_w
        if power > 300:
            self._charging_stop_candidate = None
            if not self._charging_notified:
                self._charging_start_candidate = self._charging_start_candidate or now
                if (now - self._charging_start_candidate).total_seconds() >= 20:
                    await self._async_notify(
                        "⚡ Tesla charging started",
                        self._notification_message(started=True),
                    )
                    self._charging_notified = True
                    self._charging_start_candidate = None
        elif power < 100:
            self._charging_start_candidate = None
            if self._charging_notified:
                self._charging_stop_candidate = self._charging_stop_candidate or now
                if (now - self._charging_stop_candidate).total_seconds() >= 30:
                    await self._async_notify(
                        "🔌 Tesla charging stopped",
                        self._notification_message(started=False),
                    )
                    self._charging_notified = False
                    self._charging_stop_candidate = None

    async def _async_notify(self, title: str, message: str) -> None:
        service_ref = str(self.options.get(OPT_NOTIFY_SERVICE, "")).strip()
        if not service_ref:
            return
        if "." in service_ref:
            domain, service = service_ref.split(".", 1)
        else:
            domain, service = "notify", service_ref
        if domain != "notify":
            _LOGGER.warning(
                "Notify service must be notify.<service>, got %s", service_ref
            )
            return
        if not self.hass.services.has_service(domain, service):
            _LOGGER.warning("Notify service %s is not available", service_ref)
            return
        await self.hass.services.async_call(
            domain,
            service,
            {"title": title, "message": message},
            blocking=False,
        )

    def _notification_message(self, *, started: bool) -> str:
        battery = (
            f"{self.last_battery_pct:.0f}%"
            if self.last_battery_pct is not None
            else "unknown"
        )
        if started:
            amps = self.charge_current_a or 0
            return (
                f"Battery {battery} · {self.mode} · {amps:.0f} A · "
                f"{self.live_charging_power_w / 1000:.2f} kW"
            )
        return f"Battery {battery} · Mode: {self.mode}"

    @property
    def display_status(self) -> str:
        if not self.controller_enabled:
            return "Controller disabled · No vehicle commands"

        actual = self._state(self.data.get(CONF_ACCESSORY_SWITCH))
        if self.requested_accessory and actual == "on":
            accessory = "Accessory ON"
        elif self.requested_accessory:
            accessory = "Accessory ON pending"
        elif not self.requested_accessory and actual == "on":
            accessory = "Accessory OFF pending"
        else:
            accessory = "Accessory OFF"
        parts = [self.status, accessory]
        if (
            self.sleep_status.lower() not in self.status.lower()
            and "waiting for vehicle to be plugged in" not in self.status.lower()
        ):
            parts.append(self.sleep_status)
        return " · ".join(parts)

    def _update_status(self) -> None:
        battery = self.last_battery_pct
        amps = self.charge_current_a

        if not self.controller_enabled:
            self.status = "Controller disabled · No vehicle commands"
            return

        if self.mode == MODE_ASAP:
            if battery is not None and battery >= 100:
                self.status = "ASAP target reached"
            elif self.is_charging:
                self.status = (
                    f"ASAP · Charging at {amps:.0f} A"
                    if amps is not None
                    else "ASAP · Charging"
                )
            elif self.charge_cable_connected is not True:
                self.status = (
                    "ASAP · Waiting for vehicle to be plugged in"
                    if self.charge_cable_connected is False
                    else "ASAP · Waiting for valid charge-cable data"
                )
            else:
                self.status = "ASAP · Ready"
            return

        if self.maintenance_active:
            if self.is_charging:
                self.status = (
                    f"Battery maintenance · Charging to {self.target_soc:.0f}%"
                    + (f" at {amps:.0f} A" if amps is not None else "")
                )
            elif self.charge_cable_connected is not True:
                self.status = (
                    "Maintenance · Waiting for vehicle to be plugged in"
                    if self.charge_cable_connected is False
                    else "Maintenance · Waiting for valid charge-cable data"
                )
            else:
                self.status = f"Battery maintenance · Target {self.target_soc:.0f}%"
            return

        if self.mode == MODE_SOLAR_OFFPEAK and self.off_peak:
            if self.is_charging:
                self.status = (
                    f"Cheap period · Charging at {amps:.0f} A"
                    if amps is not None
                    else "Cheap period · Charging"
                )
            elif self.charge_cable_connected is not True:
                self.status = (
                    "Unplugged · Waiting for vehicle to be plugged in"
                    if self.charge_cable_connected is False
                    else "Waiting for valid charge-cable data"
                )
            elif self.sleep_status == "Sleeping":
                self.status = "Cheap period · Vehicle sleeping"
            else:
                self.status = "Cheap period · Waiting"
            return

        if self.grid_net_power_w is None and self.is_charging:
            self.status = "Grid meter unavailable · Stopping active solar charge"
            return

        if self.is_charging:
            self.status = (
                f"Solar · Charging at {amps:.0f} A"
                if amps is not None
                else "Solar · Charging"
            )
            return

        if self.charge_cable_connected is not True:
            if self.charge_cable_connected is False:
                self.status = "Unplugged · Waiting for vehicle to be plugged in"
            else:
                self.status = "Waiting for valid charge-cable data"
            return

        if (
            not self.is_charging
            and self.recent_plug_active
            and battery is not None
            and battery < float(self.options[OPT_NORMAL_TARGET_SOC])
        ):
            if self._instant_solar_start_ready():
                self.status = (
                    f"Plugged in · Solar ready · Starting at "
                    f"{self.minimum_current_a:.0f} A"
                )
            else:
                deficit = self._solar_start_deficit_w()
                if deficit is None:
                    self.status = "Plugged in · Waiting for valid solar/grid data"
                else:
                    self.status = (
                        f"Plugged in · Waiting for {ceil(deficit):.0f} W more "
                        "solar · "
                        "Will start automatically"
                    )
            return

        if self.sleep_status == "Sleeping":
            if battery is not None and battery <= self.solar_wake_threshold_soc:
                if self._solar_start_ready():
                    self.status = "Sleeping · Solar ready · Waking automatically"
                elif self._instant_solar_start_ready():
                    self.status = (
                        "Sleeping · Solar ready · Sustained hold in progress · "
                        "Will wake automatically"
                    )
                else:
                    self.status = (
                        "Sleeping · Waiting for solar · Will wake automatically "
                        "at sufficient surplus"
                    )
            elif battery is None:
                self.status = "Sleeping · Waiting for valid battery SOC"
            else:
                self.status = (
                    f"Sleeping · SOC {battery:.0f}% · Wake threshold ≤"
                    f"{self.solar_wake_threshold_soc:.0f}%"
                )
            return

        if battery is None:
            self.status = "Waiting for valid battery SOC"
            return

        if (
            battery is not None
            and battery >= float(self.options[OPT_NORMAL_TARGET_SOC])
        ):
            if self._buffer_eligible():
                self.status = f"Solar buffer available · Target {self.target_soc:.0f}%"
            else:
                self.status = "Target reached · Waiting"
            return

        if (
            battery is not None
            and battery > self.solar_wake_threshold_soc
            and battery < float(self.options[OPT_NORMAL_TARGET_SOC])
        ):
            self.status = (
                f"Waiting · SOC hysteresis · restart at ≤"
                f"{self.solar_wake_threshold_soc:.0f}%"
            )
        elif self._solar_start_ready():
            self.status = "Solar ready · Starting charge"
        elif self._instant_solar_start_ready():
            self.status = (
                "Solar ready · Sustained start hold in progress · "
                "Will start automatically"
            )
        else:
            deficit = self._solar_start_deficit_w()
            if deficit is None:
                self.status = "Waiting for valid solar/grid data"
            else:
                self.status = (
                    f"Waiting for {ceil(deficit):.0f} W more solar · "
                    "Will start automatically"
                )

    async def _async_save(self) -> None:
        # Serialize Store writes and build the payload only after acquiring the
        # lock, so an older cache-save task cannot overwrite a newer Enabled or
        # user-intent value after a rapid state change.
        async with self._save_lock:
            await self._store.async_save(
                {
                    "controller_enabled": self.controller_enabled,
                    "mode": self.mode,
                    "requested_accessory": self.requested_accessory,
                    "accessory_command_needed": self._accessory_command_needed,
                    "maintenance_active": self.maintenance_active,
                    "last_battery_pct": self.last_battery_pct,
                    "last_charge_current_a": self.last_charge_current_a,
                    "last_charge_limit_pct": self.last_charge_limit_pct,
                }
            )
