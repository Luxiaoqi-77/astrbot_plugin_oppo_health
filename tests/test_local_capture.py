import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import local_capture


class LocalCaptureTests(unittest.TestCase):
    def test_screenshot_bytes_are_piped_to_vision_without_a_png_file(self):
        payload = {"width": 10, "height": 20, "observations": []}
        completed = local_capture.subprocess.CompletedProcess(
            args=["vision"], returncode=0,
            stdout=json.dumps(payload).encode("utf-8"), stderr=b"",
        )
        with tempfile.TemporaryDirectory() as temp_name:
            with patch.object(local_capture, "_adb", return_value=b"\x89PNG\r\n\x1a\nimage"):
                with patch.object(local_capture.subprocess, "run", return_value=completed) as run:
                    screen = local_capture._capture(Path("/unused/vision"))
            self.assertEqual(screen, payload)
            self.assertTrue(run.call_args.kwargs["input"].startswith(b"\x89PNG"))
            self.assertEqual(list(Path(temp_name).iterdir()), [])

    def test_temporary_vision_directory_is_removed_after_label_review(self):
        original_factory = tempfile.TemporaryDirectory
        created_dirs = []

        def make_tempdir(*args, **kwargs):
            directory = original_factory(*args, **kwargs)
            created_dirs.append(Path(directory.name))
            return directory

        screen = {
            "width": 100,
            "height": 200,
            "observations": [
                {"text": "Calendar", "bbox": [0.1, 0.1, 0.1, 0.1]},
                {"text": "Cycle", "bbox": [0.2, 0.1, 0.1, 0.1]},
                {"text": "Predicted period", "bbox": [0.3, 0.1, 0.2, 0.1]},
                {"text": "15 min", "bbox": [0.3, 0.1, 0.2, 0.1]},
            ],
        }
        with patch.object(local_capture, "ADB", Path(local_capture.__file__)):
            with patch.object(local_capture, "ADB_SERIAL", "emulator-5554"):
                with patch.object(local_capture, "_assert_foreground"):
                    with patch.object(local_capture, "_compile_vision", side_effect=lambda path: path / "vision"):
                        with patch.object(local_capture, "_capture", return_value=screen):
                            with patch.object(local_capture.tempfile, "TemporaryDirectory", side_effect=make_tempdir):
                                result = local_capture.collect(["cycle_calendar"], labels_only=True)
        self.assertEqual(result["labels"], ["Calendar", "Cycle", "Predicted period"])
        self.assertEqual(len(created_dirs), 1)
        self.assertFalse(created_dirs[0].exists())

    def test_adb_refuses_unconfigured_or_physical_device_targets(self):
        with patch.object(local_capture, "ADB", None):
            with self.assertRaisesRegex(local_capture.CaptureError, "adb_not_configured"):
                local_capture._adb("devices")
        with patch.object(local_capture, "ADB", "adb"):
            with patch.object(local_capture, "ADB_SERIAL", "device-serial"):
                with patch.object(local_capture.subprocess, "run") as run:
                    with self.assertRaisesRegex(local_capture.CaptureError, "emulator_serial_not_configured"):
                        local_capture._adb("devices")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
