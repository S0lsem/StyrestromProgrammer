"""
Unit tests for the MRS PLC programmer protocol modules.

Run with:  python -m pytest tests/ -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mrs_protocol.crc import block_crc, verify_block
from mrs_protocol import constants
from mrs_protocol.constants import (
    CAN_ID_PLC_BOOT,
    CAN_ID_PC_TO_PLC,
    CAN_ID_PLC_TO_PC,
    CAN_ID_PC_DATA,
    CAN_ID_PLC_DATA,
    HANDSHAKE_TX_PREFIX,
    HANDSHAKE_RX_PREFIX,
    BLOCK_PAYLOAD_SIZE,
    CRC_POLYNOMIAL,
)
from mrs_protocol.file_loader import MRSFileSet
from mrs_protocol.trc_parser import TrcParser, TrcMessage
from mrs_protocol import firmware_cache


# ---------------------------------------------------------------------------
# TestCRC — verified against real TRC capture data
# ---------------------------------------------------------------------------

class TestCRC:
    def test_empty_payload(self):
        assert block_crc(b'', init=0) == 0

    def test_known_block_1_from_trc(self):
        """Block 1 from PLC_AtoZ.trc — offset 0x2200, init=0x17."""
        payload = bytes([
            0xA6, 0xE0, 0xC7,                                      # header data (3)
            0x18, 0x02, 0x4F, 0xC7, 0x18, 0x03, 0xA6, 0x1D,      # chunk 1 (8)
            0xC7, 0x18, 0x09, 0xA6, 0x30, 0xC7, 0x18, 0x0A,      # chunk 2 (8)
            0xC6, 0xFF, 0xAF, 0xB7, 0x4A, 0xC6, 0xFF, 0xAE,      # chunk 3 (8)
            0xB7, 0x4B, 0x6E, 0x26, 0x49,                          # final data (5)
        ])
        assert len(payload) == BLOCK_PAYLOAD_SIZE
        assert block_crc(payload, init=0x17) == 0xF1

    def test_known_block_2_chained(self):
        """Block 2 — init = block 1's CRC (0xF1)."""
        payload = bytes([
            0x6E, 0xBA, 0x48,
            0x03, 0x4B, 0xFD, 0x08, 0x4B, 0xFD, 0xB6, 0x4B,
            0xA4, 0x0C, 0xA1, 0x08, 0x26, 0xF8, 0x6E, 0x92,
            0x48, 0x6E, 0x45, 0x4C, 0x0B, 0x4B, 0xFD, 0x0D,
            0x4B, 0xFD, 0xC6, 0x24, 0x40,
        ])
        assert block_crc(payload, init=0xF1) == 0xFE

    def test_verify_correct(self):
        data = bytes(range(32))
        crc = block_crc(data, init=0x17)
        assert verify_block(data, crc, init=0x17) is True

    def test_verify_wrong(self):
        data = bytes(range(32))
        crc = block_crc(data, init=0x17)
        assert verify_block(data, crc ^ 0xFF, init=0x17) is False

    def test_payload_size_is_32(self):
        assert BLOCK_PAYLOAD_SIZE == 32


# ---------------------------------------------------------------------------
# TestConstants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_can_ids_are_29_bit(self):
        max_29bit = (1 << 29) - 1
        assert CAN_ID_PLC_BOOT   <= max_29bit
        assert CAN_ID_PC_TO_PLC  <= max_29bit
        assert CAN_ID_PLC_TO_PC  <= max_29bit
        assert CAN_ID_PC_DATA    <= max_29bit
        assert CAN_ID_PLC_DATA   <= max_29bit

    def test_handshake_first_byte_differs(self):
        assert HANDSHAKE_TX_PREFIX[0] != HANDSHAKE_RX_PREFIX[0]

    def test_handshake_payload_length(self):
        assert len(HANDSHAKE_TX_PREFIX) == 2
        assert len(HANDSHAKE_RX_PREFIX) == 2


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
        assert len(errors) == 4
        assert all('Required file missing' in e for e in errors)

    def test_load_by_exact_name(self):
        fs = MRSFileSet()
        with tempfile.NamedTemporaryFile(suffix='user_code.c', delete=False) as f:
            f.write(b'// usercode')
            tmp = Path(f.name)
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


# ---------------------------------------------------------------------------
# TestFirmwareCache — encrypted-at-rest round trip + legacy wipe
# ---------------------------------------------------------------------------

class TestFirmwareCache:
    @pytest.fixture
    def fake_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(firmware_cache.Path, 'home', lambda: tmp_path)
        return tmp_path

    def test_round_trip(self, fake_home):
        files = [
            {'name': 'app.hex',  'content': 'AAECAwQF'},
            {'name': 'data.eds', 'content': 'BgcICQoL'},
        ]
        firmware_cache.cache_part('PART_X', files)
        assert firmware_cache.is_cached('PART_X')
        assert firmware_cache.load_cached_part('PART_X') == files

    def test_manifest_is_not_plaintext(self, fake_home):
        files = [{'name': 'secret.hex', 'content': 'U0VDUkVU'}]   # 'SECRET' in base64
        firmware_cache.cache_part('PART_Y', files)

        manifest = fake_home / '.mrs_programmer' / 'cache' / 'PART_Y' / '_manifest.bin'
        blob = manifest.read_bytes()
        assert b'secret.hex' not in blob
        assert b'SECRET'     not in blob
        assert b'U0VDUkVU'   not in blob

    def test_corrupt_manifest_returns_none(self, fake_home):
        firmware_cache.cache_part('PART_Z', [{'name': 'a', 'content': 'AA=='}])
        manifest = fake_home / '.mrs_programmer' / 'cache' / 'PART_Z' / '_manifest.bin'
        manifest.write_bytes(b'not a valid fernet token')
        assert firmware_cache.load_cached_part('PART_Z') is None

    def test_legacy_plaintext_wiped(self, fake_home):
        cache = fake_home / '.mrs_programmer' / 'cache' / 'OLD_PART'
        cache.mkdir(parents=True)
        legacy = cache / '_manifest.json'
        legacy.write_text('[{"name":"leak.hex","content":"AAA="}]')

        # Any cache touch should remove the legacy file.
        firmware_cache.list_cached_parts()
        assert not legacy.exists()
