"""
MRS PLC Programmer — PyQt6 desktop application.

Provides a drag-and-drop GUI for loading firmware files and flashing an
MRS PLC over a PCAN-USB CAN bus adapter.

Run:  python programmer_app.py
"""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from mrs_protocol.constants import MODULE_TYPES
from mrs_protocol.file_loader import MRSFileSet, FileSlot
from mrs_protocol.protocol import MRSFlashEngine, FlashFile


# ---------------------------------------------------------------------------
# Logging bridge
# ---------------------------------------------------------------------------

class _QLogHandler(logging.Handler, QObject):
    message_emitted = pyqtSignal(str)

    def __init__(self) -> None:
        logging.Handler.__init__(self)
        QObject.__init__(self)

    def emit(self, record: logging.LogRecord) -> None:
        self.message_emitted.emit(self.format(record))


# ---------------------------------------------------------------------------
# Adapter check worker — tests PCAN connection in a QThread
# ---------------------------------------------------------------------------

class _CheckAdapterWorker(QObject):
    result = pyqtSignal(bool, str, str)  # ok, channel, message

    def __init__(self, bitrate: int, is_can_fd: bool) -> None:
        super().__init__()
        self._bitrate   = bitrate
        self._is_can_fd = is_can_fd

    def run(self) -> None:
        ok, channel, msg = MRSFlashEngine.detect_adapter(
            self._bitrate, self._is_can_fd
        )
        self.result.emit(ok, channel, msg)


# ---------------------------------------------------------------------------
# Download worker — fetches firmware files from GitHub in a QThread
# ---------------------------------------------------------------------------

class DownloadWorker(QObject):
    progress = pyqtSignal(float, str)
    finished = pyqtSignal(list)   # list[str] of loaded slot tags
    error    = pyqtSignal(str)

    def __init__(self, part: str, file_set: MRSFileSet) -> None:
        super().__init__()
        self._part     = part
        self._file_set = file_set

    def run(self) -> None:
        try:
            from mrs_protocol.github_downloader import download_part
            loaded = download_part(self._part, self._file_set, self.progress.emit)
            self.finished.emit(loaded)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Flash worker — runs the CAN flash sequence in a QThread
# ---------------------------------------------------------------------------

class FlashWorker(QObject):
    progress = pyqtSignal(float, str)
    finished = pyqtSignal()
    error    = pyqtSignal(str)

    def __init__(
        self,
        files:     list[FlashFile],
        channel:   str,
        bitrate:   int,
        is_can_fd: bool,
    ) -> None:
        super().__init__()
        self._files     = files
        self._channel   = channel
        self._bitrate   = bitrate
        self._is_can_fd = is_can_fd

    def run(self) -> None:
        try:
            with MRSFlashEngine(
                channel=self._channel,
                bitrate=self._bitrate,
                is_can_fd=self._is_can_fd,
            ) as engine:
                engine.flash(self._files, progress=self.progress.emit)
            self.finished.emit()
        except Exception as exc:
            self.error.emit(
                ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )


# ---------------------------------------------------------------------------
# File slot widget
# ---------------------------------------------------------------------------

