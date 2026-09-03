from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration_store import CalibrationStore
from main_controller import (
    ControllerConfig,
    FrameDecoder,
    MainController,
    build_frame,
    decode_telemetry,
)
from pc_main_math import correlation_coefficient, crc16_modbus, linear_fit, linear_values


class PcMainMigrationTests(unittest.TestCase):
    def test_legacy_frame_layout(self) -> None:
        frame = build_frame(0xB2, bytes((0x01, 0x12, 0x34, 0x1F, 0x40)))
        self.assertEqual(frame, bytes.fromhex("AAB20112341F400000000000000000FF"))

    def test_rs485_speed_uses_the_documented_b5_frame(self) -> None:
        class SocketRecorder:
            def __init__(self) -> None:
                self.frames: list[bytes] = []

            def sendall(self, frame: bytes) -> None:
                self.frames.append(frame)

        controller = MainController(ControllerConfig(enabled=True, host="192.168.4.2", port=1200))
        recorder = SocketRecorder()
        controller._socket = recorder  # type: ignore[assignment]  # isolates wire-format testing
        controller._set_state(connected=True)
        controller.set_rs485_speed(30000)
        self.assertEqual(recorder.frames, [bytes.fromhex("AAB575300000000000000000000000FF")])

    def test_decoder_handles_fragmented_stream_and_noise(self) -> None:
        frame = build_frame(0xB1, b"\x01\x00")
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(b"noise" + frame[:5]), [])
        self.assertEqual(decoder.feed(frame[5:] + frame), [frame, frame])
        self.assertEqual(decoder.discarded_bytes, len(b"noise"))

    def test_telemetry_uses_big_endian_value_minus_offset(self) -> None:
        payload = (3700).to_bytes(2, "big") + (3500).to_bytes(2, "big") + (7200).to_bytes(2, "big") + b"\x52"
        telemetry = decode_telemetry(build_frame(0x01, payload))
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry.pitch, 1.0)
        self.assertEqual(telemetry.roll, -1.0)
        self.assertEqual(telemetry.yaw, 36.0)
        self.assertEqual(telemetry.battery_adc, 0x52)

    def test_calibration_store_replaces_all_legacy_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = CalibrationStore(Path(temporary_directory) / "calibration.sqlite3")
            rows = [
                {"通道": f"CH{channel}", "系数用途": f"用途{channel}", "通道增益": channel, "通道漂移": -channel}
                for channel in range(1, 36)
            ]
            items = store.import_pc_main_rows(rows)
        self.assertEqual(len(items), 35)
        self.assertEqual(items[0]["purpose"], "用途1")
        self.assertEqual(items[-1]["gain"], 35.0)
        self.assertEqual(items[-1]["offset"], -35.0)

    def test_math_utilities_match_expected_values(self) -> None:
        self.assertEqual(crc16_modbus(b"123456789"), 0x4B37)
        slope, intercept = linear_fit([1, 2, 3], [3, 5, 7])
        self.assertEqual((slope, intercept), (2.0, 1.0))
        self.assertEqual(linear_values([0, 2], slope, intercept), [1.0, 5.0])
        self.assertEqual(correlation_coefficient([1, 2, 3], [4, 5, 6]), 1.0)


if __name__ == "__main__":
    unittest.main()
