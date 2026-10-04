"""
OCPP-to-MQTT Bridge — Standalone Service

Acts as an OCPP Central System (CSMS) that EV charge points connect to
via WebSocket. All charge point events are forwarded to the docker-iot
MQTT broker, and MQTT commands are translated back to OCPP operations.

Uses the ocpp Python package (mobilityhouse/ocpp) for OCPP protocol
message types and routing. No Home Assistant dependencies.

Architecture:
  - aiohttp WebSocket server for charge point connections
  - aiomqtt client for docker-iot broker
  - React UI served from / on port 9094
  - Debug API at /debug
"""

import os
import sys
import logging
import asyncio
import json
import time
import base64
from urllib.parse import quote
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, available_timezones
from collections import deque

from aiohttp import web, WSMsgType

# OCPP protocol library (pip install ocpp)
from ocpp.routing import on, after
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16.enums import (
    RegistrationStatus,
    AuthorizationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    UnlockStatus,
    ConfigurationStatus,
    ClearCacheStatus,
    TriggerMessageStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    RecurrencyKind,
)
from ocpp.v16 import call_result as ocpp_result
from ocpp.v16.call import (
    RemoteStartTransaction, RemoteStopTransaction, Reset, UnlockConnector,
    GetConfiguration, ChangeConfiguration, ClearCache, TriggerMessage,
    GetDiagnostics, UpdateFirmware, ChangeAvailability, GetLocalListVersion,
    SendLocalList, SetChargingProfile, ClearChargingProfile, GetCompositeSchedule,
)

# Shared MQTT helpers (same pattern as other docker-iot containers)
from mqtt_connect import build_mqtt_context
from charge_history import (
    add_energy_delta,
    close_monitoring_gap,
    open_monitoring_gap,
    parse_meter_values,
    record_session_meter,
)

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)
METER_VALUE_SAMPLE_INTERVAL = 15
METER_VALUES_SAMPLED_DATA = "Power.Active.Import,Energy.Active.Import.Register,Current.Import,Voltage"

# Charger Basic auth password
AUTH_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"

# ---------------------------------------------------------------------------
# Schedule helper
# ---------------------------------------------------------------------------

def _is_charging_allowed(cp_id: str) -> bool:
    """Check if charging is currently allowed (per-CP timezone with DST).
    
    stop       → always False.
    charge_now → always True.
    auto       → True (level chosen by the Auto controller).
    """
    config = _get_schedule(cp_id)
    mode = config.get("mode", "charge_now")
    if mode == "stop":
        return False
    if mode == "charge_now":
        return True
    return True  # auto: the controller picks the level (possibly OFF)

# ---------------------------------------------------------------------------
# Safe env helpers
# ---------------------------------------------------------------------------

def _env_str(key, default=None):
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    return val

def _env_int(key, default):
    val = _env_str(key)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        _LOGGER.warning("Env var %s='%s' not a valid int, using default %s", key, val, default)
        return default

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

OCPP_HOST = _env_str("OCPP_HOST", "0.0.0.0")
OCPP_PORT = _env_int("OCPP_PORT", 9000)
UI_PORT = _env_int("UI_PORT", 9094)
MQTT_THING_NAME = _env_str("MQTT_THING_NAME", "gormantec-ocpp-bridge")
MQTT_BROKER_VAR = _env_str("MQTT_BROKER", "docker-iot_server")

# DocumentDB / CouchDB persistence
DOCDB_URL = _env_str("DOCDB_URL", "")
DOCDB_USER = _env_str("DOCDB_USER", "admin")
DOCDB_PASSWORD = _env_str("DOCDB_PASSWORD", "password")
DOCDB_ENABLED = bool(DOCDB_URL)
DOCDB_DB = _env_str("DOCDB_DB", "ocpp_mqtt")
CHARGE_HISTORY_RETENTION_DAYS = _env_int("CHARGE_HISTORY_RETENTION_DAYS", 90)
MAX_SESSION_SAMPLES = _env_int("MAX_SESSION_SAMPLES", 720)
MAX_PERSISTED_EVENTS = _env_int("MAX_PERSISTED_EVENTS", 500)
METER_HISTORY_SAMPLE_SECONDS = _env_int("METER_HISTORY_SAMPLE_SECONDS", 60)

def _docdb_key(cp_id: str, doc_type: str) -> str:
    """Build a namespaced DocumentDB key: {cp_id}:{type}"""
    return f"{cp_id}:{doc_type}"

# Service start timestamp
STARTED_AT = datetime.now(timezone.utc)

# Event ring buffer
MAX_EVENTS = 200
_event_buffer: deque = deque(maxlen=MAX_EVENTS)
_persisted_event_types = {
    "connected", "disconnected", "boot_notification", "status_notification",
    "start_transaction", "stop_transaction", "remote_start", "firmware_status",
    "diagnostics_status", "cmd_received", "solar_throttle", "schedule",
}

# Backend-maintained charging history for 96h UI backfill
HISTORY_WINDOW_HOURS = _env_int("HISTORY_WINDOW_HOURS", 96)
HISTORY_RETENTION_HOURS = _env_int("HISTORY_RETENTION_HOURS", 192)
HISTORY_SAMPLE_SECONDS = _env_int("HISTORY_SAMPLE_SECONDS", 60)
_hourly_history: dict[str, dict] = {}
_dirty_hourly_history: set[str] = set()
_expired_history_docs: set[str] = set()
SCHEDULE_OCPP_TIMEOUT_SECONDS = float(_env_str("SCHEDULE_OCPP_TIMEOUT_SECONDS", "8"))

# Daily usage/cost graph settings
DAILY_WINDOW_DAYS = _env_int("DAILY_WINDOW_DAYS", 60)
DAILY_RETENTION_DAYS = _env_int("DAILY_RETENTION_DAYS", 120)
OFFPEAK_RATE = float(_env_str("OFFPEAK_RATE", "0.08"))
GENERAL_RATE = float(_env_str("GENERAL_RATE", "0.26763"))
SUMMER_DEMAND_RATE = float(_env_str("SUMMER_DEMAND_RATE", "0.19998"))
NON_SUMMER_DEMAND_RATE = float(_env_str("NON_SUMMER_DEMAND_RATE", "0.10197"))
FEED_IN_TARIFF = float(_env_str("FEED_IN_TARIFF", "0.03"))
SUMMER_MONTHS = {12, 1, 2}
ENERGY_TZ = ZoneInfo(_env_str("ENERGY_TZ", "Australia/Sydney"))
_daily_energy_history: dict[str, dict] = {}
_dirty_daily_energy_history: set[str] = set()
_last_esy_sample_at: datetime | None = None

# Per-charge-point state
_cp_state: dict[str, dict] = {}
_charge_sessions: dict[str, dict] = {}
_active_charge_sessions: dict[str, str] = {}
_docdb_write_lock = asyncio.Lock()
_last_transaction_id = int(time.time())

# MQTT client reference (set after connection)
_mqtt_client = None

# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def _cp_topic(cp_id: str, suffix: str) -> str:
    return f"ocpp/{cp_id}/{suffix}"

async def _mqtt_publish(topic: str, payload: dict):
    """Publish JSON to MQTT if connected."""
    global _mqtt_client
    if _mqtt_client:
        try:
            await _mqtt_client.publish(topic, json.dumps(payload).encode(), qos=1)
        except Exception as e:
            _LOGGER.error("MQTT publish failed for %s: %s", topic, e)

def _record_event(cp_id: str, event_type: str, summary: str = "", details=None,
                  persist=True):
    event = {
        "time": datetime.now(timezone.utc).isoformat(),
        "charge_point_id": cp_id,
        "type": event_type,
        "summary": summary,
    }
    if details:
        event["details"] = details
    _event_buffer.append(event)
    if cp_id not in _cp_state:
        _cp_state[cp_id] = {"id": cp_id, "connected": True, "status": "unknown",
                            "connector_id": None, "last_event": None,
                            "connectors": {},  # per-connector -> status
                            "meter_values": {}}  # per-connector -> {power, energy, timestamp}
    _cp_state[cp_id]["last_event"] = event["time"]
    if persist and DOCDB_ENABLED and event_type in _persisted_event_types:
        try:
            asyncio.get_running_loop().create_task(_docdb_save_event(event))
        except RuntimeError:
            _LOGGER.warning("Could not persist %s event outside the async loop", event_type)
    return event


def _hour_bucket_key(ts: datetime) -> str:
    slot = ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return slot.isoformat()


def _current_total_power_watts() -> float:
    total = 0.0
    for cp in _cp_state.values():
        mv = cp.get("meter_values", {})
        for conn_id, conn_mv in mv.items():
            if conn_id == "0":
                continue
            power = conn_mv.get("power")
            if isinstance(power, (int, float)):
                total += max(0.0, float(power))
    return total


def _fresh_charger_power_by_cp(ts: datetime, max_age_s: float = 300.0) -> dict[str, float]:
    """Per-charger power from meter values reported within max_age_s (ignores restored stale values)."""
    result: dict[str, float] = {}
    for cp_id, cp in _cp_state.items():
        total = 0.0
        for conn_id, conn_mv in cp.get("meter_values", {}).items():
            if conn_id == "0":
                continue
            power = conn_mv.get("power")
            try:
                age = (ts - datetime.fromisoformat(conn_mv.get("received_at"))).total_seconds()
            except Exception:
                continue
            if isinstance(power, (int, float)) and 0 <= age <= max_age_s:
                total += max(0.0, float(power))
        result[cp_id] = total
    return result


def _record_hourly_sample(ts: datetime | None = None):
    """Record one backend sample of aggregate charger power into an hourly bucket."""
    global _hourly_history
    now = ts or datetime.now(timezone.utc)
    bucket_key = _hour_bucket_key(now)
    bucket = _hourly_history.get(bucket_key, {"sum_kw": 0.0, "samples": 0})
    bucket["sum_kw"] += _current_total_power_watts() / 1000.0
    bucket["samples"] += 1
    for metric, output in (
        ("pv_power", "pv_kw"),
        ("grid_export", "grid_export_kw"),
        ("grid_import", "grid_import_kw"),
        ("load_power", "load_kw"),
    ):
        bucket[f"sum_{output}"] = bucket.get(f"sum_{output}", 0.0) + max(
            0.0, float(_solar_metrics.get(metric) or 0)
        ) / 1000.0
    _hourly_history[bucket_key] = bucket
    _dirty_hourly_history.add(bucket_key)

    cutoff = now - timedelta(hours=HISTORY_RETENTION_HOURS)
    for key in list(_hourly_history.keys()):
        try:
            if datetime.fromisoformat(key) < cutoff:
                del _hourly_history[key]
                _expired_history_docs.add(f"history:hourly:{key}")
        except Exception:
            del _hourly_history[key]
            _expired_history_docs.add(f"history:hourly:{key}")


def _hourly_history_for_debug(now: datetime) -> dict:
    samples = []
    base = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for offset in range(HISTORY_WINDOW_HOURS - 1, -1, -1):
        slot = base - timedelta(hours=offset)
        key = slot.isoformat()
        bucket = _hourly_history.get(key)
        count = bucket.get("samples", 0) if bucket else 0
        point = {"hour": key, "kw": 0.0, "samples": count}
        if count:
            point["kw"] = round(bucket.get("sum_kw", 0.0) / count, 3)
            for metric in ("pv_kw", "grid_export_kw", "grid_import_kw", "load_kw"):
                point[metric] = round(bucket.get(f"sum_{metric}", 0.0) / count, 3)
        samples.append(point)
    return {
        "window_hours": HISTORY_WINDOW_HOURS,
        "samples": samples,
    }


async def _hourly_history_loop():
    """Background sampler so history exists even before a UI client opens /debug."""
    while True:
        try:
            _record_hourly_sample(datetime.now(timezone.utc))
            _record_daily_energy_sample(datetime.now(timezone.utc))
            await _docdb_flush_graph_history()
        except Exception as e:
            _LOGGER.error("Hourly history sample error: %s", e)
        await asyncio.sleep(HISTORY_SAMPLE_SECONDS)


def _daily_key(ts: datetime) -> str:
    return ts.astimezone(ENERGY_TZ).strftime("%Y-%m-%d")


