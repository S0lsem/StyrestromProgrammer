def block_checksum(data: bytes) -> int:
    """
    Compute the 1-byte checksum for a data block.
    Algorithm: XOR of all payload bytes in the block (excluding the checksum
    byte itself).

    Args:
        data: raw payload bytes of the block (everything before the checksum byte)

    Returns:
        Single checksum byte (0x00–0xFF)
    """
    result = 0
    for b in data:
        result ^= b
    return result & 0xFF


def verify_block(payload: bytes, received_crc: int) -> bool:
    """Verify a received block against its checksum."""
    return block_checksum(payload) == received_crc
