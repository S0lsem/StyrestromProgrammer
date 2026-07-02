"""
Styrestrøm AS PLC Programmer — PyQt6 desktop application.

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
import re

from mrs_protocol import event_logger


_VERSION_NUMBER_RE = re.compile(r'\d+\.\d+(?:\.\d+)?')


def _version_number(text: str) -> str:
    """Pull the bare ``X.Y`` / ``X.Y.Z`` digits out of strings like
    ``'V: 0.17.0'``, ``'----V0.1'``, or ``'0.17.0'``. Returns '' for empty
    or unparseable input."""
    if not text:
        return ''
    m = _VERSION_NUMBER_RE.search(text)
    return m.group(0) if m else ''


def _is_empty_sw(text: str) -> bool:
    """True if *text* indicates an unprogrammed PLC — the bootloader's
    default placeholder (``----V0.1``), the flasher SCAN ``NO PROG``
    status, the ``n/a`` version-field fallback, or an empty string."""
    if not text:
        return True
    t = text.strip()
    return (
        not t
        or 'NO PROG' in t
        or 'n/a' in t.lower()
        or '----V' in t
    )


def _format_sw(text: str) -> str:
    """Return the user-facing SW indicator for the status bar.

    * ``'NO SW installed'`` if the PLC is unprogrammed.
    * ``'SW: X.Y.Z'`` if a version number can be extracted.
    * ``''`` if there is no recognisable SW info at all.
    """
    if _is_empty_sw(text):
        return 'NO SW installed'
    num = _version_number(text)
    return f'SW: {num}' if num else ''
from mrs_protocol.constants import MODULE_TYPES
from mrs_protocol.console_flasher import run_flash
from mrs_protocol.protocol import detect_adapter, scan_plc, ScanError, PartialScanError
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


class _UpdateDownloadWorker(QObject):
    """Downloads the new .exe in a background thread (network + disk I/O)."""
    progress = pyqtSignal(float, str)
    finished = pyqtSignal(str)   # path to the downloaded exe
    error    = pyqtSignal(str)

    def __init__(self, url: str, dest: str) -> None:
        super().__init__()
        self._url  = url
        self._dest = dest

    def run(self) -> None:
        from mrs_protocol.self_update import download_update
        try:
            path = download_update(
                self._url, Path(self._dest),
                progress=lambda f, m: self.progress.emit(f, m),
            )
            self.finished.emit(str(path))
        except Exception as exc:
            self.error.emit(str(exc))


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
    result  = pyqtSignal(object)   # PLCInfo
    partial = pyqtSignal(int, str) # (serial, friendly message) — detected, identity unreadable (CAN FD)
    error   = pyqtSignal(str)

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
        except PartialScanError as exc:
            self.partial.emit(exc.serial, str(exc))
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
        self._update_dl_worker: Optional[_UpdateDownloadWorker] = None
        self._update_dl_thread: Optional[QThread]               = None
        self._pending_update:   Optional[dict]                  = None
        self._last_scan_label: str = ''   # carried into the flash event
        # Once the operator acknowledges the first "Flash complete" popup
        # this session, suppress it on every later flash so batch mode is
        # not interrupted between units. Reports + CSV still get written.
        self._flash_popup_dismissed: bool = False
        # In-app "how to program" guide. Clicking the ? toggles numbered
        # prefixes on the workflow buttons; this dict remembers the
        # original button text so we can restore on toggle-off.
        self._help_active: bool = False
        self._original_btn_text: dict = {}

        self._settings = QSettings('Styrestrom', 'Styrestrom PLC Programmer')
        self._migrate_legacy_settings()

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

        # ── Help (?) button — top-right ────────────────────────────────
        help_row = QHBoxLayout()
        help_row.addStretch()
        self._help_btn = QPushButton('?')
        self._help_btn.setFixedSize(28, 28)
        self._help_btn.setToolTip('HOW TO PROGRAM')
        self._help_btn.setStyleSheet(
            'QPushButton { border-radius: 14px; font-weight: bold; '
            'font-size: 14px; background: #1a7fd4; color: white; }'
            'QPushButton:hover { background: #1565b3; }'
            'QPushButton:checked { background: #2a8; }'
        )
        self._help_btn.setCheckable(True)
        self._help_btn.clicked.connect(self._toggle_help)
        help_row.addWidget(self._help_btn)
        root.addLayout(help_row)

        # ── Update banner (hidden by default) ─────────────────────────
        # A message label plus a one-click "Update & Restart" button. The
        # button downloads the new exe and swaps it in (see _on_update_clicked);
        # the label doubles as a plain GitHub link if no installable asset was
        # published, and as a live progress readout during the download.
        self._update_bar = QWidget()
        self._update_bar.setObjectName('updateBar')
        self._update_bar.setStyleSheet(
            '#updateBar { background: #fff3cd; border: 1px solid #ffc107; '
            'border-radius: 4px; }'
            '#updateBar QLabel { color: #856404; font-size: 12px; }'
            '#updateBar QPushButton { background: #2a8; color: white; border: none; '
            'padding: 5px 14px; border-radius: 4px; font-weight: bold; }'
            '#updateBar QPushButton:hover { background: #2b9; }'
            '#updateBar QPushButton:disabled { background: #9c9; }'
        )
        _update_row = QHBoxLayout(self._update_bar)
        _update_row.setContentsMargins(8, 6, 8, 6)
        self._update_banner = QLabel()
        self._update_banner.setOpenExternalLinks(True)
        _update_row.addWidget(self._update_banner, 1)
        self._update_btn = QPushButton('Update && Restart')
        self._update_btn.clicked.connect(self._on_update_clicked)
        _update_row.addWidget(self._update_btn)
        self._update_bar.setVisible(False)
        root.addWidget(self._update_bar)

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

        self._refresh_btn = QPushButton('Refresh list')
        self._refresh_btn.setFixedWidth(110)
        self._refresh_btn.clicked.connect(self._on_refresh_parts)
        gh_layout.addWidget(self._refresh_btn)

        gh_layout.addWidget(QLabel('Part:'))
        self._part_combo = QComboBox()
        self._part_combo.setMinimumWidth(260)
        self._part_combo.setPlaceholderText('— click Refresh to load list —')
        gh_layout.addWidget(self._part_combo)

        self._download_btn = QPushButton('Download')
        self._download_btn.setFixedWidth(110)
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download_part)
        gh_layout.addWidget(self._download_btn)

        self._scan_btn = QPushButton('Scan PLC')
        self._scan_btn.setFixedWidth(100)
        self._scan_btn.setToolTip(
            'Listen for a PLC boot announcement, then read its identity '
            '(SN, article, app name + version). Power-cycle the PLC after '
            'clicking. Read-only — does not erase or flash.'
        )
        self._scan_btn.clicked.connect(self._on_scan)
        gh_layout.addWidget(self._scan_btn)

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
    # In-app "How to program" guide
    # ------------------------------------------------------------------

    def _toggle_help(self) -> None:
        if self._help_active:
            self._restore_button_text()
            self._help_active = False
        else:
            self._apply_help_numbers()
            self._help_active = True

    def _apply_help_numbers(self) -> None:
        """Prefix each workflow button with its step number 1..6."""
        steps = (
            (self._check_conn_btn, 1, 'Detect adapter'),
            (self._refresh_btn,    2, 'Refresh list'),
            (self._download_btn,   4, 'Download'),
            (self._scan_btn,       5, 'Scan PLC'),
            (self._flash_btn,      6, 'Flash PLC'),
        )
        for btn, num, _label in steps:
            self._original_btn_text[btn] = btn.text()
            btn.setText(f'{num}. {btn.text()}')
        # Step 3 is the part dropdown — no button text, so we update its
        # placeholder.
        self._part_combo.setPlaceholderText(
            '3. — pick a part from the list —'
        )

    def _restore_button_text(self) -> None:
        for btn, original in self._original_btn_text.items():
            btn.setText(original)
        self._original_btn_text.clear()
        self._part_combo.setPlaceholderText(
            '— click Refresh to load list —'
        )

    # ------------------------------------------------------------------
    # Operator identity (persisted via QSettings, posted with every event)
    # ------------------------------------------------------------------

    def _migrate_legacy_settings(self) -> None:
        """Carry distributor + operator over from the old "MRS Programmer"
        QSettings key (v1.0.0–v1.0.2) so the upgraded install does not
        re-prompt operators who already filled in their identity."""
        new_has = (
            self._settings.contains('distributor')
            or self._settings.contains('operator')
        )
        if new_has:
            return
        legacy = QSettings('Styrestrom', 'MRS Programmer')
        for key in ('distributor', 'operator'):
            value = legacy.value(key, '', type=str)
            if value:
                self._settings.setValue(key, value)

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
        if not info.get('update_available'):
            return

        self._pending_update = info
        version = info['latest_version']
        url = info.get('download_url', '')
        if url:
            # One-click self-install path.
            self._update_banner.setText(f'New version <b>{version}</b> available.')
            self._update_btn.setVisible(True)
            self._update_btn.setEnabled(True)
        else:
            # No installable .exe asset — fall back to the GitHub link.
            self._update_banner.setText(
                f'New version <b>{version}</b> available on '
                f'<a href="https://github.com/S0lsem/StyrestromProgrammer/releases/latest">GitHub</a>.'
            )
            self._update_btn.setVisible(False)
        self._update_bar.setVisible(True)
        self._append_log(f'Update available: {version}')

    def _on_update_clicked(self) -> None:
        """Download the new exe and, once it's on disk, swap it in and restart."""
        info = self._pending_update or {}
        url = info.get('download_url', '')
        if not url:
            return

        from mrs_protocol import self_update
        if not self_update.is_frozen():
            QMessageBox.information(
                self, 'Update',
                'Self-install runs only in the packaged .exe.\n\n'
                'In this dev build, download the release manually from GitHub.',
            )
            return

        version = info.get('latest_version', '')
        if QMessageBox.question(
            self, 'Update & Restart',
            f'Download version {version} and restart the programmer now?\n\n'
            'Finish any flash in progress first — the app will close.',
        ) != QMessageBox.StandardButton.Yes:
            return

        # A flash or scan holds exclusive PCAN access; make sure nothing is
        # mid-operation before we tear the process down.
        self._stop_batch_listener()

        self._update_btn.setEnabled(False)
        self._update_banner.setText('Downloading update…')

        dest = str(self_update.default_download_path())
        self._update_dl_worker = _UpdateDownloadWorker(url, dest)
        self._update_dl_thread = QThread(self)
        self._update_dl_worker.moveToThread(self._update_dl_thread)
        self._update_dl_thread.started.connect(self._update_dl_worker.run)
        self._update_dl_worker.progress.connect(self._on_update_progress)
        self._update_dl_worker.finished.connect(self._on_update_downloaded)
        self._update_dl_worker.error.connect(self._on_update_error)
        self._update_dl_worker.finished.connect(self._update_dl_thread.quit)
        self._update_dl_worker.error.connect(self._update_dl_thread.quit)
        self._update_dl_thread.start()

    def _on_update_progress(self, fraction: float, message: str) -> None:
        self._update_banner.setText(f'{message} {int(fraction * 100)}%')

    def _on_update_downloaded(self, path: str) -> None:
        from mrs_protocol import self_update
        self._update_banner.setText('Update downloaded — restarting…')
        self._append_log(f'Update downloaded to {path}; restarting.')
        try:
            self_update.install_and_restart(Path(path))
        except Exception as exc:
            self._on_update_error(str(exc))
            return
        # The helper waits for us to exit before swapping the exe, so quit now.
        QApplication.instance().quit()

    def _on_update_error(self, msg: str) -> None:
        self._update_banner.setText('Update failed — see log.')
        self._update_btn.setEnabled(True)
        self._append_log(f'Update failed: {msg}')
        QMessageBox.warning(self, 'Update failed', msg)

    # ------------------------------------------------------------------
    # Adapter connection check
    # ------------------------------------------------------------------

    def _on_check_connection(self) -> None:
        # Probing PCAN channels while the batch listener holds one makes that
        # channel look busy; release it first (resumed in _on_conn_result).
        self._stop_batch_listener()
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

        # Resume batch listening if it was on (detect_adapter has released all
        # probed channels by now, so the listener can reclaim its channel).
        self._maybe_start_batch_listener()

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

    def _on_flash_thread_finished(self) -> None:
        """Drop Python refs once the flash thread has fully exited.
        Mirrors _on_batch_thread_finished — without it, ~20 rapid batch
        flashes accumulate stale wrapper state and eventually crash Qt.

        This is also where batch mode resumes. The listener needs exclusive
        PCAN access, so _maybe_start_batch_listener() refuses to start while
        the flash thread is still running. _on_flash_done() runs too early
        for that — it fires before _flash_thread.quit() is processed, so the
        guard sees the thread still alive and bails. (That's why batch
        auto-flash used to work exactly once: the first flash's completion
        popup pumped a nested event loop that let the thread finish before
        the restart, accidentally masking the bug.) Restarting here — after
        the thread has genuinely exited and refs are cleared — is reliable."""
        self._flash_worker = None
        self._flash_thread = None
        self._maybe_start_batch_listener()

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
        self._status_label.setText('Starter flasher…')
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
        # Qt-managed cleanup so batch programming doesn't race the Python
        # GC after ~20 rapid flashes. Same pattern as BatchListenerWorker:
        # clear Python refs first (so any subsequent isRunning() check on
        # them harmlessly returns False), then deleteLater the C++ objects.
        self._flash_thread.finished.connect(self._on_flash_thread_finished)
        self._flash_thread.finished.connect(self._flash_worker.deleteLater)
        self._flash_thread.finished.connect(self._flash_thread.deleteLater)

        self._flash_thread.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self._progress_bar.setValue(int(fraction * 100))
        if message:
            self._status_label.setText(message)

    def _on_plc_found(self, info) -> None:
        self._last_plc_info = info
        self._last_scan_label = info.label
        self._append_log(f'PLC detected — SN:{info.serial}  {info.label}')
        # Status line shows SN + the SW currently on the PLC (extracted
        # from the flasher's SCAN line). An unprogrammed PLC's placeholder
        # state renders as "NO SW installed". The new version we're about
        # to write goes into the event/CSV separately via _on_flash_done.
        sw_display = _format_sw(info.label)
        label = f'PLC detected — SN {info.serial}'
        if sw_display:
            label += f'  {sw_display}'
        self._status_label.setText(label)

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

        # Extract the firmware version we just wrote from the parsed .s19
        # bytes. The pre-flash scan label only reflects the empty-PLC defaults
        # (e.g. "Modulname : ----V0.1"), so HQ needs the post-flash version
        # too to know what landed on the unit.
        from mrs_protocol.s19_parser import extract_app_version
        wrote_version = (
            extract_app_version(self._firmware.data) if self._firmware else ''
        )
        if wrote_version:
            self._append_log(f'Firmware version written: {wrote_version}')
            if self._last_scan_label:
                self._last_scan_label = (
                    f'{self._last_scan_label}  →  wrote {wrote_version}'
                )
            else:
                self._last_scan_label = f'wrote {wrote_version}'

        # Write flash log
        from mrs_protocol.flash_log import write_entry
        log_path = write_entry(
            part=part, module=module, channel=channel, success=True, serial=serial,
            sw_version=wrote_version,
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
            sw_version=wrote_version,
        )
        report_path = save_report(report)
        self._append_log(f'Report saved to {report_path}')

        # Show result only on the first successful flash of the session —
        # batch programming is hostile to a popup between every unit.
        if not self._flash_popup_dismissed:
            reply = QMessageBox.information(
                self, 'Flash complete',
                f'PLC flashed successfully.\n\n'
                f'Part: {part}\nModule: {module}\n\n'
                f'Save report to a custom location?\n\n'
                f'(This confirmation will not appear again this session. '
                f'Reports are still saved automatically to '
                f'~/.mrs_programmer/reports/.)',
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Ok,
            )
            self._flash_popup_dismissed = True
            if reply == QMessageBox.StandardButton.Save:
                path, _ = QFileDialog.getSaveFileName(
                    self, 'Save flash report', f'flash_report_{part}.txt',
                    'Text files (*.txt)',
                )
                if path:
                    save_report(report, directory=Path(path).parent)
                    self._append_log(f'Report saved to {path}')

        # Batch mode: keep firmware loaded, or clear it. When firmware stays
        # loaded, the batch listener is (re)started from
        # _on_flash_thread_finished once the flash thread has actually exited
        # — not here, where the thread is still running and the restart guard
        # would bail.
        if not self._batch_check.isChecked():
            self._firmware = None
            self._loaded_part_name = ''
            self._refresh_firmware_label()

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

        # A single bad unit should not abort an in-progress batch run — the
        # batch listener is restarted from _on_flash_thread_finished once the
        # flash thread has exited, so batch mode keeps listening for the next
        # PLC after a failure too.

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

        # Scan opens the PCAN bus directly, so it needs exclusive access —
        # the batch listener holds the same channel and would otherwise cause
        # "A PCAN Channel has not been initialized yet" on can.Bus(). Release
        # it here; it's restarted from _on_scan_thread_finished afterwards.
        self._stop_batch_listener()

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
        self._scan_worker.partial.connect(self._on_scan_partial)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.result.connect(self._scan_thread.quit)
        self._scan_worker.partial.connect(self._scan_thread.quit)
        self._scan_worker.error.connect(self._scan_thread.quit)
        # Clear refs and resume batch listening only once the scan thread has
        # actually exited (its bus is shut down) — mirrors the flash-thread
        # pattern so a Scan during batch mode doesn't kill the auto-flash loop.
        self._scan_thread.finished.connect(self._on_scan_thread_finished)
        self._scan_worker.result.connect(self._scan_worker.deleteLater)
        self._scan_worker.partial.connect(self._scan_worker.deleteLater)
        self._scan_worker.error.connect(self._scan_worker.deleteLater)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)

        self._scan_thread.start()

    def _on_scan_thread_finished(self) -> None:
        """Drop scan refs once the scan thread has exited, then resume batch
        mode if it's still enabled (no-op otherwise)."""
        self._scan_worker = None
        self._scan_thread = None
        self._maybe_start_batch_listener()

    def _on_scan_done(self, info) -> None:
        self._scan_btn.setEnabled(True)
        self._check_conn_btn.setEnabled(True)
        self._flash_btn.setEnabled(True)
        sw_display = _format_sw(info.app_version)
        status = f'PLC found — SN {info.serial}'
        if sw_display:
            status += f'  {sw_display}'
        self._status_label.setText(status)

        app_version_display = (
            'NO SW installed'
            if _is_empty_sw(info.app_version)
            else info.app_version
        )

        self._append_log(f'PLC FOUND — SN: {info.serial}')
        self._append_log(f'  Article:     {info.article}')
        self._append_log(f'  Revision:    {info.revision}')
        self._append_log(f'  App name:    {info.app_name}')
        self._append_log(f'  App version: {app_version_display}')
        if info.description:
            self._append_log(f'  Description: {info.description}')
        self._append_log('Power-cycle the PLC again before clicking Flash.')

    def _on_scan_partial(self, serial: int, msg: str) -> None:
        """The PLC was detected but its identity couldn't be read (CAN FD).
        Not a failure — the unit is present and flashable, so we present this
        as an informational outcome and keep Flash ready to go."""
        self._scan_btn.setEnabled(True)
        self._check_conn_btn.setEnabled(True)
        self._flash_btn.setEnabled(True)
        self._status_label.setText(f'PLC detected — SN {serial} (CAN FD — press Flash)')
        self._append_log(f'PLC DETECTED — SN: {serial}')
        self._append_log(
            '  Full identity not readable (CAN FD module). This is expected — '
            'just press Flash; no Scan is needed for these parts.'
        )
        QMessageBox.information(self, 'PLC detected — ready to flash', msg)

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
