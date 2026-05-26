"""
MRS PLC flash protocol engine.

Implements the complete flash sequence reverse-engineered from PCAN TRC
captures of MRS Applics Flasher programming real MRS PLCs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .constants import (
    CAN_ID_PLC_BOOT,
    CAN_ID_PC_TO_PLC,
    CAN_ID_PLC_TO_PC,
    CAN_ID_PC_DATA,
    CAN_ID_PLC_DATA,
    BOOT_ACK,
    HANDSHAKE_TX_PREFIX,
    HANDSHAKE_RX_PREFIX,
    RESET_CMD,
    BOOT_TRIGGER,
    BOOT_DONE_PACKET,
    MEM_READ_PREFIX,
    PLC_MEM,
    DATA_HEADER_MAGIC,
    DATA_HEADER_ACK,
    DATA_CHUNK_ACK,
    DATA_FINAL_ACK,
    FLASH_END_CMD,
    FLASH_END_ACK,
    BLOCK_PAYLOAD_SIZE,
    DATA_HEADER_DATA_LEN,
    DATA_CHUNKS_PER_BLOCK,
    DATA_FINAL_DATA_LEN,
    DATA_OFFSET_START,
    DATA_OFFSET_INCREMENT,
    TIMEOUT_BOOT_ANNOUNCE,
    TIMEOUT_HANDSHAKE,
    TIMEOUT_RESET_ACK,
    TIMEOUT_BOOT_ENUM,
    TIMEOUT_BLOCK_ACK,
    TIMEOUT_CHUNK_ACK,
    TIMEOUT_MEM_READ,
    DEFAULT_CHANNEL,
    DEFAULT_BITRATE,
)
from .crc import block_crc

log = logging.getLogger(__name__)


@dataclass
class FlashFile:
    """A named binary file to be sent to the PLC."""
    name: str
    data: bytes


@dataclass
class PLCInfo:
    """Information read from PLC memory after boot."""
    serial: int = 0
    identity: bytes = field(default_factory=bytes)  # 4 bytes from boot announcement
    part_number: str = ''
    article: str = ''
    description: str = ''
    revision: str = ''
    app_name: str = ''
    app_version: str = ''
    production_id: str = ''
    production_date: str = ''


class ProtocolError(Exception):
    """Raised when the PLC responds unexpectedly."""


class MRSFlashEngine:
    """
    Drives the MRS PLC flash protocol over a PCAN-USB CAN bus.

    Usage::

        with MRSFlashEngine(channel='PCAN_USBBUS1', bitrate=500000) as engine:
            info = engine.wait_for_plc()
            engine.flash(files)
    """

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
        self._plc_info     = PLCInfo()

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
        """Auto-detect which USB port the PCAN adapter is on."""
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

    @property
    def plc_info(self) -> PLCInfo:
        return self._plc_info

    def wait_for_plc(
        self,
        timeout: float = TIMEOUT_BOOT_ANNOUNCE,
        progress: Optional[Callable[[float, str], None]] = None,
    ) -> PLCInfo:
        """
        Wait for a PLC to announce itself on the CAN bus.

        The PLC sends a boot announcement on CAN ID 1FFFFFF0 after power-on.
        We acknowledge it, then perform the handshake and read PLC info.

        Returns PLCInfo with serial, article number, app version, etc.
        """
        if self._bus is None:
            self._open_bus()

        if progress:
            progress(0.0, 'Waiting for PLC boot announcement…')

        # Phase 1: Wait for boot announcement on 1FFFFFF0
        identity = self._wait_boot_announcement(timeout)
        self._plc_info.identity = identity
        self._plc_info.serial = (identity[2] << 8) | identity[3]  # bytes 2-3 = serial
        log.info('PLC found — serial: %d, identity: %s', self._plc_info.serial, identity.hex())

        # Acknowledge the boot
        self._send(CAN_ID_PC_TO_PLC, BOOT_ACK)
        # Wait for repeated announcement and ack again
        try:
            self._recv(CAN_ID_PLC_BOOT, timeout=2.0)
            self._send(CAN_ID_PC_TO_PLC, BOOT_ACK)
            self._recv(CAN_ID_PLC_BOOT, timeout=2.0)
        except ProtocolError:
            pass  # not critical if we miss a repeat

        if progress:
            progress(0.3, 'Handshake…')

        # Phase 2: Handshake using PLC identity bytes
        self._handshake(identity)

        if progress:
            progress(0.5, 'Reading PLC info…')

        # Phase 3: Read PLC memory
        self._read_plc_info(identity)

        if progress:
            progress(1.0, f'PLC ready — SN:{self._plc_info.serial} {self._plc_info.article}')

        return self._plc_info

    def flash(
        self,
        files: list[FlashFile],
        progress: Optional[Callable[[float, str], None]] = None,
    ) -> None:
        """Run the complete flash sequence."""
        if self._bus is None:
            self._open_bus()

        identity = self._plc_info.identity
        if not identity:
            raise ProtocolError('PLC not detected. Call wait_for_plc() first.')

        def _report(frac: float, msg: str) -> None:
            log.info('[%3.0f%%] %s', frac * 100, msg)
            if progress:
                progress(frac, msg)

        # Pre-flash: read flash size
        _report(0.00, 'Pre-flash handshake…')
        self._handshake(identity)
        flash_size_bytes = self._read_mem(0x5B, 2)
        flash_size = (flash_size_bytes[0] << 8) | flash_size_bytes[1]
        log.info('Flash size parameter: 0x%04X', flash_size)

        # Pre-flash unlock command
        _report(0.02, 'Unlocking flash…')
        self._handshake(identity)
        self._read_mem(0x5B, 2)
        unlock_data = bytes([0x60, 0x22, 0x57, 0xF0, 0xCA, 0xEA, 0xF2, 0xF5])
        self._send(CAN_ID_PC_TO_PLC, unlock_data)
        self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_HANDSHAKE)

        # Soft reset
        _report(0.05, 'Soft reset…')
        self._handshake(identity)
        self._send(CAN_ID_PC_TO_PLC, RESET_CMD)
        self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_RESET_ACK)

        # Post-reset handshake
        self._handshake(identity)

        # Boot trigger
        _report(0.10, 'Boot trigger — waiting for enumeration…')
        self._send(CAN_ID_PC_TO_PLC, BOOT_TRIGGER)
        self._wait_boot_enum()

        # Post-boot handshake
        _report(0.20, 'Post-boot handshake…')
        self._handshake(identity)

        # Data stream
        _report(0.25, 'Sending firmware data…')
        total_bytes = sum(len(f.data) for f in files)
        sent_bytes = 0

        # CRC init = first byte of PLC identity (e.g. 0x17)
        crc_state = identity[0]

        for flash_file in files:
            log.info('Sending file: %s (%d bytes)', flash_file.name, len(flash_file.data))
            data = flash_file.data

            # Pad to multiple of BLOCK_PAYLOAD_SIZE
            remainder = len(data) % BLOCK_PAYLOAD_SIZE
            if remainder:
                data = data + b'\xFF' * (BLOCK_PAYLOAD_SIZE - remainder)

            offset = DATA_OFFSET_START

            for block_start in range(0, len(data), BLOCK_PAYLOAD_SIZE):
                payload = data[block_start: block_start + BLOCK_PAYLOAD_SIZE]
                crc_state = self._send_block(payload, offset, crc_state)
                offset += DATA_OFFSET_INCREMENT
                sent_bytes += BLOCK_PAYLOAD_SIZE
                frac = 0.25 + 0.70 * (sent_bytes / max(total_bytes, 1))
                _report(min(frac, 0.95), f'Flashing {flash_file.name} — 0x{offset - DATA_OFFSET_INCREMENT:04X}')

        # End-of-flash command
        _report(0.96, 'Finalizing flash…')
        end_cmd = bytes([*FLASH_END_CMD, 0x22, 0x00, 0xDA])
        self._send(CAN_ID_PC_DATA, end_cmd)
        ack = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_BLOCK_ACK)
        log.info('Flash end ACK: %s', ack.hex())

        # Post-flash verification handshake
        _report(0.98, 'Verifying…')
        try:
            self._handshake(identity)
        except ProtocolError:
            pass  # PLC may be rebooting

        _report(1.0, 'Flash complete')

    # ------------------------------------------------------------------
    # Protocol phases
    # ------------------------------------------------------------------

    def _wait_boot_announcement(self, timeout: float) -> bytes:
        """Wait for PLC boot announcement on CAN_ID_PLC_BOOT."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = self._bus.recv(timeout=max(remaining, 0))
            if msg is None:
                continue
            if msg.arbitration_id == CAN_ID_PLC_BOOT and len(msg.data) >= 8:
                data = bytes(msg.data)
                log.info('Boot announcement: %s', data.hex(' ').upper())
                # Identity bytes are positions 1-4: [type] [00] [serial_hi] [serial_lo]
                return data[1:5]
        raise ProtocolError('No PLC boot announcement received. Is the PLC powered on?')

    def _handshake(self, identity: bytes) -> None:
        """Perform handshake using the PLC's identity bytes."""
        tx = bytes([*HANDSHAKE_TX_PREFIX, *identity])
        self._send(CAN_ID_PC_TO_PLC, tx)
        rx = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_HANDSHAKE)
        expected_prefix = bytes([*HANDSHAKE_RX_PREFIX, *identity])
        if rx[:len(expected_prefix)] != expected_prefix:
            log.warning('Handshake mismatch: expected %s..., got %s',
                        expected_prefix.hex(), rx.hex())

    def _read_mem(self, addr: int, length: int) -> bytes:
        """Read memory from PLC: send 20 03 00 [addr] [len], receive on 1FFFFFF4."""
        cmd = bytes([*MEM_READ_PREFIX, addr, length])
        self._send(CAN_ID_PC_TO_PLC, cmd)
        rx = self._recv(CAN_ID_PLC_DATA, timeout=TIMEOUT_MEM_READ)
        return rx[:length]

    def _read_plc_info(self, identity: bytes) -> None:
        """Read all PLC info fields from memory."""
        info = self._plc_info

        # Part number (12 bytes across 2 reads)
        d1 = self._read_mem(0x08, 8)
        d2 = self._read_mem(0x10, 4)
        info.part_number = (d1 + d2).decode('ascii', errors='ignore').strip()

        # App name (30 bytes across 4 reads)
        parts = []
        for addr, length in [(0x7F, 8), (0x87, 8), (0x8F, 8), (0x97, 6)]:
            parts.append(self._read_mem(addr, length))
        info.app_name = b''.join(parts).decode('ascii', errors='ignore').strip()

        # Description (20 bytes across 3 reads)
        parts = []
        for addr, length in [(0x20, 8), (0x28, 8), (0x30, 4)]:
            parts.append(self._read_mem(addr, length))
        info.description = b''.join(parts).decode('ascii', errors='ignore').strip()

        # Article number (12 bytes across 2 reads)
        d1 = self._read_mem(0x14, 8)
        d2 = self._read_mem(0x1C, 4)
        info.article = (d1 + d2).decode('ascii', errors='ignore').strip()

        # Revision
        info.revision = self._read_mem(0x44, 2).decode('ascii', errors='ignore').strip()

        # App version (20 bytes across 3 reads)
        parts = []
        for addr, length in [(0x6B, 8), (0x73, 8), (0x7B, 4)]:
            parts.append(self._read_mem(addr, length))
        info.app_version = b''.join(parts).decode('ascii', errors='ignore').strip()

        # Production ID and date
        info.production_id = self._read_mem(0x34, 8).decode('ascii', errors='ignore').strip()
        info.production_date = self._read_mem(0x3C, 8).decode('ascii', errors='ignore').strip()

        log.info('PLC Info: SN=%d article=%s rev=%s app=%s ver=%s',
                 info.serial, info.article, info.revision, info.app_name, info.app_version)

    def _wait_boot_enum(self) -> None:
        """Wait for the 55 boot enumeration packets + boot done."""
        deadline = time.monotonic() + TIMEOUT_BOOT_ENUM
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = self._bus.recv(timeout=max(remaining, 0))
            if msg is None:
                continue
            if msg.arbitration_id == CAN_ID_PLC_TO_PC:
                data = bytes(msg.data)
                if data[:4] == BOOT_DONE_PACKET:
                    log.info('Boot enumeration complete')
                    return
        raise ProtocolError('Boot enumeration did not complete within timeout')

    def _send_block(self, payload: bytes, offset: int, crc_init: int) -> int:
        """
        Send one 32-byte payload block.

        Returns the CRC value (used as init for the next block).
        """
        assert len(payload) == BLOCK_PAYLOAD_SIZE

        crc_val = block_crc(payload, init=crc_init)

        offset_hi = (offset >> 8) & 0xFF
        offset_lo = offset & 0xFF

        # Header: 3 magic + 2 offset + 3 data bytes = 8 bytes
        header_data = payload[:DATA_HEADER_DATA_LEN]
        header = bytes([*DATA_HEADER_MAGIC, offset_hi, offset_lo, *header_data])
        self._send(CAN_ID_PC_DATA, header)
        ack = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_BLOCK_ACK)
        if ack[:len(DATA_HEADER_ACK)] != DATA_HEADER_ACK:
            raise ProtocolError(f'Bad header ACK: {ack.hex()}')

        # 3 chunks × 8 bytes
        pos = DATA_HEADER_DATA_LEN
        for _ in range(DATA_CHUNKS_PER_BLOCK):
            chunk = payload[pos: pos + 8]
            self._send(CAN_ID_PC_DATA, chunk)
            ack = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_CHUNK_ACK)
            if ack[:len(DATA_CHUNK_ACK)] != DATA_CHUNK_ACK:
                raise ProtocolError(f'Bad chunk ACK: {ack.hex()}')
            pos += 8

        # Final: 5 data bytes + 1 CRC = 6 bytes
        final_data = payload[pos: pos + DATA_FINAL_DATA_LEN]
        final = bytes([*final_data, crc_val])
        self._send(CAN_ID_PC_DATA, final)
        ack = self._recv(CAN_ID_PLC_TO_PC, timeout=TIMEOUT_BLOCK_ACK)
        if ack[:len(DATA_FINAL_ACK)] != DATA_FINAL_ACK:
            raise ProtocolError(f'Bad final ACK: {ack.hex()} (expected {DATA_FINAL_ACK.hex()})')

        return crc_val

    # ------------------------------------------------------------------
    # CAN bus helpers
    # ------------------------------------------------------------------

    def _open_bus(self) -> None:
        import can
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
                raise ProtocolError(f'Timeout waiting for 0x{arb_id:08X}')
            if msg.arbitration_id == arb_id:
                log.debug('RX %08X  %s', arb_id, bytes(msg.data).hex(' ').upper())
                return bytes(msg.data)
        raise ProtocolError(f'Timeout waiting for 0x{arb_id:08X}')
