"""Known Uponor Smatrix Wave thermostat devices, mapped to room names.

Confirmed 2026-10-01 by setting each room's physical thermostat to a
distinct, unambiguous setpoint and matching the resulting setpoint seen
in decoded L36/L51 RF frames (tag 0x3B) back to the room it was set on.

Note: right after a setpoint change, the L36 frame's setpoint updates
faster than the L51 frame's -- L51 can lag behind by several transmit
cycles before it catches up to the new value. Prefer L36 when reading
setpoint immediately after a change.
"""

from __future__ import annotations

ROOM_NAMES: dict[bytes, str] = {
    bytes.fromhex("1008B4DD"): "Sovrum 1",
    bytes.fromhex("1008B4E4"): "Sovrum 2",
    bytes.fromhex("1008BF76"): "K-E-V",
    bytes.fromhex("1008BFA2"): "Klädvård",
    bytes.fromhex("1008BF92"): "Badrum",
    bytes.fromhex("1008B4CD"): "Allrum",
    bytes.fromhex("1008B4C9"): "Sovrum 3",
    bytes.fromhex("1008B36E"): "WC",
    bytes.fromhex("1008B4E2"): "Sovrum 4",
}


def room_name(device_id: bytes) -> str | None:
    """Look up the room name for a thermostat's RF device_id, if known."""
    return ROOM_NAMES.get(device_id)


# Maps the I-167 remote-setpoint command's "room_primary" byte (see
# interface.RemoteSetpointFrame) to a room name. This is a completely
# different ID space from ROOM_NAMES above -- it has no relation to a
# thermostat's own 4-byte device_id, and is only ever a single byte.
#
# 0x7C (Sovrum 2) and 0xD0 (Sovrum 4) were confirmed directly: changing one
# room's setpoint at a time via the I-167 and matching which room_primary
# code carried the new value in the resulting RemoteSetpointFrame (sent
# three times in quick succession right after the change).
#
# The other seven were solved from the I-167 app's own room-list channel
# numbers (11, 12, 14-1A hex -- 13 is skipped, apparently unused) via the
# exact linear relationship room_primary = 21 * channel - 296, derived from
# the two confirmed pairs above and verified against every observed-but-
# then-unidentified room_primary byte in the capture logs.
ROOM_PRIMARY_NAMES: dict[int, str] = {
    0x3D: "Klädvård",
    0x52: "K-E-V",
    0x7C: "Sovrum 2",
    0x91: "WC",
    0xA6: "Sovrum 1",
    0xBB: "Badrum",
    0xD0: "Sovrum 4",
    0xE5: "Sovrum 3",
    0xFA: "Allrum",
}


def room_primary_name(room_primary: int) -> str | None:
    """Look up the room name for a RemoteSetpointFrame's room_primary byte."""
    return ROOM_PRIMARY_NAMES.get(room_primary)


# Bridges the two independent ID spaces above by matching on room name, so a
# RemoteSetpointFrame's min/max setpoint can be attached to the same MQTT
# device as that room's thermostat (ROOM_NAMES' device_id), instead of only
# being known by its unrelated room_primary byte.
_NAME_TO_DEVICE_ID: dict[str, bytes] = {name: device_id for device_id, name in ROOM_NAMES.items()}
ROOM_PRIMARY_TO_DEVICE_ID: dict[int, bytes] = {
    room_primary: _NAME_TO_DEVICE_ID[name]
    for room_primary, name in ROOM_PRIMARY_NAMES.items()
    if name in _NAME_TO_DEVICE_ID
}


def device_id_for_room_primary(room_primary: int) -> bytes | None:
    """Look up a thermostat's 4-byte device_id for a room_primary byte, via its room name."""
    return ROOM_PRIMARY_TO_DEVICE_ID.get(room_primary)
