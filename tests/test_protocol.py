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
from mrs_protocol.trc_parser import TrcParser, TrcMessage
from mrs_protocol import firmware_cache
from mrs_protocol.s19_parser import parse_s19, S19ParseError


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

class TestS19Parser:
    """Verifies the Motorola S-record parser produces the bytes the protocol
    engine expects. The values used here come from the original PCAN TRC
    capture (PLC_AtoZ.trc) — same source as TestCRC.test_known_block_1.
    """

    @staticmethod
    def _checksum(byte_count: int, body_no_checksum: bytes) -> int:
        return (~(byte_count + sum(body_no_checksum)) & 0xFF)

    @staticmethod
    def _s1(address: int, data: bytes) -> str:
        addr_bytes = address.to_bytes(2, 'big')
        body = addr_bytes + data
        byte_count = len(body) + 1
        cs = TestS19Parser._checksum(byte_count, body)
        return f'S1{byte_count:02X}{body.hex().upper()}{cs:02X}'

    def test_single_record_round_trip(self):
        data = bytes(range(16))
        s19 = self._s1(0x2200, data) + '\nS9030000FC\n'
        fw = parse_s19(s19)
        assert fw.start_address == 0x2200
        assert fw.data == data

    def test_block_1_from_trc(self):
        """The first 32 bytes parsed at offset 0x2200 must match the
        block 1 payload captured in PLC_AtoZ.trc (see TestCRC)."""
        block_1 = bytes([
            0xA6, 0xE0, 0xC7, 0x18, 0x02, 0x4F, 0xC7, 0x18,
            0x03, 0xA6, 0x1D, 0xC7, 0x18, 0x09, 0xA6, 0x30,
            0xC7, 0x18, 0x0A, 0xC6, 0xFF, 0xAF, 0xB7, 0x4A,
            0xC6, 0xFF, 0xAE, 0xB7, 0x4B, 0x6E, 0x26, 0x49,
        ])
        # Split into two 16-byte S1 records, both at expected addresses.
        s19  = self._s1(0x2200, block_1[:16]) + '\n'
        s19 += self._s1(0x2210, block_1[16:]) + '\n'
        fw = parse_s19(s19)
        assert fw.start_address == 0x2200
        assert fw.data == block_1

    def test_gap_filled_with_ff(self):
        # Two records with a 4-byte gap should be joined with 0xFF padding.
        s19  = self._s1(0x1000, b'\xAA\xBB') + '\n'
        s19 += self._s1(0x1006, b'\xCC\xDD') + '\n'
        fw = parse_s19(s19)
        assert fw.start_address == 0x1000
        assert fw.data == b'\xAA\xBB\xFF\xFF\xFF\xFF\xCC\xDD'

    def test_ignores_s0_s5_s9_records(self):
        s19 = (
            'S00F000068656C6C6F2020202000000038\n'   # S0 header
            + self._s1(0x2000, b'\x01\x02\x03') + '\n'
            + 'S5030001FB\n'                          # S5 count
            + 'S9030000FC\n'                          # S9 termination
        )
        fw = parse_s19(s19)
        assert fw.data == b'\x01\x02\x03'

    def test_bad_checksum_raises(self):
        # Build a valid record, then corrupt the checksum byte.
        good = self._s1(0x2000, b'\x42')
        bad  = good[:-2] + 'FF'
        with pytest.raises(S19ParseError, match='checksum'):
            parse_s19(bad + '\n')

    def test_empty_input_raises(self):
        with pytest.raises(S19ParseError, match='No data records'):
            parse_s19('S9030000FC\n')   # only a termination record

    def test_malformed_line_raises(self):
        with pytest.raises(S19ParseError):
            parse_s19('this is not an s-record\n')


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

    _SAMPLE_S19 = (
        'S00F000068656C6C6F2020202000000038\n'
        'S10A2200A6E0C7180204FC1F\n'
        'S9030000FC\n'
    )

    def test_round_trip(self, fake_home):
        firmware_cache.cache_part('PART_X', self._SAMPLE_S19)
        assert firmware_cache.is_cached('PART_X')
        assert firmware_cache.load_cached_part('PART_X') == self._SAMPLE_S19

    def test_manifest_is_not_plaintext(self, fake_home):
        secret = 'S10A22005345435245543F1E\n'   # contains 'SECRET' in hex
        firmware_cache.cache_part('PART_Y', secret)

        manifest = fake_home / '.mrs_programmer' / 'cache' / 'PART_Y' / '_manifest.bin'
        blob = manifest.read_bytes()
        # Neither the SREC framing nor the embedded payload should appear in clear.
        assert b'S1'      not in blob
        assert b'SECRET'  not in blob
        assert bytes.fromhex('5345435245') not in blob

    def test_corrupt_manifest_returns_none(self, fake_home):
        firmware_cache.cache_part('PART_Z', self._SAMPLE_S19)
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
