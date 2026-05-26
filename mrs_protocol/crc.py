"""
CRC-8 checksum for MRS PLC flash data blocks.

Algorithm: CRC-8, polynomial 0x09.
Initial value: first byte of PLC identity (from boot announcement).
Chaining: each block's init = previous block's CRC result.
"""
from .constants import CRC_POLYNOMIAL


def block_crc(data: bytes, init: int = 0x00, poly: int = CRC_POLYNOMIAL) -> int:
    """
    Compute the CRC-8 for a data block.

    Args:
        data: payload bytes (32 bytes for a standard block).
        init: initial CRC value — first block uses the PLC identity byte,
              subsequent blocks use the previous block's CRC.
        poly: CRC polynomial (default 0x09).

    Returns:
        Single CRC byte (0x00–0xFF).
    """
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def verify_block(payload: bytes, received_crc: int, init: int = 0x00) -> bool:
    """Verify a received block against its CRC."""
    return block_crc(payload, init) == received_crc
