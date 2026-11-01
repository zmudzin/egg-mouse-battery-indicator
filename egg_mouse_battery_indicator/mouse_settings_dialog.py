"""Dialog for onboard Endgame Gear mouse settings (CPI, Polling, LOD, Power)."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from .protocol import (
    CpiProfile,
    LiftOffDistance,
    LOD_LABELS,
    MouseStatus,
    PollingRate,
    POLLING_RATE_LABELS,
    PowerOffSetting,
    POWER_OFF_LABELS,
    apply_mouse_settings,
    apply_mouse_settings_dict,
    fetch_mouse_status,
)
from .worker import HidController

logger = logging.getLogger(__name__)


class MouseSettingsDialog(QDialog):
    settings_applied = Signal()

    def __init__(
        self,
        hidraw_path: str,
        hid_controller: Optional[HidController] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Endgame Gear Mouse Settings"))
        self.resize(500, 560)
        self.hidraw_path = hidraw_path
        self.hid_controller = hid_controller
        self._initial_status: Optional[MouseStatus] = None
        self._initial_config: Optional[dict] = None
        self._current_task_id: Optional[int] = None

        self._cpi_spins_x: list[QSpinBox] = []
        self._cpi_spins_y: list[QSpinBox] = []
        self._cpi_en_checks: list[QCheckBox] = []

        self._setup_ui()
        self._start_loading()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        # Status row with retry button
        status_row = QHBoxLayout()
        self.lbl_status = QLabel(self.tr("Reading mouse settings..."))
        self.btn_retry = QPushButton(self.tr("Retry"))
        self.btn_retry.setVisible(False)
        self.btn_retry.clicked.connect(self._start_loading)
        status_row.addWidget(self.lbl_status)
        status_row.addStretch()
        status_row.addWidget(self.btn_retry)
        main_layout.addLayout(status_row)

        # 1. CPI Profiles Group
        self.grp_cpi = QGroupBox(self.tr("CPI / DPI Profiles"))
        cpi_layout = QVBoxLayout(self.grp_cpi)

        act_row = QHBoxLayout()
        act_row.addWidget(QLabel(self.tr("Active profile:")))
        self.combo_active_cpi = QComboBox()
        for i in range(1, 5):
            self.combo_active_cpi.addItem(self.tr("Profile ") + str(i), i)
        act_row.addWidget(self.combo_active_cpi)
        act_row.addStretch()
        cpi_layout.addLayout(act_row)

        for i in range(4):
            row = QHBoxLayout()
            chk = QCheckBox(f"P{i+1}")
            chk.setChecked(True)
            self._cpi_en_checks.append(chk)
            row.addWidget(chk)

            row.addWidget(QLabel("X:"))
            spin_x = QSpinBox()
            spin_x.setRange(50, 30000)
            spin_x.setSingleStep(50)
            spin_x.setValue(800)
            self._cpi_spins_x.append(spin_x)
            row.addWidget(spin_x)

            row.addWidget(QLabel("Y:"))
            spin_y = QSpinBox()
            spin_y.setRange(50, 30000)
            spin_y.setSingleStep(50)
            spin_y.setValue(800)
            self._cpi_spins_y.append(spin_y)
            row.addWidget(spin_y)

            # Link X/Y by default
            def make_link_handler(sx, sy):
                return lambda val: sy.setValue(val) if sy.value() == sx.value() or abs(sy.value() - val) <= 50 else None
            spin_x.valueChanged.connect(make_link_handler(spin_x, spin_y))

            cpi_layout.addLayout(row)

        main_layout.addWidget(self.grp_cpi)

        # 2. Performance Group
        self.grp_perf = QGroupBox(self.tr("Performance & Sensor"))
        perf_layout = QFormLayout(self.grp_perf)

        self.combo_polling = QComboBox()
        for rate, label in POLLING_RATE_LABELS.items():
            self.combo_polling.addItem(label, rate)
        perf_layout.addRow(self.tr("Polling rate:"), self.combo_polling)

        self.combo_lod = QComboBox()
        for lod, label in LOD_LABELS.items():
            self.combo_lod.addItem(label, lod)
        perf_layout.addRow(self.tr("Lift-off distance (LOD):"), self.combo_lod)

        self.chk_motion_sync = QCheckBox(self.tr("Motion Sync (sync sensor reports with USB polling)"))
        perf_layout.addRow("", self.chk_motion_sync)

        self.chk_ripple = QCheckBox(self.tr("Ripple Control (high-DPI jitter smoothing)"))
        perf_layout.addRow("", self.chk_ripple)

        self.chk_angle_snap = QCheckBox(self.tr("Angle Snapping (straight line correction)"))
        perf_layout.addRow("", self.chk_angle_snap)

        main_layout.addWidget(self.grp_perf)

        # 3. Power Management Group
        self.grp_power = QGroupBox(self.tr("Power Management"))
        power_layout = QFormLayout(self.grp_power)

        self.combo_power_saving = QComboBox()
        for opt, label in POWER_OFF_LABELS.items():
            self.combo_power_saving.addItem(label, opt)
        power_layout.addRow(self.tr("Standby / Power save timer:"), self.combo_power_saving)

        self.combo_deep_sleep = QComboBox()
        for opt, label in POWER_OFF_LABELS.items():
            self.combo_deep_sleep.addItem(label, opt)
        power_layout.addRow(self.tr("Deep sleep timer:"), self.combo_deep_sleep)

        main_layout.addWidget(self.grp_power)

        # Dialog Buttons
        self.btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel
        )
        self.btn_box.button(QDialogButtonBox.Ok).clicked.connect(self._on_ok)
        self.btn_box.button(QDialogButtonBox.Apply).clicked.connect(self._on_apply)
        self.btn_box.rejected.connect(self.reject)
        main_layout.addWidget(self.btn_box)

        self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.grp_cpi.setEnabled(enabled)
        self.grp_perf.setEnabled(enabled)
        self.grp_power.setEnabled(enabled)
        self.btn_box.button(QDialogButtonBox.Ok).setEnabled(enabled)
        self.btn_box.button(QDialogButtonBox.Apply).setEnabled(enabled)

    def _start_loading(self) -> None:
        self.btn_retry.setVisible(False)
        self._set_controls_enabled(False)
        self.lbl_status.setText(self.tr("Reading mouse settings..."))

        if self.hid_controller:
            self._current_task_id = self.hid_controller.submit(
                0,
                fetch_mouse_status,
                self.hidraw_path,
                callback=self._on_fetch_callback,
            )
        else:
            try:
                res = fetch_mouse_status(self.hidraw_path)
                self._on_fetch_callback(res, None)
            except Exception as e:
                self._on_fetch_callback(None, e)

    def _on_fetch_callback(self, res: Any, err: Optional[Exception]) -> None:
        self._current_task_id = None
        if err is not None:
            self._on_loaded_err(str(err))
        elif isinstance(res, MouseStatus):
            if not res.settings_ok:
                msg = res.error or self.tr("Unable to read mouse settings. If mouse is asleep, move it to wake it up.")
                self._on_loaded_err(msg)
            else:
                self._on_loaded_ok(res)
        else:
            self._on_loaded_err(str(res))

    def _on_loaded_ok(self, status: MouseStatus) -> None:
        self.btn_retry.setVisible(False)
        self._initial_status = status
        if status.status == "asleep":
            self.lbl_status.setText(
                self.tr("Connected (mouse asleep): ") + status.device_name + self.tr(" - move mouse to wake")
            )
        else:
            batt = f" (Battery: {status.level}%)" if status.level >= 0 else ""
            self.lbl_status.setText(self.tr("Connected: ") + status.device_name + batt)

        # Active CPI
        act_idx = self.combo_active_cpi.findData(status.active_cpi_profile)
        if act_idx >= 0:
            self.combo_active_cpi.setCurrentIndex(act_idx)

        # Profiles
        for i, prof in enumerate(status.cpi_profiles):
            if i < len(self._cpi_spins_x):
                self._cpi_en_checks[i].setChecked(prof.enabled)
                self._cpi_spins_x[i].setValue(prof.x)
                self._cpi_spins_y[i].setValue(prof.y)

        # Polling rate
        poll_idx = self.combo_polling.findData(status.polling_rate)
        if poll_idx >= 0:
            self.combo_polling.setCurrentIndex(poll_idx)

        # LOD
        lod_idx = self.combo_lod.findData(status.lift_off)
        if lod_idx >= 0:
            self.combo_lod.setCurrentIndex(lod_idx)

        # Flags
        self.chk_motion_sync.setChecked(status.motion_sync)
        self.chk_ripple.setChecked(status.ripple_control)
        self.chk_angle_snap.setChecked(status.angle_snapping)

        # Power
        psave_idx = self.combo_power_saving.findData(status.power_saving)
        if psave_idx >= 0:
            self.combo_power_saving.setCurrentIndex(psave_idx)

        dsleep_idx = self.combo_deep_sleep.findData(status.deep_sleep)
        if dsleep_idx >= 0:
            self.combo_deep_sleep.setCurrentIndex(dsleep_idx)

        self._set_controls_enabled(True)
        self._initial_config = self._collect_changes()

    def _on_loaded_err(self, err_msg: str) -> None:
        self.btn_retry.setVisible(True)
        self.lbl_status.setText(self.tr("Error: ") + err_msg)
        QMessageBox.warning(
            self,
            self.tr("Settings Error"),
            self.tr("Unable to load mouse settings:\n{0}\n\nIf the mouse is asleep, move it to wake it up and click 'Retry'.").format(err_msg),
        )

    def _collect_changes(self) -> dict:
        profs = []
        for i in range(4):
            en = self._cpi_en_checks[i].isChecked()
            x = self._cpi_spins_x[i].value()
            y = self._cpi_spins_y[i].value()
            profs.append(CpiProfile(x=x, y=y, enabled=en))

        return {
            "active_profile": self.combo_active_cpi.currentData(),
            "profiles": profs,
            "polling_rate": self.combo_polling.currentData(),
            "lift_off": self.combo_lod.currentData(),
            "motion_sync": self.chk_motion_sync.isChecked(),
            "ripple_control": self.chk_ripple.isChecked(),
            "angle_snapping": self.chk_angle_snap.isChecked(),
            "power_saving": self.combo_power_saving.currentData(),
            "deep_sleep": self.combo_deep_sleep.currentData(),
        }

    def _on_apply(self) -> None:
        changes = self._collect_changes()
        if self._initial_config is not None and changes == self._initial_config:
            logger.info("MouseSettingsDialog Apply clicked with no changes.")
            self.lbl_status.setText(self.tr("Settings applied successfully (no changes)."))
            return

        self._apply_settings_internal(changes, close_on_success=False)

    def _on_ok(self) -> None:
        changes = self._collect_changes()
        if self._initial_config is not None and changes == self._initial_config:
            logger.info("MouseSettingsDialog OK clicked with no changes; accepting immediately.")
            self.accept()
            return

        self._apply_settings_internal(changes, close_on_success=True)

    def _apply_settings_internal(self, changes: dict, close_on_success: bool) -> None:
        self._set_controls_enabled(False)
        self.lbl_status.setText(self.tr("Applying settings to mouse..."))

        def on_done(res: Any, err: Optional[Exception]) -> None:
            self._current_task_id = None
            if err is not None:
                self._on_applied_err(str(err))
            elif res:  # apply_mouse_settings returns error string or None
                self._on_applied_err(str(res))
            else:
                self._initial_config = changes
                if close_on_success:
                    self._on_ok_applied()
                else:
                    self._on_applied_ok()

        if self.hid_controller:
            self._current_task_id = self.hid_controller.submit(
                0,
                apply_mouse_settings_dict,
                self.hidraw_path,
                changes,
                callback=on_done,
            )
        else:
            try:
                res = apply_mouse_settings_dict(self.hidraw_path, changes)
                on_done(res, None)
            except Exception as e:
                on_done(None, e)

    def closeEvent(self, event) -> None:
        if self._current_task_id is not None and self.hid_controller:
            self.hid_controller.cancel_callback(self._current_task_id)
            self._current_task_id = None
        super().closeEvent(event)

    def reject(self) -> None:
        if self._current_task_id is not None and self.hid_controller:
            self.hid_controller.cancel_callback(self._current_task_id)
            self._current_task_id = None
        super().reject()

    def _on_applied_ok(self) -> None:
        self.lbl_status.setText(self.tr("Settings applied successfully."))
        self._set_controls_enabled(True)
        self.settings_applied.emit()

    def _on_ok_applied(self) -> None:
        self.settings_applied.emit()
        self.accept()

    def _on_applied_err(self, err_msg: str) -> None:
        self.lbl_status.setText(self.tr("Error: ") + err_msg)
        self._set_controls_enabled(True)
        QMessageBox.critical(
            self,
            self.tr("Apply Error"),
            self.tr("Failed to apply settings to mouse:\n") + err_msg,
        )
