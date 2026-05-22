# CAN IDs — all 29-bit extended
CAN_ID_PC_TO_PLC = 0x1FFFFFF1   # PC → PLC  (handshake, reset, boot)
CAN_ID_PLC_TO_PC = 0x1FFFFFF2   # PLC → PC  (all ACKs / responses)
CAN_ID_PC_DATA   = 0x1FFFFFF3   # PC → PLC  (data stream)

# Handshake bytes
HANDSHAKE_TX = bytes([0x20, 0x10, 0x17, 0x01, 0x18, 0xD8])
HANDSHAKE_RX = bytes([0x21, 0x10, 0x17, 0x01, 0x18, 0xD8])

# Soft-reset command
RESET_CMD    = bytes([0x20, 0x00])

# Boot-trigger command
BOOT_TRIGGER = bytes([0x02, 0x02])

# Patterns seen at the end of the 55-packet boot enumeration sequence
BOOT_END_SEQ = [
    bytes((0, 0, 1, 0)),
    bytes((0, 1, 1, 0)),
    bytes((0, 0, 0, 1)),
]

# Final packet that signals boot complete
BOOT_DONE_PACKET = bytes((0, 0, 0, 1))

# Data-stream magic / ACK bytes
DATA_HEADER_MAGIC = bytes((0x53, 0x31, 0x23))   # 'S1#'
DATA_HEADER_ACK   = bytes((0x00, 0x01, 0x01, 0x01, 0x01))
DATA_CHUNK_ACK    = bytes([0x00, 0x01])
DATA_FINAL_ACK    = bytes((0x00, 0x00, 0x01))

# Block layout (all values are byte counts)
# Header: 3 magic + 2 offset + 5 data = 8 bytes
# Chunks: 3 × 8 bytes
# Final:  5 data + 1 CRC = 6 bytes
# Total transmitted per block = 8 + 8 + 8 + 8 + 6 = 38
DATA_BLOCK_SIZE       = 38
DATA_CHUNKS_PER_BLOCK = 3
DATA_FINAL_SIZE       = 6        # bytes in the final frame (5 data + 1 CRC)
DATA_OFFSET_INCREMENT = 0x20     # offset advances 0x20 per block

# Timeouts in seconds
TIMEOUT_HANDSHAKE = 2.0
TIMEOUT_RESET_ACK = 2.0
TIMEOUT_BOOT_ENUM = 10.0
TIMEOUT_BLOCK_ACK = 2.0
TIMEOUT_CHUNK_ACK = 1.0

# Supported module types → CAN settings
MODULE_TYPES = {
    'MRS 1.107.110.00':        {'can_fd': False, 'bitrate': 250000},
    'MRS 1.111.311.00':        {'can_fd': False, 'bitrate': 250000},
    'MRS 1.158.310.00 CAN FD': {'can_fd': True,  'bitrate': 500000},
}

DEFAULT_CHANNEL = 'PCAN_USBBUS1'
DEFAULT_BITRATE = 250000
