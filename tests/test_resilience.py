"""What a controller's bad moment costs, and what it must not.

The controller is read a module at a time, each as its own request, so a module
it cannot get to is contained: that module keeps the values it last read and
only its entities go unavailable, while everything else refreshes as usual. Only
silence from the link itself fails the poll.

And what it says while it is having a bad moment is not a description of its
hardware. A register map or a module count read off a hiccup would be wrong for
as long as the entry stays loaded, so setup fails and is tried again instead.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from modbus_connection import ModbusConnectionError, ServerDeviceBusyError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lambda_heat_pumps.const import DOMAIN, ENTRY_VERSION

from .conftest import Controller
from .test_init import entry_data, setup_entry, state_of

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

# The heat pump's first block covers this one, so a controller that will not
# answer for it is a controller that cannot be read for HP1 at all.
_HP1_FLOW_LINE = 1004


async def test_a_module_that_stops_answering_keeps_the_values_it_had(
    hass: HomeAssistant, controller: Controller
) -> None:
    """What the failed module reports is what it last read, not nothing."""
    entry = await setup_entry(hass, controller, legacy=True)
    coordinator = entry.runtime_data

    # Both modules move on the controller, but it cannot get to the heat pump.
    controller.registers[_HP1_FLOW_LINE] = 4000  # would be 40.00 °C
    controller.registers[2002] = 500  # boiler high -> 50.0 °C
    controller.answer_busy(_HP1_FLOW_LINE)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # The poll is not a failure: it read everything the controller answered for.
    assert coordinator.last_update_success
    assert set(coordinator.failed) == {"hp1"}
    assert isinstance(coordinator.failed["hp1"], ServerDeviceBusyError)
    assert state_of(hass, "eu08l_boil1_actual_high_temperature") == "50.0"
    assert coordinator.device.heat_pumps[0].flow_line_temperature == 34.12


async def test_only_the_failed_modules_entities_go_unavailable(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A kept value is not published as though it were this poll's.

    The rest of the controller is unaffected — which is the whole point, since
    one slow block used to take every entity down with it.
    """
    entry = await setup_entry(hass, controller, legacy=True)

    controller.answer_busy(_HP1_FLOW_LINE)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, "eu08l_hp1_flow_line_temperature") == "unavailable"
    assert state_of(hass, "eu08l_ambient_temperature") == "4.2"
    assert state_of(hass, "eu08l_boil1_actual_high_temperature") == "48.0"
    # The running totals are counted here rather than read off the module, so
    # they are as good as they were and stay where they are.
    assert state_of(hass, "eu08l_hp1_heating_cycling_total") != "unavailable"


async def test_a_failed_modules_lifetime_counters_stay_available(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A gap in a lifetime counter is a gap in long-term statistics.

    A heat pump that cannot be read has not un-generated the energy its counter
    already reported, and the energy dashboard reads a missing total as one. So
    the accumulating registers hold what they last read while the instantaneous
    readings beside them go unavailable.
    """
    entry = await setup_entry(hass, controller, legacy=True)

    controller.answer_busy(_HP1_FLOW_LINE)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, "eu08l_hp1_flow_line_temperature") == "unavailable"
    assert (
        state_of(hass, "eu08l_hp1_compressor_power_consumption_accumulated") == "100000"
    )
    assert (
        state_of(hass, "eu08l_hp1_compressor_thermal_energy_output_accumulated")
        == "400000"
    )


async def test_listeners_fire_only_once_every_module_has_been_tried(
    hass: HomeAssistant, controller: Controller
) -> None:
    """What a listener reads is one poll's worth of the controller."""
    entry = await setup_entry(hass, controller)
    device = entry.runtime_data.device
    seen: list[int] = []
    # The boiler is read before the heating circuit, so a notification fired at
    # its own read would land before the poll's last request.
    device.boilers[0].add_update_listener(lambda: seen.append(len(controller.reads())))

    controller.answer_busy(_HP1_FLOW_LINE)
    controller.forget_reads()
    await entry.runtime_data.async_refresh()

    # Notified once, after the last read of the poll — and not for the heat
    # pump, which has nothing new to tell anyone.
    assert seen == [len(controller.reads())]


async def test_a_healthy_poll_reads_every_installed_module(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A complete report names each module, and nothing that is not installed."""
    entry = await setup_entry(hass, controller)
    report = await entry.runtime_data.device.async_update()

    assert report.complete
    assert report.failed == {}
    assert report.updated == {"ambient", "e_manager", "hp1", "boil1", "hc1"}


async def test_a_dead_link_raises_instead_of_reporting(
    hass: HomeAssistant, controller: Controller
) -> None:
    """Nothing answering is the link, not every module at once.

    Reported as a poll where each module happened to fail, it would look like a
    controller with a lot of stale values instead of one that is not there.
    """
    entry = await setup_entry(hass, controller)
    controller.go_offline()

    with pytest.raises(ModbusConnectionError):
        await entry.runtime_data.device.async_update()


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
