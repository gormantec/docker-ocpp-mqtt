"""
OCPP Bridge — Custom wrapper for the lbbrhzn/ocpp library.

This module provides helper functions and classes that extend the
upstream OCPP library to integrate with the docker-iot MQTT ecosystem.

It patches Home Assistant-specific imports and decorators to work
in a standalone container environment (no HA dependency).
"""

import logging

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stub replacements for Home Assistant imports used by the ocpp library.
#
# The lbbrhzn/ocpp library is designed as a Home Assistant custom component
# and imports from homeassistant.*. We provide compatible no-op stubs so
# the library can run standalone without HA.
# ---------------------------------------------------------------------------

class _StubHomeAssistant:
    """Minimal Home Assistant stub for standalone operation."""
    pass

class _StubConfigEntry:
    """Minimal config entry stub."""
    pass

# Provide stubs for common HA decorators used in the ocpp library
def _stub_callback(func):
    """No-op replacement for @callback decorator."""
    return func

# Make these available as module-level replacements
# (They get injected before the ocpp library is imported)
import sys
import types

_HA_MODULES = {
    "homeassistant": types.ModuleType("homeassistant"),
    "homeassistant.core": types.ModuleType("homeassistant.core"),
    "homeassistant.config_entries": types.ModuleType("homeassistant.config_entries"),
    "homeassistant.const": types.ModuleType("homeassistant.const"),
    "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
    "homeassistant.helpers.entity": types.ModuleType("homeassistant.helpers.entity"),
}

for name, mod in _HA_MODULES.items():
    sys.modules[name] = mod

# Set up common HA constants
_HA_MODULES["homeassistant.const"].STATE_OK = "ok"
_HA_MODULES["homeassistant.const"].STATE_UNAVAILABLE = "unavailable"
_HA_MODULES["homeassistant.const"].STATE_UNKNOWN = "unknown"

# Set up ConfigEntry stub
_HA_MODULES["homeassistant.config_entries"].ConfigEntry = _StubConfigEntry

# Set up callback decorator
_HA_MODULES["homeassistant.core"].callback = _stub_callback
_HA_MODULES["homeassistant.core"].HomeAssistant = _StubHomeAssistant
_HA_MODULES["homeassistant.core"].State = type("State", (), {})

_LOGGER.info("OCPP bridge: Home Assistant stubs loaded for standalone operation")
