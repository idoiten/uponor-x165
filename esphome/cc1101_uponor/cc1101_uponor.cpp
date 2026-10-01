#include "cc1101_uponor.h"
#include "registers.h"
#include "esphome/core/log.h"

namespace esphome {
namespace cc1101_uponor {

static const char *const TAG = "cc1101_uponor";

// SPI header byte bits (CC1101 datasheet section 10.1).
static constexpr uint8_t kReadBit = 0x80;
static constexpr uint8_t kBurstBit = 0x40;
static constexpr uint8_t kFifoAddr = 0x3F;

uint8_t CC1101Uponor::strobe_(uint8_t strobe) {
  this->enable();
  uint8_t status = this->transfer_byte(strobe);
  this->disable();
  return status;
}

void CC1101Uponor::write_reg_(uint8_t reg, uint8_t value) {
  this->enable();
  this->transfer_byte(reg);
  this->transfer_byte(value);
  this->disable();
}

uint8_t CC1101Uponor::read_status_reg_(uint8_t reg) {
  // Status registers must be read with the burst bit set, per datasheet.
  this->enable();
  this->transfer_byte(kReadBit | kBurstBit | reg);
  uint8_t value = this->transfer_byte(0x00);
  this->disable();
  return value;
}

void CC1101Uponor::reset_chip_() {
  // Simplification for bring-up: a full CC1101 reset sequence waits for
  // SO (MISO) to go low after CSn asserts before clocking SRES, which
  // ESPHome's hardware-SPI abstraction doesn't expose directly. If the
  // chip doesn't come up reliably (PARTNUM/VERSION read as 0x00 or 0xFF
  // every time), that handshake is the first thing to add.
  this->disable();
  delayMicroseconds(40);
  this->strobe_(Strobe::SRES);
  delay(2);  // NOLINT -- datasheet: allow >240us after SRES before use
}

void CC1101Uponor::apply_config_() {
  for (const auto &rv : kConfig) {
    this->write_reg_(rv.reg, rv.val);
  }
}

void CC1101Uponor::enter_rx_() {
  this->strobe_(Strobe::SIDLE);
  this->strobe_(Strobe::SFRX);
  this->strobe_(Strobe::SRX);
}

void CC1101Uponor::setup() {
  this->spi_setup();
  this->gdo0_pin_->setup();

  this->reset_chip_();

  uint8_t partnum = this->read_status_reg_(StatusReg::PARTNUM);
  uint8_t version = this->read_status_reg_(StatusReg::VERSION);
  ESP_LOGI(TAG, "CC1101 PARTNUM=0x%02X VERSION=0x%02X (expect PARTNUM=0x00, "
                "VERSION=0x14 or 0x04 on genuine/clone chips)",
           partnum, version);
  if (version == 0x00 || version == 0xFF) {
    ESP_LOGE(TAG, "CC1101 not responding over SPI -- check wiring and 3.3V "
                  "power before looking at RF settings");
  }

  this->apply_config_();
  this->enter_rx_();
  ESP_LOGI(TAG, "CC1101 configured for Uponor Smatrix Wave RX, listening");
}

bool CC1101Uponor::read_and_log_packet_() {
  uint8_t rxbytes = this->read_status_reg_(StatusReg::RXBYTES);
  // Bit 7 set = RX FIFO overflow.
  if (rxbytes & 0x80) {
    ESP_LOGW(TAG, "RX FIFO overflow, flushing");
    this->strobe_(Strobe::SIDLE);
    this->strobe_(Strobe::SFRX);
    this->strobe_(Strobe::SRX);
    return false;
  }
  uint8_t available = rxbytes & 0x7F;
  if (available == 0) {
    return false;
  }

  this->enable();
  this->transfer_byte(kReadBit | kBurstBit | kFifoAddr);
  uint8_t length = this->transfer_byte(0x00);  // first byte = length field
  std::vector<uint8_t> payload;
  payload.reserve(length + 2);
  // +2 for the two CRC bytes CC1101 appends automatically; +2 more for the
  // RSSI/LQI+CRC_OK status bytes PKTCTRL1.APPEND_STATUS adds after that.
  for (uint8_t i = 0; i < length + 2 + 2; i++) {
    payload.push_back(this->transfer_byte(0x00));
  }
  this->disable();

  if (payload.size() < 2) {
    return false;
  }
  uint8_t crc_ok = payload.back() & 0x80;
  payload.pop_back();  // CRC_OK/LQI byte
  payload.pop_back();  // RSSI byte

  char hex[256] = {0};
  size_t pos = 0;
  pos += snprintf(hex, sizeof(hex), "%02X", length);
  for (uint8_t b : payload) {
    if (pos + 3 >= sizeof(hex)) break;
    pos += snprintf(hex + pos, sizeof(hex) - pos, " %02X", b);
  }
  ESP_LOGI(TAG, "packet (len=%u, crc=%s): %s", length, crc_ok ? "OK" : "BAD", hex);
  return true;
}

void CC1101Uponor::loop() {
  bool state = this->gdo0_pin_->digital_read();
  if (state && !this->last_gdo0_state_) {
    // Rising edge: packet received with CRC OK (per IOCFG0 = 0x06).
    this->read_and_log_packet_();
    // MCSM1.RXOFF_MODE=11 keeps the chip in RX, but the FIFO still needs
    // flushing/re-arming after each packet in this simple bring-up loop.
    this->strobe_(Strobe::SIDLE);
    this->strobe_(Strobe::SFRX);
    this->strobe_(Strobe::SRX);
  }
  this->last_gdo0_state_ = state;
}

}  // namespace cc1101_uponor
}  // namespace esphome
