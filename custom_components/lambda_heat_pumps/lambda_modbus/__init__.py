"""A Lambda heat pump controller, as an object over Modbus.

This package talks to the controller and nothing else — it has no Home Assistant
import, takes a :class:`modbus_connection.ModbusUnit` rather than a host or a
connection, and is tested against the in-memory mock backend. It lives inside the
integration for now because Home Assistant can only load what it ships; it is
shaped to be lifted out into its own PyPI package unchanged, which is what Core
would require.

    from modbus_connection.tmodbus import connect_tcp
    from lambda_modbus import LambdaHeatPump

    connection = await connect_tcp("192.168.1.50", port=502)
    try:
        controller = LambdaHeatPump(connection.for_unit(1), num_hps=2)
        await controller.async_setup()
        await controller.async_update()
        print(controller.ambient.temperature)
        print(controller.heat_pumps[0].flow_line_temperature)
    finally:
        await connection.close()

A controller's register map depends on its firmware: it serves a subset of the
registers a module could have, and refuses a block read that reaches a register
it does not serve — which, read atomically, would take the served registers
around it down too. So the layout is not declared and read; it is *probed*.
:meth:`LambdaHeatPump.async_setup` reads each module a run at a time, drops to
one register at a time on a run the controller refuses, and builds each module
from the registers it actually answered for. What it does not serve simply is not
read, and its entities read ``None``.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from modbus_connection import ModbusExceptionError
from modbus_connection.model import ManualComponent

from .boiler import Boiler
from .buffer import Buffer
from .general import Ambient, EManager
from .heat_pump import HeatPump, HeatPumpLowFirst
from .heating_circuit import HeatingCircuit
from .model import LambdaComponent
from .ranges import (
    AMBIENT_RANGES,
    E_MANAGER_RANGES,
    Range,
    base_address,
    module_ranges,
)
from .solar import Solar, SolarLowFirst

if TYPE_CHECKING:
    from modbus_connection import ModbusUnit, WordOrder

__all__ = [
    "Ambient",
    "Boiler",
    "Buffer",
    "EManager",
    "HeatPump",
    "HeatingCircuit",
    "LambdaComponent",
    "LambdaHeatPump",
    "LambdaManualComponent",
    "Solar",
]


class LambdaManualComponent(ManualComponent):
    """A module built from the registers the controller actually serves.

    A :class:`modbus_connection.model.ManualComponent` reached by attribute, so
    the rest of the integration reads a value as ``component.flow_line_temperature``
    (the field key) just as it did off the typed component it replaces. A key the
    controller did not serve was never added, so it reads ``None``.
    """

    # Every field the module could have, keyed by name, whether or not this
    # controller serves it. The served subset is what is read; this is for
    # metadata that does not depend on serving, like a state field's enum labels.
    declared_fields: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        # __getattr__ runs only for names normal lookup misses, so the real
        # methods and private state are untouched; a leading underscore is never
        # a field key, so let those raise rather than resolve to a None value.
        if name.startswith("_"):
            raise AttributeError(name)
        return self.get(name)


async def _probe_served(unit: ModbusUnit, ranges: tuple[Range, ...]) -> set[int]:
    """The addresses in ``ranges`` the controller answers for.

    Each run is tried as one block read; a run the controller refuses is retried
    one register at a time, so the served registers in it are still found. Only a
    Modbus *exception* (a refusal) is caught — a timeout or a dropped link is not
    an answer about the register map and propagates, so setup fails and retries.
    """
    served: set[int] = set()
    for low, high in ranges:
        try:
            await unit.read_holding_registers(low, high - low + 1)
        except ModbusExceptionError:
            for address in range(low, high + 1):
                try:
                    await unit.read_holding_registers(address, 1)
                except ModbusExceptionError:
                    continue  # a register the controller does not serve
                served.add(address)
        else:
            served.update(range(low, high + 1))
    return served


class LambdaHeatPump:
    """A Lambda controller with the modules that are installed on it.

    `word_order` is how the controller lays out its 32-bit counters across two
    registers: `"big"` (the default, high word first) or `"little"`. It varies by
    controller, which is why it is configurable rather than modelled.

    Construct, then :meth:`async_setup` once to probe the register map, then
    :meth:`async_update` on a schedule.
    """

    def __init__(
        self,
        unit: ModbusUnit,
        *,
        num_hps: int = 1,
        num_boil: int = 1,
        num_buff: int = 0,
        num_sol: int = 0,
        num_hc: int = 1,
        word_order: WordOrder = "big",
    ) -> None:
        self._unit = unit
        self._word_order = word_order
        self._counts = {
            "hp": num_hps,
            "boil": num_boil,
            "buff": num_buff,
            "sol": num_sol,
            "hc": num_hc,
        }

        # Populated by async_setup; declared here so the attributes always exist.
        self.ambient: LambdaManualComponent
        self.e_manager: LambdaManualComponent
        self.heat_pumps: list[LambdaManualComponent] = []
        self.boilers: list[LambdaManualComponent] = []
        self.buffers: list[LambdaManualComponent] = []
        self.solar_modules: list[LambdaManualComponent] = []
        self.heating_circuits: list[LambdaManualComponent] = []

    async def async_setup(self) -> None:
        """Probe the controller and build each module from what it serves."""
        heat_pump_class = HeatPump if self._word_order == "big" else HeatPumpLowFirst
        solar_class = Solar if self._word_order == "big" else SolarLowFirst

        self.ambient = await self._build(Ambient, 0, AMBIENT_RANGES)
        self.e_manager = await self._build(EManager, 0, E_MANAGER_RANGES)
        self.heat_pumps = await self._build_all(heat_pump_class, "hp")
        self.boilers = await self._build_all(Boiler, "boil")
        self.buffers = await self._build_all(Buffer, "buff")
        self.solar_modules = await self._build_all(solar_class, "sol")
        self.heating_circuits = await self._build_all(HeatingCircuit, "hc")

    async def _build_all(
        self, component_class: type[LambdaComponent], module: str
    ) -> list[LambdaManualComponent]:
        """One component per installed module, each at its own 100-register block."""
        return [
            await self._build(
                component_class, base_address(module, index), module_ranges(module)
            )
            for index in range(1, self._counts[module] + 1)
        ]

    async def _build(
        self,
        component_class: type[LambdaComponent],
        base: int,
        relative_ranges: tuple[Range, ...],
    ) -> LambdaManualComponent:
        """Probe one module's runs and add the fields it answers for.

        A field is added only when the controller serves every register it spans,
        with its address shifted from the layout-relative one the class declares
        to the absolute one this module sits at.
        """
        ranges = tuple((base + low, base + high) for low, high in relative_ranges)
        served = await _probe_served(self._unit, ranges)

        component = LambdaManualComponent(self._unit, holding_ranges=ranges)
        component.declared_fields = component_class._register_fields
        for name, field in component_class._register_fields.items():
            address = base + field.address
            if all(address + offset in served for offset in range(field.count)):
                absolute = copy.copy(field)
                absolute.address = address
                component.add(name, absolute, space="holding")
        return component

    @property
    def components(self) -> tuple[LambdaManualComponent, ...]:
        """Every sub-system that is polled."""
        return (
            self.ambient,
            self.e_manager,
            *self.heat_pumps,
            *self.boilers,
            *self.buffers,
            *self.solar_modules,
            *self.heating_circuits,
        )

    async def async_update(self) -> None:
        """Refresh every sub-system.

        Each module is read on its own, so they are independent: one that stops
        answering raises, and the caller decides what that means, without the
        others' reads riding on it.
        """
        for component in self.components:
            await component.async_update()
