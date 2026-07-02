"""
PCAN-USB adapter detection and PLC boot-mode SCAN.

The flash protocol itself is delegated to
:mod:`mrs_protocol.console_flasher`, which wraps the vendor's
``MRS_Developers_Studio_Console.exe``. This module provides the two
read-only pre-flight operations the GUI needs:

* :func:`detect_adapter` — open each PCAN_USBBUSn until one accepts the
  requested bitrate; used by the "Detect adapter" button.
* :func:`scan_plc` — listen for a PLC boot announcement, handshake, and
  read identity strings (article, revision, app name, app version) from
  PLC memory; used by the "Scan" button.

SCAN is read-only: it never writes to flash. The boot window the
handshake consumes means the operator typically needs to power-cycle
the PLC once more before clicking Flash.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Boot-mode CAN protocol constants (29-bit extended IDs)
# ---------------------------------------------------------------------------
CAN_ID_PLC_BOOT  = 0x1FFFFFF0   # PLC → PC  (boot announcement)
CAN_ID_PC_TO_PLC = 0x1FFFFFF1   # PC → PLC  (handshake, memory read)
CAN_ID_PLC_TO_PC = 0x1FFFFFF2   # PLC → PC  (handshake ACK)
CAN_ID_PLC_DATA  = 0x1FFFFFF4   # PLC → PC  (memory read responses)

BOOT_ACK            = bytes([0x00, 0x00])
HANDSHAKE_TX_PREFIX = bytes([0x20, 0x10])
HANDSHAKE_RX_PREFIX = bytes([0x21, 0x10])
MEM_READ_PREFIX     = bytes([0x20, 0x03, 0x00])

TIMEOUT_BOOT_ANNOUNCE = 30.0
TIMEOUT_HANDSHAKE     = 2.0
TIMEOUT_MEM_READ      = 2.0


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class PLCInfo:
    """Identity info read from a PLC during SCAN."""
    serial:      int   = 0
    identity:    bytes = b''   # 4 bytes from boot announcement
    article:     str   = ''
    description: str   = ''
    revision:    str   = ''
    app_name:    str   = ''
    app_version: str   = ''


class ScanError(Exception):
    """Raised when SCAN can't reach the PLC or the PLC responds unexpectedly."""


class PartialScanError(ScanError):
    """The PLC *was* detected — its boot announcement arrived and we read its
    serial — but the identity read did not complete.

    This is the expected outcome for CAN FD modules: their bootloader answers
    the handshake in a CAN FD dialect our classical-CAN scan can't read, so
    the handshake/memory read times out even though the module is present and
    healthy. Such modules flash normally via the console flasher — Scan is only
    an optional pre-flight. Carries the serial recovered from the boot
    announcement so the UI can still show which unit was seen.
    """
    def __init__(self, serial: int, message: str) -> None:
        super().__init__(message)
        self.serial = serial


# ---------------------------------------------------------------------------
# Adapter detection
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# PLC SCAN
# ---------------------------------------------------------------------------

