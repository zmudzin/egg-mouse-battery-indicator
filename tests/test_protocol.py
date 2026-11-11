import struct
import unittest

from egg_mouse_battery_indicator.protocol import (
    ALL_SUPPORTED_PIDS,
    ActionType,
    ButtonEntry,
    CMD_BATTERY,
    CMD_BUTTONS,
    CMD_FACTORY_RESET,
    CMD_PAIR,
    CMD_PERF,
    CMD_PROBE,
    CMD_READ_BLOB,
    CMD_SENSOR,
    CpiProfile,
    FLAG_FORCE_MAX_FPS,
    FLAG_MOTION_JITTER,
    FLAG_MULTICLICK_ACK,
    FLAG_SLAMCLICK,
    LiftOffDistance,
    LodEncoding,
    MODEL_SPECS,
    MouseStatus,
    PollingRate,
    PowerOffSetting,
    SPDT_GX_SAFE,
    SPDT_GX_SPEED,
    _HIDIOCGFEATURE,
    _HIDIOCSFEATURE,
    decode_config_blob,
    default_button_entries,
    effective_lod_encoding,
    encode_button_chunks,
    encode_perf_payload,
    encode_sensor_payload,
    is_left_handed,
    lod_convert_for_glass_mode,
    lod_options,
    parse_dongle_info_payload,
    parse_mouse_info_payload,
    parse_notify_packet,
    quantize_cpi,
    set_left_handed,
)


def test_ioctls():
    assert _HIDIOCSFEATURE(64) == 0xC0404806
    assert _HIDIOCGFEATURE(64) == 0xC0404807


def test_pids():
    assert 0x1970 in ALL_SUPPORTED_PIDS
    assert 0x1968 in ALL_SUPPORTED_PIDS
    assert 0x1972 in ALL_SUPPORTED_PIDS
    assert 0x1982 in ALL_SUPPORTED_PIDS
    assert 0x1984 in ALL_SUPPORTED_PIDS


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


