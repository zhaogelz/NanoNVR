import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from recorder import (
    ManagedFfmpegProcess,
    MonitorApp,
    StorageProtectionError,
    WindowsProcessJob,
    build_cleanup_plan,
)


class CleanupPlanTests(unittest.TestCase):
    def test_quota_uses_proportional_ten_percent_band_without_fixed_cap(self):
        plan = build_cleanup_plan(
            quota_gib=350.0,
            managed_gib=350.0,
            volume_total_gib=1000.0,
            volume_free_gib=200.0,
        )

        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan.healthy_gib, 315.0)
        self.assertAlmostEqual(plan.required_release_gib, 35.0)
        self.assertAlmostEqual(plan.target_managed_gib, 315.0)
        self.assertTrue(plan.quota_triggered)
        self.assertFalse(plan.volume_triggered)

    def test_no_cleanup_below_both_watermarks(self):
        plan = build_cleanup_plan(
            quota_gib=350.0,
            managed_gib=300.0,
            volume_total_gib=1000.0,
            volume_free_gib=200.0,
        )

        self.assertIsNone(plan)

    def test_volume_floor_triggers_independently_and_still_batches_by_quota(self):
        plan = build_cleanup_plan(
            quota_gib=350.0,
            managed_gib=300.0,
            volume_total_gib=1000.0,
            volume_free_gib=40.0,
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.volume_floor_gib, 50.0)
        self.assertAlmostEqual(plan.required_release_gib, 35.0)
        self.assertAlmostEqual(plan.target_managed_gib, 265.0)
        self.assertFalse(plan.quota_triggered)
        self.assertTrue(plan.volume_triggered)

    def test_volume_shortage_can_require_more_than_ten_percent(self):
        plan = build_cleanup_plan(
            quota_gib=350.0,
            managed_gib=300.0,
            volume_total_gib=1000.0,
            volume_free_gib=5.0,
        )

        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan.required_release_gib, 45.0)
        self.assertAlmostEqual(plan.target_managed_gib, 255.0)


class ManagedFfmpegProcessTests(unittest.TestCase):
    def test_stderr_is_drained_and_stop_is_idempotent(self):
        script = (
            "import sys,time\n"
            "for i in range(3000): print(f'error-{i}', file=sys.stderr)\n"
            "sys.stderr.flush()\n"
            "time.sleep(30)\n"
        )
        raw = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        managed = ManagedFfmpegProcess(raw)

        deadline = time.time() + 5
        while "error-2999" not in managed.recent_errors() and time.time() < deadline:
            time.sleep(0.02)

        self.assertIn("error-2999", managed.recent_errors())
        managed.stop()
        managed.stop()
        self.assertIsNotNone(managed.poll())

    @unittest.skipUnless(os.name == "nt", "仅 Windows 支持 Job Object")
    def test_windows_job_terminates_child_when_closed(self):
        job = WindowsProcessJob()
        if not job.available:
            self.skipTest(job.error or "Windows Job Object 不可用")
        raw = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(job.assign(raw))
            job.close()
            raw.wait(timeout=5)
            self.assertIsNotNone(raw.returncode)
        finally:
            if raw.poll() is None:
                raw.kill()


class StorageCleanupTests(unittest.TestCase):
    def _make_app(self, save_dir):
        app = MonitorApp.__new__(MonitorApp)
        app.is_recording = True
        app.save_dir = Path(save_dir)
        app._lease_lock = threading.Lock()
        app._leased_segments = set()
        app._get_volume_usage_gib = lambda: (1000.0, 200.0)
        app.log_messages = []
        app.log = app.log_messages.append
        return app

    @staticmethod
    def _write_segment(path, size, age_seconds):
        path.write_bytes(b"x" * size)
        timestamp = time.time() - age_seconds
        os.utime(path, (timestamp, timestamp))

    def test_cleanup_skips_export_lease_and_two_newest_segments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            day = Path(temp_dir) / "2026-09-01"
            day.mkdir()
            leased = day / "00_00_00.ts"
            candidate = day / "00_15_00.ts"
            newest_one = day / "00_30_00.ts"
            newest_two = day / "00_45_00.ts"
            self._write_segment(leased, 50_000, 1000)
            self._write_segment(candidate, 60_000, 900)
            self._write_segment(newest_one, 10_000, 800)
            self._write_segment(newest_two, 10_000, 700)

            app = self._make_app(temp_dir)
            app._leased_segments.add(leased.resolve())
            app.clean_old_files(0.0001)

            self.assertTrue(leased.exists())
            self.assertFalse(candidate.exists())
            self.assertTrue(newest_one.exists())
            self.assertTrue(newest_two.exists())

    def test_cleanup_fails_closed_when_exports_leave_volume_below_floor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            exports = Path(temp_dir) / "exports"
            exports.mkdir()
            (exports / "protected.mp4").write_bytes(b"x" * 200_000)
            app = self._make_app(temp_dir)
            app._get_volume_usage_gib = lambda: (1000.0, 40.0)

            with self.assertRaises(StorageProtectionError):
                app.clean_old_files(0.0001)


class LogSafetyTests(unittest.TestCase):
    def test_rtsp_password_is_redacted(self):
        value = "rtsp://admin:secret@192.168.1.10/stream1"
        self.assertEqual(
            MonitorApp._redact_rtsp_url(value),
            "rtsp://admin:***@192.168.1.10/stream1",
        )


if __name__ == "__main__":
    unittest.main()
