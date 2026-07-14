"""Modbus configuration and connection readiness for the Lambda integration.

Reading, decoding and writing registers is the job of modbus-connection and the
lambda_modbus device library. What is left here is the one Modbus setting the
user configures themselves — how the controller orders the two registers of a
32-bit value — and waiting for the controller to start answering at all.
"""

import asyncio
import logging

from modbus_connection import ModbusError

_LOGGER = logging.getLogger(__name__)

# A cold-started Home Assistant often reaches the controller before the
# controller is ready to answer.
_STABLE_CONNECTION_ATTEMPTS = 10
_HEALTH_CHECK_TIMEOUT = 2


async def wait_for_stable_connection(coordinator) -> None:
    """Wait until the controller answers a read, or give up after ~10 tries.

    Reads register 0 (the general error number) as a liveness check. Returns
    either way — the caller proceeds regardless, so a device that is merely slow
    to come up does not block setup forever.
    """
    for attempt in range(1, _STABLE_CONNECTION_ATTEMPTS + 1):
        if await _connection_is_healthy(coordinator):
            _LOGGER.debug("CONNECTION: Stable after %d attempt(s)", attempt)
            return
        _LOGGER.debug(
            "CONNECTION: Not answering yet, attempt %d/%d",
            attempt, _STABLE_CONNECTION_ATTEMPTS,
        )
        await asyncio.sleep(1)

    _LOGGER.warning(
        "CONNECTION: Controller did not answer after %d attempts, proceeding anyway",
        _STABLE_CONNECTION_ATTEMPTS,
    )


async def _connection_is_healthy(coordinator) -> bool:
    """Whether the controller answers a single-register read right now."""
    if coordinator.unit is None:
        return False
    try:
        await asyncio.wait_for(
            coordinator.unit.read_holding_registers(0, 1), timeout=_HEALTH_CHECK_TIMEOUT
        )
    except (ModbusError, asyncio.TimeoutError) as err:
        _LOGGER.debug("CONNECTION: Health check failed: %s", err)
        return False
    return True


async def get_int32_register_order(hass, entry=None) -> str:
    """
    Lädt Register-Reihenfolge-Konfiguration aus lambda_wp_config.yaml.

    Es handelt sich um die Reihenfolge der 16-Bit-Register bei 32-Bit-Werten
    (Register/Word Order), nicht um Byte-Endianness innerhalb eines Registers.

    Args:
        hass: Home Assistant Instanz

    Returns:
        str: "high_first" oder "low_first" (Standard: "high_first")

    Note:
        "high_first" = Höherwertiges Register zuerst (Register[0] enthält MSW)
        "low_first" = Niedrigwertiges Register zuerst (Register[0] enthält LSW)

        Rückwärtskompatibilität: "big" wird zu "high_first", "little" zu "low_first" konvertiert
    """
    try:
        from .utils import load_lambda_config, get_firmware_version
        from .const_base import FIRMWARE_CONFIG, DEFAULT_FIRMWARE
        config = await load_lambda_config(hass)
        modbus_config = config.get("modbus", {})

        # Prüfe zuerst neue Config, dann alte (für Rückwärtskompatibilität)
        register_order = modbus_config.get("int32_register_order")
        if register_order is None:
            # Rückwärtskompatibilität: Alte Config migrieren
            old_byte_order = modbus_config.get("int32_byte_order")
            if old_byte_order is not None:
                _LOGGER.info(
                    "Migration: int32_byte_order gefunden, verwende Wert für int32_register_order. "
                    "Bitte migrieren Sie Ihre Config zu modbus.int32_register_order"
                )
                register_order = old_byte_order
            else:
                register_order = "high_first"  # Standard

        # Rückwärtskompatibilität: Konvertiere alte Werte
        if register_order == "big":
            register_order = "high_first"
            _LOGGER.info(
                "Veralteter Wert 'big' verwendet. Bitte aktualisieren Sie Ihre Config auf 'high_first'"
            )
        elif register_order == "little":
            register_order = "low_first"
            _LOGGER.info(
                "Veralteter Wert 'little' verwendet. Bitte aktualisieren Sie Ihre Config auf 'low_first'"
            )

        # Validiere Wert
        if register_order not in ["high_first", "low_first"]:
            _LOGGER.warning("Ungültige int32_register_order: %s, verwende 'high_first'", register_order)
            return "high_first"

        return register_order

    except Exception as e:
        _LOGGER.warning("Fehler beim Laden der Register-Reihenfolge-Konfiguration: %s", e)
        return "high_first"  # Sicherer Fallback auf aktuelles Verhalten
