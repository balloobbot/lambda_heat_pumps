"""Shared base for every Lambda sub-system."""

from __future__ import annotations

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
