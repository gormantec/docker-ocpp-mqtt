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
from datetime import datetime, timezone
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
    SendLocalList, SetChargingProfile, ClearChargingProfile,
)

# Shared MQTT helpers (same pattern as other docker-iot containers)
from mqtt_connect import build_mqtt_context

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

# Charger Basic auth password
AUTH_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"

# ---------------------------------------------------------------------------
# Schedule helper
# ---------------------------------------------------------------------------

def _is_charging_allowed(cp_id: str) -> bool:
    """Check if charging is currently allowed (per-CP timezone with DST).
    
    stop       → always False.
    charge_now → always True.
    auto       → True only during peak windows (periods with limit > 0).
    """
    config = _get_schedule(cp_id)
    mode = config.get("mode", "charge_now")
    if mode == "stop":
        return False
    if mode == "charge_now":
        return True
    # auto: check if current hour (in CP's timezone) has a non-zero limit
    tz = _get_tz(cp_id)
    now = datetime.now(tz)
    periods = config.get("periods", DEFAULT_SCHEDULE["periods"])
    # Find the active period: the one with the largest start_hour <= current hour
    active = None
    for p in sorted(periods, key=lambda x: x["start_hour"]):
        if p["start_hour"] <= now.hour:
            active = p
    if active is None:
        # Before first period: allow if any period has limit > 0
        return any(p.get("limit_watts", 0) > 0 for p in periods)
    return active.get("limit_watts", 0) > 0

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
DOCDB_DB = "ocpp_mqtt"

def _docdb_key(cp_id: str, doc_type: str) -> str:
    """Build a namespaced DocumentDB key: {cp_id}:{type}"""
    return f"{cp_id}:{doc_type}"

# Service start timestamp
STARTED_AT = datetime.now(timezone.utc)

# Event ring buffer
MAX_EVENTS = 200
_event_buffer: deque = deque(maxlen=MAX_EVENTS)

# Per-charge-point state
_cp_state: dict[str, dict] = {}

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

def _record_event(cp_id: str, event_type: str, summary: str = ""):
    event = {
        "time": datetime.now(timezone.utc).isoformat(),
        "charge_point_id": cp_id,
        "type": event_type,
        "summary": summary,
    }
    _event_buffer.append(event)
    if cp_id not in _cp_state:
        _cp_state[cp_id] = {"id": cp_id, "connected": True, "status": "unknown",
                            "connector_id": None, "last_event": None,
                            "connectors": {}}  # per-connector -> status
    _cp_state[cp_id]["last_event"] = event["time"]


# ---------------------------------------------------------------------------
# MQTT-tracking ChargePoint
# ---------------------------------------------------------------------------

