"""Polls the Lambda controller and tracks what has to be derived from it.

Two things are read on a schedule:

* The **full poll** refreshes the whole register model in one pooled set of block
  reads. Entities read their values straight off it.
* The **fast poll** reads two registers per heat pump — the operating state and
  the compressor rating. A compressor start can begin and end well inside one
  full-poll window, so the cycle counters would miss it otherwise.

The rest of this module exists because two things cannot be read from the
controller at all: how many times it has entered a mode, and how much energy it
spent in each one. Both are derived from what the polls see and kept here as
totals counted **since Home Assistant started**. A counter entity restores its
own value across restarts and adds on whatever has been counted since — so
nothing here has to be persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnection,
    ModbusError,
    ModbusTimeoutError,
    ModbusUnit,
)

from .const import (
    CONF_FAST_UPDATE_INTERVAL,
    CONF_FIRMWARE_VERSION,
    CONF_HOST,
    CONF_INT32_REGISTER_ORDER,
    CONF_NAME_PREFIX,
    CONF_UPDATE_INTERVAL,
    CYCLE_MODES,
    DEFAULT_FAST_UPDATE_INTERVAL,
    DEFAULT_INT32_REGISTER_ORDER,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ELECTRICAL_ENERGY_MODES,
    MAX_ENERGY_DELTA_KWH,
    MODE_COMPRESSOR_START,
    MODE_STBY,
    MODULES,
    OPERATING_STATE_MODE,
    PERIOD_2H,
    PERIOD_4H,
    PERIOD_DAILY,
    PERIOD_HOURLY,
    PERIOD_MONTHLY,
    PERIOD_YEARLY,
    REGISTER_ORDER_LOW_FIRST,
    SIGNAL_PERIOD_ROLLOVER,
    THERMAL_ENERGY_MODES,
)
from .lambda_modbus import LambdaHeatPump

_LOGGER = logging.getLogger(__name__)

type LambdaConfigEntry = ConfigEntry[LambdaCoordinator]

# The two fields the fast poll reads; where they sit on the controller comes
# from the heat pump's own layout, not from a second copy of the address map.
_FAST_POLL_FIELDS = ("operating_state", "compressor_unit_rating")

# Consecutive timed-out polls before the link itself is suspect. A device that
# stops answering while its socket stays open never drops the link, so nothing
# would re-establish it on its own.
_TIMEOUTS_BEFORE_RECYCLING_THE_LINK = 3

# The controller reports both energy counters in Wh; the sensors are in kWh.
_WH_PER_KWH = 1000.0

# What each module type is called on its sub-device.
_MODULE_NAMES = {"hp": "HP", "boil": "Boiler", "buff": "Buffer", "sol": "Solar", "hc": "HC"}


def _refused(errors: dict[str, IllegalDataAddressError]) -> str:
    """Name the blocks the controller refused, for the log."""
    return ", ".join(
        f"{name}'s {block.space} registers "
        f"{block.address}-{block.address + block.count - 1}"
        if (block := err.block) is not None
        else f"{name}'s registers"
        for name, err in errors.items()
    )


def _periods_ending(now: datetime) -> list[str]:
    """The periods that roll over at this hour boundary."""
    periods = [PERIOD_HOURLY]
    if now.hour % 2 == 0:
        periods.append(PERIOD_2H)
    if now.hour % 4 == 0:
        periods.append(PERIOD_4H)
    if now.hour == 0:
        periods.append(PERIOD_DAILY)
        if now.day == 1:
            periods.append(PERIOD_MONTHLY)
            if now.month == 1:
                periods.append(PERIOD_YEARLY)
    return periods


@dataclass
class Totals:
    """One heat pump's running totals, keyed by mode.

    `cycles` counts entries into a mode; `electrical` and `thermal` accumulate
    the energy spent in it, in kWh. All three count from Home Assistant's start,
    not from the beginning of time — the entities hold the absolute values.
    """

    cycles: dict[str, int] = field(default_factory=dict)
    electrical: dict[str, float] = field(default_factory=dict)
    thermal: dict[str, float] = field(default_factory=dict)


class LambdaCoordinator(DataUpdateCoordinator[LambdaHeatPump]):
    """Owns the Modbus link, the register model, and the derived totals."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: LambdaConfigEntry,
        connection: ModbusConnection,
        unit: ModbusUnit,
        counts: dict[str, int],
    ) -> None:
        """Model the modules this controller was found to have."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ),
        )
        self.connection = connection
        self.unit = unit
        self.counts = counts
        self.host = entry.data[CONF_HOST]
        self.firmware_version = entry.data[CONF_FIRMWARE_VERSION]

        word_order = (
            "little"
            if entry.options.get(
                CONF_INT32_REGISTER_ORDER, DEFAULT_INT32_REGISTER_ORDER
            )
            == REGISTER_ORDER_LOW_FIRST
            else "big"
        )
        self.device = LambdaHeatPump(
            unit,
            num_hps=counts["hp"],
            num_boil=counts["boil"],
            num_buff=counts["buff"],
            num_sol=counts["sol"],
            num_hc=counts["hc"],
            word_order=word_order,
        )

        self.totals: dict[int, Totals] = {
            index: Totals() for index in range(1, counts["hp"] + 1)
        }

        # The heating-curve settings the user edits through the number entities,
        # keyed by (heating circuit, key). They are not registers — the curve is
        # computed here, not by the controller — so the number entities publish
        # them here for the heating-curve sensor to read.
        self.settings: dict[tuple[int, str], float] = {}

        # What the last poll saw, so an edge can be told from a steady state.
        self._last_operating_state: dict[int, int] = {}
        self._last_compressor_running: dict[int, bool] = {}
        # The controller's own Wh counters, as of the last poll.
        self._last_energy: dict[tuple[int, str], float] = {}

        self._fast_interval = timedelta(
            seconds=entry.options.get(
                CONF_FAST_UPDATE_INTERVAL, DEFAULT_FAST_UPDATE_INTERVAL
            )
        )
        self._polling = False
        # Consecutive polls that timed out, for spotting a wedged link.
        self._timeouts = 0

        # What the last poll could not read, by sub-system name. Those keep the
        # values they had, so the entities reading them say so.
        self.failed: dict[str, ModbusError] = {}

    def component(self, module: str, index: int):
        """The modelled sub-system for one module, by 1-based index."""
        return getattr(self.device, MODULES[module])[index - 1]

    def device_info(self, module: str | None, index: int | None) -> DeviceInfo:
        """The device a module's entities belong to, or the controller's."""
        entry = self.config_entry
        controller = (DOMAIN, entry.entry_id)
        shared = {
            "manufacturer": "Lambda",
            "model": self.firmware_version,
            "sw_version": self.firmware_version,
            "configuration_url": f"http://{self.host}",
        }
        name = entry.data[CONF_NAME_PREFIX]
        if module is None:
            return DeviceInfo(identifiers={controller}, name=name, **shared)
        return DeviceInfo(
            # A four-part identifier, as the integration has always used.
            identifiers={(DOMAIN, entry.entry_id, module, index)},
            name=f"{name} - {_MODULE_NAMES[module]}{index}",
            via_device=controller,
            **shared,
        )

    async def _async_setup(self) -> None:
        """Probe the register map, then arm the fast poll and period rollovers."""
        # Which registers the controller serves depends on its firmware, so the
        # modules are built from what it answers for — probed once here, before
        # the first poll reads them.
        try:
            await self.device.async_setup()
        except ModbusError as err:
            # A controller that is busy or out of reach while it is being probed
            # has not said what it serves, and a map read off a bad moment would
            # be wrong for as long as the entry is loaded. Fail the setup and let
            # Home Assistant try it again.
            raise UpdateFailed(f"Error probing the controller: {err}") from err

        entry = self.config_entry
        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_fast_poll, self._fast_interval
            )
        )
        # Every period a counter can be reported over rolls at the top of an
        # hour, so one hourly tick covers all of them.
        entry.async_on_unload(
            async_track_time_change(self.hass, self._rollover, minute=0, second=0)
        )

    @callback
    def _rollover(self, now: datetime) -> None:
        """Tell the counters whose period just ended to start again at zero."""
        for period in _periods_ending(now):
            async_dispatcher_send(
                self.hass,
                SIGNAL_PERIOD_ROLLOVER.format(
                    entry_id=self.config_entry.entry_id, period=period
                ),
            )

    async def _async_update_data(self) -> LambdaHeatPump:
        """Refresh the whole controller, then attribute the energy it used."""
        self._polling = True
        try:
            # The connection re-establishes itself: a request opens the link if
            # it is down, over the same unit handles, so a drop costs at most the
            # poll it happened on and nothing has to be rebuilt.
            report = await self.device.async_update()
        except ModbusError as err:
            # Only the link itself failing gets this far — a sub-system the
            # controller would not answer for is reported, not raised.
            raise UpdateFailed(f"Error reading the controller: {err}") from err
        finally:
            self._polling = False

        self.failed = report.failed

        stale: dict[str, IllegalDataAddressError] = {
            name: err
            for name, err in report.failed.items()
            if isinstance(err, IllegalDataAddressError)
        }
        if stale:
            # The controller says it does not serve an address the probe found
            # it serving, so what was read off it at setup no longer describes
            # it — a module was added or removed, or its firmware changed. Only
            # setting up again can find out what it has now, so ask for that
            # rather than telling the user to; the blocks are named for the log.
            # Every other exception code is the controller having a problem with
            # a block it does serve — busy, or a failure of its own — which the
            # next poll can just try again, so none of them reload anything.
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            raise UpdateFailed(
                f"The controller refused {_refused(stale)}, which it served when "
                f"it was set up; looking again at what it has."
            ) from next(iter(stale.values()))

        if report.failed and not report.updated:
            # Nothing answered at all, so it is the controller that is not
            # talking rather than one of its modules. A controller behind a
            # serial-to-network bridge can go on holding the socket open while
            # nothing behind it answers, so the link is never lost and never
            # re-established. Drop it after a few of these and the next poll
            # opens a fresh one, without reloading the entry.
            first = next(iter(report.failed.values()))
            if any(
                isinstance(err, ModbusTimeoutError)
                for err in report.failed.values()
            ):
                self._timeouts += 1
                if self._timeouts >= _TIMEOUTS_BEFORE_RECYCLING_THE_LINK:
                    _LOGGER.debug(
                        "Recycling the link after %d timeouts", self._timeouts
                    )
                    self._timeouts = 0
                    try:
                        await self.connection.disconnect()
                    except ModbusError as close_err:
                        # The link is dropped either way, so this is only worth a log.
                        _LOGGER.debug("Tearing the link down failed: %s", close_err)
            raise UpdateFailed(f"Error reading the controller: {first}") from first

        self._timeouts = 0
        if report.failed:
            _LOGGER.debug(
                "Kept the last values for %s",
                "; ".join(f"{name}: {err}" for name, err in report.failed.items()),
            )

        for index in self.totals:
            heat_pump = self.component("hp", index)
            if heat_pump.operating_state is not None:
                self._track_cycles(
                    index,
                    int(heat_pump.operating_state),
                    bool(heat_pump.compressor_unit_rating),
                )
            self._track_energy(index)
        return self.device

    async def _async_fast_poll(self, _now: datetime) -> None:
        """Catch the mode changes and compressor starts a slow poll would miss.

        The full poll counts cycles from what it reads too, so this only closes
        the gap between them; both feed the same running state, so a cycle seen
        by both is still only counted once.

        Each register is read on its own, as the full poll's plan reads it: the
        two sit inside one of the controller's runs, but a block spanning them
        would also cover the registers between, which is more of the controller
        than a poll this frequent has any business asking for.
        """
        if self._polling:
            return
        try:
            for index in self.totals:
                resolved = self.component("hp", index).resolved_fields
                if any(name not in resolved for name in _FAST_POLL_FIELDS):
                    # This controller does not serve one of them, so it was
                    # dropped from the read plan at setup; the full poll counts
                    # what it can and there is nothing to do here.
                    continue
                state_address, rating_address = (
                    resolved[name].address for name in _FAST_POLL_FIELDS
                )
                operating_state = (
                    await self.unit.read_holding_registers(state_address, 1)
                )[0]
                rating = (await self.unit.read_holding_registers(rating_address, 1))[0]
                self._track_cycles(index, operating_state, bool(rating))
        except ModbusError as err:
            # The full poll decides whether the device is available; a missed
            # fast poll costs at most one counted cycle.
            _LOGGER.debug("Fast poll failed: %s", err)
            return
        self.async_update_listeners()

    @callback
    def _track_cycles(
        self, index: int, operating_state: int, compressor_running: bool
    ) -> None:
        """Count an entry into a mode, and a compressor start."""
        totals = self.totals[index]

        previous = self._last_operating_state.get(index)
        self._last_operating_state[index] = operating_state
        mode = OPERATING_STATE_MODE.get(operating_state, MODE_STBY)
        if previous is not None and previous != operating_state and mode in CYCLE_MODES:
            totals.cycles[mode] = totals.cycles.get(mode, 0) + 1

        # A compressor start is the compressor beginning to run, whatever mode
        # the heat pump is in.
        was_running = self._last_compressor_running.get(index)
        self._last_compressor_running[index] = compressor_running
        if was_running is False and compressor_running:
            totals.cycles[MODE_COMPRESSOR_START] = (
                totals.cycles.get(MODE_COMPRESSOR_START, 0) + 1
            )

    @callback
    def _track_energy(self, index: int) -> None:
        """Book the energy used since the last poll against the current mode.

        The controller reports two counters that only ever climb — electrical in,
        thermal out. Whatever they climbed by is booked against the mode the heat
        pump is in now; the controller offers nothing finer to split it by.
        """
        heat_pump = self.component("hp", index)
        if (operating_state := heat_pump.operating_state) is None:
            return
        mode = OPERATING_STATE_MODE.get(int(operating_state), MODE_STBY)

        totals = self.totals[index]
        for kind, reading, modes, bucket in (
            (
                "electrical",
                heat_pump.compressor_power_consumption_accumulated,
                ELECTRICAL_ENERGY_MODES,
                totals.electrical,
            ),
            (
                "thermal",
                heat_pump.compressor_thermal_energy_output_accumulated,
                THERMAL_ENERGY_MODES,
                totals.thermal,
            ),
        ):
            delta = self._energy_delta(index, kind, reading)
            if delta and mode in modes:
                bucket[mode] = bucket.get(mode, 0.0) + delta

    def _energy_delta(self, index: int, kind: str, reading: int | None) -> float:
        """How far one of the controller's Wh counters climbed, in kWh.

        Zero whenever the reading cannot be trusted as a continuation of the last
        one: the first reading after a start, a zero (the controller reports zero
        while it boots), or a counter that went backwards or jumped implausibly
        far — it was reset, or the heat pump was replaced.
        """
        if reading is None or reading <= 0:
            return 0.0

        current = reading / _WH_PER_KWH
        previous = self._last_energy.get((index, kind))
        self._last_energy[(index, kind)] = current
        if previous is None:
            return 0.0

        delta = current - previous
        if delta < 0 or delta > MAX_ENERGY_DELTA_KWH:
            _LOGGER.debug(
                "HP%d %s counter went from %.3f to %.3f kWh; not counting that",
                index,
                kind,
                previous,
                current,
            )
            return 0.0
        return delta

