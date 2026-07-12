#!/bin/bash

# Start the OCPP-to-MQTT bridge server (handles OCPP WS + MQTT + Web UI)
cd /usr/src/app
exec python server.py
