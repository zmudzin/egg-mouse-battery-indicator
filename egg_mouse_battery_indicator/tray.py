"""System tray application for Endgame Gear mice (XM2w 4k, OP1w 4k, etc.)."""

from __future__ import annotations

import logging
import os
import socket
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

from PySide6.QtCore import SLOT, QObject, QSocketNotifier, Qt, QTimer, Signal, Slot
from PySide6.QtDBus import QDBusConnection
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import __version__
from .icon_renderer import MouseBatteryIconRenderer
from .mouse_settings_dialog import MouseSettingsDialog
from .paths import LOG_DIR, LOG_FILE
from .preferences_dialog import PreferencesDialog
from .protocol import (
    MouseStatus,
    PollingRate,
    POLLING_RATE_LABELS,
    fetch_mouse_status,
    find_control_hidraw,
    set_active_cpi_profile,
)
from .settings import AppSettings
from .worker import HidController

logger = logging.getLogger(__name__)

ERRORS_BEFORE_RESCAN = 2
RESUME_REFRESH_DELAY_MS = 2000
SLEEP_BACKOFF_STEPS = (15, 30, 60, 180, 300)  # Stepped backoff in seconds when asleep


def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    pkg_logger = logging.getLogger("egg_mouse_battery_indicator")
    pkg_logger.setLevel(logging.DEBUG)
    if any(isinstance(h, RotatingFileHandler) for h in pkg_logger.handlers):
        return
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    )
    pkg_logger.addHandler(handler)


