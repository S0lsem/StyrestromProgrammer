"""
MRS PLC Programmer — PyQt6 desktop application.

Distributor-facing flashing tool. Operator picks a part, the firmware is
fetched from the Styrestrøm proxy (never exposed as files on disk), and
the PLC is flashed over a PCAN-USB CAN bus adapter.

Run:  python programmer_app.py
"""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QSettings
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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

from types import SimpleNamespace

from mrs_protocol import event_logger
from mrs_protocol.constants import MODULE_TYPES
from mrs_protocol.console_flasher import run_flash
from mrs_protocol.protocol import detect_adapter, scan_plc, ScanError
from mrs_protocol.s19_parser import Firmware
from mrs_protocol.version import APP_VERSION


# ---------------------------------------------------------------------------
# Update check worker
# ---------------------------------------------------------------------------

class _UpdateCheckWorker(QObject):
    result = pyqtSignal(dict)

    def run(self) -> None:
        from mrs_protocol.update_checker import check_for_update
        self.result.emit(check_for_update())


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

    def __init__(self, bitrate: int, is_can_fd: bool, data_bitrate: int) -> None:
        super().__init__()
        self._bitrate      = bitrate
        self._is_can_fd    = is_can_fd
        self._data_bitrate = data_bitrate

    def run(self) -> None:
        ok, channel, msg = detect_adapter(
            self._bitrate, self._is_can_fd, self._data_bitrate
        )
        self.result.emit(ok, channel, msg)


# ---------------------------------------------------------------------------
# Download worker — fetches and parses the firmware.s19 in a QThread
# ---------------------------------------------------------------------------

class DownloadWorker(QObject):
    progress = pyqtSignal(float, str)
    finished = pyqtSignal(object)   # Firmware
    error    = pyqtSignal(str)

    def __init__(self, part: str) -> None:
        super().__init__()
        self._part = part

    def run(self) -> None:
        try:
            from mrs_protocol.github_downloader import download_part
            firmware = download_part(self._part, self.progress.emit)
            self.finished.emit(firmware)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Operator identity dialog — self-reported distributor + operator initials,
# persisted via QSettings so HQ can attribute every flash event.
# ---------------------------------------------------------------------------

class _IdentityDialog(QDialog):
    def __init__(self, parent, distributor: str, operator: str) -> None:
        super().__init__(parent)
        self.setWindowTitle('Operator identity')
        self.setMinimumWidth(360)

        self._distributor_edit = QLineEdit(distributor)
        self._distributor_edit.setPlaceholderText('e.g. Acme Norway AS')
        self._operator_edit = QLineEdit(operator)
        self._operator_edit.setPlaceholderText('e.g. EJS')
        self._operator_edit.setMaxLength(16)

        form = QFormLayout()
        form.addRow('Distributor:', self._distributor_edit)
        form.addRow('Operator initials:', self._operator_edit)

        info = QLabel(
            'Every flash will be tagged with these so Styrestrøm HQ can '
            'see which distributor / operator programmed each PLC.'
        )
        info.setWordWrap(True)
        info.setStyleSheet('color: #555; font-size: 11px;')

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        self._buttons = buttons

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(info)
        root.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self._distributor_edit.text().strip():
            QMessageBox.warning(self, 'Required', 'Distributor name is required.')
            return
        if not self._operator_edit.text().strip():
            QMessageBox.warning(self, 'Required', 'Operator initials are required.')
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        return (
            self._distributor_edit.text().strip(),
            self._operator_edit.text().strip(),
        )


# ---------------------------------------------------------------------------
# Scan worker — listens for a PLC boot announcement and reads identity
# ---------------------------------------------------------------------------

