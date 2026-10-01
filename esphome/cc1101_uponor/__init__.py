"""ESPHome external_component: CC1101 receive-only bring-up for Uponor
Smatrix Wave RF frames.

Scope of this first phase (see esphome/cc1101_uponor/README.md): verify SPI
communication with the chip, apply the RX register configuration derived
from the RTL-SDR findings, and log every hardware-validated (sync found +
CRC OK) packet as hex so it can be compared against what the existing
Python/RTL-SDR receiver decodes. TLV parsing and per-room sensors are a
later phase, once raw reception is confirmed working.

Example usage: see esphome/cc1101_uponor/example-bringup.yaml
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import pins
from esphome.components import spi
from esphome.const import CONF_ID

CODEOWNERS = ["@idoiten"]
DEPENDENCIES = ["spi"]

cc1101_uponor_ns = cg.esphome_ns.namespace("cc1101_uponor")
CC1101Uponor = cc1101_uponor_ns.class_(
    "CC1101Uponor", cg.Component, spi.SPIDevice
)

CONF_GDO0_PIN = "gdo0_pin"

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(CC1101Uponor),
            cv.Required(CONF_GDO0_PIN): pins.internal_gpio_input_pin_schema,
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(spi.spi_device_schema(cs_pin_required=True))
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await spi.register_spi_device(var, config)

    gdo0 = await cg.gpio_pin_expression(config[CONF_GDO0_PIN])
    cg.add(var.set_gdo0_pin(gdo0))
