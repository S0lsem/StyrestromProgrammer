"""
Unit tests for the MRS PLC programmer protocol modules.

Run with:  python -m pytest tests/ -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mrs_protocol.trc_parser import TrcParser, TrcMessage
from mrs_protocol import firmware_cache
from mrs_protocol.s19_parser import parse_s19, S19ParseError
from mrs_protocol import protocol
from mrs_protocol.protocol import scan_plc, ScanError, PartialScanError, CAN_ID_PLC_BOOT


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
# TestScan — boot announcement handling and the CAN FD partial-scan path
# ---------------------------------------------------------------------------


class _FakeMsg:
    def __init__(self, arb_id: int, data: bytes) -> None:
        self.arbitration_id = arb_id
        self.data = data


class _FakeBus:
    """A minimal stand-in for ``can.Bus``.

    Hands out ``boot_count`` copies of the boot announcement, then returns
    ``None`` forever — i.e. the module announces itself but never answers the
    handshake on 0x1FFFFFF2. That is exactly the CAN FD signature seen in the
    field: classical boot frame gets through, FD handshake reply does not.
    """
    def __init__(self, announcement: bytes, boot_count: int) -> None:
        self._ann = announcement
        self._boot_left = boot_count
        self.shutdown_called = False

    def recv(self, timeout: float = 0):
        if self._boot_left > 0:
            self._boot_left -= 1
            return _FakeMsg(CAN_ID_PLC_BOOT, self._ann)
        return None

    def send(self, msg) -> None:
        pass

    def shutdown(self) -> None:
        self.shutdown_called = True


class TestScan:
    # The real boot announcement captured from a 1494X_32BIT_CANFD_RELAY:
    # serial = (0x51 << 16) | (0xE6 << 8) | 0x19 = 5_367_321.
    _CANFD_ANNOUNCE = bytes([0x11, 0xCD, 0x51, 0xE6, 0x19, 0x00, 0x00, 0x25])

    def _patch_bus(self, monkeypatch, bus):
        import can
        monkeypatch.setattr(can, 'Bus', lambda **kwargs: bus)

    def test_boot_seen_but_no_handshake_is_partial(self, monkeypatch):
        """Boot announcement arrives, handshake never answered → PartialScanError
        carrying the recovered serial (not a hard ScanError)."""
        bus = _FakeBus(self._CANFD_ANNOUNCE, boot_count=3)
        self._patch_bus(monkeypatch, bus)

        with pytest.raises(PartialScanError) as excinfo:
            scan_plc('PCAN_USBBUS1', 125000)

        assert excinfo.value.serial == 5_367_321
        assert bus.shutdown_called          # bus is always cleaned up

    def test_no_announcement_is_hard_error(self, monkeypatch):
        """A silent bus (no PLC) still raises a plain ScanError, never the
        partial variant — we must not tell the operator to 'just flash'."""
        bus = _FakeBus(self._CANFD_ANNOUNCE, boot_count=0)
        self._patch_bus(monkeypatch, bus)

        with pytest.raises(ScanError) as excinfo:
            scan_plc('PCAN_USBBUS1', 125000, timeout=0.05)
        assert not isinstance(excinfo.value, PartialScanError)


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
