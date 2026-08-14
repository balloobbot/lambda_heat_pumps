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
from homeassistant.core import HomeAssistant, State
from modbus_connection import (
    ModbusConnectionError,
    ModbusTimeoutError,
    ServerDeviceBusyError,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

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


async def test_a_silent_controller_leaves_the_totals_alone(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A controller that is switched off is the case that costs the most.

    Heat pumps are shut down for the season and inverters sleep every night, so
    a whole poll failing is routine rather than exceptional — and it used to
    take every entity down, counters included, because a failed poll fails them
    all at the coordinator.
    """
    entry = await setup_entry(hass, controller, legacy=True)
    coordinator = entry.runtime_data

    controller.go_offline()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert state_of(hass, "eu08l_hp1_flow_line_temperature") == "unavailable"
    assert (
        state_of(hass, "eu08l_hp1_compressor_power_consumption_accumulated") == "100000"
    )
    # Counted rather than read, and just as much worth keeping.
    assert state_of(hass, "eu08l_hp1_heating_cycling_total") != "unavailable"


async def test_a_total_whose_register_is_gone_keeps_what_it_had(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A total that reads as nothing holds its value, across a restart too.

    A controller whose firmware does not serve the counter block has not
    un-generated the energy it already reported, so the counter picks up what it
    last said rather than starting the statistics over at unknown.
    """
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                # The entity id comes from the sensor's name, not its unique id.
                State("sensor.eu08l_hp1_compressor_power_accumulated", "100000"),
                {"native_value": 100000, "native_unit_of_measurement": "Wh"},
            ),
        ),
    )
    controller.refuse(1020)  # the electrical counter, read as its own range
    controller.refuse(1021)
    await setup_entry(hass, controller, legacy=True)

    assert (
        state_of(hass, "eu08l_hp1_compressor_power_consumption_accumulated") == "100000"
    )
    # An instantaneous reading has no history to protect, and reads as usual.
    assert state_of(hass, "eu08l_hp1_flow_line_temperature") == "34.12"


async def test_a_total_that_dips_by_a_hair_keeps_what_it_had(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A counter read mid-carry is not a meter reset.

    The lifetime counters are 32 bits across two registers, and a controller
    that serves them while it is updating them answers with a value a hair below
    the last one. Published, Home Assistant reads the step backwards as the
    meter having been replaced and starts the long-term statistics over.
    """
    entry = await setup_entry(hass, controller, legacy=True)

    controller.registers[1021] = 0x869F  # 100000 Wh -> 99999 Wh
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert (
        state_of(hass, "eu08l_hp1_compressor_power_consumption_accumulated") == "100000"
    )

    # And it takes the reading again as soon as the counter has caught up.
    controller.registers[1021] = 0x86A1  # 100001 Wh
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert (
        state_of(hass, "eu08l_hp1_compressor_power_consumption_accumulated") == "100001"
    )


async def test_a_total_that_really_falls_is_published(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A counter that starts over is a counter that started over.

    Holding the old value there would be a total that never comes back down,
    and the statistics would carry a step the heat pump never generated. Only a
    dip small enough to be one torn reading is ignored.
    """
    entry = await setup_entry(hass, controller, legacy=True)

    controller.registers[1020] = 0  # a replaced heat pump, counting from 5 kWh
    controller.registers[1021] = 5000
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, "eu08l_hp1_compressor_power_consumption_accumulated") == "5000"


async def test_a_module_that_stops_answering_is_logged_once(
    hass: HomeAssistant, controller: Controller, caplog: pytest.LogCaptureFixture
) -> None:
    """A module that keeps failing would otherwise say so on every poll."""
    entry = await setup_entry(hass, controller, legacy=True)
    coordinator = entry.runtime_data

    controller.answer_busy(_HP1_FLOW_LINE)
    caplog.clear()
    await coordinator.async_refresh()
    assert caplog.text.count("Failed to fetch hp1") == 1

    await coordinator.async_refresh()
    assert caplog.text.count("Failed to fetch hp1") == 1


async def test_a_controller_that_answers_nothing_says_what_went_wrong(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A poll where every sub-system failed has to say what it ran into.

    Home Assistant logs the message at error level and the traceback only at
    debug, so one of the errors has to be in the message itself; the others ride
    along on the cause for whoever turns debug on.
    """
    entry = await setup_entry(hass, controller, legacy=True)
    coordinator = entry.runtime_data

    # A controller too busy for anything at all: it answers every sub-system,
    # and what it answers is that it could not get to it.
    for address in (0, 100, _HP1_FLOW_LINE, 2000, 5000):
        controller.answer_busy(address)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    error = coordinator.last_exception
    assert "exception code 6" in str(error)
    assert isinstance(error.__cause__, ExceptionGroup)
    assert len(error.__cause__.exceptions) == len(coordinator.failed) == 5


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


async def test_a_controller_that_answers_nothing_is_not_walked_module_by_module(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A first sub-system that times out ends the poll there.

    Nothing has answered — not a value, and not a refusal either, which would at
    least prove the controller is there. Reading on would pay a full timeout for
    every module in turn, so one poll of a controller that is asleep or behind a
    bridge that has stopped relaying takes minutes and reports the whole device
    as a pile of stale values.
    """
    entry = await setup_entry(hass, controller)
    controller.time_out(0)  # the ambient block, which a poll reads first
    controller.forget_reads()

    with pytest.raises(ModbusTimeoutError):
        await entry.runtime_data.device.async_update()

    # It asked once and stopped, rather than trying every module in turn.
    assert len(controller.reads()) == 1


async def test_a_module_that_times_out_after_another_answered_is_contained(
    hass: HomeAssistant, controller: Controller
) -> None:
    """A timeout is only fatal while nothing has answered.

    Once the controller has answered for something, a module that does not is
    that module's problem — one slow block loses its own component and no more,
    which is what reading them separately is for.
    """
    entry = await setup_entry(hass, controller, legacy=True)
    coordinator = entry.runtime_data

    controller.registers[2002] = 500  # the boiler, read after the heat pump
    controller.time_out(_HP1_FLOW_LINE)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert set(coordinator.failed) == {"hp1"}
    assert isinstance(coordinator.failed["hp1"], ModbusTimeoutError)
    assert state_of(hass, "eu08l_boil1_actual_high_temperature") == "50.0"


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
