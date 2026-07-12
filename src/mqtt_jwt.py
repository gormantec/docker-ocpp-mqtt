"""
MQTT JWT Authentication Helper

Generates a JWT signed with the device's RSA private key.
The JWT proves possession of the private key matching the device certificate.
Used as the MQTT password for WSS connections.

The docker-iot broker verifies the JWT using the public key from the
stored device certificate, eliminating the need for a shared password.

JWT Payload:
  sub:  Thing name (MQTT client identifier)
  iat:  Issued-at timestamp (seconds since epoch)
  exp:  Expiration timestamp (iat + JWT_TTL_SECONDS, default 2 hours)
  jti:  JWT ID (UUID4, unique per token — prevents replay)
"""

import time
import json
import uuid
import base64

# JWT is valid for 2 hours — client regenerates on each connection
JWT_TTL_SECONDS = 7200


def create_mqtt_jwt(thing_name: str, private_key_path: str) -> str:
    """
    Create a JWT signed with the device's RSA private key.

    Args:
        thing_name: The MQTT client identifier (thing name)
        private_key_path: Path to the device's PEM private key file

    Returns:
        JWT string (header.payload.signature), or raises on error
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        raise ImportError(
            "cryptography package required for JWT auth. "
            "Install: pip install cryptography"
        )

    try:
        with open(private_key_path, 'rb') as f:
            private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "sub": thing_name,
            "iat": now,
            "exp": now + JWT_TTL_SECONDS,
            "jti": str(uuid.uuid4()),
        }

        header_b64 = _b64url(json.dumps(header).encode())
        payload_b64 = _b64url(json.dumps(payload).encode())
        sign_input = f"{header_b64}.{payload_b64}"

        signature = private_key.sign(
            sign_input.encode('utf-8'),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        signature_b64 = _b64url(signature)

        return f"{sign_input}.{signature_b64}"
    except Exception as e:
        raise RuntimeError(f"Failed to create MQTT JWT: {e}")


def _b64url(data: bytes) -> str:
    """Base64url encode (no padding, URL-safe characters)."""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')
