import unittest

from src.charge_history import add_energy_delta, parse_meter_values


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

    def test_cumulative_energy_delta_handles_meter_reset(self):
        state = {"last_energy_wh": 1000, "energy_delivered_wh": 0}

        self.assertEqual(add_energy_delta(state, 1500), 500)
        self.assertEqual(state["energy_delivered_wh"], 500)
        self.assertEqual(add_energy_delta(state, 25), 0)
        self.assertEqual(state["meter_resets"], 1)
        self.assertEqual(add_energy_delta(state, 100), 75)
        self.assertEqual(state["energy_delivered_wh"], 575)


if __name__ == "__main__":
    unittest.main()