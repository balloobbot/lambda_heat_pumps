"""The diagnostics download: raw registers, and what surrounds them."""

from __future__ import annotations

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.lambda_heat_pumps.const import CONF_HOST

from .conftest import Controller
from .test_init import setup_entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _diagnostics(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client
) -> dict:
    return await get_diagnostics_for_config_entry(hass, hass_client, entry)


async def test_the_dump_carries_the_raw_registers(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """The registers come out undecoded — the words, not the values."""
    entry = await setup_entry(hass, controller, legacy=True)
    registers = (await _diagnostics(hass, entry, hass_client))["registers"]

    # Keyed by address space, as the mock backend replays a snapshot back in.
    holding = registers["holding"]
    # The raw word, not the 34.12 °C the entity shows for it.
    assert holding["1004"] == 3412
    # A 32-bit counter is two separate words here; the dump does not combine them.
    assert holding["1020"] == 0x0001
    assert holding["1021"] == 0x86A0


async def test_the_dump_covers_the_installed_modules_only(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """It reads the blocks the controller answers for, and no others."""
    entry = await setup_entry(hass, controller, legacy=True)
    diagnostics = await _diagnostics(hass, entry, hass_client)

    addresses = {int(address) for address in diagnostics["registers"]["holding"]}
    assert 1004 in addresses  # the one heat pump
    assert 2002 in addresses  # the one boiler
    # A second heat pump is not installed, so its block is never read.
    assert not any(1100 <= address < 1200 for address in addresses)
    assert diagnostics["detected_modules"] == {
        "hp": 1,
        "boil": 1,
        "buff": 0,
        "sol": 0,
        "hc": 1,
    }


async def test_the_dump_keeps_going_past_a_refused_register(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """A register the controller refuses drops out; the rest still comes through.

    A truncated controller is the one whose dump is worth having. The read plan
    was narrowed around the refused registers at setup, so the dump asks for
    what is there and does not stop at the first register that is not.
    """
    controller.refuse(2004)  # inside the boiler block
    controller.refuse(2005)
    entry = await setup_entry(hass, controller, legacy=True)
    holding = (await _diagnostics(hass, entry, hass_client))["registers"]["holding"]

    # The served registers on both sides of the refusal are there.
    assert holding["2002"] == 480
    assert holding["2050"] == 520
    # The refused ones are simply absent, not an error that ended the dump.
    assert "2004" not in holding
    assert holding["5002"] == 340  # a later module still got read


async def test_the_dump_says_what_the_last_poll_made_of_the_controller(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """A module that stopped answering is named, with what it said.

    Without it a dump of a controller with one sulking module looks like a dump
    of a healthy one, only with a few values that are quietly out of date.
    """
    entry = await setup_entry(hass, controller, legacy=True)
    controller.answer_busy(1004)  # inside the heat pump's first block
    await entry.runtime_data.async_refresh()
    poll = (await _diagnostics(hass, entry, hass_client))["poll"]

    assert "hp1" not in poll["updated"]
    assert "boil1" in poll["updated"]
    assert "hp1" in poll["failed"]
    assert poll["failed"]["hp1"]  # the error, stringified


async def test_a_module_that_will_not_answer_does_not_cost_the_dump(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """A controller having trouble is the one whose registers are worth reading.

    The module that will not answer is simply missing from the dump; every other
    one is read as usual, rather than the whole download coming back empty.
    """
    entry = await setup_entry(hass, controller, legacy=True)
    controller.answer_busy(1004)  # inside the heat pump's first block
    holding = (await _diagnostics(hass, entry, hass_client))["registers"]["holding"]

    assert "1004" not in holding
    assert holding["2002"] == 480  # the boiler still came through
    assert holding["5002"] == 340


async def test_the_dump_says_where_each_field_was_read_from(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """The layout ties a raw word in the dump to the field that reports it.

    It is the layout the poll used, not the one the model declares, so a field
    the controller does not serve is missing from it — which is how a dump shows
    that an entity reads as unknown because the register was never asked for.
    """
    controller.refuse(2004)  # actual_circulation_temperature
    entry = await setup_entry(hass, controller, legacy=True)
    layout = (await _diagnostics(hass, entry, hass_client))["layout"]

    assert layout["hp1"]["flow_line_temperature"] == 1004
    assert layout["ambient"]["temperature"] == 2
    assert layout["boil1"]["actual_high_temperature"] == 2002
    assert "actual_circulation_temperature" not in layout["boil1"]


async def test_the_host_is_redacted(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """The one thing that says where the user is does not go in the download."""
    entry = await setup_entry(hass, controller, legacy=True)
    diagnostics = await _diagnostics(hass, entry, hass_client)

    assert diagnostics["entry"]["data"][CONF_HOST] == REDACTED
