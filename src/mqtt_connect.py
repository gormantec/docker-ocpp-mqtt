"""
Shared MQTT connection helper for docker-iot containers.

Certs come from base64-encoded environment variables (from Secrets Manager).
No cert files on disk — decoded to temp files with 400 perms, cleaned on exit.

Env vars:
  MQTT_BROKER          (default: docker-iot_server)
  MQTT_PORT            (default: 8883)
  MQTT_TRANSPORT       tcp | websockets (default: tcp)
  MQTT_CA_CERT_B64     Base64-encoded CA certificate PEM
  MQTT_CLIENT_CERT_B64 Base64-encoded client certificate PEM
  MQTT_CLIENT_KEY_B64  Base64-encoded client private key PEM

Usage:
    from mqtt_connect import build_mqtt_context
    async with build_mqtt_context("my-thing") as client:
        await client.publish("topic", b"payload")
"""

import os
import ssl
import base64
import logging
import tempfile
import atexit
import shutil
import aiomqtt
from mqtt_jwt import create_mqtt_jwt

_LOGGER = logging.getLogger(__name__)

BROKER = os.environ.get("MQTT_BROKER", "").strip() or "docker-iot_server"
PORT = int(os.environ.get("MQTT_PORT", "") or "8883")
TRANSPORT = os.environ.get("MQTT_TRANSPORT", "").strip() or "tcp"
WS_PATH = os.environ.get("MQTT_WS_PATH", "").strip() or "/mqtt"

_CERT_TMPDIR = tempfile.mkdtemp(prefix="iot-certs-")
atexit.register(lambda: shutil.rmtree(_CERT_TMPDIR, ignore_errors=True))


def _b64cert(env_name: str, label: str) -> str:
    """Decode a base64 env var to a temp file with 400 perms. Raises on missing."""
    b64 = os.environ.get(env_name, "")
    if not b64:
        raise RuntimeError(f"Missing required env var: {env_name}")
    path = os.path.join(_CERT_TMPDIR, f"{label}.pem")
    with open(path, "wb") as f:
        os.fchmod(f.fileno(), 0o400)
        f.write(base64.b64decode(b64))
    return path


def build_mqtt_context(thing_name: str):
    """Build an aiomqtt.Client with certs from _B64 env vars."""
    if TRANSPORT == "tcp":
        ca = _b64cert("MQTT_CA_CERT_B64", "ca")
        cert = _b64cert("MQTT_CLIENT_CERT_B64", "cert")
        key = _b64cert("MQTT_CLIENT_KEY_B64", "key")

        tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls_ctx.load_verify_locations(ca)
        tls_ctx.load_cert_chain(certfile=cert, keyfile=key)
        tls_ctx.check_hostname = False

        _LOGGER.info("MQTT/TCP → %s:%d", BROKER, PORT)
        return aiomqtt.Client(
            hostname=BROKER, port=PORT, transport="tcp",
            tls_context=tls_ctx, identifier=thing_name, keepalive=60,
        )

    # WSS mode
    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ca = _b64cert("MQTT_CA_CERT_B64", "ca")
    tls_ctx.load_verify_locations(ca)
    tls_ctx.check_hostname = False

    jwt = ""
    try:
        key = _b64cert("MQTT_CLIENT_KEY_B64", "key")
        jwt = create_mqtt_jwt(thing_name, key)
    except Exception as e:
        _LOGGER.warning("JWT creation failed: %s", e)

    kwargs = dict(hostname=BROKER, port=PORT, transport="websockets",
                  websocket_path=WS_PATH, tls_context=tls_ctx,
                  identifier=thing_name, keepalive=60)
    if jwt:
        kwargs["password"] = jwt
    return aiomqtt.Client(**kwargs)
