# CAN IDs — all 29-bit extended
CAN_ID_PLC_BOOT  = 0x1FFFFFF0   # PLC → PC  (boot announcement)
CAN_ID_PC_TO_PLC = 0x1FFFFFF1   # PC → PLC  (handshake, reset, boot, memory read)
CAN_ID_PLC_TO_PC = 0x1FFFFFF2   # PLC → PC  (handshake ACKs, boot enum, data ACKs)
CAN_ID_PC_DATA   = 0x1FFFFFF3   # PC → PLC  (data stream)
CAN_ID_PLC_DATA  = 0x1FFFFFF4   # PLC → PC  (memory read responses)

# Boot announcement acknowledgement
BOOT_ACK = bytes([0x00, 0x00])

# Handshake prefix — the remaining bytes come from the PLC boot announcement
HANDSHAKE_TX_PREFIX = bytes([0x20, 0x10])
HANDSHAKE_RX_PREFIX = bytes([0x21, 0x10])

# Soft-reset command
RESET_CMD = bytes([0x20, 0x00])

# Boot-trigger command
BOOT_TRIGGER = bytes([0x02, 0x02])

# Boot enumeration: PLC sends 55× (00 [seq] 36 00), then done
BOOT_ENUM_PATTERN = bytes([0x36, 0x00])   # bytes 2-3 of each enum packet
BOOT_DONE_PACKET  = bytes([0x00, 0x00, 0x00, 0x01])

# Memory read command prefix: 20 03 00 [addr] [len]
MEM_READ_PREFIX = bytes([0x20, 0x03, 0x00])

# PLC memory map — addresses for reading device info
PLC_MEM = {
    'part_number':  (0x08, 8),    # + (0x10, 4) for continuation
    'article':      (0x14, 8),    # + (0x1C, 4) for "1.107.110.00"
    'description':  (0x20, 8),    # + (0x28, 8) + (0x30, 4) for "uSPS-CAN"
    'production_id': (0x34, 8),
    'production_date': (0x3C, 8),
    'revision':     (0x44, 2),
    'status':       (0x4E, 1),
    'serial_id':    (0x53, 2),
    'prog_status':  (0x55, 2),
    'flash_size':   (0x5B, 2),
    'app_version':  (0x6B, 8),    # + (0x73, 8) + (0x7B, 4)
    'app_name':     (0x7F, 8),    # + (0x87, 8) + (0x8F, 8) + (0x97, 6)
}

# Data-stream magic / ACK bytes
DATA_HEADER_MAGIC = bytes([0x53, 0x31, 0x23])   # 'S1#'
DATA_HEADER_ACK   = bytes([0x00, 0x01, 0x01, 0x01, 0x01])
DATA_CHUNK_ACK    = bytes([0x00, 0x01])
DATA_FINAL_ACK    = bytes([0x00, 0x00, 0x01])

# End-of-flash command and expected response
FLASH_END_CMD     = bytes([0x53, 0x39, 0x03])
FLASH_END_ACK     = bytes([0x00, 0x12, 0x34])

# Block layout:
#   Header: 3 magic + 2 offset + 3 data = 8 bytes
#   Chunks: 3 × 8 bytes = 24 bytes
#   Final:  5 data + 1 CRC = 6 bytes
#   Payload per block: 3 + 24 + 5 = 32 bytes (= 0x20)
BLOCK_PAYLOAD_SIZE    = 32
DATA_HEADER_DATA_LEN  = 3     # data bytes in header frame
DATA_CHUNKS_PER_BLOCK = 3
DATA_FINAL_DATA_LEN   = 5     # data bytes in final frame (before CRC)
DATA_OFFSET_INCREMENT = 0x20

# CRC-8 algorithm: polynomial 0x09, init = first byte of PLC identity,
# chained — each block's init = previous block's CRC result.
CRC_POLYNOMIAL = 0x09

# Timeouts in seconds
TIMEOUT_BOOT_ANNOUNCE = 10.0   # waiting for PLC to announce itself
TIMEOUT_HANDSHAKE     = 2.0
TIMEOUT_RESET_ACK     = 2.0
TIMEOUT_BOOT_ENUM     = 10.0
TIMEOUT_BLOCK_ACK     = 2.0
TIMEOUT_CHUNK_ACK     = 1.0
TIMEOUT_MEM_READ      = 2.0

# Supported module types → CAN settings
# In boot mode all PLCs communicate at 125 kbit/s
MODULE_TYPES = {
    'MRS 1.107.110.00 (500 kbit/s)':              {'can_fd': False, 'bitrate': 500000, 'data_bitrate': 0},
    'MRS 1.111.311.00 (500 kbit/s)':              {'can_fd': False, 'bitrate': 500000, 'data_bitrate': 0},
    'MRS 1.158.310.00 CAN FD (2000/500 kbit/s)':  {'can_fd': True,  'bitrate': 500000, 'data_bitrate': 2000000},
    'Boot mode (125 kbit/s)':                      {'can_fd': False, 'bitrate': 125000, 'data_bitrate': 0},
}

DEFAULT_CHANNEL = 'PCAN_USBBUS1'
DEFAULT_BITRATE = 125000
