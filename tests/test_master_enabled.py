"""Master switch and final-dispatch tests using isolated controller methods."""

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_solar_stop import method


def switch_method(name):
    source = Path(__file__).resolve().parents[1] / "custom_components/tesla_solar_controller/switch.py"
    tree = ast.parse(source.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TeslaControllerEnabledSwitch")
    node = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace[name]


class MasterDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        async def call(domain, service, data, blocking):
            self.calls.append((domain, service, data))

        self.c = SimpleNamespace(
            _command_lock=asyncio.Lock(), _runtime_active=True,
            controller_enabled=False, _command_epoch=2,
            _task_command_epochs={},
            charge_cable_connected=True, solar_wake_cable_allowed=True,
            _solar_dispatch_ready=lambda bypass: True,
            _confirm_pending_vehicle_commands=lambda: None,
            _command_already_reflected=lambda key, desired: False,
            _command_should_dispatch=lambda key, desired, now: True,
            _record_vehicle_command=lambda key, desired, now: None,
            hass=SimpleNamespace(services=SimpleNamespace(async_call=call)),
        )
        self.dispatch = method("_async_vehicle_command", {
            "asyncio": asyncio,
            "dt_util": SimpleNamespace(utcnow=lambda: self.now),
            "_LOGGER": Mock(),
        })

    async def issue(self, key, domain, service, *, cable=False, solar=False):
        return await self.dispatch(
            self.c, command_key=key, desired="on", domain=domain,
            service=service, data={"entity_id": "tesla"},
            requires_cable=cable, requires_solar_start=solar,
        )

    async def test_off_blocks_all_command_categories(self):
        # These are the final service shapes used by Solar, off-peak, ASAP,
        # maintenance, accessory power, current, limit, wake, and refresh.
        for key, domain, service, cable, solar in [
            ("wake", "button", "press", True, True),
            ("charge_state", "switch", "turn_on", True, True),
            ("charge_state", "switch", "turn_on", True, False),
            ("charge_state", "switch", "turn_off", False, False),
            ("charge_current", "number", "set_value", True, False),
            ("charge_limit", "number", "set_value", True, False),
            ("accessory_power", "switch", "turn_on", False, False),
            ("accessory_power", "switch", "turn_off", False, False),
            (None, "homeassistant", "update_entity", False, False),
        ]:
            self.assertFalse(await self.issue(key, domain, service, cable=cable, solar=solar))
        self.assertEqual(self.calls, [])

    async def test_delayed_old_epoch_is_blocked_after_reenable(self):
        task = asyncio.current_task()
        self.c._task_command_epochs[task] = 1
        self.c.controller_enabled = True
        self.assertFalse(await self.issue("charge_current", "number", "set_value", cable=True))
        self.assertEqual(self.calls, [])
        self.c._task_command_epochs.pop(task)
        self.assertTrue(await self.issue("charge_current", "number", "set_value", cable=True))
        self.assertEqual(len(self.calls), 1)

    async def test_off_while_charging_does_not_send_final_stop(self):
        self.c.is_charging = True
        self.c._pending_command_opposes = lambda key, desired: False
        stop = method("_async_stop_charge_if_active", {"CONF_CHARGE_SWITCH": "charge"})
        self.c.data = {"charge": "tesla"}
        self.c._async_vehicle_command = lambda **kwargs: self.dispatch(self.c, **kwargs)
        self.assertFalse(await stop(self.c))
        self.assertEqual(self.calls, [])


class MasterSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_switch_publishes_actual_state_even_if_save_fails(self):
        writes = []

        async def fail_after_setting(value):
            c.controller_enabled = value
            raise RuntimeError("storage failed")

        c = SimpleNamespace(controller_enabled=False)
        entity = SimpleNamespace(
            controller=c,
            async_write_ha_state=lambda: writes.append(c.controller_enabled),
        )
        c.async_set_controller_enabled = fail_after_setting
        with self.assertRaisesRegex(RuntimeError, "storage failed"):
            await switch_method("async_turn_on")(entity)
        self.assertEqual(writes, [True])

    async def test_repeated_request_republishes_switch_state(self):
        published = []
        c = SimpleNamespace(
            controller_enabled=True,
            _update_status=lambda: None,
            _notify=lambda: published.append(c.controller_enabled),
        )
        toggle = method("async_set_controller_enabled", {"asyncio": asyncio})
        await toggle(c, True)
        self.assertEqual(published, [True])

    async def test_off_is_published_before_storage_finishes(self):
        release_save = asyncio.Event()
        published = []

        async def save():
            await release_save.wait()

        c = SimpleNamespace(
            controller_enabled=True, _command_epoch=1,
            _accessory_task=None, _evaluate_task=None,
            _evaluation_pending=False,
            _reset_control_timers=lambda: None,
            _async_save=save, _update_status=lambda: None,
            _notify=lambda: published.append(c.controller_enabled),
        )
        toggle = method("async_set_controller_enabled", {"asyncio": asyncio})
        task = asyncio.create_task(toggle(c, False))
        await asyncio.sleep(0)
        self.assertFalse(c.controller_enabled)
        self.assertEqual(published, [False])
        self.assertFalse(task.done())
        release_save.set()
        await task

    async def test_turn_off_resets_timers_saves_state_and_turn_on_reevaluates(self):
        actions = []

        async def save():
            actions.append(("save", c.controller_enabled))

        c = SimpleNamespace(
            controller_enabled=True, _command_epoch=3,
            _accessory_task=None, _evaluate_task=None,
            _evaluation_pending=False,
            _reset_control_timers=lambda: actions.append(("reset", c.controller_enabled)),
            _async_save=save, _update_status=lambda: None,
            _notify=lambda: None,
            _schedule_evaluate=lambda reason: actions.append(("evaluate", reason)),
        )
        toggle = method("async_set_controller_enabled", {"asyncio": asyncio})
        await toggle(c, False)
        self.assertFalse(c.controller_enabled)
        self.assertIn(("reset", False), actions)
        self.assertIn(("save", False), actions)
        self.assertFalse(any(a[0] == "evaluate" for a in actions))
        await toggle(c, True)
        self.assertTrue(c.controller_enabled)
        self.assertEqual(actions[-1], ("evaluate", "controller_enabled"))
        self.assertEqual(c._command_epoch, 5)

    async def test_restored_off_state_remains_off(self):
        saved = {"controller_enabled": False}
        c = SimpleNamespace(
            _store=SimpleNamespace(async_load=lambda: asyncio.sleep(0, result=saved)),
            controller_enabled=True, mode="Solar only", requested_accessory=False,
            _accessory_command_needed=False, maintenance_active=False,
            last_battery_pct=None, last_charge_current_a=None,
            last_charge_limit_pct=None,
            _remember_live_charge_cable_state=lambda: None,
            _refresh_cached_values=lambda: None, _update_status=lambda: None,
        )
        initialize = method("async_initialize", {"MODES": ["Solar only"]})
        await initialize(c)
        self.assertFalse(c.controller_enabled)


class DisabledEvaluationTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_modes_never_reach_mode_handlers(self):
        evaluate = method("async_evaluate", {"asyncio": asyncio, "_LOGGER": Mock()})
        for mode, maintenance, accessory in [
            ("Solar only", False, False),
            ("Solar + off-peak", False, False),
            ("Charge ASAP", False, False),
            ("Solar only", True, False),
            ("Solar only", False, True),
        ]:
            actions = []
            c = SimpleNamespace(
                controller_enabled=False, mode=mode,
                maintenance_active=maintenance, requested_accessory=accessory,
                grid_net_power_w=-2500, solar_power_w=3000,
                last_battery_pct=20,
                _refresh_cached_values=lambda: None,
                _confirm_pending_vehicle_commands=lambda: None,
                _desired_target_soc=lambda: 80,
                _reset_control_timers=lambda: actions.append("reset"),
                _update_status=lambda: None,
                _notify=lambda: None,
                _update_solar_timers=lambda: actions.append("solar_timer"),
            )
            await evaluate(c, "test")
            self.assertEqual(actions, ["reset"], (mode, maintenance, accessory))