class ScanWorker(QObject):
    result = pyqtSignal(object)   # PLCInfo
    error  = pyqtSignal(str)

    def __init__(
        self,
        channel:      str,
        bitrate:      int,
        is_can_fd:    bool,
        data_bitrate: int,
    ) -> None:
        super().__init__()
        self._channel      = channel
        self._bitrate      = bitrate
        self._is_can_fd    = is_can_fd
        self._data_bitrate = data_bitrate

    def run(self) -> None:
        try:
            info = scan_plc(
                self._channel, self._bitrate, self._is_can_fd, self._data_bitrate
            )
            self.result.emit(info)
        except ScanError as exc:
            self.error.emit(str(exc))
        except Exception as exc:
            self.error.emit(
                ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )


# ---------------------------------------------------------------------------
# Batch listener — between flashes in batch mode, watches for the next PLC's
# boot announcement so the GUI can auto-trigger the next flash. The bus is
# released as soon as the announcement is seen because the console flasher
# needs exclusive PCAN access.
# ---------------------------------------------------------------------------

class BatchListenerWorker(QObject):
    plc_detected = pyqtSignal(int)   # serial decoded from boot announcement
    error        = pyqtSignal(str)

    def __init__(self, channel: str) -> None:
        super().__init__()
        self._channel = channel
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        from mrs_protocol.protocol import CAN_ID_PLC_BOOT
        import can
        try:
            bus = can.Bus(
                interface='pcan',
                channel=self._channel,
                bitrate=125000,
                fd=False,
            )
        except Exception as exc:
            self.error.emit(f'Batch listener could not open PCAN at 125k: {exc}')
            return

        serial = 0
        try:
            while not self._stop:
                msg = bus.recv(timeout=0.5)
                if msg is None:
                    continue
                if msg.arbitration_id == CAN_ID_PLC_BOOT and len(msg.data) >= 5:
                    data = bytes(msg.data)
                    serial = (data[2] << 16) | (data[3] << 8) | data[4]
                    break
        finally:
            try:
                bus.shutdown()
            except Exception:
                pass

        if not self._stop:
            self.plc_detected.emit(serial)


# ---------------------------------------------------------------------------
# Flash worker — runs the CAN flash sequence in a QThread
# ---------------------------------------------------------------------------

