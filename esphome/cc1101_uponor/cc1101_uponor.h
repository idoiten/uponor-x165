#pragma once

#include "esphome/core/component.h"
#include "esphome/core/hal.h"
#include "esphome/components/spi/spi.h"

namespace esphome {
namespace cc1101_uponor {

// Receive-only CC1101 bring-up for Uponor Smatrix Wave RF frames.
// See README.md in this directory for the RF parameters and register
// choices this is built from.
class CC1101Uponor : public Component,
                      public spi::SPIDevice<spi::BIT_ORDER_MSB_FIRST,
                                             spi::CLOCK_POLARITY_LOW,
                                             spi::CLOCK_PHASE_LEADING,
                                             spi::DATA_RATE_4MHZ> {
 public:
  void set_gdo0_pin(InternalGPIOPin *pin) { gdo0_pin_ = pin; }

  void setup() override;
  void loop() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

 protected:
  void reset_chip_();
  uint8_t strobe_(uint8_t strobe);
  void write_reg_(uint8_t reg, uint8_t value);
  uint8_t read_status_reg_(uint8_t reg);
  void apply_config_();
  void enter_rx_();
  // Reads one complete packet out of the RX FIFO once GDO0 signals one is
  // ready, logs it, and re-arms RX. Returns false if the chip reported no
  // usable packet (e.g. a CRC failure that still toggled the line).
  bool read_and_log_packet_();

  InternalGPIOPin *gdo0_pin_{nullptr};
  bool last_gdo0_state_{false};
};

}  // namespace cc1101_uponor
}  // namespace esphome
