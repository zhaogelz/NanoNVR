import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from recorder import (
    ManagedFfmpegProcess,
    MonitorApp,
    RecordingProgressWatchdog,
    StorageProtectionError,
    WindowsProcessJob,
    build_cleanup_plan,
    validate_recording_quota,
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


class RecordingQuotaValidationTests(unittest.TestCase):
    def test_rejects_non_finite_and_non_positive_values(self):
        for value in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
            with self.subTest(value=value):
                self.assertIsNotNone(validate_recording_quota(value, 1000.0))

    def test_reserves_larger_of_twenty_gib_or_five_percent(self):
        self.assertIsNone(validate_recording_quota(950.0, 1000.0))
        error = validate_recording_quota(950.01, 1000.0)

        self.assertIsNotNone(error)
        self.assertIn("最多可设为 950.00 GiB", error)

    def test_small_volume_cannot_bypass_twenty_gib_floor(self):
        error = validate_recording_quota(1.0, 10.0)

        self.assertIsNotNone(error)
        self.assertIn("最多可设为 0.00 GiB", error)


class RecordingProgressWatchdogTests(unittest.TestCase):
    def test_stalls_when_no_file_appears_before_timeout(self):
        watchdog = RecordingProgressWatchdog(timeout_seconds=180)
        watchdog.start(100.0)

        self.assertFalse(watchdog.observe(None, 279.9))
        self.assertTrue(watchdog.observe(None, 280.0))

    def test_new_file_growth_resets_timeout(self):
        watchdog = RecordingProgressWatchdog(timeout_seconds=180)
        watchdog.start(100.0)

        self.assertFalse(watchdog.observe(("a.ts", 100, 1), 200.0))
        self.assertFalse(watchdog.observe(("a.ts", 200, 2), 370.0))
        self.assertFalse(watchdog.observe(("a.ts", 200, 2), 549.9))
        self.assertTrue(watchdog.observe(("a.ts", 200, 2), 550.0))


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

    def test_export_excludes_actual_latest_filename_and_leases_the_rest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            date_str = datetime.now().strftime("%Y-%m-%d")
            day = Path(temp_dir) / date_str
            day.mkdir()
            complete = day / "17_41_13.ts"
            active = day / "17_56_13.ts"
            self._write_segment(complete, 10_000, 20)
            self._write_segment(active, 10_000, 10)
            app = self._make_app(temp_dir)

            chosen, leased, excluded = app._select_and_lease_export_segments(
                date_str, "00:00:00", "23:59:59"
            )

            self.assertEqual(chosen, [complete])
            self.assertEqual(excluded, active.resolve())
            self.assertIn(complete.resolve(), app._leased_segments)
            app._release_segment_leases(leased)
            self.assertNotIn(complete.resolve(), app._leased_segments)

    def test_cleanup_rechecks_lease_atomically_before_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            day = Path(temp_dir) / "2026-09-01"
            day.mkdir()
            candidate = day / "00_00_00.ts"
            newest_one = day / "00_15_00.ts"
            newest_two = day / "00_30_00.ts"
            self._write_segment(candidate, 100_000, 1000)
            self._write_segment(newest_one, 10_000, 900)
            self._write_segment(newest_two, 10_000, 800)
            app = self._make_app(temp_dir)
            cleanup_started = threading.Event()
            errors = []

            def log(message):
                if "开始批量删除" in message:
                    cleanup_started.set()

            def run_cleanup():
                try:
                    app.clean_old_files(0.00005)
                except StorageProtectionError as exc:
                    errors.append(exc)

            app.log = log
            with app._lease_lock:
                worker = threading.Thread(target=run_cleanup)
                worker.start()
                self.assertTrue(cleanup_started.wait(timeout=2))
                app._leased_segments.add(candidate.resolve())
            worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertTrue(candidate.exists())
            self.assertEqual(len(errors), 1)


class SaveDirectoryValidationTests(unittest.TestCase):
    class EntryStub:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    def test_explicit_unavailable_directory_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            blocking_file = Path(temp_dir) / "not-a-directory"
            blocking_file.write_text("x", encoding="utf-8")
            original = Path(temp_dir) / "original"
            app = MonitorApp.__new__(MonitorApp)
            app.entry_save_dir = self.EntryStub(str(blocking_file / "child"))
            app.save_dir = original
            messages = []
            app.log = messages.append

            self.assertFalse(app.refresh_save_dir())
            self.assertEqual(app.save_dir, original)
            self.assertTrue(any("已阻止" in message for message in messages))

    def test_empty_directory_setting_keeps_documented_program_default(self):
        app = MonitorApp.__new__(MonitorApp)
        app.entry_save_dir = self.EntryStub("")
        app.save_dir = Path("unused")

        self.assertTrue(app.refresh_save_dir())
        self.assertEqual(app.save_dir, Path(__file__).parents[1])


class LogSafetyTests(unittest.TestCase):
    def test_rtsp_password_is_redacted(self):
        value = "rtsp://admin:secret@192.168.1.10/stream1"
        self.assertEqual(
            MonitorApp._redact_rtsp_url(value),
            "rtsp://admin:***@192.168.1.10/stream1",
        )


if __name__ == "__main__":
    unittest.main()
