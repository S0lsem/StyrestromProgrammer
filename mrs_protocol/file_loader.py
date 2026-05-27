from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .protocol import FlashFile


@dataclass
class FileSlot:
    """Represents one expected file in the programming set."""

    tag: str
    filename: str
    required: bool = True
    data: Optional[bytes] = None
    path: Optional[Path] = None

    @property
    def loaded(self) -> bool:
        return self.data is not None

    @property
    def size(self) -> int:
        return len(self.data) if self.data else 0


class MRSFileSet:
    """
    Manages the set of files needed to program an MRS PLC.

    Slot order matters — files are sent to the PLC in the order
    they appear here (matching Applix Studio behavior).
    """

    SLOTS: list[dict] = [
        {'tag': 'Usercode C', 'filename': 'user_code.c',      'required': True},
        {'tag': 'Usercode H', 'filename': 'user_code.h',      'required': True},
        {'tag': 'CAN DB C',   'filename': 'can_db_tables.c',  'required': True},
        {'tag': 'CAN DB H',   'filename': 'can_db_tables.h',  'required': True},
        {'tag': 'DSL Config', 'filename': 'dsl_cfg.h',        'required': False},
    ]

    def __init__(self) -> None:
        self._slots = [FileSlot(**s) for s in self.SLOTS]

    @property
    def slots(self) -> list[FileSlot]:
        return self._slots

    @property
    def all_required_loaded(self) -> bool:
        return all(s.loaded for s in self._slots if s.required)

    @property
    def loaded_count(self) -> int:
        return sum(1 for s in self._slots if s.loaded)

    @property
    def total_count(self) -> int:
        return len(self._slots)

    def load_file(self, path: Path | str) -> FileSlot:
        """
        Load a file from disk and assign it to the best matching slot.
        Matching is case-insensitive on filename stem.

        Returns the slot it was assigned to.
        Raises ValueError if no matching slot found.
        """
        path = Path(path)
        name_lower = path.name.lower()

        # Exact filename match (case-insensitive)
        for slot in self._slots:
            if slot.filename.lower() == name_lower:
                slot.data = path.read_bytes()
                slot.path = path
                return slot

        # Fuzzy stem match (strip .c/.h extension from slot filename)
        for slot in self._slots:
            stem = slot.filename.lower().rstrip('.ch')
            if stem in name_lower:
                slot.data = path.read_bytes()
                slot.path = path
                return slot

        raise ValueError(
            f"File '{path.name}' doesn't match any expected slot: "
            + ', '.join(s.filename for s in self._slots)
        )

    def load_bytes(self, filename: str, data: bytes) -> FileSlot:
        """
        Load file content from an in-memory bytes object into the matching slot.
        Uses the same matching logic as load_file (exact name first, then stem).

        Raises ValueError if no matching slot found.
        """
        name_lower = filename.lower()

        for slot in self._slots:
            if slot.filename.lower() == name_lower:
                slot.data = data
                slot.path = None
                return slot

        for slot in self._slots:
            stem = slot.filename.lower().rstrip('.ch')
            if stem in name_lower:
                slot.data = data
                slot.path = None
                return slot

        raise ValueError(
            f"File '{filename}' doesn't match any expected slot: "
            + ', '.join(s.filename for s in self._slots)
        )

    def load_directory(self, directory: Path | str) -> list[FileSlot]:
        """
        Attempt to load all matching files from a directory.
        Returns list of slots that were loaded.
        """
        directory = Path(directory)
        loaded = []
        for f in sorted(directory.iterdir()):
            if not f.is_file():
                continue
            try:
                slot = self.load_file(f)
                loaded.append(slot)
            except ValueError:
                pass
        return loaded

    def clear_slot(self, tag: str) -> None:
        for slot in self._slots:
            if slot.tag == tag:
                slot.data = None
                slot.path = None

    def clear_all(self) -> None:
        for slot in self._slots:
            slot.data = None
            slot.path = None

    def to_flash_files(self) -> list[FlashFile]:
        """Return loaded files as FlashFile objects, in programming order."""
        return [
            FlashFile(name=slot.filename, data=slot.data)
            for slot in self._slots
            if slot.loaded
        ]

    def validation_errors(self) -> list[str]:
        """Return list of human-readable validation errors.

        Messages reference the slot tag, never the firmware filename — the
        filename is a trade secret and must not surface in distributor-visible
        UI or logs.
        """
        errors = []
        for slot in self._slots:
            if slot.required and not slot.loaded:
                errors.append(f'Required file missing: {slot.tag}')
            if slot.loaded and slot.size == 0:
                errors.append(f'File is empty: {slot.tag}')
        return errors