class FlashWorker(QObject):
    progress   = pyqtSignal(float, str)
    plc_found  = pyqtSignal(object)   # SimpleNamespace(serial=str, label=str)
    finished   = pyqtSignal()
    error      = pyqtSignal(str)

    def __init__(self, firmware: Firmware) -> None:
        super().__init__()
        self._firmware = firmware

    def run(self) -> None:
        try:
            def _on_plc(sn: str, label: str) -> None:
                self.plc_found.emit(SimpleNamespace(serial=sn, label=label))

            result = run_flash(
                self._firmware,
                progress=self.progress.emit,
                plc_found=_on_plc,
            )

            if result.success:
                self.finished.emit()
                return

            detail = result.error_message or f'exit code {result.exit_code}'
            if result.error_code:
                detail = f'0x{result.error_code:02X} ({result.error_code}): {detail}'
            self.error.emit(
                f'Console flasher reported failure: {detail}\n\n'
                f'--- flasher output ---\n{result.output}'
            )
        except Exception as exc:
            self.error.emit(
                ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f'Styrestrøm AS PLC Programmer V. {APP_VERSION}')
        self.setMinimumSize(720, 700)

        self._firmware: Optional[Firmware] = None
        self._loaded_part_name: str        = ''
        self._detected_channel: str        = ''
        self._dl_worker:     Optional[DownloadWorker] = None
        self._dl_thread:     Optional[QThread]        = None
        self._flash_worker:  Optional[FlashWorker]    = None
        self._flash_thread:  Optional[QThread]        = None
        self._scan_worker:   Optional[ScanWorker]     = None
        self._scan_thread:   Optional[QThread]        = None
        self._batch_listener: Optional[BatchListenerWorker] = None
        self._batch_thread:   Optional[QThread]             = None
        self._last_scan_label: str = ''   # carried into the flash event

        self._settings = QSettings('Styrestrom', 'MRS Programmer')

        self._build_ui()
        self._build_menu()
        self._setup_logging()
        self._check_for_updates()
        self._ensure_identity()
        event_logger.replay_pending()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)

        # ── Update banner (hidden by default) ─────────────────────────
        self._update_banner = QLabel()
        self._update_banner.setStyleSheet(
            'background: #fff3cd; color: #856404; padding: 8px; '
            'border: 1px solid #ffc107; border-radius: 4px; font-size: 12px;'
        )
        self._update_banner.setOpenExternalLinks(True)
        self._update_banner.setVisible(False)
        root.addWidget(self._update_banner)

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

        self._scan_btn = QPushButton('Scan PLC')
        self._scan_btn.setFixedWidth(100)
        self._scan_btn.setToolTip(
            'Listen for a PLC boot announcement, then read its identity '
            '(SN, article, app name + version). Power-cycle the PLC after '
            'clicking. Read-only — does not erase or flash.'
        )
        self._scan_btn.clicked.connect(self._on_scan)
        conn_layout.addWidget(self._scan_btn)

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

        # ── Batch mode ────────────────────────────────────────────────
        batch_box = QGroupBox('Batch programming')
        batch_layout = QHBoxLayout(batch_box)
        self._batch_check = QCheckBox('Keep firmware loaded + auto-flash on next PLC boot')
        self._batch_check.setToolTip(
            'When checked, firmware stays loaded after flashing and the app\n'
            'listens for the next PLC to boot. Power-cycle the next PLC and\n'
            'flashing starts automatically — no need to click Flash PLC.'
        )
        self._batch_check.stateChanged.connect(self._on_batch_toggled)
        batch_layout.addWidget(self._batch_check)
        batch_layout.addStretch()
        root.addWidget(batch_box)

        # ── Firmware status ───────────────────────────────────────────
        fw_box = QGroupBox('Firmware')
        fw_layout = QHBoxLayout(fw_box)
        self._firmware_label = QLabel('— not loaded —')
        self._firmware_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._firmware_label.setStyleSheet('color: #888;')
        fw_layout.addWidget(self._firmware_label)
        self._clear_fw_btn = QPushButton('Clear')
        self._clear_fw_btn.setFixedWidth(80)
        self._clear_fw_btn.setEnabled(False)
        self._clear_fw_btn.clicked.connect(self._on_clear_firmware)
        fw_layout.addWidget(self._clear_fw_btn)
        root.addWidget(fw_box)

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

        log_btn_row = QHBoxLayout()
        log_btn_row.addStretch()
        self._clear_log_btn = QPushButton('Clear log')
        self._clear_log_btn.setFixedWidth(100)
        self._clear_log_btn.clicked.connect(self._log_edit.clear)
        log_btn_row.addWidget(self._clear_log_btn)
        log_layout.addLayout(log_btn_row)

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
    # Operator identity (persisted via QSettings, posted with every event)
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        bar = self.menuBar()
        settings_menu = bar.addMenu('&Settings')
        action = QAction('&Operator identity…', self)
        action.triggered.connect(self._on_change_identity)
        settings_menu.addAction(action)

    def _distributor(self) -> str:
        return str(self._settings.value('distributor', '', type=str))

    def _operator(self) -> str:
        return str(self._settings.value('operator', '', type=str))

    def _ensure_identity(self) -> None:
        """Prompt on first run; subsequent launches read from QSettings."""
        if self._distributor() and self._operator():
            return
        self._prompt_identity(first_run=True)

    def _on_change_identity(self) -> None:
        self._prompt_identity(first_run=False)

    def _prompt_identity(self, first_run: bool) -> None:
        dlg = _IdentityDialog(self, self._distributor(), self._operator())
        if first_run:
            dlg.setWindowTitle('Welcome — set your operator identity')
        if dlg.exec() == QDialog.DialogCode.Accepted:
            distributor, operator = dlg.values()
            self._settings.setValue('distributor', distributor)
            self._settings.setValue('operator', operator)
            self._append_log(
                f'Operator identity set: {operator} @ {distributor}'
            )

    # ------------------------------------------------------------------
    # Update check (runs on startup, background thread)
    # ------------------------------------------------------------------

    def _check_for_updates(self) -> None:
        self._update_worker = _UpdateCheckWorker()
        self._update_thread = QThread(self)
        self._update_worker.moveToThread(self._update_thread)
        self._update_thread.started.connect(self._update_worker.run)
        self._update_worker.result.connect(self._on_update_result)
        self._update_thread.start()

    def _on_update_result(self, info: dict) -> None:
        self._update_thread.quit()
        if info.get('error'):
            return  # silently ignore — don't bother the user
        if info.get('update_available'):
            version = info['latest_version']
            url = info.get('download_url', '')
            if url:
                self._update_banner.setText(
                    f'New version <b>{version}</b> available! '
                    f'<a href="{url}">Download update</a>'
                )
            else:
                self._update_banner.setText(
                    f'New version <b>{version}</b> available on GitHub.'
                )
            self._update_banner.setVisible(True)
            self._append_log(f'Update available: {version}')

    # ------------------------------------------------------------------
    # Adapter connection check
    # ------------------------------------------------------------------

    def _on_check_connection(self) -> None:
        self._check_conn_btn.setEnabled(False)
        self._conn_status.setText('  Scanning…')
        self._conn_status.setStyleSheet('color: #888; font-weight: bold;')

        module_name = self._module_combo.currentText()
        cfg = MODULE_TYPES[module_name]

        self._conn_worker = _CheckAdapterWorker(cfg['bitrate'], cfg['can_fd'], cfg['data_bitrate'])
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

        self._firmware = None
        self._loaded_part_name = ''
        self._refresh_firmware_label()
        self._download_btn.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._flash_btn.setEnabled(False)
        self._progress_bar.setValue(0)
        self._status_label.setText(f'Downloading {part}…')
        self._append_log(f'Downloading part: {part}')

        self._dl_worker = DownloadWorker(part)
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

    def _on_dl_done(self, firmware: Firmware) -> None:
        self._firmware = firmware
        self._loaded_part_name = self._part_combo.currentText()
        self._refresh_firmware_label()
        self._download_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._flash_btn.setEnabled(True)
        self._progress_bar.setValue(100)
        self._status_label.setText(f'Firmware ready: {self._loaded_part_name}')
        self._append_log(
            f'Firmware loaded: {self._loaded_part_name} '
            f'({len(firmware):,} bytes from 0x{firmware.start_address:04X})'
        )
        # If batch was already on when the user downloaded firmware, this
        # is the moment all preconditions become satisfied.
        self._maybe_start_batch_listener()

    def _on_dl_error(self, msg: str) -> None:
        self._refresh_firmware_label()
        self._download_btn.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._flash_btn.setEnabled(True)
        self._status_label.setText('Download failed — see log')
        self._append_log(f'Download error: {msg}')
        QMessageBox.critical(self, 'Download failed', msg)

    # ------------------------------------------------------------------
    # Flash
    # ------------------------------------------------------------------

    def _on_clear_firmware(self) -> None:
        self._stop_batch_listener()
        self._firmware = None
        self._loaded_part_name = ''
        self._refresh_firmware_label()
        self._append_log('Firmware cleared.')

    # ------------------------------------------------------------------
    # Batch listener — auto-flash next power-cycled PLC
    # ------------------------------------------------------------------

    def _on_batch_toggled(self, _state: int) -> None:
        if self._batch_check.isChecked():
            self._maybe_start_batch_listener()
        else:
            self._stop_batch_listener()
            if not (self._flash_thread and self._flash_thread.isRunning()):
                self._status_label.setText('Ready')

    def _maybe_start_batch_listener(self) -> None:
        """Start the listener if all preconditions hold; no-op otherwise."""
        if not self._batch_check.isChecked():
            return
        if self._firmware is None:
            return
        if not self._detected_channel:
            return
        if self._thread_is_running(self._flash_thread):
            return
        if self._thread_is_running(self._batch_thread):
            return
        self._start_batch_listener()

    @staticmethod
    def _thread_is_running(thread) -> bool:
        """Safe isRunning() — the QThread may have been deleteLater-ed,
        in which case touching it raises RuntimeError from sip."""
        if thread is None:
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            return False

    def _start_batch_listener(self) -> None:
        self._batch_listener = BatchListenerWorker(self._detected_channel)
        self._batch_thread = QThread()
        self._batch_listener.moveToThread(self._batch_thread)

        self._batch_thread.started.connect(self._batch_listener.run)
        self._batch_listener.plc_detected.connect(self._on_batch_plc_detected)
        self._batch_listener.error.connect(self._on_batch_listener_error)
        self._batch_listener.plc_detected.connect(self._batch_thread.quit)
        self._batch_listener.error.connect(self._batch_thread.quit)
        # Qt-managed cleanup. Order matters: our ref-clear slot fires first
        # (drops Python refs while wrappers are still valid), then deleteLater
        # schedules C++ destruction. Without explicit deleteLater the Python
        # GC can race the Qt event loop and crash on pending signal slots.
        self._batch_thread.finished.connect(self._on_batch_thread_finished)
        self._batch_thread.finished.connect(self._batch_listener.deleteLater)
        self._batch_thread.finished.connect(self._batch_thread.deleteLater)

        self._batch_thread.start()
        self._status_label.setText('Batch mode — waiting for next PLC…')
        self._append_log('Batch mode: listening for next PLC boot announcement.')

    def _on_batch_thread_finished(self) -> None:
        """Drop Python refs once the thread has fully exited. Safe because
        the connected bound-method slots captured the worker at connect time
        and don't depend on self._batch_listener."""
        self._batch_listener = None
        self._batch_thread = None

    def _stop_batch_listener(self) -> None:
        if self._batch_listener is None:
            return
        self._batch_listener.request_stop()
        if self._thread_is_running(self._batch_thread):
            try:
                self._batch_thread.quit()
                self._batch_thread.wait(2000)
            except RuntimeError:
                pass
        self._batch_listener = None
        self._batch_thread = None

    def _on_batch_plc_detected(self, serial: int) -> None:
        # Ref cleanup happens via _on_batch_thread_finished — touching the
        # worker/thread refs here while signals are still in flight crashes
        # Qt.
        self._append_log(f'Auto-detected PLC SN {serial} on boot — starting flash.')
        self._on_flash()

    def _on_batch_listener_error(self, msg: str) -> None:
        self._append_log(f'Batch listener stopped: {msg}')
        self._status_label.setText('Batch listener error — see log.')

    def closeEvent(self, event) -> None:   # noqa: N802 — Qt method name
        self._stop_batch_listener()
        super().closeEvent(event)

    def _on_flash(self) -> None:
        if self._firmware is None:
            QMessageBox.warning(
                self, 'No firmware',
                'Pick a part and click Download before flashing.',
            )
            return

        if not self._detected_channel:
            QMessageBox.warning(
                self, 'No adapter',
                'No PCAN adapter detected.\n\nClick "Detect adapter" first.',
            )
            return

        # If the batch listener is running, stop it — the flasher needs
        # exclusive PCAN access. We'll restart it after this flash completes.
        self._stop_batch_listener()

        module_name  = self._module_combo.currentText()
        channel      = self._detected_channel
        firmware     = self._firmware

        self._last_plc_info = None
        self._flash_btn.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._progress_bar.setValue(0)
        self._status_label.setText('Starting MRS console flasher…')
        self._append_log(f'Starting flash — module: {module_name}  channel: {channel}')
        self._append_log('Console flasher will detect the PLC; power-cycle it if needed.')

        self._flash_worker = FlashWorker(firmware)
        self._flash_thread = QThread()
        self._flash_worker.moveToThread(self._flash_thread)

        self._flash_thread.started.connect(self._flash_worker.run)
        self._flash_worker.progress.connect(self._on_progress)
        self._flash_worker.plc_found.connect(self._on_plc_found)
        self._flash_worker.finished.connect(self._on_flash_done)
        self._flash_worker.error.connect(self._on_flash_error)
        self._flash_worker.finished.connect(self._flash_thread.quit)
        self._flash_worker.error.connect(self._flash_thread.quit)
        self._flash_thread.finished.connect(self._flash_thread.deleteLater)

        self._flash_thread.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self._progress_bar.setValue(int(fraction * 100))
        self._status_label.setText(message)

    def _on_plc_found(self, info) -> None:
        self._last_plc_info = info
        self._last_scan_label = info.label
        self._append_log(f'PLC detected — SN:{info.serial}  {info.label}')

    def _on_flash_done(self) -> None:
        self._flash_btn.setEnabled(True)
        self._download_btn.setEnabled(True)
        self._progress_bar.setValue(100)
        self._status_label.setText('Flash complete!')
        self._append_log('Flash complete.')

        part    = self._part_combo.currentText() or '—'
        module  = self._module_combo.currentText()
        channel = self._detected_channel
        info    = self._last_plc_info
        serial  = str(info.serial) if info else ''

        # Write flash log
        from mrs_protocol.flash_log import write_entry
        log_path = write_entry(
            part=part, module=module, channel=channel, success=True, serial=serial,
            distributor=self._distributor(), operator=self._operator(),
        )
        self._append_log(f'Flash logged to {log_path}')

        # Report event to the proxy (with offline fallback)
        self._post_flash_event(
            plc_serial=serial,
            part=part,
            module=module,
            channel=channel,
            result='OK',
        )

        # Generate report
        from mrs_protocol.flash_report import generate_report, save_report
        report = generate_report(
            part=part, module=module, channel=channel, serial=serial,
        )
        report_path = save_report(report)
        self._append_log(f'Report saved to {report_path}')

        # Show result
        reply = QMessageBox.information(
            self, 'Flash complete',
            f'PLC flashed successfully.\n\n'
            f'Part: {part}\nModule: {module}\n\n'
            f'Save report to a custom location?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Ok,
        )
        if reply == QMessageBox.StandardButton.Save:
            path, _ = QFileDialog.getSaveFileName(
                self, 'Save flash report', f'flash_report_{part}.txt',
                'Text files (*.txt)',
            )
            if path:
                save_report(report, directory=Path(path).parent)
                self._append_log(f'Report saved to {path}')

        # Batch mode: keep firmware loaded, or clear it
        if not self._batch_check.isChecked():
            self._firmware = None
            self._loaded_part_name = ''
            self._refresh_firmware_label()
        else:
            # Firmware stayed loaded → start listening for the next PLC.
            self._maybe_start_batch_listener()

    def _on_flash_error(self, tb: str) -> None:
        self._flash_btn.setEnabled(True)
        self._download_btn.setEnabled(True)
        self._status_label.setText('Error — see log')
        self._append_log('ERROR:\n' + tb)

        part   = self._part_combo.currentText() or '—'
        module = self._module_combo.currentText()
        info   = self._last_plc_info
        serial = str(info.serial) if info else ''

        # Log the failure
        from mrs_protocol.flash_log import write_entry
        write_entry(
            part=part, module=module, channel=self._detected_channel,
            success=False, error_msg=tb[:200],
            distributor=self._distributor(), operator=self._operator(),
        )

        # Report failure event to the proxy
        self._post_flash_event(
            plc_serial=serial,
            part=part,
            module=module,
            channel=self._detected_channel,
            result='FAIL',
            error_message=tb[:500],
        )

        QMessageBox.critical(self, 'Flash failed', 'An error occurred:\n\n' + tb[:500])

        # If batch mode is still on and firmware is still loaded, keep
        # listening for the next PLC — a single bad unit should not abort
        # an in-progress batch run.
        self._maybe_start_batch_listener()

    def _post_flash_event(
        self,
        *,
        plc_serial:    str,
        part:          str,
        module:        str,
        channel:       str,
        result:        str,
        error_message: str = '',
    ) -> None:
        """Send the flash event to the proxy with offline fallback."""
        distributor = self._distributor()
        operator    = self._operator()
        if not distributor or not operator:
            self._append_log(
                'Event NOT reported — operator identity is unset. '
                'Open Settings → Operator identity… to fix.'
            )
            return

        event = event_logger.build_event(
            distributor=distributor,
            operator=operator,
            plc_serial=plc_serial,
            part=part,
            module=module,
            channel=channel,
            result=result,
            error_message=error_message,
            scan_label=self._last_scan_label,
        )
        response = event_logger.report_event(event)
        if response is None:
            self._append_log(
                'Event queued offline (proxy unreachable); will retry on next launch.'
            )
        else:
            tag = (
                'FIRST-TIME PROGRAM'
                if response.get('first_program_for_sn') else
                'reflash'
            )
            self._append_log(
                f'Event reported to HQ ({tag}) by {operator} @ {distributor}.'
            )

    # ------------------------------------------------------------------
    # Scan handlers
    # ------------------------------------------------------------------

    def _on_scan(self) -> None:
        if not self._detected_channel:
            QMessageBox.warning(
                self, 'No adapter',
                'No PCAN adapter detected.\n\nClick "Detect adapter" first.',
            )
            return

        module_name = self._module_combo.currentText()
        cfg         = MODULE_TYPES[module_name]

        self._scan_btn.setEnabled(False)
        self._check_conn_btn.setEnabled(False)
        self._flash_btn.setEnabled(False)
        self._status_label.setText('Scanning — power-cycle the PLC now…')
        self._append_log(
            f'Scan started — module: {module_name}  channel: {self._detected_channel}'
        )
        self._append_log('Power-cycle the PLC now…')

        self._scan_worker = ScanWorker(
            self._detected_channel,
            cfg['bitrate'],
            cfg['can_fd'],
            cfg['data_bitrate'],
        )
        self._scan_thread = QThread()
        self._scan_worker.moveToThread(self._scan_thread)

        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.result.connect(self._on_scan_done)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.result.connect(self._scan_thread.quit)
        self._scan_worker.error.connect(self._scan_thread.quit)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)

        self._scan_thread.start()

    def _on_scan_done(self, info) -> None:
        self._scan_btn.setEnabled(True)
        self._check_conn_btn.setEnabled(True)
        self._flash_btn.setEnabled(True)
        self._status_label.setText(f'PLC found — SN {info.serial}')

        self._append_log(f'PLC FOUND — SN: {info.serial}')
        self._append_log(f'  Article:     {info.article}')
        self._append_log(f'  Revision:    {info.revision}')
        self._append_log(f'  App name:    {info.app_name}')
        self._append_log(f'  App version: {info.app_version}')
        if info.description:
            self._append_log(f'  Description: {info.description}')
        self._append_log('Power-cycle the PLC again before clicking Flash.')

    def _on_scan_error(self, msg: str) -> None:
        self._scan_btn.setEnabled(True)
        self._check_conn_btn.setEnabled(True)
        self._flash_btn.setEnabled(True)
        self._status_label.setText('Scan failed')
        self._append_log(f'Scan failed: {msg}')
        QMessageBox.warning(self, 'Scan failed', msg)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _refresh_firmware_label(self) -> None:
        if self._firmware is not None:
            self._firmware_label.setText(
                f'Loaded: {self._loaded_part_name}  '
                f'({len(self._firmware):,} bytes)'
            )
            self._firmware_label.setStyleSheet('color: #2a2; font-weight: bold;')
            self._clear_fw_btn.setEnabled(True)
        else:
            self._firmware_label.setText('— not loaded —')
            self._firmware_label.setStyleSheet('color: #888;')
            self._clear_fw_btn.setEnabled(False)

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
