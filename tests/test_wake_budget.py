"""Wake reservations, Zurich calendar boundaries, and scheduled charging."""

import ast
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import test_solar_stop as fixture


class WakeBudgetTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ControllerTest.asyncSetUp
    tick = fixture.ControllerTest.tick

    def local_time(self, year=2026, month=11, day=12, hour=19, minute=0, second=0):
        self.clock.time_zone = ZoneInfo("Europe/Zurich")
        self.clock.now = datetime(
            year, month, day, hour, minute, second, tzinfo=self.clock.time_zone
        ).astimezone(timezone.utc)

    async def reload(self):
        other = fixture.controller_type(self.clock)(self.hass, self.entry)
        other._store.saved = deepcopy(self.c._store.saved)
        await other.async_initialize()
        other._active = True
        self.c = other

    async def exhaust_grid_wakes(self):
        self.c.mode = "Charge ASAP"
        self.states["vehicle_status"] = "off"
        self.fail_service = True
        with self.assertLogs(level="ERROR"):
            await self.tick()
            await self.tick(120)
        await self.tick(120)
        self.assertEqual(self.c.daily_wakes, 2)
        self.assertEqual(len(self.calls), 2)

    async def test_zurich_midnight_resets_before_utc_midnight(self):
        for month in (1, 7):
            with self.subTest(month=month):
                self.local_time(month=month, hour=23, minute=59, second=59)
                self.c.budget_day = (
                    self.clock.now.astimezone(self.clock.time_zone).date().isoformat()
                )
                self.c._roll_day()
                self.c.controller_enabled = False
                self.c.daily_commands = 91
                self.c.daily_wakes = self.c.daily_solar_sessions = 2
                await self.tick(1)
                self.assertEqual(self.c.budget_day, f"2026-{month:02d}-13")
                self.assertEqual(self.c.daily_wakes, 0)
                self.assertEqual(self.c.daily_commands, 0)
                self.assertEqual(self.c.daily_solar_sessions, 0)
                self.assertEqual(self.calls, [])

    async def test_utc_midnight_does_not_reset_same_zurich_day(self):
        self.local_time(hour=0, minute=59, second=59)
        self.c._roll_day()
        self.c.daily_wakes = 2
        self.c.controller_enabled = False
        await self.tick(1)
        self.assertEqual(self.c.daily_wakes, 2)

    async def test_reload_same_day_preserves_counters_and_retry_records(self):
        self.local_time()
        await self.exhaust_grid_wakes()
        saved = deepcopy(self.c._store.saved)
        await self.reload()
        self.assertEqual(self.c.daily_wakes, 2)
        self.assertEqual(self.c.daily_commands, 2)
        self.assertEqual(self.c._commands, saved["commands"])
        await self.tick(720)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.c.status, "Daily wake budget reached")

    async def test_restart_after_midnight_rolls_before_first_evaluation(self):
        self.local_time(hour=23, minute=59)
        self.c._roll_day()
        self.c.daily_wakes = 2
        self.c.daily_commands = 91
        self.c.daily_solar_sessions = 2
        await self.c._async_save()
        self.clock.now += timedelta(minutes=1)
        await self.reload()
        self.assertEqual(self.c.budget_day, "2026-11-13")
        self.assertEqual(self.c.daily_wakes, 0)
        self.assertEqual(self.c.daily_commands, 0)
        self.assertEqual(self.c.daily_solar_sessions, 0)
        self.assertEqual(self.c._store.saved["daily_wakes"], 0)
        self.assertEqual(self.calls, [])

    async def test_rollover_clears_obsolete_budget_status(self):
        self.local_time(hour=20)
        await self.exhaust_grid_wakes()
        self.c.status = "Daily wake budget reached"
        self.clock.now += timedelta(days=1)
        self.c._roll_day()
        self.assertNotIn("budget reached", self.c.display_status)
        self.fail_service = False
        await self.tick(grid_net_power=0)
        self.assertNotIn("budget reached", self.c.display_status)

    async def test_midnight_replenishes_exhausted_wake_retry_episode(self):
        self.local_time(hour=20)
        await self.exhaust_grid_wakes()
        self.local_time(day=13, hour=0)
        self.fail_service = False
        await self.tick()
        self.assertEqual(self.c.daily_wakes, 1)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.c.status, "Preparing asap charging")

    async def test_clock_rollback_keeps_future_budget_date(self):
        self.local_time(day=13)
        self.c._roll_day()
        self.c.daily_wakes = 2
        await self.c._async_save()
        self.local_time(day=12)
        await self.reload()
        self.assertEqual(self.c.budget_day, "2026-11-13")
        self.assertEqual(self.c.daily_wakes, 2)
        self.assertEqual(
            self.c.wake_diagnostics["next_budget_reset"], "2026-11-14T00:00:00+01:00"
        )

    async def test_weekday_1900_plug_standby_until_2200(self):
        self.local_time()
        self.c.mode = "Solar + off-peak"
        await self.tick(charge_cable="off", vehicle_status="off", grid_net_power=300)
        await self.tick(charge_cable="on")
        await self.tick(3 * 3600 - 1)
        self.assertEqual(self.calls, [])
        self.assertIn("off-peak", self.c.status)
        await self.tick(1)
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("button", "press")])
        await self.tick(vehicle_status="on")
        self.assertEqual(self.calls[-1][1], "turn_on")
        await self.tick(charge_switch="on")
        self.assertEqual(self.calls[-1][2]["value"], 8)
        self.assertEqual(self.c.target_soc, 80)

    async def test_failed_solar_wake_preserves_scheduled_wake_across_reload(self):
        self.local_time(hour=12)
        self.c.mode = "Solar + off-peak"
        self.fail_service = True
        await self.tick(vehicle_status="off")
        with self.assertLogs(level="ERROR"):
            await self.tick(1800)
        for _ in range(120):
            await self.tick(120)
        self.assertEqual(self.c.daily_wakes, 1)
        self.assertEqual(len(self.calls), 1)
        await self.reload()
        self.fail_service = False
        self.local_time(hour=22)
        await self.tick(grid_net_power=1000)
        self.assertEqual(self.c.daily_wakes, 2)
        self.assertEqual(len(self.calls), 2)
        await self.tick(vehicle_status="on")
        self.assertEqual(self.calls[-1][1], "turn_on")

    async def test_missing_p1_fallback_also_preserves_final_wake(self):
        self.local_time(hour=12)
        await self.tick(vehicle_status="off")
        await self.tick(1800)
        await self.tick(720)
        self.assertEqual(self.c.daily_wakes, 1)
        self.local_time(hour=22)
        await self.tick(grid_net_power="unavailable")
        self.assertEqual(self.c.daily_wakes, 2)
        self.assertEqual(self.c.session_kind, "offpeak")

    async def test_scheduled_failed_wakes_remain_bounded_and_counted(self):
        self.local_time(hour=22)
        self.c.mode = "Solar + off-peak"
        self.fail_service = True
        with self.assertLogs(level="ERROR"):
            await self.tick(vehicle_status="off")
            await self.tick(120)
        for _ in range(100):
            await self.tick(30)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.c.daily_commands, 2)
        self.assertEqual(self.c.daily_wakes, 2)
        self.assertEqual(self.c.status, "Daily wake budget reached")

    async def test_offpeak_cannot_bypass_disabled_or_cable_gate(self):
        self.local_time(hour=22)
        self.c.mode = "Solar + off-peak"
        for enabled, cable in ((False, "on"), (True, "off"), (True, "unavailable")):
            self.c.controller_enabled = enabled
            await self.tick(vehicle_status="off", charge_cable=cable)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.c.daily_wakes, 0)

    async def test_offpeak_rechecks_safety_after_wake_reservation(self):
        self.local_time(hour=22)
        self.c.mode = "Solar + off-peak"
        self.states["vehicle_status"] = "off"
        for gate in ("controller_enabled", "charge_cable"):
            with self.subTest(gate=gate):
                self.c.controller_enabled = True
                self.states["charge_cable"] = "on"
                previous_wakes = self.c.daily_wakes

                def change_gate():
                    # Start preparation saves before the command reservation.
                    if self.c.daily_wakes > previous_wakes:
                        if gate == "controller_enabled":
                            self.c.controller_enabled = False
                        else:
                            self.states["charge_cable"] = "off"

                self.c._store.before_save = change_gate
                await self.tick(720)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.c.daily_wakes, previous_wakes + 1)
        self.assertEqual(self.c.daily_wakes, 2)

    async def test_diagnostics_persist_reason_time_and_share_sensor_attributes(self):
        self.local_time(hour=22)
        self.c.mode = "Solar + off-peak"
        await self.tick(vehicle_status="off")
        expected_time = self.clock.now.isoformat()
        await self.reload()
        attrs = self.c.wake_diagnostics
        self.assertEqual(attrs["wakes_today"], 1)
        self.assertEqual(attrs["wake_limit"], 2)
        self.assertEqual(attrs["last_wake_reason"], "offpeak")
        self.assertEqual(attrs["last_wake_time"], expected_time)
        self.assertEqual(attrs["next_budget_reset"], "2026-11-13T00:00:00+01:00")
        self.assertEqual(attrs["budget_timezone"], "Europe/Zurich")
        # Exercise the actual entity property and diagnostics function without HA.
        sensor_tree = ast.parse((fixture.ROOT / "sensor.py").read_text())
        cls = next(
            n for n in sensor_tree.body
            if isinstance(n, ast.ClassDef) and n.name == "TeslaStatusSensor"
        )
        method = next(
            n for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == "extra_state_attributes"
        )
        method.decorator_list = []
        ns = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "sensor.py", "exec"), ns)
        sensor_attrs = ns["extra_state_attributes"](SimpleNamespace(controller=self.c))
        diag_tree = ast.parse((fixture.ROOT / "diagnostics.py").read_text())
        diag_tree.body = [
            n for n in diag_tree.body
            if not isinstance(n, ast.ImportFrom) or n.module == "__future__"
        ]
        exec(compile(diag_tree, "diagnostics.py", "exec"), ns)
        diagnostics = await ns["async_get_config_entry_diagnostics"](
            self.hass, SimpleNamespace(runtime_data=self.c, data={}, options={})
        )
        for key, value in attrs.items():
            self.assertEqual(sensor_attrs[key], value)
            self.assertEqual(diagnostics[key], value)

    async def test_next_reset_uses_local_midnight_across_dst(self):
        for month, day, expected in (
            (3, 29, "2026-03-30T00:00:00+02:00"),
            (10, 25, "2026-10-26T00:00:00+01:00"),
        ):
            with self.subTest(month=month):
                self.local_time(month=month, day=day, hour=0)
                self.c.budget_day = ""
                self.c._roll_day()
                self.assertEqual(self.c.wake_diagnostics["next_budget_reset"], expected)

    async def test_solar_success_waits_for_feedback_before_aborting_preparation(self):
        self.local_time(hour=12)
        await self.tick(vehicle_status="off")
        await self.tick(1800)
        await self.tick(719)
        self.assertTrue(self.c.starting)
        self.assertIsNone(self.c.cooldown_until)
        self.assertEqual(self.c.daily_wakes, 1)
        await self.tick(vehicle_status="on")
        self.assertEqual(self.calls[-1][1], "turn_on")
        self.assertEqual(self.c.daily_wakes, 1)

    async def test_solar_dispatch_cannot_consume_reserved_wake_after_mode_change(self):
        self.local_time(hour=12)
        self.c.mode = "Solar + off-peak"
        self.states["vehicle_status"] = "off"
        await self.c._dispatch("wake", "on")
        await self.c.async_set_mode("Solar only")
        self.clock.now += timedelta(minutes=12)
        await self.c._dispatch("wake", "on")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.c.daily_wakes, 1)
        self.assertIn("reserved for off-peak", self.c.status)

    async def test_wake_reservation_is_persisted_before_failed_service_dispatch(self):
        self.local_time(hour=22)
        self.c.mode = "Solar + off-peak"

        async def failed_call(domain, service, data, blocking):
            saved = self.c._store.saved
            self.assertEqual(saved["daily_wakes"], 1)
            self.assertEqual(saved["daily_commands"], 1)
            self.assertEqual(saved["commands"]["wake:on"]["reason"], "offpeak")
            self.calls.append((domain, service, data))
            raise RuntimeError("service failed")

        self.hass.services.async_call = failed_call
        with self.assertLogs(level="ERROR"):
            await self.tick(vehicle_status="off")
        await self.reload()
        self.assertEqual(self.c.daily_wakes, 1)
        self.assertEqual(len(self.calls), 1)

    async def test_reload_exhausted_budget_has_no_restored_error_status(self):
        self.local_time(hour=20)
        await self.exhaust_grid_wakes()
        await self.reload()
        self.assertEqual(self.c.display_status, "Initializing")
        self.assertEqual(self.c.daily_wakes, 2)
        # An awake car needs no wake allowance to charge on the schedule.
        self.c.mode = "Solar + off-peak"
        self.fail_service = False
        self.c.options["normal_target_soc"] = 85
        self.local_time(hour=22)
        await self.tick(vehicle_status="on", charge_limit=85)
        self.assertEqual(self.calls[-1][1], "turn_on")
        self.assertEqual(self.c.target_soc, 85)
        self.assertNotIn("budget reached", self.c.display_status)
