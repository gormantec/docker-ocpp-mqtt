"""
OCPP-to-MQTT Bridge — Standalone Service

Bridges EV chargers using the Open Charge Point Protocol (OCPP) to the
docker-iot MQTT broker. Uses the lbbrhzn/ocpp library files directly
(ocpp/charge_point.py, ocpp/central_system.py, etc.) without any Home
Assistant dependencies.

Architecture:
  - Acts as an OCPP Central System (CSMS) that charge points connect to
  - Publishes charge point status/telemetry to MQTT
  - Subscribes to MQTT commands and translates them to OCPP operations
  - Serves a React UI with live status and event history

Files are extracted by build.sh from:
  https://github.com/lbbrhzn/ocpp
"""

import os
import sys
import logging
import asyncio
import json
import ssl
import time
from datetime import datetime, timezone
from collections import deque

from aiohttp import web

# Local library files (extracted from lbbrhzn/ocpp)
# and our custom bridge wrapper
from ocpp.charge_point import (
    ChargePoint as OcppChargePoint,
)
from ocpp.central_system import (
    CentralSystem as OcppCentralSystem,
)

# Shared MQTT helpers (same pattern as other docker-iot containers)
from mqtt_connect import build_mqtt_context

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

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
        _LOGGER.warning("Env var %s='%s' is not a valid int, using default %s", key, val, default)
        return default

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

# OCPP Central System config
OCPP_HOST = _env_str("OCPP_HOST", "0.0.0.0")
OCPP_PORT = _env_int("OCPP_PORT", 9000)
OCPP_WS_PATH = _env_str("OCPP_WS_PATH", "/{charge_point_id}")

# Web UI / Debug API port
UI_PORT = _env_int("UI_PORT", 9094)

# MQTT config — handled by mqtt_connect module
MQTT_THING_NAME = _env_str("MQTT_THING_NAME", "gormantec-ocpp-bridge")
MQTT_BROKER_VAR = _env_str("MQTT_BROKER", "docker-iot_server")

# Service start timestamp
STARTED_AT = datetime.now(timezone.utc)

# Event ring buffer (last N events for the UI)
MAX_EVENTS = 200
_event_buffer: deque = deque(maxlen=MAX_EVENTS)

# Per-charge-point state (for the UI debug endpoint)
_cp_state: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# MQTT topic helpers
# ---------------------------------------------------------------------------

def _cp_topic(charge_point_id: str, suffix: str) -> str:
    """Build an MQTT topic for a specific charge point."""
    return f"ocpp/{charge_point_id}/{suffix}"

# ---------------------------------------------------------------------------
# Event tracking
# ---------------------------------------------------------------------------

def _record_event(charge_point_id: str, event_type: str, summary: str = ""):
    """Record an event in the ring buffer."""
    event = {
        "time": datetime.now(timezone.utc).isoformat(),
        "charge_point_id": charge_point_id,
        "type": event_type,
        "summary": summary,
    }
    _event_buffer.append(event)

    # Update per-charge-point state
    if charge_point_id not in _cp_state:
        _cp_state[charge_point_id] = {
            "id": charge_point_id,
            "connected": True,
            "status": "unknown",
            "connector_id": None,
            "last_event": None,
        }
    _cp_state[charge_point_id]["last_event"] = event["time"]

# ---------------------------------------------------------------------------
# OCPP Bridge Central System
# ---------------------------------------------------------------------------

