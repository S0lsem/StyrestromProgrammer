# Supported module types — baudrates for the GUI's "Detect adapter" probe
# and the standalone Scan button.
#
# Boot mode (125 kbit/s) is first so the combo box defaults to it: the
# bootloader handshake — used by both Scan and the start of every Flash —
# always runs at 125 kbit/s, and the console flasher auto-switches up to
# the app baudrate (e.g. 500k) during the data stream itself. Picking a
# different entry here is only useful for testing the PCAN adapter at a
# specific app-side bitrate.
MODULE_TYPES = {
    'Boot mode (125 kbit/s)':                      {'can_fd': False, 'bitrate': 125000, 'data_bitrate': 0},
    'MRS 1.107.110.00 (500 kbit/s)':              {'can_fd': False, 'bitrate': 500000, 'data_bitrate': 0},
    'MRS 1.111.311.00 (500 kbit/s)':              {'can_fd': False, 'bitrate': 500000, 'data_bitrate': 0},
    'MRS 1.158.310.00 CAN FD (2000/500 kbit/s)':  {'can_fd': True,  'bitrate': 500000, 'data_bitrate': 2000000},
}

DEFAULT_CHANNEL = 'PCAN_USBBUS1'
DEFAULT_BITRATE = 125000
