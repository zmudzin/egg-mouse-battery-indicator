"""Persistent application settings for Endgame Gear Mouse Battery Indicator."""

from __future__ import annotations

import logging
import re

from PySide6.QtCore import QObject, QSettings, Signal

logger = logging.getLogger(__name__)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def is_valid_hex_color(value: str) -> bool:
    return bool(_HEX_COLOR_RE.match(str(value)))


class AppSettings(QObject):
    icon_fill_color_changed = Signal(str)
    icon_border_color_changed = Signal(str)
    icon_charging_color_changed = Signal(str)
    icon_scale_changed = Signal(int)
    icon_show_text_changed = Signal(bool)
    poll_interval_changed = Signal(int)
    notify_enabled_changed = Signal(bool)
    notify_threshold_changed = Signal(int)
    disconnected_style_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings()
        self._settings.beginGroup("EggMouseBatteryIndicator")
        self._load_all()

    def _load_all(self) -> None:
        self._icon_fill_color = str(
            self._settings.value("iconFillColor", "#a6e3a1", type=str)
        )
        self._icon_border_color = str(
            self._settings.value("iconBorderColor", "#a6adc8", type=str)
        )
        self._icon_charging_color = str(
            self._settings.value("iconChargingColor", "#89b4fa", type=str)
        )
        self._icon_scale = int(self._settings.value("iconScale", 100, type=int))
        self._icon_show_text = bool(self._settings.value("iconShowText", False, type=bool))
        self._poll_interval = int(self._settings.value("pollInterval", 600, type=int))
        self._notify_enabled = bool(self._settings.value("notifyEnabled", True, type=bool))
        self._notify_threshold = int(self._settings.value("notifyThreshold", 20, type=int))
        self._disconnected_style = str(
            self._settings.value("disconnectedStyle", "empty", type=str)
        )

    @property
    def icon_fill_color(self) -> str:
        return self._icon_fill_color

    @icon_fill_color.setter
    def icon_fill_color(self, value: str) -> None:
        if not is_valid_hex_color(value):
            return
        if self._icon_fill_color != value:
            self._icon_fill_color = value
            self._settings.setValue("iconFillColor", value)
            self.icon_fill_color_changed.emit(value)

    @property
    def icon_border_color(self) -> str:
        return self._icon_border_color

    @icon_border_color.setter
    def icon_border_color(self, value: str) -> None:
        if not is_valid_hex_color(value):
            return
        if self._icon_border_color != value:
            self._icon_border_color = value
            self._settings.setValue("iconBorderColor", value)
            self.icon_border_color_changed.emit(value)

    @property
    def icon_charging_color(self) -> str:
        return self._icon_charging_color

    @icon_charging_color.setter
    def icon_charging_color(self, value: str) -> None:
        if not is_valid_hex_color(value):
            return
        if self._icon_charging_color != value:
            self._icon_charging_color = value
            self._settings.setValue("iconChargingColor", value)
            self.icon_charging_color_changed.emit(value)

    @property
    def icon_scale(self) -> int:
        return self._icon_scale

    @icon_scale.setter
    def icon_scale(self, value: int) -> None:
        value = max(50, min(150, int(value)))
        if self._icon_scale != value:
            self._icon_scale = value
            self._settings.setValue("iconScale", value)
            self.icon_scale_changed.emit(value)

    @property
    def icon_show_text(self) -> bool:
        return self._icon_show_text

    @icon_show_text.setter
    def icon_show_text(self, value: bool) -> None:
        value = bool(value)
        if self._icon_show_text != value:
            self._icon_show_text = value
            self._settings.setValue("iconShowText", value)
            self.icon_show_text_changed.emit(value)

    @property
    def poll_interval(self) -> int:
        return self._poll_interval

    @poll_interval.setter
    def poll_interval(self, value: int) -> None:
        value = max(60, min(3600, int(value)))
        if self._poll_interval != value:
            self._poll_interval = value
            self._settings.setValue("pollInterval", value)
            self.poll_interval_changed.emit(value)

    @property
    def notify_enabled(self) -> bool:
        return self._notify_enabled

    @notify_enabled.setter
    def notify_enabled(self, value: bool) -> None:
        value = bool(value)
        if self._notify_enabled != value:
            self._notify_enabled = value
            self._settings.setValue("notifyEnabled", value)
            self.notify_enabled_changed.emit(value)

    @property
    def notify_threshold(self) -> int:
        return self._notify_threshold

    @notify_threshold.setter
    def notify_threshold(self, value: int) -> None:
        value = max(5, min(50, int(value)))
        if self._notify_threshold != value:
            self._notify_threshold = value
            self._settings.setValue("notifyThreshold", value)
            self.notify_threshold_changed.emit(value)

    @property
    def disconnected_style(self) -> str:
        return self._disconnected_style

    @disconnected_style.setter
    def disconnected_style(self, value: str) -> None:
        if value not in ("empty", "dim", "hide"):
            return
        if self._disconnected_style != value:
            self._disconnected_style = value
            self._settings.setValue("disconnectedStyle", value)
            self.disconnected_style_changed.emit(value)

