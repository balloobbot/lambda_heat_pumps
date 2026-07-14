"""Setting up a controller: what it finds, and what it creates."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lambda_heat_pumps.const import (
    CONF_FIRMWARE_VERSION,
    CONF_SLAVE_ID,
    CONF_USE_LEGACY_MODBUS_NAMES,
    DOMAIN,
    ENTRY_VERSION,
)

from .conftest import SLAVE_ID, LambdaServer


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations, socket_enabled):
    """Let Home Assistant load the integration, and the server bind a socket."""
    return


def entry_data(server: LambdaServer, *, legacy: bool = False) -> dict:
    """A config entry pointing at the test server."""
    return {
        CONF_NAME: "EU08L",
        CONF_HOST: server.host,
        CONF_PORT: server.port,
        CONF_SLAVE_ID: SLAVE_ID,
        CONF_FIRMWARE_VERSION: "V0.0.8-3K",
        CONF_USE_LEGACY_MODBUS_NAMES: legacy,
    }


async def setup_entry(
    hass: HomeAssistant, server: LambdaServer, *, legacy: bool = False, options=None
) -> MockConfigEntry:
    """Set up a controller against the test server."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=ENTRY_VERSION,
        data=entry_data(server, legacy=legacy),
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_detects_the_modules_the_controller_has(
    hass: HomeAssistant, server: LambdaServer
) -> None:
    """The probe finds one of each installed module, and none of the rest."""
    entry = await setup_entry(hass, server)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.counts == {"hp": 1, "boil": 1, "buff": 0, "sol": 0, "hc": 1}


def state_of(hass: HomeAssistant, unique_id: str) -> str:
    """A sensor's state, found the way its identity is actually defined."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id, f"no entity for {unique_id}"
    return hass.states.get(entity_id).state


async def test_setup_reads_the_controller(
    hass: HomeAssistant, server: LambdaServer
) -> None:
    """Values come back decoded — scaled, signed, and state codes resolved."""
    await setup_entry(hass, server, legacy=True)

    assert state_of(hass, "eu08l_ambient_temperature") == "4.2"
    assert state_of(hass, "eu08l_hp1_flow_line_temperature") == "34.12"
    assert state_of(hass, "eu08l_hp1_state") == "START COMPRESSOR"
    assert state_of(hass, "eu08l_hp1_operating_state") == "CH"
    assert state_of(hass, "eu08l_boil1_actual_high_temperature") == "48.0"
    # A 32-bit counter, over two registers.
    assert (
        state_of(hass, "eu08l_hp1_compressor_power_consumption_accumulated") == "100000"
    )


async def test_unique_ids_are_unchanged(
    hass: HomeAssistant, server: LambdaServer
) -> None:
    """An existing installation's entities keep the ids they have always had."""
    entry = await setup_entry(hass, server, legacy=True)
    registry = er.async_get(hass)

    for unique_id in (
        "eu08l_ambient_temperature",
        "eu08l_hp1_flow_line_temperature",
        "eu08l_hp1_heating_cycling_total",
        "eu08l_hp1_heating_cycling_yesterday",
        "eu08l_hp1_heating_energy_daily",
        "eu08l_hp1_heating_thermal_energy_monthly",
        "eu08l_hp1_heating_cop_total",
        "eu08l_boil1_target_high_temperature",
        "eu08l_hc1_heating_curve_flow_line_temperature_calc",
    ):
        assert registry.async_get_entity_id("sensor", DOMAIN, unique_id), unique_id

    assert registry.async_get_entity_id("climate", DOMAIN, "eu08l_boil1_hot_water")
    assert registry.async_get_entity_id(
        "number", DOMAIN, "eu08l_hc1_flow_line_offset_temperature_number"
    )
    assert entry.state is ConfigEntryState.LOADED


async def test_modules_are_their_own_devices(
    hass: HomeAssistant, server: LambdaServer
) -> None:
    """Each module hangs off the controller as its own device."""
    entry = await setup_entry(hass, server)
    devices = dr.async_get(hass)

    controller = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert controller is not None
    assert controller.name == "EU08L"

    heat_pump = devices.async_get_device(
        identifiers={(DOMAIN, entry.entry_id, "hp", 1)}
    )
    assert heat_pump is not None
    assert heat_pump.name == "EU08L - HP1"
    assert heat_pump.via_device_id == controller.id


async def test_unload_closes_the_connection(
    hass: HomeAssistant, server: LambdaServer
) -> None:
    """The integration owns the link, so it lets go of it."""
    entry = await setup_entry(hass, server)
    connection = entry.runtime_data.connection

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not connection.connected
