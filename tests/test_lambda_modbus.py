"""Tests for the lambda_modbus device library.

These run against the in-memory mock backend that ships with modbus-connection —
no Home Assistant, no Modbus server, no hardware.

The parity test is the important one: it checks every field of the model against
the register templates the integration has always used, so the migration to
modbus-connection cannot silently move an address, drop a scale, or change a
sign.
"""

import pytest
from modbus_connection.cli_helper import CountingUnit
from modbus_connection.model import RegisterField

from custom_components.lambda_heat_pumps.const import (
    BOIL_SENSOR_TEMPLATES,
    BUFF_SENSOR_TEMPLATES,
    HC_SENSOR_TEMPLATES,
    HP_SENSOR_TEMPLATES,
    SENSOR_TYPES,
    SOL_SENSOR_TEMPLATES,
)
from custom_components.lambda_heat_pumps.lambda_modbus import (
    Ambient,
    Boiler,
    Buffer,
    EManager,
    HeatingCircuit,
    HeatPump,
    LambdaHeatPump,
    Solar,
)
from custom_components.lambda_heat_pumps.lambda_modbus.heat_pump import HeatPumpLowFirst

# The general sensors live in one flat dict, keyed by a prefixed name; each
# prefix belongs to one component and the rest of the key is the field name.
GENERAL_COMPONENTS = {"ambient_": Ambient, "emgr_": EManager}

MODULE_COMPONENTS = [
    (HP_SENSOR_TEMPLATES, HeatPump),
    (BOIL_SENSOR_TEMPLATES, Boiler),
    (BUFF_SENSOR_TEMPLATES, Buffer),
    (SOL_SENSOR_TEMPLATES, Solar),
    (HC_SENSOR_TEMPLATES, HeatingCircuit),
]


def _fields(component_class):
    """The component's register fields, by attribute name."""
    return {
        name: value
        for klass in reversed(component_class.__mro__)
        for name, value in vars(klass).items()
        if isinstance(value, RegisterField)
    }


def _template_cases():
    """(component class, field name, template) for every modelled register."""
    for key, template in SENSOR_TYPES.items():
        prefix = next(p for p in GENERAL_COMPONENTS if key.startswith(p))
        yield GENERAL_COMPONENTS[prefix], key.removeprefix(prefix), template
    for templates, component_class in MODULE_COMPONENTS:
        for key, template in templates.items():
            yield component_class, key, template


@pytest.mark.parametrize(
    ("component_class", "name", "template"),
    [pytest.param(*case, id=f"{case[0].__name__}.{case[1]}") for case in _template_cases()],
)
def test_field_matches_template(component_class, name, template):
    """Every template register is modelled at the same address, scale and sign."""
    field = _fields(component_class).get(name)
    assert field is not None, f"{component_class.__name__} has no field {name!r}"

    address = template.get("address", template.get("relative_address"))
    assert field.address == address
    assert field.scale == pytest.approx(template["scale"])
    assert field.count == (2 if template["data_type"] == "int32" else 1)
    assert field.signed is (template["data_type"] != "uint16")
    assert field.unit == template["unit"]
    assert bool(field.writable) is template["writeable"]


def test_no_unmodelled_fields():
    """The model declares nothing the templates don't — the two stay in step."""
    for templates, component_class in MODULE_COMPONENTS:
        assert set(_fields(component_class)) == set(templates)


@pytest.mark.asyncio
async def test_reads_a_scaled_value(mock_modbus_unit):
    """A heat pump's temperature decodes through the template's 0.01 scale."""
    mock_modbus_unit.holding[1004] = 3412  # HP1 flow line temperature

    controller = LambdaHeatPump(mock_modbus_unit, num_hps=1)
    await controller.async_update()

    assert controller.heat_pumps[0].flow_line_temperature == pytest.approx(34.12)


@pytest.mark.asyncio
async def test_reads_a_negative_value(mock_modbus_unit):
    """A negative temperature comes back signed, not as 65 thousand."""
    mock_modbus_unit.holding[2] = 0xFFF6  # -10 raw, ambient temperature

    controller = LambdaHeatPump(mock_modbus_unit)
    await controller.async_update()

    assert controller.ambient.temperature == pytest.approx(-1.0)


@pytest.mark.asyncio
async def test_modules_are_addressed_by_block(mock_modbus_unit):
    """Module n reads from its own 100-register block."""
    mock_modbus_unit.holding[1004] = 3000  # HP1
    mock_modbus_unit.holding[1104] = 4000  # HP2
    mock_modbus_unit.holding[5002] = 250  # HC1 flow line temperature
    mock_modbus_unit.holding[5102] = 300  # HC2

    controller = LambdaHeatPump(mock_modbus_unit, num_hps=2, num_hc=2)
    await controller.async_update()

    assert controller.heat_pumps[0].flow_line_temperature == pytest.approx(30.0)
    assert controller.heat_pumps[1].flow_line_temperature == pytest.approx(40.0)
    assert controller.heating_circuits[0].flow_line_temperature == pytest.approx(25.0)
    assert controller.heating_circuits[1].flow_line_temperature == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("component_class", "registers", "expected"),
    [
        (HeatPump, [0x0001, 0x86A0], 100000),  # high word first
        (HeatPumpLowFirst, [0x86A0, 0x0001], 100000),  # low word first
    ],
)
@pytest.mark.asyncio
async def test_int32_word_order(
    mock_modbus_unit, component_class, registers, expected
):
    """The 32-bit counters honour the controller's word order."""
    mock_modbus_unit.holding[1020] = registers

    heat_pump = component_class(mock_modbus_unit, base_offset=1000)
    await heat_pump.async_update()

    assert heat_pump.compressor_power_consumption_accumulated == expected


@pytest.mark.asyncio
async def test_a_write_reverses_the_scale(mock_modbus_unit):
    """Writing an engineering value stores the raw register the device expects."""
    controller = LambdaHeatPump(mock_modbus_unit, num_boil=1)

    await controller.boilers[0].write("target_high_temperature", 52.5)

    assert await mock_modbus_unit.read_holding_registers(2050, 1) == [525]


@pytest.mark.asyncio
async def test_the_whole_device_reads_in_few_calls(mock_modbus_unit):
    """Pooled planning collapses ~180 registers into a handful of block reads."""
    counting = CountingUnit(mock_modbus_unit)
    controller = LambdaHeatPump(
        counting, num_hps=2, num_boil=1, num_buff=1, num_sol=1, num_hc=2
    )

    await controller.async_update()

    # 2 general + 2x16 heat pump + 2 boiler + 2 buffer + 3 solar + 2x3 circuit.
    # 24 of the 47 are the capacity limits, which the controller only serves one
    # register at a time; the other ~150 registers take 23 reads.
    assert counting.reads == 47
