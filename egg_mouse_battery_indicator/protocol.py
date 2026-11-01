"""Endgame Gear Wireless/Wired HID protocol implementation.

Supports:
- Battery status (percentage, voltage, charging state, sleep state)
- CPI profiles (1..4 stages, X/Y values)
- Polling rate (125, 1000, 2000, 4000 Hz)
- Lift-off distance (1mm, 2mm)
- Power saving / sleep timers
- Motion Sync, Ripple Control, Angle Snapping
"""

from __future__ import annotations

import errno
import fcntl
import glob
import logging
import os
import select
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)

# HID constants
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

# Supported Wired Product IDs
WIRED_PIDS = {
    0x1968: "XM2w 4k (Wired)",
    0x1967: "XM2w 4k Variant (Wired)",
    0x1982: "XM2w 4k v2 (Wired)",
    0x1981: "XM2w 4k v2 Variant (Wired)",
    0x1972: "OP1w 4k (Wired)",
    0x1971: "OP1w 4k Variant (Wired)",
    0x1984: "OP1w 4k v2 (Wired)",
    0x1983: "OP1w 4k v2 Variant (Wired)",
}

ALL_SUPPORTED_PIDS = {**DONGLE_PIDS, **WIRED_PIDS}

REPORT_ID = 0xA1
REPORT_LEN = 64
BLOB_REPORT_LEN = 1041

# Commands
CMD_PROBE = 0x0F
CMD_DONGLE_FW = 0x0D
CMD_MOUSE_FW = 0x0E
CMD_READ_BLOB = 0x12
CMD_SENSOR = 0x14
CMD_PERF = 0x15
CMD_BATTERY = 0xB4

_hid_lock = threading.Lock()


class PollingRate(IntEnum):
    PR125Hz = 0x40
    PR1000Hz = 0x08
    PR2000Hz = 0x04
    PR4000Hz = 0x02
    PR1000Hz_Eco = 0x80


POLLING_RATE_LABELS = {
    PollingRate.PR125Hz: "125 Hz (Office)",
    PollingRate.PR1000Hz: "1000 Hz",
    PollingRate.PR2000Hz: "2000 Hz",
    PollingRate.PR4000Hz: "4000 Hz",
    PollingRate.PR1000Hz_Eco: "1000 Hz (Eco)",
}


class LiftOffDistance(IntEnum):
    Low = 1   # 1.0 mm
    High = 2  # 2.0 mm


LOD_LABELS = {
    LiftOffDistance.Low: "1.0 mm",
    LiftOffDistance.High: "2.0 mm",
}


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


@dataclass
class CpiProfile:
    x: int = 800
    y: int = 800
    enabled: bool = True


@dataclass
class BatteryStatus:
    status: str  # ok | asleep | unavailable | error
    level: int = -1
    voltage_mv: int = 0
    is_charging: bool = False
    is_wired: bool = False
    device_name: str = "Endgame Gear Mouse"
    error: str = ""
    power_saving: PowerOffSetting = PowerOffSetting.Minutes1
    deep_sleep: PowerOffSetting = PowerOffSetting.Minutes3


@dataclass
class MouseStatus(BatteryStatus):
    """Battery plus performance settings."""

    active_cpi_profile: int = 1  # 1..4
    cpi_profiles: list[CpiProfile] = field(default_factory=list)
    polling_rate: PollingRate = PollingRate.PR1000Hz
    lift_off: LiftOffDistance = LiftOffDistance.Low
    motion_sync: bool = False
    ripple_control: bool = False
    angle_snapping: bool = False
    settings_ok: bool = False


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
                
                # Check phys suffix as fallback (input1 is commonly control on dongle)
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

    # Prefer explicitly identified control interfaces
    for c in candidates:
        if c["is_control"]:
            return c["node"]

    # Fallback to candidate whose phys ends with input1 or first candidate
    for c in candidates:
        if c["phys"].endswith("/input1"):
            return c["node"]

    return candidates[0]["node"]


