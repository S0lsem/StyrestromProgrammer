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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

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


class _TracingBus:
    """Wraps a python-can Bus so every frame sent/received is reported to
    ``on_frame(direction, arb_id, data)`` — used to feed the GUI's CAN trace
    window. All other attributes proxy straight through to the real bus.
    """
    def __init__(self, bus, on_frame: Callable[[str, int, bytes], None]) -> None:
        self._bus = bus
        self._on_frame = on_frame

    def send(self, msg) -> None:
        self._bus.send(msg)
        try:
            self._on_frame('Tx', msg.arbitration_id, bytes(msg.data))
        except Exception:
            pass

    def recv(self, timeout=None):
        msg = self._bus.recv(timeout=timeout)
        if msg is not None:
            try:
                # Error frames are NOT bus traffic — PCAN emits one whenever it
                # sees edges it can't decode, which is what a bit-rate mismatch
                # looks like. Tracing them as 'Rx' makes a dead bus look busy
                # and sends you hunting for a protocol bug that isn't there.
                direction = 'Er' if getattr(msg, 'is_error_frame', False) else 'Rx'
                self._on_frame(direction, msg.arbitration_id, bytes(msg.data))
            except Exception:
                pass
        return msg

    def shutdown(self) -> None:
        self._bus.shutdown()


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
# Bus opening
# ---------------------------------------------------------------------------

# PCAN clocks python-can will accept for CAN FD, best first. 80 MHz divides
# cleanly for 500k:2000k; the rest are fallbacks for future baud combinations.
_PCAN_FD_CLOCKS = (80_000_000, 60_000_000, 40_000_000, 30_000_000,
                   24_000_000, 20_000_000)


def open_pcan(channel: str, bitrate: int, is_can_fd: bool = False,
              data_bitrate: int = 0):
    """Open a PCAN channel for classical CAN or CAN FD.

    Every bus in this app must go through here. CAN FD cannot be opened by
    passing ``fd=True`` alongside ``bitrate``/``data_bitrate`` — python-can's
    PCAN backend ignores both of those in FD mode and instead builds the
    InitializeFD string from ``f_clock`` plus the eight nom_*/data_* segment
    values. With none supplied it sends an empty string and PCANBasic answers
    "A parameter contains an invalid value". Passing a ``BitTimingFd`` fills
    those segments in, which is the only form that actually opens.
    """
    import can
    if not (is_can_fd and data_bitrate):
        with _busy_channel_hint():
            return can.Bus(interface='pcan', channel=channel,
                           bitrate=bitrate, fd=False)

    from can import BitTimingFd
    last_exc = None
    for f_clock in _PCAN_FD_CLOCKS:
        try:
            timing = BitTimingFd.from_sample_point(
                f_clock=f_clock,
                nom_bitrate=bitrate,    nom_sample_point=87.5,
                data_bitrate=data_bitrate, data_sample_point=80.0,
            )
        except Exception as exc:      # this baud pair doesn't divide this clock
            last_exc = exc
            continue
        with _busy_channel_hint():
            return can.Bus(interface='pcan', channel=channel, timing=timing)
    raise ValueError(
        f'No usable PCAN FD timing for {bitrate}:{data_bitrate} '
        f'(tried {len(_PCAN_FD_CLOCKS)} clocks): {last_exc}'
    )


class ChannelBusyError(ScanError):
    """The PCAN channel is held by another process (usually the flasher).

    Subclasses ScanError so the GUI's ``except ScanError`` path shows the
    message in the normal "Scan failed" dialog. Left as a bare Exception it
    fell through to the catch-all and the operator got a stack trace.
    """


