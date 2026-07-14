"""A Lambda heat pump controller, as an object over Modbus.

This package talks to the controller and nothing else — it has no Home Assistant
import, takes a :class:`modbus_connection.ModbusUnit` rather than a host or a
connection, and is tested against the in-memory mock backend. It lives inside the
integration for now because Home Assistant can only load what it ships; it is
shaped to be lifted out into its own PyPI package unchanged, which is what Core
would require.

    from modbus_connection.pymodbus import connect_tcp
    from lambda_modbus import LambdaHeatPump

    connection = await connect_tcp("192.168.1.50", port=502)
    try:
        controller = LambdaHeatPump(connection.for_unit(1), num_hps=2)
        await controller.async_update()
        print(controller.ambient.temperature)
        print(controller.heat_pumps[0].flow_line_temperature)
    finally:
        await connection.close()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modbus_connection.model import Component, ComponentGroup

from .boiler import Boiler
from .buffer import Buffer
from .general import Ambient, EManager
from .heat_pump import HeatPump, HeatPumpLowFirst
from .heating_circuit import HeatingCircuit
from .model import LambdaComponent
from .ranges import base_address, readable_ranges
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
    "Solar",
]


class LambdaHeatPump:
    """A Lambda controller with the modules that are installed on it.

    `word_order` is how the controller lays out its 32-bit counters across two
    registers: `"big"` (the default, high word first) or `"little"`. It varies by
    controller, which is why it is configurable rather than modelled.
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

        heat_pump_class = HeatPump if word_order == "big" else HeatPumpLowFirst
        solar_class = Solar if word_order == "big" else SolarLowFirst

        self.ambient = Ambient(unit)
        self.e_manager = EManager(unit)
        self.heat_pumps = self._build(heat_pump_class, unit, "hp", num_hps)
        self.boilers = self._build(Boiler, unit, "boil", num_boil)
        self.buffers = self._build(Buffer, unit, "buff", num_buff)
        self.solar_modules = self._build(solar_class, unit, "sol", num_sol)
        self.heating_circuits = self._build(HeatingCircuit, unit, "hc", num_hc)

        # Which registers the controller answers depends on which modules are
        # installed, so the ranges are computed here and pushed onto every
        # component — a ComponentGroup requires its members to agree on them.
        ranges = readable_ranges(
            {
                "hp": num_hps,
                "boil": num_boil,
                "buff": num_buff,
                "sol": num_sol,
                "hc": num_hc,
            }
        )
        for component in self.components:
            component.register_ranges = ranges

        self._group = ComponentGroup(unit, self.components)

    @staticmethod
    def _build[C: LambdaComponent](
        component_class: type[C], unit: ModbusUnit, module: str, count: int
    ) -> list[C]:
        """One component per installed module, each at its own 100-register block."""
        return [
            component_class(unit, index=index, base_offset=base_address(module, index))
            for index in range(1, count + 1)
        ]

    @property
    def components(self) -> tuple[Component, ...]:
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
        """Refresh every sub-system in one pooled set of block reads."""
        await self._group.async_update()
