"""Module auto-detection utilities for Lambda Heat Pumps integration."""

from __future__ import annotations

import logging
import asyncio

from typing import TYPE_CHECKING

from modbus_connection import ModbusError

from .lambda_modbus.ranges import base_address

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from modbus_connection import ModbusUnit

_LOGGER = logging.getLogger(__name__)

# Maximum expected modules per type
MAX_MODULE_COUNTS = {
    "hp": 3,
    "boil": 5,
    "buff": 5,
    "sol": 2,
    "hc": 12,
}

# A module's first register is its error number; a module that is not installed
# does not answer for it.
_PROBE_OFFSET = 0
_PROBE_TIMEOUT = 2.0
_DETECT_TIMEOUT = 15.0


async def auto_detect_modules(unit: ModbusUnit, slave_id: int = 0) -> dict[str, int]:
    """
    Automatically detect installed modules by testing register accessibility.

    Args:
        unit: The Modbus unit handle for the controller.
        slave_id: Unused; the unit is already bound to the station address.

    Returns:
        Dict with detected module counts: {
            "hp": 1, "boil": 1, "hc": 2, "buff": 0, "sol": 0
        }

    """
    async def _auto_detect_internal():
        detected = {"hp": 0, "boil": 0, "buff": 0, "sol": 0, "hc": 0}

        for module_type, max_count in MAX_MODULE_COUNTS.items():
            _LOGGER.debug("Testing %s modules (max: %s)", module_type, max_count)

            for index in range(1, max_count + 1):
                probe = base_address(module_type, index) + _PROBE_OFFSET
                try:
                    await asyncio.wait_for(
                        unit.read_holding_registers(probe, 1), timeout=_PROBE_TIMEOUT
                    )
                except (ModbusError, asyncio.TimeoutError) as ex:
                    # The controller refused or did not answer — no such module,
                    # and none beyond it either.
                    _LOGGER.debug(
                        "No %s module %s at address %s: %s", module_type, index, probe, ex
                    )
                    break

                detected[module_type] = index
                _LOGGER.debug(
                    "Detected %s module %s at address %s", module_type, index, probe
                )

        # Ensure minimum counts for critical modules
        if detected["hp"] == 0:
            detected["hp"] = 1  # Always assume at least 1 heat pump
            _LOGGER.info("No heat pump detected, assuming 1 (minimum required)")

        _LOGGER.info("Auto-detected modules: %s", detected)
        return detected

    try:
        return await asyncio.wait_for(_auto_detect_internal(), timeout=_DETECT_TIMEOUT)
    except asyncio.TimeoutError:
        _LOGGER.warning("Auto-detection timed out after 15 seconds, using fallback values")
        return {"hp": 1, "boil": 1, "buff": 0, "sol": 0, "hc": 1}  # Fallback-Werte


async def update_entry_with_detected_modules(
    hass: HomeAssistant, entry: ConfigEntry, detected_modules: dict
) -> bool:
    """
    Update config entry with auto-detected module counts.

    Args:
        hass: HomeAssistant instance
        entry: Config entry to update
        detected_modules: Dict with detected module counts

    Returns:
        True if entry was updated, False if no changes needed

    """
    current_data = dict(entry.data)
    updated = False

    for module_type, count in detected_modules.items():
        key = f"num_{module_type}s" if module_type == "hp" else f"num_{module_type}"
        if module_type == "hc":
            key = "num_hc"

        current_count = current_data.get(key, 0)
        if current_count != count:
            current_data[key] = count
            updated = True
            _LOGGER.info("Updated %s from %s to %s", key, current_count, count)

    if updated:
        hass.config_entries.async_update_entry(entry, data=current_data)
        _LOGGER.info("Config entry updated with auto-detected module counts")
        return True

    _LOGGER.debug("No module count changes needed")
    return False