@contextmanager
def _busy_channel_hint():
    """Translate PCAN's cryptic in-use error into something actionable.

    Only ONE process may hold a PCAN channel. While the .NET flasher runs it
    owns the adapter, so any bus we try to open fails with "A PCAN Channel has
    not been initialized yet or the initialization process has failed" — which
    reads like a driver fault rather than "something else is using it".
    """
    try:
        yield
    except Exception as exc:
        if 'has not been initialized' in str(exc):
            raise ChannelBusyError(
                'The CAN adapter is already in use by another program.\n\n'
                'This is almost always a flash still running — wait for it to '
                'finish and try again. Otherwise close any other CAN tool '
                '(PCAN-View, MRS Applics Studio) and retry.'
            ) from exc
        raise


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
    busy = False
    for i in range(1, 17):
        channel = f'PCAN_USBBUS{i}'
        try:
            bus = open_pcan(channel, bitrate, is_can_fd, data_bitrate)
            bus.shutdown()
            return True, channel, f'Connected on {channel}'
        except ChannelBusyError:
            # The adapter IS there — something else is holding it. Don't let
            # this fall through to "not plugged in", which sends the operator
            # hunting for a cable fault that doesn't exist.
            busy = True
            continue
        except Exception:
            continue
    if busy:
        return False, '', (
            'A CAN adapter is present but in use by another program '
            '(a flash still running, PCAN-View, or MRS Applics Studio). '
            'Close it and try again.'
        )
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
    on_frame:     Callable[[str, int, bytes], None] = lambda d, i, data: None,
) -> PLCInfo:
    """Wait for a PLC boot announcement, handshake, read identity info.

    The caller must power-cycle the PLC after invoking this — the
    bootloader announces itself on ``CAN_ID_PLC_BOOT`` during the first
    few hundred milliseconds of each boot. After SCAN succeeds the PLC
    is in a post-handshake state; a fresh power-cycle is usually needed
    before the console flasher can take over.

    Raises ScanError on timeout or malformed response.

    ``bitrate`` / ``is_can_fd`` / ``data_bitrate`` come from the app's Module
    dropdown and select the bus to listen on.

    SCOPE — Scan only works on a module sitting in its BOOTLOADER (a blank
    unit, dropdown "Boot mode (125 kbit/s)"). Those announce themselves
    repeatedly, so a passive listener catches them.

    A *programmed* module cannot be scanned at any bit rate. It runs its
    firmware and has no reason to announce anything; the only way to reach it
    is to actively command it into the bootloader, which is what the .NET
    flasher's ``--restart-module`` does and this passive scan does not
    implement. Measured 2026-08-11: with a programmed CAN FD module
    power-cycling on the bench, every candidate rate (125k/250k/500k/1M
    classical, 500k:2000k and 250k:1000k FD) produced CAN error frames and
    zero decodable frames, and the flasher's own ``device list`` found nothing
    at 500k:2000k — the same rate at which flashing that module succeeds.
    Flashing needs no Scan, so this is a documented limitation, not a bug.
    """
    bus = _TracingBus(open_pcan(channel, bitrate, is_can_fd, data_bitrate),
                      on_frame)
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
                f'not be read — the module answered its boot announcement and '
                f'then stopped responding. The unit is present and flashable: '
                f'just press Flash, no Scan is needed.',
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
    errors = 0          # PCAN error frames: signal present, but undecodable
    valid  = 0          # frames that decoded cleanly, whatever their ID
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        msg = bus.recv(timeout=max(remaining, 0))
        if msg is None:
            continue
        if getattr(msg, 'is_error_frame', False):
            errors += 1
            continue
        valid += 1
        if msg.arbitration_id == CAN_ID_PLC_BOOT and len(msg.data) >= 8:
            data = bytes(msg.data)
            log.info('Boot announcement: %s', data.hex(' ').upper())
            return data[1:5]

    # A burst of error frames with nothing decodable means the module IS
    # transmitting and we are listening at the wrong bit rate — a completely
    # different problem from a silent bus, and worth saying so plainly.
    if errors and not valid:
        raise ScanError(
            f'A module is transmitting, but nothing could be decoded at the '
            f'selected speed ({errors} CAN error frames, 0 valid frames).\n\n'
            f'This is what an ALREADY-PROGRAMMED module looks like: it is '
            f'running its firmware, not sitting in the bootloader, so Scan '
            f'cannot read it. That is expected — just press Flash, which '
            f'commands the module into the bootloader itself.\n\n'
            f'Scan only works on blank modules, with the dropdown on '
            f'"Boot mode (125 kbit/s)".'
        )
    if valid:
        raise ScanError(
            f'Traffic was seen on the bus ({valid} frames) but no PLC boot '
            f'announcement among it. Power-cycle the PLC while Scan is running.'
        )
    raise ScanError(
        'No PLC boot announcement received — the bus was completely silent.\n\n'
        'Power-cycle the PLC after clicking Scan, and check power, CAN-H/CAN-L '
        'and termination.'
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
