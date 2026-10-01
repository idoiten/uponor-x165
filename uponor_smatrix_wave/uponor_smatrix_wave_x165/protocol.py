"""Frame validation and conservative thermostat TLV parsing."""

from __future__ import annotations

from dataclasses import dataclass

from .crc import crc16_cms


PREAMBLE_SYNC = bytes.fromhex("AA AA AA AA D3 91 D3 91")
DEFAULT_CONTROLLER_ID = None
# Discovery uses frame structure, never a personal ID list.
KNOWN_THERMOSTAT_IDS: set[bytes] = set()
SHORT_TAGS = (0x40, 0x3E, 0x3F, 0x3B)
MEDIUM_TAGS = (0x40, 0x3E, 0x3F, 0x3B, 0x42)
LONG_TAGS = (0x2D, 0x3D, 0x0C, 0x37, 0x38, 0x3B, 0x3C, 0x35, 0x39, 0x3A)


class FrameError(ValueError):
    """A candidate frame failed structural or CRC validation."""


@dataclass(frozen=True)
class ParsedThermostatFrame:
    raw: bytes
    controller_id: bytes
    device_id: bytes
    tlvs: dict[int, int]
    tlv_items: tuple[tuple[int, int], ...]
    unknown_inner_word: bytes

    @property
    def frame_name(self) -> str:
        return f"L{len(self.raw)}"

    @property
    def raw_temperature(self) -> int | None:
        return self.tlvs.get(0x40)

    @property
    def raw_setpoint(self) -> int | None:
        return self.tlvs.get(0x3B)

    @property
    def temperature_c(self) -> float | None:
        return uponor_temperature(self.raw_temperature) if self.raw_temperature is not None else None

    @property
    def setpoint_c(self) -> float | None:
        return uponor_temperature(self.raw_setpoint) if self.raw_setpoint is not None else None

    @property
    def bypass_enabled(self) -> bool | None:
        """Observed room bypass configuration bit from long status frames."""
        value = self.tlvs.get(0x35)
        return bool(value & 0x0001) if value is not None else None

    @property
    def actuator_open(self) -> bool | None:
        """Observed actuator-open bit from long status frames."""
        value = self.tlvs.get(0x3D)
        return bool(value & 0x0040) if value is not None else None


def uponor_temperature(raw: int) -> float:
    """Decode an Uponor uint16 value expressed in tenths Fahrenheit."""
    if not 0 <= raw <= 0xFFFF:
        raise ValueError("temperature raw value must be uint16")
    return ((raw / 10.0) - 32.0) * 5.0 / 9.0


def validate_frame(raw: bytes) -> bytes:
    """Validate sync, declared length and CRC; return the exact frame bytes."""
    if len(raw) < 11:
        raise FrameError("frame is shorter than minimum")
    if not raw.startswith(PREAMBLE_SYNC):
        raise FrameError("preamble/sync mismatch")
    declared = raw[8] + 11
    if len(raw) != declared:
        raise FrameError(f"declared length {declared}, actual length {len(raw)}")
    received = int.from_bytes(raw[-2:], "big")
    calculated = crc16_cms(raw[8:-2])
    if received != calculated:
        raise FrameError(f"CRC mismatch: received {received:04X}, calculated {calculated:04X}")
    return raw


def parse_thermostat_frame(
    raw: bytes,
    *,
    controller_id: bytes | None = DEFAULT_CONTROLLER_ID,
) -> ParsedThermostatFrame:
    """Parse a CRC-valid dual-endpoint thermostat frame.

    Discovery is conservative: when configured, the controller must occupy bytes
    09-12 and the complete ordered TLV tag sequence must match one of the three
    observed thermostat message structures (L33, L36, L51). The thermostat ID
    itself is not restricted to the currently known IDs.
    """
    raw = validate_frame(raw)
    if len(raw) not in (33, 36, 51):
        raise FrameError(f"unsupported thermostat frame length {len(raw)}")
    if controller_id is not None and len(controller_id) != 4:
        raise ValueError("controller_id must contain exactly 4 bytes")
    if controller_id is not None and raw[9:13] != controller_id:
        raise FrameError(f"unexpected controller endpoint {raw[9:13].hex().upper()}")
    device_id = raw[13:17]
    if len(device_id) != 4:
        raise FrameError("missing device endpoint")
    payload = raw[17:-4]
    if len(payload) % 3:
        raise FrameError("TLV payload is not divisible into 3-byte entries")
    items = tuple((payload[i], int.from_bytes(payload[i + 1:i + 3], "big")) for i in range(0, len(payload), 3))
    expected_tags = {33: SHORT_TAGS, 36: MEDIUM_TAGS, 51: LONG_TAGS}[len(raw)]
    if tuple(tag for tag, _ in items) != expected_tags:
        actual = " ".join(f"{tag:02X}" for tag, _ in items)
        raise FrameError(f"unexpected thermostat TLV tag sequence: {actual}")
    return ParsedThermostatFrame(
        raw=raw,
        controller_id=raw[9:13],
        device_id=device_id,
        tlvs=dict(items),
        tlv_items=items,
        unknown_inner_word=raw[-4:-2],
    )