class TestProtocolCodecs(unittest.TestCase):
    def test_legacy_functions(self):
        test_ioctls()
        test_pids()
        test_enums()
        test_dataclasses()

    def _build_sample_blob(self) -> bytearray:
        b = bytearray(1024)
        b[0x01] = 0xF6  # -10 deg angle tuning
        b[0x03] = 15    # deep sleep 15 min
        b[0x04] = 0x80 | 5  # power saving disabled (5 min stored)
        b[0x05] = PollingRate.PR4000Hz
        b[0x06] = FLAG_SLAMCLICK | FLAG_FORCE_MAX_FPS | 0x02  # plus unknown bit 0x02
        b[0x07] = 0x42  # unknown0
        b[0x08] = 0x00  # led_on_liftoff = False (Disable LED on Lift-Off checked)
        b[0x09] = 8     # 1.5 mm on v2 tenths scale
        b[0x0A] = 1     # angle snapping on
        b[0x0B] = 1     # ripple control on
        b[0x0C] = 1     # motion sync on
        b[0x0D] = 2     # stage 3 (0-based 2)
        b[0x0E] = 3     # 3 CPI levels

        cpis = [(410, 410), (800, 1200), (1600, 1600), (3200, 3200)]
        for i, (x, y) in enumerate(cpis):
            base = 0x23 + i * 5
            b[base] = 1 if x != y else 0
            struct.pack_into("<HH", b, base + 1, x, y)

        btns = default_button_entries()
        btns[0].multiclick_filter = SPDT_GX_SPEED
        btns[1].multiclick_filter = SPDT_GX_SAFE
        btns[2].multiclick_filter = 5
        btns[3].set_keyboard(0x03, 0x06)  # Back -> Ctrl+Shift+C
        btns[4].set_fixed_cpi(600, 400)   # Forward -> Fixed CPI 600x400
        for i, e in enumerate(btns):
            base = 0x37 + i * 7
            b[base] = e.action_type
            b[base + 1] = e.code
            b[base + 2] = e.key
            b[base + 3 : base + 6] = e.rest
            b[base + 6] = e.multiclick_filter

        b[0x6F] = 0  # glass mode off
        return b

    def test_decode_blob_reads_actual_cpi_and_all_fields(self):
        blob = bytes(self._build_sample_blob())
        v2 = MODEL_SPECS[0x1982]  # XM2w 4k v2
        ms = decode_config_blob(blob, model=v2)

        self.assertEqual(ms.cpi_levels, 3)
        self.assertEqual(ms.active_cpi_profile, 3)
        self.assertEqual(ms.cpi_profiles[0].x, 410)
        self.assertEqual(ms.cpi_profiles[0].y, 410)
        self.assertEqual(ms.cpi_profiles[1].x, 800)
        self.assertEqual(ms.cpi_profiles[1].y, 1200)
        self.assertTrue(ms.cpi_profiles[0].enabled)
        self.assertTrue(ms.cpi_profiles[2].enabled)
        self.assertFalse(ms.cpi_profiles[3].enabled)

        self.assertEqual(ms.angle_tuning, -10)
        self.assertEqual(ms.unknown0, 0x42)
        self.assertFalse(ms.led_on_liftoff)
        self.assertEqual(ms.lod_raw, 8)
        self.assertTrue(ms.angle_snapping)
        self.assertTrue(ms.ripple_control)
        self.assertTrue(ms.motion_sync)
        self.assertTrue(ms.slamclick_filter)
        self.assertTrue(ms.force_max_fps)
        self.assertFalse(ms.power_saving_enabled)
        self.assertEqual(ms.power_saving_minutes, 5)
        self.assertTrue(ms.deep_sleep_enabled)
        self.assertEqual(ms.deep_sleep_minutes, 15)

        self.assertEqual(ms.button_filters, [SPDT_GX_SPEED, SPDT_GX_SAFE, 5, 8, 8])
        self.assertEqual(ms.button_entries[3].describe(), "Ctrl+Shift+C")
        self.assertEqual(ms.button_entries[4].describe(), "Fixed CPI: 600x400")

    def test_encode_sensor_v1_vs_v2(self):
        blob = bytes(self._build_sample_blob())
        v1 = MODEL_SPECS[0x1968]  # XM2w 4k
        ms_v1 = decode_config_blob(blob, model=v1)
        ms_v1.active_cpi_profile = 4  # Should NOT overwrite raw_active_stage on v1
        ms_v1.angle_tuning = 15
        p_v1 = encode_sensor_payload(ms_v1, v1)
        self.assertEqual(p_v1[0], 0x42)  # preserved unknown0
        self.assertEqual(p_v1[5], 0x00)  # v1 clears angle tuning
        self.assertEqual(p_v1[7], 2)     # echoed raw_active_stage
        # v1 rounds CPI 410 -> 400 (nearest 50)
        x0, y0 = struct.unpack_from("<HH", p_v1, 8 + 1)
        self.assertEqual((x0, y0), (400, 400))

        v2 = MODEL_SPECS[0x1982]
        ms_v2 = decode_config_blob(blob, model=v2)
        ms_v2.active_cpi_profile = 4
        ms_v2.angle_tuning = -10
        p_v2 = encode_sensor_payload(ms_v2, v2)
        self.assertEqual(p_v2[5], 0xF6)
        self.assertEqual(p_v2[6], 3)
        self.assertEqual(p_v2[7], 3)
        x0_v2, _ = struct.unpack_from("<HH", p_v2, 8 + 1)
        self.assertEqual(x0_v2, 410)

    def test_encode_perf_v1_vs_v2_preserves_unknown_bits(self):
        blob = bytes(self._build_sample_blob())
        v1 = MODEL_SPECS[0x1968]
        ms_v1 = decode_config_blob(blob, model=v1)
        ms_v1.motion_jitter_filter = True
        perf_v1 = encode_perf_payload(ms_v1, v1)
        self.assertEqual(len(perf_v1), 10)
        self.assertTrue(perf_v1[2] & FLAG_MOTION_JITTER)
        self.assertTrue(perf_v1[2] & 0x02)  # preserved unknown bit
        self.assertEqual(perf_v1[4], SPDT_GX_SPEED)
        self.assertEqual(perf_v1[5], SPDT_GX_SAFE)

        v2 = MODEL_SPECS[0x1982]
        ms_v2 = decode_config_blob(blob, model=v2)
        ms_v2.glass_mode = True
        perf_v2 = encode_perf_payload(ms_v2, v2)
        self.assertEqual(len(perf_v2), 11)
        self.assertEqual(perf_v2[10], 1)
        self.assertEqual(perf_v2[9], 15)
        self.assertEqual(perf_v2[3], 0x80 | 5)

    def test_button_chunks_and_left_handed(self):
        btns = default_button_entries()
        btns[4].set_fixed_cpi(1200, 800)
        swapped = set_left_handed(btns, True)
        self.assertTrue(is_left_handed(swapped))
        self.assertEqual(swapped[0].code, 0x02)
        self.assertEqual(swapped[1].code, 0x01)
        # Physical filters stay aligned when synced
        c1, c2 = encode_button_chunks(swapped, [SPDT_GX_SPEED, 8, 6, 8, 8])
        self.assertEqual(len(c1), 28)
        self.assertEqual(len(c2), 28)
        self.assertEqual(c1[6], SPDT_GX_SPEED)
        self.assertEqual(c1[13], 8)
        # Forward (entry 4, in chunk 2 offset 0) keeps Fixed CPI intact
        self.assertEqual(c2[0], ActionType.CPI_PRESET)
        self.assertEqual(c2[1], 1)  # xy_split = 1
        x = c2[2] | (c2[3] << 8)
        y = c2[4] | (c2[5] << 8)
        self.assertEqual((x, y), (1200, 800))

    def test_lod_glass_mode_conversions(self):
        v2 = MODEL_SPECS[0x1982]
        self.assertEqual(effective_lod_encoding(v2, False), LodEncoding.TENTHS_FROM_07)
        self.assertEqual(effective_lod_encoding(v2, True), LodEncoding.MILLIMETRES)
        self.assertEqual(len(lod_options(LodEncoding.TENTHS_FROM_07)), 11)
        self.assertEqual(lod_convert_for_glass_mode(3, True), 1)
        self.assertEqual(lod_convert_for_glass_mode(8, True), 2)
        self.assertEqual(lod_convert_for_glass_mode(1, False), 3)
        self.assertEqual(lod_convert_for_glass_mode(2, False), 10)

    def test_quantize_cpi(self):
        v1 = MODEL_SPECS[0x1968]
        v2 = MODEL_SPECS[0x1982]
        self.assertEqual(quantize_cpi(30000, v1), 26000)
        self.assertEqual(quantize_cpi(430, v1), 450)
        self.assertEqual(quantize_cpi(12, v2), 10)
        self.assertEqual(quantize_cpi(10026, v2), 10050)

    def test_firmware_version_and_notify_parsing(self):
        payload = bytes([0x67, 0x33, 0x68, 0x19, 0x67, 0x19, 0x01, 0x10])
        vid, pid, fw = parse_mouse_info_payload(payload)
        self.assertEqual((vid, pid, fw), (0x3367, 0x1968, "1.10"))
        self.assertEqual(parse_dongle_info_payload(bytes([0x01, 0x01])), "1.01")

        self.assertEqual(
            parse_notify_packet(bytes([0x03, 0xB4, 75, 0xE8, 0x0F, 0, 0, 0]))["level"],
            75,
        )
        self.assertTrue(
            parse_notify_packet(bytes([0x03, 0xB1, 0x01, 0, 0, 0, 0, 0]))["awake"]
        )
        self.assertFalse(
            parse_notify_packet(bytes([0x03, 0xB1, 0xF0, 0x0A, 0, 0, 0, 0]))["awake"]
        )
        self.assertEqual(
            parse_notify_packet(bytes([0x03, 0x06, 0x02, 0, 0, 0, 0, 0]))["rate"],
            PollingRate.PR4000Hz,
        )
        self.assertEqual(
            parse_notify_packet(bytes([0x03, 0x02, 0x03, 0, 0, 0, 0, 0]))["stage"],
            4,
        )


if __name__ == "__main__":
    unittest.main()
