from collections.abc import Mapping
from datetime import datetime, timezone
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


def record_session_meter(session, sample, received_at, sample_interval_seconds, max_samples):
    """Update session totals and append a bounded, cadence-limited sample."""
    energy_wh = sample.get("energy_wh")
    if energy_wh is not None:
        add_energy_delta(session, energy_wh)

    soc = sample.get("soc_percent")
    if soc is not None:
        if session.get("soc_start_percent") is None:
            session["soc_start_percent"] = soc
        session["soc_end_percent"] = soc
        session["soc_min_percent"] = min(session.get("soc_min_percent", soc), soc)
        session["soc_max_percent"] = max(session.get("soc_max_percent", soc), soc)

    previous = session.get("last_history_sample_at")
    if isinstance(previous, str):
        try:
            previous = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        except ValueError:
            previous = None
    if previous and previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    if previous and (received_at - previous).total_seconds() < sample_interval_seconds:
        return False

    session.setdefault("samples", []).append({
        "received_at": received_at.isoformat(),
        "charger_timestamp": sample.get("timestamp"),
        "power_w": sample.get("power_w"),
        "energy_wh": energy_wh,
        "energy_delivered_wh": session.get("energy_delivered_wh", 0.0),
        "soc_percent": soc,
        "current_a": sample.get("current_a"),
        "voltage_v": sample.get("voltage_v"),
        "measurements": sample.get("measurements", []),
    })
    session["samples"] = session["samples"][-max(1, int(max_samples)):]
    session["last_history_sample_at"] = received_at.isoformat()
    session["last_event_at"] = received_at.isoformat()
    return True


def open_monitoring_gap(session, started_at, reason):
    """Record a gap once, leaving it open until connection recovery."""
    gaps = session.setdefault("monitoring_gaps", [])
    if any(not gap.get("ended_at") for gap in gaps):
        session["health"] = "monitoring_gap"
        return False
    gaps.append({"started_at": started_at.isoformat(), "reason": reason})
    session["health"] = "monitoring_gap"
    return True


def close_monitoring_gap(session, ended_at, reason="charge_point_reconnected"):
    """Close the newest open gap and restore the session's remaining health."""
    for gap in reversed(session.get("monitoring_gaps", [])):
        if not gap.get("ended_at"):
            gap["ended_at"] = ended_at.isoformat()
            gap["reason_resolved"] = reason
            session["health"] = "faulted" if session.get("faults") else "ok"
            session["last_event_at"] = ended_at.isoformat()
            return True
    return False