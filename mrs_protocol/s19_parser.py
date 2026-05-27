"""
Motorola S-record (S19 / S28 / S37) parser.

MRS Applics Studio's build step produces a single .s19 file containing the
linked firmware image. This module reads that text format and returns a
contiguous binary blob suitable for streaming to the PLC bootloader.

Record types handled:
  S0  - header (ignored)
  S1  - data, 16-bit address
  S2  - data, 24-bit address
  S3  - data, 32-bit address
  S5  - data record count (ignored)
  S7  - termination, 32-bit start address (ignored)
  S8  - termination, 24-bit start address (ignored)
  S9  - termination, 16-bit start address (ignored)

Checksums are verified on every record; a mismatch raises S19ParseError.
Gaps between record address ranges are filled with 0xFF (the erased-flash
default), so the result is one contiguous bytes object that maps directly
onto the PLC's flash from start_address upward.
"""
from __future__ import annotations

from dataclasses import dataclass


class S19ParseError(ValueError):
    """Raised when an S-record file is malformed or fails checksum."""


@dataclass(frozen=True)
class Firmware:
    """A parsed firmware image extracted from a Motorola S-record file.

    Attributes:
        start_address:  The lowest address referenced by any data record.
                        This is the address of byte 0 of ``data``.
        data:           Contiguous bytes covering start_address to the end
                        of the highest-addressed record. Gaps between
                        records are filled with 0xFF.
    """
    start_address: int
    data:          bytes

    def __len__(self) -> int:
        return len(self.data)


_DATA_RECORD_ADDR_BYTES = {
    'S1': 2,
    'S2': 3,
    'S3': 4,
}

_IGNORED_RECORDS = {'S0', 'S5', 'S7', 'S8', 'S9'}


def parse_s19(text: str) -> Firmware:
    """Parse Motorola S-record text and return the contiguous firmware image.

    Raises:
        S19ParseError: if the file contains no data records, has malformed
                       lines, or any record's checksum doesn't match.
    """
    records: list[tuple[int, bytes]] = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if len(line) < 4 or line[0] != 'S':
            raise S19ParseError(f'Line {line_no}: not an S-record: {raw!r}')

        rtype = line[:2]
        if rtype in _IGNORED_RECORDS:
            continue
        addr_bytes = _DATA_RECORD_ADDR_BYTES.get(rtype)
        if addr_bytes is None:
            raise S19ParseError(f'Line {line_no}: unknown record type {rtype}')

        try:
            byte_count = int(line[2:4], 16)
            body_hex = line[4:]
            if len(body_hex) != byte_count * 2:
                raise S19ParseError(
                    f'Line {line_no}: declared {byte_count} bytes but '
                    f'got {len(body_hex) // 2}'
                )
            body = bytes.fromhex(body_hex)
        except ValueError as exc:
            raise S19ParseError(f'Line {line_no}: bad hex — {exc}') from exc

        # Verify checksum: ones-complement of (byte_count + address + data)
        expected = (~(byte_count + sum(body[:-1])) & 0xFF)
        if body[-1] != expected:
            raise S19ParseError(
                f'Line {line_no}: checksum mismatch '
                f'(got 0x{body[-1]:02X}, expected 0x{expected:02X})'
            )

        address = int.from_bytes(body[:addr_bytes], 'big')
        data    = body[addr_bytes:-1]   # strip address and checksum
        records.append((address, data))

    if not records:
        raise S19ParseError('No data records found in S-record file.')

    min_addr = min(addr for addr, _ in records)
    max_addr = max(addr + len(data) for addr, data in records)

    image = bytearray(b'\xFF' * (max_addr - min_addr))
    for addr, data in records:
        offset = addr - min_addr
        image[offset: offset + len(data)] = data

    return Firmware(start_address=min_addr, data=bytes(image))
