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
# Confirmed by changing one room's setpoint at a time via the I-167 and
# matching which room_primary code carried the new value in the resulting
# RemoteSetpointFrame (it's sent three times in quick succession right after
# the change).
ROOM_PRIMARY_NAMES: dict[int, str] = {
    0x7C: "Sovrum 2",
    0xD0: "Sovrum 4",
    # Still unmapped: 0xFA, 0x3D, 0x52, 0x91, 0xA6, 0xBB, 0xE5.
}


def room_primary_name(room_primary: int) -> str | None:
    """Look up the room name for a RemoteSetpointFrame's room_primary byte."""
    return ROOM_PRIMARY_NAMES.get(room_primary)
