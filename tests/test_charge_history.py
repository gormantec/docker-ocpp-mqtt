import unittest
from datetime import datetime, timedelta, timezone

from src.charge_history import (
    add_energy_delta,
    close_monitoring_gap,
    open_monitoring_gap,
    parse_meter_values,
    record_session_meter,
)


class ChargeHistoryTests(unittest.TestCase):
    def test_parses_meter_units_and_optional_soc(self):
        values = parse_meter_values([{
            "timestamp": "2026-10-02T00:00:00Z",
            "sampledValue": [
                {"value": "4.8", "measurand": "Power.Active.Import", "unit": "kW"},
                {"value": "12.5", "measurand": "Energy.Active.Import.Register", "unit": "kWh"},
                {"value": "76", "measurand": "SoC", "unit": "Percent"},
                {"value": "230", "measurand": "Voltage", "unit": "V", "phase": "L1"},
                {"value": "231", "measurand": "Voltage", "unit": "V", "phase": "L2"},
            ],
        }])

        self.assertEqual(values[0]["power_w"], 4800)
        self.assertEqual(values[0]["energy_wh"], 12500)
        self.assertEqual(values[0]["soc_percent"], 76)
        self.assertEqual(values[0]["voltage_v"], 230.5)
        self.assertEqual(len(values[0]["measurements"]), 5)

    def test_soc_absence_is_not_reported_as_zero(self):
        values = parse_meter_values([{"sampledValue": [
            {"value": "2", "measurand": "Power.Active.Import", "unit": "W"},
        ]}])

        self.assertIsNone(values[0]["soc_percent"])

    def test_parses_snake_case_meter_values_from_ocpp_handler(self):
        values = parse_meter_values([{
            "timestamp": "2026-10-02T00:00:00Z",
            "sampled_value": [
                {"value": "1531", "measurand": "Power.Active.Import", "unit": "W"},
                {"value": "2491880", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
            ],
        }])

        self.assertEqual(values[0]["power_w"], 1531)
        self.assertEqual(values[0]["energy_wh"], 2491880)

    def test_cumulative_energy_delta_handles_meter_reset(self):
        state = {"last_energy_wh": 1000, "energy_delivered_wh": 0}

        self.assertEqual(add_energy_delta(state, 1500), 500)
        self.assertEqual(state["energy_delivered_wh"], 500)
        self.assertEqual(add_energy_delta(state, 25), 0)
        self.assertEqual(state["meter_resets"], 1)
        self.assertEqual(add_energy_delta(state, 100), 75)
        self.assertEqual(state["energy_delivered_wh"], 575)

    def test_session_meter_tracks_totals_and_bounds_cadenced_samples(self):
        session = {"samples": []}
        start = datetime(2026, 10, 2, tzinfo=timezone.utc)

        self.assertTrue(record_session_meter(session, {
            "energy_wh": 1000, "soc_percent": 40, "power_w": 3500,
        }, start, 60, 1))
        self.assertFalse(record_session_meter(session, {
            "energy_wh": 1010, "soc_percent": 41, "power_w": 3600,
        }, start + timedelta(seconds=30), 60, 1))
        self.assertEqual(session["energy_delivered_wh"], 10)
        self.assertEqual(session["soc_end_percent"], 41)
        self.assertEqual(len(session["samples"]), 1)

        self.assertTrue(record_session_meter(session, {
            "energy_wh": 1025, "soc_percent": 42, "power_w": 3700,
        }, start + timedelta(seconds=60), 60, 1))
        self.assertEqual(session["energy_delivered_wh"], 25)
        self.assertEqual(session["soc_start_percent"], 40)
        self.assertEqual(session["soc_max_percent"], 42)
        self.assertEqual(len(session["samples"]), 1)
        self.assertEqual(session["samples"][0]["energy_wh"], 1025)

    def test_monitoring_gap_is_deduplicated_and_closed_on_recovery(self):
        session = {"health": "ok", "faults": []}
        disconnected_at = datetime(2026, 10, 2, tzinfo=timezone.utc)

        self.assertTrue(open_monitoring_gap(session, disconnected_at, "disconnected"))
        self.assertFalse(open_monitoring_gap(
            session, disconnected_at + timedelta(seconds=5), "restarted"
        ))
        self.assertEqual(len(session["monitoring_gaps"]), 1)
        self.assertEqual(session["health"], "monitoring_gap")
        self.assertEqual(session["last_event_at"], disconnected_at.isoformat())

        recovered_at = disconnected_at + timedelta(minutes=2)
        self.assertTrue(close_monitoring_gap(session, recovered_at))
        self.assertEqual(session["monitoring_gaps"][0]["ended_at"], recovered_at.isoformat())
        self.assertEqual(session["health"], "ok")
        self.assertFalse(close_monitoring_gap(session, recovered_at))


if __name__ == "__main__":
    unittest.main()