def _io_transact(
    fd: int,
    cmd: int,
    target: int = 0x00,
    payload: bytes | bytearray | list[int] | None = None,
    declared_len: int | None = None,
    pace: float = 0.08,
    retries: int = 5,
    retry_delay: float = 0.06,
    retry_statuses: tuple[int, ...] = (0x03,),
) -> tuple[int, bytes]:
    """Send a 64-byte command feature report and read the 64-byte response.
    Automatically retries if device returns status in retry_statuses (e.g. 0x03 Busy, 0x08 RF ACK wait).
    """
    send_buf = bytearray(REPORT_LEN)
    send_buf[0] = REPORT_ID
    send_buf[1] = cmd & 0xFF
    send_buf[2] = target & 0xFF
    if payload:
        dlen = declared_len if declared_len is not None else len(payload)
        send_buf[3] = dlen & 0xFF
        send_buf[16 : 16 + len(payload)] = payload

    status_code = 0xFF
    recv_buf = bytearray(REPORT_LEN)

    for attempt in range(retries + 1):
        fcntl.ioctl(fd, _HIDIOCSFEATURE(len(send_buf)), send_buf)
        if pace > 0:
            time.sleep(pace)

        recv_buf[0] = REPORT_ID
        fcntl.ioctl(fd, _HIDIOCGFEATURE(len(recv_buf)), recv_buf)
        status_code = recv_buf[1]

        # 0x03 means Busy; 0x08 means RF No ACK (mouse busy writing Flash NVMC or asleep)
        if status_code in retry_statuses and attempt < retries:
            time.sleep(retry_delay)
            continue

        break

    return status_code, bytes(recv_buf)


def query_battery(hidraw_path: str) -> BatteryStatus:
    """Fetch current battery percentage, voltage, and connection state."""
    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
        except OSError as e:
            return BatteryStatus(status="error", error=f"Cannot open device: {e}")

        try:
            # Check if this node is dongle or wired
            nodes = list_egg_hidraw_nodes()
            is_dongle = True
            dev_name = "Endgame Gear Mouse"
            for n in nodes:
                if n["node"] == hidraw_path:
                    is_dongle = n["is_dongle"]
                    dev_name = n["name"]
                    break

            if is_dongle:
                # 0. Probe mouse through dongle
                probe_buf = bytearray(REPORT_LEN)
                probe_buf[0] = REPORT_ID
                probe_buf[1] = CMD_PROBE
                probe_buf[2] = 0x0F
                try:
                    fcntl.ioctl(fd, _HIDIOCSFEATURE(len(probe_buf)), probe_buf)
                    time.sleep(0.08)
                    recv_probe = bytearray(REPORT_LEN)
                    recv_probe[0] = REPORT_ID
                    fcntl.ioctl(fd, _HIDIOCGFEATURE(len(recv_probe)), recv_probe)
                except OSError as e:
                    return BatteryStatus(status="error", error=f"Probe failed: {e}")

                if recv_probe[1] == 0x08:
                    return BatteryStatus(
                        status="asleep",
                        device_name=dev_name,
                        is_charging=False,
                        is_wired=False,
                    )

            # Battery request cycles (first wakes up, second returns data)
            bat_cmd = bytearray(REPORT_LEN)
            bat_cmd[0] = REPORT_ID
            bat_cmd[1] = CMD_BATTERY

            # Cycle 1
            fcntl.ioctl(fd, _HIDIOCSFEATURE(len(bat_cmd)), bat_cmd)
            time.sleep(0.15)
            discard = bytearray(REPORT_LEN)
            discard[0] = REPORT_ID
            fcntl.ioctl(fd, _HIDIOCGFEATURE(len(discard)), discard)

            time.sleep(0.05)

            # Cycle 2
            fcntl.ioctl(fd, _HIDIOCSFEATURE(len(bat_cmd)), bat_cmd)
            time.sleep(0.15)
            recv = bytearray(REPORT_LEN)
            recv[0] = REPORT_ID
            fcntl.ioctl(fd, _HIDIOCGFEATURE(len(recv)), recv)

            status = recv[1]
            pct = min(100, max(0, int(recv[16])))
            voltage = int(recv[17]) | (int(recv[18]) << 8)

            # Li-Ion/LiPo battery operates between 3200mV and 4300mV
            if 3000 <= voltage <= 4500 and pct <= 100:
                return BatteryStatus(
                    status="ok",
                    level=pct,
                    voltage_mv=voltage,
                    is_charging=not is_dongle,
                    is_wired=not is_dongle,
                    device_name=dev_name,
                )

            if status == 0x01:
                return BatteryStatus(
                    status="ok",
                    level=pct,
                    voltage_mv=voltage,
                    is_charging=not is_dongle,
                    is_wired=not is_dongle,
                    device_name=dev_name,
                )
            elif status == 0x08 or voltage == 0:
                return BatteryStatus(
                    status="asleep",
                    device_name=dev_name,
                    is_charging=False,
                    is_wired=not is_dongle,
                )
            else:
                return BatteryStatus(
                    status="unavailable",
                    device_name=dev_name,
                    error=f"Device returned status 0x{status:02X}",
                )

        except Exception as e:
            return BatteryStatus(status="error", error=str(e))
        finally:
            os.close(fd)


