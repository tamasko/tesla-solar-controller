"""Real file retention and read-only viewer, plus controller logging safety."""

import ast
import asyncio
import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_solar_stop as fixture

spec = importlib.util.spec_from_file_location("tesla_activity_log", fixture.ROOT / "activity_log.py")
activity_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activity_module)
ActivityLog = activity_module.ActivityLog


class ActivityFileTest(unittest.TestCase):
    def test_close_drains_and_reopen_appends_with_escaped_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.log"
            payload = {"event": "mode_changed", "mode": "Solar only\n<script>"}
            writer = ActivityLog(path)
            writer.record(payload)
            writer.close()
            writer.close()
            writer = ActivityLog(path)
            try:
                writer.record({"event": "controller_initialized"})
                lines = writer.read().splitlines()
                self.assertEqual(len(lines), 2)
                self.assertEqual(json.loads(lines[0]), payload)
                self.assertEqual(json.loads(lines[1])["event"], "controller_initialized")
            finally:
                writer.close()

    def test_rotation_is_bounded_and_snapshot_keeps_chronological_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.log"
            writer = ActivityLog(path, max_bytes=150, backups=2)
            try:
                for n in range(100):
                    writer.record({"n": n, "message": "0123456789"})
                records = [json.loads(line) for line in writer.read().splitlines()]
                self.assertEqual(records[-1]["n"], 99)
                self.assertEqual([r["n"] for r in records], sorted(r["n"] for r in records))
                self.assertGreater(records[0]["n"], 0)
                self.assertEqual(len(list(Path(directory).iterdir())), 3)
                self.assertTrue(all(p.stat().st_size <= 150 for p in Path(directory).iterdir()))
            finally:
                writer.close()

    def test_record_only_queues_and_does_not_write_on_callers_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = ActivityLog(Path(directory) / "controller.log")
            try:
                with patch.object(writer._queue_handler, "enqueue") as enqueue:
                    writer.record({"event": "command_dispatch"})
                    enqueue.assert_called_once()
                    self.assertIn("command_dispatch", enqueue.call_args.args[0].msg)
            finally:
                writer.close()


class ControllerActivityTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ControllerTest.asyncSetUp
    tick = fixture.ControllerTest.tick

    def capture(self):
        self.events = []
        self.c._activity_log = SimpleNamespace(record=lambda event: self.events.append(deepcopy(event)))

    async def test_command_lifecycle_and_awake_feedback_are_distinct(self):
        self.capture()
        self.c.mode = "Charge ASAP"
        self.states["vehicle_status"] = "off"
        await self.c._dispatch("wake", "on", wake_reason="asap")
        self.assertEqual([e["event"] for e in self.events], [
            "command_requested", "command_reserved", "command_dispatch", "command_succeeded"
        ])
        sent = self.events[2]
        self.assertEqual(sent["wakes_today"], 1)
        self.assertEqual(sent["service"], "button.press")
        self.assertEqual(sent["wake_reason"], "asap")
        self.assertEqual(sent["cable_connected"], True)
        self.assertFalse(sent["awake"])
        self.assertEqual(sent["p1_w"], -2000)
        self.assertEqual(sent["service_data"], {"entity_id": "wake"})
        self.states["vehicle_status"] = "on"
        self.c._observe_feedback()
        self.assertEqual(self.events[-1]["event"], "command_confirmed")
        count = len(self.events)
        self.c._observe_feedback()
        self.assertEqual(len(self.events), count)
        self.assertEqual(len(self.calls), 1)

    async def test_failure_duplicate_and_budget_decisions_are_logged(self):
        self.capture()
        self.c.mode = "Charge ASAP"
        self.fail_service = True
        self.states["vehicle_status"] = "off"
        with self.assertLogs(level="ERROR"):
            await self.c._dispatch("wake", "on")
        self.assertEqual(self.events[-1]["event"], "command_failed")
        self.assertEqual(self.events[-1]["error_type"], "RuntimeError")
        await self.c._dispatch("wake", "on")
        self.assertEqual(self.events[-1]["reason"], "duplicate_suppressed")
        self.c.daily_wakes = 2
        await self.c._dispatch("wake", "on")
        self.assertEqual(self.events[-1]["reason"], "Daily wake budget reached")
        self.assertEqual(len(self.calls), 1)

    async def test_disabled_and_cable_rejections_are_logged_without_dispatch(self):
        self.capture()
        self.c.controller_enabled = False
        await self.c._dispatch("wake", "on")
        self.assertEqual(self.events[-1]["reason"], "controller_disabled")
        self.c.controller_enabled = True
        self.states["charge_cable"] = "off"
        await self.c._dispatch("charge", "on")
        self.assertEqual(self.events[-1]["reason"], "cable_not_positively_connected")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.c.daily_commands, 0)

    async def test_gate_change_while_saving_is_logged_and_still_blocks(self):
        self.capture()
        self.c._store.before_save = lambda: setattr(self.c, "controller_enabled", False)
        await self.c._dispatch("current", 10)
        self.assertEqual(self.events[-1]["reason"], "after_reservation: controller_disabled")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.c.daily_commands, 1)

    async def test_log_enqueue_can_change_gate_without_bypassing_final_check(self):
        self.capture()
        def record(event):
            self.events.append(event)
            if event["event"] == "command_reserved":
                self.states["charge_cable"] = "off"
        self.c._activity_log = SimpleNamespace(record=record)
        await self.c._dispatch("current", 10)
        self.assertEqual(self.calls, [])
        self.assertIn("cable_not_positively_connected", self.events[-1]["reason"])

    async def test_mode_enabled_status_and_rollover_events(self):
        self.capture()
        self.c._active = False
        await self.c.async_set_mode("Solar + off-peak")
        event = next(e for e in self.events if e["event"] == "mode_changed")
        self.assertEqual(event["previous_mode"], "Solar only")
        self.assertEqual(event["mode"], "Solar + off-peak")
        await self.c.async_set_controller_enabled(False)
        self.assertTrue(any(e["event"] == "enabled_changed" and not e["enabled"] for e in self.events))
        self.c._record_status("test")
        count = len(self.events)
        self.c._record_status("test")
        self.assertEqual(len(self.events), count)
        self.clock.now += fixture.timedelta(days=1)
        self.c._roll_day()
        self.assertEqual(self.events[-1]["event"], "daily_budget_reset")
        self.assertEqual(self.calls, [])

    async def test_real_file_records_commands_and_reload_without_extra_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = ActivityLog(Path(directory) / "controller.log")
            try:
                self.c._activity_log = writer
                await self.c._dispatch("current", 10)
                other = fixture.controller_type(self.clock)(
                    self.hass, self.entry, activity_log=writer
                )
                other._store.saved = deepcopy(self.c._store.saved)
                await other.async_initialize()
                events = [json.loads(line) for line in writer.read().splitlines()]
                names = [event["event"] for event in events]
                self.assertIn("command_dispatch", names)
                self.assertIn("command_succeeded", names)
                self.assertIn("controller_initialized", names)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(other.daily_commands, 1)
            finally:
                writer.close()

    async def test_source_and_option_changes_log_context_without_tesla_calls(self):
        self.capture()
        self.c._active = False
        self.c._changed(SimpleNamespace(data={
            "entity_id": "charge_current",
            "old_state": SimpleNamespace(state="5"),
            "new_state": SimpleNamespace(state="10"),
        }))
        self.assertEqual(self.events[-1]["event"], "source_changed")
        self.assertEqual(self.events[-1]["new_state"], "10")
        count = len(self.events)
        self.c._changed(SimpleNamespace(data={"entity_id": "grid_net_power"}))
        self.assertEqual(len(self.events), count)
        await self.c.async_options_updated({"evening_offpeak_start_hour": 21})
        self.assertEqual(self.events[-1]["options"]["evening_offpeak_start_hour"], 21)
        self.assertEqual(self.calls, [])

    async def test_logging_failure_does_not_interrupt_safety_or_commands(self):
        def fail(payload):
            raise OSError("log unavailable")
        self.c._activity_log = SimpleNamespace(record=fail)
        with self.assertLogs(level="ERROR"):
            await self.c._dispatch("current", 10)
            self.c.controller_enabled = False
            await self.c._dispatch("charge", "off")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2]["value"], 10)

    async def test_reservation_failure_logged_without_service_call(self):
        self.capture()
        self.c._store.fail = True
        with self.assertLogs(level="ERROR"):
            await self.c._dispatch("current", 10)
        self.assertEqual(self.events[-1]["event"], "command_storage_failed")
        self.assertEqual(self.calls, [])

    async def test_cancelled_service_records_unknown_outcome(self):
        self.capture()
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError
        self.hass.services.async_call = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.c._dispatch("current", 10)
        self.assertEqual(self.events[-1]["event"], "command_cancelled")
        self.assertEqual(self.events[-1]["reason"], "service_outcome_unknown")
        self.assertEqual(self.c.daily_commands, 1)