def _import_rate_for_time(ts: datetime, cp_id: str) -> float:
    cfg = _schedule_configs.get(cp_id) or DEFAULT_SCHEDULE
    local = ts.astimezone(ENERGY_TZ)
    start = cfg.get("off_peak_start_hour", 0)
    end = cfg.get("off_peak_end_hour", 6)
    in_off_peak = (start <= local.hour < end) if start < end else (local.hour >= start or local.hour < end)
    if in_off_peak:
        return float(cfg.get("off_peak_rate", OFFPEAK_RATE))
    key = "peak_rate_summer" if local.month in SUMMER_MONTHS else "peak_rate_other"
    return float(cfg.get(key, DEFAULT_SCHEDULE[key]))


def _record_daily_energy_sample(ts: datetime):
    """Integrate charger power over time into per-day energy and time-of-use cost buckets."""
    global _last_esy_sample_at
    if _last_esy_sample_at is None:
        _last_esy_sample_at = ts
        return

    dt_hours = (ts - _last_esy_sample_at).total_seconds() / 3600.0
    _last_esy_sample_at = ts
    if dt_hours <= 0:
        return

    dt_hours = min(dt_hours, 1.0)

    import_kwh = 0.0
    cost_delta = 0.0
    for cp_id, watts in _fresh_charger_power_by_cp(ts).items():
        kwh = watts * dt_hours / 1000.0
        import_kwh += kwh
        cost_delta += kwh * _import_rate_for_time(ts, cp_id)
    export_kwh = 0.0
    load_kwh = import_kwh

    day_key = _daily_key(ts)
    bucket = _daily_energy_history.get(day_key, {
        "import_kwh": 0.0,
        "export_kwh": 0.0,
        "load_kwh": 0.0,
        "net_kwh": 0.0,
        "cost": 0.0,
        "samples": 0,
    })
    bucket["import_kwh"] += import_kwh
    bucket["export_kwh"] += export_kwh
    bucket["load_kwh"] += load_kwh
    bucket["net_kwh"] += (import_kwh - export_kwh)
    bucket["cost"] += cost_delta
    bucket["samples"] += 1
    _daily_energy_history[day_key] = bucket
    _dirty_daily_energy_history.add(day_key)

    cutoff_day = (ts.astimezone(ENERGY_TZ) - timedelta(days=DAILY_RETENTION_DAYS)).date()
    for key in list(_daily_energy_history.keys()):
        try:
            if datetime.strptime(key, "%Y-%m-%d").date() < cutoff_day:
                del _daily_energy_history[key]
                _expired_history_docs.add(f"history:chargerdaily:{key}")
        except Exception:
            del _daily_energy_history[key]
            _expired_history_docs.add(f"history:chargerdaily:{key}")


def _daily_usage_60d_for_debug(now: datetime) -> dict:
    days = []
    window_load = 0.0
    window_cost = 0.0

    local_now = now.astimezone(ENERGY_TZ)
    for offset in range(DAILY_WINDOW_DAYS - 1, -1, -1):
        day = (local_now - timedelta(days=offset)).date()
        key = day.isoformat()
        bucket = _daily_energy_history.get(key, {})
        import_kwh = float(bucket.get("import_kwh", 0.0))
        export_kwh = float(bucket.get("export_kwh", 0.0))
        load_kwh = float(bucket.get("load_kwh", import_kwh))
        net_kwh = float(bucket.get("net_kwh", import_kwh - export_kwh))
        cost = float(bucket.get("cost", 0.0))
        day_price = (cost / load_kwh) if load_kwh > 0 else 0.0
        window_load += load_kwh
        window_cost += cost
        days.append({
            "date": key,
            "usage_kwh": round(import_kwh, 3),
            "load_kwh": round(load_kwh, 3),
            "export_kwh": round(export_kwh, 3),
            "net_kwh": round(net_kwh, 3),
            "cost": round(cost, 4),
            "samples": int(bucket.get("samples", 0)),
            "avg_price_per_kw": round(day_price, 5),
        })

    window_avg = (window_cost / window_load) if window_load > 0 else 0.0
    for row in days:
        row["avg_price_60d_per_kw"] = round(window_avg, 5)

    return {
        "window_days": DAILY_WINDOW_DAYS,
        "avg_price_per_kw": round(window_avg, 5),
        "days": days,
    }


# ---------------------------------------------------------------------------
# MQTT-tracking ChargePoint
# ---------------------------------------------------------------------------

