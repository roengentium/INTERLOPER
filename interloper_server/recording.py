import struct
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import List, Tuple

MAGIC = b"ILDR"

TICK_STRUCT_FMT = "<3f3fI3f"
TICK_SIZE = struct.calcsize(TICK_STRUCT_FMT)  # 40 bytes

HEADER_FMT = "<4sBB8sI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

EVENT_HEADER_FMT = "<IBB"


class Button(IntFlag):
    """Bit values match the Source SDK's IN_* flags (in_buttons.h) so the
    buttons field is meaningful to any Source client without translation."""
    ATTACK = 1 << 0
    JUMP = 1 << 1
    DUCK = 1 << 2
    FORWARD = 1 << 3
    BACK = 1 << 4
    USE = 1 << 5
    MOVELEFT = 1 << 9
    MOVERIGHT = 1 << 10
    ATTACK2 = 1 << 11
    RELOAD = 1 << 13
    SPEED = 1 << 17   # sprint in HL2


class EventType(IntEnum):
    KILL = 0
    SOUND = 1
    WEAPON_EQUIP = 2
    ENTITY_DELETE = 3
    ENTITY_SPAWN = 4


class CommitFormat(IntEnum):
    STANDARD = 0   # "##-##-##"
    TODAY = 1      # Type 7 special case: reads as "COMMITTED TODAY"


@dataclass
class Tick:
    origin: Tuple[float, float, float]
    angles: Tuple[float, float, float]  # pitch, yaw, roll
    buttons: int
    move: Tuple[float, float, float]    # forward, side, up

    def pack(self) -> bytes:
        return struct.pack(
            TICK_STRUCT_FMT,
            self.origin[0], self.origin[1], self.origin[2],
            self.angles[0], self.angles[1], self.angles[2],
            self.buttons & 0xFFFFFFFF,
            self.move[0], self.move[1], self.move[2],
        )

    @staticmethod
    def unpack(data: bytes) -> "Tick":
        ox, oy, oz, ap, ay, ar, buttons, mf, ms, mu = struct.unpack(TICK_STRUCT_FMT, data)
        return Tick((ox, oy, oz), (ap, ay, ar), buttons, (mf, ms, mu))


@dataclass
class Event:
    tick_index: int
    event_type: EventType
    param: bytes = b""

    def pack(self) -> bytes:
        if len(self.param) > 255:
            raise ValueError("event param must be <= 255 bytes")
        return struct.pack(EVENT_HEADER_FMT, self.tick_index, int(self.event_type), len(self.param)) + self.param


@dataclass
class Recording:
    demo_type: int
    commit_format: CommitFormat
    commit_number: str
    ticks: List[Tick] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)

    def serialize(self) -> bytes:
        commit_bytes = self.commit_number.encode("ascii", "replace")[:8].ljust(8, b"\x00")
        header = struct.pack(
            HEADER_FMT,
            MAGIC,
            self.demo_type,
            int(self.commit_format),
            commit_bytes,
            len(self.ticks),
        )
        tick_data = b"".join(t.pack() for t in self.ticks)
        event_data = struct.pack("<I", len(self.events)) + b"".join(e.pack() for e in self.events)
        return header + tick_data + event_data

    @staticmethod
    def deserialize(data: bytes) -> "Recording":
        """Reference implementation for testing the server's own output.
        A real game client would reimplement this in C++."""
        magic, demo_type, commit_format, commit_bytes, tick_count = struct.unpack(
            HEADER_FMT, data[:HEADER_SIZE]
        )
        if magic != MAGIC:
            raise ValueError("bad magic, not an ILDR recording")

        offset = HEADER_SIZE
        ticks = []
        for _ in range(tick_count):
            ticks.append(Tick.unpack(data[offset:offset + TICK_SIZE]))
            offset += TICK_SIZE

        event_count, = struct.unpack("<I", data[offset:offset + 4])
        offset += 4
        events = []
        for _ in range(event_count):
            tick_index, event_type, param_len = struct.unpack(
                EVENT_HEADER_FMT, data[offset:offset + 6]
            )
            offset += 6
            param = data[offset:offset + param_len]
            offset += param_len
            events.append(Event(tick_index, EventType(event_type), param))

        commit_number = commit_bytes.split(b"\x00", 1)[0].decode("ascii", "replace")
        return Recording(demo_type, CommitFormat(commit_format), commit_number, ticks, events)
