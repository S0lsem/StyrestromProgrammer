"""
MRS PLC flash protocol engine.

Implements the 5-phase flash sequence reverse-engineered from a PCAN TRC
capture of Applix Studio programming a real MRS PLC.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .constants import (
    CAN_ID_PC_TO_PLC,
    CAN_ID_PLC_TO_PC,
    CAN_ID_PC_DATA,
    HANDSHAKE_TX,
    HANDSHAKE_RX,
    RESET_CMD,
    BOOT_TRIGGER,
    BOOT_DONE_PACKET,
    DATA_HEADER_MAGIC,
    DATA_HEADER_ACK,
    DATA_CHUNK_ACK,
    DATA_FINAL_ACK,
    DATA_BLOCK_SIZE,
    DATA_CHUNKS_PER_BLOCK,
    DATA_FINAL_SIZE,
    DATA_OFFSET_INCREMENT,
    TIMEOUT_HANDSHAKE,
    TIMEOUT_RESET_ACK,
    TIMEOUT_BOOT_ENUM,
    TIMEOUT_BLOCK_ACK,
    TIMEOUT_CHUNK_ACK,
    MODULE_TYPES,
    DEFAULT_CHANNEL,
    DEFAULT_BITRATE,
)
from .crc import block_checksum

log = logging.getLogger(__name__)


@dataclass
class FlashFile:
    """A named binary file to be sent to the PLC."""

    name: str
    data: bytes


class ProtocolError(Exception):
    """Raised when the PLC responds unexpectedly."""


class MRSFlashEngine:
    """
    Drives the MRS PLC flash protocol over a PCAN-USB CAN bus.

    Usage::

        engine = MRSFlashEngine(channel='PCAN_USBBUS1', bitrate=250000)
        engine.flash(files, progress=lambda pct, msg: print(f'{pct:.0%} {msg}'))
        engine.close()

    Or use as a context manager::

        with MRSFlashEngine() as engine:
            engine.flash(files)
    """

    # Payload bytes per block (5 header-data + 3×8 chunks + 5 final-data)
    _PAYLOAD_BYTES = 34
    _DATA_OFFSET_START = 0x2200

    def __init__(
        self,
        channel: str = DEFAULT_CHANNEL,
        bitrate: int = DEFAULT_BITRATE,
        is_can_fd: bool = False,
        data_bitrate: int = 0,
    ) -> None:
        self._channel      = channel
        self._bitrate      = bitrate
        self._is_can_fd    = is_can_fd
        self._data_bitrate = data_bitrate
        self._bus           = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> MRSFlashEngine:
        self._open_bus()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def detect_adapter(
        bitrate: int = DEFAULT_BITRATE,
        is_can_fd: bool = False,
        data_bitrate: int = 0,
    ) -> tuple[bool, str, str]:
        """
        Auto-detect which USB port the PCAN adapter is on.

        Tries PCAN_USBBUS1 through PCAN_USBBUS16.

        Returns:
            (True, channel, 'Connected on <channel>') on success.
            (False, '', '<error message>') if no adapter found.
        """
        import can
        for i in range(1, 17):
            channel = f'PCAN_USBBUS{i}'
            try:
                kwargs = {
                    'interface': 'pcan',
                    'channel': channel,
                    'bitrate': bitrate,
                    'fd': is_can_fd,
                }
                if is_can_fd and data_bitrate:
                    kwargs['data_bitrate'] = data_bitrate
                bus = can.Bus(**kwargs)
                bus.shutdown()
                return True, channel, f'Connected on {channel}'
            except Exception:
                continue
        return False, '', 'No PCAN-USB adapter found. Is it plugged in?'

    def open(self) -> None:
        self._open_bus()

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:
                pass
            self._bus = None

    def flash(
        self,
        files: list[FlashFile],
        progress: Optional[Callable[[float, str], None]] = None,
    ) -> None:
        """
        Run the complete 5-phase flash sequence.

        Args:
            files:    Ordered list of FlashFile objects (from MRSFileSet.to_flash_files()).
            progress: Optional callback(fraction, message) called at each major step.
        """
        if self._bus is None:
            self._open_bus()

        def _report(frac: float, msg: str) -> None:
            log.info('[%3.0f%%] %s', frac * 100, msg)
            if progress:
                progress(frac, msg)

        _report(0.00, 'Phase 1 — handshake')
        self._handshake()

        _report(0.05, 'Phase 2 — soft reset')
        self._soft_reset()

        _report(0.15, 'Phase 3 — boot trigger')
        self._boot_trigger()

        _report(0.25, 'Phase 4 — pre-data handshake')
        self._handshake()
        self._handshake()

        _report(0.30, 'Phase 5 — data stream')
        total_bytes = sum(len(f.data) for f in files)
        sent_bytes  = 0

        for flash_file in files:
            log.info('Sending file: %s (%d bytes)', flash_file.name, len(flash_file.data))
            offset = self._DATA_OFFSET_START
            data   = flash_file.data

            # Pad to a multiple of _PAYLOAD_BYTES
            remainder = len(data) % self._PAYLOAD_BYTES
            if remainder:
                data = data + b'\xff' * (self._PAYLOAD_BYTES - remainder)

            for block_start in range(0, len(data), self._PAYLOAD_BYTES):
                payload = data[block_start: block_start + self._PAYLOAD_BYTES]
                self._send_block(payload, offset)
                offset      += DATA_OFFSET_INCREMENT
                sent_bytes  += self._PAYLOAD_BYTES
                frac = 0.30 + 0.70 * (sent_bytes / max(total_bytes, 1))
                _report(frac, f'Flashing {flash_file.name} — block @0x{offset - DATA_OFFSET_INCREMENT:04X}')

        _report(1.00, 'Done')

    # ------------------------------------------------------------------
    # Protocol phases
    # ------------------------------------------------------------------

    def _handshake(self) -> None:
        self._send(CAN_ID_PC_TO_PLC, HANDSHAKE_TX)
        rx = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_HANDSHAKE)
        # First 6 bytes must match; trailing bytes (firmware/device ID) are logged but not fatal
        if rx[:len(HANDSHAKE_RX)] != HANDSHAKE_RX:
            log.warning(
                'Handshake mismatch: expected %s, got %s',
                HANDSHAKE_RX.hex(), rx.hex()
            )

    def _soft_reset(self) -> None:
        self._send(CAN_ID_PC_TO_PLC, RESET_CMD)
        self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_RESET_ACK)
        # Protocol spec: handshake repeats ×2 after reset
        self._handshake()
        self._handshake()

    def _boot_trigger(self) -> None:
        self._send(CAN_ID_PC_TO_PLC, BOOT_TRIGGER)
        deadline = time.monotonic() + TIMEOUT_BOOT_ENUM
        while time.monotonic() < deadline:
            rx = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_BOOT_ENUM)
            if rx[:len(BOOT_DONE_PACKET)] == BOOT_DONE_PACKET:
                return
        raise ProtocolError('Boot enumeration did not complete within timeout')

    def _send_block(self, payload: bytes, offset: int) -> None:
        """Send one 34-byte payload block with header, chunks, and final+CRC frame."""
        assert len(payload) == self._PAYLOAD_BYTES

        offset_hi = (offset >> 8) & 0xFF
        offset_lo =  offset       & 0xFF

        # Header frame: 3 magic + 2 offset + 5 data bytes = 8 bytes
        header_data = payload[:5]
        header_frame = bytes([*DATA_HEADER_MAGIC, offset_hi, offset_lo, *header_data])
        self._send(CAN_ID_PC_DATA, header_frame)
        ack = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_BLOCK_ACK)
        if ack[:len(DATA_HEADER_ACK)] != DATA_HEADER_ACK:
            raise ProtocolError(f'Bad header ACK: {ack.hex()}')

        # Chunk frames: 3 × 8 bytes
        pos = 5
        for _ in range(DATA_CHUNKS_PER_BLOCK):
            chunk = payload[pos: pos + 8]
            self._send(CAN_ID_PC_DATA, chunk)
            ack = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_CHUNK_ACK)
            if ack[:len(DATA_CHUNK_ACK)] != DATA_CHUNK_ACK:
                raise ProtocolError(f'Bad chunk ACK: {ack.hex()}')
            pos += 8

        # Final frame: 5 data bytes + 1 CRC byte = 6 bytes
        final_data = payload[pos: pos + 5]
        crc        = block_checksum(payload)
        final_frame = bytes([*final_data, crc])
        self._send(CAN_ID_PC_DATA, final_frame)
        ack = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_BLOCK_ACK)
        if ack[:len(DATA_FINAL_ACK)] != DATA_FINAL_ACK:
            log.warning('Unexpected final ACK: %s (expected %s)', ack.hex(), DATA_FINAL_ACK.hex())

    # ------------------------------------------------------------------
    # CAN bus helpers
    # ------------------------------------------------------------------

    def _open_bus(self) -> None:
        import can  # imported lazily so unit tests can run without hardware
        kwargs = {
            'interface': 'pcan',
            'channel': self._channel,
            'bitrate': self._bitrate,
            'fd': self._is_can_fd,
        }
        if self._is_can_fd and self._data_bitrate:
            kwargs['data_bitrate'] = self._data_bitrate
        self._bus = can.Bus(**kwargs)

    def _send(self, arb_id: int, data: bytes) -> None:
        import can
        msg = can.Message(
            arbitration_id=arb_id,
            data=data,
            is_extended_id=True,
        )
        self._bus.send(msg)
        log.debug('TX %08X  %s', arb_id, data.hex(' ').upper())

    def _recv(self, arb_id: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = self._bus.recv(timeout=max(remaining, 0))
            if msg is None:
                raise ProtocolError(
                    f'Timeout waiting for response on 0x{arb_id:08X}'
                )
            if msg.arbitration_id == arb_id:
                log.debug('RX %08X  %s', arb_id, bytes(msg.data).hex(' ').upper())
                return bytes(msg.data)
        raise ProtocolError(f'Timeout waiting for response on 0x{arb_id:08X}')
