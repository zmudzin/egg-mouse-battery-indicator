"""Tabbed dialog for full Endgame Gear mouse configuration (v1 & v2)."""

from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .protocol import (
    ActionType,
    BUTTON_FILTER_NAMES,
    BUTTON_TABLE_NAMES,
    ButtonEntry,
    CONSUMER_ACTIONS,
    CPI_STAGE_COLORS,
    CPI_STAGE_COLOR_NAMES,
    CpiProfile,
    HID_KEY_NAMES,
    LodEncoding,
    MODEL_SPECS,
    MOUSE_BUTTON_ACTIONS,
    ModelSpec,
    MouseStatus,
    POLLING_RATE_LABELS,
    PollingRate,
    SPDT_GX_SAFE,
    SPDT_GX_SPEED,
    UNKNOWN_MODEL,
    apply_mouse_settings_dict,
    effective_lod_encoding,
    factory_reset,
    fetch_mouse_status,
    is_left_handed,
    lod_convert_for_glass_mode,
    lod_options,
    pair_dongle,
    polling_options_for_model,
    quantize_cpi,
    set_left_handed,
)
from .worker import HidController

logger = logging.getLogger(__name__)

# Qt.Key -> USB HID Keyboard Usage ID
_QT_TO_HID_KEY: dict[int, int] = {
    **{getattr(Qt.Key, f"Key_{chr(ord('A') + i)}"): 0x04 + i for i in range(26)},
    Qt.Key.Key_1: 0x1E,
    Qt.Key.Key_2: 0x1F,
    Qt.Key.Key_3: 0x20,
    Qt.Key.Key_4: 0x21,
    Qt.Key.Key_5: 0x22,
    Qt.Key.Key_6: 0x23,
    Qt.Key.Key_7: 0x24,
    Qt.Key.Key_8: 0x25,
    Qt.Key.Key_9: 0x26,
    Qt.Key.Key_0: 0x27,
    Qt.Key.Key_Return: 0x28,
    Qt.Key.Key_Enter: 0x58,
    Qt.Key.Key_Escape: 0x29,
    Qt.Key.Key_Backspace: 0x2A,
    Qt.Key.Key_Tab: 0x2B,
    Qt.Key.Key_Space: 0x2C,
    Qt.Key.Key_Minus: 0x2D,
    Qt.Key.Key_Equal: 0x2E,
    Qt.Key.Key_BracketLeft: 0x2F,
    Qt.Key.Key_BracketRight: 0x30,
    Qt.Key.Key_Backslash: 0x31,
    Qt.Key.Key_Semicolon: 0x33,
    Qt.Key.Key_Apostrophe: 0x34,
    Qt.Key.Key_QuoteLeft: 0x35,
    Qt.Key.Key_Comma: 0x36,
    Qt.Key.Key_Period: 0x37,
    Qt.Key.Key_Slash: 0x38,
    Qt.Key.Key_CapsLock: 0x39,
    **{getattr(Qt.Key, f"Key_F{i + 1}"): 0x3A + i for i in range(12)},
    Qt.Key.Key_Print: 0x46,
    Qt.Key.Key_ScrollLock: 0x47,
    Qt.Key.Key_Pause: 0x48,
    Qt.Key.Key_Insert: 0x49,
    Qt.Key.Key_Home: 0x4A,
    Qt.Key.Key_PageUp: 0x4B,
    Qt.Key.Key_Delete: 0x4C,
    Qt.Key.Key_End: 0x4D,
    Qt.Key.Key_PageDown: 0x4E,
    Qt.Key.Key_Right: 0x4F,
    Qt.Key.Key_Left: 0x50,
    Qt.Key.Key_Down: 0x51,
    Qt.Key.Key_Up: 0x52,
    Qt.Key.Key_NumLock: 0x53,
    **{getattr(Qt.Key, f"Key_F{i + 13}"): 0x68 + i for i in range(12)},
}

# Standard preset button actions in UI order
_PRESET_ACTIONS: list[tuple[str, int, int]] = [
    ("Left Click", ActionType.MOUSE_BUTTON, 0x01),
    ("Right Click", ActionType.MOUSE_BUTTON, 0x02),
    ("Middle Click", ActionType.MOUSE_BUTTON, 0x04),
    ("Back", ActionType.MOUSE_BUTTON, 0x08),
    ("Forward", ActionType.MOUSE_BUTTON, 0x10),
    ("Scroll Up", ActionType.WHEEL, 0x01),
    ("Scroll Down", ActionType.WHEEL, 0xFF),
    ("CPI Loop", ActionType.SPECIAL, 0xF1),
    ("Play / Pause", ActionType.CONSUMER, 0xCD),
    ("Next Track", ActionType.CONSUMER, 0xB5),
    ("Previous Track", ActionType.CONSUMER, 0xB6),
    ("Mute", ActionType.CONSUMER, 0xE2),
    ("Volume Up", ActionType.CONSUMER, 0xE9),
    ("Volume Down", ActionType.CONSUMER, 0xEA),
    ("Browser", ActionType.CONSUMER, 0x96),
    ("File Explorer", ActionType.CONSUMER, 0x94),
    ("Disable", ActionType.DISABLED, 0x00),
]

_SENTINEL_FIXED_CPI = ("__fixed_cpi__",)
_SENTINEL_KEYBOARD = ("__keyboard__",)
_SENTINEL_CUSTOM = ("__custom__",)


