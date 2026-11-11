"""Endgame Gear Wireless/Wired HID protocol implementation.

Supports OP1w 4k / XM2w 4k (v1) and OP1w 4k v2 / XM2w 4k v2:
- Device model & firmware identification (cmd 0x0D, 0x0E)
- Battery status (percentage, voltage, charging state, sleep state, cmd 0xB4)
- Full 1024-byte configuration blob read-modify-write (cmd 0x12, report 0xA0/0xA1)
- Sensor & CPI block (cmd 0x14): 1..4 CPI levels, X/Y stages, LOD (v1 mm / v2 0.7-1.7 mm),
  Angle Snapping, Ripple Control, Disable LED on Lift-Off, Sensor Angle Tuning (v2)
- Performance, power & click filter block (cmd 0x15): Polling rate, Motion Sync,
  Slamclick Filter, Motion Jitter Filter (v1), Force Max Sensor FPS (v2),
  Sensor Glass Mode (v2), per-button Multiclick Filter & SPDT GX Safe/Speed,
  1..120 min Power Saving & Deep Sleep timers
- Button mapping table (cmd 0x16): Left-handed mode + 8 button/wheel records
- Dongle re-pairing (cmd 0x70) and Factory Reset (cmd 0x13)
- Asynchronous HID notification parser (Report ID 0x03)
"""

from __future__ import annotations

import copy
import fcntl
import glob
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)

# HID ioctl constants
_IOC_WRITE = 1
_IOC_READ = 2


def _HIDIOCSFEATURE(length: int) -> int:
    return ((_IOC_WRITE | _IOC_READ) << 30) | (ord("H") << 8) | (0x06 << 0) | (length << 16)


def _HIDIOCGFEATURE(length: int) -> int:
    return ((_IOC_WRITE | _IOC_READ) << 30) | (ord("H") << 8) | (0x07 << 0) | (length << 16)


EGG_VENDOR_ID = 0x3367

# Supported Dongle Product IDs
DONGLE_PIDS = {
    0x1970: "Endgame Gear HS Dongle",
    0x1969: "Endgame Gear Dongle (Variant)",
}

# Supported Wired Mouse Product IDs (also match mouse_pid returned by cmd 0x0E)
WIRED_PIDS = {
    0x1968: "Endgame Gear XM2w 4k",
    0x1972: "Endgame Gear OP1w 4k",
    0x1982: "Endgame Gear XM2w 4k v2",
    0x1984: "Endgame Gear OP1w 4k v2",
}

ALL_SUPPORTED_PIDS = {**DONGLE_PIDS, **WIRED_PIDS}

REPORT_ID = 0xA1
BLOB_REPORT_ID = 0xA0
NOTIFY_REPORT_ID = 0x03
REPORT_LEN = 64
BLOB_REPORT_LEN = 1041
BLOB_SIZE = 1024

# Commands
CMD_DONGLE_FW = 0x0D
CMD_MOUSE_FW = 0x0E
CMD_PROBE = 0x0F
CMD_READ_BLOB = 0x12
CMD_FACTORY_RESET = 0x13
CMD_SENSOR = 0x14
CMD_PERF = 0x15
CMD_BUTTONS = 0x16
CMD_PAIR = 0x70
CMD_BATTERY = 0xB4

# Target selectors
TARGET_NONE = 0x00
TARGET_DONGLE = 0x01
TARGET_MOUSE = 0x0F

# Status codes
STATUS_OK = 0x01
STATUS_BUSY = 0x03
STATUS_NOT_APPLICABLE = 0x07
STATUS_UNREACHABLE_BAT = 0x08
STATUS_UNREACHABLE_CFG = 0x11

# cmd 0x15 / blob 0x06 filter bitmasks
FLAG_SLAMCLICK = 1 << 0
FLAG_MOTION_JITTER = 1 << 4   # v1 only
FLAG_MULTICLICK_ACK = 1 << 5  # v2 UI acknowledgement
FLAG_FORCE_MAX_FPS = 1 << 6   # v2 only

# SPDT / per-button filter encodings
SPDT_GX_SAFE = 0xF0
SPDT_GX_SPEED = 0xF1

# Fixed stage indicator LED colours (1..4: blue, green, yellow, red)
CPI_STAGE_COLORS = ("#0000FF", "#00FF00", "#FFFF00", "#FF0000")
CPI_STAGE_COLOR_NAMES = ("Blue", "Green", "Yellow", "Red")

# Physical order in cmd 0x15 (+4..+8) and cmd 0x16 (0..7)
BUTTON_FILTER_NAMES = ("Left", "Right", "Middle", "Back", "Forward")
BUTTON_TABLE_NAMES = (
    "Left",
    "Right",
    "Middle",
    "Back",
    "Forward",
    "CPI / Mode (Bottom)",
    "Wheel Up",
    "Wheel Down",
)

_hid_lock = threading.Lock()


class LodEncoding(IntEnum):
    MILLIMETRES = 0     # v1 normal (1, 2 mm) or v2 in Glass Mode (1.0, 2.0 mm)
    TENTHS_FROM_07 = 1  # v2 normal: 0..10 => 0.7..1.7 mm


@dataclass(frozen=True)
class ModelSpec:
    mouse_pid: int
    name: str
    is_v2: bool
    lod_encoding: LodEncoding
    power_payload_len: int
    has_angle_tuning: bool
    has_glass_mode: bool
    has_motion_jitter: bool
    has_multiclick_ack: bool
    has_force_max_fps: bool
    has_power_save_polling: bool
    has_cpi_stage_select: bool
    cpi_min: int
    cpi_max: int
    cpi_step: int
    cpi_step_high: int


MODEL_SPECS: dict[int, ModelSpec] = {
    0x1968: ModelSpec(
        mouse_pid=0x1968,
        name="Endgame Gear XM2w 4k",
        is_v2=False,
        lod_encoding=LodEncoding.MILLIMETRES,
        power_payload_len=10,
        has_angle_tuning=False,
        has_glass_mode=False,
        has_motion_jitter=True,
        has_multiclick_ack=False,
        has_force_max_fps=False,
        has_power_save_polling=False,
        has_cpi_stage_select=False,
        cpi_min=50,
        cpi_max=26000,
        cpi_step=50,
        cpi_step_high=50,
    ),
    0x1972: ModelSpec(
        mouse_pid=0x1972,
        name="Endgame Gear OP1w 4k",
        is_v2=False,
        lod_encoding=LodEncoding.MILLIMETRES,
        power_payload_len=10,
        has_angle_tuning=False,
        has_glass_mode=False,
        has_motion_jitter=True,
        has_multiclick_ack=False,
        has_force_max_fps=False,
        has_power_save_polling=False,
        has_cpi_stage_select=False,
        cpi_min=50,
        cpi_max=26000,
        cpi_step=50,
        cpi_step_high=50,
    ),
    0x1982: ModelSpec(
        mouse_pid=0x1982,
        name="Endgame Gear XM2w 4k v2",
        is_v2=True,
        lod_encoding=LodEncoding.TENTHS_FROM_07,
        power_payload_len=11,
        has_angle_tuning=True,
        has_glass_mode=True,
        has_motion_jitter=False,
        has_multiclick_ack=True,
        has_force_max_fps=True,
        has_power_save_polling=True,
        has_cpi_stage_select=True,
        cpi_min=10,
        cpi_max=30000,
        cpi_step=10,
        cpi_step_high=50,
    ),
    0x1984: ModelSpec(
        mouse_pid=0x1984,
        name="Endgame Gear OP1w 4k v2",
        is_v2=True,
        lod_encoding=LodEncoding.TENTHS_FROM_07,
        power_payload_len=11,
        has_angle_tuning=True,
        has_glass_mode=True,
        has_motion_jitter=False,
        has_multiclick_ack=True,
        has_force_max_fps=True,
        has_power_save_polling=True,
        has_cpi_stage_select=True,
        cpi_min=10,
        cpi_max=30000,
        cpi_step=10,
        cpi_step_high=50,
    ),
}

