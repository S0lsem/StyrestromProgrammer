"""
Unit tests for the MRS PLC programmer protocol modules.

Run with:  python -m pytest tests/ -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mrs_protocol.crc import block_checksum, verify_block
from mrs_protocol import constants
from mrs_protocol.constants import (
    CAN_ID_PC_TO_PLC,
    CAN_ID_PLC_TO_PC,
    CAN_ID_PC_DATA,
    HANDSHAKE_TX,
    HANDSHAKE_RX,
)
from mrs_protocol.file_loader import MRSFileSet
from mrs_protocol.trc_parser import TrcParser, TrcMessage


# ---------------------------------------------------------------------------
# TestCRC
# ---------------------------------------------------------------------------

class TestCRC:
    def test_empty_payload(self):
        assert block_checksum(b'') == 0

    def test_single_byte(self):
        assert block_checksum(bytes([0xAB])) == 0xAB

    def test_two_same_bytes_xor_to_zero(self):
        assert block_checksum(bytes([0x5C, 0x5C])) == 0

    def test_known_block_from_trc(self):
        # 34-byte payload reconstructed from a known TRC capture block.
        # XOR of all bytes equals the CRC byte that follows in the TRC.
        payload = bytes([
            0x01, 0x02, 0x03, 0x04, 0x05,  # header data
            0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,  # chunk 1
            0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27,  # chunk 2
            0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,  # chunk 3
            0x40, 0x41, 0x42, 0x43, 0x44,                    # final data
        ])
        expected = 0
        for b in payload:
            expected ^= b
        assert block_checksum(payload) == expected & 0xFF

    def test_verify_correct(self):
        data = bytes(range(16))
        crc  = block_checksum(data)
        assert verify_block(data, crc) is True

    def test_verify_wrong(self):
        data = bytes(range(16))
        crc  = block_checksum(data)
        assert verify_block(data, crc ^ 0xFF) is False


# ---------------------------------------------------------------------------
# TestConstants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_can_ids_are_29_bit(self):
        max_29bit = (1 << 29) - 1
        assert CAN_ID_PC_TO_PLC <= max_29bit
        assert CAN_ID_PLC_TO_PC <= max_29bit
        assert CAN_ID_PC_DATA    <= max_29bit

    def test_handshake_first_byte_differs(self):
        assert HANDSHAKE_TX[0] != HANDSHAKE_RX[0]

    def test_handshake_payload_length(self):
        assert len(HANDSHAKE_TX) == 6
        assert len(HANDSHAKE_RX) == 6


# ---------------------------------------------------------------------------
# TestFileLoader
# ---------------------------------------------------------------------------

class TestFileLoader:
    def test_slots_defined(self):
        fs = MRSFileSet()
        assert len(fs.slots) == 5

    def test_required_slots(self):
        fs = MRSFileSet()
        required = [s for s in fs.slots if s.required]
        assert len(required) == 4

    def test_not_loaded_initially(self):
        fs = MRSFileSet()
        assert fs.loaded_count == 0
        assert not fs.all_required_loaded

    def test_validation_errors_when_empty(self):
        fs = MRSFileSet()
        errors = fs.validation_errors()
        # Four required slots → four errors
        assert len(errors) == 4
        assert all('Required file missing' in e for e in errors)

    def test_load_by_exact_name(self):
        fs = MRSFileSet()
        with tempfile.NamedTemporaryFile(suffix='user_code.c', delete=False) as f:
            f.write(b'// usercode')
            tmp = Path(f.name)
        # Rename to match exactly
        target = tmp.parent / 'user_code.c'
        tmp.rename(target)
        try:
            slot = fs.load_file(target)
            assert slot.tag == 'Usercode C'
            assert slot.loaded
            assert slot.data == b'// usercode'
        finally:
            target.unlink(missing_ok=True)

    def test_load_unknown_file_raises(self):
        fs = MRSFileSet()
        with tempfile.NamedTemporaryFile(suffix='.xyz', delete=False) as f:
            f.write(b'garbage')
            tmp = Path(f.name)
        target = tmp.parent / 'unknown_file.xyz'
        tmp.rename(target)
        try:
            with pytest.raises(ValueError, match="doesn't match any expected slot"):
                fs.load_file(target)
        finally:
            target.unlink(missing_ok=True)

    def test_to_flash_files_empty_when_no_files(self):
        fs = MRSFileSet()
        assert fs.to_flash_files() == []

    def test_to_flash_files_order(self):
        fs = MRSFileSet()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            # Create files for all required slots
            for name, content in [
                ('user_code.c',      b'uc'),
                ('user_code.h',      b'uh'),
                ('can_db_tables.c',  b'cc'),
                ('can_db_tables.h',  b'ch'),
            ]:
                (p / name).write_bytes(content)
            fs.load_directory(p)

        flash_files = fs.to_flash_files()
        names = [ff.name for ff in flash_files]
        assert names == ['user_code.c', 'user_code.h', 'can_db_tables.c', 'can_db_tables.h']

    def test_clear_all(self):
        fs = MRSFileSet()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'user_code.c').write_bytes(b'x')
            fs.load_file(p / 'user_code.c')

        assert fs.loaded_count >= 1
        fs.clear_all()
        assert fs.loaded_count == 0


# ---------------------------------------------------------------------------
# TestTrcParser
# ---------------------------------------------------------------------------

_SAMPLE_TRC = """\
;$FILEVERSION=1.1
;$STARTTIME=46234.584027778
;   Message   Time    Type  ID     Rx/Tx
;   Number    [ms]          [hex]
;------------------------------------------------------------------------------
       1       0.000 Tx 1FFFFFF1 6 20 10 17 01 18 D8
       2       1.500 Rx 1FFFFFF2 8 21 10 17 01 18 D8 00 49
       3      15.000 Tx 1FFFFFF1 2 20 00
       4      16.200 Rx 1FFFFFF2 4 21 00 00 00
"""


class TestTrcParser:
    def test_parse_trc_file(self):
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.trc', delete=False, encoding='utf-8'
        ) as f:
            f.write(_SAMPLE_TRC)
            tmp = Path(f.name)

        try:
            msgs = TrcParser.parse_file(tmp)
            assert len(msgs) == 4

            assert msgs[0].seq       == 1
            assert msgs[0].direction == 'Tx'
            assert msgs[0].arb_id   == 0x1FFFFFF1
            assert msgs[0].data     == bytes([0x20, 0x10, 0x17, 0x01, 0x18, 0xD8])

            assert msgs[1].seq       == 2
            assert msgs[1].direction == 'Rx'
            assert msgs[1].arb_id   == 0x1FFFFFF2
            assert len(msgs[1].data) == 8

            assert msgs[2].data == bytes([0x20, 0x00])
        finally:
            tmp.unlink(missing_ok=True)

    def test_timing(self):
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.trc', delete=False, encoding='utf-8'
        ) as f:
            f.write(_SAMPLE_TRC)
            tmp = Path(f.name)

        try:
            msgs = TrcParser.parse_file(tmp)
            assert msgs[0].time_ms == pytest.approx(0.0)
            assert msgs[1].time_ms == pytest.approx(1.5)
            assert msgs[2].time_ms == pytest.approx(15.0)
            assert msgs[3].time_ms == pytest.approx(16.2)
        finally:
            tmp.unlink(missing_ok=True)
