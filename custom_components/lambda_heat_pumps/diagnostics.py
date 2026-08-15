"""Diagnostics for the Lambda Heat Pumps integration.

The download dumps the controller's raw registers — the undecoded words, exactly
as they come off the wire. That is what makes a diagnostics download worth having
here: a value that reads wrong in Home Assistant can be checked against the
datasheet without a Modbus tool, and the dump replays straight into the mock
backend, so a bug report can back a regression test with no hardware.

The device object reads them, over the same plan a poll uses, so this asks the
controller for exactly what the integration asks it for and nothing else.

Alongside it goes the layout the model resolved to: which address each field was
read from, so a raw word in the dump can be tied to the entity that reports it,
and a field the probe found unserved is visible by its absence — and what the
last poll made of the controller, so a stale value has something to be read
against.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from modbus_connection import ModbusError

from .const import CONF_HOST, MODULES
from .coordinator import LambdaConfigEntry

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
        # What the last poll came back with, so an entity holding a stale value
        # can be tied to the sub-system that stopped answering.
        "poll": {
            "updated": sorted(coordinator.updated),
            "failed": {name: str(err) for name, err in coordinator.failed.items()},
        },
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
    # Polled apart from the modules, so not in MODULES; still worth dumping.
    for index, component in enumerate(device.capacity_limits, 1):
        components[f"hp{index}_capacity_limits"] = component
    return {
        name: {
            field: resolved.address
            for field, resolved in component.resolved_fields.items()
        }
        for name, component in components.items()
    }


async def _async_read_registers(coordinator) -> dict[str, Any]:
    """The controller's raw registers, by address space and then address.

    Read fresh, so it is the controller as it stands at download time, and over
    the read plan setup settled: a register the probe found it does not serve
    was dropped from the plan, so it is absent here for the same reason its
    entity reads as unknown.
    """
    try:
        return await coordinator.device.async_read_raw()
    except ModbusError as err:
        # A download that says why it could not read the controller is worth
        # more than one that fails and leaves the user with nothing.
        return {"error": str(err)}
