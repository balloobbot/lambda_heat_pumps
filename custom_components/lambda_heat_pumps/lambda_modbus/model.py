"""Shared base for every Lambda sub-system."""

from __future__ import annotations

from dataclasses import dataclass

from modbus_connection import ModbusError
from modbus_connection.model import Component


class LambdaComponent(Component):
    """A Lambda sub-system.

    The controller's readable ranges are not a property of a single sub-system —
    they depend on how many modules are configured — so :class:`LambdaHeatPump`
    computes them once and assigns ``register_ranges`` to every component it
    builds. See :mod:`.ranges`.
    """

    # Every value the controller exposes lives in holding registers (FC03); it
    # has no input registers, coils or discrete inputs.
    register_space = "holding"


@dataclass(frozen=True)
class UpdateReport:
    """What one poll refreshed, by sub-system name.

    The names are the controller's own two sub-systems, ``ambient`` and
    ``e_manager``, and one per installed module — ``hp1``, ``boil1``, ``hc2``.

    A failed sub-system kept its previous values and did not notify its
    listeners; the error that failed it rides along. A dead link is never in
    here — the update raises ``ModbusConnectionError`` instead of reporting
    partial silence.
    """

    updated: set[str]
    failed: dict[str, ModbusError]

    @property
    def complete(self) -> bool:
        """Whether every polled sub-system refreshed."""
        return not self.failed