class MqttChargePoint(BaseChargePoint):
    """
    OCPP v1.6 ChargePoint handler (Central System side).

    Each connected charge point gets its own instance. All OCPP actions
    received from the charge point are forwarded to MQTT.
    """

    def __init__(self, cp_id: str, connection):
        super().__init__(cp_id, connection)
        _record_event(cp_id, "connected", "Charge point connected")
        _cp_state[cp_id] = {
            "id": cp_id, "connected": True, "status": "unknown",
            "connector_id": None, "last_event": datetime.now(timezone.utc).isoformat(),
            "connectors": {},  # per-connector -> status
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
        summary = f"status={status}"
        if connector_id is not None:
            summary += f" connector={connector_id}"
        if error_code and error_code != "NoError":
            summary += f" error={error_code}"

        _LOGGER.info("StatusNotification from %s: %s", cp_id, summary)
        _record_event(cp_id, "status_notification", summary)

        if cp_id in _cp_state:
            # Track per-connector status in a clean dict
            conn_key = str(connector_id) if connector_id is not None else "0"
            _cp_state[cp_id]["connectors"][conn_key] = status
            if connector_id is not None:
                _cp_state[cp_id]["connector_id"] = connector_id

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
        _LOGGER.info("StartTransaction from %s: id_tag=%s connector=%s meter=%s",
                     self.id, id_tag, connector_id, meter_start)
        _record_event(self.id, "start_transaction",
                      f"id_tag={id_tag} meter_start={meter_start}")
        if self.id in _cp_state:
            _cp_state[self.id]["status"] = "Charging"

        payload = {
            "connector_id": connector_id, "id_tag": id_tag,
            "meter_start": meter_start, "timestamp": timestamp,
            "reservation_id": reservation_id,
        }
        await _mqtt_publish(_cp_topic(self.id, "start_transaction"), payload)
        return ocpp_result.StartTransaction(
            transaction_id=1,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on("StopTransaction")
    async def on_stop_transaction(self, meter_stop, timestamp, transaction_id,
                                   reason=None, id_tag=None, **kwargs):
        _LOGGER.info("StopTransaction from %s: meter_stop=%s reason=%s",
                     self.id, meter_stop, reason)
        _record_event(self.id, "stop_transaction",
                      f"meter_stop={meter_stop} reason={reason}")
        if self.id in _cp_state:
            _cp_state[self.id]["status"] = "Available"

        payload = {
            "meter_stop": meter_stop, "timestamp": timestamp,
            "transaction_id": transaction_id, "reason": reason, "id_tag": id_tag,
        }
        await _mqtt_publish(_cp_topic(self.id, "stop_transaction"), payload)
        return ocpp_result.StopTransaction(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on("MeterValues")
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        _LOGGER.debug("MeterValues from %s: connector=%s", self.id, connector_id)
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
                    if "pvPower" in data:
                        _solar_metrics["pv_power"] = int(float(data["pvPower"]))
                    _solar_metrics["last_update"] = datetime.now(timezone.utc)
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
        "solar_metrics": {
            "grid_import": _solar_metrics["grid_import"],
            "grid_export": _solar_metrics["grid_export"],
            "battery_soc": _solar_metrics["battery_soc"],
            "pv_power": _solar_metrics["pv_power"],
            "last_update": _solar_metrics["last_update"].isoformat() if _solar_metrics["last_update"] else None,
        },
        "solar_throttle": {k: v["throttled_watts"] for k, v in _solar_throttle.items()},
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
    "battery_soc": None,  # Battery % (None = unknown)
    "pv_power": 0,        # Solar generation Watts
    "last_update": None,  # datetime of last ESY telemetry
}

# Solar Smart per-CP throttle state
# {cp_id: {"throttled_watts": float, "direction": "down"|"up"|None, "consecutive": int}}
_solar_throttle: dict[str, dict] = {}

# ESY sunhomes MQTT thing name (must match cloudformation)
ESY_THING_NAME = _env_str("ESY_THING_NAME", "gormantec-battery1")
ESY_TELEMETRY_TOPIC = f"$iothub/twin/PATCH/properties/reported/{ESY_THING_NAME}"

# Solar Smart constants
SOLAR_GRID_IMPORT_THRESHOLD = 500     # Watts — above this, throttle down
SOLAR_RAMP_STEP = 480                 # Watts per step
SOLAR_MIN_WATTS = 1440                # Floor — never go below this
SOLAR_DOWN_CHECKS = 4                 # 2min @ 30s intervals → ramp down
SOLAR_UP_CHECKS = 20                  # 10min @ 30s intervals → ramp up
SOLAR_UP_BATTERY_SOC_MIN = 30        # Battery SOC must be > this to ramp up
SOLAR_UP_PV_MIN = 500                # PV must be > this to ramp up
SOLAR_CHECK_INTERVAL = 30            # Seconds between throttle checks

# Per-CP schedule config (persisted to DocumentDB)
# {cp_id: {mode, peak_start_hour, peak_end_hour, peak_watts, off_peak_watts}}
_schedule_configs: dict[str, dict] = {}

# Default schedule config
# Default schedule config — periods map to TxDefaultProfile ChargingSchedulePeriod
# Each period: {start_hour: 0-23, limit_watts: 0-50000}
# Periods are relative to charging start (ChargingProfileKindType.relative)
DEFAULT_SCHEDULE = {
    "mode": "charge_now",
    "timezone": "Australia/Sydney",
    "periods": [
        {"start_hour": 0, "limit_watts": 4800.0},
        {"start_hour": 16, "limit_watts": 1440.0},
    ],
    # Solar Smart: dynamically throttle charging based on grid import/export
    "solar_smart": False,
    "off_peak_start_hour": 0,   # off-peak grid window (cheap power, allow grid import)
    "off_peak_end_hour": 6,
}

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


def _validate_periods(periods):
    """Validate and normalize schedule periods. Returns (ok, normalized_or_error)."""
    if not periods or not isinstance(periods, list) or len(periods) == 0:
        return False, "At least one period is required"
    out = []
    for p in periods:
        sh = p.get("start_hour")
        lw = p.get("limit_watts")
        if sh is None or not isinstance(sh, (int, float)) or sh < 0 or sh > 23:
            return False, f"Invalid start_hour: {sh} (must be 0-23)"
        if lw is None or not isinstance(lw, (int, float)) or lw < 0 or lw > 50000:
            return False, f"Invalid limit_watts: {lw} (must be 0-50000)"
        out.append({"start_hour": int(sh), "limit_watts": float(lw)})
    # Sort by start_hour ascending
    out.sort(key=lambda p: p["start_hour"])
    # Deduplicate start_hour
    seen = set()
    deduped = []
    for p in out:
        if p["start_hour"] not in seen:
            seen.add(p["start_hour"])
            deduped.append(p)
    return True, deduped


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
        auth = None
        if DOCDB_USER:
            from aiohttp import BasicAuth
            auth = BasicAuth(DOCDB_USER, DOCDB_PASSWORD)
        async with _aiohttp.ClientSession(auth=auth) as sess:
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

def _get_period_limit_for_hour(cp_id: str, hour: int) -> float:
    """Get the configured watt limit for a given hour from the CP's periods."""
    config = _get_schedule(cp_id)
    periods = sorted(config.get("periods", DEFAULT_SCHEDULE["periods"]),
                     key=lambda p: p["start_hour"])
    active = periods[0]
    for p in periods:
        if p["start_hour"] <= hour:
            active = p
    return active.get("limit_watts", 4800.0)


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


async def _apply_throttled_watts(cp_id: str, watts: float):
    """Send SetChargingProfile with a throttled watt limit."""
    cp = _active_cps.get(cp_id)
    if not cp:
        return
    from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
    from ocpp.v16.enums import ChargingProfilePurposeType, ChargingProfileKindType, ChargingRateUnitType
    try:
        await cp.call(SetChargingProfile(
            connector_id=0,
            cs_charging_profiles=ChargingProfile(
                charging_profile_id=1, stack_level=0,
                charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                charging_profile_kind=ChargingProfileKindType.relative,
                charging_schedule=ChargingSchedule(
                    charging_rate_unit=ChargingRateUnitType.watts,
                    charging_schedule_period=[
                        ChargingSchedulePeriod(start_period=0, limit=watts),
                    ],
                ),
            ),
        ))
        _LOGGER.info("Solar Smart: %s throttled to %.0fW", cp_id, watts)
        _record_event(cp_id, "solar_throttle", f"throttled to {watts:.0f}W")
    except Exception as e:
        _LOGGER.warning("Solar Smart: SetChargingProfile failed for %s: %s", cp_id, e)


async def _solar_smart_tick():
    """Periodic check: adjust charge rates based on grid import/export."""
    for cp_id, config in list(_schedule_configs.items()):
        mode = config.get("mode", "charge_now")
        solar_smart = config.get("solar_smart", False)
        if mode != "auto" or not solar_smart:
            continue
        cp = _active_cps.get(cp_id)
        if not cp:
            continue

        # Off-peak window: reset to configured period rate, no throttling
        if _is_off_peak(cp_id):
            tz = _get_tz(cp_id)
            hour = datetime.now(tz).hour
            configured_watts = _get_period_limit_for_hour(cp_id, hour)
            if cp_id in _solar_throttle:
                del _solar_throttle[cp_id]
                await _apply_throttled_watts(cp_id, configured_watts)
                _LOGGER.info("Solar Smart: %s off-peak, reset to %.0fW", cp_id, configured_watts)
            continue

        grid_import = _solar_metrics.get("grid_import", 0)
        battery_soc = _solar_metrics.get("battery_soc")
        pv_power = _solar_metrics.get("pv_power", 0)

        tz = _get_tz(cp_id)
        hour = datetime.now(tz).hour
        configured_watts = _get_period_limit_for_hour(cp_id, hour)

        throttle = _solar_throttle.get(cp_id, {
            "throttled_watts": configured_watts,
            "direction": None,
            "consecutive": 0,
        })
        current_watts = throttle["throttled_watts"]

        if grid_import > SOLAR_GRID_IMPORT_THRESHOLD:
            # Ramp DOWN — too much grid import
            if throttle.get("direction") == "down":
                throttle["consecutive"] += 1
            else:
                throttle["direction"] = "down"
                throttle["consecutive"] = 1

            if throttle["consecutive"] >= SOLAR_DOWN_CHECKS:
                new_watts = max(current_watts - SOLAR_RAMP_STEP, SOLAR_MIN_WATTS)
                if new_watts < current_watts:
                    throttle["throttled_watts"] = new_watts
                    throttle["consecutive"] = 0
                    await _apply_throttled_watts(cp_id, new_watts)
        elif (grid_import <= SOLAR_GRID_IMPORT_THRESHOLD
              and (battery_soc is None or battery_soc > SOLAR_UP_BATTERY_SOC_MIN)
              and pv_power > SOLAR_UP_PV_MIN):
            # Ramp UP — grid is fine, solar is abundant, battery healthy
            if throttle.get("direction") == "up":
                throttle["consecutive"] += 1
            else:
                throttle["direction"] = "up"
                throttle["consecutive"] = 1

            if throttle["consecutive"] >= SOLAR_UP_CHECKS:
                new_watts = min(current_watts + SOLAR_RAMP_STEP, configured_watts)
                if new_watts > current_watts:
                    throttle["throttled_watts"] = new_watts
                    throttle["consecutive"] = 0
                    await _apply_throttled_watts(cp_id, new_watts)
        else:
            # Reset direction tracking if neither condition met
            throttle["direction"] = None
            throttle["consecutive"] = 0

        _solar_throttle[cp_id] = throttle


async def _solar_smart_loop():
    """Background task: run Solar Smart throttle check every SOLAR_CHECK_INTERVAL seconds."""
    while True:
        try:
            await _solar_smart_tick()
        except Exception as e:
            _LOGGER.error("Solar Smart tick error: %s", e)
        await asyncio.sleep(SOLAR_CHECK_INTERVAL)

async def handle_schedule_post(request):
    """POST /schedule — Set charging mode and TxDefaultProfile periods.
    
    Body: {
        "cp_id": "4b8609",
        "mode": "stop"|"auto"|"charge_now",
        "periods": [                          // only for mode="auto"
            {"start_hour": 0, "limit_watts": 4800},
            {"start_hour": 16, "limit_watts": 1440}
        ]
    }
    
    Periods map directly to OCPP ChargingSchedulePeriod:
      start_period = start_hour * 3600 (seconds from charging start)
      limit = limit_watts (watts, StarCharge only accepts W not A)
    """
    try:
        body = await request.json()
        cp_id = body.get("cp_id")
        mode = body.get("mode")

        if not cp_id or mode not in ("stop", "auto", "charge_now"):
            return web.json_response({"error": "Missing cp_id or invalid mode (use stop|auto|charge_now)"}, status=400)

        cp = _active_cps.get(cp_id)
        if not cp:
            return web.json_response({"error": f"Charge point {cp_id} not connected"}, status=404)

        config = _get_schedule(cp_id)
        config["mode"] = mode

        # Update timezone if provided
        if "timezone" in body:
            tz_name = body["timezone"]
            if tz_name in available_timezones():
                config["timezone"] = tz_name
            else:
                return web.json_response({"error": f"Invalid timezone: {tz_name}"}, status=400)

        # Solar Smart fields
        if "solar_smart" in body:
            config["solar_smart"] = bool(body["solar_smart"])
        if "off_peak_start_hour" in body:
            h = int(body["off_peak_start_hour"])
            if not (0 <= h <= 23):
                return web.json_response({"error": f"Invalid off_peak_start_hour: {h}"}, status=400)
            config["off_peak_start_hour"] = h
        if "off_peak_end_hour" in body:
            h = int(body["off_peak_end_hour"])
            if not (0 <= h <= 23):
                return web.json_response({"error": f"Invalid off_peak_end_hour: {h}"}, status=400)
            config["off_peak_end_hour"] = h

        # Validate and store periods for auto mode
        periods = None
        if mode == "auto":
            raw_periods = body.get("periods")
            if raw_periods:
                ok, result = _validate_periods(raw_periods)
                if not ok:
                    return web.json_response({"error": result}, status=400)
                periods = result
            else:
                # Use defaults or existing
                periods = config.get("periods", DEFAULT_SCHEDULE["periods"])
            config["periods"] = periods

        _schedule_configs[cp_id] = config
        _schedule_state[cp_id] = {"mode": mode}  # backward compat

        from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
        from ocpp.v16.enums import ChargingProfilePurposeType, ChargingProfileKindType, ChargingRateUnitType

        if mode == "stop":
            _LOGGER.info("STOP mode for %s — clearing profile + stopping any active charge", cp_id)
            await cp.call(ClearChargingProfile(
                id=1, connector_id=0,
                charging_profile_purpose="TxDefaultProfile", stack_level=0,
            ))
            tx_id = _tx_ids.get(cp_id, 0)
            if tx_id:
                try:
                    await cp.call(RemoteStopTransaction(transaction_id=tx_id))
                except Exception as e:
                    _LOGGER.warning("RemoteStopTransaction failed: %s", e)
            await cp.call(SetChargingProfile(
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
            # Build ChargingSchedulePeriod list from config periods
            cs_periods = []
            for p in periods:
                cs_periods.append(ChargingSchedulePeriod(
                    start_period=p["start_hour"] * 3600,
                    limit=p["limit_watts"],
                ))
            desc = ", ".join(f"{p['start_hour']:02d}:00→{p['limit_watts']:.0f}W" for p in periods)
            _LOGGER.info("AUTO mode for %s — periods: %s", cp_id, desc)
            await cp.call(SetChargingProfile(
                connector_id=0,
                cs_charging_profiles=ChargingProfile(
                    charging_profile_id=1, stack_level=0,
                    charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                    charging_profile_kind=ChargingProfileKindType.relative,
                    charging_schedule=ChargingSchedule(
                        charging_rate_unit=ChargingRateUnitType.watts,
                        charging_schedule_period=cs_periods,
                    ),
                ),
            ))
            _record_event(cp_id, "schedule", f"Mode: AUTO — {desc}")

        else:  # charge_now
            _LOGGER.info("CHARGE NOW for %s — clearing profile + full power", cp_id)
            await cp.call(ClearChargingProfile(
                id=1, connector_id=0,
                charging_profile_purpose="TxDefaultProfile", stack_level=0,
            ))
            await cp.call(SetChargingProfile(
                connector_id=0,
                cs_charging_profiles=ChargingProfile(
                    charging_profile_id=0, stack_level=0,
                    charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                    charging_profile_kind=ChargingProfileKindType.relative,
                    charging_schedule=ChargingSchedule(
                        charging_rate_unit=ChargingRateUnitType.watts,
                        charging_schedule_period=[
                            ChargingSchedulePeriod(start_period=0, limit=4800.0),
                        ],
                    ),
                ),
            ))
            conn1_status = _cp_state.get(cp_id, {}).get("connectors", {}).get("1", "")
            if conn1_status in ("Available", "Preparing"):
                try:
                    await cp.call(RemoteStartTransaction(
                        id_tag="0000003934", connector_id=1,
                    ))
                except Exception as e:
                    _LOGGER.warning("RemoteStartTransaction failed: %s", e)
            _record_event(cp_id, "schedule", "Mode: CHARGE NOW")

        # Persist to DocumentDB
        asyncio.create_task(_docdb_save_schedule(cp_id))

        await _mqtt_publish(_cp_topic(cp_id, "schedule"), {"mode": mode})
        return web.json_response({"status": "ok", "mode": mode, "config": config})

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
        await _docdb_load_schedules()

    # Start Solar Smart background loop
    asyncio.create_task(_solar_smart_loop())

    app = web.Application()

    # API routes (must be BEFORE wildcard OCPP routes)
    app.router.add_get("/debug", handle_debug)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/schedule", handle_schedule_get)
    app.router.add_post("/schedule", handle_schedule_post)
    app.router.add_get("/timezones", handle_timezones)
    app.router.add_get("/test-profile/{cp_id}", handle_test_profile)

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
