// CC1101 register addresses, strobes and the RX-only configuration table used
// to receive Uponor Smatrix Wave thermostat frames.
//
// Values derived from the RF parameters already reverse-engineered in the
// Python/RTL-SDR receiver (uponor_smatrix_wave_x165/{protocol,crc,dsp}.py):
//   - carrier 868.25 MHz
//   - ~38.4 kBaud (measured 38377-38382 baud)
//   - ~16.5-20 kHz deviation
//   - sync word D3 91 D3 91 (= CC1101's native 32-bit sync: a 16-bit sync
//     word transmitted twice)
//   - variable-length packets, length byte = payload length excluding CRC
//   - CRC-16, poly 0x8005, init 0xFFFF, non-reflected -- identical to
//     CC1101's built-in hardware CRC
#pragma once
#include <cstdint>

namespace cc1101_uponor {

// --- Strobe commands (single-byte, no data phase) ---
enum Strobe : uint8_t {
  SRES = 0x30,     // Reset chip
  SFSTXON = 0x31,
  SXOFF = 0x32,
  SCAL = 0x33,      // Calibrate frequency synthesizer
  SRX = 0x34,       // Enable RX
  STX = 0x35,
  SIDLE = 0x36,     // Exit RX/TX, go to IDLE
  SWOR = 0x38,
  SPWD = 0x39,
  SFRX = 0x3A,      // Flush RX FIFO (must be IDLE first)
  SFTX = 0x3B,
  SWORRST = 0x3C,
  SNOP = 0x3D,
};

// --- Config register addresses (written with the SPI "write" bit, 0x00|addr) ---
enum Reg : uint8_t {
  IOCFG2 = 0x00,
  IOCFG0 = 0x02,
  FIFOTHR = 0x03,
  SYNC1 = 0x04,
  SYNC0 = 0x05,
  PKTLEN = 0x06,
  PKTCTRL1 = 0x07,
  PKTCTRL0 = 0x08,
  ADDR = 0x09,
  CHANNR = 0x0A,
  FSCTRL1 = 0x0B,
  FSCTRL0 = 0x0C,
  FREQ2 = 0x0D,
  FREQ1 = 0x0E,
  FREQ0 = 0x0F,
  MDMCFG4 = 0x10,
  MDMCFG3 = 0x11,
  MDMCFG2 = 0x12,
  MDMCFG1 = 0x13,
  MDMCFG0 = 0x14,
  DEVIATN = 0x15,
  MCSM2 = 0x16,
  MCSM1 = 0x17,
  MCSM0 = 0x18,
  FOCCFG = 0x19,
  BSCFG = 0x1A,
  AGCCTRL2 = 0x1B,
  AGCCTRL1 = 0x1C,
  AGCCTRL0 = 0x1D,
  FREND1 = 0x21,
  FREND0 = 0x22,
  FSCAL3 = 0x23,
  FSCAL2 = 0x24,
  FSCAL1 = 0x25,
  FSCAL0 = 0x26,
  TEST2 = 0x2C,
  TEST1 = 0x2D,
  TEST0 = 0x2E,
};

// --- Status registers (read with burst+read bits, 0xC0|addr) ---
enum StatusReg : uint8_t {
  PARTNUM = 0x30,
  VERSION = 0x31,
  MARCSTATE = 0x35,
  RXBYTES = 0x3B,
};

struct RegVal {
  uint8_t reg;
  uint8_t val;
};

// RX-only config. GFSK is tried first (MDMCFG2=0x13); if bring-up shows no
// sync detection at all, switch MDMCFG2 to 0x03 for plain 2-FSK -- the
// RTL-SDR software demod cannot distinguish the two from IQ samples alone,
// so this is the one parameter we genuinely don't know for certain yet.
inline constexpr RegVal kConfig[] = {
    // GDO0 asserts on sync-word found / packet received with CRC OK,
    // de-asserts once the first byte is read out of the RX FIFO. This is
    // the interrupt line the firmware waits on.
    {IOCFG0, 0x06},

    // Default FIFO threshold (not critical for our short ~51-byte frames,
    // we read the whole packet after GDO0 fires rather than streaming it).
    {FIFOTHR, 0x47},

    // Sync word: D391 D391, hardware-matched as CC1101's native 32-bit sync.
    {SYNC1, 0xD3},
    {SYNC0, 0x91},

    // Variable packet length mode: PKTLEN is just the *maximum* accepted
    // length here, not a fixed size. Longest observed frame is 51 bytes
    // total; minus 8 (sync, stripped in hardware) minus 1 (length byte
    // itself) = 42 bytes of payload+CRC. Leave generous headroom.
    {PKTLEN, 0x3E},  // 62

    // PKTCTRL1: default — APPEND_STATUS=1 (RSSI/LQI/CRC_OK appended after
    // payload in the RX FIFO), no address filtering.
    {PKTCTRL1, 0x04},

    // PKTCTRL0: CRC_EN=1 (hardware CRC check, matches crc16_cms exactly),
    // LENGTH_CONFIG=01 (variable length, first RX byte = length).
    {PKTCTRL0, 0x05},

    // No address filtering used.
    {ADDR, 0x00},
    {CHANNR, 0x00},

    // IF frequency: 152 kHz, the standard value for a 26 MHz crystal.
    {FSCTRL1, 0x06},
    {FSCTRL0, 0x00},

    // Carrier 868.25 MHz: FREQ = round(868_250_000 * 2^16 / 26_000_000).
    {FREQ2, 0x21},
    {FREQ1, 0x64},
    {FREQ0, 0xF1},

    // Data rate ~38.38 kBaud (DRATE_E=10, DRATE_M=131) -- TI's standard
    // "38.4 kBaud" preset, which lands almost exactly on our measured
    // 38377-38382 baud. Upper nibble sets channel filter bandwidth to
    // ~101.6 kHz (CHANBW_E=3, CHANBW_M=0), comfortably wider than our
    // ~16.5-20 kHz deviation needs.
    {MDMCFG4, 0xCA},
    {MDMCFG3, 0x83},

    // Modulation GFSK, 32/32 sync word bits required, no Manchester.
    // -> try MDMCFG2 = 0x03 (2-FSK) instead if bring-up finds no sync at all.
    {MDMCFG2, 0x13},

    // 4-byte preamble (matches the AA AA AA AA we already see), default
    // channel spacing (single fixed channel, irrelevant here).
    {MDMCFG1, 0x22},
    {MDMCFG0, 0xF8},

    // ~20.6 kHz frequency deviation (DEVIATN_E=3, DEVIATN_M=5) -- within
    // the ~16.5-20 kHz range implied by the two FSK tones seen in the
    // RTL-SDR captures, with a little margin for crystal drift.
    {DEVIATN, 0x35},

    // MCSM2 default (RX timeout disabled by MCSM1 below, so irrelevant).
    {MCSM2, 0x07},
    // MCSM1: CCA always, stay in RX forever after a packet (we want
    // continuous monitoring, not one-shot RX).
    {MCSM1, 0x3C},
    // MCSM0: auto-calibrate when leaving IDLE for RX.
    {MCSM0, 0x18},

    // Standard frequency-offset-compensation / bit-sync / AGC settings for
    // this data rate and channel bandwidth (TI SmartRF defaults). These are
    // reasonable starting points; AGCCTRL may need retuning once real
    // over-the-air packets are being received.
    {FOCCFG, 0x16},
    {BSCFG, 0x6C},
    {AGCCTRL2, 0x43},
    {AGCCTRL1, 0x40},
    {AGCCTRL0, 0x91},

    {FREND1, 0xB6},
    {FREND0, 0x10},

    // Frequency synthesizer calibration defaults (TI standard).
    {FSCAL3, 0xE9},
    {FSCAL2, 0x2A},
    {FSCAL1, 0x00},
    {FSCAL0, 0x1F},

    // TI-recommended test register values (undocumented tuning, always
    // set to these defaults regardless of application).
    {TEST2, 0x81},
    {TEST1, 0x35},
    {TEST0, 0x09},
};

}  // namespace cc1101_uponor
