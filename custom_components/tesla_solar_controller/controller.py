"""Controller/state machine for Tesla Solar Controller."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ACCESSORY_SYNC_DELAY_SECONDS,
    CONF_ACCESSORY_SWITCH,
    CONF_BATTERY_LEVEL,
    CONF_CHARGE_CURRENT,
    CONF_CHARGE_LIMIT,
    CONF_CHARGE_SWITCH,
    CONF_FORECAST_TOMORROW,
    CONF_GRID_NET_POWER,
    CONF_L2_VOLTAGE,
    CONF_SOLAR_POWER,
    CONF_VEHICLE_STATUS,
    CONF_WAKE_BUTTON,
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
    OPT_MAX_CURRENT_A,
    OPT_MIN_CURRENT_A,
    OPT_NORMAL_TARGET_SOC,
    OPT_NOTIFY_SERVICE,
    OPT_OFFPEAK_CURRENT_A,
    OPT_POOR_FORECAST_THRESHOLD_KWH,
    OPT_SOLAR_START_EXPORT_W,
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
        self._accessory_task: asyncio.Task | None = None
        self._evaluate_task: asyncio.Task | None = None

        self.mode = MODE_SOLAR_ONLY
        self.requested_accessory = False
        self.maintenance_active = False
        self.last_battery_pct: float | None = None
        self.last_charge_current_a: float | None = None
        self.last_charge_limit_pct: float | None = None
        self.status = "Initializing"
        self.target_soc = float(DEFAULT_OPTIONS[OPT_NORMAL_TARGET_SOC])

        self._solar_export_since: datetime | None = None
        self._solar_import_since: datetime | None = None
        self._actual_accessory_candidate: str | None = None
        self._actual_accessory_candidate_since: datetime | None = None
        self._asap_wake_attempted = False
        self._last_current_adjustment: datetime | None = None
        self._last_charge_start_attempt: datetime | None = None
        self._last_charge_stop_attempt: datetime | None = None

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
        if self.data.get(CONF_FORECAST_TOMORROW):
            keys.append(CONF_FORECAST_TOMORROW)
        return [self.data[key] for key in keys if self.data.get(key)]

    async def async_initialize(self) -> None:
        """Restore persistent controller state before entities are created."""
        saved = await self._store.async_load() or {}
        mode = saved.get("mode")
        if mode in MODES:
            self.mode = mode
        self.requested_accessory = bool(saved.get("requested_accessory", False))
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
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        for task in (self._accessory_task, self._evaluate_task):
            if task and not task.done():
                task.cancel()
        await self._async_save()

    async def async_options_updated(self, options: dict[str, Any]) -> None:
        """Apply options without reloading the integration."""
        self.options = dict(DEFAULT_OPTIONS)
        self.options.update(options)
        self._update_status()
        self._notify()
        self._schedule_evaluate("options_updated")

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
        self.mode = mode
        if mode != MODE_ASAP:
            self._asap_wake_attempted = False
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
        self.requested_accessory = bool(value)
        if not value:
            self.maintenance_active = False
        await self._async_save()
        self._actual_accessory_candidate = None
        self._actual_accessory_candidate_since = None
        self._update_status()
        self._notify()

        if restored or vehicle_sync:
            self._schedule_evaluate("accessory_state_restored")
            return

        if self._accessory_task and not self._accessory_task.done():
            self._accessory_task.cancel()
        self._accessory_task = self.hass.async_create_task(
            self._async_command_accessory(value),
            "tesla_solar_controller_accessory_command",
        )

    @callback
    def _async_source_state_changed(self, event: Event) -> None:
        self._refresh_cached_battery()
        self._schedule_evaluate("source_state_changed")

    @callback
    def _async_interval(self, now: datetime) -> None:
        self._schedule_evaluate("interval")

    @callback
    def _schedule_evaluate(self, reason: str) -> None:
        if self._evaluate_task and not self._evaluate_task.done():
            return
        self._evaluate_task = self.hass.async_create_task(
            self.async_evaluate(reason), "tesla_solar_controller_evaluate"
        )

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
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _refresh_cached_values(self) -> None:
        changed = False
        battery = self._float_state(self.data.get(CONF_BATTERY_LEVEL))
        if battery is not None and 0 <= battery <= 100 and self.last_battery_pct != battery:
            self.last_battery_pct = battery
            changed = True

        current = self._float_state(self.data.get(CONF_CHARGE_CURRENT))
        if current is not None and current >= 0 and self.last_charge_current_a != current:
            self.last_charge_current_a = current
            changed = True

        limit = self._float_state(self.data.get(CONF_CHARGE_LIMIT))
        if limit is not None and 0 <= limit <= 100 and self.last_charge_limit_pct != limit:
            self.last_charge_limit_pct = limit
            changed = True

        if changed:
            self.hass.async_create_task(self._async_save())

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
        if grid is None:
            return 0.0
        return max(0.0, self.live_charging_power_w - grid)

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
        required = float(self.options[OPT_MIN_CURRENT_A]) * self.l2_voltage_v + 150.0
        return self.solar_surplus_available_w >= required

    def _desired_target_soc(self) -> float:
        if self.mode == MODE_ASAP:
            return 100.0
        if self._buffer_eligible():
            return float(self.options[OPT_BUFFER_TARGET_SOC])
        return float(self.options[OPT_NORMAL_TARGET_SOC])

    async def async_evaluate(self, reason: str) -> None:
        """Evaluate control logic using cached/local HA state only."""
        try:
            self._refresh_cached_values()
            await self._async_sync_accessory_from_vehicle()
            self._update_solar_timers()
            self.target_soc = self._desired_target_soc()

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

            if self._command_lock.locked():
                self._update_status()
                self._notify()
                return

            # Keep the Tesla's actual charge limit aligned with the controller's
            # current target whenever the vehicle is already awake. This never
            # wakes a sleeping vehicle merely to change the limit.
            if self.is_awake:
                await self._async_ensure_charge_limit(self.target_soc)

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
        if grid is None:
            self._solar_export_since = None
            self._solar_import_since = None
            return

        if grid <= -float(self.options[OPT_SOLAR_START_EXPORT_W]):
            self._solar_export_since = self._solar_export_since or now
        else:
            self._solar_export_since = None

        if grid >= float(self.options[OPT_SOLAR_STOP_IMPORT_W]):
            self._solar_import_since = self._solar_import_since or now
        else:
            self._solar_import_since = None

    def _solar_start_ready(self) -> bool:
        if self._solar_export_since is None:
            return False
        elapsed = (dt_util.utcnow() - self._solar_export_since).total_seconds()
        return elapsed >= float(self.options[OPT_SOLAR_START_MINUTES]) * 60

    def _solar_stop_ready(self) -> bool:
        if self._solar_import_since is None:
            return False
        elapsed = (dt_util.utcnow() - self._solar_import_since).total_seconds()
        return elapsed >= float(self.options[OPT_SOLAR_STOP_MINUTES]) * 60

    async def _async_handle_asap(self, battery: float | None) -> None:
        if battery is not None and battery >= 100:
            self.status = "ASAP target reached"
            return
        if self.is_charging and (self.charge_current_a or 0) >= float(
            self.options[OPT_MAX_CURRENT_A]
        ):
            self.status = f"ASAP · Charging at {self.charge_current_a:.0f} A"
            return
        if not self._asap_wake_attempted:
            self._asap_wake_attempted = True
            await self._async_start_charge(
                limit=100.0,
                amps=float(self.options[OPT_MAX_CURRENT_A]),
                allow_wake=True,
                reason="ASAP",
            )

    async def _async_handle_maintenance(
        self, battery: float | None, normal_target: float
    ) -> None:
        if battery is not None and battery >= self.target_soc:
            self.maintenance_active = False
            await self._async_save()
            await self._async_stop_charge_if_awake()
            return

        # From the normal target upward, only continue the solar buffer while
        # surplus remains genuinely available. Never buy grid energy to reach 83%.
        if battery is not None and battery >= normal_target:
            if not self._buffer_eligible():
                self.maintenance_active = False
                await self._async_save()
                await self._async_stop_charge_if_awake()
                return

        if not self.is_charging:
            await self._async_start_charge(
                limit=self.target_soc,
                amps=float(self.options[OPT_MIN_CURRENT_A]),
                allow_wake=True,
                reason="battery maintenance",
            )
            return

        await self._async_regulate_current(
            minimum=float(self.options[OPT_MIN_CURRENT_A]),
            allow_stop=False,
        )

    async def _async_handle_offpeak(
        self, battery: float | None, normal_target: float
    ) -> None:
        buffer_target = float(self.options[OPT_BUFFER_TARGET_SOC])

        # Once the normal target is reached, an optional 80->83% extension is
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
                        amps=float(self.options[OPT_MIN_CURRENT_A]),
                        allow_wake=False,
                        reason="poor-forecast solar buffer",
                    )
                else:
                    await self._async_regulate_current(
                        minimum=float(self.options[OPT_MIN_CURRENT_A]),
                        allow_stop=True,
                    )
                return
            await self._async_stop_charge_if_awake()
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
            )
            return

        await self._async_regulate_current(
            minimum=float(self.options[OPT_OFFPEAK_CURRENT_A]),
            allow_stop=False,
        )

    async def _async_handle_solar_only(
        self, battery: float | None, normal_target: float
    ) -> None:
        # Hard sleep-protection rule: solar surplus alone never wakes the Tesla.
        if not self.is_awake:
            return

        buffer_target = float(self.options[OPT_BUFFER_TARGET_SOC])

        # Optional poor-forecast buffer from 80 to 83: only while already awake,
        # only with accessory use enabled, and only with enough real surplus.
        if battery is not None and battery >= normal_target:
            if battery < buffer_target and self._buffer_eligible():
                if not self.is_charging:
                    await self._async_start_charge(
                        limit=buffer_target,
                        amps=float(self.options[OPT_MIN_CURRENT_A]),
                        allow_wake=False,
                        reason="poor-forecast solar buffer",
                    )
                else:
                    await self._async_regulate_current(
                        minimum=float(self.options[OPT_MIN_CURRENT_A]),
                        allow_stop=True,
                    )
                return
            await self._async_stop_charge_if_awake()
            return

        if self.is_charging:
            await self._async_regulate_current(
                minimum=float(self.options[OPT_MIN_CURRENT_A]),
                allow_stop=True,
            )
            return

        if self._solar_start_ready():
            await self._async_start_charge(
                limit=self.target_soc,
                amps=float(self.options[OPT_MIN_CURRENT_A]),
                allow_wake=False,
                reason="solar surplus",
            )

    async def _async_regulate_current(self, minimum: float, allow_stop: bool) -> None:
        if not self.is_awake:
            return
        grid = self.grid_net_power_w
        amps = self.charge_current_a
        if grid is None or amps is None:
            return

        now = dt_util.utcnow()
        if (
            self._last_current_adjustment is not None
            and (now - self._last_current_adjustment).total_seconds() < 25
        ):
            return

        max_amps = float(self.options[OPT_MAX_CURRENT_A])
        if grid < -float(self.options[OPT_SOLAR_STEP_UP_EXPORT_W]) and amps < max_amps:
            await self._async_set_charge_current(min(amps + 1, max_amps))
            return

        if grid > float(self.options[OPT_GRID_STEP_DOWN_IMPORT_W]) and amps > minimum:
            await self._async_set_charge_current(max(amps - 1, minimum))
            return

        if allow_stop and amps <= minimum and self._solar_stop_ready():
            await self._async_stop_charge_if_awake()

    async def _async_command_accessory(self, turn_on: bool) -> None:
        try:
            async with self._command_lock:
                if not self.is_awake:
                    await self._async_wake_vehicle()

                if turn_on:
                    await self._call("switch", "turn_on", self.data[CONF_ACCESSORY_SWITCH])
                else:
                    await self._call("switch", "turn_off", self.data[CONF_ACCESSORY_SWITCH])

                await asyncio.sleep(12)
                await self._async_refresh_vehicle_entities()
                await asyncio.sleep(20)
                await self._async_refresh_vehicle_entities()

                actual = self._state(self.data[CONF_ACCESSORY_SWITCH])
                expected = "on" if turn_on else "off"
                if actual != expected:
                    await self._async_wake_vehicle()
                    if turn_on:
                        await self._call(
                            "switch", "turn_on", self.data[CONF_ACCESSORY_SWITCH]
                        )
                    else:
                        await self._call(
                            "switch", "turn_off", self.data[CONF_ACCESSORY_SWITCH]
                        )
                    await asyncio.sleep(15)
                    await self._async_refresh_vehicle_entities()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Accessory power command failed")
        finally:
            self._actual_accessory_candidate = None
            self._actual_accessory_candidate_since = None
            self._schedule_evaluate("accessory_command_complete")

    async def _async_sync_accessory_from_vehicle(self) -> None:
        """Mirror stable Tesla-app changes back into the custom switch."""
        actual = self._state(self.data.get(CONF_ACCESSORY_SWITCH))
        if actual not in ("on", "off"):
            self._actual_accessory_candidate = None
            self._actual_accessory_candidate_since = None
            return

        desired = "on" if self.requested_accessory else "off"
        if actual == desired:
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
        self, *, limit: float, amps: float, allow_wake: bool, reason: str
    ) -> None:
        now = dt_util.utcnow()
        if (
            self._last_charge_start_attempt is not None
            and (now - self._last_charge_start_attempt).total_seconds() < 60
        ):
            return
        self._last_charge_start_attempt = now

        if not self.is_awake:
            if not allow_wake:
                return
            await self._async_wake_vehicle()
        if not self.is_awake and allow_wake:
            # Still send commands after a deliberate wake attempt; Tesla Fleet may
            # lag behind the app even when the car is already awake.
            _LOGGER.debug("Vehicle status still stale after wake for %s", reason)

        async with self._command_lock:
            await self._call_number(self.data[CONF_CHARGE_LIMIT], limit)
            await self._call_number(self.data[CONF_CHARGE_CURRENT], amps)
            self._last_current_adjustment = dt_util.utcnow()
            await self._call("switch", "turn_on", self.data[CONF_CHARGE_SWITCH])
        self.status = f"Starting charge · {reason}"

    async def _async_stop_charge_if_awake(self) -> None:
        if not self.is_awake or not self.is_charging:
            return
        now = dt_util.utcnow()
        if (
            self._last_charge_stop_attempt is not None
            and (now - self._last_charge_stop_attempt).total_seconds() < 30
        ):
            return
        self._last_charge_stop_attempt = now
        async with self._command_lock:
            await self._call("switch", "turn_off", self.data[CONF_CHARGE_SWITCH])

    async def _async_ensure_charge_limit(self, limit: float) -> None:
        current = self._float_state(self.data.get(CONF_CHARGE_LIMIT))
        if current is None or abs(current - limit) < 0.5:
            return
        async with self._command_lock:
            await self._call_number(self.data[CONF_CHARGE_LIMIT], limit)

    async def _async_set_charge_current(self, amps: float) -> None:
        if not self.is_awake:
            return
        async with self._command_lock:
            await self._call_number(self.data[CONF_CHARGE_CURRENT], amps)
            self._last_current_adjustment = dt_util.utcnow()

    async def _async_wake_vehicle(self) -> None:
        await self._call("button", "press", self.data[CONF_WAKE_BUTTON])
        await asyncio.sleep(10)
        await self._call(
            "homeassistant", "update_entity", self.data[CONF_VEHICLE_STATUS]
        )
        for _ in range(4):
            if self.is_awake:
                return
            await asyncio.sleep(5)
        _LOGGER.debug("Tesla wake timeout; continuing with command")

    async def _async_refresh_vehicle_entities(self) -> None:
        entities = [
            self.data[CONF_VEHICLE_STATUS],
            self.data[CONF_ACCESSORY_SWITCH],
            self.data[CONF_BATTERY_LEVEL],
            self.data[CONF_CHARGE_CURRENT],
            self.data[CONF_CHARGE_LIMIT],
            self.data[CONF_CHARGE_SWITCH],
        ]
        await self.hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": entities},
            blocking=True,
        )
        self._refresh_cached_values()

    async def _call(self, domain: str, service: str, entity_id: str) -> None:
        await self.hass.services.async_call(
            domain, service, {"entity_id": entity_id}, blocking=True
        )

    async def _call_number(self, entity_id: str, value: float) -> None:
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

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
            _LOGGER.warning("Notify service must be notify.<service>, got %s", service_ref)
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
            f"{self.last_battery_pct:.0f}%" if self.last_battery_pct is not None else "unknown"
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
        actual = self._state(self.data.get(CONF_ACCESSORY_SWITCH))
        if self.requested_accessory and actual == "on":
            accessory = "Accessory ON"
        elif self.requested_accessory:
            accessory = "Accessory ON pending"
        elif not self.requested_accessory and actual == "on":
            accessory = "Accessory OFF pending"
        else:
            accessory = "Accessory OFF"
        return f"{self.status} · {accessory} · {self.sleep_status}"

    def _update_status(self) -> None:
        battery = self.last_battery_pct
        amps = self.charge_current_a
        if self.maintenance_active:
            if self.is_charging:
                self.status = (
                    f"Battery maintenance · Charging to {self.target_soc:.0f}%"
                    + (f" at {amps:.0f} A" if amps is not None else "")
                )
            else:
                self.status = f"Battery maintenance · Target {self.target_soc:.0f}%"
            return

        if self.mode == MODE_ASAP:
            self.status = (
                f"ASAP · Charging at {amps:.0f} A"
                if self.is_charging and amps is not None
                else "ASAP · Ready"
            )
            return

        if self.mode == MODE_SOLAR_OFFPEAK and self.off_peak:
            if self.is_charging:
                self.status = (
                    f"Cheap period · Charging at {amps:.0f} A"
                    if amps is not None
                    else "Cheap period · Charging"
                )
            elif self.sleep_status == "Sleeping":
                self.status = "Cheap period · Vehicle sleeping"
            else:
                self.status = "Cheap period · Waiting"
            return

        if self.is_charging:
            self.status = (
                f"Solar · Charging at {amps:.0f} A"
                if amps is not None
                else "Solar · Charging"
            )
            return

        if self.sleep_status == "Sleeping":
            self.status = "Sleeping · Solar will not wake vehicle"
            return

        if battery is not None and battery >= float(self.options[OPT_NORMAL_TARGET_SOC]):
            if self._buffer_eligible():
                self.status = f"Solar buffer available · Target {self.target_soc:.0f}%"
            else:
                self.status = "Target reached · Waiting"
            return

        if self._solar_start_ready():
            self.status = "Solar ready · Starting charge"
        elif self.solar_surplus_available_w > 0:
            required = max(
                float(self.options[OPT_SOLAR_START_EXPORT_W])
                - self.solar_surplus_available_w,
                0,
            )
            self.status = f"Solar available · Need {required:.0f} W more"
        else:
            self.status = "Waiting for solar"

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "mode": self.mode,
                "requested_accessory": self.requested_accessory,
                "maintenance_active": self.maintenance_active,
                "last_battery_pct": self.last_battery_pct,
                "last_charge_current_a": self.last_charge_current_a,
                "last_charge_limit_pct": self.last_charge_limit_pct,
            }
        )
