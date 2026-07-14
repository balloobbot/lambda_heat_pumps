"""A Lambda controller, backed by modbus-connection's in-memory mock backend.

The mock implements the same `ModbusConnection` / `ModbusUnit` protocols the real
backends do, so the integration runs against it unchanged — the register model,
the decoding and the entities are all exercised for real; only the wire is not.

Two things are added on top of it, because they are properties of a *Lambda*
rather than of Modbus:

* the registers a controller reports, and
* refusing the register block of a module that is not installed, which is the
  only way a controller says it does not have one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

from modbus_connection import ModbusConnectionError, ModbusExceptionError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit
import pytest

SLAVE_ID = 1

# Where the controller is. Nothing dials it — the mock backend stands in for the
# wire — but the config entry has to say something, and the integration hands
# these to `connect_tcp`.
HOST = "192.168.1.50"
PORT = 502

# One heat pump, one boiler, one heating circuit.
HOLDING: dict[int, int] = {
    # The controller itself.
    0: 0,  # ambient error number
    1: 1,  # ambient operating state -> AUTOMATIK
    2: 42,  # ambient temperature -> 4.2 °C
    3: 40,
    4: 38,  # ambient temperature calculated -> 3.8 °C
    100: 0,
    101: 1,
    102: 1500,  # e-manager actual power -> 1500 W
    103: 800,
    104: 0,
    # Heat pump 1.
    1000: 0,  # error state -> NONE
    1002: 5,  # state -> START COMPRESSOR
    1003: 1,  # operating state -> CH (heating)
    1004: 3412,  # flow line -> 34.12 °C
    1005: 2890,
    1010: 6500,  # compressor rating -> 65 %
    1011: 82,  # heating capacity -> 8.2 kW
    1013: 431,  # COP -> 4.31
    1020: 0x0001,  # electrical counter, high word
    1021: 0x86A0,  # -> 100000 Wh = 100 kWh
    1022: 0x0006,  # thermal counter, high word
    1023: 0x1A80,  # -> 400000 Wh = 400 kWh
    # Boiler 1.
    2000: 0,
    2001: 1,  # operating state -> DHW
    2002: 480,  # actual high -> 48.0 °C
    2050: 520,  # target high -> 52.0 °C
    # Heating circuit 1.
    5000: 0,
    5001: 0,  # operating state -> HEATING
    5002: 340,
    5004: 215,  # room device temperature -> 21.5 °C
    5006: 1,  # operating mode -> MANUAL
    5050: 0,  # flow line offset -> 0.0 °C
    5051: 210,  # target room temperature -> 21.0 °C
}

# The blocks a second module of each type would occupy. Nothing is installed
# there, so the controller refuses to read them.
ABSENT_BLOCKS = (1100, 2100, 3000, 4000, 5100)
BLOCK_SIZE = 100
ILLEGAL_DATA_ADDRESS = 2


@dataclass
class Controller:
    """The device under test.

    `registers` is the controller's memory — seed it before setup, read it back
    after a write, change it mid-test to make the controller do something.
    """

    registers: dict[int, int]


def _refuse_absent_modules(unit: MockModbusUnit) -> None:
    """Make the unit answer for the modules it has, and no others."""
    answer = unit.read_holding_registers

    async def read_holding_registers(address: int, count: int) -> list[int]:
        if any(
            base <= address + offset < base + BLOCK_SIZE
            for base in ABSENT_BLOCKS
            for offset in range(count)
        ):
            raise ModbusExceptionError(ILLEGAL_DATA_ADDRESS)
        return await answer(address, count)

    unit.read_holding_registers = read_holding_registers


@pytest.fixture
def controller() -> Iterator[Controller]:
    """A Lambda controller, reached over the mock backend.

    Every call to `connect_tcp` opens a fresh connection to the same controller,
    as it would in life: the config flow closing the link it probed with does not
    stop setup from opening its own.
    """
    registers = dict(HOLDING)

    def connect(host: str, *, port: int) -> MockModbusConnection:
        connection = MockModbusConnection()
        unit = connection.for_unit(SLAVE_ID)
        # The controller's memory, not this connection's — what is written over
        # one link is there to be read over the next.
        unit.holding = registers
        _refuse_absent_modules(unit)
        return connection

    connector: Callable[..., MockModbusConnection] = AsyncMock(side_effect=connect)
    with (
        patch("custom_components.lambda_heat_pumps.connect_tcp", connector),
        patch("custom_components.lambda_heat_pumps.config_flow.connect_tcp", connector),
    ):
        yield Controller(registers)


@pytest.fixture
def unreachable() -> Iterator[None]:
    """A controller that does not answer."""
    with (
        patch(
            "custom_components.lambda_heat_pumps.config_flow.connect_tcp",
            AsyncMock(side_effect=ModbusConnectionError("no route to host")),
        ),
        patch(
            "custom_components.lambda_heat_pumps.connect_tcp",
            AsyncMock(side_effect=ModbusConnectionError("no route to host")),
        ),
    ):
        yield
