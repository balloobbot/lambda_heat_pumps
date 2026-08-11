"""Diagnostics for the Lambda Heat Pumps integration.

The download dumps the controller's raw registers — the undecoded words, block by
block, exactly as they come off the wire. That is what makes a diagnostics
download worth having here: a value that reads wrong in Home Assistant can be
checked against the datasheet without a Modbus tool, and a register the
integration does not model yet can be read straight out of the dump.

Alongside it goes the layout the model resolved to: which address each field was
read from, so a raw word in the dump can be tied to the entity that reports it,
and a field the probe found unserved is visible by its absence.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from modbus_connection import ModbusExceptionError

from .const import CONF_HOST, MODULES
from .coordinator import LambdaConfigEntry
from .lambda_modbus.ranges import readable_ranges

# The host is the one thing here that identifies where the user lives on their
# network; everything else describes the appliance.
TO_REDACT = {CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LambdaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "detected_modules": coordinator.counts,
        "layout": _layout(coordinator),
        "registers": await _async_read_registers(coordinator),
        # What the integration counts for itself, so a wrong cycle or energy
        # figure can be told apart from a wrong register.
        "totals": {
            index: {
                "cycles": totals.cycles,
                "electrical": totals.electrical,
                "thermal": totals.thermal,
            }
            for index, totals in coordinator.totals.items()
        },
    }


def _layout(coordinator) -> dict[str, dict[str, int]]:
    """Where each sub-system's fields were read from, field name -> address.

    Taken from the model rather than restated here, so it is the layout the poll
    actually used: narrowed to the fields the probe found the controller serving,
    and at the addresses each module's block sits at.
    """
    device = coordinator.device
    components = {"ambient": device.ambient, "e_manager": device.e_manager}
    for module, attribute in MODULES.items():
        for index, component in enumerate(getattr(device, attribute), 1):
            components[f"{module}{index}"] = component
    return {
        name: {
            field: resolved.address
            for field, resolved in component.resolved_fields.items()
        }
        for name, component in components.items()
    }


async def _async_read_registers(coordinator) -> dict[str, Any]:
    """The controller's raw holding registers, address -> value.

    Reads the ranges the model polls; a range the controller refuses (a firmware
    that does not serve every register a module could have) is retried one
    register at a time, so the dump shows exactly the registers this controller
    serves rather than stopping at the first it does not.
    """
    registers: dict[int, int] = {}
    for low, high in readable_ranges(coordinator.counts):
        try:
            values = await coordinator.unit.read_holding_registers(low, high - low + 1)
        except ModbusExceptionError:
            await _read_by_register(coordinator, low, high, registers)
        else:
            registers.update(zip(range(low, high + 1), values, strict=True))
    # JSON object keys are strings; keep them numeric-looking and sorted so the
    # dump reads like an address map.
    return {str(address): registers[address] for address in sorted(registers)}


async def _read_by_register(
    coordinator, low: int, high: int, registers: dict[int, int]
) -> None:
    """Read a refused range a register at a time, keeping the served ones."""
    for address in range(low, high + 1):
        try:
            (value,) = await coordinator.unit.read_holding_registers(address, 1)
        except ModbusExceptionError:
            continue  # a register this controller does not serve
        registers[address] = value