def view_namespace():
    tree = ast.parse((fixture.ROOT / "log_view.py").read_text())
    tree.body = [node for node in tree.body if not isinstance(node, ast.ImportFrom) or not (
        node.level or node.module.startswith("homeassistant")
    )]
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            node.decorator_list = []
    ns = dict(ActivityLog=ActivityLog, DOMAIN="tesla_solar_controller", EVENT_HOMEASSISTANT_STOP="stop", __file__=str(fixture.ROOT / "log_view.py"))
    exec(compile(tree, "log_view.py", "exec"), ns)
    return ns


class LogViewTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_and_missing_user_cannot_read_file(self):
        handler = view_namespace()["websocket_activity_log"]
        for user in (None, SimpleNamespace(is_admin=False)):
            errors = []
            connection = SimpleNamespace(user=user, send_error=lambda *args: errors.append(args))
            # No hass.data/executor: accessing either would fail this test.
            await handler(SimpleNamespace(), connection, {"id": 1})
            self.assertEqual(errors[0][1], "unauthorized")

    async def test_authenticated_admin_reads_only_fixed_activity_file(self):
        handler = view_namespace()["websocket_activity_log"]
        with tempfile.TemporaryDirectory() as directory:
            writer = ActivityLog(Path(directory) / "controller.log")
            try:
                writer.record({"event": "mode_changed"})
                results = []
                hass = SimpleNamespace(
                    data={"tesla_solar_controller": {"activity_log": writer}},
                    async_add_executor_job=asyncio.to_thread,
                )
                connection = SimpleNamespace(user=SimpleNamespace(is_admin=True), send_result=lambda *args: results.append(args))
                await handler(hass, connection, {"id": 1, "path": "/etc/passwd"})
                self.assertEqual(results[0][1]["filename"], "tesla_solar_controller.log")
                self.assertEqual(json.loads(results[0][1]["content"])["event"], "mode_changed")
            finally:
                writer.close()

    async def test_unavailable_log_has_explicit_error(self):
        errors = []
        connection = SimpleNamespace(user=SimpleNamespace(is_admin=True), send_error=lambda *args: errors.append(args))
        await view_namespace()["websocket_activity_log"](
            SimpleNamespace(data={"tesla_solar_controller": {"activity_log": None}}),
            connection, {"id": 1},
        )
        self.assertEqual(errors[0][1], "log_unavailable")

    async def test_registration_and_shutdown_flush(self):
        ns = view_namespace()
        panels, routes, callbacks, commands = [], [], [], []
        async def static_paths(paths):
            routes.extend(paths)
        ns.update(
            frontend=SimpleNamespace(async_register_built_in_panel=lambda *a, **kw: panels.append(kw)),
            websocket_api=SimpleNamespace(async_register_command=lambda *args: commands.append(args)),
            StaticPathConfig=lambda url, path, cache: (url, path, cache),
        )
        with tempfile.TemporaryDirectory() as directory:
            hass = SimpleNamespace(
                data={}, async_add_executor_job=asyncio.to_thread,
                config=SimpleNamespace(path=lambda name: str(Path(directory) / name)),
                http=SimpleNamespace(async_register_static_paths=static_paths),
                bus=SimpleNamespace(async_listen_once=lambda event, cb: callbacks.append(cb)),
            )
            await ns["async_setup_activity_log"](hass)
            try:
                self.assertTrue(panels[0]["require_admin"])
                self.assertFalse(panels[0]["show_in_sidebar"])
                self.assertEqual(len(commands), 1)
                self.assertTrue(routes[0][1].endswith("log-panel.js"))
                self.assertNotIn(".log", routes[0][0])
                hass.data["tesla_solar_controller"]["activity_log"].record({"event": "shutdown"})
            finally:
                await callbacks[0](None)
            self.assertEqual(json.loads((Path(directory) / "tesla_solar_controller.log").read_text())["event"], "shutdown")
