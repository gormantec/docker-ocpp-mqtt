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
from zoneinfo import ZoneInfo
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

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# Charger Basic auth password
AUTH_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"

# ---------------------------------------------------------------------------
# Schedule helper
# ---------------------------------------------------------------------------

def _is_charging_allowed(cp_id: str) -> bool:
    """Check if charging is currently allowed.
    
    stop   → always False (terminate + deny all).
    auto   → True 12am-4pm Sydney, False 4pm-12am.
    charge_now → always True.
    """
    state = _schedule_state.get(cp_id, {}).get("mode", "charge_now")
    if state == "stop":
        return False
    if state == "charge_now":
        return True
    # auto (scheduled)
    now_syd = datetime.now(SYDNEY_TZ)
    return now_syd.hour < 16

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
                            "connector_id": None, "last_event": None}
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
            _cp_state[cp_id]["status"] = status
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
        _LOGGER.info("Subscribed to MQTT topic: ocpp/+/cmd")

        async for message in client.messages:
            topic = str(message.topic)
            parts = topic.split("/")
            if len(parts) >= 3 and parts[2] == "cmd":
                cp_id = parts[1]
                await _handle_mqtt_command(cp_id, message.payload)


# ---------------------------------------------------------------------------
# Web UI / Debug API
# ---------------------------------------------------------------------------

async def handle_debug(request):
    """GET /debug — Full state for the React UI."""
    now = datetime.now(timezone.utc)
    uptime = (now - STARTED_AT).total_seconds()

    charge_points = list(_cp_state.values())
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

async def handle_schedule_post(request):
    """POST /schedule — Set charging mode.
    Body: {"cp_id": "4b8609", "mode": "stop"|"auto"|"charge_now"}
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

        from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
        from ocpp.v16.enums import ChargingProfilePurposeType, ChargingProfileKindType, ChargingRateUnitType

        if mode == "stop":
            _LOGGER.info("STOP mode for %s — clearing profile + stopping any active charge", cp_id)
            # Clear on-device schedule
            await cp.call(ClearChargingProfile(
                id=1, connector_id=0,
                charging_profile_purpose="TxDefaultProfile", stack_level=0,
            ))
            # Stop any active transaction
            tx_id = _tx_ids.get(cp_id, 0)
            if tx_id:
                try:
                    await cp.call(RemoteStopTransaction(transaction_id=tx_id))
                    _LOGGER.info("Sent RemoteStopTransaction tx=%d for %s", tx_id, cp_id)
                except Exception as e:
                    _LOGGER.warning("RemoteStopTransaction failed: %s", e)
            # Set watt profile to 0 (prevent charging)
            result = await cp.call(SetChargingProfile(
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
            _schedule_state[cp_id] = {"mode": "stop"}
            _record_event(cp_id, "schedule", "Mode: STOP — charging blocked")

        elif mode == "auto":
            _LOGGER.info("AUTO mode for %s — 2-period schedule (4800W/1440W)", cp_id)
            result = await cp.call(SetChargingProfile(
                connector_id=0,
                cs_charging_profiles=ChargingProfile(
                    charging_profile_id=1, stack_level=0,
                    charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                    charging_profile_kind=ChargingProfileKindType.relative,
                    charging_schedule=ChargingSchedule(
                        charging_rate_unit=ChargingRateUnitType.watts,
                        charging_schedule_period=[
                            ChargingSchedulePeriod(start_period=0, limit=4800.0),
                            ChargingSchedulePeriod(start_period=57600, limit=1440.0),
                        ],
                    ),
                ),
            ))
            _schedule_state[cp_id] = {"mode": "auto"}
            _record_event(cp_id, "schedule", "Mode: AUTO (4800W 12am-4pm, 1440W 4pm-12am)")

        else:  # charge_now
            _LOGGER.info("CHARGE NOW for %s — clearing profile + triggering start", cp_id)
            await cp.call(ClearChargingProfile(
                id=1, connector_id=0,
                charging_profile_purpose="TxDefaultProfile", stack_level=0,
            ))
            # Set full-power profile as fallback
            result = await cp.call(SetChargingProfile(
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
            # Try to initiate charging if a car is connected (connector 1)
            cp_status = _cp_state.get(cp_id, {}).get("status", "")
            if cp_status in ("Available", "Preparing"):
                try:
                    # Use the last-seen id_tag (or a default)
                    await cp.call(RemoteStartTransaction(
                        id_tag="0000003934", connector_id=1,
                    ))
                    _LOGGER.info("Sent RemoteStartTransaction for %s", cp_id)
                except Exception as e:
                    _LOGGER.warning("RemoteStartTransaction failed: %s", e)
            _schedule_state[cp_id] = {"mode": "charge_now"}
            _record_event(cp_id, "schedule", "Mode: CHARGE NOW")

        await _mqtt_publish(_cp_topic(cp_id, "schedule"), {"mode": mode})
        return web.json_response({"status": "ok", "mode": mode})

    except Exception as e:
        _LOGGER.error("Schedule error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_schedule_get(request):
    """GET /schedule — Return schedule state and active CPs."""
    return web.json_response({
        "schedule_state": _schedule_state,
        "active_cps": list(_active_cps.keys()),
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    global _mqtt_client

    _LOGGER.info("Starting OCPP-MQTT Bridge...")
    _LOGGER.info("OCPP CSMS → %s:%d", OCPP_HOST, OCPP_PORT)
    _LOGGER.info("Web UI → http://0.0.0.0:%d/", UI_PORT)

    app = web.Application()

    # API routes (must be BEFORE wildcard OCPP routes)
    app.router.add_get("/debug", handle_debug)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/schedule", handle_schedule_get)
    app.router.add_post("/schedule", handle_schedule_post)

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
