from egg_mouse_battery_indicator.protocol import (
    ALL_SUPPORTED_PIDS,
    CMD_BATTERY,
    CMD_PROBE,
    CMD_READ_BLOB,
    CMD_SENSOR,
    CMD_PERF,
    CpiProfile,
    LiftOffDistance,
    MouseStatus,
    PollingRate,
    PowerOffSetting,
    _HIDIOCSFEATURE,
    _HIDIOCGFEATURE,
)


def test_ioctls():
    assert _HIDIOCSFEATURE(64) == 0xC0404806
    assert _HIDIOCGFEATURE(64) == 0xC0404807


def test_pids():
    assert 0x1970 in ALL_SUPPORTED_PIDS
    assert 0x1968 in ALL_SUPPORTED_PIDS


def test_enums():
    assert PollingRate.PR1000Hz == 0x08
    assert PollingRate.PR2000Hz == 0x04
    assert PollingRate.PR4000Hz == 0x02
    assert LiftOffDistance.Low == 1
    assert LiftOffDistance.High == 2


def test_dataclasses():
    prof = CpiProfile(x=1600, y=1600, enabled=True)
    assert prof.x == 1600
    status = MouseStatus(status="ok", level=85)
    assert status.level == 85
    assert status.status == "ok"

