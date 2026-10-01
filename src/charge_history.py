from collections.abc import Mapping
from math import isfinite


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _scale(unit, units):
    return units.get(str(unit or ""), 1.0)


def parse_meter_values(meter_values):
    """Normalize OCPP MeterValue entries while retaining their sampled values."""
    parsed = []
    for entry in meter_values if isinstance(meter_values, list) else []:
        if not isinstance(entry, Mapping):
            continue

        measurements = []
        values = {}
        for sample in entry.get("sampledValue", []):
            if not isinstance(sample, Mapping):
                continue
            item = dict(sample)
            measurements.append(item)
            measurand = str(item.get("measurand") or "Energy.Active.Import.Register")
            value = _number(item.get("value"))
            if value is None:
                continue
            unit = item.get("unit")
            phase = item.get("phase")
            values.setdefault(measurand, []).append((value * _scale(unit, {
                "kW": 1000.0,
                "kWh": 1000.0,
                "mA": 0.001,
                "mV": 0.001,
            }), phase))

        def select(measurand, aggregate="last"):
            candidates = values.get(measurand, [])
            unphased = [value for value, phase in candidates if not phase]
            selected = unphased or [value for value, _ in candidates]
            if not selected:
                return None
            if aggregate == "sum":
                return sum(selected)
            if aggregate == "average":
                return sum(selected) / len(selected)
            return selected[-1]

        soc = select("SoC")
        parsed.append({
            "timestamp": entry.get("timestamp"),
            "power_w": select("Power.Active.Import", "sum"),
            "energy_wh": select("Energy.Active.Import.Register"),
            "soc_percent": soc if soc is not None and 0 <= soc <= 100 else None,
            "current_a": select("Current.Import", "sum"),
            "voltage_v": select("Voltage", "average"),
            "measurements": measurements,
        })
    return parsed


def add_energy_delta(state, meter_wh):
    """Accumulate non-negative energy deltas from a cumulative import meter."""
    meter_wh = _number(meter_wh)
    if meter_wh is None or meter_wh < 0:
        return 0.0

    previous = _number(state.get("last_energy_wh"))
    delta = 0.0
    if previous is None:
        state.setdefault("meter_start_wh", meter_wh)
    elif meter_wh >= previous:
        delta = meter_wh - previous
    else:
        state["meter_resets"] = int(state.get("meter_resets", 0)) + 1

    state["last_energy_wh"] = meter_wh
    state["energy_delivered_wh"] = round(
        (_number(state.get("energy_delivered_wh")) or 0.0) + delta, 3
    )
    return delta