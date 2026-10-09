"""Packet format for the emergency mesh network.

The byte layout here is the one intended for the ESP32 firmware, so the
simulator works with realistic packet sizes and LoRa time-on-air.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag


class PacketType(IntEnum):
    SOS = 1
    ACK = 2
    HEARTBEAT = 3


class Priority(IntEnum):
    SAFE = 0       # "I'm safe" check-in
    SUPPLIES = 1   # food, water, medicine needed
    URGENT = 2     # water rising, children or elderly at risk
    CRITICAL = 3   # life-threatening: injured, trapped, medical


class EmergencyType(IntEnum):
    MEDICAL = 1
    TRAPPED = 2
    FLOODING = 3
    FIRE = 4
    COLLAPSE = 5
    OTHER = 6


class Flag(IntFlag):
    NONE = 0
    INJURED = 1
    TRAPPED = 2
    CHILD = 4
    ELDERLY = 8
    WATER_RISING = 16


DEFAULT_HOP_LIMIT = 6
MAX_LANDMARK_BYTES = 16

# type, origin, seq, attempt, hop_limit, hop_count, priority  -> 9 bytes
HEADER = struct.Struct("<BHHBBBB")
# emergency_type, people, flags, session, landmark_length     -> 6 bytes (+ landmark)
SOS_BODY = struct.Struct("<BBBHB")
# ref_origin, ref_seq                                          -> 4 bytes
ACK_BODY = struct.Struct("<HH")
# battery_mv, uptime_s, neighbours                             -> 7 bytes
HEARTBEAT_BODY = struct.Struct("<HIB")

MAX_SOS_BYTES = HEADER.size + SOS_BODY.size + MAX_LANDMARK_BYTES


@dataclass(frozen=True)
class Packet:
    type: PacketType
    origin: int
    seq: int
    attempt: int = 1
    hop_limit: int = DEFAULT_HOP_LIMIT
    hop_count: int = 0
    priority: Priority = Priority.SAFE
    # SOS fields
    emergency_type: int = 0
    people: int = 0
    flags: int = 0
    session: int = 0  # sender ID: the home unit's ID (rate limit, address lookup)
    landmark: str = ""
    # ACK fields
    ref_origin: int = 0
    ref_seq: int = 0
    # HEARTBEAT fields
    battery_mv: int = 0
    uptime_s: int = 0
    neighbours: int = 0
    # Simulation-only metadata: never encoded or sent over the air.
    path: tuple = field(default=(), compare=False)
    created_at: float = field(default=0.0, compare=False)

    @property
    def msg_key(self) -> tuple:
        """Unique ID of this transmission attempt (used for deduplication)."""
        return (int(self.type), self.origin, self.seq, self.attempt)

    @property
    def incident_key(self) -> tuple:
        """Unique ID of the emergency itself, shared by all retry attempts."""
        return (self.origin, self.seq)


def _landmark_bytes(text: str) -> bytes:
    raw = text.encode("utf-8")[:MAX_LANDMARK_BYTES]
    # Drop any multi-byte character that the cut left incomplete.
    return raw.decode("utf-8", "ignore").encode("utf-8")


def encode(p: Packet) -> bytes:
    """Serialise a packet to the over-the-air byte format."""
    try:
        head = HEADER.pack(int(p.type), p.origin, p.seq, p.attempt,
                           p.hop_limit, p.hop_count, int(p.priority))
        if p.type == PacketType.SOS:
            lm = _landmark_bytes(p.landmark)
            return head + SOS_BODY.pack(int(p.emergency_type), p.people,
                                        int(p.flags), p.session, len(lm)) + lm
        if p.type == PacketType.ACK:
            return head + ACK_BODY.pack(p.ref_origin, p.ref_seq)
        if p.type == PacketType.HEARTBEAT:
            return head + HEARTBEAT_BODY.pack(p.battery_mv, p.uptime_s, p.neighbours)
    except struct.error as exc:
        raise ValueError(f"field out of range: {exc}") from exc
    raise ValueError(f"unknown packet type {p.type!r}")


def decode(data: bytes) -> Packet:
    """Parse bytes received over the air back into a Packet."""
    if len(data) < HEADER.size:
        raise ValueError("packet too short for header")
    t, origin, seq, attempt, hop_limit, hop_count, prio = HEADER.unpack_from(data)
    try:
        ptype = PacketType(t)
        priority = Priority(prio)
    except ValueError as exc:
        raise ValueError(f"invalid header: {exc}") from exc
    body = data[HEADER.size:]
    common = dict(type=ptype, origin=origin, seq=seq, attempt=attempt,
                  hop_limit=hop_limit, hop_count=hop_count, priority=priority)

    if ptype == PacketType.SOS:
        if len(body) < SOS_BODY.size:
            raise ValueError("SOS body too short")
        etype, people, flags, session, n = SOS_BODY.unpack_from(body)
        lm = body[SOS_BODY.size:SOS_BODY.size + n]
        if len(lm) != n:
            raise ValueError("landmark truncated")
        return Packet(**common, emergency_type=etype, people=people, flags=flags,
                      session=session, landmark=lm.decode("utf-8", "ignore"))
    if ptype == PacketType.ACK:
        if len(body) < ACK_BODY.size:
            raise ValueError("ACK body too short")
        ref_origin, ref_seq = ACK_BODY.unpack_from(body)
        return Packet(**common, ref_origin=ref_origin, ref_seq=ref_seq)
    if len(body) < HEARTBEAT_BODY.size:
        raise ValueError("heartbeat body too short")
    battery_mv, uptime_s, neighbours = HEARTBEAT_BODY.unpack_from(body)
    return Packet(**common, battery_mv=battery_mv, uptime_s=uptime_s, neighbours=neighbours)


def lora_airtime(payload_bytes: int, spreading_factor: int = 9, bandwidth_hz: int = 125_000,
                 coding_rate: int = 1, preamble_symbols: int = 8,
                 crc: bool = True, explicit_header: bool = True) -> float:
    """Time on air in seconds for one LoRa packet.

    Uses the standard SX127x time-on-air formula published by Semtech.
    coding_rate 1..4 means 4/5..4/8. Low-data-rate optimisation switches on
    automatically when a symbol lasts longer than 16 ms.
    """
    if not 6 <= spreading_factor <= 12:
        raise ValueError("spreading factor must be 6-12")
    t_sym = (2 ** spreading_factor) / bandwidth_hz
    low_dr = 1 if t_sym > 0.016 else 0
    ih = 0 if explicit_header else 1
    numerator = 8 * payload_bytes - 4 * spreading_factor + 28 + 16 * int(crc) - 20 * ih
    payload_symbols = 8 + max(
        math.ceil(numerator / (4 * (spreading_factor - 2 * low_dr))) * (coding_rate + 4), 0)
    return (preamble_symbols + 4.25) * t_sym + payload_symbols * t_sym
