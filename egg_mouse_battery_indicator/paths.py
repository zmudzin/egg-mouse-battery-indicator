"""Filesystem locations for settings and logs."""

import os

LOG_DIR = os.path.join(
    os.path.expanduser("~"),
    ".local",
    "share",
    "EggMouseBatteryIndicator",
    "logs",
)
LOG_FILE = os.path.join(LOG_DIR, "app.log")