class OcppBridgeCentralSystem(OcppCentralSystem):
    """
    Extended CentralSystem that forwards all charge point events to MQTT
    and handles incoming MQTT commands by translating them to OCPP calls.
    """

    def __init__(self, mqtt_client):
        super().__init__()
        self._mqtt = mqtt_client
        self._charge_points: dict[str, OcppChargePoint] = {}

    # ---- OCPP → MQTT: Forward charge point messages to MQTT ----

    async def _publish_cp_event(self, charge_point_id: str, event_type: str, payload: dict):
        """Publish a charge point event to MQTT."""
        topic = _cp_topic(charge_point_id, event_type)
        try:
            await self._mqtt.publish(topic, json.dumps(payload).encode(), qos=1)
            _LOGGER.debug("MQTT publish → %s: %s", topic, payload)
        except Exception as e:
            _LOGGER.error("MQTT publish failed for %s: %s", topic, e)

    @staticmethod
    def _summarize(payload: dict, max_len: int = 80) -> str:
        """Create a short human-readable summary from an OCPP payload."""
        try:
            return json.dumps(payload, default=str)
        except Exception:
            return str(payload)[:max_len]

    # ---- OCPP message handlers (override to add MQTT forwarding) ----

    async def on_boot_notification(self, charge_point_id: str, **kwargs):
        _LOGGER.info("BootNotification from %s: %s", charge_point_id, kwargs)
        _record_event(charge_point_id, "boot_notification",
                      f"vendor={kwargs.get('charge_point_vendor','?')} "
                      f"model={kwargs.get('charge_point_model','?')}")
        await self._publish_cp_event(charge_point_id, "boot_notification", kwargs)
        return await super().on_boot_notification(charge_point_id, **kwargs)

    async def on_heartbeat(self, charge_point_id: str, **kwargs):
        _LOGGER.debug("Heartbeat from %s", charge_point_id)
        _record_event(charge_point_id, "heartbeat", "")
        await self._publish_cp_event(charge_point_id, "heartbeat", kwargs)
        return await super().on_heartbeat(charge_point_id, **kwargs)

    async def on_status_notification(self, charge_point_id: str, **kwargs):
        status = kwargs.get("status", "unknown")
        connector_id = kwargs.get("connector_id")
        error_code = kwargs.get("error_code", "")
        _LOGGER.info("StatusNotification from %s: status=%s connector=%s error=%s",
                      charge_point_id, status, connector_id, error_code)

        summary = f"status={status}"
        if connector_id is not None:
            summary += f" connector={connector_id}"
        if error_code and error_code != "NoError":
            summary += f" error={error_code}"

        _record_event(charge_point_id, "status_notification", summary)

        # Update per-charge-point state
        if charge_point_id in _cp_state:
            _cp_state[charge_point_id]["status"] = status
            if connector_id is not None:
                _cp_state[charge_point_id]["connector_id"] = connector_id

        await self._publish_cp_event(charge_point_id, "status_notification", kwargs)
        return await super().on_status_notification(charge_point_id, **kwargs)

    async def on_authorize(self, charge_point_id: str, **kwargs):
        id_tag = kwargs.get("id_tag", "?")
        _LOGGER.info("Authorize from %s: id_tag=%s", charge_point_id, id_tag)
        _record_event(charge_point_id, "authorize", f"id_tag={id_tag}")
        await self._publish_cp_event(charge_point_id, "authorize", kwargs)
        return await super().on_authorize(charge_point_id, **kwargs)

    async def on_start_transaction(self, charge_point_id: str, **kwargs):
        id_tag = kwargs.get("id_tag", "?")
        connector_id = kwargs.get("connector_id")
        meter_start = kwargs.get("meter_start")
        _LOGGER.info("StartTransaction from %s: id_tag=%s connector=%s meter_start=%s",
                      charge_point_id, id_tag, connector_id, meter_start)
        _record_event(charge_point_id, "start_transaction",
                      f"id_tag={id_tag} meter_start={meter_start}")

        if charge_point_id in _cp_state:
            _cp_state[charge_point_id]["status"] = "Charging"

        await self._publish_cp_event(charge_point_id, "start_transaction", kwargs)
        return await super().on_start_transaction(charge_point_id, **kwargs)

    async def on_stop_transaction(self, charge_point_id: str, **kwargs):
        meter_stop = kwargs.get("meter_stop")
        reason = kwargs.get("reason", "?")
        _LOGGER.info("StopTransaction from %s: meter_stop=%s reason=%s",
                      charge_point_id, meter_stop, reason)
        _record_event(charge_point_id, "stop_transaction",
                      f"meter_stop={meter_stop} reason={reason}")

        if charge_point_id in _cp_state:
            _cp_state[charge_point_id]["status"] = "Available"

        await self._publish_cp_event(charge_point_id, "stop_transaction", kwargs)
        return await super().on_stop_transaction(charge_point_id, **kwargs)

    async def on_meter_values(self, charge_point_id: str, **kwargs):
        _LOGGER.debug("MeterValues from %s: %s", charge_point_id, kwargs)
        # Don't record every meter value as an event (too noisy)
        # But still publish to MQTT
        await self._publish_cp_event(charge_point_id, "meter_values", kwargs)
        return await super().on_meter_values(charge_point_id, **kwargs)

    async def on_data_transfer(self, charge_point_id: str, **kwargs):
        _LOGGER.info("DataTransfer from %s: %s", charge_point_id, kwargs)
        _record_event(charge_point_id, "data_transfer",
                      f"vendor={kwargs.get('vendor_id','?')} "
                      f"msg={kwargs.get('message_id','?')}")
        await self._publish_cp_event(charge_point_id, "data_transfer", kwargs)
        return await super().on_data_transfer(charge_point_id, **kwargs)

    async def on_firmware_status_notification(self, charge_point_id: str, **kwargs):
        _LOGGER.info("FirmwareStatusNotification from %s: %s", charge_point_id, kwargs)
        _record_event(charge_point_id, "firmware_status",
                      f"status={kwargs.get('status','?')}")
        await self._publish_cp_event(charge_point_id, "firmware_status", kwargs)
        return await super().on_firmware_status_notification(charge_point_id, **kwargs)

    async def on_diagnostics_status_notification(self, charge_point_id: str, **kwargs):
        _LOGGER.info("DiagnosticsStatusNotification from %s: %s", charge_point_id, kwargs)
        _record_event(charge_point_id, "diagnostics_status",
                      f"status={kwargs.get('status','?')}")
        await self._publish_cp_event(charge_point_id, "diagnostics_status", kwargs)
        return await super().on_diagnostics_status_notification(charge_point_id, **kwargs)

    # ---- Connection lifecycle ----

    async def on_connect(self, websocket, charge_point_id: str):
        _LOGGER.info("Charge point connected: %s", charge_point_id)
        _record_event(charge_point_id, "connected", f"Charge point connected")
        _cp_state[charge_point_id] = {
            "id": charge_point_id,
            "connected": True,
            "status": "unknown",
            "connector_id": None,
            "last_event": datetime.now(timezone.utc).isoformat(),
        }
        await super().on_connect(websocket, charge_point_id)

    async def on_disconnect(self, websocket, charge_point_id: str):
        _LOGGER.info("Charge point disconnected: %s", charge_point_id)
        _record_event(charge_point_id, "disconnected", f"Charge point disconnected")
        if charge_point_id in _cp_state:
            _cp_state[charge_point_id]["connected"] = False
        await super().on_disconnect(websocket, charge_point_id)

    # ---- MQTT → OCPP: Handle incoming commands from MQTT ----

    async def handle_mqtt_command(self, charge_point_id: str, payload: bytes):
        """
        Process an incoming MQTT command and translate it to OCPP.
        Expected payload format:
          {"action": "RemoteStartTransaction", "params": {...}}
          {"action": "RemoteStopTransaction", "params": {...}}
          {"action": "Reset", "params": {"type": "Hard"}}
          {"action": "UnlockConnector", "params": {...}}
          {"action": "GetConfiguration", "params": {...}}
          {"action": "ChangeConfiguration", "params": {"key": "...", "value": "..."}}
          {"action": "ClearCache", "params": {}}
          {"action": "TriggerMessage", "params": {"requested_message": "..."}}
        """
        try:
            msg = json.loads(payload)
            action = msg.get("action")
            params = msg.get("params", {})

            cp = self._charge_points.get(charge_point_id)
            if not cp:
                _LOGGER.warning("No charge point connected with id: %s", charge_point_id)
                return

            _LOGGER.info("MQTT → OCPP: %s → %s %s", charge_point_id, action, params)
            _record_event(charge_point_id, "cmd_received", f"action={action}")

            if action == "RemoteStartTransaction":
                result = await cp.remote_start_transaction(**params)
                await self._publish_cp_event(charge_point_id, "cmd_result", {
                    "action": action, "result": result
                })

            elif action == "RemoteStopTransaction":
                result = await cp.remote_stop_transaction(**params)
                await self._publish_cp_event(charge_point_id, "cmd_result", {
                    "action": action, "result": result
                })

            elif action == "Reset":
                result = await cp.reset(**params)
                await self._publish_cp_event(charge_point_id, "cmd_result", {
                    "action": action, "result": result
                })

            elif action == "UnlockConnector":
                result = await cp.unlock_connector(**params)
                await self._publish_cp_event(charge_point_id, "cmd_result", {
                    "action": action, "result": result
                })

            elif action == "GetConfiguration":
                result = await cp.get_configuration(**params)
                await self._publish_cp_event(charge_point_id, "cmd_result", {
                    "action": action, "result": result
                })

            elif action == "ChangeConfiguration":
                result = await cp.change_configuration(**params)
                await self._publish_cp_event(charge_point_id, "cmd_result", {
                    "action": action, "result": result
                })

            elif action == "ClearCache":
                result = await cp.clear_cache(**params)
                await self._publish_cp_event(charge_point_id, "cmd_result", {
                    "action": action, "result": result
                })

            elif action == "TriggerMessage":
                result = await cp.trigger_message(**params)
                await self._publish_cp_event(charge_point_id, "cmd_result", {
                    "action": action, "result": result
                })

            else:
                _LOGGER.warning("Unknown MQTT command action: %s", action)

        except json.JSONDecodeError as e:
            _LOGGER.error("Invalid JSON in MQTT command: %s", e)
        except Exception as e:
            _LOGGER.error("Error handling MQTT command: %s", e)