def fetch_mouse_status(hidraw_path: str, timeout: float = 1.0) -> MouseStatus:
    """Fetch complete mouse status (battery + all onboard performance settings)."""
    bat = query_battery(hidraw_path)
    if bat.status == "asleep":
        time.sleep(0.08)
        bat2 = query_battery(hidraw_path)
        if bat2.status == "ok":
            bat = bat2

    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
        except OSError as e:
            return MouseStatus(
                status=bat.status,
                level=bat.level,
                voltage_mv=bat.voltage_mv,
                is_charging=bat.is_charging,
                is_wired=bat.is_wired,
                device_name=bat.device_name,
                settings_ok=False,
                error=f"Cannot open device: {e}",
            )

        try:
            # Send command 0x12 to request the 1024-byte config blob
            send_buf = bytearray(REPORT_LEN)
            send_buf[0] = REPORT_ID
            send_buf[1] = CMD_READ_BLOB
            fcntl.ioctl(fd, _HIDIOCSFEATURE(len(send_buf)), send_buf)
            time.sleep(0.15)

            recv_buf = bytearray(BLOB_REPORT_LEN)
            recv_buf[0] = REPORT_ID
            fcntl.ioctl(fd, _HIDIOCGFEATURE(len(recv_buf)), recv_buf)

            if len(recv_buf) < 64:
                raise ValueError("Config blob too short")

            b = recv_buf[16 : 16 + 1024]

            # Parse deep sleep and power saving timers
            deep_sleep_val = b[0x03] & 0x7F
            if b[0x03] & 0x80:
                deep_sleep = PowerOffSetting.Never
            else:
                try:
                    deep_sleep = PowerOffSetting(deep_sleep_val)
                except ValueError:
                    deep_sleep = PowerOffSetting.Minutes3

            power_saving_val = b[0x04] & 0x7F
            if b[0x04] & 0x80:
                power_saving = PowerOffSetting.Never
            else:
                try:
                    power_saving = PowerOffSetting(power_saving_val)
                except ValueError:
                    power_saving = PowerOffSetting.Minutes1

            # Polling rate
            polling_raw = b[0x05]
            try:
                polling_rate = PollingRate(polling_raw)
            except ValueError:
                polling_rate = PollingRate.PR2000Hz

            # Lift-off distance
            lod_val = b[0x09]
            lod = LiftOffDistance.High if lod_val == 2 else LiftOffDistance.Low

            angle_snap = bool(b[0x0A])
            ripple = bool(b[0x0B])
            motion_sync = bool(b[0x0C])

            active_cpi = (b[0x0D] & 3) + 1 if b[0x0D] else 1

            # CPI profile stages (standard defaults: 400, 800, 1600, 3200)
            default_cpis = [400, 800, 1600, 3200]
            profiles = []
            for n in range(4):
                # Stage enabled flag in blob at byte 34, 39, 44, 49
                en = True
                flag_idx = 34 + n * 5
                if flag_idx < len(recv_buf):
                    en = bool(recv_buf[flag_idx])
                val = default_cpis[n]
                profiles.append(CpiProfile(x=val, y=val, enabled=en))

            final_status = "ok" if bat.status == "ok" else "asleep"

            return MouseStatus(
                status=final_status,
                level=bat.level,
                voltage_mv=bat.voltage_mv,
                is_charging=bat.is_charging,
                is_wired=bat.is_wired,
                device_name=bat.device_name,
                active_cpi_profile=active_cpi,
                cpi_profiles=profiles,
                polling_rate=polling_rate,
                lift_off=lod,
                motion_sync=motion_sync,
                ripple_control=ripple,
                angle_snapping=angle_snap,
                power_saving=power_saving,
                deep_sleep=deep_sleep,
                settings_ok=True,
            )

        except Exception as e:
            logger.warning("Failed to parse config blob: %s", e)
            return MouseStatus(
                status=bat.status,
                level=bat.level,
                voltage_mv=bat.voltage_mv,
                is_charging=bat.is_charging,
                is_wired=bat.is_wired,
                device_name=bat.device_name,
                settings_ok=False,
                error=str(e),
            )
        finally:
            os.close(fd)


