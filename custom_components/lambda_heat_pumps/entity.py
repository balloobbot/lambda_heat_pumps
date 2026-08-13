"""Base entity for the Lambda Heat Pumps integration.

Every module the controller has — each heat pump, boiler, buffer, solar module
and heating circuit — is its own sub-device, linked to the controller via
`via_device`. The two always-present sub-systems (ambient and the e-manager)
belong to the controller itself.

The unique-id shape here is load-bearing: it is what keeps an existing
installation's entities attached to their history.
"""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NAME_PREFIX, CONF_USE_LEGACY_MODBUS_NAMES
from .coordinator import LambdaCoordinator


class LambdaEntity(CoordinatorEntity[LambdaCoordinator]):
    """Identity and device info shared by every Lambda entity.

    `module` and `index` name the sub-device the entity belongs to — ("hp", 1) —
    or are None for an entity that belongs to the controller itself.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LambdaCoordinator,
        key: str,
        module: str | None = None,
        index: int | None = None,
        component: str | None = None,
    ) -> None:
        """Give the entity its unique id and its device."""
        super().__init__(coordinator)
        self._module = module
        self._index = index
        # Which sub-system a poll has to have read for this entity's value to be
        # current. An entity whose value is derived, accumulated or set by the
        # user names none, and stays available whatever the controller answered.
        self._polled = component

        entry = coordinator.config_entry
        # Installations created before Home Assistant named entities from their
        # device prefix every unique id with the entry's name.
        legacy = (
            f"{entry.data[CONF_NAME_PREFIX].lower()}_"
            if entry.data[CONF_USE_LEGACY_MODBUS_NAMES]
            else ""
        )
        module_prefix = f"{module}{index}_" if module else ""
        self._attr_unique_id = f"{legacy}{module_prefix}{key}"
        self._attr_device_info = coordinator.device_info(module, index)

    @property
    def available(self) -> bool:
        """Whether what this entity reports is what the controller holds.

        A sub-system the last poll could not read kept the values it had, which
        are no longer the controller's — so its entities go unavailable while
        the rest of the controller carries on reporting.

        An entity that names no sub-system holds its own value and stays
        available whatever happened to the poll, including a controller that is
        gone for good: a gap in a running total reads as a counter reset and
        takes the long-term statistics with it, and heat pumps are switched off
        for the season as inverters are at night. That test comes first, above
        the coordinator's own — a controller answering nothing at all is exactly
        the case the totals have to survive. Saying whether the controller is
        answering is a connectivity entity's job, not a counter's.
        """
        return self._polled is None or (
            super().available and self._polled not in self.coordinator.failed
        )
