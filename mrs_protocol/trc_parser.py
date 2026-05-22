from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class TrcMessage:
    seq: int
    time_ms: float
    direction: str
    arb_id: int
    data: bytes

    @property
    def is_tx(self) -> bool:
        return self.direction == 'Tx'

    @property
    def is_rx(self) -> bool:
        return self.direction == 'Rx'

    def __str__(self) -> str:
        data_hex = self.data.hex(' ').upper()
        return (
            f"{self.seq:5d}  "
            f"{self.time_ms:10.1f}ms  "
            f"{self.direction:2s}  "
            f"{self.arb_id:08X}  "
            f"[{len(self.data)}]  "
            f"{data_hex}"
        )


class TrcParser:
    """
    Parses PCAN .trc files (format version 1.1).

    Line format:
      <seq>  <time_ms>  <direction>  <arb_id>  <dlc>  <data bytes...>
    """

    LINE_RE = re.compile(
        r'^\s*(\d+)\s+([\d.]+)\s+(Tx|Rx)\s+([0-9A-Fa-f]+)\s+(\d+)\s+((?:[0-9A-Fa-f]{2}\s*)*)'
    )

    @classmethod
    def parse_file(cls, path: Path | str) -> list[TrcMessage]:
        return list(cls.iter_file(path))

    @classmethod
    def iter_file(cls, path: Path | str) -> Iterator[TrcMessage]:
        path = Path(path)
        with path.open('r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(';'):
                    continue
                m = cls.LINE_RE.match(line)
                if not m:
                    continue
                seq       = int(m.group(1))
                time_ms   = float(m.group(2))
                direction = m.group(3)
                arb_id    = int(m.group(4), 16)
                data_hex  = m.group(6).split()
                data      = bytes(int(b, 16) for b in data_hex)
                yield TrcMessage(
                    seq=seq,
                    time_ms=time_ms,
                    direction=direction,
                    arb_id=arb_id,
                    data=data,
                )

    @classmethod
    def summarize(cls, path: Path | str) -> str:
        """Print a human-readable summary of a TRC file."""
        msgs  = cls.parse_file(path)
        lines = [
            f'TRC file: {path}',
            f'Messages: {len(msgs)}',
        ]
        tx = [m for m in msgs if m.is_tx]
        rx = [m for m in msgs if m.is_rx]
        lines.append(f'  Tx: {len(tx)}  Rx: {len(rx)}')
        ids = set(m.arb_id for m in msgs)
        lines.append(
            '  CAN IDs seen: ' + ', '.join(f'0x{i:08X}' for i in sorted(ids))
        )
        if msgs:
            dur = msgs[-1].time_ms - msgs[0].time_ms
            lines.append(f'  Duration: {dur:.1f} ms')
        return '\n'.join(lines)
