"""Fixtures for the integration tests: a Modbus TCP server that acts as a Lambda.

The tests talk to it over a real socket, so nothing about the Modbus layer is
mocked: a passing test means the register model, the connection and the entities
all agree with each other.

The server is written out here rather than taken from a library because what the
tests need from it is precisely the thing a library server makes hard — refusing
the register blocks of a module that is not installed, which is how a Lambda
controller says it does not have one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import socket
import struct

import pytest

SLAVE_ID = 1

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

READ_HOLDING = 3
WRITE_REGISTER = 6
WRITE_REGISTERS = 16
ILLEGAL_DATA_ADDRESS = 2


class LambdaServer:
    """A Modbus TCP server that answers as a Lambda controller would."""

    def __init__(self) -> None:
        """Start from the registers a real controller would be reporting."""
        self.registers = dict(HOLDING)
        self.host = "127.0.0.1"
        self.port = _free_port()
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()

    def _absent(self, address: int, count: int) -> bool:
        """Whether the block covers a module the controller does not have."""
        return any(
            base <= address + offset < base + 100
            for base in ABSENT_BLOCKS
            for offset in range(count)
        )

    async def start(self) -> None:
        """Listen."""
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self) -> None:
        """Stop listening, and hang up on anyone still connected.

        A connection has to be closed from this end too: waiting for the server
        to close waits for its handlers, and a handler runs until its client goes
        away.
        """
        for writer in list(self._clients):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._clients.add(writer)
        try:
            while True:
                header = await reader.readexactly(8)
                transaction, _, _, unit, function = struct.unpack(">HHHBB", header)
                body = await self._respond(function, reader)
                payload = bytes([unit]) + body
                writer.write(struct.pack(">HHH", transaction, 0, len(payload)) + payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()

    async def _respond(self, function: int, reader: asyncio.StreamReader) -> bytes:
        """The body of the response to one request."""
        if function == READ_HOLDING:
            address, count = struct.unpack(">HH", await reader.readexactly(4))
            if self._absent(address, count):
                return struct.pack(">BB", function | 0x80, ILLEGAL_DATA_ADDRESS)
            values = [self.registers.get(address + i, 0) for i in range(count)]
            return struct.pack(">BB", function, count * 2) + b"".join(
                struct.pack(">H", value) for value in values
            )

        if function == WRITE_REGISTER:
            address, value = struct.unpack(">HH", await reader.readexactly(4))
            self.registers[address] = value
            return struct.pack(">BHH", function, address, value)

        if function == WRITE_REGISTERS:
            address, count, _ = struct.unpack(">HHB", await reader.readexactly(5))
            data = await reader.readexactly(count * 2)
            for i in range(count):
                self.registers[address + i] = struct.unpack(">H", data[i * 2 : i * 2 + 2])[0]
            return struct.pack(">BHH", function, address, count)

        return struct.pack(">BB", function | 0x80, 1)  # illegal function


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def server(socket_enabled) -> AsyncIterator[LambdaServer]:
    """A Lambda controller on a real socket."""
    controller = LambdaServer()
    await controller.start()
    try:
        yield controller
    finally:
        await controller.stop()
