"""What a controller's bad moment costs, and what it must not.

What a controller says while it is having one is not a description of its
hardware. A register map or a module count read off a hiccup would be wrong for
as long as the entry stays loaded, so setup fails and is tried again instead.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lambda_heat_pumps.const import DOMAIN, ENTRY_VERSION

from .conftest import Controller
from .test_init import entry_data, state_of

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_a_controller_too_busy_to_be_probed_is_probed_again(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A hiccup while the register map is being read is not a register map.

    "Busy" says nothing about which registers a controller has, and one written
    off here would go unread for as long as the entry stays loaded. Setup fails
    instead, so what the controller serves is read once it is free to answer.
    """
    controller.answer_busy(2004)  # a boiler register, while the probe reads it
    entry = MockConfigEntry(
        domain=DOMAIN, version=ENTRY_VERSION, data=entry_data(legacy=True)
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY

    controller.stop_being_busy()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # The register it was busy for is read, not written off for good.
    assert state_of(hass, "eu08l_boil1_actual_circulation_temperature") == "0.0"


async def test_a_module_the_controller_is_busy_for_is_still_detected(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A module counted out over a hiccup would have no entities at all.

    The count comes from which module blocks answer, so "busy" there is the same
    ambiguity one register further in — and getting it wrong costs the module.
    """
    controller.answer_busy(2000)  # boiler 1's own probe register
    entry = MockConfigEntry(
        domain=DOMAIN, version=ENTRY_VERSION, data=entry_data(legacy=True)
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY

    controller.stop_being_busy()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.counts["boil"] == 1
    assert state_of(hass, "eu08l_boil1_actual_high_temperature") == "48.0"