def _parse_utc_time(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _charge_session_key(cp_id, connector_id):
    return f"{cp_id}:{connector_id}"


def _find_active_charge_session(cp_id, transaction_id=None):
    for session_id in _active_charge_sessions.values():
        session = _charge_sessions.get(session_id)
        if not session or session.get("charge_point_id") != cp_id:
            continue
        if transaction_id is None or session.get("transaction_id") == transaction_id:
            return session
    return None


def _get_or_create_charge_session(cp_id, connector_id, received_at, source):
    key = _charge_session_key(cp_id, connector_id)
    existing_id = _active_charge_sessions.get(key)
    existing = _charge_sessions.get(existing_id) if existing_id else None
    if existing and not existing.get("ended_at"):
        return existing

    session_id = uuid4().hex
    document_id = f"charge:{cp_id}:{session_id}"
    session = {
        "_id": document_id,
        "session_id": session_id,
        "charge_point_id": cp_id,
        "connector_id": int(connector_id),
        "state": "plugged",
        "health": "ok",
        "source": source,
        "plugged_at": received_at.isoformat(),
        "last_event_at": received_at.isoformat(),
        "ended_at": None,
        "transaction_id": None,
        "meter_start_wh": None,
        "last_energy_wh": None,
        "energy_delivered_wh": 0.0,
        "meter_resets": 0,
        "soc_start_percent": None,
        "soc_end_percent": None,
        "soc_min_percent": None,
        "soc_max_percent": None,
        "samples": [],
        "faults": [],
        "monitoring_gaps": [],
    }
    _charge_sessions[document_id] = session
    _active_charge_sessions[key] = document_id
    return session


async def _persist_charge_session(session):
    if not DOCDB_ENABLED:
        return
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not await _docdb_put_document(session):
        _LOGGER.error("Could not persist charge session %s", session.get("session_id"))


async def _mark_charge_point_reconnected(cp_id, reconnected_at):
    for session_id in tuple(_active_charge_sessions.values()):
        session = _charge_sessions.get(session_id)
        if not session or session.get("charge_point_id") != cp_id:
            continue
        open_gap = next((
            gap for gap in reversed(session.get("monitoring_gaps", []))
            if not gap.get("ended_at")
        ), None)
        if open_gap and close_monitoring_gap(session, reconnected_at):
            await _persist_charge_session(session)


def _allocate_transaction_id():
    global _last_transaction_id
    _last_transaction_id = max(int(time.time()), _last_transaction_id + 1)
    return _last_transaction_id


def _charge_session_summary(session):
    fields = (
        "session_id", "charge_point_id", "connector_id", "state", "health",
        "plugged_at", "transaction_started_at", "ended_at", "complete",
        "transaction_id", "meter_start_wh", "last_energy_wh", "meter_stop_wh",
        "energy_delivered_wh", "meter_resets", "stop_reason", "soc_start_percent",
        "soc_end_percent", "soc_min_percent", "soc_max_percent", "faults",
        "monitoring_gaps", "last_event_at", "last_history_sample_at",
    )
    result = {field: session.get(field) for field in fields}
    samples = session.get("samples", [])
    result["sample_count"] = len(samples)
    if samples:
        sample = samples[-1]
        result["last_sample"] = {
            key: sample.get(key)
            for key in ("received_at", "power_w", "energy_wh", "soc_percent")
        }
    return result


def _recent_charge_sessions(cp_id, limit=20):
    sessions = [
        session for session in _charge_sessions.values()
        if session.get("charge_point_id") == cp_id
    ]
    sessions.sort(key=lambda session: session.get("plugged_at", ""), reverse=True)
    return [_charge_session_summary(session) for session in sessions[:limit]]


class MqttChargePoint(BaseChargePoint):
    """
    OCPP v1.6 ChargePoint handler (Central System side).

    Each connected charge point gets its own instance. All OCPP actions
    received from the charge point are forwarded to MQTT.
    """

    def __init__(self, cp_id: str, connection):
        super().__init__(cp_id, connection)
        _record_event(cp_id, "connected", "Charge point connected")
        previous = _cp_state.get(cp_id) or {}
        _cp_state[cp_id] = {
            "id": cp_id, "connected": True,
            "status": previous.get("status", "unknown"),
            "connector_id": previous.get("connector_id"),
            "last_event": datetime.now(timezone.utc).isoformat(),
            "connectors": dict(previous.get("connectors") or {}),  # per-connector -> status
            "meter_values": dict(previous.get("meter_values") or {}),  # last known readings
        }
        _LOGGER.info("Charge point connected: %s", cp_id)

    # ---- OCPP message handlers ----

    @on("BootNotification")
    async def on_boot_notification(self, charge_point_vendor, charge_point_model,
                                   charge_point_serial_number=None, **kwargs):
        cp_id = self.id
        payload = {
            "charge_point_vendor": charge_point_vendor,
            "charge_point_model": charge_point_model,
            "charge_point_serial_number": charge_point_serial_number,
        }
        _LOGGER.info("BootNotification from %s: vendor=%s model=%s",
                     cp_id, charge_point_vendor, charge_point_model)
        _record_event(cp_id, "boot_notification",
                      f"vendor={charge_point_vendor} model={charge_point_model}")
        await _mqtt_publish(_cp_topic(cp_id, "boot_notification"), payload)
        return ocpp_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=300,
            status=RegistrationStatus.accepted,
        )

    async def _configure_meter_reporting(self):
        await asyncio.sleep(2)
        if _active_cps.get(self.id) is not self:
            return

        keys = ["MeterValueSampleInterval", "MeterValuesSampledData"]
        try:
            result = await self.call(GetConfiguration(key=keys))
        except Exception as e:
            _LOGGER.warning("Could not read meter reporting configuration for %s: %s", self.id, e)
            result = None

        if result is not None:
            configuration_keys = getattr(result, "configuration_key", []) or []
            supported = {
                item.get("key") if isinstance(item, dict) else getattr(item, "key", None)
                for item in configuration_keys
            }
            for key, value in (
                ("MeterValueSampleInterval", str(METER_VALUE_SAMPLE_INTERVAL)),
                ("MeterValuesSampledData", METER_VALUES_SAMPLED_DATA),
            ):
                if key not in supported:
                    _LOGGER.info("Charge point %s does not advertise %s", self.id, key)
                    continue
                try:
                    change_result = await self.call(ChangeConfiguration(key=key, value=value))
                    status = getattr(change_result, "status", "Unknown")
                    _LOGGER.info("Meter reporting configuration for %s: %s=%s status=%s",
                                 self.id, key, value, status)
                except Exception as e:
                    _LOGGER.warning("Could not set %s for %s: %s", key, self.id, e)

        try:
            trigger_result = await self.call(TriggerMessage(
                requested_message="MeterValues",
                connector_id=1,
            ))
            _LOGGER.info("Initial MeterValues trigger for %s: %s",
                         self.id, getattr(trigger_result, "status", "Unknown"))
        except Exception as e:
            _LOGGER.warning("Could not trigger initial MeterValues for %s: %s", self.id, e)

    @on("Heartbeat")
    async def on_heartbeat(self, **kwargs):
        _LOGGER.debug("Heartbeat from %s", self.id)
        _record_event(self.id, "heartbeat", "")
        await _mqtt_publish(_cp_topic(self.id, "heartbeat"), {})
        return ocpp_result.Heartbeat(
            current_time=datetime.now(timezone.utc).isoformat()
        )

    @on("StatusNotification")
    async def on_status_notification(self, connector_id, error_code, status,
                                      info=None, vendor_id=None, **kwargs):
        cp_id = self.id
        received_at = datetime.now(timezone.utc)
        summary = f"status={status}"
        if connector_id is not None:
            summary += f" connector={connector_id}"
        if error_code and error_code != "NoError":
            summary += f" error={error_code}"

        _LOGGER.info("StatusNotification from %s: %s", cp_id, summary)
        charger_timestamp = kwargs.get("timestamp")
        _record_event(cp_id, "status_notification", summary, {
            "connector_id": connector_id,
            "status": status,
            "error_code": error_code,
            "info": info,
            "vendor_id": vendor_id,
            "charger_timestamp": charger_timestamp,
            "received_at": received_at.isoformat(),
        })

        if cp_id in _cp_state:
            # Track per-connector status in a clean dict
            conn_key = str(connector_id) if connector_id is not None else "0"
            _cp_state[cp_id]["connectors"][conn_key] = status
            if connector_id is not None:
                _cp_state[cp_id]["connector_id"] = connector_id
            await _docdb_save_cp_state(cp_id, force=True)

        session = None
        try:
            physical_connector = int(connector_id) if connector_id is not None else 0
        except (TypeError, ValueError):
            physical_connector = 0
        if physical_connector > 0:
            if status == "Preparing":
                session = _get_or_create_charge_session(cp_id, physical_connector, received_at, "connector_status")
            elif status in {"Charging", "SuspendedEV", "SuspendedEVSE", "Finishing", "Faulted"}:
                session = _get_or_create_charge_session(cp_id, physical_connector, received_at, "connector_status")
            elif status == "Available":
                session = _find_active_charge_session(cp_id)
                if session and session.get("connector_id") == physical_connector:
                    session["ended_at"] = received_at.isoformat()
                    session["state"] = "completed" if session.get("transaction_started_at") else "unplugged"
                    session["complete"] = True
                    session["stop_reason"] = "connector_available"
                    _active_charge_sessions.pop(_charge_session_key(cp_id, physical_connector), None)

        if session:
            session["last_event_at"] = received_at.isoformat()
            session["last_connector_status"] = status
            session["charger_timestamp"] = charger_timestamp
            if status == "Charging":
                session["state"] = "charging"
            elif status in {"SuspendedEV", "SuspendedEVSE"}:
                session["state"] = "suspended"
            elif status == "Finishing":
                session["state"] = "finishing"
            elif status == "Faulted" or (error_code and error_code != "NoError"):
                session["health"] = "faulted"
                session.setdefault("faults", []).append({
                    "time": received_at.isoformat(),
                    "error_code": error_code,
                    "info": info,
                })
            await _persist_charge_session(session)

        # Car plugged in & ready — try to start if charging is allowed
        if status == "Preparing" and _is_charging_allowed(cp_id):
            _LOGGER.info("Car detected on %s — initiating RemoteStartTransaction", cp_id)
            asyncio.create_task(self._auto_start(cp_id))

        payload = {
            "connector_id": connector_id, "error_code": error_code,
            "status": status, "info": info, "vendor_id": vendor_id,
        }
        await _mqtt_publish(_cp_topic(cp_id, "status_notification"), payload)
        return ocpp_result.StatusNotification()

    async def _auto_start(self, cp_id: str):
        """Auto-start charging when a car is connected."""
        try:
            # Small delay to let the charger settle
            await asyncio.sleep(1)
            result = await self.call(RemoteStartTransaction(
                id_tag="0000003934", connector_id=1,
            ))
            _LOGGER.info("RemoteStartTransaction response for %s: %s", cp_id, result)
            _record_event(cp_id, "remote_start", f"status={getattr(result, 'status', result)}")
        except Exception as e:
            _LOGGER.warning("RemoteStartTransaction failed for %s: %s", cp_id, e)

    @on("Authorize")
    async def on_authorize(self, id_tag, **kwargs):
        _LOGGER.info("Authorize from %s: id_tag=%s", self.id, id_tag)
        _record_event(self.id, "authorize", f"id_tag={id_tag}")
        await _mqtt_publish(_cp_topic(self.id, "authorize"), {"id_tag": id_tag})
        if _is_charging_allowed(self.id):
            return ocpp_result.Authorize(
                id_tag_info={"status": AuthorizationStatus.accepted}
            )
        else:
            _LOGGER.info("Rejecting authorize for %s — scheduled off-peak", self.id)
            return ocpp_result.Authorize(
                id_tag_info={"status": AuthorizationStatus.invalid}
            )

    @on("StartTransaction")
    async def on_start_transaction(self, connector_id, id_tag, meter_start,
                                    timestamp=None, reservation_id=None, **kwargs):
        received_at = datetime.now(timezone.utc)
        transaction_id = _allocate_transaction_id()
        _tx_ids[self.id] = transaction_id
        _LOGGER.info("StartTransaction from %s: connector=%s meter_start=%s",
                     self.id, connector_id, meter_start)
        event = _record_event(self.id, "start_transaction", f"meter_start={meter_start}", {
            "connector_id": connector_id,
            "meter_start_wh": meter_start,
            "transaction_id": transaction_id,
            "charger_timestamp": timestamp,
            "received_at": received_at.isoformat(),
        }, persist=False)
        try:
            connector = int(connector_id)
        except (TypeError, ValueError):
            connector = 1
        session = _get_or_create_charge_session(self.id, connector, received_at, "start_transaction")
        meter_start_value = None
        try:
            meter_start_value = float(meter_start)
        except (TypeError, ValueError):
            pass
        if meter_start_value is not None and session.get("last_energy_wh") is None:
            session["meter_start_wh"] = meter_start_value
            session["last_energy_wh"] = meter_start_value
        session["transaction_id"] = transaction_id
        session["transaction_started_at"] = received_at.isoformat()
        session["charger_start_timestamp"] = timestamp
        session["state"] = "charging"
        session["last_event_at"] = received_at.isoformat()
        await _persist_charge_session(session)
        await _docdb_save_event(event)
        if self.id in _cp_state:
            _cp_state[self.id]["status"] = "Charging"

        payload = {
            "connector_id": connector_id, "id_tag": id_tag,
            "meter_start": meter_start, "timestamp": timestamp,
            "reservation_id": reservation_id, "transaction_id": transaction_id,
        }
        await _mqtt_publish(_cp_topic(self.id, "start_transaction"), payload)
        return ocpp_result.StartTransaction(
            transaction_id=transaction_id,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on("StopTransaction")
    async def on_stop_transaction(self, meter_stop, timestamp, transaction_id,
                                   reason=None, id_tag=None, transaction_data=None, **kwargs):
        received_at = datetime.now(timezone.utc)
        _LOGGER.info("StopTransaction from %s: meter_stop=%s reason=%s",
                     self.id, meter_stop, reason)
        session = _find_active_charge_session(self.id, transaction_id)
        if session is None:
            session = _find_active_charge_session(self.id)
        if _tx_ids.get(self.id) == transaction_id:
            _tx_ids.pop(self.id, None)
        if session and transaction_data:
            for sample in parse_meter_values(transaction_data):
                record_session_meter(
                    session, sample, received_at, METER_HISTORY_SAMPLE_SECONDS,
                    MAX_SESSION_SAMPLES,
                )
        if session:
            try:
                session["meter_stop_wh"] = float(meter_stop)
                add_energy_delta(session, meter_stop)
            except (TypeError, ValueError):
                session["meter_stop_wh"] = None
            session["ended_at"] = received_at.isoformat()
            session["state"] = "completed"
            session["complete"] = True
            session["stop_reason"] = reason
            session["charger_stop_timestamp"] = timestamp
            session["last_event_at"] = received_at.isoformat()
            _active_charge_sessions.pop(
                _charge_session_key(self.id, session.get("connector_id")), None
            )
        event = _record_event(self.id, "stop_transaction",
                              f"meter_stop={meter_stop} reason={reason}", {
            "transaction_id": transaction_id,
            "meter_stop_wh": meter_stop,
            "reason": reason,
            "charger_timestamp": timestamp,
            "received_at": received_at.isoformat(),
            "energy_delivered_wh": session.get("energy_delivered_wh") if session else None,
        }, persist=False)
        if session:
            await _persist_charge_session(session)
        await _docdb_save_event(event)
        if self.id in _cp_state:
            _cp_state[self.id]["status"] = "Available"

        payload = {
            "meter_stop": meter_stop, "timestamp": timestamp,
            "transaction_id": transaction_id, "reason": reason, "id_tag": id_tag,
            "energy_delivered_wh": session.get("energy_delivered_wh") if session else None,
        }
        await _mqtt_publish(_cp_topic(self.id, "stop_transaction"), payload)
        return ocpp_result.StopTransaction(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on("MeterValues")
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        _LOGGER.debug("MeterValues from %s: connector=%s", self.id, connector_id)
        received_at = datetime.now(timezone.utc)
        cp = _cp_state.get(self.id)
        conn_key = str(connector_id) if connector_id is not None else "0"
        session_id = _active_charge_sessions.get(_charge_session_key(self.id, conn_key))
        session = _charge_sessions.get(session_id) if session_id else None
        for sample in parse_meter_values(meter_value):
            sample_time = sample.get("timestamp") or received_at.isoformat()
            if cp:
                cp["meter_values"][conn_key] = {
                    "power": sample.get("power_w"),
                    "energy": sample.get("energy_wh"),
                    "soc_percent": sample.get("soc_percent"),
                    "current_a": sample.get("current_a"),
                    "voltage_v": sample.get("voltage_v"),
                    "timestamp": sample_time,
                    "received_at": received_at.isoformat(),
                }
            if session and record_session_meter(
                session, sample, received_at, METER_HISTORY_SAMPLE_SECONDS, MAX_SESSION_SAMPLES
            ):
                await _persist_charge_session(session)

        await _docdb_save_cp_state(self.id)
        payload = {"connector_id": connector_id, "meter_value": meter_value}
        await _mqtt_publish(_cp_topic(self.id, "meter_values"), payload)

        return ocpp_result.MeterValues()

    @on("DataTransfer")
    async def on_data_transfer(self, vendor_id, message_id=None, data=None, **kwargs):
        _LOGGER.info("DataTransfer from %s: vendor=%s msg=%s", self.id, vendor_id, message_id)
        _record_event(self.id, "data_transfer",
                      f"vendor={vendor_id} msg={message_id}")
        payload = {"vendor_id": vendor_id, "message_id": message_id, "data": data}
        await _mqtt_publish(_cp_topic(self.id, "data_transfer"), payload)
        return ocpp_result.DataTransfer(status="Accepted")

    @on("FirmwareStatusNotification")
    async def on_firmware_status_notification(self, status, **kwargs):
        _LOGGER.info("FirmwareStatus from %s: %s", self.id, status)
        _record_event(self.id, "firmware_status", f"status={status}")
        await _mqtt_publish(_cp_topic(self.id, "firmware_status"), {"status": status})
        return ocpp_result.FirmwareStatusNotification()

    @on("DiagnosticsStatusNotification")
    async def on_diagnostics_status_notification(self, status, **kwargs):
        _LOGGER.info("DiagnosticsStatus from %s: %s", self.id, status)
        _record_event(self.id, "diagnostics_status", f"status={status}")
        await _mqtt_publish(_cp_topic(self.id, "diagnostics_status"), {"status": status})
        return ocpp_result.DiagnosticsStatusNotification()

    # ---- Disconnection ----

    async def on_disconnect(self):
        cp_id = self.id
        _LOGGER.info("Charge point disconnected: %s", cp_id)
        _record_event(cp_id, "disconnected", "Charge point disconnected")
        if cp_id in _cp_state:
            _cp_state[cp_id]["connected"] = False
            _cp_state[cp_id]["status"] = "Unavailable"
        for session_id in _active_charge_sessions.values():
            session = _charge_sessions.get(session_id)
            if session and session.get("charge_point_id") == cp_id:
                open_monitoring_gap(
                    session, datetime.now(timezone.utc), "charge_point_disconnected"
                )
                await _persist_charge_session(session)
        await _mqtt_publish(_cp_topic(cp_id, "disconnected"), {})


# ---------------------------------------------------------------------------
# aiohttp WebSocket → ocpp library adapter
# ---------------------------------------------------------------------------

class _AiohttpWsAdapter:
    """
    Wraps aiohttp.web.WebSocketResponse to provide the websockets-like
    recv()/send()/close() API that the ocpp library's ChargePoint.start()
    expects.
    """

    def __init__(self, ws: web.WebSocketResponse):
        self._ws = ws

    async def recv(self) -> str:
        msg = await self._ws.receive()
        if msg.type == WSMsgType.TEXT:
            return msg.data
        elif msg.type == WSMsgType.BINARY:
            return msg.data.decode("utf-8")
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
            raise ConnectionError("WebSocket closed")
        elif msg.type == WSMsgType.ERROR:
            raise ConnectionError(f"WebSocket error: {self._ws.exception()}")
        else:
            _LOGGER.warning("Unexpected WS message type: %s, ignoring", msg.type)
            return await self.recv()

    async def send(self, data: str):
        await self._ws.send_str(data)

    async def close(self):
        await self._ws.close()


# ---------------------------------------------------------------------------
# OCPP WebSocket Server (aiohttp)
# ---------------------------------------------------------------------------

# Registry of active charge points (cp_id → MqttChargePoint)
_active_cps: dict[str, MqttChargePoint] = {}

async def ocpp_ws_handler(request: web.Request):
    """Handle an OCPP WebSocket connection from a charge point."""
    cp_id = request.match_info.get("cp_id", "unknown")

    # ── Basic auth check ──
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        _LOGGER.warning("Rejected %s — no Basic auth header", cp_id)
        return web.json_response({"error": "Authorization required"}, status=401)
    try:
        creds = base64.b64decode(auth_header[6:]).decode("utf-8")
        parts = creds.split(":", 1)
        password = parts[1] if len(parts) > 1 else ""
        if password != AUTH_KEY:
            _LOGGER.warning("Rejected %s — bad auth key (got %s)", cp_id, password[:20])
            return web.json_response({"error": "Invalid authorization"}, status=401)
    except Exception as e:
        _LOGGER.warning("Rejected %s — auth decode error: %s", cp_id, e)
        return web.json_response({"error": "Invalid authorization"}, status=401)

    _LOGGER.info("WS connect — cp_id=%s (authenticated)", cp_id)

    ws = web.WebSocketResponse(protocols=["ocpp1.6"])
    await ws.prepare(request)

    # Wrap aiohttp WS in adapter so ocpp library can use recv()/send()
    adapted = _AiohttpWsAdapter(ws)
    cp = MqttChargePoint(cp_id, adapted)
    _active_cps[cp_id] = cp
    await _mark_charge_point_reconnected(cp_id, datetime.now(timezone.utc))
    asyncio.create_task(cp._configure_meter_reporting())
    asyncio.create_task(_sync_profile_on_connect(cp_id, cp))

    try:
        await cp.start()
    except Exception as e:
        _LOGGER.error("ChargePoint %s error: %s", cp_id, e)
    finally:
        await cp.on_disconnect()
        _active_cps.pop(cp_id, None)

    return ws


# ---------------------------------------------------------------------------
# MQTT Command Listener
# ---------------------------------------------------------------------------

async def _handle_mqtt_command(cp_id: str, payload: bytes):
    """Process an MQTT command → OCPP call."""
    cp = _active_cps.get(cp_id)
    if not cp:
        _LOGGER.warning("Cannot send command — charge point %s not connected", cp_id)
        return

    try:
        msg = json.loads(payload)
        action = msg.get("action")
        params = msg.get("params", {})

        _LOGGER.info("MQTT → OCPP: %s → %s %s", cp_id, action, params)
        _record_event(cp_id, "cmd_received", f"action={action}")

        # All OCPP v1.6 Central System → Charge Point actions
        if action == "RemoteStartTransaction":
            result = await cp.call(RemoteStartTransaction(
                id_tag=params.get("id_tag", ""),
                connector_id=params.get("connector_id"),
            ))
        elif action == "RemoteStopTransaction":
            result = await cp.call(RemoteStopTransaction(
                transaction_id=params.get("transaction_id", 1),
            ))
        elif action == "Reset":
            result = await cp.call(Reset(
                type=params.get("type", "Soft"),
            ))
        elif action == "UnlockConnector":
            result = await cp.call(UnlockConnector(
                connector_id=params.get("connector_id", 0),
            ))
        elif action == "GetConfiguration":
            keys = params.get("key", [])
            result = await cp.call(GetConfiguration(key=keys))
        elif action == "ChangeConfiguration":
            result = await cp.call(ChangeConfiguration(
                key=params.get("key", ""),
                value=params.get("value", ""),
            ))
        elif action == "ClearCache":
            result = await cp.call(ClearCache())
        elif action == "TriggerMessage":
            result = await cp.call(TriggerMessage(
                requested_message=params.get("requested_message", ""),
                connector_id=params.get("connector_id"),
            ))
        elif action == "GetDiagnostics":
            result = await cp.call(GetDiagnostics(
                location=params.get("location", ""),
                retries=params.get("retries", 1),
                retry_interval=params.get("retry_interval", 60),
                start_time=params.get("start_time"),
                stop_time=params.get("stop_time"),
            ))
        elif action == "UpdateFirmware":
            result = await cp.call(UpdateFirmware(
                location=params.get("location", ""),
                retrieve_date=params.get("retrieve_date", datetime.now(timezone.utc).isoformat()),
                retries=params.get("retries", 1),
                retry_interval=params.get("retry_interval", 60),
            ))
        elif action == "ChangeAvailability":
            result = await cp.call(ChangeAvailability(
                connector_id=params.get("connector_id", 0),
                type=params.get("type", "Operative"),
            ))
        elif action == "GetLocalListVersion":
            result = await cp.call(GetLocalListVersion())
        elif action == "SendLocalList":
            result = await cp.call(SendLocalList(
                list_version=params.get("list_version", 0),
                update_type=params.get("update_type", "Full"),
                local_authorization_list=params.get("local_authorization_list", []),
            ))
        elif action == "SetChargingProfile":
            cs_profiles = params.get("cs_charging_profiles", params)
            result = await cp.call(SetChargingProfile(
                connector_id=params.get("connector_id", 0),
                cs_charging_profiles=cs_profiles,
            ))
        elif action == "ClearChargingProfile":
            result = await cp.call(ClearChargingProfile(
                id=params.get("id"),
                connector_id=params.get("connector_id", 0),
                charging_profile_purpose=params.get("charging_profile_purpose"),
                stack_level=params.get("stack_level"),
            ))
        else:
            _LOGGER.warning("Unknown MQTT command action: %s", action)
            return

        await _mqtt_publish(_cp_topic(cp_id, "cmd_result"), {
            "action": action, "result": str(result),
        })

    except json.JSONDecodeError as e:
        _LOGGER.error("Invalid JSON in MQTT command: %s", e)
    except Exception as e:
        _LOGGER.error("Error handling MQTT command for %s: %s", cp_id, e)


async def mqtt_listener():
    """Subscribe to MQTT command topics and forward to charge points."""
    global _mqtt_client
    _LOGGER.info("Starting MQTT listener...")

    mqtt_ctx = build_mqtt_context(MQTT_THING_NAME)
    async with mqtt_ctx as client:
        _mqtt_client = client
        await client.subscribe("ocpp/+/cmd")
        await client.subscribe(ESY_TELEMETRY_TOPIC)
        _LOGGER.info("Subscribed to MQTT topics: ocpp/+/cmd, %s", ESY_TELEMETRY_TOPIC)

        async for message in client.messages:
            topic = str(message.topic)
            parts = topic.split("/")
            if len(parts) >= 3 and parts[2] == "cmd":
                cp_id = parts[1]
                await _handle_mqtt_command(cp_id, message.payload)
            elif topic == ESY_TELEMETRY_TOPIC:
                try:
                    payload_str = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
                    data = json.loads(payload_str)
                    if "gridImport" in data:
                        _solar_metrics["grid_import"] = int(float(data["gridImport"]))
                    if "gridExport" in data:
                        _solar_metrics["grid_export"] = int(float(data["gridExport"]))
                    if "batterySoc" in data:
                        _solar_metrics["battery_soc"] = int(float(data["batterySoc"]))
                    if "loadPower" in data:
                        _solar_metrics["load_power"] = int(float(data["loadPower"]))
                    if "pvPower" in data:
                        _solar_metrics["pv_power"] = int(float(data["pvPower"]))
                    _solar_metrics["last_update"] = datetime.now(timezone.utc)
                    await _docdb_save_metrics()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Web UI / Debug API
# ---------------------------------------------------------------------------

async def handle_debug(request):
    """GET /debug — Full state for the React UI."""
    now = datetime.now(timezone.utc)
    uptime = (now - STARTED_AT).total_seconds()

    charge_points = list(_cp_state.values())
    # Compute best status across connectors
    STATUS_RANK = {"Charging": 5, "Preparing": 4, "SuspendedEV": 3, "SuspendedEVSE": 2, "Available": 1, "Faulted": 0, "Unavailable": 0}
    for cp in charge_points:
        best, best_conn = "unknown", None
        for conn_id, conn_status in cp.get("connectors", {}).items():
            if STATUS_RANK.get(conn_status, -1) > STATUS_RANK.get(best, -1):
                best, best_conn = conn_status, int(conn_id)
        cp["status"] = best
        cp["connector_id"] = best_conn if best_conn is not None else cp.get("connector_id")
        # Physical connector status (non-zero connectors — the actual cables)
        cp["physical_status"] = {k: v for k, v in cp.get("connectors", {}).items() if k != "0"}
    charge_points.sort(key=lambda cp: (
        not cp.get("connected", False),
        cp.get("last_event") or "",
    ))
    for cp in charge_points:
        cp["recent_charge_sessions"] = _recent_charge_sessions(cp["id"])

    recent_events = list(_event_buffer)
    recent_events.reverse()

    return web.json_response({
        "timestamp": now.isoformat(),
        "uptime_seconds": int(uptime),
        "started_at": STARTED_AT.isoformat(),
        "mqtt_broker": MQTT_BROKER_VAR,
        "mqtt_thing_name": MQTT_THING_NAME,
        "ocpp_port": OCPP_PORT,
        "ui_port": UI_PORT,
        "charge_points": charge_points,
        "recent_events": recent_events[:50],
        "solar_control": {
            "grid_import_threshold_w": AUTO_DEADBAND_W,
            "states": {
                cp_id: {
                    "target_watts": state.get("throttled_watts"),
                    "direction": state.get("direction"),
                    "level_a": state.get("level_a"),
                    "reason": state.get("reason"),
                }
                for cp_id, state in _solar_throttle.items()
            },
        },
        "solar_metrics": {
            "grid_import": _solar_metrics["grid_import"],
            "grid_export": _solar_metrics["grid_export"],
            "load_power": _solar_metrics["load_power"],
            "battery_soc": _solar_metrics["battery_soc"],
            "pv_power": _solar_metrics["pv_power"],
            "last_update": _solar_metrics["last_update"].isoformat() if _solar_metrics["last_update"] else None,
        },
        "solar_throttle": {k: v["throttled_watts"] for k, v in _solar_throttle.items()},
        "profile_checks": _profile_checks,
        "hourly_history": _hourly_history_for_debug(now),
        "daily_usage_60d": _daily_usage_60d_for_debug(now),
        "grid_tariff": {
            **{k: (_schedule_configs.get(next(iter(_cp_state), ""), DEFAULT_SCHEDULE)).get(k, DEFAULT_SCHEDULE[k])
               for k in ("off_peak_rate", "peak_rate_summer", "peak_rate_other",
                         "off_peak_start_hour", "off_peak_end_hour")},
            "timezone": str(ENERGY_TZ),
        },
    })


async def handle_health(request):
    return web.json_response({"status": "ok"})


async def handle_index(request):
    ui_dist = os.path.join(os.path.dirname(__file__), "ui", "dist")
    index_path = os.path.join(ui_dist, "index.html")
    if os.path.exists(index_path):
        return web.FileResponse(index_path)
    return await handle_debug(request)


# ---------------------------------------------------------------------------
# Schedule API
# ---------------------------------------------------------------------------

_schedule_state: dict[str, dict] = {}
# Track active transaction IDs per CP for RemoteStopTransaction
_tx_ids: dict[str, int] = {}

# Solar Smart state — populated from ESY sunhomes MQTT telemetry
_solar_metrics = {
    "grid_import": 0,     # Watts importing from grid
    "grid_export": 0,     # Watts exporting to grid
    "load_power": 0,      # Watts consumed by load (any source)
    "battery_soc": None,  # Battery % (None = unknown)
    "pv_power": 0,        # Solar generation Watts
    "last_update": None,  # datetime of last ESY telemetry
}

# Solar Smart per-CP throttle state
# {cp_id: {"throttled_watts": float, "direction": "down"|"up"|None, "consecutive": int}}
_solar_throttle: dict[str, dict] = {}
_auto_off_peak_start_attempts: dict[str, str] = {}

# ESY sunhomes MQTT thing name (must match cloudformation)
ESY_THING_NAME = _env_str("ESY_THING_NAME", "gormantec-battery1")
ESY_TELEMETRY_TOPIC = f"$iothub/twin/PATCH/properties/reported/{ESY_THING_NAME}"

# Solar Smart constants
SOLAR_CHECK_INTERVAL = 30            # Seconds between throttle checks

# Per-CP schedule config (persisted to DocumentDB)
# {cp_id: {mode, peak_start_hour, peak_end_hour, peak_watts, off_peak_watts}}
_schedule_configs: dict[str, dict] = {}

# Per-charger settings. Auto mode picks OFF/6/8/16/32A from site power;
# grid import is only allowed inside the off-peak window.
DEFAULT_SCHEDULE = {
    "mode": "charge_now",
    "timezone": "Australia/Sydney",
    "off_peak_start_hour": 0,
    "off_peak_end_hour": 6,
    "max_amps": 16,          # cap for Auto (off-peak and solar)
    "min_battery_soc": 50,   # Auto outside off-peak only runs at/above this %
    "grid_deadband_w": 150,
    # All-in import prices in $/kWh, used for the charging cost estimate
    "off_peak_rate": OFFPEAK_RATE,
    "peak_rate_summer": round(GENERAL_RATE + SUMMER_DEMAND_RATE, 5),
    "peak_rate_other": round(GENERAL_RATE + NON_SUMMER_DEMAND_RATE, 5),
}
AUTO_DEADBAND_W = 150

# Common timezones for UI dropdown
COMMON_TIMEZONES = sorted([
    "Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane",
    "Australia/Perth", "Australia/Adelaide", "Australia/Darwin",
    "Pacific/Auckland", "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore",
    "Asia/Kolkata", "Asia/Dubai", "Europe/London", "Europe/Paris",
    "Europe/Berlin", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "UTC",
])


def _get_tz(cp_id: str) -> ZoneInfo:
    """Get the timezone for a charge point, with error fallback."""
    tz_name = _get_schedule(cp_id).get("timezone", "Australia/Sydney")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# DocumentDB / CouchDB persistence (via aiohttp)
# ---------------------------------------------------------------------------

async def _docdb_request(method: str, path: str, body: dict = None):
    """Make a CouchDB REST API call. Returns (ok: bool, data: dict)."""
    if not DOCDB_ENABLED:
        return False, {}
    url = f"{DOCDB_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        import aiohttp as _aiohttp
        headers = None
        if DOCDB_USER:
            from aiohttp import encode_basic_auth
            headers = {
                "Authorization": encode_basic_auth(DOCDB_USER, DOCDB_PASSWORD),
            }
        async with _aiohttp.ClientSession(headers=headers) as sess:
            if body is not None:
                async with sess.request(method, url, json=body) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        data = {"_raw": text}
                    return resp.status < 400, data
            else:
                async with sess.request(method, url) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        data = {"_raw": text}
                    return resp.status < 400, data
    except Exception as e:
        _LOGGER.warning("DocDB request failed (%s %s): %s", method, path, e)
        return False, {}


def _docdb_document_path(doc_id: str) -> str:
    return f"{quote(DOCDB_DB, safe='')}/{quote(doc_id, safe='')}"


async def _docdb_put_document(document: dict) -> bool:
    if not DOCDB_ENABLED or not document.get("_id"):
        return False

    path = _docdb_document_path(document["_id"])
    async with _docdb_write_lock:
        for _ in range(3):
            found, current = await _docdb_request("GET", path)
            if not found and current.get("status") != 404:
                return False
            payload = {key: value for key, value in document.items() if key != "_rev"}
            if found and current.get("_rev"):
                payload["_rev"] = current["_rev"]
            ok, result = await _docdb_request("PUT", path, payload)
            if ok:
                return True
            if result.get("status") != 409:
                _LOGGER.warning("Could not persist DocumentDB document %s", document["_id"])
                return False
    _LOGGER.warning("DocumentDB revision conflict for %s", document["_id"])
    return False


async def _docdb_delete_document(doc_id: str) -> bool:
    path = _docdb_document_path(doc_id)
    async with _docdb_write_lock:
        found, current = await _docdb_request("GET", path)
        if not found:
            return current.get("status") == 404
        revision = quote(str(current.get("_rev") or ""), safe="-")
        ok, _ = await _docdb_request("DELETE", f"{path}?rev={revision}")
        return ok


async def _docdb_save_event(event: dict):
    if not DOCDB_ENABLED:
        return

    path = _docdb_document_path("history:events")
    async with _docdb_write_lock:
        for _ in range(3):
            found, current = await _docdb_request("GET", path)
            if not found and current.get("status") != 404:
                return
            events = list(current.get("events", [])) if found else []
            events.append(event)
            document = {
                "_id": "history:events",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "events": events[-MAX_PERSISTED_EVENTS:],
            }
            if found and current.get("_rev"):
                document["_rev"] = current["_rev"]
            ok, result = await _docdb_request("PUT", path, document)
            if ok:
                return
            if result.get("status") != 409:
                _LOGGER.warning("Could not persist recent OCPP event")
                return


async def _docdb_flush_graph_history():
    if not DOCDB_ENABLED:
        return
    for doc_id in tuple(_expired_history_docs):
        if await _docdb_delete_document(doc_id):
            _expired_history_docs.discard(doc_id)

    for key in tuple(_dirty_hourly_history):
        bucket = _hourly_history.get(key)
        if not bucket:
            _dirty_hourly_history.discard(key)
            continue
        snapshot = dict(bucket)
        if await _docdb_put_document({"_id": f"history:hourly:{key}", "hour": key, **snapshot}):
            if _hourly_history.get(key) == snapshot:
                _dirty_hourly_history.discard(key)

    for key in tuple(_dirty_daily_energy_history):
        bucket = _daily_energy_history.get(key)
        if not bucket:
            _dirty_daily_energy_history.discard(key)
            continue
        snapshot = dict(bucket)
        if await _docdb_put_document({"_id": f"history:chargerdaily:{key}", "date": key, **snapshot}):
            if _daily_energy_history.get(key) == snapshot:
                _dirty_daily_energy_history.discard(key)


async def _docdb_load_history():
    if not DOCDB_ENABLED:
        return
    path = f"{quote(DOCDB_DB, safe='')}/_all_docs?include_docs=true&limit=10000"
    ok, data = await _docdb_request("GET", path)
    if not ok:
        _LOGGER.warning("Could not restore persisted OCPP history")
        return

    now = datetime.now(timezone.utc)
    hourly_cutoff = now - timedelta(hours=HISTORY_RETENTION_HOURS)
    daily_cutoff = (now.astimezone(ENERGY_TZ) - timedelta(days=DAILY_RETENTION_DAYS)).date()
    session_cutoff = now - timedelta(days=CHARGE_HISTORY_RETENTION_DAYS)
    expired = []
    restored_hourly = restored_daily = 0
    global _last_transaction_id
    for row in data.get("rows", []):
        document = row.get("doc") or {}
        doc_id = document.get("_id", row.get("id", ""))
        if doc_id.startswith("history:hourly:"):
            key = document.get("hour", doc_id.removeprefix("history:hourly:"))
            try:
                slot = datetime.fromisoformat(key.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                expired.append(doc_id)
                continue
            if slot < hourly_cutoff:
                expired.append(doc_id)
            else:
                _hourly_history[key] = {
                    key: document.get(key, 0.0)
                    for key in ("sum_kw", "sum_pv_kw", "sum_grid_export_kw", "sum_grid_import_kw", "sum_load_kw", "samples")
                }
                restored_hourly += 1
        elif doc_id.startswith("history:chargerdaily:"):
            key = document.get("date", doc_id.removeprefix("history:chargerdaily:"))
            try:
                day = datetime.strptime(key, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                expired.append(doc_id)
                continue
            if day < daily_cutoff:
                expired.append(doc_id)
            else:
                _daily_energy_history[key] = {
                    name: document.get(name, 0.0)
                    for name in ("import_kwh", "export_kwh", "load_kwh", "net_kwh", "cost", "samples")
                }
                restored_daily += 1
        elif doc_id == "history:events":
            events = sorted(document.get("events", []), key=lambda item: item.get("time", ""))
            _event_buffer.extend(events[-MAX_EVENTS:])
        elif doc_id.startswith("charge:"):
            plugged_at = _parse_utc_time(document.get("plugged_at"))
            if plugged_at and plugged_at < session_cutoff and document.get("ended_at"):
                expired.append(doc_id)
                continue
            _charge_sessions[doc_id] = document
            try:
                _last_transaction_id = max(
                    _last_transaction_id, int(document.get("transaction_id") or 0)
                )
            except (TypeError, ValueError):
                pass
            if not document.get("ended_at"):
                open_monitoring_gap(document, now, "bridge_restarted")
                key = _charge_session_key(
                    document.get("charge_point_id"), document.get("connector_id")
                )
                _active_charge_sessions[key] = doc_id
                await _persist_charge_session(document)

    for doc_id in expired:
        await _docdb_delete_document(doc_id)
    _LOGGER.info("Restored OCPP graph history: %d hourly and %d daily buckets", restored_hourly, restored_daily)


async def _docdb_ensure_db():
    """Ensure the schedules database exists (idempotent)."""
    if not DOCDB_ENABLED:
        return
    ok, _ = await _docdb_request("PUT", DOCDB_DB)
    if ok:
        _LOGGER.info("DocDB database '%s' ready", DOCDB_DB)
    else:
        _LOGGER.info("DocDB database '%s' already exists", DOCDB_DB)


async def _docdb_save_schedule(cp_id: str):
    """Persist a charge point's schedule config to DocumentDB."""
    if not DOCDB_ENABLED:
        return
    config = _schedule_configs.get(cp_id, DEFAULT_SCHEDULE)
    doc = {"_id": _docdb_key(cp_id, "schedule"), "cp_id": cp_id, **config}
    ok, _ = await _docdb_request("PUT", f"{DOCDB_DB}/{doc['_id']}", doc)
    if ok:
        _LOGGER.info("Saved schedule for %s to DocDB: mode=%s", cp_id, config.get("mode"))


_metrics_last_saved = 0.0
METRICS_SAVE_INTERVAL_S = 30


async def _docdb_save_metrics(force: bool = False):
    """Persist the last known site metrics (throttled) so restarts keep them."""
    global _metrics_last_saved
    if not DOCDB_ENABLED or not _solar_metrics.get("last_update"):
        return
    now = time.monotonic()
    if not force and now - _metrics_last_saved < METRICS_SAVE_INTERVAL_S:
        return
    _metrics_last_saved = now
    doc = {"_id": "site:metrics", **{k: v for k, v in _solar_metrics.items() if k != "last_update"},
           "last_update": _solar_metrics["last_update"].isoformat()}
    await _docdb_put_document(doc)


_cp_state_last_saved: dict = {}


async def _docdb_save_cp_state(cp_id: str, force: bool = False):
    """Persist last known connector status and meter readings (throttled)."""
    cp = _cp_state.get(cp_id)
    if not DOCDB_ENABLED or not cp:
        return
    now = time.monotonic()
    if not force and now - _cp_state_last_saved.get(cp_id, 0.0) < METRICS_SAVE_INTERVAL_S:
        return
    _cp_state_last_saved[cp_id] = now
    await _docdb_put_document({
        "_id": f"cpstate:{cp_id}",
        "cp_id": cp_id,
        "status": cp.get("status"),
        "connector_id": cp.get("connector_id"),
        "connectors": cp.get("connectors") or {},
        "meter_values": cp.get("meter_values") or {},
        "last_event": cp.get("last_event"),
    })


async def _docdb_load_cp_states():
    """Restore last known charger state so tiles are not blank after a restart."""
    if not DOCDB_ENABLED:
        return
    ok, data = await _docdb_request("GET", f"{DOCDB_DB}/_all_docs?include_docs=true")
    if not ok:
        return
    for row in data.get("rows", []):
        doc = row.get("doc") or {}
        if not str(doc.get("_id", "")).startswith("cpstate:") or not doc.get("cp_id"):
            continue
        _cp_state.setdefault(doc["cp_id"], {
            "id": doc["cp_id"], "connected": False,
            "status": doc.get("status") or "unknown",
            "connector_id": doc.get("connector_id"),
            "last_event": doc.get("last_event"),
            "connectors": doc.get("connectors") or {},
            "meter_values": doc.get("meter_values") or {},
        })
        _LOGGER.info("Restored charger state for %s from DocDB", doc["cp_id"])


async def _docdb_load_metrics():
    """Restore last known site metrics; the original timestamp is kept so age stays honest."""
    if not DOCDB_ENABLED:
        return
    ok, doc = await _docdb_request("GET", _docdb_document_path("site:metrics"))
    if not ok or not isinstance(doc, dict) or not doc.get("last_update"):
        return
    try:
        last = datetime.fromisoformat(doc["last_update"])
        if _solar_metrics.get("last_update") and _solar_metrics["last_update"] > last:
            return
        for k in ("grid_import", "grid_export", "load_power", "battery_soc", "pv_power"):
            if k in doc:
                _solar_metrics[k] = doc[k]
        _solar_metrics["last_update"] = last
        _LOGGER.info("Restored site metrics from DocDB (last_update=%s)", doc["last_update"])
    except Exception:
        _LOGGER.warning("Could not restore site metrics", exc_info=True)


async def _docdb_load_schedules():
    """Load all persisted schedule configs from DocumentDB."""
    if not DOCDB_ENABLED:
        return
    ok, data = await _docdb_request("GET", f"{DOCDB_DB}/_all_docs?include_docs=true")
    if not ok:
        _LOGGER.warning("Failed to load schedules from DocDB")
        return
    rows = data.get("rows", [])
    loaded = 0
    for row in rows:
        doc = row.get("doc", {})
        doc_id = doc.get("_id", "")
        if doc_id.startswith("_"):
            continue
        # Only load schedule docs
        if not doc_id.endswith(":schedule"):
            continue
        cp_id = doc.get("cp_id", "")
        if not cp_id:
            continue
        config = {k: v for k, v in doc.items() if not k.startswith("_") and k != "cp_id"}
        if "mode" in config:
            _schedule_configs[cp_id] = {**DEFAULT_SCHEDULE, **config}
            loaded += 1
    if loaded:
        _LOGGER.info("Loaded %d schedule config(s) from DocDB", loaded)
    else:
        _LOGGER.info("No existing schedule configs in DocDB")


def _get_schedule(cp_id: str) -> dict:
    """Get schedule config for a charge point (defaults if unknown)."""
    return _schedule_configs.get(cp_id, dict(DEFAULT_SCHEDULE))


# ---------------------------------------------------------------------------
# Solar Smart throttling
# ---------------------------------------------------------------------------

def _is_off_peak(cp_id: str) -> bool:
    """Check if we're in the off-peak grid window for this CP."""
    config = _get_schedule(cp_id)
    tz = _get_tz(cp_id)
    hour = datetime.now(tz).hour
    start = config.get("off_peak_start_hour", 0)
    end = config.get("off_peak_end_hour", 6)
    if start <= end:
        return start <= hour < end
    else:
        return hour >= start or hour < end


def _off_peak_window_key(cp_id: str, now=None):
    config = _get_schedule(cp_id)
    tz = _get_tz(cp_id)
    now = (now or datetime.now(tz)).astimezone(tz)
    start = config.get("off_peak_start_hour", 0)
    end = config.get("off_peak_end_hour", 6)
    if start == end:
        return None

    if start < end:
        if not start <= now.hour < end:
            return None
        window_date = now.date()
    elif now.hour >= start:
        window_date = now.date()
    elif now.hour < end:
        window_date = now.date() - timedelta(days=1)
    else:
        return None

    return f"{window_date.isoformat()}:{start:02d}-{end:02d}"


async def _try_auto_off_peak_start(cp_id: str, cp, window_key: str):
    if _auto_off_peak_start_attempts.get(cp_id) == window_key:
        return

    connector_status = _cp_state.get(cp_id, {}).get("connectors", {}).get("1")
    session = _find_active_charge_session(cp_id)
    if connector_status != "SuspendedEV":
        return
    if cp_id in _tx_ids or (session and session.get("transaction_id") is not None):
        return

    _auto_off_peak_start_attempts[cp_id] = window_key
    try:
        result = await cp.call(RemoteStartTransaction(
            id_tag="0000003934", connector_id=1,
        ))
        status = getattr(result, "status", str(result))
        _record_event(cp_id, "remote_start", f"status={status}", {
            "status": status,
            "source": "auto_off_peak",
            "window": window_key,
        })
        _LOGGER.info("AUTO off-peak RemoteStartTransaction for %s: %s", cp_id, status)
    except Exception as e:
        _record_event(cp_id, "remote_start", f"status=error: {e}", {
            "status": "error",
            "source": "auto_off_peak",
            "window": window_key,
        })
        _LOGGER.warning("AUTO off-peak RemoteStartTransaction failed for %s: %s", cp_id, e)


# ---------------------------------------------------------------------------
# Charging profile management + verification
# ---------------------------------------------------------------------------

PROFILE_ID = 1                      # Single profile id used by every mode
CHARGE_NOW_WATTS = 4800.0
DEFAULT_VOLTAGE = 230.0
VERIFY_DELAY_SECONDS = 4
WATCHDOG_INTERVAL_SECONDS = 120
HEAL_COOLDOWN_SECONDS = 600
LIMIT_TOLERANCE_W = 100.0

# {cp_id: last verification result}
_profile_checks: dict[str, dict] = {}
_last_heal: dict[str, float] = {}
_last_current_alert: dict[str, float] = {}


def _result_get(obj, *names):
    """Read a field from a dataclass or dict, accepting snake/camel names."""
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return None


async def _clear_all_profiles(cp, call=None) -> list[str]:
    """Remove every stored charging profile (not just id=1 / TxDefault stack 0)."""
    call = call or (lambda _label, obj: cp.call(obj))
    statuses = []
    res = await call("ClearChargingProfile(all)", ClearChargingProfile())
    statuses.append(str(getattr(res, "status", res)))
    for purpose in ("TxDefaultProfile", "TxProfile", "ChargePointMaxProfile"):
        res = await call(f"ClearChargingProfile({purpose})",
                         ClearChargingProfile(charging_profile_purpose=purpose))
        statuses.append(f"{purpose}={getattr(res, 'status', res)}")
    return statuses


def _expected_limit_w(cp_id: str) -> float | None:
    config = _get_schedule(cp_id)
    mode = config.get("mode", "charge_now")
    if mode == "stop":
        return 0.0
    if mode == "charge_now":
        return CHARGE_NOW_WATTS
    st = _solar_throttle.get(cp_id)
    return float(st["throttled_watts"]) if st and st.get("initialised") else None


def _measured_voltage(cp_id: str) -> float:
    mv = (_cp_state.get(cp_id, {}).get("meter_values", {}) or {}).get("1") or {}
    try:
        v = float(mv.get("voltage_v"))
        if 180 <= v <= 280:
            return v
    except (TypeError, ValueError):
        pass
    return DEFAULT_VOLTAGE


async def _reapply_mode_profile(cp_id: str, cp, reason: str):
    """Clear all stored profiles, then push the profile for the current mode."""
    from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
    config = _get_schedule(cp_id)
    mode = config.get("mode", "charge_now")
    if mode == "stop":
        periods = [(0, 0.0)]
    elif mode == "charge_now":
        periods = [(0, CHARGE_NOW_WATTS)]
    else:
        _solar_throttle.pop(cp_id, None)
        periods = [(0, _auto_decide(cp_id)[0])]
    clear = await _clear_all_profiles(cp)
    kwargs = {}
    if mode == "auto":
        kind = ChargingProfileKindType.recurring
        kwargs["recurrency_kind"] = RecurrencyKind.daily
    else:
        kind = ChargingProfileKindType.relative
    result = await cp.call(SetChargingProfile(
        connector_id=0,
        cs_charging_profiles=ChargingProfile(
            charging_profile_id=PROFILE_ID, stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
            charging_profile_kind=kind,
            charging_schedule=ChargingSchedule(
                charging_rate_unit="W",
                charging_schedule_period=[
                    ChargingSchedulePeriod(start_period=s, limit=l) for s, l in periods
                ],
            ),
            **kwargs,
        ),
    ))
    status = str(getattr(result, "status", result))
    if mode != "auto":
        _solar_throttle.pop(cp_id, None)
    _record_event(cp_id, "profile_sync", f"{reason}: mode={mode} clear={clear} set={status}")
    _LOGGER.info("Profile sync for %s (%s): mode=%s clear=%s set=%s",
                 cp_id, reason, mode, clear, status)
    return status


async def _verify_profile(cp_id: str, cp=None, reason: str = "check") -> dict:
    """Ask the charger for its composite schedule and compare with what we expect."""
    cp = cp or _active_cps.get(cp_id)
    expected_w = _expected_limit_w(cp_id)
    voltage = _measured_voltage(cp_id)
    check = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "mode": _get_schedule(cp_id).get("mode", "charge_now"),
        "expected_w": expected_w,
        "expected_a": round(expected_w / voltage, 1) if expected_w is not None else None,
        "voltage_v": voltage,
        "actual_w": None,
        "actual_a": None,
        "unit": None,
        "periods": None,
        "status": None,
        "ok": None,
    }
    if not cp:
        check["status"] = "not_connected"
        _profile_checks[cp_id] = check
        return check
    try:
        res = await asyncio.wait_for(cp.call(GetCompositeSchedule(
            connector_id=1, duration=3600, charging_rate_unit="W",
        )), timeout=SCHEDULE_OCPP_TIMEOUT_SECONDS)
        check["status"] = str(_result_get(res, "status"))
        schedule = _result_get(res, "charging_schedule", "chargingSchedule")
        periods = _result_get(schedule, "charging_schedule_period", "chargingSchedulePeriod") if schedule else None
        unit = _result_get(schedule, "charging_rate_unit", "chargingRateUnit") if schedule else None
        unit = getattr(unit, "value", unit)
        if periods:
            check["periods"] = [
                {"start": _result_get(p, "start_period", "startPeriod"), "limit": _result_get(p, "limit")}
                for p in periods
            ]
            first = float(check["periods"][0]["limit"])
            check["unit"] = unit
            if unit == "A":
                check["actual_a"] = first
                check["actual_w"] = round(first * voltage)
            else:
                check["actual_w"] = first
                check["actual_a"] = round(first / voltage, 1)
            if expected_w is not None:
                check["ok"] = abs(check["actual_w"] - expected_w) <= max(LIMIT_TOLERANCE_W, expected_w * 0.05)
        elif check["status"] == "Accepted":
            check["ok"] = False  # charger accepted but reports no schedule at all
    except Exception as e:
        check["status"] = f"error: {e}"

    mv = (_cp_state.get(cp_id, {}).get("meter_values", {}) or {}).get("1") or {}
    check["measured_a"] = mv.get("current_a")
    check["measured_w"] = mv.get("power")

    _profile_checks[cp_id] = check
    level = "OK" if check["ok"] else "MISMATCH" if check["ok"] is False else "UNKNOWN"
    _record_event(cp_id, "profile_check",
                  f"{level} expected={expected_w}W/{check['expected_a']}A "
                  f"charger={check['actual_w']}W/{check['actual_a']}A ({reason})",
                  check)
    if check["ok"] is False:
        _LOGGER.warning("Profile MISMATCH on %s: expected %sW charger reports %sW (%s)",
                        cp_id, expected_w, check["actual_w"], reason)
    return check


async def _verify_profile_later(cp_id: str, reason: str):
    await asyncio.sleep(VERIFY_DELAY_SECONDS)
    await _verify_profile(cp_id, reason=reason)


async def _profile_watchdog_tick():
    for cp_id, cp in list(_active_cps.items()):
        check = await _verify_profile(cp_id, cp, reason="watchdog")
        now = time.time()
        if check["ok"] is False and now - _last_heal.get(cp_id, 0) > HEAL_COOLDOWN_SECONDS:
            _last_heal[cp_id] = now
            try:
                await _reapply_mode_profile(cp_id, cp, "self-heal after mismatch")
                await asyncio.sleep(VERIFY_DELAY_SECONDS)
                await _verify_profile(cp_id, cp, reason="post-heal")
            except Exception as e:
                _LOGGER.warning("Self-heal failed for %s: %s", cp_id, e)

        # Actual current vs commanded: informational (the car may legitimately draw less)
        status = _cp_state.get(cp_id, {}).get("connectors", {}).get("1")
        exp_a, meas_a = check.get("expected_a"), check.get("measured_a")
        if status == "Charging" and exp_a and meas_a is not None:
            try:
                meas_a = float(meas_a)
            except (TypeError, ValueError):
                continue
            if meas_a < exp_a * 0.8 and now - _last_current_alert.get(cp_id, 0) > 900:
                _last_current_alert[cp_id] = now
                _record_event(cp_id, "current_mismatch",
                              f"charging at {meas_a:.1f}A but expected up to {exp_a:.1f}A "
                              f"(charger reports limit {check.get('actual_a')}A)", check)


async def _profile_watchdog_loop():
    await asyncio.sleep(60)
    while True:
        try:
            await _profile_watchdog_tick()
        except Exception as e:
            _LOGGER.error("Profile watchdog error: %s", e)
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)


async def _sync_profile_on_connect(cp_id: str, cp):
    """A (re)connected charger may hold stale profiles: wipe and push the current mode."""
    await asyncio.sleep(5)
    if _active_cps.get(cp_id) is not cp:
        return
    try:
        await _reapply_mode_profile(cp_id, cp, "charger connected")
        await _verify_profile_later(cp_id, "post-connect")
    except Exception as e:
        _LOGGER.warning("Profile sync on connect failed for %s: %s", cp_id, e)


async def handle_profile_check(request):
    """GET /profile-check/{cp_id}[?fix=1] — live GetCompositeSchedule + smart-charging config."""
    cp_id = request.match_info.get("cp_id", "")
    cp = _active_cps.get(cp_id)
    if not cp:
        return web.json_response({"error": f"Charge point {cp_id} not connected"}, status=404)
    out = {}
    if request.query.get("fix"):
        try:
            out["resync"] = await _reapply_mode_profile(cp_id, cp, "manual fix")
            await asyncio.sleep(VERIFY_DELAY_SECONDS)
        except Exception as e:
            out["resync"] = f"error: {e}"
    out["check"] = await _verify_profile(cp_id, cp, reason="manual")
    try:
        res = await asyncio.wait_for(cp.call(GetConfiguration(key=[
            "ChargeProfileMaxStackLevel", "ChargingScheduleAllowedChargingRateUnit",
            "ChargingScheduleMaxPeriods", "MaxChargingProfilesInstalled",
            "ConnectorSwitch3to1PhaseSupported", "SupportedFeatureProfiles",
        ])), timeout=SCHEDULE_OCPP_TIMEOUT_SECONDS)
        out["configuration"] = {
            _result_get(i, "key"): _result_get(i, "value")
            for i in (_result_get(res, "configuration_key", "configurationKey") or [])
        }
    except Exception as e:
        out["configuration"] = f"error: {e}"
    return web.json_response(out)


AUTO_LEVELS_A = (6, 8, 16, 32)   # OFF (0) plus these current levels
AUTO_DOWN_CHECKS = 2             # 60s of grid import before stepping down
AUTO_UP_CHECKS = 6               # 3min of surplus before stepping up
AUTO_METRICS_MAX_AGE_S = 180


def _snap_level(amps: float) -> int:
    """Highest allowed current level <= amps (0 = OFF)."""
    best = 0
    for lvl in AUTO_LEVELS_A:
        if lvl <= amps:
            best = lvl
    return best


def _auto_state(cp_id: str) -> dict:
    return _solar_throttle.setdefault(cp_id, {
        "throttled_watts": 0.0, "level_a": 0, "direction": None,
        "consecutive": 0, "reason": "", "initialised": False,
    })


def _auto_decide(cp_id: str) -> tuple[float, bool]:
    """Pick the Auto-mode current level. Returns (watts, changed).

    Off-peak: full overnight level. Otherwise never import from the grid:
    step down when importing, step up only on real surplus (or a battery-backed
    probe), and go OFF if site metrics are stale.
    """
    cfg = _get_schedule(cp_id)
    st = _auto_state(cp_id)
    first = not st["initialised"]
    cur = st["level_a"]
    volts = _measured_voltage(cp_id)
    target, reason, immediate = cur, "hold", first

    if _is_off_peak(cp_id):
        target = _snap_level(cfg.get("max_amps", 16)) or AUTO_LEVELS_A[0]
        reason, immediate = "off-peak", True
    else:
        m = _solar_metrics
        last = m.get("last_update")
        age = (datetime.now(timezone.utc) - last).total_seconds() if last else None
        if age is None or age > AUTO_METRICS_MAX_AGE_S:
            target, reason, immediate = 0, "site metrics stale", True
        else:
            imp = m.get("grid_import") or 0
            exp = m.get("grid_export") or 0
            soc = m.get("battery_soc")
            cap = _snap_level(cfg.get("max_amps", 16)) or AUTO_LEVELS_A[0]
            mv = (_cp_state.get(cp_id, {}).get("meter_values", {}) or {}).get("1") or {}
            try:
                ev_w = max(0.0, float(mv.get("power") or 0))
            except (TypeError, ValueError):
                ev_w = 0.0
            deadband = cfg.get("grid_deadband_w", AUTO_DEADBAND_W)
            fit = min(cap, _snap_level((ev_w + exp - imp - deadband) / volts))
            lower = max([l for l in (0,) + AUTO_LEVELS_A if l < cur], default=0)
            if soc is not None and soc < cfg.get("min_battery_soc", 50):
                candidate, direction, need, why = 0, "down", 1, f"battery {soc}% below minimum"
            elif cur > cap:
                candidate, direction, need, why = cap, "down", 1, "above max amps"
            elif imp > deadband:
                candidate = min(fit, lower) if fit < cur else lower
                direction, need, why = "down", AUTO_DOWN_CHECKS, f"grid import {imp}W"
            elif fit > cur:
                candidate, direction, need, why = fit, "up", AUTO_UP_CHECKS, f"surplus {fit * volts:.0f}W"
            elif cur < cap and soc is not None:
                # No import at this level: battery is covering it, probe one step up.
                candidate = min(l for l in AUTO_LEVELS_A if l > cur)
                direction, need, why = "up", AUTO_UP_CHECKS, f"no grid import, battery {soc}%"
            else:
                candidate, direction, need, why = cur, None, 0, "hold"
            if candidate == cur:
                st["direction"], st["consecutive"] = None, 0
            else:
                st["consecutive"] = st["consecutive"] + 1 if st["direction"] == direction else 1
                st["direction"] = direction
                if first or st["consecutive"] >= need:
                    target, reason = candidate, why
                    immediate = True

    st["initialised"] = True
    if target == cur and not first:
        return st["throttled_watts"], False
    if not immediate and not first:
        return st["throttled_watts"], False
    st.update(level_a=target, throttled_watts=float(target * DEFAULT_VOLTAGE),
              direction=None, consecutive=0, reason=reason)
    return st["throttled_watts"], True


async def _apply_throttled_watts(cp_id: str, watts: float, reason: str = ""):
    """Send SetChargingProfile (id 1, flat) with the chosen Auto limit."""
    cp = _active_cps.get(cp_id)
    if not cp:
        return
    from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
    from ocpp.v16.enums import ChargingProfilePurposeType, ChargingProfileKindType
    try:
        result = await cp.call(SetChargingProfile(
            connector_id=0,
            cs_charging_profiles=ChargingProfile(
                charging_profile_id=PROFILE_ID, stack_level=0,
                charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                charging_profile_kind=ChargingProfileKindType.recurring,
                recurrency_kind=RecurrencyKind.daily,
                charging_schedule=ChargingSchedule(
                    charging_rate_unit="W",
                    charging_schedule_period=[
                        ChargingSchedulePeriod(start_period=0, limit=watts),
                    ],
                ),
            ),
        ))
        status = str(getattr(result, "status", result))
        msg = f"Auto level {watts / DEFAULT_VOLTAGE:.0f}A ({watts:.0f}W) — {reason} [{status}]"
        _LOGGER.info("Auto: %s %s", cp_id, msg)
        _record_event(cp_id, "solar_throttle", msg)
    except Exception as e:
        _LOGGER.warning("Auto: SetChargingProfile failed for %s: %s", cp_id, e)


async def _solar_smart_tick():
    """Periodic Auto-mode check: choose OFF/6/8/16/32A from site power."""
    for cp_id, config in list(_schedule_configs.items()):
        if config.get("mode", "charge_now") != "auto":
            continue
        cp = _active_cps.get(cp_id)
        if not cp:
            continue

        window_key = _off_peak_window_key(cp_id)
        if window_key:
            await _try_auto_off_peak_start(cp_id, cp, window_key)

        watts, changed = _auto_decide(cp_id)
        if changed:
            await _apply_throttled_watts(cp_id, watts, _auto_state(cp_id)["reason"])
            asyncio.create_task(_verify_profile_later(cp_id, "auto level change"))


async def _solar_smart_loop():
    """Background task: run Solar Smart throttle check every SOLAR_CHECK_INTERVAL seconds."""
    while True:
        try:
            await _solar_smart_tick()
        except Exception as e:
            _LOGGER.error("Solar Smart tick error: %s", e)
        await asyncio.sleep(SOLAR_CHECK_INTERVAL)

async def handle_schedule_post(request):
    """POST /schedule — Set charging mode.

    Body: {"cp_id", "mode": "stop"|"auto"|"charge_now", "timezone"?,
           "off_peak_start_hour"?, "off_peak_end_hour"?,
           "max_amps"? (6|8|16|32), "min_battery_soc"? (0-100),
           "grid_deadband_w"? (0-5000)}
    """
    try:
        body = await request.json()
        cp_id = body.get("cp_id")
        mode = body.get("mode")
        call_warnings = []

        if not cp_id or mode not in ("stop", "auto", "charge_now"):
            return web.json_response({"error": "Missing cp_id or invalid mode (use stop|auto|charge_now)"}, status=400)

        cp = _active_cps.get(cp_id)
        if not cp:
            return web.json_response({"error": f"Charge point {cp_id} not connected"}, status=404)

        config = _get_schedule(cp_id)
        previous_mode = config.get("mode", "charge_now")
        config["mode"] = mode
        if mode != "auto":
            _auto_off_peak_start_attempts.pop(cp_id, None)

        # Update timezone if provided
        if "timezone" in body:
            tz_name = body["timezone"]
            if tz_name in available_timezones():
                config["timezone"] = tz_name
            else:
                return web.json_response({"error": f"Invalid timezone: {tz_name}"}, status=400)

        for key in ("off_peak_start_hour", "off_peak_end_hour"):
            if key in body:
                h = int(body[key])
                if not (0 <= h <= 23):
                    return web.json_response({"error": f"Invalid {key}: {h}"}, status=400)
                config[key] = h
        if "max_amps" in body:
            amps = int(body["max_amps"])
            if amps not in AUTO_LEVELS_A:
                return web.json_response({"error": f"Invalid max_amps: {amps} (use {AUTO_LEVELS_A})"}, status=400)
            config["max_amps"] = amps
        if "min_battery_soc" in body:
            soc = int(body["min_battery_soc"])
            if not (0 <= soc <= 100):
                return web.json_response({"error": f"Invalid min_battery_soc: {soc}"}, status=400)
            config["min_battery_soc"] = soc
        if "grid_deadband_w" in body:
            deadband = int(body["grid_deadband_w"])
            if not (0 <= deadband <= 5000):
                return web.json_response({"error": f"Invalid grid_deadband_w: {deadband}"}, status=400)
            config["grid_deadband_w"] = deadband
        for key in ("off_peak_rate", "peak_rate_summer", "peak_rate_other"):
            if key in body:
                rate = float(body[key])
                if not (0 <= rate <= 5):
                    return web.json_response({"error": f"Invalid {key}: {rate}"}, status=400)
                config[key] = rate

        _schedule_configs[cp_id] = config
        _schedule_state[cp_id] = {"mode": mode}  # backward compat

        from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
        from ocpp.v16.enums import ChargingProfilePurposeType, ChargingProfileKindType, ChargingRateUnitType

        async def safe_cp_call(label: str, call_obj):
            """Run OCPP call with timeout so HTTP schedule endpoint cannot hang."""
            task = asyncio.create_task(cp.call(call_obj))
            try:
                done, _ = await asyncio.wait({task}, timeout=SCHEDULE_OCPP_TIMEOUT_SECONDS)
                if not done:
                    task.cancel()
                    msg = f"{label} timed out after {SCHEDULE_OCPP_TIMEOUT_SECONDS:.0f}s"
                    _LOGGER.warning("Schedule %s on %s: %s", mode, cp_id, msg)
                    call_warnings.append(msg)
                    return None
                result = task.result()
                status = str(getattr(result, "status", "Accepted"))
                is_clear = label.startswith("ClearChargingProfile")
                if status in ("Rejected", "NotSupported") or (status == "Unknown" and not is_clear):
                    msg = f"{label} returned {status}"
                    _LOGGER.warning("Schedule %s on %s: %s", mode, cp_id, msg)
                    call_warnings.append(msg)
                return result
            except asyncio.CancelledError:
                msg = f"{label} timed out after {SCHEDULE_OCPP_TIMEOUT_SECONDS:.0f}s"
                _LOGGER.warning("Schedule %s on %s: %s", mode, cp_id, msg)
                call_warnings.append(msg)
            except Exception as e:
                msg = f"{label} failed: {e}"
                _LOGGER.warning("Schedule %s on %s: %s", mode, cp_id, msg)
                call_warnings.append(msg)
            return None

        if mode == "stop":
            _LOGGER.info("STOP mode for %s — clearing profile + stopping any active charge", cp_id)
            await _clear_all_profiles(cp, safe_cp_call)
            if not call_warnings:
                tx_id = _tx_ids.get(cp_id, 0)
                if tx_id:
                    await safe_cp_call("RemoteStopTransaction", RemoteStopTransaction(transaction_id=tx_id))
            if not call_warnings:
                await safe_cp_call("SetChargingProfile(stop)", SetChargingProfile(
                    connector_id=0,
                    cs_charging_profiles=ChargingProfile(
                        charging_profile_id=1, stack_level=0,
                        charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                        charging_profile_kind=ChargingProfileKindType.relative,
                        charging_schedule=ChargingSchedule(
                            charging_rate_unit=ChargingRateUnitType.watts,
                            charging_schedule_period=[
                                ChargingSchedulePeriod(start_period=0, limit=0.0),
                            ],
                        ),
                    ),
                ))
            _record_event(cp_id, "schedule", "Mode: STOP — charging blocked")

        elif mode == "auto":
            _solar_throttle.pop(cp_id, None)
            auto_w, _ = _auto_decide(cp_id)
            desc = f"{auto_w / DEFAULT_VOLTAGE:.0f}A ({_auto_state(cp_id)['reason']})"
            _LOGGER.info("AUTO mode for %s — initial level %s", cp_id, desc)
            await _clear_all_profiles(cp, safe_cp_call)
            await safe_cp_call("SetChargingProfile(auto)", SetChargingProfile(
                connector_id=0,
                cs_charging_profiles=ChargingProfile(
                    charging_profile_id=1, stack_level=0,
                    charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                    charging_profile_kind=ChargingProfileKindType.recurring,
                    recurrency_kind=RecurrencyKind.daily,
                    charging_schedule=ChargingSchedule(
                        charging_rate_unit=ChargingRateUnitType.watts,
                        charging_schedule_period=[
                            ChargingSchedulePeriod(start_period=0, limit=auto_w),
                        ],
                    ),
                ),
            ))
            _record_event(cp_id, "schedule", f"Mode: AUTO — {desc}")

        else:  # charge_now
            _LOGGER.info("CHARGE NOW for %s — clearing profile + full power", cp_id)
            _solar_throttle.pop(cp_id, None)
            await _clear_all_profiles(cp, safe_cp_call)
            if not call_warnings:
                await safe_cp_call("SetChargingProfile(charge_now)", SetChargingProfile(
                    connector_id=0,
                    cs_charging_profiles=ChargingProfile(
                        charging_profile_id=PROFILE_ID, stack_level=0,
                        charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                        charging_profile_kind=ChargingProfileKindType.relative,
                        charging_schedule=ChargingSchedule(
                            charging_rate_unit=ChargingRateUnitType.watts,
                            charging_schedule_period=[
                                ChargingSchedulePeriod(start_period=0, limit=CHARGE_NOW_WATTS),
                            ],
                        ),
                    ),
                ))
            conn1_status = _cp_state.get(cp_id, {}).get("connectors", {}).get("1", "")
            should_start = previous_mode == "stop" or conn1_status in ("Available", "Preparing")
            if (not call_warnings) and should_start:
                start_result = await safe_cp_call("RemoteStartTransaction", RemoteStartTransaction(
                    id_tag="0000003934", connector_id=1,
                ))
                start_status = getattr(start_result, "status", "unknown")
                _record_event(cp_id, "remote_start", f"status={start_status}", {
                    "status": start_status,
                    "source": "charge_now",
                })
            _record_event(cp_id, "schedule", "Mode: CHARGE NOW")

        # Persist to DocumentDB
        asyncio.create_task(_docdb_save_schedule(cp_id))
        asyncio.create_task(_verify_profile_later(cp_id, f"mode change → {mode}"))

        await _mqtt_publish(_cp_topic(cp_id, "schedule"), {"mode": mode})
        response = {"status": "ok", "mode": mode, "config": config}
        if call_warnings:
            response["warnings"] = call_warnings
        return web.json_response(response)

    except Exception as e:
        _LOGGER.error("Schedule error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_schedule_get(request):
    """GET /schedule — Return schedule configs and active CPs."""
    configs = {}
    for cp_id in _active_cps:
        configs[cp_id] = _get_schedule(cp_id)
    for cp_id in _schedule_configs:
        if cp_id not in configs:
            configs[cp_id] = _schedule_configs[cp_id]
    return web.json_response({
        "schedule_state": _schedule_state,  # backward compat
        "schedule_configs": configs,
        "active_cps": list(_active_cps.keys()),
        "timezones": COMMON_TIMEZONES,
    })


async def handle_timezones(request):
    """GET /timezones — List available timezones for schedule config."""
    return web.json_response({"timezones": COMMON_TIMEZONES})


async def handle_test_profile(request):
    """GET /test-profile/{cp_id} — Try Absolute and Recurring TxDefaultProfile.
    
    Tests each profile kind against the connected charger and reports
    which ones are accepted vs rejected.
    """
    cp_id = request.match_info.get("cp_id", "")
    cp = _active_cps.get(cp_id)
    if not cp:
        return web.json_response({"error": f"Charge point {cp_id} not connected"}, status=404)

    from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    # Two-period schedule: 4800W 12am-4pm, 1440W 4pm-12am
    tests = []

    # Test 1: Relative TxDefaultProfile (known working baseline)
    tests.append(("Relative", SetChargingProfile(
        connector_id=0,
        cs_charging_profiles=ChargingProfile(
            charging_profile_id=1, stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
            charging_profile_kind=ChargingProfileKindType.relative,
            charging_schedule=ChargingSchedule(
                charging_rate_unit="W",
                charging_schedule_period=[
                    ChargingSchedulePeriod(start_period=0, limit=4800.0),
                    ChargingSchedulePeriod(start_period=57600, limit=1440.0),
                ],
            ),
        ),
    )))

    # Test 2: Recurring Daily TxDefaultProfile
    tests.append(("Recurring+Daily", SetChargingProfile(
        connector_id=0,
        cs_charging_profiles=ChargingProfile(
            charging_profile_id=2, stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
            charging_profile_kind=ChargingProfileKindType.recurring,
            recurrency_kind=RecurrencyKind.daily,
            charging_schedule=ChargingSchedule(
                charging_rate_unit="W",
                charging_schedule_period=[
                    ChargingSchedulePeriod(start_period=0, limit=4800.0),
                    ChargingSchedulePeriod(start_period=57600, limit=1440.0),
                ],
            ),
        ),
    )))

    # Test 3: Absolute TxDefaultProfile
    tests.append(("Absolute", SetChargingProfile(
        connector_id=0,
        cs_charging_profiles=ChargingProfile(
            charging_profile_id=3, stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
            charging_profile_kind=ChargingProfileKindType.absolute,
            valid_from=today_start.isoformat(),
            valid_to=(tomorrow_start + timedelta(days=365)).isoformat(),
            charging_schedule=ChargingSchedule(
                start_schedule=today_start.isoformat(),
                duration=86400,
                charging_rate_unit="W",
                charging_schedule_period=[
                    ChargingSchedulePeriod(start_period=0, limit=4800.0),
                    ChargingSchedulePeriod(start_period=57600, limit=1440.0),
                ],
            ),
        ),
    )))

    results = []
    for label, call_obj in tests:
        try:
            result = await cp.call(call_obj)
            status = getattr(result, "status", str(result))
            results.append({"kind": label, "accepted": (status == "Accepted"), "status": status})
            _LOGGER.info("Test profile %s → %s", label, status)
        except Exception as e:
            results.append({"kind": label, "accepted": False, "error": str(e)[:200]})
            _LOGGER.warning("Test profile %s → ERROR: %s", label, e)

    # Reset to known-good Relative profile
    await cp.call(SetChargingProfile(
        connector_id=0,
        cs_charging_profiles=ChargingProfile(
            charging_profile_id=1, stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
            charging_profile_kind=ChargingProfileKindType.relative,
            charging_schedule=ChargingSchedule(
                charging_rate_unit="W",
                charging_schedule_period=[
                    ChargingSchedulePeriod(start_period=0, limit=4800.0),
                    ChargingSchedulePeriod(start_period=57600, limit=1440.0),
                ],
            ),
        ),
    ))

    return web.json_response({"cp_id": cp_id, "results": results})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    global _mqtt_client

    _LOGGER.info("Starting OCPP-MQTT Bridge...")
    _LOGGER.info("OCPP CSMS → %s:%d", OCPP_HOST, OCPP_PORT)
    _LOGGER.info("Web UI → http://0.0.0.0:%d/", UI_PORT)
    if DOCDB_ENABLED:
        _LOGGER.info("DocumentDB → %s (db=%s)", DOCDB_URL, DOCDB_DB)

    # Initialize DocumentDB
    if DOCDB_ENABLED:
        await _docdb_ensure_db()
        await _docdb_load_history()
        await _docdb_load_schedules()
        await _docdb_load_metrics()
        await _docdb_load_cp_states()

    # Start Solar Smart background loop
    asyncio.create_task(_solar_smart_loop())
    asyncio.create_task(_hourly_history_loop())
    asyncio.create_task(_profile_watchdog_loop())

    app = web.Application()

    # API routes (must be BEFORE wildcard OCPP routes)
    app.router.add_get("/debug", handle_debug)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/schedule", handle_schedule_get)
    app.router.add_post("/schedule", handle_schedule_post)
    app.router.add_put("/schedule", handle_schedule_post)
    app.router.add_get("/timezones", handle_timezones)
    app.router.add_get("/test-profile/{cp_id}", handle_test_profile)
    app.router.add_get("/profile-check/{cp_id}", handle_profile_check)

    # OCPP WebSocket endpoint — chargers connect via wss://ocpp.gormantec.com/ocpp16/{cp_id}
    app.router.add_get("/ocpp16/{cp_id}", ocpp_ws_handler)

    # Static UI
    ui_dist = os.path.join(os.path.dirname(__file__), "ui", "dist")
    if os.path.exists(ui_dist):
        assets_dir = os.path.join(ui_dist, "assets")
        if os.path.exists(assets_dir):
            app.router.add_static("/assets", assets_dir)
        app.router.add_get("/", handle_index)
        app.router.add_get("/{path:.*}", handle_index)
        _LOGGER.info("Serving UI from %s", ui_dist)
    else:
        app.router.add_get("/", handle_debug)

    # Start MQTT listener in background
    asyncio.create_task(mqtt_listener())

    # Start aiohttp server (handles both OCPP WS + Web UI)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, OCPP_HOST, OCPP_PORT)
    await site.start()

    _LOGGER.info("OCPP-MQTT Bridge ready — listening on %s:%d", OCPP_HOST, OCPP_PORT)
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
