"""
PCAN-USB adapter detection.

The flash protocol itself is no longer implemented here — we wrap
``MRS_Developers_Studio_Console.exe`` via :mod:`mrs_protocol.console_flasher`
to avoid reimplementing the proprietary per-block CRC of the MRS
bootloader. This module only provides the pre-flight adapter probe
used by the GUI's "Detect adapter" button.
"""
from __future__ import annotations


def detect_adapter(
    bitrate: int,
    is_can_fd: bool = False,
    data_bitrate: int = 0,
) -> tuple[bool, str, str]:
    """Probe PCAN-USB channels 1–16 for a usable adapter.

    Returns ``(ok, channel, message)``. ``channel`` is the first
    PCAN_USBBUSn that opens successfully (e.g. ``'PCAN_USBBUS1'``);
    on failure it is empty and ``message`` explains why.
    """
    import can
    for i in range(1, 17):
        channel = f'PCAN_USBBUS{i}'
        try:
            kwargs = {
                'interface':   'pcan',
                'channel':     channel,
                'bitrate':     bitrate,
                'fd':          is_can_fd,
            }
            if is_can_fd and data_bitrate:
                kwargs['data_bitrate'] = data_bitrate
            bus = can.Bus(**kwargs)
            bus.shutdown()
            return True, channel, f'Connected on {channel}'
        except Exception:
            continue
    return False, '', 'No PCAN-USB adapter found. Is it plugged in?'
