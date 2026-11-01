"""Preferences dialog for Endgame Gear Mouse Battery Indicator."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from .settings import AppSettings, is_valid_hex_color


class PreferencesDialog(QDialog):
    settings_saved = Signal()

    def __init__(self, app_settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Preferences"))
        self.resize(380, 380)
        self.app_settings = app_settings
        self.temp_fill = app_settings.icon_fill_color
        self.temp_border = app_settings.icon_border_color
        self.temp_charging = app_settings.icon_charging_color
        self._setup_ui()
        self._load_current_settings()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        fill_row = QHBoxLayout()
        self.btn_fill = QPushButton()
        self.btn_fill.setFixedWidth(32)
        self.btn_fill.clicked.connect(lambda: self._pick_color("fill"))
        self.edit_fill = QLineEdit()
        self.edit_fill.setMaxLength(7)
        self.edit_fill.editingFinished.connect(lambda: self._on_hex_edited("fill"))
        fill_row.addWidget(self.btn_fill)
        fill_row.addWidget(self.edit_fill)
        form.addRow(self.tr("Battery fill color:"), fill_row)

        border_row = QHBoxLayout()
        self.btn_border = QPushButton()
        self.btn_border.setFixedWidth(32)
        self.btn_border.clicked.connect(lambda: self._pick_color("border"))
        self.edit_border = QLineEdit()
        self.edit_border.setMaxLength(7)
        self.edit_border.editingFinished.connect(lambda: self._on_hex_edited("border"))
        border_row.addWidget(self.btn_border)
        border_row.addWidget(self.edit_border)
        form.addRow(self.tr("Outline color:"), border_row)

        charging_row = QHBoxLayout()
        self.btn_charging = QPushButton()
        self.btn_charging.setFixedWidth(32)
        self.btn_charging.clicked.connect(lambda: self._pick_color("charging"))
        self.edit_charging = QLineEdit()
        self.edit_charging.setMaxLength(7)
        self.edit_charging.editingFinished.connect(lambda: self._on_hex_edited("charging"))
        charging_row.addWidget(self.btn_charging)
        charging_row.addWidget(self.edit_charging)
        form.addRow(self.tr("Fill color when charging:"), charging_row)

        self.spin_scale = QSpinBox()
        self.spin_scale.setRange(50, 150)
        self.spin_scale.setSuffix(" %")
        form.addRow(self.tr("Icon scale:"), self.spin_scale)

        self.chk_show_text = QCheckBox(self.tr("Show percentage on icon (experimental)"))
        form.addRow("", self.chk_show_text)

        self.spin_poll = QSpinBox()
        self.spin_poll.setRange(30, 3600)
        self.spin_poll.setSingleStep(30)
        self.spin_poll.setSuffix(self.tr(" s"))
        self.spin_poll.setToolTip(self.tr("How often to poll battery and mouse settings (default 600 s)."))
        form.addRow(self.tr("Refresh status interval:"), self.spin_poll)

        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(5, 50)
        self.spin_threshold.setSuffix(" %")
        form.addRow(self.tr("Low battery alert:"), self.spin_threshold)

        self._disconnected_values = ["empty", "error", "hide"]
        self.combo_disconnected = QComboBox()
        self.combo_disconnected.addItems(
            [
                self.tr("Empty battery"),
                self.tr("Error icon"),
                self.tr("Hide tray icon"),
            ]
        )
        form.addRow(self.tr("When disconnected:"), self.combo_disconnected)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_settings)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_current_settings(self) -> None:
        self.temp_fill = self.app_settings.icon_fill_color
        self.temp_border = self.app_settings.icon_border_color
        self.temp_charging = self.app_settings.icon_charging_color
        self._update_color_buttons()
        self.edit_fill.setText(self.temp_fill)
        self.edit_border.setText(self.temp_border)
        self.edit_charging.setText(self.temp_charging)
        self.spin_scale.setValue(self.app_settings.icon_scale)
        self.chk_show_text.setChecked(self.app_settings.icon_show_text)
        self.spin_poll.setValue(self.app_settings.poll_interval)
        self.spin_threshold.setValue(self.app_settings.notify_threshold)
        idx = self._disconnected_values.index(self.app_settings.disconnected_style)
        self.combo_disconnected.setCurrentIndex(idx)

    def _update_color_buttons(self) -> None:
        for btn, color in (
            (self.btn_fill, self.temp_fill),
            (self.btn_border, self.temp_border),
            (self.btn_charging, self.temp_charging),
        ):
            btn.setStyleSheet(f"background-color: {color}; border: 1px solid #888;")

    def _pick_color(self, which: str) -> None:
        current = {
            "fill": self.temp_fill,
            "border": self.temp_border,
            "charging": self.temp_charging,
        }[which]
        chosen = QColorDialog.getColor(QColor(current), self)
        if not chosen.isValid():
            return
        hex_color = chosen.name(QColor.HexRgb)
        if which == "fill":
            self.temp_fill = hex_color
            self.edit_fill.setText(hex_color)
        elif which == "border":
            self.temp_border = hex_color
            self.edit_border.setText(hex_color)
        else:
            self.temp_charging = hex_color
            self.edit_charging.setText(hex_color)
        self._update_color_buttons()

    def _on_hex_edited(self, which: str) -> None:
        value = {
            "fill": self.edit_fill.text(),
            "border": self.edit_border.text(),
            "charging": self.edit_charging.text(),
        }[which]
        if not is_valid_hex_color(value):
            return
        if which == "fill":
            self.temp_fill = value
        elif which == "border":
            self.temp_border = value
        else:
            self.temp_charging = value
        self._update_color_buttons()

    def _save_settings(self) -> None:
        if is_valid_hex_color(self.temp_fill):
            self.app_settings.icon_fill_color = self.temp_fill
        if is_valid_hex_color(self.temp_border):
            self.app_settings.icon_border_color = self.temp_border
        if is_valid_hex_color(self.temp_charging):
            self.app_settings.icon_charging_color = self.temp_charging
        self.app_settings.icon_scale = self.spin_scale.value()
        self.app_settings.icon_show_text = self.chk_show_text.isChecked()
        self.app_settings.poll_interval = self.spin_poll.value()
        self.app_settings.notify_threshold = self.spin_threshold.value()
        self.app_settings.disconnected_style = self._disconnected_values[
            self.combo_disconnected.currentIndex()
        ]
        self.settings_saved.emit()
        self.accept()

