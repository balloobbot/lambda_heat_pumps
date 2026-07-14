"""State mappings for Lambda WP sensors.

The codes and their labels are declared once, on the enums in the lambda_modbus
device library — the model is the datasheet. These dicts are derived from them
and kept for the code (dashboards, templates, tests) that expects the mapping in
this shape.
"""

from .lambda_modbus.enums import (
    AmbientOperatingState,
    BoilerOperatingState,
    BufferOperatingState,
    BufferRequestType,
    CirculationPumpState,
    EManagerOperatingState,
    HeatingCircuitOperatingMode,
    HeatingCircuitOperatingState,
    HeatPumpErrorState,
    HeatPumpOperatingState,
    HeatPumpRequestType,
    HeatPumpState,
    RelaisState,
    SolarOperatingState,
)


def _labels(state_enum) -> dict[int, str]:
    """The enum's code -> label mapping."""
    return {member.value: member.label for member in state_enum}


# Heat Pump States
HP_ERROR_STATE = _labels(HeatPumpErrorState)
HP_STATE = _labels(HeatPumpState)
HP_RELAIS_STATE_2ND_HEATING_STAGE = _labels(RelaisState)
HP_OPERATING_STATE = _labels(HeatPumpOperatingState)
HP_REQUEST_TYPE = _labels(HeatPumpRequestType)

# Boiler States
BOIL_CIRCULATION_PUMP_STATE = _labels(RelaisState)
BOIL_OPERATING_STATE = _labels(BoilerOperatingState)

# Heating Circuit States
HC_OPERATING_STATE = _labels(HeatingCircuitOperatingState)
HC_OPERATING_MODE = _labels(HeatingCircuitOperatingMode)

# Buffer States
BUFF_OPERATING_STATE = _labels(BufferOperatingState)
BUFF_REQUEST_TYPE = _labels(BufferRequestType)

# Solar States
SOL_OPERATING_STATE = _labels(SolarOperatingState)

# Circulation Pump States
MAIN_CIRCULATION_PUMP_STATE = _labels(CirculationPumpState)

# Ambient States
MAIN_AMBIENT_OPERATING_STATE = _labels(AmbientOperatingState)

# E-Manager States
MAIN_E_MANAGER_OPERATING_STATE = _labels(EManagerOperatingState)
