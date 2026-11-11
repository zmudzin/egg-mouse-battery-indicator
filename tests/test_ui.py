import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from egg_mouse_battery_indicator.mouse_settings_dialog import MouseSettingsDialog
from egg_mouse_battery_indicator.protocol import (
    CpiProfile,
    MODEL_SPECS,
    MouseStatus,
    PollingRate,
    SPDT_GX_SAFE,
    SPDT_GX_SPEED,
    default_button_entries,
)


class _DummyDialog(MouseSettingsDialog):
    def _start_loading(self) -> None:
        # Skip hardware I/O in unit tests
        pass


class TestMouseSettingsDialogUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make_status(self, pid: int, wired: bool = False) -> MouseStatus:
        model = MODEL_SPECS[pid]
        btns = default_button_entries()
        btns[3].set_keyboard(0x01, 0x06)  # Ctrl+C
        btns[4].set_fixed_cpi(600, 400)
        return MouseStatus(
            status="ok",
            level=72,
            is_charging=wired,
            is_wired=wired,
            device_name=model.name,
            mouse_pid=pid,
            mouse_fw="1.10" if not model.is_v2 else "1.07",
            dongle_fw="" if wired else "1.01",
            active_cpi_profile=2,
            cpi_levels=3,
            cpi_profiles=[
                CpiProfile(400, 400, True),
                CpiProfile(800, 1200, True),
                CpiProfile(1600, 1600, True),
                CpiProfile(3200, 3200, False),
            ],
            polling_rate=PollingRate.PR2000Hz,
            lod_raw=1 if not model.is_v2 else 3,
            led_on_liftoff=False,
            motion_sync=True,
            ripple_control=False,
            angle_snapping=False,
            angle_tuning=-5 if model.has_angle_tuning else 0,
            slamclick_filter=True,
            motion_jitter_filter=model.has_motion_jitter,
            multiclick_ack=model.has_multiclick_ack,
            force_max_fps=model.has_force_max_fps,
            glass_mode=False,
            power_saving_enabled=True,
            power_saving_minutes=15,
            deep_sleep_enabled=True,
            deep_sleep_minutes=30,
            button_filters=[SPDT_GX_SPEED, SPDT_GX_SAFE, 7, 8, 8],
            button_entries=btns,
            settings_ok=True,
        )

    def test_v1_populate_and_roundtrip_collect(self):
        dlg = _DummyDialog("/dev/null")
        status = self._make_status(0x1968, wired=True)
        dlg._on_loaded_ok(status)

        self.assertEqual(dlg.combo_cpi_levels.currentData(), 3)
        self.assertTrue(dlg.chk_split_xy.isChecked())
        self.assertEqual(dlg._cpi_spins_x[1].value(), 800)
        self.assertEqual(dlg._cpi_spins_y[1].value(), 1200)
        self.assertFalse(dlg._cpi_radios[0].isEnabled())  # v1 disables software stage select
        self.assertTrue(dlg.combo_polling.isEnabled())    # user can update stored wireless polling rate even when wired
        self.assertFalse(dlg.lbl_polling_wired_note.isHidden())
        self.assertTrue(dlg.chk_disable_led_liftoff.isChecked())
        self.assertTrue(dlg.chk_motion_jitter.isEnabled())
        self.assertFalse(dlg.chk_glass_mode.isEnabled())
        self.assertFalse(dlg.spin_angle_tuning.isEnabled())
        self.assertEqual(dlg.spin_power_saving.value(), 15)
        self.assertEqual(dlg.spin_deep_sleep.value(), 30)

        # Verify tooltips are populated on key controls
        for w in (
            dlg.combo_polling,
            dlg.chk_motion_sync,
            dlg.chk_slamclick,
            dlg.chk_motion_jitter,
            dlg.chk_glass_mode,
            dlg.chk_force_max_fps,
            dlg.spin_angle_tuning,
            dlg.combo_lod,
            dlg.chk_angle_snap,
            dlg.chk_ripple,
            dlg._spdt_combos[0],
        ):
            self.assertTrue(bool(w.toolTip().strip()))

        collected = dlg._collect_changes()
        self.assertEqual(collected, dlg._initial_config)
        self.assertEqual(collected["polling_rate"], PollingRate.PR2000Hz)
        self.assertEqual(collected["button_filters"], [SPDT_GX_SPEED, SPDT_GX_SAFE, 7, 8, 8])
        self.assertEqual(collected["button_entries"][3].describe(), "Ctrl+C")
        self.assertEqual(collected["button_entries"][4].describe(), "Fixed CPI: 600x400")
        dlg.close()

    def test_v2_glass_mode_and_left_handed_toggle(self):
        dlg = _DummyDialog("/dev/null")
        status = self._make_status(0x1982, wired=False)
        dlg._on_loaded_ok(status)

        self.assertTrue(dlg._cpi_radios[0].isEnabled())
        self.assertTrue(dlg.combo_polling.isEnabled())
        self.assertTrue(dlg.chk_glass_mode.isEnabled())
        self.assertTrue(dlg.spin_angle_tuning.isEnabled())
        self.assertFalse(dlg.chk_motion_jitter.isEnabled())
        self.assertEqual(dlg.combo_lod.count(), 11)
        self.assertEqual(dlg.combo_lod.currentData(), 3)  # 1.0 mm

        # Toggle glass mode on -> LOD combo switches to 2 entries (1.0 mm, 2.0 mm)
        dlg.chk_glass_mode.setChecked(True)
        self.assertEqual(dlg.combo_lod.count(), 2)
        self.assertEqual(dlg.combo_lod.currentData(), 1)

        # Toggle left-handed mode -> Left & Right swap
        dlg.chk_left_handed.setChecked(True)
        collected = dlg._collect_changes()
        self.assertEqual(collected["button_entries"][0].code, 0x02)
        self.assertEqual(collected["button_entries"][1].code, 0x01)
        dlg.close()


if __name__ == "__main__":
    unittest.main()