class MouseBatteryTray(QSystemTrayIcon):
    def __init__(self, app_settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.app_settings = app_settings
        self.hidraw_path: Optional[str] = find_control_hidraw()
        self.last_status: Optional[MouseStatus] = None
        self.last_known_level: int = 100
        self.notified_low_battery = False
        self._consecutive_errors = 0
        self._last_icon_key: Optional[tuple] = None
        self._dpi_actions: list[QAction] = []
        self._reconnect_mode = False
        self._sleep_backoff_idx = 0

        # Netlink hotplug state
        self._nl_sock: Optional[socket.socket] = None
        self._nl_notifier: Optional[QSocketNotifier] = None
        self._hotplug_timer: Optional[QTimer] = None

        # Dedicated I/O Actor thread (Single-threaded hardware execution queue)
        self.hid_controller = HidController(self)
        self.hid_controller.start()

        self.icon_renderer = MouseBatteryIconRenderer(
            fill_color=self.app_settings.icon_fill_color,
            border_color=self.app_settings.icon_border_color,
            charging_color=self.app_settings.icon_charging_color,
            scale=self.app_settings.icon_scale,
            show_text=self.app_settings.icon_show_text,
        )

        self.app_settings.icon_fill_color_changed.connect(self._on_visual_settings_changed)
        self.app_settings.icon_border_color_changed.connect(self._on_visual_settings_changed)
        self.app_settings.icon_charging_color_changed.connect(self._on_visual_settings_changed)
        self.app_settings.icon_scale_changed.connect(self._on_visual_settings_changed)
        self.app_settings.icon_show_text_changed.connect(self._on_visual_settings_changed)
        self.app_settings.poll_interval_changed.connect(self._on_poll_interval_changed)
        self.app_settings.notify_threshold_changed.connect(lambda _: None)

        logger.info("Application starting up.")
        if self.hidraw_path:
            logger.info("Using HID device: %s", self.hidraw_path)
        else:
            logger.warning("Endgame Gear control interface not found.")

        self.menu = QMenu()
        self._setup_menu()
        self.setContextMenu(self.menu)
        self.activated.connect(self._on_tray_activated)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.update_status(priority=10))
        self.timer.start(self.app_settings.poll_interval * 1000)

        self._setup_sleep_monitor()
        self._setup_hotplug_monitor()

        self.setIcon(self.icon_renderer.render(0, False))
        self.update_status(priority=0)
        self.setVisible(True)

    def _setup_hotplug_monitor(self) -> None:
        """Listen to kernel Netlink kobject uevents directly via QSocketNotifier."""
        self._hotplug_timer = QTimer(self)
        self._hotplug_timer.setSingleShot(True)
        self._hotplug_timer.setInterval(400)
        self._hotplug_timer.timeout.connect(self._on_hotplug_debounced)

        try:
            NETLINK_KOBJECT_UEVENT = 15
            self._nl_sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_KOBJECT_UEVENT)
            self._nl_sock.bind((os.getpid(), 1))  # Multicast group 1 (kobject uevent)
            self._nl_sock.setblocking(False)
            self._nl_notifier = QSocketNotifier(self._nl_sock.fileno(), QSocketNotifier.Type.Read, self)
            self._nl_notifier.activated.connect(self._on_uevent_received)
            logger.info("Kernel Netlink hotplug monitor active.")
        except Exception as e:
            logger.warning("Could not initialize Netlink hotplug monitor: %s", e)
            self._nl_sock = None
            self._nl_notifier = None

    def _on_uevent_received(self) -> None:
        """Triggered asynchronously by kernel when any USB/input device is added or removed."""
        if not self._nl_sock:
            return
        relevant = False
        while True:
            try:
                data = self._nl_sock.recv(4096)
                if not data:
                    break
                # Filter strictly for Endgame Gear Vendor ID (3367) or product IDs
                if b"3367" in data or b"1970" in data or b"1968" in data or b"1982" in data or b"1969" in data:
                    relevant = True
            except BlockingIOError:
                break
            except Exception as e:
                logger.debug("Netlink read error: %s", e)
                break

        if relevant and self._hotplug_timer:
            logger.debug("Endgame Gear USB/hidraw uevent detected; debouncing HID refresh.")
            self._hotplug_timer.start(400)

    def _on_hotplug_debounced(self) -> None:
        logger.info("Hotplug event detected for Endgame Gear; refreshing device.")
        self.hidraw_path = None
        self._consecutive_errors = 0
        self.update_status(priority=0)

    def _setup_sleep_monitor(self) -> None:
        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            logger.warning("System D-Bus unavailable; sleep/resume refresh disabled.")
            return
        connected = bus.connect(
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            self,
            SLOT("on_prepare_for_sleep(bool)"),
        )
        if connected:
            logger.info("Listening for system sleep/resume events.")
        else:
            logger.warning("Could not subscribe to logind PrepareForSleep signal.")

    @Slot(bool)
    def on_prepare_for_sleep(self, sleeping: bool) -> None:
        if sleeping:
            logger.info("System going to sleep; suspending polling.")
            self.timer.stop()
            return
        logger.info("System resumed from sleep; scheduling HID refresh.")
        self.hidraw_path = None
        self._consecutive_errors = 0
        QTimer.singleShot(RESUME_REFRESH_DELAY_MS, lambda: self.update_status(priority=0))

    def _setup_menu(self) -> None:
        self.status_action = QAction(self.tr("Checking…"), self)
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)

        refresh_action = QAction(self.tr("Update status now"), self)
        refresh_action.triggered.connect(lambda: self.update_status(priority=0))
        self.menu.addAction(refresh_action)

        self.dpi_menu = QMenu(self.tr("CPI profile"), self.menu)
        self._dpi_group = QActionGroup(self)
        self._dpi_group.setExclusive(True)
        self._dpi_actions = []
        for i in range(1, 5):
            action = QAction(self.tr("Profile ") + str(i), self)
            action.setCheckable(True)
            action.setData(i)
            action.triggered.connect(lambda checked, p=i: self._on_dpi_profile_chosen(p, checked))
            self._dpi_group.addAction(action)
            self.dpi_menu.addAction(action)
            self._dpi_actions.append(action)
        self.menu.addMenu(self.dpi_menu)

        mouse_settings_action = QAction(self.tr("Mouse settings…"), self)
        mouse_settings_action.triggered.connect(self._open_mouse_settings)
        self.menu.addAction(mouse_settings_action)

        self.menu.addSeparator()

        self.interval_menu = QMenu(self.tr("Refresh status interval"), self.menu)
        self._interval_group = QActionGroup(self)
        self._interval_group.setExclusive(True)
        self._interval_actions: list[QAction] = []
        for seconds, label in (
            (30, self.tr("30 seconds")),
            (60, self.tr("1 minute")),
            (120, self.tr("2 minutes")),
            (300, self.tr("5 minutes")),
            (600, self.tr("10 minutes")),
            (1800, self.tr("30 minutes")),
            (3600, self.tr("1 hour")),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(seconds)
            action.triggered.connect(
                lambda checked, s=seconds: self._on_interval_chosen(s, checked)
            )
            self._interval_group.addAction(action)
            self.interval_menu.addAction(action)
            self._interval_actions.append(action)
        self.menu.addMenu(self.interval_menu)
        self._sync_interval_menu()

        self.notify_action = QAction(self.tr("Notify on low battery"), self)
        self.notify_action.setCheckable(True)
        self.notify_action.setChecked(self.app_settings.notify_enabled)
        self.notify_action.triggered.connect(self._toggle_notify)
        self.menu.addAction(self.notify_action)

        prefs_action = QAction(self.tr("Preferences…"), self)
        prefs_action.triggered.connect(self._open_preferences)
        self.menu.addAction(prefs_action)

        self.menu.addSeparator()

        version_action = QAction(f"v{__version__}", self)
        version_action.setEnabled(False)
        self.menu.addAction(version_action)

        quit_action = QAction(self.tr("Quit"), self)
        quit_action.triggered.connect(self._on_quit)
        self.menu.addAction(quit_action)

    def _on_quit(self) -> None:
        self.timer.stop()
        if self._hotplug_timer:
            self._hotplug_timer.stop()
        if self._nl_notifier:
            self._nl_notifier.setEnabled(False)
            try:
                self._nl_notifier.activated.disconnect()
            except Exception:
                pass
            self._nl_notifier = None
        if self._nl_sock:
            try:
                self._nl_sock.close()
            except Exception:
                pass
            self._nl_sock = None
        self.hid_controller.stop()
        QApplication.quit()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.update_status(priority=0)

    def _sync_interval_menu(self) -> None:
        current = self.app_settings.poll_interval
        for action in self._interval_actions:
            action.blockSignals(True)
            action.setChecked(int(action.data()) == current)
            action.blockSignals(False)

    def _on_interval_chosen(self, seconds: int, checked: bool) -> None:
        if not checked:
            return
        self.app_settings.poll_interval = seconds
        self._sync_interval_menu()

    def _toggle_notify(self, checked: bool) -> None:
        self.app_settings.notify_enabled = checked

    def _open_preferences(self) -> None:
        dialog = PreferencesDialog(self.app_settings, None)
        dialog.settings_saved.connect(self._on_prefs_saved)
        dialog.exec()

    def _open_mouse_settings(self) -> None:
        if not self.hidraw_path:
            self.hidraw_path = find_control_hidraw()
        if not self.hidraw_path:
            QMessageBox.warning(
                None,
                self.tr("Mouse settings"),
                self.tr("Endgame Gear device not found."),
            )
            return
        dialog = MouseSettingsDialog(self.hidraw_path, self.hid_controller, None)
        dialog.settings_applied.connect(lambda: self.update_status(priority=0))
        dialog.exec()
        self.update_status(priority=0)

    def _on_dpi_profile_chosen(self, profile: int, checked: bool) -> None:
        if not checked or not self.hidraw_path:
            return

        def on_done(res: Any, err: Optional[Exception]):
            err_msg = str(err) if err else (str(res) if res else "")
            self._on_dpi_switch_done(profile, err_msg)

        # High priority 0 preempts background polling; pure function passed
        self.hid_controller.submit(
            0,
            set_active_cpi_profile,
            self.hidraw_path,
            profile,
            callback=on_done,
        )

    def _on_dpi_switch_done(self, profile: int, error: str) -> None:
        if error:
            QMessageBox.warning(None, self.tr("CPI profile"), error)
            self.update_status(priority=0)
            return
        if self.last_status and self.last_status.settings_ok:
            self.last_status.active_cpi_profile = profile
            self._update_dpi_menu(self.last_status)
            self._apply_status(self.last_status, force_icon=True)
        else:
            self.update_status(priority=0)

    def _on_prefs_saved(self) -> None:
        self._on_visual_settings_changed()
        self.update_status(priority=0)

    def _on_visual_settings_changed(self) -> None:
        self.icon_renderer.reconfigure(
            fill_color=self.app_settings.icon_fill_color,
            border_color=self.app_settings.icon_border_color,
            charging_color=self.app_settings.icon_charging_color,
            scale=self.app_settings.icon_scale,
            show_text=self.app_settings.icon_show_text,
        )
        self._last_icon_key = None
        if self.last_status:
            self._apply_status(self.last_status, force_icon=True)

    def _set_reconnect_mode(self, enabled: bool) -> None:
        """Manage active interval vs stepped backoff when asleep or offline."""
        if enabled:
            if not self._reconnect_mode:
                self._reconnect_mode = True
                self._sleep_backoff_idx = 0

            # Stepped backoff: 15s -> 30s -> 60s -> 180s -> 300s
            step_idx = min(self._sleep_backoff_idx, len(SLEEP_BACKOFF_STEPS) - 1)
            interval_sec = SLEEP_BACKOFF_STEPS[step_idx]
            self._sleep_backoff_idx += 1

            logger.info(
                "Mouse sleeping/offline; polling in %ss.",
                interval_sec,
            )
            self.timer.start(interval_sec * 1000)
        else:
            if self._reconnect_mode:
                self._reconnect_mode = False
                self._sleep_backoff_idx = 0
                logger.info("Mouse active; restoring interval %ss.", self.app_settings.poll_interval)
            self.timer.start(self.app_settings.poll_interval * 1000)

    def _on_poll_interval_changed(self, seconds: int) -> None:
        if not self._reconnect_mode:
            self.timer.start(seconds * 1000)
        self._sync_interval_menu()

    def update_status(self, priority: int = 10) -> None:
        """Queue a status query task into the I/O Actor thread."""
        if not self.hidraw_path:
            self.hidraw_path = find_control_hidraw()
            if not self.hidraw_path:
                self._apply_status(MouseStatus(status="unavailable", error="Device not found"))
                self._set_reconnect_mode(True)
                return

        def on_done(res: Any, err: Optional[Exception]):
            if err is not None:
                logger.warning("Fetch status exception: %s", err)
                self._apply_status(MouseStatus(status="error", error=str(err)))
                self._set_reconnect_mode(True)
            elif isinstance(res, MouseStatus):
                logger.debug(
                    "Mouse poll: status=%s level=%s cpi_profile=%s polling=%s error=%s",
                    res.status,
                    res.level,
                    res.active_cpi_profile if res.settings_ok else None,
                    int(res.polling_rate) if res.settings_ok else None,
                    res.error,
                )
                self._apply_status(res)
                if res.status == "ok":
                    self._set_reconnect_mode(False)
                else:
                    self._set_reconnect_mode(True)

        # Pure module-level function passed without any QWidget/closure references
        self.hid_controller.submit(
            priority,
            fetch_mouse_status,
            self.hidraw_path,
            callback=on_done,
        )

    def _current_cpi_text(self, status: MouseStatus) -> Optional[str]:
        if not status.settings_ok or not status.cpi_profiles:
            return None
        idx = status.active_cpi_profile - 1
        if 0 <= idx < len(status.cpi_profiles):
            prof = status.cpi_profiles[idx]
            if prof.x == prof.y:
                return f"{prof.x} CPI"
            return f"{prof.x}x{prof.y} CPI"
        return None

    def _update_dpi_menu(self, status: MouseStatus) -> None:
        try:
            if not status.settings_ok or not status.cpi_profiles:
                self.dpi_menu.setEnabled(False)
                return
            self.dpi_menu.setEnabled(True)
            for i, action in enumerate(self._dpi_actions):
                if i < len(status.cpi_profiles):
                    p = status.cpi_profiles[i]
                    cpi_str = f"{p.x} CPI" if p.x == p.y else f"{p.x}x{p.y} CPI"
                    action.setText(self.tr("Profile {n}: {cpi}").format(n=i + 1, cpi=cpi_str))
                    action.blockSignals(True)
                    action.setChecked(status.active_cpi_profile == i + 1)
                    action.blockSignals(False)
        except Exception:
            logger.exception("CPI menu update failed")

    def _apply_status(self, status: MouseStatus, force_icon: bool = False) -> None:
        if status.status == "ok":
            self._consecutive_errors = 0
            self.last_known_level = status.level
        elif status.status == "error":
            self._consecutive_errors += 1
            if self._consecutive_errors >= ERRORS_BEFORE_RESCAN:
                self.hidraw_path = find_control_hidraw()
                self._consecutive_errors = 0

        self.last_status = status
        self._update_dpi_menu(status)

        if status.status == "ok":
            icon_key = ("ok", status.level, status.is_charging)
            if force_icon or icon_key != self._last_icon_key:
                self.setIcon(self.icon_renderer.render(status.level, status.is_charging))
                self._last_icon_key = icon_key

            charging = self.tr("charging") if status.is_charging else self.tr("discharging")
            cpi_text = self._current_cpi_text(status)
            poll_text = POLLING_RATE_LABELS.get(status.polling_rate)

            status_bits = [f"{status.level}%", charging]
            if cpi_text:
                status_bits.append(cpi_text)
            if poll_text:
                status_bits.append(poll_text)

            tooltip = f"Endgame Gear: {', '.join(status_bits)}"
            self.status_action.setText(f"Endgame Gear — {' · '.join(status_bits)}")
            self.setToolTip(tooltip)
            self._handle_low_battery(status.level)
            self.setVisible(True)
            return

        if status.status == "asleep":
            icon_key = ("asleep", self.last_known_level)
            if force_icon or icon_key != self._last_icon_key:
                self.setIcon(self.icon_renderer.render(self.last_known_level, False, asleep=True))
                self._last_icon_key = icon_key

            status_bits = [f"{self.last_known_level}%", self.tr("asleep")]
            cpi_text = self._current_cpi_text(status)
            if cpi_text:
                status_bits.append(cpi_text)

            tooltip = f"Endgame Gear (Asleep): {', '.join(status_bits)}"
            self.status_action.setText(f"Endgame Gear — {' · '.join(status_bits)}")
            self.setToolTip(tooltip)
            self.setVisible(True)
            return

        style = self.app_settings.disconnected_style
        if status.status == "unavailable":
            if style == "hide":
                self.setVisible(False)
                return
            error_icon = (style == "error")
            icon_key = ("unavailable", error_icon)
            if force_icon or icon_key != self._last_icon_key:
                self.setIcon(self.icon_renderer.render(0, False, error=error_icon))
                self._last_icon_key = icon_key
            self.status_action.setText(self.tr("Endgame Gear — disconnected"))
            self.setToolTip(self.tr("Endgame Gear mouse disconnected"))
            self.setVisible(True)
            self.notified_low_battery = False
            return

        # Error
        icon_key = ("error",)
        if force_icon or icon_key != self._last_icon_key:
            self.setIcon(self.icon_renderer.render(0, False, error=True))
            self._last_icon_key = icon_key
        self.status_action.setText(self.tr("Endgame Gear — unavailable"))
        self.setToolTip(status.error or self.tr("Unable to communicate with mouse"))
        self.setVisible(True)

    def _handle_low_battery(self, level: int) -> None:
        if not self.app_settings.notify_enabled:
            return
        if level <= self.app_settings.notify_threshold:
            if not self.notified_low_battery:
                try:
                    self.showMessage(
                        self.tr("Low mouse battery"),
                        self.tr("Endgame Gear battery is at {level}%").format(level=level),
                        QSystemTrayIcon.Warning,
                        5000,
                    )
                except Exception:
                    logger.exception("Low-battery notification failed")
                self.notified_low_battery = True
        else:
            self.notified_low_battery = False
