"""H323Evt-only startup captured in data/ipo_h323evt_only.txt."""

from __future__ import annotations

from typing import Final

INITIALIZE: Final = bytes.fromhex(
    "00000000000000040000029401094b57227f737123472e"
)
INITIALIZE_COMPLETE_EVENT: Final = 0x00C7
KEEPALIVE: Final = bytes.fromhex("000000000000000600000294010400000000")
FILTER_QUERY: Final = bytes.fromhex("0000000000000001000000fb0000")


def _h323_event_filter() -> bytes:
    """Reproduce the opaque 583-byte bitmap in capture frame 27 exactly."""
    bitmap = bytearray(bytes.fromhex("000000000000005e000000b5"))
    bitmap.extend(b"\xff" * (583 - len(bitmap)))
    # Absolute payload offsets; no meaning is inferred for individual flags.
    for offset in (
        0x0C, 0x1E, 0x1E2, 0x1E3, 0x1E4, 0x1FC, 0x1FD, 0x236, 0x246
    ):
        bitmap[offset] = 0
    for offset in (0x75, 0x76, 0x14C, 0x172, 0x18A, 0x1CC, 0x1EE):
        bitmap[offset] = 1
    return bytes(bitmap)


EVENT_FILTER: Final = _h323_event_filter()
POST_FILTER_COMMANDS: Final = (
    bytes.fromhex("00000000000000010000021b0100"),
    bytes.fromhex("0000000000000001000002df0000"),
    bytes.fromhex("0000000000000001000001fb0100"),
    bytes.fromhex("000000000000005b00000000000102"),
    bytes.fromhex("00000000000000010000021b0100"),
    bytes.fromhex("0000000000000001000002df0000"),
    bytes.fromhex("0000000000000001000002080100"),
    bytes.fromhex("0000000000000001000001fb0100"),
)
# Frame 30 answers the post-filter requests; no 0x02df response is captured.
FILTER_COMPLETE_EVENT: Final = 0x01FB