def set_active_cpi_profile(hidraw_path: str, profile_idx: int) -> Optional[str]:
    """Quick switch active CPI profile (1..4)."""
    if not (1 <= profile_idx <= 4):
        return "Invalid profile index"

    status = fetch_mouse_status(hidraw_path)
    if not status.settings_ok:
        return status.error or "Failed to read mouse settings"

    # Build sensor payload (cmd 0x14)
    # p[6] = levels, p[7] = active (0-indexed), p[8..27] = CPI stages
    payload = bytearray(28)
    payload[1] = 0  # LED on liftoff
    payload[2] = 1 if status.lift_off == LiftOffDistance.Low else 2
    payload[3] = 1 if status.angle_snapping else 0
    payload[4] = 1 if status.ripple_control else 0
    payload[6] = len(status.cpi_profiles)
    payload[7] = (profile_idx - 1) & 3

    for i, p in enumerate(status.cpi_profiles):
        base = 8 + i * 5
        payload[base] = 1 if p.enabled else 0
        payload[base + 1] = p.x & 0xFF
        payload[base + 2] = (p.x >> 8) & 0xFF
        payload[base + 3] = p.y & 0xFF
        payload[base + 4] = (p.y >> 8) & 0xFF

    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
            try:
                # Wake up mouse if needed before writing settings
                _io_transact(fd, CMD_PROBE, target=0x0F, pace=0.03, retries=2, retry_delay=0.05)
                time.sleep(0.03)

                st, _ = _io_transact(
                    fd,
                    CMD_SENSOR,
                    target=0x0F,
                    payload=payload,
                    pace=0.1,
                    retries=6,
                    retry_delay=0.10,
                    retry_statuses=(0x03, 0x08),
                )
                if st != 0x01:
                    return f"Mouse returned status 0x{st:02X}"
                return None
            finally:
                os.close(fd)
        except OSError as e:
            return str(e)


