"""Check v1 entry migration without importing Home Assistant."""

import ast
import unittest
from types import SimpleNamespace

from test_solar_stop import CONSTANTS, ROOT


def migration():
    tree = ast.parse((ROOT / "__init__.py").read_text())
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_migrate_entry"
    )
    node.body = [n for n in node.body if not isinstance(n, ast.ImportFrom)]
    flow = ast.parse((ROOT / "config_flow.py").read_text())
    domains = next(
        n
        for n in flow.body
        if isinstance(n, ast.Assign) and n.targets[0].id == "ENTITY_DOMAINS"
    )
    ns = dict(CONSTANTS)
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            domains,
            node,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / "__init__.py"), "exec"), ns)
    return ns["async_migrate_entry"]


class MigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_retains_current_inputs_targets_schedule_and_notifications(self):
        updates = []
        entry = SimpleNamespace(
            version=1,
            data={
                "grid_net_power": "sensor.p1",
                "l2_voltage": "sensor.l2",
                "charge_cable": "binary_sensor.cable",
                "solar_power": "sensor.old",
                "forecast_tomorrow": "sensor.forecast",
                "accessory_switch": "switch.old",
            },
            options={
                "normal_target_soc": 85,
                "solar_wake_delta_soc": 4,
                "offpeak_current_a": 32,
                "weekday_offpeak_end_hour": 6,
                "notify_service": "notify.phone",
                "adaptive_buffer_enabled": True,
                "min_current_a": 3,
            },
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda e, **kwargs: updates.append(kwargs)
            )
        )
        self.assertTrue(await migration()(hass, entry))
        result = updates[0]
        self.assertEqual(result["version"], 2)
        self.assertEqual(
            result["data"],
            {
                "grid_net_power": "sensor.p1",
                "l2_voltage": "sensor.l2",
                "charge_cable": "binary_sensor.cable",
            },
        )
        self.assertEqual(result["options"]["normal_target_soc"], 85)
        self.assertEqual(result["options"]["solar_wake_delta_soc"], 4)
        self.assertEqual(result["options"]["offpeak_current_a"], 10)
        self.assertEqual(result["options"]["weekday_offpeak_end_hour"], 6)
        self.assertEqual(result["options"]["notify_service"], "notify.phone")
        self.assertNotIn("adaptive_buffer_enabled", result["options"])
        self.assertNotIn("min_current_a", result["options"])

    async def test_newer_entry_version_is_not_downgraded(self):
        self.assertFalse(await migration()(None, SimpleNamespace(version=3)))
