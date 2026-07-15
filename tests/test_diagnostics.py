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

    # The raw word, not the 34.12 °C the entity shows for it.
    assert registers["1004"] == 3412
    # A 32-bit counter is two separate words here; the dump does not combine them.
    assert registers["1020"] == 0x0001
    assert registers["1021"] == 0x86A0


async def test_the_dump_covers_the_installed_modules_only(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """It reads the blocks the controller answers for, and no others."""
    entry = await setup_entry(hass, controller, legacy=True)
    diagnostics = await _diagnostics(hass, entry, hass_client)

    addresses = {int(address) for address in diagnostics["registers"]}
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


async def test_the_host_is_redacted(
    hass: HomeAssistant, controller: Controller, hass_client
) -> None:
    """The one thing that says where the user is does not go in the download."""
    entry = await setup_entry(hass, controller, legacy=True)
    diagnostics = await _diagnostics(hass, entry, hass_client)

    assert diagnostics["entry"]["data"][CONF_HOST] == REDACTED
