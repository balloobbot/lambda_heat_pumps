"""A Lambda heat pump controller, as an object over Modbus.

This package talks to the controller and nothing else — it has no Home Assistant
import, takes a :class:`modbus_connection.ModbusUnit` rather than a host or a
connection, and is tested against the in-memory mock backend. It lives inside the
integration for now because Home Assistant can only load what it ships; it is
shaped to be lifted out into its own PyPI package unchanged, which is what Core
would require.

    from modbus_connection import ModbusTcpParams
    from modbus_connection.tmodbus import ModbusConnection
    from lambda_modbus import LambdaHeatPump

    connection = ModbusConnection(ModbusTcpParams(host="192.168.1.50", port=502))
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
around it down too. So the layout is declared here but *confirmed* against the
controller. :meth:`LambdaHeatPump.async_setup` reads each module a run at a time,
drops to one register at a time on a run the controller refuses, and narrows each
module to the registers it actually answered for. A register it does not serve is
dropped from that module's read plan, so it is never read and reads as ``None``,
while everything else stays the ordinary typed component with the ordinary
update.

A poll reads each sub-system on its own and returns an :class:`UpdateReport`:
one the controller could not answer for keeps the values it had and is named in
the report, while the rest still refresh. Only the link itself failing raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modbus_connection import (
    IllegalDataAddressError,
    IllegalFunctionError,
    ModbusConnectionError,
    ModbusError,
)

from .boiler import Boiler
from .buffer import Buffer
from .general import Ambient, EManager
from .heat_pump import HeatPump, HeatPumpLowFirst
from .heating_circuit import HeatingCircuit
from .model import LambdaComponent, UpdateReport
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
    "Solar",
    "UpdateReport",
]

# What a controller answers with when a register is not there. Every other
# answer — busy, a failure of its own, a gateway that could not reach it — says
# nothing about the register map, and a register written off over one of those
# would stay unread for the life of the object.
_NOT_SERVED = (IllegalDataAddressError, IllegalFunctionError)


async def _probe_served(unit: ModbusUnit, ranges: tuple[Range, ...]) -> set[int]:
    """The addresses in ``ranges`` the controller answers for.

    Each run is tried as one block read; a run the controller refuses is retried
    one register at a time, so the served registers in it are still found. Only a
    refusal is an answer about the register map — anything else propagates, so
    setup fails and retries rather than narrowing the read plan over a hiccup.
    """
    served: set[int] = set()
    for low, high in ranges:
        try:
            await unit.read_holding_registers(low, high - low + 1)
        except _NOT_SERVED:
            for address in range(low, high + 1):
                try:
                    await unit.read_holding_registers(address, 1)
                except _NOT_SERVED:
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
        self.ambient: Ambient
        self.e_manager: EManager
        self.heat_pumps: list[HeatPump] = []
        self.boilers: list[Boiler] = []
        self.buffers: list[Buffer] = []
        self.solar_modules: list[Solar] = []
        self.heating_circuits: list[HeatingCircuit] = []

        self._polled: dict[str, LambdaComponent] | None = None

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

        # What a poll reads, in read order, named as the report names it. It is
        # also the marker that setup ran: a setup that fails part-way leaves it
        # None, so the next update probes the controller again.
        self._polled = {
            "ambient": self.ambient,
            "e_manager": self.e_manager,
            **{
                f"{module}{index}": component
                for module, components in (
                    ("hp", self.heat_pumps),
                    ("boil", self.boilers),
                    ("buff", self.buffers),
                    ("sol", self.solar_modules),
                    ("hc", self.heating_circuits),
                )
                for index, component in enumerate(components, 1)
            },
        }

    async def _build_all[C: LambdaComponent](
        self, component_class: type[C], module: str
    ) -> list[C]:
        """One component per installed module, each at its own 100-register block."""
        return [
            await self._build(
                component_class,
                base_address(module, index),
                module_ranges(module),
                index=index,
            )
            for index in range(1, self._counts[module] + 1)
        ]

    async def _build[C: LambdaComponent](
        self,
        component_class: type[C],
        base: int,
        relative_ranges: tuple[Range, ...],
        index: int = 1,
    ) -> C:
        """Probe one module's runs and keep only the fields it answers for.

        The component is the ordinary typed one — same fields, same update — with
        its read plan narrowed to the registers this controller serves. A field
        the controller does not serve is dropped from the plan, so it is never
        read and reads as ``None``.
        """
        # The probe talks to the wire, so it works in absolute addresses.
        served = await _probe_served(
            self._unit,
            tuple((base + low, base + high) for low, high in relative_ranges),
        )

        component = component_class(self._unit, index=index, base_offset=base)
        # Ranges are declared in the same coordinates as the field addresses, so
        # they are stated relative to the block and the component shifts them.
        component.register_ranges = relative_ranges
        # `resolved_fields` says where each field actually landed on the device,
        # base offset and all, so the probe's absolute addresses can be compared
        # against it directly. Keeping only the fields the controller serves also
        # splits the ranges around the ones it does not, so a block never spans a
        # refused register.
        component.restrict_fields(
            [
                name
                for name, resolved in component.resolved_fields.items()
                if all(
                    resolved.address + offset in served
                    for offset in range(resolved.count)
                )
            ]
        )
        return component

    async def async_update(self) -> UpdateReport:
        """Refresh every sub-system, one at a time.

        Each module is read on its own, so they are independent: one that stops
        answering keeps the values it had and is named in the report, while the
        rest still refresh. Listeners fire only once every sub-system has been
        tried, and only for the ones that did refresh — so what a listener reads
        is one poll's worth of the controller, not half of it. A failure of the
        link itself raises ``ModbusConnectionError`` rather than reporting a
        silence that is not the controller's.
        """
        if self._polled is None:
            await self.async_setup()
        assert self._polled is not None  # async_setup() builds it
        updated: set[str] = set()
        failed: dict[str, ModbusError] = {}
        for name, component in self._polled.items():
            try:
                await component.async_update(notify=False)
            except ModbusConnectionError:
                raise
            except ModbusError as err:
                failed[name] = err
            else:
                updated.add(name)
        for name in updated:
            self._polled[name].notify()
        return UpdateReport(updated, failed)

    async def async_read_raw(self) -> dict[str, dict[int, int | bool]]:
        """Every register this controller reads, undecoded — for diagnostics.

        The poll list is the whole device here: nothing is read only at setup,
        since the probe settles the read plan rather than building a component
        of its own. So this is every register a poll asks for, and only those —
        the ones the probe found this controller does not serve were dropped
        from the plan and are absent.

        A sub-system is read on its own, as a poll reads it, and one the
        controller will not answer for is left out rather than taking the dump
        down with it: a controller having trouble is the one whose registers are
        worth reading. Only the link itself failing raises.

        A dump is not a poll, so it fires no listeners: the fields refresh, but
        downloading diagnostics does not write a state for every entity off the
        poll cycle.
        """
        raw: dict[str, dict[int, int | bool]] = {}
        for component in (self._polled or {}).values():
            try:
                values_by_space = await component.async_read_raw(notify=False)
            except ModbusConnectionError:
                raise
            except ModbusError:
                continue
            for space, values in values_by_space.items():
                raw.setdefault(space, {}).update(values)
        return {space: dict(sorted(values.items())) for space, values in raw.items()}
