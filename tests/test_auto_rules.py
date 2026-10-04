import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src import server


class AutoRulesTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            **server.DEFAULT_SCHEDULE,
            "mode": "auto",
            "max_amps": 16,
            "min_battery_soc": 50,
            "grid_deadband_w": 150,
        }
        self.metrics = {
            "grid_import": 0,
            "grid_export": 0,
            "load_power": 0,
            "battery_soc": 80,
            "pv_power": 0,
            "last_update": datetime.now(timezone.utc),
        }
        self.state = {
            "throttled_watts": 16 * 230,
            "level_a": 16,
            "direction": None,
            "consecutive": 0,
            "reason": "",
            "initialised": True,
        }
        self.stack = [
            patch.object(server, "_get_schedule", return_value=self.config),
            patch.object(server, "_is_off_peak", return_value=False),
            patch.object(server, "_measured_voltage", return_value=230),
            patch.dict(server._solar_metrics, self.metrics),
            patch.dict(server._solar_throttle, {"test-cp": self.state}, clear=True),
            patch.dict(server._cp_state, {}, clear=True),
        ]
        for patcher in self.stack:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_stale_metrics_turn_auto_off(self):
        server._solar_metrics["last_update"] = datetime.now(timezone.utc) - timedelta(seconds=181)

        watts, changed = server._auto_decide("test-cp")

        self.assertTrue(changed)
        self.assertEqual(watts, 0)
        self.assertEqual(server._auto_state("test-cp")["reason"], "site metrics stale")

    def test_low_battery_turns_auto_off(self):
        server._solar_metrics["battery_soc"] = 49

        watts, changed = server._auto_decide("test-cp")

        self.assertTrue(changed)
        self.assertEqual(watts, 0)
        self.assertIn("below minimum", server._auto_state("test-cp")["reason"])

    def test_surplus_steps_up_after_six_checks(self):
        server._solar_metrics["grid_export"] = 6000
        server._solar_throttle["test-cp"].update(
            throttled_watts=0, level_a=0, initialised=True
        )

        for _ in range(server.AUTO_UP_CHECKS - 1):
            watts, changed = server._auto_decide("test-cp")
            self.assertFalse(changed)
        watts, changed = server._auto_decide("test-cp")

        self.assertTrue(changed)
        self.assertEqual(watts, 16 * 230)

    def test_import_steps_down_after_two_checks_using_configured_deadband(self):
        server._solar_metrics.update(grid_import=1000, grid_export=0)
        server._cp_state["test-cp"] = {"meter_values": {"1": {"power": 16 * 230}}}
        self.config["grid_deadband_w"] = 150

        _, changed = server._auto_decide("test-cp")
        self.assertFalse(changed)
        watts, changed = server._auto_decide("test-cp")

        self.assertTrue(changed)
        self.assertEqual(watts, 8 * 230)

    def test_configured_deadband_is_used_for_import_gate(self):
        server._solar_metrics["grid_import"] = 500
        self.config["grid_deadband_w"] = 1000

        watts, changed = server._auto_decide("test-cp")

        self.assertFalse(changed)
        self.assertEqual(watts, 16 * 230)

    def test_off_peak_uses_configured_cap_immediately(self):
        with patch.object(server, "_is_off_peak", return_value=True):
            server._solar_throttle["test-cp"].update(
                throttled_watts=0, level_a=0, initialised=True
            )
            watts, changed = server._auto_decide("test-cp")

        self.assertTrue(changed)
        self.assertEqual(watts, 16 * 230)


if __name__ == "__main__":
    unittest.main()