def scan_plc(
    channel: str,
    bitrate: int,
    is_can_fd:    bool  = False,
    data_bitrate: int   = 0,
    timeout:      float = TIMEOUT_BOOT_ANNOUNCE,
) -> PLCInfo:
    """Wait for a PLC boot announcement, handshake, read identity info.

    The caller must power-cycle the PLC after invoking this — the
    bootloader announces itself on ``CAN_ID_PLC_BOOT`` during the first
    few hundred milliseconds of each boot. After SCAN succeeds the PLC
    is in a post-handshake state; a fresh power-cycle is usually needed
    before the console flasher can take over.

    Raises ScanError on timeout or malformed response.
    """
    import can
    kwargs = {
        'interface': 'pcan',
        'channel':   channel,
        'bitrate':   bitrate,
        'fd':        is_can_fd,
    }
    if is_can_fd and data_bitrate:
        kwargs['data_bitrate'] = data_bitrate

    bus = can.Bus(**kwargs)
    try:
        info = PLCInfo()

        identity = _wait_boot_announcement(bus, timeout)
        info.identity = identity
        # Serial is a 24-bit value in identity bytes 1..3
        # (e.g. 01 18 D7 → 0x118D7 → 71895).
        info.serial = (identity[1] << 16) | (identity[2] << 8) | identity[3]
        log.info('PLC boot — serial: %d, identity: %s',
                 info.serial, identity.hex())

        # ACK the boot, then absorb the repeated announcement so the
        # PLC's bootloader settles into a steady state before handshake.
        _send(bus, CAN_ID_PC_TO_PLC, BOOT_ACK)
        try:
            _recv(bus, CAN_ID_PLC_BOOT, timeout=2.0)
            _send(bus, CAN_ID_PC_TO_PLC, BOOT_ACK)
            _recv(bus, CAN_ID_PLC_BOOT, timeout=2.0)
        except ScanError:
            pass  # repeated announcement is best-effort

        # From here on the PLC is confirmed present (we have its serial). A
        # timeout now means the identity read didn't complete — expected for
        # CAN FD modules — so surface it as a PartialScanError, not a hard
        # failure, so the UI can say "detected, just flash it".
        try:
            _handshake(bus, identity)

            info.article     = _read_string(bus, [(0x14, 8), (0x1C, 4)])
            info.revision    = _read_string(bus, [(0x44, 2)])
            info.description = _read_string(bus, [(0x20, 8), (0x28, 8), (0x30, 4)])
            info.app_name    = _read_string(bus, [(0x7F, 8), (0x87, 8), (0x8F, 8), (0x97, 6)])
            info.app_version = _read_string(bus, [(0x6B, 8), (0x73, 8), (0x7B, 4)])
        except ScanError as exc:
            raise PartialScanError(
                info.serial,
                f'PLC detected (SN {info.serial}), but its full identity could '
                f'not be read. This is normal for CAN FD modules — they cannot '
                f'be scanned, but they flash correctly. Just press Flash — no '
                f'Scan is needed.',
            ) from exc

        log.info(
            'PLC Info: SN=%d article=%s rev=%s app=%s ver=%s',
            info.serial, info.article, info.revision, info.app_name, info.app_version,
        )
        return info
    finally:
        try:
            bus.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _wait_boot_announcement(bus, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        msg = bus.recv(timeout=max(remaining, 0))
        if msg is None:
            continue
        if msg.arbitration_id == CAN_ID_PLC_BOOT and len(msg.data) >= 8:
            data = bytes(msg.data)
            log.info('Boot announcement: %s', data.hex(' ').upper())
            return data[1:5]
    raise ScanError(
        'No PLC boot announcement received. Power-cycle the PLC after clicking Scan.'
    )


def _send(bus, arb_id: int, data: bytes) -> None:
    import can
    msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=True)
    bus.send(msg)
    log.debug('TX %08X  %s', arb_id, data.hex(' ').upper())


def _recv(bus, arb_id: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        msg = bus.recv(timeout=max(remaining, 0))
        if msg is None:
            raise ScanError(f'Timeout waiting for 0x{arb_id:08X}')
        if msg.arbitration_id == arb_id:
            data = bytes(msg.data)
            log.debug('RX %08X  %s', arb_id, data.hex(' ').upper())
            return data
    raise ScanError(f'Timeout waiting for 0x{arb_id:08X}')


def _handshake(bus, identity: bytes) -> None:
    tx = bytes([*HANDSHAKE_TX_PREFIX, *identity])
    _send(bus, CAN_ID_PC_TO_PLC, tx)
    rx = _recv(bus, CAN_ID_PLC_TO_PC, timeout=TIMEOUT_HANDSHAKE)
    expected = bytes([*HANDSHAKE_RX_PREFIX, *identity])
    if rx[:len(expected)] != expected:
        log.warning('Handshake mismatch: expected %s..., got %s',
                    expected.hex(), rx.hex())


def _read_mem(bus, addr: int, length: int) -> bytes:
    cmd = bytes([*MEM_READ_PREFIX, addr, length])
    _send(bus, CAN_ID_PC_TO_PLC, cmd)
    rx = _recv(bus, CAN_ID_PLC_DATA, timeout=TIMEOUT_MEM_READ)
    return rx[:length]


def _read_string(bus, ranges: list) -> str:
    parts = [_read_mem(bus, addr, length) for addr, length in ranges]
    text = b''.join(parts).decode('ascii', errors='ignore')
    return text.replace('\x00', '').strip()
