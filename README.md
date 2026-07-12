# OCPP-to-MQTT Bridge

A containerised Python service that bridges EV chargers using the **Open Charge Point Protocol (OCPP)** to the **docker-iot MQTT** broker. It acts as an OCPP Central System (CSMS) that charge points connect to via WebSocket, and translates all OCPP messages to/from MQTT topics.

Built on top of the [lbbrhzn/ocpp](https://github.com/lbbrhzn/ocpp) Home Assistant library, extracted and patched to run standalone without any Home Assistant dependencies.

## Purpose

This service allows you to integrate OCPP-compliant EV chargers into the docker-iot ecosystem:

- **OCPP → MQTT**: All charge point events (BootNotification, StatusNotification, MeterValues, StartTransaction, StopTransaction, etc.) are published to MQTT topics
- **MQTT → OCPP**: Send OCPP commands (RemoteStartTransaction, RemoteStopTransaction, Reset, UnlockConnector, etc.) via MQTT topics

## How It Works

### Architecture

```
┌─────────────────┐     WebSocket (OCPP)     ┌──────────────────────┐     MQTT      ┌──────────────┐
│  EV Charger      │ ◄─────────────────────► │  docker-ocpp-mqtt    │ ◄───────────► │  docker-iot   │
│  (Charge Point)  │                          │  (CSMS + Bridge)     │               │  MQTT Broker  │
└─────────────────┘                          └──────────────────────┘               └──────────────┘
```

1. **OCPP Central System**: The service runs a WebSocket server that EV chargers connect to using the standard OCPP 1.6 JSON protocol
2. **MQTT Bridge**: All OCPP messages are forwarded to structured MQTT topics
3. **Command Relay**: MQTT messages on command topics are translated back to OCPP operations and sent to the charger

### MQTT Topics

All topics are under the `ocpp/` prefix:

| Topic | Direction | Description |
|-------|-----------|-------------|
| `ocpp/{cp_id}/boot_notification` | OCPP → MQTT | Charger boot notification |
| `ocpp/{cp_id}/heartbeat` | OCPP → MQTT | Charger heartbeat |
| `ocpp/{cp_id}/status_notification` | OCPP → MQTT | Connector status changes |
| `ocpp/{cp_id}/meter_values` | OCPP → MQTT | Energy meter readings |
| `ocpp/{cp_id}/start_transaction` | OCPP → MQTT | Charging session started |
| `ocpp/{cp_id}/stop_transaction` | OCPP → MQTT | Charging session stopped |
| `ocpp/{cp_id}/authorize` | OCPP → MQTT | RFID/card authorization |
| `ocpp/{cp_id}/cmd` | MQTT → OCPP | Send commands to charger |
| `ocpp/{cp_id}/cmd_result` | OCPP → MQTT | Command execution results |

### Sending Commands

Publish a JSON message to `ocpp/{charge_point_id}/cmd`:

```json
{
  "action": "RemoteStartTransaction",
  "params": {
    "id_tag": "RFID_TAG_123",
    "connector_id": 1
  }
}
```

Supported actions: `RemoteStartTransaction`, `RemoteStopTransaction`, `Reset`, `UnlockConnector`, `GetConfiguration`, `ChangeConfiguration`, `ClearCache`, `TriggerMessage`

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OCPP_HOST` | `0.0.0.0` | CSMS WebSocket listen address |
| `OCPP_PORT` | `9000` | CSMS WebSocket listen port |
| `OCPP_WS_PATH` | `/{charge_point_id}` | WebSocket path pattern |
| `MQTT_BROKER` | `docker-iot_server` | MQTT broker hostname |
| `MQTT_PORT` | `8883` | MQTT broker port |
| `MQTT_THING_NAME` | `gormantec-ocpp-bridge` | MQTT client identifier |

## Building

```bash
# Build and push via IoT CLI
npm run build:image-force

# Or manually
docker build -t ghcr.io/gormantec/docker-ocpp-mqtt:latest .
docker push ghcr.io/gormantec/docker-ocpp-mqtt:latest
```