# ---------------------------------------------------------------------------
# MQTT Listener
# ---------------------------------------------------------------------------

async def mqtt_listener(cs: OcppBridgeCentralSystem):
    """Subscribe to MQTT command topics for all known charge points."""
    _LOGGER.info("Starting MQTT listener...")

    async with cs._mqtt as client:
        # Subscribe to wildcard command topic for all charge points
        cmd_wildcard = "ocpp/+/cmd"
        await client.subscribe(cmd_wildcard)
        _LOGGER.info("Subscribed to MQTT topic: %s", cmd_wildcard)

        async for message in client.messages:
            topic = str(message.topic)
            # Parse charge_point_id from topic: ocpp/{id}/cmd
            parts = topic.split("/")
            if len(parts) >= 3 and parts[2] == "cmd":
                charge_point_id = parts[1]
                await cs.handle_mqtt_command(charge_point_id, message.payload)


# ---------------------------------------------------------------------------
# Web UI / Debug API (aiohttp)
# ---------------------------------------------------------------------------

async def handle_debug(request):
    """GET /debug - Full state for the React UI."""
    now = datetime.now(timezone.utc)
    uptime = (now - STARTED_AT).total_seconds()

    charge_points = list(_cp_state.values())

    # Sort: connected first, then by last event time
    charge_points.sort(key=lambda cp: (
        not cp.get("connected", False),
        cp.get("last_event") or "",
    ))

    recent_events = list(_event_buffer)
    recent_events.reverse()  # newest first

    return web.json_response({
        "timestamp": now.isoformat(),
        "uptime_seconds": int(uptime),
        "started_at": STARTED_AT.isoformat(),
        "mqtt_broker": MQTT_BROKER_VAR,
        "mqtt_thing_name": MQTT_THING_NAME,
        "ocpp_port": OCPP_PORT,
        "ui_port": UI_PORT,
        "charge_points": charge_points,
        "recent_events": recent_events[:50],  # limit to 50 most recent
    })


