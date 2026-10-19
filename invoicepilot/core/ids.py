import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562, version 7).

    Primary keys stay non-enumerable in URLs, while the timestamp prefix keeps
    B-tree inserts close to append-only.
    """
    if hasattr(uuid, "uuid7"):  # Python 3.14+
        return uuid.uuid7()
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (unix_ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76  # version
    value |= ((rand >> 68) & 0xFFF) << 64  # rand_a, 12 bits
    value |= 0b10 << 62  # RFC 9562 variant
    value |= rand & 0x3FFF_FFFF_FFFF_FFFF  # rand_b, 62 bits
    return uuid.UUID(int=value)
