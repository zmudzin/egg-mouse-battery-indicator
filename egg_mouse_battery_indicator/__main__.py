#!/usr/bin/env python3

import fcntl
import logging
import os
import signal
import sys
import time
from typing import Optional

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from .paths import LOG_FILE
from .settings import AppSettings
from .tray import MouseBatteryTray, setup_logging

logger = logging.getLogger(__name__)

TRAY_WAIT_TIMEOUT_SEC = 60
TRAY_WAIT_POLL_SEC = 0.5


def _acquire_single_instance_lock() -> Optional[int]:
    """Acquire exclusive kernel flock on a runtime lockfile.
    
    Guarantees that only one instance of the indicator runs per user session.
    Automatically unlocked by kernel if process is killed or crashes.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    lock_file = os.path.join(runtime_dir, "egg-mouse-battery-indicator.lock")
    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, BlockingIOError):
        return None


def _wait_for_system_tray(timeout_sec: float = TRAY_WAIT_TIMEOUT_SEC) -> bool:
    """Plasma/GNOME tray is often not ready at autostart; wait briefly."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if QSystemTrayIcon.isSystemTrayAvailable():
            return True
        QApplication.processEvents()
        time.sleep(TRAY_WAIT_POLL_SEC)
    return QSystemTrayIcon.isSystemTrayAvailable()


def main() -> None:
    if "-h" in sys.argv or "--help" in sys.argv:
        print(
            "Endgame Gear Mouse Battery Indicator\n\n"
            "System tray battery monitor and onboard settings (CPI, polling, LOD, ...)\n"
            "for Endgame Gear wireless mice (XM2w 4k, OP1w 4k, etc.).\n\n"
            "Usage: egg-mouse-battery-indicator\n"
        )
        sys.exit(0)

    # 1. Single-instance lock
    lock_fd = _acquire_single_instance_lock()
    if lock_fd is None:
        print("Egg Mouse Battery Indicator is already running. Exiting.")
        sys.exit(0)

    setup_logging()
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setOrganizationName("EggMouseBatteryIndicator")
    app.setApplicationName("EggMouseBatteryIndicator")
    app.setDesktopFileName("egg-mouse-battery-indicator")
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        logger.warning(
            "System tray not ready yet; waiting up to %ss (common at login).",
            TRAY_WAIT_TIMEOUT_SEC,
        )
        if not _wait_for_system_tray():
            logger.critical("System tray not available after waiting.")
            print("Error: system tray not available.")
            sys.exit(1)
        logger.info("System tray became available.")

    app_settings = AppSettings()
    MouseBatteryTray(app_settings, parent=app)
    logger.info("Log file: %s", LOG_FILE)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
