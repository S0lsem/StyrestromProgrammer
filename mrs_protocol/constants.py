# Supported module types — baudrates for the GUI's "Detect adapter" probe.
# Boot mode covers any MRS PLC during bootloader handshake (always 125 kbit/s).
MODULE_TYPES = {
    'MRS 1.107.110.00 (500 kbit/s)':              {'can_fd': False, 'bitrate': 500000, 'data_bitrate': 0},
    'MRS 1.111.311.00 (500 kbit/s)':              {'can_fd': False, 'bitrate': 500000, 'data_bitrate': 0},
    'MRS 1.158.310.00 CAN FD (2000/500 kbit/s)':  {'can_fd': True,  'bitrate': 500000, 'data_bitrate': 2000000},
    'Boot mode (125 kbit/s)':                      {'can_fd': False, 'bitrate': 125000, 'data_bitrate': 0},
}

DEFAULT_CHANNEL = 'PCAN_USBBUS1'
DEFAULT_BITRATE = 125000
