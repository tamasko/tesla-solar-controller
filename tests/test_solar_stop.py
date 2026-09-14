"""Focused controller timing tests without a Home Assistant installation."""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/tesla_solar_controller/controller.py"
TREE = ast.parse(SOURCE.read_text())
CLASS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "TeslaSolarController")
METHODS = {n.name: n for n in CLASS.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def method(name, namespace):
    node = METHODS[name]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


class SolarStopTest(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        ns = {
            "dt_util": SimpleNamespace(utcnow=lambda: self.clock.now),
            "OPT_SOLAR_START_EXPORT_W": "start",
            "OPT_SOLAR_STOP_IMPORT_W": "stop",
            "OPT_SOLAR_STOP_MINUTES": "minutes",
        }
        self.update = method("_update_solar_timers", ns)
        self.ready = method("_solar_stop_ready", ns)
        self.c = SimpleNamespace(
            grid_net_power_w=400, charge_current_a=5, minimum_current_a=5,
            is_charging=True, _solar_control_active=True, maintenance_active=False,
            _solar_production_ready=lambda: True,
            options={"start": 1350, "stop": 300, "minutes": 3},
            _solar_export_since=None, _solar_import_since=None,
        )

    def tick(self, seconds, grid=400, amps=5):
        self.clock.now += timedelta(seconds=seconds)
        self.c.grid_net_power_w = grid
        self.c.charge_current_a = amps
        self.update(self.c)
        return self.ready(self.c)

    def test_continuous_ten_minutes_and_reset(self):
        self.update(self.c)
        self.assertFalse(self.tick(599))
        self.assertTrue(self.tick(1))
        self.assertFalse(self.tick(1, grid=0))
        self.assertFalse(self.tick(420, grid=0))
        self.assertFalse(self.tick(1, grid=400))
        self.assertFalse(self.tick(599))
        self.assertTrue(self.tick(1))

    def test_timer_starts_only_at_minimum(self):
        self.tick(0, amps=6)
        self.assertFalse(self.tick(600, amps=6))
        self.assertFalse(self.tick(0, amps=5))
        self.assertTrue(self.tick(600, amps=5))


class SurplusDiagnosticTest(unittest.TestCase):
    def test_zero_transition_logs_once_with_raw_inputs(self):
        from math import isfinite
        from unittest.mock import Mock

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        states = {
            "grid": SimpleNamespace(state="-2000", last_updated=now),
            "solar": SimpleNamespace(state="2500", last_updated=now),
        }
        logger = Mock()
        ns = {
            "dt_util": SimpleNamespace(utcnow=lambda: now),
            "_LOGGER": logger,
            "CONF_GRID_NET_POWER": "grid", "CONF_SOLAR_POWER": "solar",
            "CONF_CHARGE_SWITCH": "charge", "CONF_CHARGE_CABLE": "cable",
            "Any": object, "isfinite": isfinite,
        }
        parse = method("_parse_float_state", ns).__func__
        missing = method("_missing_power_reason", ns).__func__
        surplus = method("solar_surplus_available_w", ns).fget
        c = SimpleNamespace(
            hass=SimpleNamespace(states=SimpleNamespace(get=states.get)),
            data={"grid": "grid", "solar": "solar", "charge": "charge", "cable": "cable"},
            live_charging_power_w=0, controller_enabled=True,
            _last_calculated_surplus_w=None,
            _parse_float_state=parse, _missing_power_reason=missing,
            _state=lambda entity: "off" if entity == "charge" else "on",
        )
        self.assertEqual(surplus(c), 2000)
        states["grid"].state = "unavailable"
        self.assertEqual(surplus(c), 0)
        self.assertEqual(surplus(c), 0)
        logger.warning.assert_called_once()
        args = logger.warning.call_args.args
        self.assertIn("Solar surplus false-zero diagnostic", args[0])
        self.assertEqual(args[3], "grid_unavailable")
        self.assertEqual(args[4], "unavailable")
        self.assertEqual(args[7], "2500")


class CurrentStepDownTest(unittest.IsolatedAsyncioTestCase):
    async def test_step_down_still_uses_thirty_seconds(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = SimpleNamespace(now=now)
        ns = {
            "dt_util": SimpleNamespace(utcnow=lambda: clock.now),
            "OPT_SOLAR_STEP_UP_EXPORT_W": "up",
            "OPT_GRID_STEP_DOWN_IMPORT_W": "down",
            "CURRENT_INCREASE_HOLD_SECONDS": 60,
            "CURRENT_DECREASE_HOLD_SECONDS": 30,
        }
        regulate = method("_async_regulate_current", ns)
        commands = []

        async def set_current(amps, *, final_guard):
            self.assertTrue(final_guard())
            commands.append(amps)
            return True

        c = SimpleNamespace(
            _clamp_charge_current=lambda amps: amps,
            grid_net_power_w=400, charge_current_a=6,
            is_charging=True, is_awake=True, charging_commands_allowed=True,
            maximum_current_a=10, options={"up": 300, "down": 150},
            _command_values_match=lambda a, b, tolerance: abs(a-b) <= tolerance,
            _solar_production_ready=lambda: True,
            _solar_stop_ready=lambda: False,
            _reset_current_regulation_timers=lambda: None,
            _async_set_charge_current=set_current,
            _current_increase_since=None, _current_decrease_since=None,
        )
        await regulate(c, 5, True)
        clock.now += timedelta(seconds=29)
        await regulate(c, 5, True)
        self.assertEqual(commands, [])
        clock.now += timedelta(seconds=1)
        await regulate(c, 5, True)
        self.assertEqual(commands, [5])


if __name__ == "__main__":
    unittest.main()
