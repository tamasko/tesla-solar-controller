"""Exercise the complete controller with fake HA state, services and storage."""

import ast
import asyncio
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / "custom_components/tesla_solar_controller"
CONSTANTS = {}
exec((ROOT / "const.py").read_text(), CONSTANTS)


class FakeStore:
    def __init__(self, *args):
        self.saved = None
        self.fail = False
        self.before_save = None

    async def async_load(self):
        return deepcopy(self.saved)

    async def async_save(self, data):
        if self.before_save:
            self.before_save()
        if self.fail:
            raise RuntimeError("storage failed")
        self.saved = deepcopy(data)


def controller_type(clock):
    tree = ast.parse((ROOT / "controller.py").read_text())
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.ImportFrom)
            and (node.level or node.module.startswith("homeassistant"))
        )
    ]
    ns = dict(
        CONSTANTS,
        Store=FakeStore,
        callback=lambda f: f,
        dt_util=SimpleNamespace(utcnow=lambda: clock.now, now=lambda: clock.now),
    )
    exec(compile(tree, str(ROOT / "controller.py"), "exec"), ns)
    return ns["TeslaSolarController"]


class ControllerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = SimpleNamespace(
            now=datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        )
        self.states = dict(
            vehicle_status="on",
            charge_cable="on",
            battery_level="50",
            charge_switch="off",
            charge_current="5",
            charge_limit="80",
            grid_net_power="-2000",
            l2_voltage="230",
        )
        self.calls = []
        self.fail_service = False

        async def call(domain, service, data, blocking):
            self.calls.append((domain, service, data))
            if self.fail_service:
                raise RuntimeError("service failed")

        self.hass = SimpleNamespace(
            states=SimpleNamespace(
                get=lambda k: (
                    SimpleNamespace(state=self.states[k]) if k in self.states else None
                )
            ),
            services=SimpleNamespace(async_call=call),
            async_create_task=asyncio.create_task,
        )
        self.entry = SimpleNamespace(
            entry_id="test",
            data={k: k for k in self.states} | {"wake_button": "wake"},
            options={},
        )
        self.c = controller_type(self.clock)(self.hass, self.entry)
        await self.c.async_initialize()
        self.c._active = True

    async def tick(self, seconds=0, **states):
        self.clock.now += timedelta(seconds=seconds)
        self.states.update({k: str(v) for k, v in states.items()})
        await self.c.async_evaluate("test")

    async def solar_active(self, amps=5, grid=400):
        self.states.update(
            charge_switch="on", charge_current=str(amps), grid_net_power=str(grid)
        )
        self.c.session_kind = "solar"
        await self.tick()

    async def test_start_requires_full_thirty_minutes_and_resets_on_break(self):
        await self.tick()
        await self.tick(1799)
        self.assertEqual(self.calls, [])
        await self.tick(1, grid_net_power=0)
        await self.tick(1, grid_net_power=-2000)
        await self.tick(1799)
        self.assertEqual(self.calls, [])
        await self.tick(1)
        self.assertEqual(self.calls[-1][1], "turn_on")
        self.assertEqual(self.c.daily_solar_sessions, 1)

    async def test_plug_in_does_not_bypass_hold(self):
        await self.tick(charge_cable="off")
        await self.tick(1, charge_cable="on")
        self.assertEqual(self.calls, [])
        await self.tick(1800)
        self.assertEqual(self.calls[-1][1], "turn_on")

    async def test_stop_requires_thirty_minutes_at_five_amps(self):
        await self.solar_active()
        await self.tick(1799)
        self.assertEqual(self.calls, [])
        await self.tick(1)
        self.assertEqual(self.calls[-1][1], "turn_off")
        self.assertTrue(self.c.stopping)
        self.assertEqual(self.c.cooldown_until, self.clock.now + timedelta(hours=1))

    async def test_stop_import_break_resets_timer(self):
        await self.solar_active()
        await self.tick(1700, grid_net_power=299)
        await self.tick(1, grid_net_power=300)
        await self.tick(1799)
        self.assertEqual(self.calls, [])
        await self.tick(1)
        self.assertEqual(self.calls[-1][1], "turn_off")

    async def test_above_minimum_does_not_build_stop_hold(self):
        await self.solar_active(amps=6)
        await self.tick(1800)
        self.assertEqual(self.calls[-1][2]["value"], 5)
        self.assertFalse(any(call[1] == "turn_off" for call in self.calls))
        await self.tick(charge_current=5)
        await self.tick(1799)
        self.assertFalse(any(call[1] == "turn_off" for call in self.calls))
        await self.tick(1)
        self.assertEqual(self.calls[-1][1], "turn_off")

    async def test_multi_amp_increase_and_five_minute_pacing(self):
        await self.solar_active(grid=-2000)
        await self.tick(59)
        self.assertEqual(self.calls, [])
        await self.tick(1)
        self.assertEqual(self.calls[-1][2]["value"], 10)
        await self.tick(charge_current=10, grid_net_power=1000)
        await self.tick(299)
        self.assertEqual(len(self.calls), 1)
        await self.tick(1)
        self.assertEqual(self.calls[-1][2]["value"], 5)

    async def test_increase_and_decrease_holds(self):
        await self.solar_active(amps=8, grid=200)
        await self.tick(29)
        self.assertEqual(self.calls, [])
        await self.tick(1)
        self.assertEqual(self.calls[-1][2]["value"], 7)

    async def test_cooldown_precedes_new_qualification(self):
        self.c.cooldown_until = self.clock.now + timedelta(hours=1)
        await self.tick()
        await self.tick(3599)
        self.assertEqual(self.calls, [])
        await self.tick(1)
        await self.tick(1799)
        self.assertEqual(self.calls, [])
        await self.tick(1)
        self.assertEqual(self.calls[-1][1], "turn_on")

    async def test_two_solar_sessions_even_if_already_awake(self):
        self.c.daily_solar_sessions = 2
        self.c.budget_day = self.clock.now.date().isoformat()
        await self.tick()
        await self.tick(1800)
        self.assertEqual(self.calls, [])

    async def test_solar_hysteresis_and_configured_target(self):
        self.c.options["normal_target_soc"] = 85
        await self.tick(battery_level=83)
        await self.tick(1800)
        self.assertEqual(self.calls, [])
        await self.tick(battery_level=82, charge_limit=85)
        self.assertEqual(self.calls[-1][1], "turn_on")

    async def test_missing_p1_stops_and_waits_outside_schedule(self):
        await self.solar_active()
        await self.tick(grid_net_power="unavailable")
        self.assertEqual(self.calls[-1][1], "turn_off")
        await self.tick(charge_switch="off")
        self.assertIn("off-peak", self.c.status)

    async def test_missing_p1_falls_back_to_scheduled_charge_in_solar_only(self):
        self.clock.now = self.clock.now.replace(hour=23)
        await self.tick(grid_net_power="unavailable")
        self.assertEqual(self.c.session_kind, "offpeak")
        self.assertEqual(self.calls[-1][1], "turn_on")
        await self.tick(charge_switch="on")
        self.assertEqual(self.calls[-1][2]["value"], 8)

    async def test_offpeak_mode_ignores_p1_during_schedule(self):
        self.clock.now = self.clock.now.replace(hour=23)
        self.c.mode = "Solar + off-peak"
        await self.tick(grid_net_power=3000)
        self.assertEqual(self.calls[-1][1], "turn_on")

    async def test_end_offpeak_stops_before_solar_qualification(self):
        self.c.mode = "Solar + off-peak"
        self.c.session_kind = "offpeak"
        await self.tick(charge_switch="on")
        self.assertEqual(self.calls[-1][1], "turn_off")

    async def test_asap_sets_ten_amps_then_hundred_then_starts(self):
        self.c.mode = "Charge ASAP"
        await self.tick(grid_net_power="unavailable")
        self.assertEqual(self.calls[-1][2]["value"], 10)
        await self.tick(charge_current=10)
        self.assertEqual(self.calls[-1][2]["value"], 100)
        await self.tick(charge_limit=100)
        self.assertEqual(self.calls[-1][1], "turn_on")

    async def test_enabled_off_blocks_every_command_and_no_final_stop(self):
        self.c.controller_enabled = False
        await self.tick(charge_switch="on", charge_current=32)
        for key, value in (
            ("wake", "on"),
            ("current", 10),
            ("limit", 100),
            ("charge", "on"),
            ("charge", "off"),
        ):
            await self.c._dispatch(key, value)
        self.assertEqual(self.calls, [])

    async def test_unknown_or_off_cable_blocks_all_except_active_stop(self):
        for cable in ("off", "unavailable"):
            self.states["charge_cable"] = cable
            for key, value in (
                ("wake", "on"),
                ("current", 10),
                ("limit", 100),
                ("charge", "on"),
            ):
                await self.c._dispatch(key, value)
        self.assertEqual(self.calls, [])
        self.states["charge_switch"] = "on"
        await self.c._dispatch("charge", "off")
        self.assertEqual(self.calls[-1][1], "turn_off")

    async def test_two_wake_attempts_including_service_errors(self):
        self.states["vehicle_status"] = "off"
        self.fail_service = True
        with self.assertLogs(level="ERROR"):
            await self.c._dispatch("wake", "on")
            self.clock.now += timedelta(seconds=120)
            await self.c._dispatch("wake", "on")
        self.clock.now += timedelta(seconds=120)
        await self.c._dispatch("wake", "on")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.c.daily_wakes, 2)
        self.assertEqual(self.c.daily_commands, 2)

    async def test_budget_reserves_last_command_for_stop(self):
        self.c._roll_day()
        self.c.daily_commands = 149
        await self.c._dispatch("current", 10)
        self.assertEqual(self.calls, [])
        self.states["charge_switch"] = "on"
        await self.c._dispatch("charge", "off")
        self.assertEqual(self.c.daily_commands, 150)
        self.clock.now += timedelta(seconds=120)
        await self.c._dispatch("charge", "off")
        self.assertEqual(len(self.calls), 1)

    async def test_budget_exhaustion_stops_active_charge(self):
        self.c._roll_day()
        self.c.daily_commands = 149
        await self.solar_active()
        self.assertEqual(self.calls[-1][1], "turn_off")
        self.assertEqual(self.c.daily_commands, 150)

    async def test_duplicate_suppression_and_bounded_retry(self):
        await self.c._dispatch("current", 10)
        await self.c._dispatch("current", 10)
        self.assertEqual(len(self.calls), 1)
        self.clock.now += timedelta(seconds=120)
        await self.c._dispatch("current", 10)
        self.clock.now += timedelta(seconds=120)
        await self.c._dispatch("current", 10)
        self.assertEqual(len(self.calls), 2)

    async def test_persistence_of_allowances_and_reset_at_midnight(self):
        self.c._roll_day()
        self.c.daily_commands, self.c.daily_wakes, self.c.daily_solar_sessions = (
            91,
            2,
            2,
        )
        self.c.controller_enabled = False
        await self.c._async_save()
        other = controller_type(self.clock)(self.hass, self.entry)
        other._store.saved = self.c._store.saved
        await other.async_initialize()
        self.assertEqual(
            (other.daily_commands, other.daily_wakes, other.daily_solar_sessions),
            (91, 2, 2),
        )
        self.assertFalse(other.controller_enabled)
        self.clock.now += timedelta(days=1)
        other._roll_day()
        self.assertEqual(
            (other.daily_commands, other.daily_wakes, other.daily_solar_sessions),
            (0, 0, 0),
        )

    async def test_storage_failure_prevents_dispatch(self):
        self.c._store.fail = True
        with self.assertLogs(level="ERROR"):
            await self.c._dispatch("current", 10)
        self.assertEqual(self.calls, [])

    async def test_disable_during_reservation_blocks_dispatch(self):
        self.c._store.before_save = lambda: setattr(self.c, "controller_enabled", False)
        await self.c._dispatch("current", 10)
        self.assertEqual(self.calls, [])

    async def test_epoch_change_during_reservation_blocks_dispatch(self):
        self.c._store.before_save = lambda: setattr(self.c, "_epoch", self.c._epoch + 1)
        await self.c._dispatch("current", 10)
        self.assertEqual(self.calls, [])

    async def test_cable_loss_during_reservation_blocks_dispatch(self):
        self.c._store.before_save = lambda: self.states.update(charge_cable="off")
        await self.c._dispatch("current", 10)
        self.assertEqual(self.calls, [])

    async def test_stop_retries_without_waiting_another_thirty_minutes(self):
        await self.solar_active()
        await self.tick(1800)
        await self.tick(120)
        self.assertEqual([c[1] for c in self.calls], ["turn_off", "turn_off"])

    async def test_surplus_uses_only_p1_and_invalid_fails_zero(self):
        self.assertEqual(self.c.solar_surplus_available_w, 2000)
        self.states.update(charge_switch="on", charge_current="10")
        self.assertEqual(self.c.solar_surplus_available_w, 2000)
        for invalid in ("unavailable", "unknown", "nan", "inf"):
            self.states["grid_net_power"] = invalid
            self.assertEqual(self.c.solar_surplus_available_w, 0)

    async def test_hard_current_clamp(self):
        await self.c._dispatch("current", 32)
        self.assertEqual(self.calls[-1][2]["value"], 10)
        self.states["charge_current"] = "10"
        await self.c._dispatch("current", 1)
        self.assertEqual(self.calls[-1][2]["value"], 5)

    async def test_unconfirmed_start_can_be_cancelled_while_charge_still_reads_off(
        self,
    ):
        await self.c._dispatch("charge", "on")
        self.assertTrue(self.c._unconfirmed_start())
        await self.tick(grid_net_power="unavailable")
        self.assertEqual([call[1] for call in self.calls], ["turn_on", "turn_off"])
        self.assertFalse(self.c._unconfirmed_start())

    async def test_daily_rollover_replenishes_wake_retries(self):
        self.states["vehicle_status"] = "off"
        await self.c._dispatch("wake", "on")
        self.clock.now += timedelta(seconds=720)
        await self.c._dispatch("wake", "on")
        self.clock.now += timedelta(days=1)
        await self.c._dispatch("wake", "on")
        self.assertEqual(self.c.daily_wakes, 1)
        self.assertEqual(len(self.calls), 3)

    async def test_sleep_during_reservation_blocks_current_command(self):
        self.c._store.before_save = lambda: self.states.update(vehicle_status="off")
        await self.c._dispatch("current", 10)
        self.assertEqual(self.calls, [])

    async def test_wake_waits_for_awake_feedback_without_forced_refresh(self):
        self.c.mode = "Charge ASAP"
        await self.tick(vehicle_status="off")
        await self.tick(30)
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("button", "press")])
        await self.tick(vehicle_status="on")
        self.assertEqual(self.calls[-1][2]["value"], 10)

    async def test_solar_preparation_exhausted_wakes_enters_cooldown(self):
        await self.tick(vehicle_status="off")
        await self.tick(1800)
        await self.tick(720)
        await self.tick(120)
        self.assertEqual(self.c.daily_wakes, 2)
        self.assertFalse(self.c.starting)
        self.assertIsNotNone(self.c.cooldown_until)

    async def test_failed_limit_update_cannot_delay_solar_stop(self):
        await self.solar_active()
        await self.tick(1800, charge_limit="unavailable")
        self.assertEqual(self.calls[-1][1], "turn_off")

    async def test_midnight_during_save_does_not_dispatch_yesterdays_reservation(self):
        self.clock.now = self.clock.now.replace(hour=23, minute=59, second=59)
        self.c._store.before_save = lambda: setattr(
            self.clock, "now", self.clock.now + timedelta(seconds=1)
        )
        await self.c._dispatch("current", 10)
        self.assertEqual(self.calls, [])

    async def test_reload_of_incomplete_solar_start_requires_cooldown(self):
        await self.tick(vehicle_status="off")
        await self.tick(1800)
        other = controller_type(self.clock)(self.hass, self.entry)
        other._store.saved = deepcopy(self.c._store.saved)
        await other.async_initialize()
        self.assertEqual(other.daily_solar_sessions, 1)
        self.assertEqual(other.daily_wakes, 1)
        self.assertFalse(other.starting)
        self.assertEqual(other.cooldown_until, self.clock.now + timedelta(hours=1))

    async def test_active_solar_session_survives_reload(self):
        await self.solar_active(grid=0)
        other = controller_type(self.clock)(self.hass, self.entry)
        other._store.saved = deepcopy(self.c._store.saved)
        await other.async_initialize()
        other._active = True
        await other.async_evaluate("reload")
        self.assertEqual(other.session_kind, "solar")
        self.assertEqual(self.calls, [])

    async def test_multi_amp_increase_rechecks_full_power_after_saving(self):
        await self.solar_active(grid=-2000)
        self.c._store.before_save = lambda: self.states.update(grid_net_power="-500")
        await self.tick(60)
        self.assertEqual(self.calls, [])

    async def test_optional_notification_does_not_consume_tesla_budget(self):
        self.c.options["notify_service"] = "notify.mobile"
        self.c.session_kind = "solar"
        await self.tick(charge_switch="on", grid_net_power=0)
        self.assertEqual(self.calls[-1][0], "notify")
        self.assertEqual(self.c.daily_commands, 0)

    async def test_solar_session_does_not_restart_near_target_on_later_day(self):
        await self.tick(battery_level=79)
        await self.tick(1800)
        await self.tick(24 * 3600)
        self.assertEqual(self.calls, [])

    async def test_last_two_commands_do_not_allow_new_start(self):
        self.c._roll_day()
        self.c.daily_commands = 148
        await self.tick()
        await self.tick(1800)
        self.assertEqual(self.calls, [])
        self.assertIn("Insufficient", self.c.status)

    async def test_successful_wake_allows_time_for_normal_fleet_poll_before_retry(self):
        self.states["vehicle_status"] = "off"
        await self.c._dispatch("wake", "on")
        self.clock.now += timedelta(seconds=120)
        await self.c._dispatch("wake", "on")
        self.assertEqual(len(self.calls), 1)
        self.clock.now += timedelta(seconds=600)
        await self.c._dispatch("wake", "on")
        self.assertEqual(len(self.calls), 2)

    async def test_stale_active_charge_cannot_cause_implicit_wake_for_stop(self):
        self.states.update(vehicle_status="off", charge_switch="on")
        await self.c._dispatch("charge", "off")
        self.assertEqual(self.calls, [])
