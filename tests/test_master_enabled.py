"""Entity master switch always republishes the actual controller state."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


def switch_method(name):
    source = (
        Path(__file__).resolve().parents[1]
        / "custom_components/tesla_solar_controller/switch.py"
    )
    tree = ast.parse(source.read_text())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "TeslaControllerEnabledSwitch"
    )
    node = next(
        n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace[name]


class MasterSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_state_published_even_if_save_fails(self):
        writes = []
        c = SimpleNamespace(controller_enabled=True)

        async def disable(value):
            c.controller_enabled = value
            raise RuntimeError("storage failed")

        c.async_set_controller_enabled = disable
        entity = SimpleNamespace(
            controller=c,
            async_write_ha_state=lambda: writes.append(c.controller_enabled),
        )
        with self.assertRaises(RuntimeError):
            await switch_method("async_turn_off")(entity)
        self.assertEqual(writes, [False])