UNKNOWN_MODEL = ModelSpec(
    mouse_pid=0,
    name="Endgame Gear Mouse (Unknown)",
    is_v2=False,
    lod_encoding=LodEncoding.MILLIMETRES,
    power_payload_len=10,
    has_angle_tuning=False,
    has_glass_mode=False,
    has_motion_jitter=False,
    has_multiclick_ack=False,
    has_force_max_fps=False,
    has_power_save_polling=False,
    has_cpi_stage_select=False,
    cpi_min=50,
    cpi_max=26000,
    cpi_step=50,
    cpi_step_high=50,
)


def model_for_pid(pid: int) -> ModelSpec:
    return MODEL_SPECS.get(pid, UNKNOWN_MODEL)


def quantize_cpi(value: int, model: ModelSpec) -> int:
    """Clamp and round CPI value using the vendor tool rules for the given generation."""
    v = max(model.cpi_min, min(model.cpi_max, int(value)))
    step = model.cpi_step if (not model.is_v2 or v <= 10000) else model.cpi_step_high
    rounded = ((v + step // 2) // step) * step
    return max(model.cpi_min, min(model.cpi_max, rounded))


class PollingRate(IntEnum):
    PR125Hz = 0x40
    PR1000Hz = 0x08
    PR2000Hz = 0x04
    PR4000Hz = 0x02
    PR1000Hz_Eco = 0x80


POLLING_RATE_LABELS = {
    PollingRate.PR4000Hz: "4000 Hz",
    PollingRate.PR2000Hz: "2000 Hz",
    PollingRate.PR1000Hz: "1000 Hz",
    PollingRate.PR1000Hz_Eco: "1000 Hz (Power Saving)",
    PollingRate.PR125Hz: "125 Hz (Office Mode)",
}


def polling_options_for_model(model: ModelSpec) -> list[tuple[PollingRate, str]]:
    """Return available polling rate options in dropdown order."""
    opts = [
        (PollingRate.PR4000Hz, POLLING_RATE_LABELS[PollingRate.PR4000Hz]),
        (PollingRate.PR2000Hz, POLLING_RATE_LABELS[PollingRate.PR2000Hz]),
        (PollingRate.PR1000Hz, POLLING_RATE_LABELS[PollingRate.PR1000Hz]),
    ]
    if model.has_power_save_polling:
        opts.append((PollingRate.PR1000Hz_Eco, POLLING_RATE_LABELS[PollingRate.PR1000Hz_Eco]))
        opts.append((PollingRate.PR125Hz, POLLING_RATE_LABELS[PollingRate.PR125Hz]))
    return opts


class LiftOffDistance(IntEnum):
    Low = 1   # 1.0 mm on v1 / Glass Mode
    High = 2  # 2.0 mm on v1 / Glass Mode


LOD_LABELS = {
    LiftOffDistance.Low: "1.0 mm",
    LiftOffDistance.High: "2.0 mm",
}


def effective_lod_encoding(model: ModelSpec, glass_mode: bool) -> LodEncoding:
    if model.has_glass_mode and glass_mode:
        return LodEncoding.MILLIMETRES
    return model.lod_encoding


def lod_options(encoding: LodEncoding) -> list[tuple[int, str]]:
    """Return (raw_byte, label) pairs for a given LOD encoding."""
    if encoding == LodEncoding.MILLIMETRES:
        return [(1, "1.0 mm"), (2, "2.0 mm")]
    return [(idx, f"{(idx + 7) / 10:.1f} mm") for idx in range(11)]


def lod_label(raw_value: int, encoding: LodEncoding) -> str:
    for code, label in lod_options(encoding):
        if code == raw_value:
            return label
    return f"0x{raw_value:02X}"


def lod_convert_for_glass_mode(raw_value: int, glass_now_on: bool) -> int:
    """Convert stored LOD index across v2 normal <-> glass mode using vendor mapping."""
    if glass_now_on:
        if raw_value < 8:
            return 1
        if raw_value <= 10:
            return 2
        return raw_value
    if raw_value == 1:
        return 3   # 1.0 mm in tenths scale
    if raw_value == 2:
        return 10  # 1.7 mm (max in tenths scale)
    return raw_value


class PowerOffSetting(IntEnum):
    Minutes1 = 1
    Minutes2 = 2
    Minutes3 = 3
    Minutes5 = 5
    Minutes10 = 10
    Never = 0x80


POWER_OFF_LABELS = {
    PowerOffSetting.Minutes1: "1 min",
    PowerOffSetting.Minutes2: "2 min",
    PowerOffSetting.Minutes3: "3 min",
    PowerOffSetting.Minutes5: "5 min",
    PowerOffSetting.Minutes10: "10 min",
    PowerOffSetting.Never: "Never",
}


class ActionType(IntEnum):
    MOUSE_BUTTON = 0x00
    WHEEL = 0x01
    KEYBOARD = 0x02
    SPECIAL = 0x09
    CPI_PRESET = 0x0C
    CONSUMER = 0x20
    DISABLED = 0xFF


CONSUMER_ACTIONS: dict[int, str] = {
    0xCD: "Play / Pause",
    0xB5: "Next Track",
    0xB6: "Previous Track",
    0xE2: "Mute",
    0xE9: "Volume Up",
    0xEA: "Volume Down",
    0x96: "Browser",
    0x94: "File Explorer",
}

MOUSE_BUTTON_ACTIONS: dict[int, str] = {
    0x01: "Left Click",
    0x02: "Right Click",
    0x04: "Middle Click",
    0x08: "Back",
    0x10: "Forward",
}

# USB HID keyboard usage IDs <-> readable names
HID_KEY_NAMES: dict[int, str] = {
    **{0x04 + i: chr(ord("A") + i) for i in range(26)},
    0x1E: "1",
    0x1F: "2",
    0x20: "3",
    0x21: "4",
    0x22: "5",
    0x23: "6",
    0x24: "7",
    0x25: "8",
    0x26: "9",
    0x27: "0",
    0x28: "Enter",
    0x29: "Esc",
    0x2A: "Backspace",
    0x2B: "Tab",
    0x2C: "Space",
    0x2D: "-",
    0x2E: "=",
    0x2F: "[",
    0x30: "]",
    0x31: "\\",
    0x33: ";",
    0x34: "'",
    0x35: "`",
    0x36: ",",
    0x37: ".",
    0x38: "/",
    0x39: "Caps Lock",
    **{0x3A + i: f"F{i + 1}" for i in range(12)},
    0x46: "Print Screen",
    0x47: "Scroll Lock",
    0x48: "Pause",
    0x49: "Insert",
    0x4A: "Home",
    0x4B: "Page Up",
    0x4C: "Delete",
    0x4D: "End",
    0x4E: "Page Down",
    0x4F: "Right Arrow",
    0x50: "Left Arrow",
    0x51: "Down Arrow",
    0x52: "Up Arrow",
    0x53: "Num Lock",
    0x54: "Num /",
    0x55: "Num *",
    0x56: "Num -",
    0x57: "Num +",
    0x58: "Num Enter",
    0x59: "Num 1",
    0x5A: "Num 2",
    0x5B: "Num 3",
    0x5C: "Num 4",
    0x5D: "Num 5",
    0x5E: "Num 6",
    0x5F: "Num 7",
    0x60: "Num 8",
    0x61: "Num 9",
    0x62: "Num 0",
    0x63: "Num .",
    **{0x68 + i: f"F{i + 13}" for i in range(12)},
    0xE0: "Left Ctrl",
    0xE1: "Left Shift",
    0xE2: "Left Alt",
    0xE3: "Left Win",
    0xE4: "Right Ctrl",
    0xE5: "Right Shift",
    0xE6: "Right Alt",
    0xE7: "Right Win",
}


@dataclass
class CpiProfile:
    x: int = 800
    y: int = 800
    enabled: bool = True


@dataclass
class ButtonEntry:
    action_type: int = ActionType.MOUSE_BUTTON
    code: int = 0
    key: int = 0
    rest: bytes = b"\x00\x00\x00"
    multiclick_filter: int = 8

    def is_fixed_cpi(self) -> bool:
        return self.action_type == ActionType.CPI_PRESET

    def fixed_cpi_xy(self) -> tuple[int, int]:
        x = int(self.key) | (int(self.rest[0]) << 8)
        y = int(self.rest[1]) | (int(self.rest[2]) << 8)
        return x, y

    def set_fixed_cpi(self, x: int, y: int) -> None:
        self.action_type = ActionType.CPI_PRESET
        self.code = 1 if x != y else 0
        self.key = x & 0xFF
        self.rest = bytes(((x >> 8) & 0xFF, y & 0xFF, (y >> 8) & 0xFF))

    def set_simple_action(self, action_type: int, code: int = 0, key: int = 0) -> None:
        self.action_type = action_type & 0xFF
        self.code = code & 0xFF
        self.key = key & 0xFF
        self.rest = b"\x00\x00\x00"

    def set_keyboard(self, modifiers: int, hid_key: int) -> None:
        self.set_simple_action(ActionType.KEYBOARD, modifiers & 0xFF, hid_key & 0xFF)

    def same_binding(self, other: ButtonEntry) -> bool:
        return (
            self.action_type == other.action_type
            and self.code == other.code
            and self.key == other.key
            and self.rest == other.rest
        )

    def describe(self) -> str:
        t = self.action_type
        if t == ActionType.MOUSE_BUTTON:
            return MOUSE_BUTTON_ACTIONS.get(self.code, f"Mouse 0x{self.code:02X}")
        if t == ActionType.WHEEL:
            if self.code == 0x01:
                return "Scroll Up"
            if self.code == 0xFF:
                return "Scroll Down"
            signed = int.from_bytes([self.code], "little", signed=True)
            return f"Scroll {signed}"
        if t == ActionType.KEYBOARD:
            mods = []
            if self.code & 0x01:
                mods.append("Ctrl")
            if self.code & 0x02:
                mods.append("Shift")
            if self.code & 0x04:
                mods.append("Alt")
            if self.code & 0x08:
                mods.append("Win")
            if self.code & 0x10:
                mods.append("RCtrl")
            if self.code & 0x20:
                mods.append("RShift")
            if self.code & 0x40:
                mods.append("RAlt")
            if self.code & 0x80:
                mods.append("RWin")
            kname = HID_KEY_NAMES.get(self.key, f"0x{self.key:02X}" if self.key else "")
            if kname:
                mods.append(kname)
            return "+".join(mods) if mods else "Keyboard (none)"
        if t == ActionType.SPECIAL:
            return "CPI Loop" if self.code == 0xF1 else f"Special 0x{self.code:02X}"
        if t == ActionType.CPI_PRESET:
            x, y = self.fixed_cpi_xy()
            return f"Fixed CPI: {x}" if x == y else f"Fixed CPI: {x}x{y}"
        if t == ActionType.CONSUMER:
            return CONSUMER_ACTIONS.get(self.code, f"Media 0x{self.code:02X}")
        if t == ActionType.DISABLED:
            return "Disabled"
        return f"Unknown (0x{t:02X})"


def default_button_entries() -> list[ButtonEntry]:
    return [
        ButtonEntry(ActionType.MOUSE_BUTTON, 0x01, 0, b"\x00\x00\x00", 8),  # Left
        ButtonEntry(ActionType.MOUSE_BUTTON, 0x02, 0, b"\x00\x00\x00", 8),  # Right
        ButtonEntry(ActionType.MOUSE_BUTTON, 0x04, 0, b"\x00\x00\x00", 8),  # Middle
        ButtonEntry(ActionType.MOUSE_BUTTON, 0x08, 0, b"\x00\x00\x00", 8),  # Back
        ButtonEntry(ActionType.MOUSE_BUTTON, 0x10, 0, b"\x00\x00\x00", 8),  # Forward
        ButtonEntry(ActionType.SPECIAL, 0xF1, 0, b"\x00\x00\x00", 0),       # Special (CPI)
        ButtonEntry(ActionType.WHEEL, 0x01, 0, b"\x00\x00\x00", 0),         # Wheel Up
        ButtonEntry(ActionType.WHEEL, 0xFF, 0, b"\x00\x00\x00", 0),         # Wheel Down
    ]


def is_left_handed(buttons: list[ButtonEntry]) -> bool:
    if len(buttons) < 2:
        return False
    right_is_primary = (
        buttons[1].action_type == ActionType.MOUSE_BUTTON and buttons[1].code == 0x01
    )
    left_is_primary = (
        buttons[0].action_type == ActionType.MOUSE_BUTTON and buttons[0].code == 0x01
    )
    return right_is_primary and not left_is_primary


def set_left_handed(buttons: list[ButtonEntry], enabled: bool) -> list[ButtonEntry]:
    """Swap Left and Right bindings while keeping each slot's physical multiclick filter."""
    result = copy.deepcopy(buttons)
    if len(result) < 2:
        return result
    if enabled != is_left_handed(result):
        b0, b1 = result[0], result[1]
        b0.action_type, b1.action_type = b1.action_type, b0.action_type
        b0.code, b1.code = b1.code, b0.code
        b0.key, b1.key = b1.key, b0.key
        b0.rest, b1.rest = b1.rest, b0.rest
    return result


@dataclass
class BatteryStatus:
    status: str  # ok | asleep | unavailable | error
    level: int = -1
    voltage_mv: int = 0
    is_charging: bool = False
    is_wired: bool = False
    device_name: str = "Endgame Gear Mouse"
    error: str = ""
    mouse_pid: int = 0
    mouse_fw: str = ""
    dongle_fw: str = ""
    power_saving: PowerOffSetting = PowerOffSetting.Minutes1
    deep_sleep: PowerOffSetting = PowerOffSetting.Minutes3


@dataclass
class MouseStatus(BatteryStatus):
    """Battery plus full onboard configuration."""

    active_cpi_profile: int = 1  # 1..4
    cpi_levels: int = 4          # 1..4 active stages in cycle
    cpi_profiles: list[CpiProfile] = field(default_factory=list)
    polling_rate: PollingRate = PollingRate.PR1000Hz
    lift_off: LiftOffDistance = LiftOffDistance.Low
    lod_raw: int = 1
    led_on_liftoff: bool = True
    motion_sync: bool = False
    ripple_control: bool = False
    angle_snapping: bool = False
    angle_tuning: int = 0
    unknown0: int = 0
    raw_active_stage: int = 0
    filter_flags: int = FLAG_SLAMCLICK
    slamclick_filter: bool = True
    motion_jitter_filter: bool = False
    multiclick_ack: bool = False
    force_max_fps: bool = False
    glass_mode: bool = False
    power_saving_enabled: bool = True
    power_saving_minutes: int = 1
    deep_sleep_enabled: bool = True
    deep_sleep_minutes: int = 3
    button_filters: list[int] = field(default_factory=lambda: [8, 8, 8, 8, 8])
    button_entries: list[ButtonEntry] = field(default_factory=default_button_entries)
    raw_blob: bytes = b""
    settings_ok: bool = False

    @property
    def model(self) -> ModelSpec:
        return model_for_pid(self.mouse_pid)

    @property
    def model_identified(self) -> bool:
        return self.mouse_pid in MODEL_SPECS


def _parse_uevent(path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    data[k] = v
    except OSError:
        pass
    return data


def list_egg_hidraw_nodes() -> list[dict]:
    """Find all connected Endgame Gear hidraw control interfaces."""
    results = []
    for sysfs in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent_path = os.path.join(sysfs, "device", "uevent")
        if not os.path.exists(uevent_path):
            continue
        uevent = _parse_uevent(uevent_path)
        hid_id = uevent.get("HID_ID", "")
        parts = hid_id.split(":")
        if len(parts) >= 3:
            try:
                vid = int(parts[1], 16)
                pid = int(parts[2], 16)
            except ValueError:
                continue
            if vid == EGG_VENDOR_ID and pid in ALL_SUPPORTED_PIDS:
                desc_path = os.path.join(sysfs, "device", "report_descriptor")
                is_control = False
                if os.path.exists(desc_path):
                    try:
                        with open(desc_path, "rb") as df:
                            desc = df.read()
                            # Report ID 0xA1 is tagged with 0x85 0xA1
                            if b"\x85\xa1" in desc:
                                is_control = True
                    except OSError:
                        pass

                phys = uevent.get("HID_PHYS", "")
                node = f"/dev/{os.path.basename(sysfs)}"
                results.append({
                    "node": node,
                    "pid": pid,
                    "is_dongle": pid in DONGLE_PIDS,
                    "is_control": is_control,
                    "name": ALL_SUPPORTED_PIDS[pid],
                    "phys": phys,
                })
    return results


def find_control_hidraw() -> Optional[str]:
    """Return the primary control hidraw node for the Endgame Gear mouse/dongle."""
    candidates = list_egg_hidraw_nodes()
    if not candidates:
        return None

    for c in candidates:
        if c["is_control"]:
            return c["node"]

    for c in candidates:
        if c["phys"].endswith("/input1"):
            return c["node"]

    return candidates[0]["node"]


def _format_fw_version(major: int, minor: int) -> str:
    """Firmware versions use BCD-like hex digits (0x01, 0x10 -> '1.10')."""
    return f"{major:x}.{minor:02x}"


def parse_mouse_info_payload(payload: bytes) -> tuple[int, int, str]:
    """Extract (vid, mouse_pid, fw_str) from cmd 0x0E response payload (offset 16..)."""
    vid = int(payload[0]) | (int(payload[1]) << 8)
    pid = int(payload[2]) | (int(payload[3]) << 8)
    fw = _format_fw_version(int(payload[6]), int(payload[7]))
    return vid, pid, fw


def parse_dongle_info_payload(payload: bytes) -> str:
    """Extract dongle firmware string from cmd 0x0D response payload (offset 16..)."""
    return _format_fw_version(int(payload[0]), int(payload[1]))


def parse_notify_packet(data: bytes) -> Optional[dict]:
    """Parse an 8-byte HID input notification (Report ID 0x03)."""
    if len(data) < 3 or data[0] != NOTIFY_REPORT_ID:
        return None
    code = data[1]
    if code == CMD_BATTERY and len(data) >= 4:
        return {"type": "battery", "level": int(data[2]), "raw": data}
    if code == 0xB1:
        sub = data[2]
        if sub == 0x01:
            return {"type": "link", "awake": True, "raw": data}
        if sub in (0xF0, 0xF1):
            return {"type": "link", "awake": False, "raw": data}
    if code == 0x06:
        try:
            rate = PollingRate(data[2])
        except ValueError:
            rate = None
        return {"type": "polling", "rate": rate, "raw_rate": int(data[2]), "raw": data}
    if code == 0x02:
        return {"type": "cpi_stage", "stage": (int(data[2]) & 3) + 1, "raw": data}
    return {"type": "unknown", "code": code, "raw": data}


def _legacy_power_off(minutes: int, enabled: bool) -> PowerOffSetting:
    if not enabled:
        return PowerOffSetting.Never
    try:
        return PowerOffSetting(minutes)
    except ValueError:
        return PowerOffSetting.Minutes1


def decode_config_blob(blob: bytes, *, model: ModelSpec | None = None) -> MouseStatus:
    """Decode the 1024-byte configuration payload (report offset 16..1039)."""
    if len(blob) < 0x70:
        raise ValueError(f"Config blob too short ({len(blob)} bytes)")

    b = blob[:BLOB_SIZE]
    deep_sleep_enabled = not bool(b[0x03] & 0x80)
    deep_sleep_minutes = max(1, min(120, int(b[0x03] & 0x7F) or 1))
    power_saving_enabled = not bool(b[0x04] & 0x80)
    power_saving_minutes = max(1, min(120, int(b[0x04] & 0x7F) or 1))

    try:
        polling_rate = PollingRate(b[0x05])
    except ValueError:
        polling_rate = PollingRate.PR1000Hz

    filter_flags = int(b[0x06])
    slamclick = bool(filter_flags & FLAG_SLAMCLICK)
    motion_jitter = bool(filter_flags & FLAG_MOTION_JITTER)
    multiclick_ack = bool(filter_flags & FLAG_MULTICLICK_ACK)
    force_max_fps = bool(filter_flags & FLAG_FORCE_MAX_FPS)

    unknown0 = int(b[0x07])
    led_on_liftoff = bool(b[0x08])
    lod_raw = int(b[0x09])
    angle_snapping = bool(b[0x0A])
    ripple_control = bool(b[0x0B])
    motion_sync = bool(b[0x0C])
    raw_active_stage = int(b[0x0D])
    active_cpi = (raw_active_stage & 3) + 1

    raw_levels = int(b[0x0E])
    cpi_levels = raw_levels if 1 <= raw_levels <= 4 else 4

    angle_tuning = int.from_bytes([b[0x01]], "little", signed=True)
    glass_mode = bool(b[0x6F])

    profiles: list[CpiProfile] = []
    for n in range(4):
        base = 0x23 + n * 5
        x = int(b[base + 1]) | (int(b[base + 2]) << 8)
        y = int(b[base + 3]) | (int(b[base + 4]) << 8)
        if x == 0:
            x = [400, 800, 1600, 3200][n]
        if y == 0:
            y = x
        profiles.append(CpiProfile(x=x, y=y, enabled=(n < cpi_levels)))

    buttons: list[ButtonEntry] = []
    for n in range(8):
        base = 0x37 + n * 7
        buttons.append(
            ButtonEntry(
                action_type=int(b[base]),
                code=int(b[base + 1]),
                key=int(b[base + 2]),
                rest=bytes(b[base + 3 : base + 6]),
                multiclick_filter=int(b[base + 6]),
            )
        )

    button_filters = [buttons[i].multiclick_filter for i in range(5)]

    lod_legacy = LiftOffDistance.High if lod_raw == 2 else LiftOffDistance.Low
    resolved_model = model or UNKNOWN_MODEL

    return MouseStatus(
        status="ok",
        mouse_pid=resolved_model.mouse_pid,
        device_name=resolved_model.name if resolved_model.mouse_pid else "Endgame Gear Mouse",
        active_cpi_profile=active_cpi,
        cpi_levels=cpi_levels,
        cpi_profiles=profiles,
        polling_rate=polling_rate,
        lift_off=lod_legacy,
        lod_raw=lod_raw,
        led_on_liftoff=led_on_liftoff,
        motion_sync=motion_sync,
        ripple_control=ripple_control,
        angle_snapping=angle_snapping,
        angle_tuning=angle_tuning,
        unknown0=unknown0,
        raw_active_stage=raw_active_stage,
        filter_flags=filter_flags,
        slamclick_filter=slamclick,
        motion_jitter_filter=motion_jitter,
        multiclick_ack=multiclick_ack,
        force_max_fps=force_max_fps,
        glass_mode=glass_mode,
        power_saving=_legacy_power_off(power_saving_minutes, power_saving_enabled),
        deep_sleep=_legacy_power_off(deep_sleep_minutes, deep_sleep_enabled),
        power_saving_enabled=power_saving_enabled,
        power_saving_minutes=power_saving_minutes,
        deep_sleep_enabled=deep_sleep_enabled,
        deep_sleep_minutes=deep_sleep_minutes,
        button_filters=button_filters,
        button_entries=buttons,
        raw_blob=bytes(b),
        settings_ok=True,
    )


def encode_sensor_payload(status: MouseStatus, model: ModelSpec) -> bytes:
    """Build 28-byte cmd 0x14 payload."""
    p = bytearray(28)
    p[0] = status.unknown0 & 0xFF
    p[1] = 1 if status.led_on_liftoff else 0
    p[2] = status.lod_raw & 0xFF
    p[3] = 1 if status.angle_snapping else 0
    p[4] = 1 if status.ripple_control else 0
    if model.has_angle_tuning:
        tuning = max(-127, min(127, int(status.angle_tuning)))
        p[5] = tuning & 0xFF
    else:
        p[5] = 0
    p[6] = max(1, min(4, int(status.cpi_levels)))
    if model.has_cpi_stage_select:
        p[7] = (int(status.active_cpi_profile) - 1) & 3
    else:
        # v1 firmware ignores software CPI stage writes; echo stored byte verbatim
        p[7] = status.raw_active_stage & 0xFF

    for i in range(4):
        prof = status.cpi_profiles[i] if i < len(status.cpi_profiles) else CpiProfile()
        x = quantize_cpi(prof.x, model)
        y = quantize_cpi(prof.y, model)
        base = 8 + i * 5
        p[base] = 1 if x != y else 0
        p[base + 1] = x & 0xFF
        p[base + 2] = (x >> 8) & 0xFF
        p[base + 3] = y & 0xFF
        p[base + 4] = (y >> 8) & 0xFF
    return bytes(p)


def encode_perf_payload(status: MouseStatus, model: ModelSpec) -> bytes:
    """Build 10-byte (v1) or 11-byte (v2) cmd 0x15 payload."""
    length = model.power_payload_len
    p = bytearray(length)
    p[0] = 1 if status.motion_sync else 0
    p[1] = int(status.polling_rate) & 0xFF

    flags = status.filter_flags & 0xFF
    flags = (flags | FLAG_SLAMCLICK) if status.slamclick_filter else (flags & ~FLAG_SLAMCLICK)
    if model.has_motion_jitter:
        flags = (flags | FLAG_MOTION_JITTER) if status.motion_jitter_filter else (flags & ~FLAG_MOTION_JITTER)
    if model.has_multiclick_ack:
        flags = (flags | FLAG_MULTICLICK_ACK) if status.multiclick_ack else (flags & ~FLAG_MULTICLICK_ACK)
    if model.has_force_max_fps:
        flags = (flags | FLAG_FORCE_MAX_FPS) if status.force_max_fps else (flags & ~FLAG_FORCE_MAX_FPS)
    p[2] = flags & 0xFF

    psave_min = max(1, min(120, int(status.power_saving_minutes)))
    p[3] = (psave_min & 0x7F) | (0x00 if status.power_saving_enabled else 0x80)

    for i in range(5):
        val = status.button_filters[i] if i < len(status.button_filters) else 8
        p[4 + i] = val & 0xFF

    dsleep_min = max(1, min(120, int(status.deep_sleep_minutes)))
    p[9] = (dsleep_min & 0x7F) | (0x00 if status.deep_sleep_enabled else 0x80)

    if length > 10:
        p[10] = 1 if status.glass_mode else 0
    return bytes(p)


def sync_button_filters(buttons: list[ButtonEntry], button_filters: list[int]) -> list[ButtonEntry]:
    """Copy cmd 0x15 per-button filter values into button table entries 0..4."""
    synced = copy.deepcopy(buttons)
    for i in range(min(5, len(synced), len(button_filters))):
        synced[i].multiclick_filter = button_filters[i] & 0xFF
    return synced


def encode_button_chunks(
    buttons: list[ButtonEntry], button_filters: list[int] | None = None
) -> tuple[bytes, bytes]:
    """Build the two 28-byte chunks for cmd 0x16."""
    entries = sync_button_filters(buttons, button_filters) if button_filters is not None else buttons
    buf = bytearray(56)
    for i in range(min(8, len(entries))):
        e = entries[i]
        base = i * 7
        buf[base] = e.action_type & 0xFF
        buf[base + 1] = e.code & 0xFF
        buf[base + 2] = e.key & 0xFF
        rest = (e.rest + b"\x00\x00\x00")[:3]
        buf[base + 3 : base + 6] = rest
        buf[base + 6] = e.multiclick_filter & 0xFF
    return bytes(buf[:28]), bytes(buf[28:56])


def _get_feature_with_busy_retry(
    fd: int, report_id: int, length: int, busy_retries: int = 3
) -> bytearray:
    """Read a HID feature report, waiting and retrying if status byte is 0x03 (busy)."""
    recv_buf = bytearray(length)
    recv_buf[0] = report_id
    fcntl.ioctl(fd, _HIDIOCGFEATURE(length), recv_buf)
    if recv_buf[1] == STATUS_BUSY:
        for round_idx in range(busy_retries):
            time.sleep(0.20 * (round_idx + 1))
            recv_buf = bytearray(length)
            recv_buf[0] = report_id
            fcntl.ioctl(fd, _HIDIOCGFEATURE(length), recv_buf)
            if recv_buf[1] != STATUS_BUSY:
                break
    return recv_buf


def _io_transact(
    fd: int,
    cmd: int,
    target: int = TARGET_NONE,
    payload: bytes | bytearray | list[int] | None = None,
    declared_len: int | None = None,
    chunk_index: int = 0,
    pace: float = 0.08,
    retries: int = 4,
    retry_delay: float = 0.08,
    retry_statuses: tuple[int, ...] = (STATUS_BUSY,),
) -> tuple[int, bytes]:
    """Send a 64-byte feature command and read the 64-byte response."""
    send_buf = bytearray(REPORT_LEN)
    send_buf[0] = REPORT_ID
    send_buf[1] = cmd & 0xFF
    send_buf[2] = target & 0xFF
    if payload:
        dlen = declared_len if declared_len is not None else len(payload)
        send_buf[3] = dlen & 0xFF
        send_buf[16 : 16 + len(payload)] = payload
    send_buf[6] = chunk_index & 0xFF

    status_code = 0xFF
    recv_buf = bytearray(REPORT_LEN)

    for attempt in range(retries + 1):
        fcntl.ioctl(fd, _HIDIOCSFEATURE(len(send_buf)), send_buf)
        if pace > 0:
            time.sleep(pace)

        recv_buf = _get_feature_with_busy_retry(fd, REPORT_ID, REPORT_LEN)
        status_code = recv_buf[1]

        if status_code in retry_statuses and attempt < retries:
            time.sleep(retry_delay)
            continue
        break

    return status_code, bytes(recv_buf)


def _probe_device(fd: int, is_dongle: bool) -> bool:
    """Issue liveness probe (and clear dongle wedge state after any failed command).

    Over dongle: tries 0x0F target 0x01, then target 0x0F (answers even while mouse sleeps).
    Over USB cable: 0x0F returns 0x07, so falls back to 0x0E which answers on wired links.
    """
    if is_dongle:
        st, _ = _io_transact(fd, CMD_PROBE, target=TARGET_DONGLE, pace=0.04, retries=1)
        if st == STATUS_OK:
            return True
        st, _ = _io_transact(fd, CMD_PROBE, target=TARGET_MOUSE, pace=0.04, retries=1)
        if st == STATUS_OK:
            return True
    else:
        st, _ = _io_transact(fd, CMD_PROBE, target=TARGET_MOUSE, pace=0.04, retries=0)
        if st == STATUS_OK:
            return True
    st, _ = _io_transact(fd, CMD_MOUSE_FW, target=TARGET_NONE, pace=0.05, retries=1)
    return st == STATUS_OK


def _resolve_node_info(hidraw_path: str) -> tuple[bool, int, str]:
    for n in list_egg_hidraw_nodes():
        if n["node"] == hidraw_path:
            return bool(n["is_dongle"]), int(n["pid"]), str(n["name"])
    return True, 0, "Endgame Gear Mouse"


def _read_battery_internal(fd: int, is_dongle: bool) -> tuple[int, int, int]:
    """Execute 2-cycle 0xB4 battery query, returning (status, pct, voltage_mv)."""
    st = 0xFF
    recv = b""
    for cycle in range(2):
        st, recv = _io_transact(fd, CMD_BATTERY, target=TARGET_NONE, pace=0.14, retries=1)
        if cycle == 0:
            time.sleep(0.05)
    if len(recv) < 19:
        return st, 0, 0
    pct = max(0, int(recv[16]))
    voltage = int(recv[17]) | (int(recv[18]) << 8)
    return st, pct, voltage


def _read_config_blob_internal(fd: int) -> tuple[int, bytes]:
    """Issue cmd 0x12 and read the 1041-byte config blob via report 0xA0 (fallback 0xA1)."""
    send_buf = bytearray(REPORT_LEN)
    send_buf[0] = REPORT_ID
    send_buf[1] = CMD_READ_BLOB
    fcntl.ioctl(fd, _HIDIOCSFEATURE(len(send_buf)), send_buf)
    time.sleep(0.22)

    try:
        recv_buf = _get_feature_with_busy_retry(fd, BLOB_REPORT_ID, BLOB_REPORT_LEN)
    except OSError:
        recv_buf = _get_feature_with_busy_retry(fd, REPORT_ID, BLOB_REPORT_LEN)

    if len(recv_buf) < 16 + BLOB_SIZE:
        raise ValueError("Config blob too short")
    return int(recv_buf[1]), bytes(recv_buf[16 : 16 + BLOB_SIZE])


def _make_battery_status_from_raw(
    st: int,
    pct: int,
    voltage: int,
    is_dongle: bool,
    dev_name: str,
    mouse_pid: int,
    mouse_fw: str,
    dongle_fw: str,
) -> BatteryStatus:
    is_wired = not is_dongle
    if is_wired:
        is_charging = pct < 100
        clamped_pct = min(100, pct)
        if st == STATUS_OK or (3000 <= voltage <= 4500) or pct <= 100:
            return BatteryStatus(
                status="ok",
                level=clamped_pct,
                voltage_mv=voltage,
                is_charging=is_charging,
                is_wired=True,
                device_name=dev_name,
                mouse_pid=mouse_pid,
                mouse_fw=mouse_fw,
                dongle_fw=dongle_fw,
            )

    if st in (STATUS_UNREACHABLE_BAT, STATUS_UNREACHABLE_CFG) or (voltage == 0 and st != STATUS_OK):
        return BatteryStatus(
            status="asleep",
            device_name=dev_name,
            is_charging=False,
            is_wired=is_wired,
            mouse_pid=mouse_pid,
            mouse_fw=mouse_fw,
            dongle_fw=dongle_fw,
        )

    if st == STATUS_OK or (3000 <= voltage <= 4500 and pct <= 100):
        return BatteryStatus(
            status="ok",
            level=min(100, pct),
            voltage_mv=voltage,
            is_charging=False,
            is_wired=False,
            device_name=dev_name,
            mouse_pid=mouse_pid,
            mouse_fw=mouse_fw,
            dongle_fw=dongle_fw,
        )

    return BatteryStatus(
        status="unavailable",
        device_name=dev_name,
        mouse_pid=mouse_pid,
        mouse_fw=mouse_fw,
        dongle_fw=dongle_fw,
        error=f"Device returned status 0x{st:02X}",
    )


def query_battery(hidraw_path: str) -> BatteryStatus:
    """Fetch current battery percentage, voltage, firmware, and connection state."""
    is_dongle, usb_pid, usb_name = _resolve_node_info(hidraw_path)
    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
        except OSError as e:
            return BatteryStatus(status="error", error=f"Cannot open device: {e}")

        try:
            _probe_device(fd, is_dongle)

            dongle_fw = ""
            if is_dongle:
                st_d, resp_d = _io_transact(fd, CMD_DONGLE_FW, pace=0.06, retries=1)
                if st_d == STATUS_OK:
                    dongle_fw = parse_dongle_info_payload(resp_d[16:])
                else:
                    _probe_device(fd, is_dongle)

            mouse_pid = 0 if is_dongle else usb_pid
            mouse_fw = ""
            dev_name = usb_name if not is_dongle else "Endgame Gear Mouse"

            st_m, resp_m = _io_transact(fd, CMD_MOUSE_FW, pace=0.06, retries=1)
            if st_m == STATUS_OK:
                vid, pid, fw = parse_mouse_info_payload(resp_m[16:])
                if vid == EGG_VENDOR_ID and pid in MODEL_SPECS:
                    mouse_pid = pid
                    dev_name = MODEL_SPECS[pid].name
                mouse_fw = fw
            elif st_m in (STATUS_UNREACHABLE_CFG, STATUS_UNREACHABLE_BAT):
                _probe_device(fd, is_dongle)
                return BatteryStatus(
                    status="asleep",
                    device_name=dev_name,
                    is_charging=False,
                    is_wired=not is_dongle,
                    mouse_pid=mouse_pid,
                    dongle_fw=dongle_fw,
                )
            else:
                _probe_device(fd, is_dongle)

            st_b, pct, voltage = _read_battery_internal(fd, is_dongle)
            if st_b != STATUS_OK:
                _probe_device(fd, is_dongle)

            return _make_battery_status_from_raw(
                st_b, pct, voltage, is_dongle, dev_name, mouse_pid, mouse_fw, dongle_fw
            )
        except Exception as e:
            return BatteryStatus(status="error", error=str(e))
        finally:
            os.close(fd)


def fetch_mouse_status(hidraw_path: str, timeout: float = 1.0) -> MouseStatus:
    """Fetch complete mouse status (battery + firmware + full 1024-byte config blob)."""
    is_dongle, usb_pid, usb_name = _resolve_node_info(hidraw_path)

    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
        except OSError as e:
            return MouseStatus(
                status="error",
                settings_ok=False,
                error=f"Cannot open device: {e}",
            )

        try:
            _probe_device(fd, is_dongle)

            dongle_fw = ""
            if is_dongle:
                st_d, resp_d = _io_transact(fd, CMD_DONGLE_FW, pace=0.06, retries=1)
                if st_d == STATUS_OK:
                    dongle_fw = parse_dongle_info_payload(resp_d[16:])
                else:
                    _probe_device(fd, is_dongle)

            mouse_pid = 0 if is_dongle else usb_pid
            mouse_fw = ""
            dev_name = usb_name if not is_dongle else "Endgame Gear Mouse"

            st_m, resp_m = _io_transact(fd, CMD_MOUSE_FW, pace=0.06, retries=1)
            if st_m == STATUS_OK:
                vid, pid, fw = parse_mouse_info_payload(resp_m[16:])
                if vid == EGG_VENDOR_ID and pid in MODEL_SPECS:
                    mouse_pid = pid
                    dev_name = MODEL_SPECS[pid].name
                mouse_fw = fw
            elif st_m in (STATUS_UNREACHABLE_CFG, STATUS_UNREACHABLE_BAT):
                _probe_device(fd, is_dongle)
                return MouseStatus(
                    status="asleep",
                    device_name=dev_name,
                    is_charging=False,
                    is_wired=not is_dongle,
                    mouse_pid=mouse_pid,
                    dongle_fw=dongle_fw,
                    settings_ok=False,
                )
            else:
                _probe_device(fd, is_dongle)

            st_b, pct, voltage = _read_battery_internal(fd, is_dongle)
            if st_b != STATUS_OK:
                _probe_device(fd, is_dongle)

            bat = _make_battery_status_from_raw(
                st_b, pct, voltage, is_dongle, dev_name, mouse_pid, mouse_fw, dongle_fw
            )
            if bat.status == "asleep":
                return MouseStatus(
                    status="asleep",
                    level=bat.level,
                    voltage_mv=bat.voltage_mv,
                    is_charging=bat.is_charging,
                    is_wired=bat.is_wired,
                    device_name=bat.device_name,
                    mouse_pid=bat.mouse_pid,
                    mouse_fw=bat.mouse_fw,
                    dongle_fw=bat.dongle_fw,
                    settings_ok=False,
                )

            st_blob, blob = _read_config_blob_internal(fd)
            if st_blob != STATUS_OK:
                _probe_device(fd, is_dongle)
                if st_blob in (STATUS_UNREACHABLE_CFG, STATUS_UNREACHABLE_BAT):
                    return MouseStatus(
                        status="asleep",
                        device_name=bat.device_name,
                        is_charging=False,
                        is_wired=bat.is_wired,
                        mouse_pid=bat.mouse_pid,
                        mouse_fw=bat.mouse_fw,
                        dongle_fw=bat.dongle_fw,
                        settings_ok=False,
                    )
                raise ValueError(f"Config blob returned status 0x{st_blob:02X}")

            model = model_for_pid(mouse_pid)
            ms = decode_config_blob(blob, model=model)
            ms.status = "ok" if bat.status == "ok" else bat.status
            ms.level = bat.level
            ms.voltage_mv = bat.voltage_mv
            ms.is_charging = bat.is_charging
            ms.is_wired = bat.is_wired
            ms.device_name = dev_name
            ms.mouse_pid = mouse_pid
            ms.mouse_fw = mouse_fw
            ms.dongle_fw = dongle_fw
            return ms

        except Exception as e:
            logger.warning("Failed to fetch mouse status: %s", e)
            return MouseStatus(
                status="error",
                is_wired=not is_dongle,
                device_name=usb_name if not is_dongle else "Endgame Gear Mouse",
                settings_ok=False,
                error=str(e),
            )
        finally:
            os.close(fd)


def set_active_cpi_profile(hidraw_path: str, profile_idx: int) -> Optional[str]:
    """Quick switch active CPI profile (1..4). Supported on v2 firmware only."""
    if not (1 <= profile_idx <= 4):
        return "Invalid profile index"
    return apply_mouse_settings(hidraw_path, active_profile=profile_idx)


def apply_mouse_settings(
    hidraw_path: str,
    *,
    cpi_levels: int | None = None,
    active_profile: int | None = None,
    profiles: list[CpiProfile] | None = None,
    polling_rate: PollingRate | int | None = None,
    lift_off: LiftOffDistance | None = None,
    lod_raw: int | None = None,
    led_on_liftoff: bool | None = None,
    motion_sync: bool | None = None,
    ripple_control: bool | None = None,
    angle_snapping: bool | None = None,
    angle_tuning: int | None = None,
    slamclick_filter: bool | None = None,
    motion_jitter_filter: bool | None = None,
    multiclick_ack: bool | None = None,
    force_max_fps: bool | None = None,
    glass_mode: bool | None = None,
    power_saving: PowerOffSetting | None = None,
    deep_sleep: PowerOffSetting | None = None,
    power_saving_enabled: bool | None = None,
    power_saving_minutes: int | None = None,
    deep_sleep_enabled: bool | None = None,
    deep_sleep_minutes: int | None = None,
    button_filters: list[int] | None = None,
    button_entries: list[ButtonEntry] | None = None,
) -> Optional[str]:
    """Read-modify-write updated sensor, performance, power, and button settings."""
    cur = fetch_mouse_status(hidraw_path)
    if not cur.settings_ok:
        return cur.error or "Cannot read current settings (is mouse asleep?)"

    if not cur.model_identified:
        return (
            "Mouse model could not be identified (cmd 0x0E did not answer). "
            "Wake the mouse and retry before writing settings."
        )

    model = cur.model

    if active_profile is not None and active_profile != cur.active_cpi_profile:
        if not model.has_cpi_stage_select:
            return (
                f"{model.name} does not support switching CPI profiles from software. "
                "Use the CPI button underneath the mouse (or bind 'CPI Loop')."
            )

    new_status = copy.deepcopy(cur)

    if cpi_levels is not None:
        new_status.cpi_levels = max(1, min(4, int(cpi_levels)))
    if active_profile is not None and model.has_cpi_stage_select:
        new_status.active_cpi_profile = max(1, min(4, int(active_profile)))
    if profiles is not None:
        new_status.cpi_profiles = [
            CpiProfile(
                x=quantize_cpi(p.x, model),
                y=quantize_cpi(p.y, model),
                enabled=(idx < new_status.cpi_levels),
            )
            for idx, p in enumerate(profiles[:4])
        ]
    if polling_rate is not None and not cur.is_wired:
        new_status.polling_rate = PollingRate(int(polling_rate))

    if glass_mode is not None and model.has_glass_mode:
        new_status.glass_mode = bool(glass_mode)

    if lod_raw is not None:
        new_status.lod_raw = int(lod_raw) & 0xFF
    elif lift_off is not None:
        if model.is_v2 and not new_status.glass_mode:
            new_status.lod_raw = 3 if lift_off == LiftOffDistance.Low else 10
        else:
            new_status.lod_raw = int(lift_off)
    elif model.has_glass_mode and new_status.glass_mode != cur.glass_mode:
        new_status.lod_raw = lod_convert_for_glass_mode(cur.lod_raw, new_status.glass_mode)

    if led_on_liftoff is not None:
        new_status.led_on_liftoff = bool(led_on_liftoff)
    if motion_sync is not None:
        new_status.motion_sync = bool(motion_sync)
    if ripple_control is not None:
        new_status.ripple_control = bool(ripple_control)
    if angle_snapping is not None:
        new_status.angle_snapping = bool(angle_snapping)
    if angle_tuning is not None and model.has_angle_tuning:
        new_status.angle_tuning = max(-127, min(127, int(angle_tuning)))

    if slamclick_filter is not None:
        new_status.slamclick_filter = bool(slamclick_filter)
    if motion_jitter_filter is not None and model.has_motion_jitter:
        new_status.motion_jitter_filter = bool(motion_jitter_filter)
    if multiclick_ack is not None and model.has_multiclick_ack:
        new_status.multiclick_ack = bool(multiclick_ack)
    if force_max_fps is not None and model.has_force_max_fps:
        new_status.force_max_fps = bool(force_max_fps)

    # Power timers (support both legacy PowerOffSetting and granular 1..120 min)
    if power_saving_enabled is not None:
        new_status.power_saving_enabled = bool(power_saving_enabled)
    if power_saving_minutes is not None:
        new_status.power_saving_minutes = max(1, min(120, int(power_saving_minutes)))
    if power_saving is not None and power_saving_enabled is None and power_saving_minutes is None:
        if power_saving == PowerOffSetting.Never:
            new_status.power_saving_enabled = False
        else:
            new_status.power_saving_enabled = True
            new_status.power_saving_minutes = int(power_saving)

    if deep_sleep_enabled is not None:
        new_status.deep_sleep_enabled = bool(deep_sleep_enabled)
    if deep_sleep_minutes is not None:
        new_status.deep_sleep_minutes = max(1, min(120, int(deep_sleep_minutes)))
    if deep_sleep is not None and deep_sleep_enabled is None and deep_sleep_minutes is None:
        if deep_sleep == PowerOffSetting.Never:
            new_status.deep_sleep_enabled = False
        else:
            new_status.deep_sleep_enabled = True
            new_status.deep_sleep_minutes = int(deep_sleep)

    if button_filters is not None:
        new_status.button_filters = [int(v) & 0xFF for v in button_filters[:5]]
    if button_entries is not None:
        new_status.button_entries = copy.deepcopy(button_entries[:8])

    # Always keep cmd 0x15 button filters and cmd 0x16 byte +6 synchronized
    new_status.button_entries = sync_button_filters(
        new_status.button_entries, new_status.button_filters
    )

    cur_sensor_p = encode_sensor_payload(cur, model)
    new_sensor_p = encode_sensor_payload(new_status, model)
    cur_perf_p = encode_perf_payload(cur, model)
    new_perf_p = encode_perf_payload(new_status, model)
    cur_btn_c1, cur_btn_c2 = encode_button_chunks(cur.button_entries)
    new_btn_c1, new_btn_c2 = encode_button_chunks(new_status.button_entries)

    sensor_changed = new_sensor_p != cur_sensor_p
    perf_changed = new_perf_p != cur_perf_p
    buttons_changed = (new_btn_c1 != cur_btn_c1) or (new_btn_c2 != cur_btn_c2)

    if not sensor_changed and not perf_changed and not buttons_changed:
        logger.debug("apply_mouse_settings: no wire bytes changed; skipping write.")
        return None

    is_dongle, _, _ = _resolve_node_info(hidraw_path)

    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
        except OSError as e:
            return str(e)

        try:
            _probe_device(fd, is_dongle)
            wrote_any = False

            if sensor_changed:
                st1, _ = _io_transact(
                    fd,
                    CMD_SENSOR,
                    target=TARGET_MOUSE,
                    payload=new_sensor_p,
                    pace=0.12,
                    retries=5,
                    retry_delay=0.10,
                    retry_statuses=(STATUS_BUSY, STATUS_UNREACHABLE_BAT),
                )
                if st1 != STATUS_OK:
                    _probe_device(fd, is_dongle)
                    return f"Failed writing sensor settings (status 0x{st1:02X})"
                wrote_any = True

            if perf_changed:
                if wrote_any:
                    time.sleep(0.15)
                st2, _ = _io_transact(
                    fd,
                    CMD_PERF,
                    target=TARGET_MOUSE,
                    payload=new_perf_p,
                    declared_len=10,  # vendor under-declares 10 even when sending 11 on v2
                    pace=0.12,
                    retries=5,
                    retry_delay=0.10,
                    retry_statuses=(STATUS_BUSY, STATUS_UNREACHABLE_BAT),
                )
                if st2 != STATUS_OK:
                    _probe_device(fd, is_dongle)
                    return f"Failed writing performance settings (status 0x{st2:02X})"
                wrote_any = True

            if buttons_changed:
                if wrote_any:
                    time.sleep(0.15)
                for chunk_idx, chunk in ((1, new_btn_c1), (2, new_btn_c2)):
                    st3, _ = _io_transact(
                        fd,
                        CMD_BUTTONS,
                        target=TARGET_MOUSE,
                        payload=chunk,
                        chunk_index=chunk_idx,
                        pace=0.12,
                        retries=5,
                        retry_delay=0.10,
                        retry_statuses=(STATUS_BUSY, STATUS_UNREACHABLE_BAT),
                    )
                    if st3 != STATUS_OK:
                        _probe_device(fd, is_dongle)
                        return f"Failed writing button mapping chunk {chunk_idx} (status 0x{st3:02X})"
                    if chunk_idx == 1:
                        time.sleep(0.08)

            return None
        except OSError as e:
            return str(e)
        finally:
            os.close(fd)


def apply_mouse_settings_dict(hidraw_path: str, changes: dict) -> Optional[str]:
    """Helper to apply settings dictionary from HidController."""
    return apply_mouse_settings(hidraw_path, **changes)


def pair_dongle(hidraw_path: str) -> Optional[str]:
    """Send cmd 0x70 to re-establish radio link between mouse and wireless dongle."""
    is_dongle, _, _ = _resolve_node_info(hidraw_path)
    if not is_dongle:
        return "Pairing is only available when connected through the wireless dongle."

    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
        except OSError as e:
            return str(e)
        try:
            _probe_device(fd, True)
            st, _ = _io_transact(
                fd,
                CMD_PAIR,
                target=TARGET_NONE,
                pace=0.20,
                retries=3,
                retry_delay=0.15,
                retry_statuses=(STATUS_BUSY,),
            )
            if st != STATUS_OK:
                _probe_device(fd, True)
                return f"Pair command returned status 0x{st:02X}"
            return None
        except OSError as e:
            return str(e)
        finally:
            os.close(fd)


def factory_reset(hidraw_path: str) -> Optional[str]:
    """Send cmd 0x13 to restore onboard mouse factory defaults."""
    is_dongle, _, _ = _resolve_node_info(hidraw_path)
    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
        except OSError as e:
            return str(e)
        try:
            _probe_device(fd, is_dongle)
            st, _ = _io_transact(
                fd,
                CMD_FACTORY_RESET,
                target=TARGET_NONE,
                pace=1.10,
                retries=3,
                retry_delay=0.20,
                retry_statuses=(STATUS_BUSY,),
            )
            if st != STATUS_OK:
                _probe_device(fd, is_dongle)
                return f"Factory reset returned status 0x{st:02X}"
            return None
        except OSError as e:
            return str(e)
        finally:
            os.close(fd)
