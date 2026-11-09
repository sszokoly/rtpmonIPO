"""Captured SysMonitor startup messages required to receive monitor events."""

from __future__ import annotations

from typing import Final

# These messages reproduce the default Monitor startup captured from IP Office
# 11.1.2.2. The filter bitmap is the h323summary=0 variant, which was observed
# receiving RTP(END) event 0x0233 records.
INITIALIZE: Final = bytes.fromhex(
    "00000000000000040000029401094b57227f737123472e"
)
INITIALIZE_COMPLETE_EVENT: Final = 0x00C7
KEEPALIVE: Final = bytes.fromhex("000000000000000600000294010400000000")

FILTER_QUERY: Final = bytes.fromhex("0000000000000001000000fb0000")
EVENT_FILTER: Final = bytes.fromhex(
    "000000000000005e0000000c01ffff01ffff01ffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffff01ffffffffffffffffffffffffffffffffffffff"
    "ff0101ffff01ffffffffffffffffffffffffffffffffffff010101ffff01ff01"
    "01ff01010101010101010101ffff01ffffff01ffff0101ffffffff0101ffff01"
    "ffffffffffffff00ffffffffffffffffffffffffffff01ffffffffffffffffff"
    "ffffffffffff0101ffffffffffffffffff0101ffffffffffffffffffffffffff"
    "ffffffffffffffffffffff01ffffffffffffffffffffffffffffffffffff0101"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "01ffffffffffffff01ffff01ffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffff01ffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffff010101ffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffff01ffffffff"
    "ffffffffffffffffffffffffffffffffffffff01ffffffffff0101ffffffffff"
    "ffffff0101010101ffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffff0101ffffffffffffffffffff"
    "ffffffffffffffffffffff000000ffffffffffffffff010101ffffffffffffff"
    "ffffffffff0101ffffffffffffffffffffff01ffffffffffffffffffff01ffff"
    "ff01ff01ffffffffffffffffffffffffff0101ffffffffffffffffffffffff01"
)
FILTER_COMPLETE_EVENT: Final = 0x02DF

POST_FILTER_COMMANDS: Final = (
    bytes.fromhex("00000000000000010000021b0100"),
    bytes.fromhex("0000000000000001000000b5010400000001"),
    bytes.fromhex("0000000000000001000000a5010400000004"),
    bytes.fromhex("0000000000000001000000bf010400000004"),
    bytes.fromhex("000000000000000100000296010400000004"),
    bytes.fromhex("0000000000000001000002a5010400000002"),
    bytes.fromhex("0000000000000001000002a6010400000004"),
    bytes.fromhex("0000000000000001000002df0100"),
    bytes.fromhex("0000000000000001000002c1010400000004"),
    bytes.fromhex("0000000000000001000002c3010400000004"),
    bytes.fromhex("0000000000000001000001fb0100"),
    bytes.fromhex("000000000000005b00000000000102"),
    bytes.fromhex(
        "000000000000005e000000a501ffffffffffffffffffffffffffffff01ffffff"
        "ffffffffffff01ffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffff01ffffffffffffffffffffffffffffffff"
        "ffff01ffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffff01ffff"
        "ffffffffffffffffffffffff0101ffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffff01ff01ffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffff01"
    ),
    bytes.fromhex("00000000000000010000021b0100"),
    bytes.fromhex("0000000000000001000000b5010400000001"),
    bytes.fromhex("0000000000000001000000a5010400000004"),
    bytes.fromhex("0000000000000001000000bf010400000004"),
    bytes.fromhex("000000000000000100000296010400000004"),
    bytes.fromhex("0000000000000001000002a5010400000002"),
    bytes.fromhex("0000000000000001000002a6010400000004"),
    bytes.fromhex("0000000000000001000002df0100"),
    bytes.fromhex("0000000000000001000002c1010400000004"),
    bytes.fromhex("0000000000000001000002c3010400000004"),
    bytes.fromhex("0000000000000001000001fb0100"),
)