class _KeyCaptureLineEdit(QLineEdit):
    key_captured = Signal(int, int)  # (hid_key, qt_mod_mask)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText(self.tr("Click here and press a key…"))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (
            Qt.Key.Key_Control,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Alt,
            Qt.Key.Key_AltGr,
            Qt.Key.Key_Meta,
            Qt.Key.Key_Super_L,
            Qt.Key.Key_Super_R,
        ):
            event.accept()
            return

        hid_key = _QT_TO_HID_KEY.get(key)
        if hid_key is None:
            event.ignore()
            return

        mods = event.modifiers()
        mod_mask = 0
        if mods & Qt.KeyboardModifier.ControlModifier:
            mod_mask |= 0x01
        if mods & Qt.KeyboardModifier.ShiftModifier:
            mod_mask |= 0x02
        if mods & Qt.KeyboardModifier.AltModifier:
            mod_mask |= 0x04
        if mods & Qt.KeyboardModifier.MetaModifier:
            mod_mask |= 0x08

        self.key_captured.emit(hid_key, mod_mask)
        event.accept()


class _KeyboardShortcutDialog(QDialog):
    """Dialog to bind a mouse button to a USB HID key + modifiers."""

    def __init__(
        self,
        initial_mods: int = 0,
        initial_key: int = 0x04,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Keyboard Key Binding"))
        self.resize(360, 220)

        layout = QVBoxLayout(self)

        self.capture = _KeyCaptureLineEdit()
        self.capture.key_captured.connect(self._on_captured)
        layout.addWidget(QLabel(self.tr("Press a key combination or choose a key below:")))
        layout.addWidget(self.capture)

        mod_row = QHBoxLayout()
        self.chk_ctrl = QCheckBox("CTRL")
        self.chk_shift = QCheckBox("SHIFT")
        self.chk_alt = QCheckBox("ALT")
        self.chk_win = QCheckBox("WIN")
        self.chk_ctrl.setChecked(bool(initial_mods & 0x01))
        self.chk_shift.setChecked(bool(initial_mods & 0x02))
        self.chk_alt.setChecked(bool(initial_mods & 0x04))
        self.chk_win.setChecked(bool(initial_mods & 0x08))
        for chk in (self.chk_ctrl, self.chk_shift, self.chk_alt, self.chk_win):
            mod_row.addWidget(chk)
        layout.addLayout(mod_row)

        key_form = QFormLayout()
        self.combo_key = QComboBox()
        for hid_code, label in sorted(HID_KEY_NAMES.items(), key=lambda kv: kv[0]):
            self.combo_key.addItem(f"{label} (0x{hid_code:02X})", hid_code)
        idx = self.combo_key.findData(initial_key if initial_key else 0x04)
        if idx >= 0:
            self.combo_key.setCurrentIndex(idx)
        key_form.addRow(self.tr("Key:"), self.combo_key)
        layout.addLayout(key_form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_preview()
        self.combo_key.currentIndexChanged.connect(lambda _: self._sync_preview())
        for chk in (self.chk_ctrl, self.chk_shift, self.chk_alt, self.chk_win):
            chk.toggled.connect(lambda _: self._sync_preview())

    def _on_captured(self, hid_key: int, mod_mask: int) -> None:
        self.chk_ctrl.setChecked(bool(mod_mask & 0x01))
        self.chk_shift.setChecked(bool(mod_mask & 0x02))
        self.chk_alt.setChecked(bool(mod_mask & 0x04))
        self.chk_win.setChecked(bool(mod_mask & 0x08))
        idx = self.combo_key.findData(hid_key)
        if idx >= 0:
            self.combo_key.setCurrentIndex(idx)
        self._sync_preview()

    def _sync_preview(self) -> None:
        entry = ButtonEntry()
        entry.set_keyboard(self.modifiers(), self.hid_key())
        self.capture.setText(entry.describe())

    def modifiers(self) -> int:
        mask = 0
        if self.chk_ctrl.isChecked():
            mask |= 0x01
        if self.chk_shift.isChecked():
            mask |= 0x02
        if self.chk_alt.isChecked():
            mask |= 0x04
        if self.chk_win.isChecked():
            mask |= 0x08
        return mask

    def hid_key(self) -> int:
        return int(self.combo_key.currentData() or 0x04)


class _FixedCpiDialog(QDialog):
    """Dialog to configure a Fixed CPI button binding (X / Y)."""

    def __init__(
        self,
        model: ModelSpec,
        initial_x: int = 800,
        initial_y: int = 800,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Fixed CPI"))
        self.model = model

        layout = QVBoxLayout(self)
        self.chk_split = QCheckBox(self.tr("Independent X / Y CPI"))
        self.chk_split.setChecked(initial_x != initial_y)
        layout.addWidget(self.chk_split)

        form = QFormLayout()
        self.spin_x = QSpinBox()
        self.spin_x.setRange(model.cpi_min, model.cpi_max)
        self.spin_x.setSingleStep(model.cpi_step)
        self.spin_x.setValue(max(model.cpi_min, min(model.cpi_max, initial_x)))
        form.addRow("X:", self.spin_x)

        self.spin_y = QSpinBox()
        self.spin_y.setRange(model.cpi_min, model.cpi_max)
        self.spin_y.setSingleStep(model.cpi_step)
        self.spin_y.setValue(max(model.cpi_min, min(model.cpi_max, initial_y)))
        self.spin_y.setEnabled(self.chk_split.isChecked())
        form.addRow("Y:", self.spin_y)
        layout.addLayout(form)

        def on_x_changed(val: int) -> None:
            if model.is_v2:
                self.spin_x.setSingleStep(10 if val <= 10000 else 50)
            if not self.chk_split.isChecked():
                self.spin_y.setValue(val)

        def on_split_toggled(on: bool) -> None:
            self.spin_y.setEnabled(on)
            if not on:
                self.spin_y.setValue(self.spin_x.value())

        self.spin_x.valueChanged.connect(on_x_changed)
        self.chk_split.toggled.connect(on_split_toggled)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[int, int]:
        x = quantize_cpi(self.spin_x.value(), self.model)
        y = quantize_cpi(self.spin_y.value(), self.model) if self.chk_split.isChecked() else x
        return x, y


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
        self.resize(620, 620)
        self.hidraw_path = hidraw_path
        self.hid_controller = hid_controller
        self._initial_status: Optional[MouseStatus] = None
        self._initial_config: Optional[dict] = None
        self._current_task_id: Optional[int] = None
        self._populating = False
        self._last_glass_mode = False
        self._working_buttons: list[ButtonEntry] = []

        self._cpi_radios: list[QRadioButton] = []
        self._cpi_spins_x: list[QSpinBox] = []
        self._cpi_spins_y: list[QSpinBox] = []
        self._btn_filter_spins: list[QSpinBox] = []
        self._spdt_combos: dict[int, QComboBox] = {}
        self._button_combos: list[QComboBox] = []
        self._button_row_labels: list[QLabel] = []

        self._setup_ui()
        self._start_loading()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        # Top device status / firmware info row
        info_box = QGroupBox(self.tr("Device Info"))
        info_grid = QGridLayout(info_box)

        self.lbl_status = QLabel(self.tr("Reading mouse settings…"))
        self.lbl_status.setWordWrap(True)
        self.lbl_conn = QLabel("—")
        self.lbl_battery = QLabel("—")
        self.lbl_mouse_fw = QLabel("—")
        self.lbl_dongle_fw = QLabel("—")

        self.btn_reload = QPushButton(self.tr("Reload"))
        self.btn_reload.clicked.connect(self._start_loading)

        info_grid.addWidget(self.lbl_status, 0, 0, 1, 4)
        info_grid.addWidget(self.btn_reload, 0, 4)
        info_grid.addWidget(QLabel(self.tr("Connection:")), 1, 0)
        info_grid.addWidget(self.lbl_conn, 1, 1)
        info_grid.addWidget(QLabel(self.tr("Mouse FW:")), 1, 2)
        info_grid.addWidget(self.lbl_mouse_fw, 1, 3)
        info_grid.addWidget(QLabel(self.tr("Battery:")), 2, 0)
        info_grid.addWidget(self.lbl_battery, 2, 1)
        info_grid.addWidget(QLabel(self.tr("Dongle FW:")), 2, 2)
        info_grid.addWidget(self.lbl_dongle_fw, 2, 3)
        info_grid.setColumnStretch(1, 1)
        info_grid.setColumnStretch(3, 1)
        main_layout.addWidget(info_box)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_basic_tab(), self.tr("Basic Settings"))
        self.tabs.addTab(self._build_advanced_tab(), self.tr("Advanced Settings"))
        self.tabs.addTab(self._build_buttons_tab(), self.tr("Button Mapping"))
        self.tabs.addTab(self._build_power_device_tab(), self.tr("Power & Device"))
        main_layout.addWidget(self.tabs, 1)

        # Dialog buttons
        self.btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel
        )
        self.btn_box.button(QDialogButtonBox.Ok).clicked.connect(self._on_ok)
        self.btn_box.button(QDialogButtonBox.Apply).clicked.connect(self._on_apply)
        self.btn_box.rejected.connect(self.reject)
        main_layout.addWidget(self.btn_box)

        self._set_controls_enabled(False)

    def _build_basic_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        # CPI section
        self.grp_cpi = QGroupBox(self.tr("CPI / DPI Stages"))
        cpi_vlayout = QVBoxLayout(self.grp_cpi)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel(self.tr("CPI Levels:")))
        self.combo_cpi_levels = QComboBox()
        for n in range(1, 5):
            self.combo_cpi_levels.addItem(str(n), n)
        self.combo_cpi_levels.currentIndexChanged.connect(self._on_cpi_levels_changed)
        top_row.addWidget(self.combo_cpi_levels)

        top_row.addSpacing(16)
        self.chk_split_xy = QCheckBox(self.tr("Independent X / Y CPI"))
        self.chk_split_xy.toggled.connect(self._on_split_xy_toggled)
        top_row.addWidget(self.chk_split_xy)
        top_row.addStretch()
        cpi_vlayout.addLayout(top_row)

        grid = QGridLayout()
        self._cpi_stage_group = QButtonGroup(self)
        self._cpi_stage_group.setExclusive(True)

        for i in range(4):
            swatch = QLabel()
            swatch.setFixedSize(14, 14)
            swatch.setStyleSheet(
                f"background-color: {CPI_STAGE_COLORS[i]}; border: 1px solid #666; border-radius: 2px;"
            )
            swatch.setToolTip(
                self.tr("Underside LED glows {color} for CPI {n}").format(
                    color=CPI_STAGE_COLOR_NAMES[i], n=i + 1
                )
            )

            radio = QRadioButton(f"CPI {i + 1}")
            self._cpi_stage_group.addButton(radio, i + 1)
            self._cpi_radios.append(radio)

            spin_x = QSpinBox()
            spin_x.setRange(10, 30000)
            spin_x.setSingleStep(50)
            spin_x.setValue(800)
            self._cpi_spins_x.append(spin_x)

            spin_y = QSpinBox()
            spin_y.setRange(10, 30000)
            spin_y.setSingleStep(50)
            spin_y.setValue(800)
            self._cpi_spins_y.append(spin_y)

            def make_x_handler(idx: int) -> Any:
                def on_val(val: int) -> None:
                    model = self._current_model()
                    if model.is_v2:
                        self._cpi_spins_x[idx].setSingleStep(10 if val <= 10000 else 50)
                    if not self._populating and not self.chk_split_xy.isChecked():
                        self._cpi_spins_y[idx].setValue(val)
                return on_val

            spin_x.valueChanged.connect(make_x_handler(i))

            grid.addWidget(swatch, i, 0)
            grid.addWidget(radio, i, 1)
            grid.addWidget(QLabel("X:"), i, 2)
            grid.addWidget(spin_x, i, 3)
            grid.addWidget(QLabel("Y:"), i, 4)
            grid.addWidget(spin_y, i, 5)

        cpi_vlayout.addLayout(grid)

        self.lbl_cpi_v1_hint = QLabel(
            self.tr("Note: v1 models switch active CPI stage via the button under the mouse (or a 'CPI Loop' binding).")
        )
        self.lbl_cpi_v1_hint.setWordWrap(True)
        self.lbl_cpi_v1_hint.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_cpi_v1_hint.setVisible(False)
        cpi_vlayout.addWidget(self.lbl_cpi_v1_hint)

        layout.addWidget(self.grp_cpi)

        # Sensor section
        self.grp_sensor = QGroupBox(self.tr("Sensor"))
        sensor_form = QFormLayout(self.grp_sensor)

        self.combo_lod = QComboBox()
        sensor_form.addRow(self.tr("Lift-off distance (LOD):"), self.combo_lod)

        self.chk_angle_snap = QCheckBox(self.tr("Angle Snapping (straight line correction)"))
        sensor_form.addRow("", self.chk_angle_snap)

        self.chk_ripple = QCheckBox(self.tr("Ripple Control (high-CPI jitter smoothing)"))
        sensor_form.addRow("", self.chk_ripple)

        self.chk_disable_led_liftoff = QCheckBox(self.tr("Disable LED on Lift-Off"))
        sensor_form.addRow("", self.chk_disable_led_liftoff)

        layout.addWidget(self.grp_sensor)
        layout.addStretch()
        return page

    def _build_advanced_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.grp_perf = QGroupBox(self.tr("Polling & Sensor Tuning"))
        perf_form = QFormLayout(self.grp_perf)

        self.combo_polling = QComboBox()
        perf_form.addRow(self.tr("Polling rate:"), self.combo_polling)

        self.chk_motion_sync = QCheckBox(self.tr("Motion Sync (sync sensor SPI reads with USB polling)"))
        perf_form.addRow("", self.chk_motion_sync)

        self.chk_slamclick = QCheckBox(self.tr("Slamclick Filter (ignore accidental clicks on pad slam)"))
        perf_form.addRow("", self.chk_slamclick)

        self.chk_motion_jitter = QCheckBox(self.tr("Motion Jitter Filter (v1 only)"))
        perf_form.addRow("", self.chk_motion_jitter)

        self.chk_glass_mode = QCheckBox(self.tr("Sensor Glass Mode (v2 only)"))
        self.chk_glass_mode.toggled.connect(self._on_glass_mode_toggled)
        perf_form.addRow("", self.chk_glass_mode)

        self.chk_force_max_fps = QCheckBox(self.tr("Force max Sensor FPS (v2 only)"))
        perf_form.addRow("", self.chk_force_max_fps)

        angle_row = QHBoxLayout()
        self.slider_angle_tuning = QSlider(Qt.Orientation.Horizontal)
        self.slider_angle_tuning.setRange(-30, 30)
        self.spin_angle_tuning = QSpinBox()
        self.spin_angle_tuning.setRange(-30, 30)
        self.spin_angle_tuning.setSuffix(" °")
        self.slider_angle_tuning.valueChanged.connect(
            lambda v: self.spin_angle_tuning.setValue(v) if self.spin_angle_tuning.value() != v else None
        )
        self.spin_angle_tuning.valueChanged.connect(
            lambda v: self.slider_angle_tuning.setValue(max(-30, min(30, v)))
            if self.slider_angle_tuning.value() != v
            else None
        )
        angle_row.addWidget(self.slider_angle_tuning, 1)
        angle_row.addWidget(self.spin_angle_tuning)
        perf_form.addRow(self.tr("Sensor Angle Tuning (v2 only):"), angle_row)

        layout.addWidget(self.grp_perf)

        # Click filters & SPDT
        self.grp_clicks = QGroupBox(self.tr("Click Filter & Kailh GX SPDT Mode"))
        click_grid = QGridLayout(self.grp_clicks)

        click_note = QLabel(
            self.tr("Multiclick Filter is not a traditional latency slider (recommended default: 8).")
        )
        click_note.setWordWrap(True)
        click_note.setStyleSheet("color: gray; font-size: 11px;")
        click_grid.addWidget(click_note, 0, 0, 1, 4)

        for i, name in enumerate(BUTTON_FILTER_NAMES):
            row_idx = i + 1
            click_grid.addWidget(QLabel(f"{name}:"), row_idx, 0)

            spin = QSpinBox()
            spin.setRange(1, 15)
            spin.setValue(8)
            spin.setToolTip(self.tr("Multiclick Filter count (1–15)"))
            self._btn_filter_spins.append(spin)
            click_grid.addWidget(spin, row_idx, 1)

            if i < 2:
                click_grid.addWidget(QLabel(self.tr("SPDT:")), row_idx, 2)
                spdt = QComboBox()
                spdt.addItem(self.tr("OFF (Multiclick Filter)"), 0)
                spdt.addItem(self.tr("GX Speed Mode"), SPDT_GX_SPEED)
                spdt.addItem(self.tr("GX Safe Mode"), SPDT_GX_SAFE)

                def make_spdt_handler(btn_idx: int, cmb: QComboBox, sp: QSpinBox) -> Any:
                    def on_changed() -> None:
                        sp.setEnabled(int(cmb.currentData() or 0) == 0)
                    return on_changed

                spdt.currentIndexChanged.connect(make_spdt_handler(i, spdt, spin))
                self._spdt_combos[i] = spdt
                click_grid.addWidget(spdt, row_idx, 3)

        self.chk_multiclick_ack = QCheckBox(
            self.tr("I understand lower Multiclick Filter values don't reduce click latency (v2)")
        )
        click_grid.addWidget(self.chk_multiclick_ack, len(BUTTON_FILTER_NAMES) + 1, 0, 1, 4)

        layout.addWidget(self.grp_clicks)
        layout.addStretch()
        return page

    def _build_buttons_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.chk_left_handed = QCheckBox(self.tr("Left-handed Mode (swap Left and Right clicks)"))
        self.chk_left_handed.toggled.connect(self._on_left_handed_toggled)
        layout.addWidget(self.chk_left_handed)

        self.grp_buttons = QGroupBox(self.tr("Button Assignments"))
        form = QFormLayout(self.grp_buttons)

        for i, name in enumerate(BUTTON_TABLE_NAMES):
            lbl = QLabel(f"{name}:")
            combo = QComboBox()
            combo.activated.connect(lambda idx, b=i: self._on_button_combo_activated(b, idx))
            self._button_row_labels.append(lbl)
            self._button_combos.append(combo)
            form.addRow(lbl, combo)

        layout.addWidget(self.grp_buttons)

        btn_note = QLabel(
            self.tr("Note: The primary click button (Left in right-handed mode, Right in left-handed mode) stays locked to prevent losing mouse click. 'CPI / Mode (Bottom)' is hidden in the Windows tool.")
        )
        btn_note.setWordWrap(True)
        btn_note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(btn_note)
        layout.addStretch()
        return page

    def _build_power_device_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.grp_power = QGroupBox(self.tr("Power Management"))
        power_form = QFormLayout(self.grp_power)

        ps_row = QHBoxLayout()
        self.chk_power_saving = QCheckBox(self.tr("Enable Power Saving after"))
        self.spin_power_saving = QSpinBox()
        self.spin_power_saving.setRange(1, 120)
        self.spin_power_saving.setSuffix(self.tr(" min"))
        self.chk_power_saving.toggled.connect(self.spin_power_saving.setEnabled)
        ps_row.addWidget(self.chk_power_saving)
        ps_row.addWidget(self.spin_power_saving)
        ps_row.addStretch()
        power_form.addRow(self.tr("Standby:"), ps_row)

        ds_row = QHBoxLayout()
        self.chk_deep_sleep = QCheckBox(self.tr("Enable Deep Sleep after"))
        self.spin_deep_sleep = QSpinBox()
        self.spin_deep_sleep.setRange(1, 120)
        self.spin_deep_sleep.setSuffix(self.tr(" min"))
        self.chk_deep_sleep.toggled.connect(self.spin_deep_sleep.setEnabled)
        ds_row.addWidget(self.chk_deep_sleep)
        ds_row.addWidget(self.spin_deep_sleep)
        ds_row.addStretch()
        power_form.addRow(self.tr("Deep sleep:"), ds_row)

        layout.addWidget(self.grp_power)

        self.grp_device_ops = QGroupBox(self.tr("Pairing & Reset"))
        ops_layout = QVBoxLayout(self.grp_device_ops)

        pair_row = QHBoxLayout()
        self.btn_pair = QPushButton(self.tr("Pair Dongle"))
        self.btn_pair.clicked.connect(self._on_pair_dongle)
        pair_row.addWidget(self.btn_pair)
        pair_row.addWidget(
            QLabel(self.tr("Re-establish the 2.4 GHz radio link between mouse and dongle."))
        )
        pair_row.addStretch()
        ops_layout.addLayout(pair_row)

        reset_row = QHBoxLayout()
        self.btn_factory_reset = QPushButton(self.tr("Factory Reset"))
        self.btn_factory_reset.clicked.connect(self._on_factory_reset)
        reset_row.addWidget(self.btn_factory_reset)
        reset_row.addWidget(
            QLabel(self.tr("Restore all onboard CPI, sensor, power, and button settings to defaults."))
        )
        reset_row.addStretch()
        ops_layout.addLayout(reset_row)

        layout.addWidget(self.grp_device_ops)
        layout.addStretch()
        return page

    def _current_model(self) -> ModelSpec:
        if self._initial_status and self._initial_status.mouse_pid in MODEL_SPECS:
            return self._initial_status.model
        return UNKNOWN_MODEL

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.tabs.setEnabled(enabled)
        self.btn_box.button(QDialogButtonBox.Ok).setEnabled(enabled)
        self.btn_box.button(QDialogButtonBox.Apply).setEnabled(enabled)

    def _start_loading(self) -> None:
        self.btn_reload.setEnabled(False)
        self._set_controls_enabled(False)
        self.lbl_status.setText(self.tr("Reading mouse settings…"))

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
        self.btn_reload.setEnabled(True)
        if err is not None:
            self._on_loaded_err(str(err))
        elif isinstance(res, MouseStatus):
            if not res.settings_ok:
                self._update_info_header(res)
                msg = res.error or (
                    self.tr("Mouse is asleep. Move the mouse to wake it up and click 'Reload'.")
                    if res.status == "asleep"
                    else self.tr("Unable to read mouse settings.")
                )
                self._on_loaded_err(msg)
            else:
                self._on_loaded_ok(res)
        else:
            self._on_loaded_err(str(res))

    def _update_info_header(self, status: MouseStatus) -> None:
        if status.status == "asleep":
            self.lbl_conn.setText(self.tr("Asleep"))
            self.lbl_battery.setText("—")
        elif status.is_wired:
            self.lbl_conn.setText(self.tr("Wired (USB-C)"))
            self.lbl_battery.setText(
                self.tr("100% (Full)") if not status.is_charging else self.tr("Charging")
            )
        else:
            self.lbl_conn.setText(self.tr("Wireless (2.4 GHz)"))
            self.lbl_battery.setText(f"{status.level}%" if status.level >= 0 else "—")

        self.lbl_mouse_fw.setText(f"v{status.mouse_fw}" if status.mouse_fw else "—")
        self.lbl_dongle_fw.setText(
            self.tr("N/A (Wired)") if status.is_wired else (f"v{status.dongle_fw}" if status.dongle_fw else "—")
        )

    def _rebuild_lod_combo(self, model: ModelSpec, glass_mode: bool, selected_raw: int) -> None:
        enc = effective_lod_encoding(model, glass_mode)
        self.combo_lod.clear()
        for raw_val, label in lod_options(enc):
            self.combo_lod.addItem(label, raw_val)
        idx = self.combo_lod.findData(selected_raw)
        if idx < 0:
            self.combo_lod.addItem(self.tr("0x{0:02X} (current)").format(selected_raw), selected_raw)
            idx = self.combo_lod.count() - 1
        self.combo_lod.setCurrentIndex(idx)

    def _on_loaded_ok(self, status: MouseStatus) -> None:
        self._populating = True
        try:
            self._initial_status = status
            self._last_glass_mode = status.glass_mode
            self._working_buttons = copy.deepcopy(status.button_entries)
            model = status.model
            identified = status.model_identified

            self._update_info_header(status)
            self.lbl_status.setText(self.tr("Connected: ") + status.device_name)

            # Basic tab: CPI levels & profiles
            lvl_idx = self.combo_cpi_levels.findData(status.cpi_levels)
            if lvl_idx >= 0:
                self.combo_cpi_levels.setCurrentIndex(lvl_idx)

            split = any(p.x != p.y for p in status.cpi_profiles)
            self.chk_split_xy.setChecked(split)

            for i, prof in enumerate(status.cpi_profiles[:4]):
                spin_x = self._cpi_spins_x[i]
                spin_y = self._cpi_spins_y[i]
                spin_x.setRange(min(model.cpi_min, prof.x), max(model.cpi_max, prof.x))
                spin_y.setRange(min(model.cpi_min, prof.y), max(model.cpi_max, prof.y))
                step_x = (10 if prof.x <= 10000 else 50) if model.is_v2 else model.cpi_step
                step_y = (10 if prof.y <= 10000 else 50) if model.is_v2 else model.cpi_step
                spin_x.setSingleStep(step_x)
                spin_y.setSingleStep(step_y)
                spin_x.setValue(prof.x)
                spin_y.setValue(prof.y)

                radio = self._cpi_radios[i]
                radio.setEnabled(identified and model.has_cpi_stage_select)
                radio.setChecked(status.active_cpi_profile == i + 1)

            self.lbl_cpi_v1_hint.setVisible(identified and not model.has_cpi_stage_select)
            self._update_cpi_row_states()

            # LOD & Sensor checkboxes
            self._rebuild_lod_combo(model, status.glass_mode, status.lod_raw)
            self.combo_lod.setEnabled(identified)
            self.chk_angle_snap.setChecked(status.angle_snapping)
            self.chk_ripple.setChecked(status.ripple_control)
            self.chk_disable_led_liftoff.setChecked(not status.led_on_liftoff)

            # Advanced tab: Polling rate
            self.combo_polling.clear()
            for rate, label in polling_options_for_model(model):
                self.combo_polling.addItem(label, int(rate))
            poll_idx = self.combo_polling.findData(int(status.polling_rate))
            if poll_idx < 0:
                fallback = POLLING_RATE_LABELS.get(status.polling_rate, f"0x{int(status.polling_rate):02X}")
                self.combo_polling.addItem(fallback, int(status.polling_rate))
                poll_idx = self.combo_polling.count() - 1
            self.combo_polling.setCurrentIndex(poll_idx)
            self.combo_polling.setEnabled(identified and not status.is_wired)
            self.combo_polling.setToolTip(
                self.tr("Cabled USB-C operates at Full-Speed (1000 Hz max); stored wireless rate is preserved.")
                if status.is_wired
                else ""
            )

            self.chk_motion_sync.setChecked(status.motion_sync)
            self.chk_slamclick.setChecked(status.slamclick_filter)
            self.chk_motion_jitter.setChecked(status.motion_jitter_filter)
            self.chk_motion_jitter.setEnabled(identified and model.has_motion_jitter)

            self.chk_glass_mode.setChecked(status.glass_mode)
            self.chk_glass_mode.setEnabled(identified and model.has_glass_mode)

            self.chk_force_max_fps.setChecked(status.force_max_fps)
            self.chk_force_max_fps.setEnabled(identified and model.has_force_max_fps)

            tuning = status.angle_tuning if model.has_angle_tuning else 0
            self.spin_angle_tuning.setRange(min(-30, tuning), max(30, tuning))
            self.spin_angle_tuning.setValue(tuning)
            self.slider_angle_tuning.setValue(max(-30, min(30, tuning)))
            self.spin_angle_tuning.setEnabled(identified and model.has_angle_tuning)
            self.slider_angle_tuning.setEnabled(identified and model.has_angle_tuning)

            # Per-button click filters & SPDT
            for i in range(5):
                val = status.button_filters[i] if i < len(status.button_filters) else 8
                spin = self._btn_filter_spins[i]
                if i in self._spdt_combos:
                    spdt = self._spdt_combos[i]
                    if val in (SPDT_GX_SPEED, SPDT_GX_SAFE):
                        sidx = spdt.findData(val)
                        spdt.setCurrentIndex(sidx if sidx >= 0 else 0)
                        spin.setRange(1, 15)
                        spin.setValue(8)
                        spin.setEnabled(False)
                    else:
                        spdt.setCurrentIndex(0)
                        spin.setRange(1, max(15, val))
                        spin.setValue(max(1, val))
                        spin.setEnabled(True)
                else:
                    spin.setRange(1, max(15, val))
                    spin.setValue(max(1, val))

            self.chk_multiclick_ack.setChecked(status.multiclick_ack)
            self.chk_multiclick_ack.setEnabled(identified and model.has_multiclick_ack)

            # Button mapping tab
            self.chk_left_handed.setChecked(is_left_handed(self._working_buttons))
            self._populate_button_rows()

            # Power & Device tab
            self.chk_power_saving.setChecked(status.power_saving_enabled)
            self.spin_power_saving.setValue(max(1, min(120, status.power_saving_minutes)))
            self.spin_power_saving.setEnabled(status.power_saving_enabled)

            self.chk_deep_sleep.setChecked(status.deep_sleep_enabled)
            self.spin_deep_sleep.setValue(max(1, min(120, status.deep_sleep_minutes)))
            self.spin_deep_sleep.setEnabled(status.deep_sleep_enabled)

            self.btn_pair.setEnabled(not status.is_wired)

            self._set_controls_enabled(True)
        finally:
            self._populating = False

        self._initial_config = self._collect_changes()

    def _on_loaded_err(self, err_msg: str) -> None:
        self.lbl_status.setText(self.tr("Error: ") + err_msg)
        QMessageBox.warning(
            self,
            self.tr("Settings Error"),
            self.tr("Unable to load mouse settings:\n{0}\n\nIf the mouse is asleep, move it to wake it up and click 'Reload'.").format(err_msg),
        )

    def _update_cpi_row_states(self) -> None:
        levels = int(self.combo_cpi_levels.currentData() or 4)
        split = self.chk_split_xy.isChecked()
        for i in range(4):
            active_in_cycle = i < levels
            self._cpi_spins_x[i].setEnabled(active_in_cycle)
            self._cpi_spins_y[i].setEnabled(active_in_cycle and split)

    def _on_cpi_levels_changed(self) -> None:
        self._update_cpi_row_states()

    def _on_split_xy_toggled(self, on: bool) -> None:
        if not self._populating and not on:
            for i in range(4):
                self._cpi_spins_y[i].setValue(self._cpi_spins_x[i].value())
        self._update_cpi_row_states()

    def _on_glass_mode_toggled(self, on: bool) -> None:
        if self._populating:
            return
        model = self._current_model()
        if not model.has_glass_mode:
            return
        cur_lod = int(self.combo_lod.currentData() or 0)
        converted = lod_convert_for_glass_mode(cur_lod, on)
        self._rebuild_lod_combo(model, on, converted)

    def _on_left_handed_toggled(self, on: bool) -> None:
        if self._populating:
            return
        self._working_buttons = set_left_handed(self._working_buttons, on)
        self._populate_button_rows()

    def _populate_button_rows(self) -> None:
        left_handed = self.chk_left_handed.isChecked()
        primary_idx = 1 if left_handed else 0

        for i in range(min(8, len(self._working_buttons))):
            entry = self._working_buttons[i]
            combo = self._button_combos[i]
            lbl = self._button_row_labels[i]

            is_primary_locked = (
                i == primary_idx
                and entry.action_type == ActionType.MOUSE_BUTTON
                and entry.code == 0x01
            )
            lbl_text = BUTTON_TABLE_NAMES[i]
            if is_primary_locked:
                lbl.setText(f"{lbl_text} (Primary):")
            else:
                lbl.setText(f"{lbl_text}:")

            combo.blockSignals(True)
            combo.clear()

            matched_idx = -1
            for label, atype, acode in _PRESET_ACTIONS:
                combo.addItem(label, (int(atype), int(acode)))
                if (
                    entry.action_type == atype
                    and entry.code == acode
                    and entry.key == 0
                    and entry.rest == b"\x00\x00\x00"
                ):
                    matched_idx = combo.count() - 1

            combo.addItem(self.tr("Fixed CPI…"), _SENTINEL_FIXED_CPI)
            combo.addItem(self.tr("Keyboard Key…"), _SENTINEL_KEYBOARD)

            if matched_idx < 0:
                combo.addItem(entry.describe(), _SENTINEL_CUSTOM)
                matched_idx = combo.count() - 1

            combo.setCurrentIndex(matched_idx)
            combo.setEnabled(not is_primary_locked)
            combo.blockSignals(False)

    def _on_button_combo_activated(self, btn_idx: int, combo_idx: int) -> None:
        if btn_idx >= len(self._working_buttons):
            return
        combo = self._button_combos[btn_idx]
        data = combo.itemData(combo_idx)
        entry = self._working_buttons[btn_idx]

        if data == _SENTINEL_FIXED_CPI:
            cur_x, cur_y = entry.fixed_cpi_xy() if entry.is_fixed_cpi() else (800, 800)
            dlg = _FixedCpiDialog(self._current_model(), cur_x, cur_y, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                x, y = dlg.values()
                entry.set_fixed_cpi(x, y)
            self._populate_button_rows()
            return

        if data == _SENTINEL_KEYBOARD:
            init_mods = entry.code if entry.action_type == ActionType.KEYBOARD else 0
            init_key = entry.key if entry.action_type == ActionType.KEYBOARD else 0x04
            dlg = _KeyboardShortcutDialog(init_mods, init_key, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                entry.set_keyboard(dlg.modifiers(), dlg.hid_key())
            self._populate_button_rows()
            return

        if data == _SENTINEL_CUSTOM:
            return

        if isinstance(data, tuple) and len(data) == 2:
            atype, acode = data
            entry.set_simple_action(atype, acode)
            self._populate_button_rows()

    def _collect_changes(self) -> dict:
        model = self._current_model()
        profs: list[CpiProfile] = []
        levels = int(self.combo_cpi_levels.currentData() or 4)
        split = self.chk_split_xy.isChecked()
        for i in range(4):
            x = quantize_cpi(self._cpi_spins_x[i].value(), model)
            y = quantize_cpi(self._cpi_spins_y[i].value(), model) if split else x
            profs.append(CpiProfile(x=x, y=y, enabled=(i < levels)))

        btn_filters: list[int] = []
        for i in range(5):
            if i in self._spdt_combos and int(self._spdt_combos[i].currentData() or 0) != 0:
                btn_filters.append(int(self._spdt_combos[i].currentData()))
            else:
                btn_filters.append(int(self._btn_filter_spins[i].value()))

        changes: dict[str, Any] = {
            "cpi_levels": levels,
            "profiles": profs,
            "lod_raw": int(self.combo_lod.currentData() or 1),
            "led_on_liftoff": not self.chk_disable_led_liftoff.isChecked(),
            "angle_snapping": self.chk_angle_snap.isChecked(),
            "ripple_control": self.chk_ripple.isChecked(),
            "motion_sync": self.chk_motion_sync.isChecked(),
            "slamclick_filter": self.chk_slamclick.isChecked(),
            "power_saving_enabled": self.chk_power_saving.isChecked(),
            "power_saving_minutes": int(self.spin_power_saving.value()),
            "deep_sleep_enabled": self.chk_deep_sleep.isChecked(),
            "deep_sleep_minutes": int(self.spin_deep_sleep.value()),
            "button_filters": btn_filters,
            "button_entries": copy.deepcopy(self._working_buttons),
        }

        if self._initial_status and not self._initial_status.is_wired:
            changes["polling_rate"] = PollingRate(int(self.combo_polling.currentData()))

        if model.has_cpi_stage_select:
            checked_id = self._cpi_stage_group.checkedId()
            if 1 <= checked_id <= 4:
                changes["active_profile"] = checked_id

        if model.has_motion_jitter:
            changes["motion_jitter_filter"] = self.chk_motion_jitter.isChecked()
        if model.has_glass_mode:
            changes["glass_mode"] = self.chk_glass_mode.isChecked()
        if model.has_force_max_fps:
            changes["force_max_fps"] = self.chk_force_max_fps.isChecked()
        if model.has_angle_tuning:
            changes["angle_tuning"] = int(self.spin_angle_tuning.value())
        if model.has_multiclick_ack:
            changes["multiclick_ack"] = self.chk_multiclick_ack.isChecked()

        return changes

    def _on_apply(self) -> None:
        changes = self._collect_changes()
        if self._initial_config is not None and changes == self._initial_config:
            self.lbl_status.setText(self.tr("Settings applied (no changes)."))
            return
        self._apply_settings_internal(changes, close_on_success=False)

    def _on_ok(self) -> None:
        changes = self._collect_changes()
        if self._initial_config is not None and changes == self._initial_config:
            self.accept()
            return
        self._apply_settings_internal(changes, close_on_success=True)

    def _apply_settings_internal(self, changes: dict, close_on_success: bool) -> None:
        self._set_controls_enabled(False)
        self.btn_reload.setEnabled(False)
        self.lbl_status.setText(self.tr("Applying settings to mouse…"))

        def on_done(res: Any, err: Optional[Exception]) -> None:
            self._current_task_id = None
            self.btn_reload.setEnabled(True)
            if err is not None:
                self._on_applied_err(str(err))
            elif res:
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

    def _on_pair_dongle(self) -> None:
        choice = QMessageBox.question(
            self,
            self.tr("Pair Dongle"),
            self.tr(
                "Re-establish the radio link between the mouse and wireless dongle?\n\n"
                "The mouse will briefly disconnect and reconnect. Your settings are preserved."
            ),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Ok:
            return

        self._set_controls_enabled(False)
        self.btn_reload.setEnabled(False)
        self.lbl_status.setText(self.tr("Sending pairing command…"))

        def on_done(res: Any, err: Optional[Exception]) -> None:
            self._current_task_id = None
            self.btn_reload.setEnabled(True)
            if err is not None or res:
                self._set_controls_enabled(True)
                msg = str(err) if err else str(res)
                self.lbl_status.setText(self.tr("Pair error: ") + msg)
                QMessageBox.warning(self, self.tr("Pair Dongle"), msg)
            else:
                self.lbl_status.setText(self.tr("Pairing initiated — reloading…"))
                self._start_loading()

        if self.hid_controller:
            self._current_task_id = self.hid_controller.submit(
                0, pair_dongle, self.hidraw_path, callback=on_done
            )
        else:
            on_done(pair_dongle(self.hidraw_path), None)

    def _on_factory_reset(self) -> None:
        choice = QMessageBox.warning(
            self,
            self.tr("Factory Reset"),
            self.tr(
                "Reset the mouse to factory defaults?\n\n"
                "All CPI stages, sensor settings, click filters, and button mappings will be erased."
            ),
            QMessageBox.StandardButton.Reset | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Reset:
            return

        self._set_controls_enabled(False)
        self.btn_reload.setEnabled(False)
        self.lbl_status.setText(self.tr("Resetting mouse to factory defaults…"))

        def on_done(res: Any, err: Optional[Exception]) -> None:
            self._current_task_id = None
            self.btn_reload.setEnabled(True)
            if err is not None or res:
                self._set_controls_enabled(True)
                msg = str(err) if err else str(res)
                self.lbl_status.setText(self.tr("Reset error: ") + msg)
                QMessageBox.critical(self, self.tr("Factory Reset"), msg)
            else:
                self.settings_applied.emit()
                self._start_loading()

        if self.hid_controller:
            self._current_task_id = self.hid_controller.submit(
                0, factory_reset, self.hidraw_path, callback=on_done
            )
        else:
            on_done(factory_reset(self.hidraw_path), None)

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
        if self._initial_status:
            self.lbl_status.setText(
                self.tr("Connected: ") + self._initial_status.device_name + self.tr(" — Settings applied.")
            )
        else:
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