async def handle_health(request):
    """GET /health - Simple health check."""
    return web.json_response({"status": "ok"})


async def handle_index(request):
    """Serve index.html for SPA routing."""
    ui_dist = os.path.join(os.path.dirname(__file__), '..', 'ui', 'dist')
    index_path = os.path.join(ui_dist, 'index.html')
    if os.path.exists(index_path):
        return web.FileResponse(index_path)
    # Fallback to debug JSON if UI not built
    return await handle_debug(request)


async def start_web_server():
    """Start the aiohttp HTTP server with UI and debug API."""
    app = web.Application()

    # API routes
    app.router.add_get('/debug', handle_debug)
    app.router.add_get('/health', handle_health)

    # Static files for React UI
    ui_dist = os.path.join(os.path.dirname(__file__), '..', 'ui', 'dist')
    if os.path.exists(ui_dist):
        # Serve static assets
        assets_dir = os.path.join(ui_dist, 'assets')
        if os.path.exists(assets_dir):
            app.router.add_static('/assets', assets_dir)
        # SPA fallback - serve index.html for all other routes
        app.router.add_get('/', handle_index)
        app.router.add_get('/{path:.*}', handle_index)
        _LOGGER.info("Serving UI from %s", ui_dist)
    else:
        app.router.add_get('/', handle_debug)
        _LOGGER.info("UI not built - serving debug JSON at /")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', UI_PORT)
    await site.start()
    _LOGGER.info("Web UI listening on http://0.0.0.0:%d/", UI_PORT)


# ---------------------------------------------------------------------------
# Main server entry point
# ---------------------------------------------------------------------------

async def main():
    """Start OCPP Central System WebSocket server + MQTT bridge + Web UI."""
    _LOGGER.info("Starting OCPP-MQTT Bridge...")
    _LOGGER.info("OCPP CSMS → %s:%d%s", OCPP_HOST, OCPP_PORT, OCPP_WS_PATH)
    _LOGGER.info("Web UI → http://0.0.0.0:%d/", UI_PORT)

    # Build MQTT client connection
    mqtt_client = build_mqtt_context(MQTT_THING_NAME)

    # Create the bridge central system
    cs = OcppBridgeCentralSystem(mqtt_client)

    try:
        # Run MQTT listener, OCPP WebSocket server, and Web UI concurrently
        async with asyncio.TaskGroup() as tg:
            tg.create_task(mqtt_listener(cs))
            tg.create_task(cs.start(OCPP_HOST, OCPP_PORT))
            tg.create_task(start_web_server())
    except Exception as e:
        _LOGGER.error("Server error: %s", e)
        raise


if __name__ == "__main__":
    asyncio.run(main())