class SlotWidget(QWidget):
    def __init__(self, slot: FileSlot) -> None:
        super().__init__()
        self._slot = slot

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        tag_label = QLabel(slot.tag)
        tag_label.setFixedWidth(100)
        layout.addWidget(tag_label)

        self._file_label = QLabel('— not loaded —')
        self._file_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self._file_label)

        req_label = QLabel('(required)' if slot.required else '(optional)')
        req_label.setStyleSheet('color: grey; font-size: 10px;')
        layout.addWidget(req_label)

        self.refresh()

    def refresh(self) -> None:
        if self._slot.loaded:
            name = self._slot.path.name if self._slot.path else self._slot.filename
            self._file_label.setText(f'{name}  ({self._slot.size:,} bytes)')
            self._file_label.setStyleSheet('color: #2a2; font-weight: bold;')
        else:
            self._file_label.setText('— not loaded —')
            self._file_label.setStyleSheet('color: #888;')


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle('MRS PLC Programmer — Styrestrøm AS')
        self.setMinimumSize(720, 680)
        self.setAcceptDrops(True)

        self._file_set       = MRSFileSet()
        self._detected_channel: str = ''
        self._dl_worker:     Optional[DownloadWorker] = None
        self._dl_thread:     Optional[QThread]        = None
        self._flash_worker:  Optional[FlashWorker]    = None
        self._flash_thread:  Optional[QThread]        = None

        self._build_ui()
        self._setup_logging()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)

        # ── Connection ────────────────────────────────────────────────
        conn_box = QGroupBox('PCAN Adapter')
        conn_layout = QHBoxLayout(conn_box)
        conn_layout.addWidget(QLabel('Module:'))
        self._module_combo = QComboBox()
        for name in MODULE_TYPES:
            self._module_combo.addItem(name)
        conn_layout.addWidget(self._module_combo)
        conn_layout.addSpacing(16)

        self._check_conn_btn = QPushButton('Detect adapter')
        self._check_conn_btn.setFixedWidth(120)
        self._check_conn_btn.clicked.connect(self._on_check_connection)
        conn_layout.addWidget(self._check_conn_btn)

        self._conn_status = QLabel('  Not connected')
        self._conn_status.setStyleSheet('color: #c22; font-weight: bold;')
        conn_layout.addWidget(self._conn_status)

        conn_layout.addStretch()
        root.addWidget(conn_box)

        # ── Part download from GitHub ─────────────────────────────────
        gh_box = QGroupBox('Download firmware from Styrestrøm')
        gh_layout = QHBoxLayout(gh_box)

        gh_layout.addWidget(QLabel('Part:'))
        self._part_combo = QComboBox()
        self._part_combo.setMinimumWidth(260)
        self._part_combo.setPlaceholderText('— click Refresh to load list —')
        gh_layout.addWidget(self._part_combo)

        self._refresh_btn = QPushButton('Refresh list')
        self._refresh_btn.setFixedWidth(110)
        self._refresh_btn.clicked.connect(self._on_refresh_parts)
        gh_layout.addWidget(self._refresh_btn)

        self._download_btn = QPushButton('Download')
        self._download_btn.setFixedWidth(110)
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download_part)
        gh_layout.addWidget(self._download_btn)

        gh_layout.addStretch()
        root.addWidget(gh_box)

        # ── Firmware file slots ───────────────────────────────────────
        files_box = QGroupBox('Firmware files  (or drag & drop files here)')
        files_layout = QVBoxLayout(files_box)
        self._slot_widgets: list[SlotWidget] = []
        for slot in self._file_set.slots:
            sw = SlotWidget(slot)
            files_layout.addWidget(sw)
            self._slot_widgets.append(sw)
        clear_btn = QPushButton('Clear all files')
        clear_btn.clicked.connect(self._on_clear_all)
        files_layout.addWidget(clear_btn)
        root.addWidget(files_box)

        # ── Progress ──────────────────────────────────────────────────
        prog_box = QGroupBox('Progress')
        prog_layout = QVBoxLayout(prog_box)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        prog_layout.addWidget(self._progress_bar)
        self._status_label = QLabel('Ready')
        prog_layout.addWidget(self._status_label)
        root.addWidget(prog_box)

        # ── Log ───────────────────────────────────────────────────────
        log_box = QGroupBox('Log')
        log_layout = QVBoxLayout(log_box)
        self._log_edit = QTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setFont(QFont('Courier New', 9))
        log_layout.addWidget(self._log_edit)
        root.addWidget(log_box)

        # ── Flash button ──────────────────────────────────────────────
        self._flash_btn = QPushButton('Flash PLC')
        self._flash_btn.setFixedHeight(40)
        self._flash_btn.setStyleSheet(
            'QPushButton { background: #1a7fd4; color: white; font-weight: bold; font-size: 14px; }'
            'QPushButton:disabled { background: #888; }'
        )
        self._flash_btn.clicked.connect(self._on_flash)
        root.addWidget(self._flash_btn)

    def _setup_logging(self) -> None:
        self._log_handler = _QLogHandler()
        self._log_handler.setFormatter(
            logging.Formatter('%(levelname)-8s %(name)s — %(message)s')
        )
        self._log_handler.message_emitted.connect(self._append_log)
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger().setLevel(logging.DEBUG)

    # ------------------------------------------------------------------
    # Adapter connection check
    # ------------------------------------------------------------------

    def _on_check_connection(self) -> None:
        self._check_conn_btn.setEnabled(False)
        self._conn_status.setText('  Scanning…')
        self._conn_status.setStyleSheet('color: #888; font-weight: bold;')

        module_name = self._module_combo.currentText()
        cfg = MODULE_TYPES[module_name]

        self._conn_worker = _CheckAdapterWorker(cfg['bitrate'], cfg['can_fd'])
        self._conn_thread = QThread(self)
        self._conn_worker.moveToThread(self._conn_thread)
        self._conn_thread.started.connect(self._conn_worker.run)
        self._conn_worker.result.connect(self._on_conn_result)
        self._conn_thread.start()

    def _on_conn_result(self, ok: bool, channel: str, msg: str) -> None:
        self._conn_thread.quit()
        self._check_conn_btn.setEnabled(True)
        if ok:
            self._detected_channel = channel
            self._conn_status.setText(f'  {channel}')
            self._conn_status.setStyleSheet('color: #2a2; font-weight: bold;')
            self._append_log(f'PCAN adapter found on {channel}')
        else:
            self._detected_channel = ''
            self._conn_status.setText('  Not connected')
            self._conn_status.setStyleSheet('color: #c22; font-weight: bold;')
            self._append_log(f'PCAN adapter not found: {msg}')

    # ------------------------------------------------------------------
    # GitHub download
    # ------------------------------------------------------------------

    def _on_refresh_parts(self) -> None:
        self._refresh_btn.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._part_combo.clear()
        self._status_label.setText('Fetching part list from GitHub…')

        # Run in a thread so the UI doesn't freeze.
        # Store as instance vars so they don't get garbage collected.
        self._parts_worker = _ListPartsWorker()
        self._parts_thread = QThread(self)
        self._parts_worker.moveToThread(self._parts_thread)
        self._parts_thread.started.connect(self._parts_worker.run)
        self._parts_worker.finished.connect(
            lambda parts: self._on_parts_loaded(parts)
        )
        self._parts_worker.error.connect(
            lambda msg: self._on_parts_error(msg)
        )
        self._parts_thread.start()

    def _on_parts_loaded(self, parts: list[str]) -> None:
        self._parts_thread.quit()
        self._refresh_btn.setEnabled(True)
        self._part_combo.clear()
        for p in parts:
            self._part_combo.addItem(p)
        self._download_btn.setEnabled(bool(parts))
        self._status_label.setText(f'{len(parts)} part(s) found.')
        self._append_log(f'Parts available: {", ".join(parts)}')

    def _on_parts_error(self, msg: str) -> None:
        self._parts_thread.quit()
        self._refresh_btn.setEnabled(True)
        self._status_label.setText('Error fetching parts — see log')
        self._append_log(f'GitHub error: {msg}')
        QMessageBox.warning(self, 'GitHub error', msg)

    def _on_download_part(self) -> None:
        part = self._part_combo.currentText()
        if not part:
            return

        self._file_set.clear_all()
        self._refresh_slots()
        self._download_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._flash_btn.setEnabled(False)
        self._progress_bar.setValue(0)
        self._status_label.setText(f'Downloading {part}…')
        self._append_log(f'Downloading part: {part}')

        self._dl_worker = DownloadWorker(part, self._file_set)
        self._dl_thread = QThread()
        self._dl_worker.moveToThread(self._dl_thread)

        self._dl_thread.started.connect(self._dl_worker.run)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.finished.connect(self._on_dl_done)
        self._dl_worker.error.connect(self._on_dl_error)
        self._dl_worker.finished.connect(self._dl_thread.quit)
        self._dl_worker.error.connect(self._dl_thread.quit)
        self._dl_thread.finished.connect(self._dl_thread.deleteLater)

        self._dl_thread.start()

    def _on_dl_progress(self, fraction: float, message: str) -> None:
        self._progress_bar.setValue(int(fraction * 100))
        self._status_label.setText(message)

    def _on_dl_done(self, loaded_tags: list[str]) -> None:
        self._refresh_slots()
        self._download_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._flash_btn.setEnabled(True)
        self._progress_bar.setValue(100)
        self._status_label.setText(f'Downloaded: {", ".join(loaded_tags)}')
        self._append_log(f'Files loaded: {", ".join(loaded_tags)}')

        errors = self._file_set.validation_errors()
        if errors:
            self._append_log('WARNING — ' + '; '.join(errors))

    def _on_dl_error(self, msg: str) -> None:
        self._refresh_slots()
        self._download_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._flash_btn.setEnabled(True)
        self._status_label.setText('Download failed — see log')
        self._append_log(f'Download error: {msg}')
        QMessageBox.critical(self, 'Download failed', msg)

    # ------------------------------------------------------------------
    # Drag & drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir():
                self._file_set.load_directory(path)
                self._append_log(f'Loaded directory: {path}')
            elif path.is_file():
                try:
                    slot = self._file_set.load_file(path)
                    self._append_log(f'Loaded: {path.name} → {slot.tag}')
                except ValueError as exc:
                    self._append_log(f'Skipped: {path.name} — {exc}')
        self._refresh_slots()

    # ------------------------------------------------------------------
    # Flash
    # ------------------------------------------------------------------

    def _on_clear_all(self) -> None:
        self._file_set.clear_all()
        self._refresh_slots()
        self._append_log('All files cleared.')

    def _on_flash(self) -> None:
        errors = self._file_set.validation_errors()
        if errors:
            QMessageBox.warning(
                self, 'Files missing',
                'Cannot flash — missing required files:\n\n' + '\n'.join(errors),
            )
            return

        if not self._detected_channel:
            QMessageBox.warning(
                self, 'No adapter',
                'No PCAN adapter detected.\n\nClick "Detect adapter" first.',
            )
            return

        module_name = self._module_combo.currentText()
        cfg       = MODULE_TYPES[module_name]
        channel   = self._detected_channel
        bitrate   = cfg['bitrate']
        is_can_fd = cfg['can_fd']
        files     = self._file_set.to_flash_files()

        self._flash_btn.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._progress_bar.setValue(0)
        self._status_label.setText('Flashing…')
        self._append_log(f'Starting flash — module: {module_name}  channel: {channel}')

        self._flash_worker = FlashWorker(files, channel, bitrate, is_can_fd)
        self._flash_thread = QThread()
        self._flash_worker.moveToThread(self._flash_thread)

        self._flash_thread.started.connect(self._flash_worker.run)
        self._flash_worker.progress.connect(self._on_progress)
        self._flash_worker.finished.connect(self._on_flash_done)
        self._flash_worker.error.connect(self._on_flash_error)
        self._flash_worker.finished.connect(self._flash_thread.quit)
        self._flash_worker.error.connect(self._flash_thread.quit)
        self._flash_thread.finished.connect(self._flash_thread.deleteLater)

        self._flash_thread.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self._progress_bar.setValue(int(fraction * 100))
        self._status_label.setText(message)

    def _on_flash_done(self) -> None:
        self._flash_btn.setEnabled(True)
        self._download_btn.setEnabled(True)
        self._progress_bar.setValue(100)
        self._status_label.setText('Flash complete!')
        self._append_log('Flash complete.')
        QMessageBox.information(self, 'Done', 'PLC flashed successfully.')

    def _on_flash_error(self, tb: str) -> None:
        self._flash_btn.setEnabled(True)
        self._download_btn.setEnabled(True)
        self._status_label.setText('Error — see log')
        self._append_log('ERROR:\n' + tb)
        QMessageBox.critical(self, 'Flash failed', 'An error occurred:\n\n' + tb[:500])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _refresh_slots(self) -> None:
        for sw in self._slot_widgets:
            sw.refresh()

    def _append_log(self, text: str) -> None:
        self._log_edit.append(text)
        self._log_edit.ensureCursorVisible()


# ---------------------------------------------------------------------------
# Helper worker for listing parts (keeps _on_refresh_parts clean)
# ---------------------------------------------------------------------------

class _ListPartsWorker(QObject):
    finished = pyqtSignal(list)
    error    = pyqtSignal(str)

    def run(self) -> None:
        try:
            from mrs_protocol.github_downloader import list_parts
            self.finished.emit(list_parts())
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