def apply_mouse_settings(
    hidraw_path: str,
    *,
    active_profile: int | None = None,
    profiles: list[CpiProfile] | None = None,
    polling_rate: PollingRate | None = None,
    lift_off: LiftOffDistance | None = None,
    motion_sync: bool | None = None,
    ripple_control: bool | None = None,
    angle_snapping: bool | None = None,
    power_saving: PowerOffSetting | None = None,
    deep_sleep: PowerOffSetting | None = None,
) -> Optional[str]:
    """Write updated sensor and performance settings to the mouse."""
    cur = fetch_mouse_status(hidraw_path)
    if not cur.settings_ok:
        return cur.error or "Cannot read current settings"

    act_idx = active_profile if active_profile is not None else cur.active_cpi_profile
    profs = profiles if profiles is not None else cur.cpi_profiles
    poll = polling_rate if polling_rate is not None else cur.polling_rate
    lod = lift_off if lift_off is not None else cur.lift_off
    msync = motion_sync if motion_sync is not None else cur.motion_sync
    rip = ripple_control if ripple_control is not None else cur.ripple_control
    asnap = angle_snapping if angle_snapping is not None else cur.angle_snapping
    psave = power_saving if power_saving is not None else cur.power_saving
    dsleep = deep_sleep if deep_sleep is not None else cur.deep_sleep

    # Check which sections actually changed
    sensor_changed = (
        act_idx != cur.active_cpi_profile
        or profs != cur.cpi_profiles
        or lod != cur.lift_off
        or asnap != cur.angle_snapping
        or rip != cur.ripple_control
    )
    perf_changed = (
        poll != cur.polling_rate
        or msync != cur.motion_sync
        or psave != cur.power_saving
        or dsleep != cur.deep_sleep
    )

    if not sensor_changed and not perf_changed:
        logger.debug("apply_mouse_settings called but no parameters changed; skipping write.")
        return None

    # 1. Write Sensor Settings (0x14)
    sensor_p = bytearray(28)
    sensor_p[1] = 0
    sensor_p[2] = 1 if lod == LiftOffDistance.Low else 2
    sensor_p[3] = 1 if asnap else 0
    sensor_p[4] = 1 if rip else 0
    sensor_p[6] = len(profs)
    sensor_p[7] = (act_idx - 1) & 3

    for i, p in enumerate(profs):
        base = 8 + i * 5
        sensor_p[base] = 1 if p.enabled else 0
        sensor_p[base + 1] = p.x & 0xFF
        sensor_p[base + 2] = (p.x >> 8) & 0xFF
        sensor_p[base + 3] = p.y & 0xFF
        sensor_p[base + 4] = (p.y >> 8) & 0xFF

    # 2. Write Performance & Power Settings (0x15)
    perf_p = bytearray(11)
    perf_p[0] = 1 if msync else 0
    perf_p[1] = int(poll) & 0xFF
    perf_p[3] = 0x80 if psave == PowerOffSetting.Never else int(psave)
    # Default 8ms button debounce filter (p[4..8])
    for i in range(5):
        perf_p[4 + i] = 8
    perf_p[9] = 0x80 if dsleep == PowerOffSetting.Never else int(dsleep)

    with _hid_lock:
        try:
            fd = os.open(hidraw_path, os.O_RDWR)
            try:
                # Wake up mouse if needed before writing settings
                _io_transact(fd, CMD_PROBE, target=0x0F, pace=0.03, retries=2, retry_delay=0.05)
                time.sleep(0.03)

                if sensor_changed:
                    st1, _ = _io_transact(
                        fd,
                        CMD_SENSOR,
                        target=0x0F,
                        payload=sensor_p,
                        pace=0.1,
                        retries=6,
                        retry_delay=0.10,
                        retry_statuses=(0x03, 0x08),
                    )
                    if st1 != 0x01:
                        return f"Failed writing sensor settings (status 0x{st1:02X})"

                if sensor_changed and perf_changed:
                    # 150ms delay to let device write NVMC Flash and re-enable RF interrupts
                    time.sleep(0.15)

                if perf_changed:
                    # Endgame Gear 4k protocol declares 10 bytes, actually transmits 11
                    st2, _ = _io_transact(
                        fd,
                        CMD_PERF,
                        target=0x0F,
                        payload=perf_p,
                        declared_len=10,
                        pace=0.1,
                        retries=6,
                        retry_delay=0.10,
                        retry_statuses=(0x03, 0x08),
                    )
                    if st2 != 0x01:
                        return f"Failed writing performance settings (status 0x{st2:02X})"

                return None
            finally:
                os.close(fd)
        except OSError as e:
            return str(e)


def apply_mouse_settings_dict(hidraw_path: str, changes: dict) -> Optional[str]:
    """Helper to apply settings dictionary from pure function call."""
    return apply_mouse_settings(hidraw_path, **changes)
