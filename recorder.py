import os
import sys
import subprocess
import time
import threading
import json
import math
import re
import shutil
import tempfile
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from tkinter import messagebox, scrolledtext, ttk, filedialog
import webbrowser
from datetime import datetime
from pathlib import Path

# ====== 固定内部参数 ======
SEGMENT_DURATION = 900
RETRY_INTERVAL = 10
CLEAN_INTERVAL = 300
POLL_INTERVAL = 10
DELETE_SAFE_SECONDS = 120
FFMPEG_TIMEOUT_US = "120000000"
WRITE_STALL_TIMEOUT = 180
HEALTHY_WATERMARK_RATIO = 0.90
MIN_VOLUME_FREE_GIB = 20.0
MIN_VOLUME_FREE_RATIO = 0.05
PROTECTED_NEWEST_SEGMENTS = 2

# 获取正确的运行目录（兼容 PyInstaller 独立 exe 运行方式）
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent.absolute()
else:
    BASE_DIR = Path(__file__).parent.absolute()

CONFIG_FILE = BASE_DIR / "config.json"


class StorageProtectionError(RuntimeError):
    """无法恢复到安全存储水位时停止录像，避免继续写满磁盘。"""


@dataclass(frozen=True)
class CleanupPlan:
    """一次容量保护操作的不可变计划。"""

    quota_gib: float
    healthy_gib: float
    managed_gib: float
    volume_free_gib: float
    volume_floor_gib: float
    target_managed_gib: float
    required_release_gib: float
    quota_triggered: bool
    volume_triggered: bool

    @property
    def reason(self) -> str:
        if self.quota_triggered and self.volume_triggered:
            return "录像达到配额且磁盘余量不足"
        if self.quota_triggered:
            return "录像达到配额"
        return "磁盘余量不足"


def build_cleanup_plan(quota_gib, managed_gib, volume_total_gib, volume_free_gib):
    """按录像配额计算高低水位，并用真实卷余量提供独立兜底。"""
    healthy_gib = quota_gib * HEALTHY_WATERMARK_RATIO
    volume_floor_gib = max(MIN_VOLUME_FREE_GIB, volume_total_gib * MIN_VOLUME_FREE_RATIO)
    quota_triggered = managed_gib >= quota_gib
    volume_triggered = volume_free_gib < volume_floor_gib
    if not quota_triggered and not volume_triggered:
        return None

    required_release_gib = 0.0
    if quota_triggered:
        required_release_gib = max(required_release_gib, managed_gib - healthy_gib)
    if volume_triggered:
        # 余量告急时也按配额的 10% 成批释放，避免刚越线就反复删除。
        required_release_gib = max(
            required_release_gib,
            volume_floor_gib - volume_free_gib,
            quota_gib * (1.0 - HEALTHY_WATERMARK_RATIO),
        )

    required_release_gib = min(managed_gib, required_release_gib)
    return CleanupPlan(
        quota_gib=quota_gib,
        healthy_gib=healthy_gib,
        managed_gib=managed_gib,
        volume_free_gib=volume_free_gib,
        volume_floor_gib=volume_floor_gib,
        target_managed_gib=max(0.0, managed_gib - required_release_gib),
        required_release_gib=required_release_gib,
        quota_triggered=quota_triggered,
        volume_triggered=volume_triggered,
    )


def validate_recording_quota(quota_gib, volume_total_gib):
    """返回配额错误文案；合法时返回 None。"""
    if not math.isfinite(quota_gib) or quota_gib <= 0:
        return "录像配额必须是大于 0 的有限数字"

    volume_floor_gib = max(MIN_VOLUME_FREE_GIB, volume_total_gib * MIN_VOLUME_FREE_RATIO)
    safe_limit_gib = max(0.0, volume_total_gib - volume_floor_gib)
    if quota_gib > safe_limit_gib:
        return (
            f"当前磁盘总容量约 {volume_total_gib:.2f} GiB，需要保留 {volume_floor_gib:.2f} GiB "
            f"安全余量；录像配额最多可设为 {safe_limit_gib:.2f} GiB"
        )
    return None


class RecordingProgressWatchdog:
    """只关心录像是否持续产生新字节，不把“进程存活”误当成“正在录像”。"""

    def __init__(self, timeout_seconds=WRITE_STALL_TIMEOUT):
        self.timeout_seconds = timeout_seconds
        self.last_signature = None
        self.last_progress_at = None

    def start(self, now):
        self.last_signature = None
        self.last_progress_at = now

    def observe(self, signature, now):
        if self.last_progress_at is None:
            self.start(now)
        if signature is not None and signature != self.last_signature:
            self.last_signature = signature
            self.last_progress_at = now
            return False
        return now - self.last_progress_at >= self.timeout_seconds


class ManagedFfmpegProcess:
    """持续抽干错误输出，并提供幂等、分级的 FFmpeg 停止接口。"""

    def __init__(self, process):
        self.process = process
        self._errors = deque(maxlen=200)
        self._stop_lock = threading.Lock()
        self._drain_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drain_thread.start()

    def _drain_stderr(self):
        try:
            for line in self.process.stderr or ():
                line = line.strip()
                if line:
                    self._errors.append(line)
        except Exception:
            pass

    def poll(self):
        return self.process.poll()

    def recent_errors(self, max_chars=1000):
        text = "\n".join(self._errors)
        return text[-max_chars:]

    def _close_pipes(self):
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass

    def stop(self):
        with self._stop_lock:
            if self.process.poll() is not None:
                self._drain_thread.join(timeout=1)
                self._close_pipes()
                return

            try:
                if self.process.stdin:
                    self.process.stdin.write("q\n")
                    self.process.stdin.flush()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=3)
                except Exception:
                    try:
                        self.process.kill()
                        self.process.wait(timeout=3)
                    except Exception:
                        pass
            self._drain_thread.join(timeout=1)
            self._close_pipes()


class WindowsProcessJob:
    """把子进程放进随主程序关闭的 Windows Job，防止异常退出后继续写盘。"""

    KILL_ON_JOB_CLOSE = 0x00002000
    EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self):
        self._handle = None
        self._kernel32 = None
        self._lock = threading.Lock()
        self.error = None
        if os.name != "nt":
            return

        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimitInformation),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())

            info = ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                self.EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                error = ctypes.WinError(ctypes.get_last_error())
                kernel32.CloseHandle(handle)
                raise error

            self._kernel32 = kernel32
            self._handle = handle
        except Exception as exc:
            self.error = str(exc)

    @property
    def available(self):
        return self._handle is not None

    def assign(self, process):
        if not self.available:
            return False
        from ctypes import wintypes

        with self._lock:
            if not self._handle:
                return False
            if not self._kernel32.AssignProcessToJobObject(
                self._handle,
                wintypes.HANDLE(process._handle),
            ):
                import ctypes

                raise ctypes.WinError(ctypes.get_last_error())
            return True

    def close(self):
        with self._lock:
            if self._handle:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None

class MonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NanoNVR")
        self.root.geometry("640x480")
        self.root.minsize(540, 440)
        
        self.is_recording = False
        self.record_thread = None
        self.current_process = None
        self.exporting = False
        self.closing = False
        self._process_lock = threading.Lock()
        self._lease_lock = threading.Lock()
        self._leased_segments = set()
        self._process_job = WindowsProcessJob()
        self.save_dir = BASE_DIR  # 录像与导出的保存根目录（可由用户配置，默认=程序所在目录）
        
        self.setup_ui()
        if self._process_job.error:
            self.log(f"子进程防遗留保护未启用: {self._process_job.error}")
        # 使用 root.after 确保 UI 完全渲染完再填入配置
        self.root.after(100, self.load_config)
        # 绑定关闭窗口事件，退出时也自动保存一次
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_ui(self):
        # 参数配置区
        frame_config = tk.Frame(self.root, pady=10, padx=10)
        frame_config.pack(fill=tk.X)

        tk.Label(frame_config, text="RTSP 流地址:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.entry_rtsp = tk.Entry(frame_config, width=50)
        self.entry_rtsp.grid(row=0, column=1, sticky=tk.W, pady=5, padx=5)

        tk.Label(frame_config, text="录像配额(GiB):").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.entry_max_gb = tk.Entry(frame_config, width=15)
        self.entry_max_gb.grid(row=1, column=1, sticky=tk.W, pady=5, padx=5)
        self.entry_max_gb.insert(0, "150")

        tk.Label(
            frame_config,
            text="达到配额后自动清理至 90%，并保留磁盘安全余量",
            fg="gray",
        ).grid(row=2, column=1, sticky=tk.W, padx=5)

        # 保存目录（ts 录像分片与 mp4 导出都落在此目录下，留空=程序所在目录）
        tk.Label(frame_config, text="保存目录:").grid(row=3, column=0, sticky=tk.W, pady=5)
        frame_save = tk.Frame(frame_config)
        frame_save.grid(row=3, column=1, sticky=tk.W, pady=5, padx=5)
        self.entry_save_dir = tk.Entry(frame_save, width=44)
        self.entry_save_dir.pack(side=tk.LEFT)
        tk.Button(frame_save, text="浏览...", command=self.browse_save_dir).pack(side=tk.LEFT, padx=5)

        # 启动 / 导出按键
        frame_btns = tk.Frame(frame_config)
        frame_btns.grid(row=4, column=0, columnspan=2, pady=10)

        self.btn_start = tk.Button(frame_btns, text="▶ 启动录制", command=self.toggle_recording, width=15, bg="green", fg="white", font=("", 10, "bold"))
        self.btn_start.pack(side=tk.LEFT, padx=5)

        self.btn_export = tk.Button(frame_btns, text="📤 导出录像", command=self.open_export_dialog, width=15, bg="#1976D2", fg="white", font=("", 10, "bold"))
        self.btn_export.pack(side=tk.LEFT, padx=5)

        self.lbl_status = tk.Label(
            frame_config,
            text="状态：未开始",
            fg="gray",
            wraplength=580,
            justify=tk.LEFT,
        )
        self.lbl_status.grid(row=5, column=0, columnspan=2, pady=(0, 5))

        # 开源与作者声明
        frame_notice = tk.Frame(frame_config)
        frame_notice.grid(row=6, column=0, columnspan=2, pady=(0, 5))
        
        lbl_notice = tk.Label(frame_notice, text="本项目基于 MIT 协议完全免费开源，项目地址：", fg="gray")
        lbl_notice.pack(side=tk.LEFT)
        
        lbl_link = tk.Label(frame_notice, text="https://github.com/zhaogelz/NanoNVR", fg="blue", cursor="hand2")
        lbl_link.pack(side=tk.LEFT)
        lbl_link.bind("<Button-1>", lambda e: webbrowser.open_new("https://github.com/zhaogelz/NanoNVR"))

        # 日志输出区
        frame_log = tk.Frame(self.root, padx=10, pady=5)
        frame_log.pack(fill=tk.BOTH, expand=True)
        tk.Label(frame_log, text="运行日志:").pack(anchor=tk.W)
        self.text_log = scrolledtext.ScrolledText(frame_log, state=tk.DISABLED, bg="#f0f0f0", height=15)
        self.text_log.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        """将日志输出到界面（超过 2000 行自动截断旧日志，防止长期运行内存膨胀）"""
        def append():
            self.text_log.config(state=tk.NORMAL)
            self.text_log.insert(tk.END, message + "\n")
            self.text_log.see(tk.END)
            # 自动截断：保留最近 2000 行
            try:
                last_line = int(self.text_log.index("end-1c").split(".")[0])
                if last_line > 2000:
                    self.text_log.delete("1.0", f"{last_line - 2000}.0")
            except Exception:
                pass
            self.text_log.config(state=tk.DISABLED)
        self.root.after(0, append)
        print(message)

    def _set_status(self, text, color="gray"):
        def apply():
            if not self.closing:
                self.lbl_status.config(text=f"状态：{text}", fg=color)
        try:
            self.root.after(0, apply)
        except Exception:
            pass

    @staticmethod
    def _redact_rtsp_url(value):
        """隐藏 RTSP 地址中的密码，避免运行日志泄露摄像头凭据。"""
        return re.sub(r"(rtsp://[^:/@\s]+:)[^@/\s]+@", r"\1***@", value)

    def load_config(self):
        """加载上次保存的配置"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "rtsp_url" in data:
                        self.entry_rtsp.delete(0, tk.END)
                        self.entry_rtsp.insert(0, data["rtsp_url"])
                    if "max_gb" in data:
                        self.entry_max_gb.delete(0, tk.END)
                        self.entry_max_gb.insert(0, str(data["max_gb"]))
                    if "save_dir" in data:
                        self.entry_save_dir.delete(0, tk.END)
                        self.entry_save_dir.insert(0, str(data["save_dir"]))
            except Exception as e:
                self.log(f"读取配置失败: {e}")
        self.refresh_save_dir()

    def save_config(self):
        """保存配置"""
        data = {
            "rtsp_url": self.entry_rtsp.get().strip(),
            "max_gb": self.entry_max_gb.get().strip(),
            "save_dir": self.entry_save_dir.get().strip()
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.log(f"保存配置失败: {e}")

    def browse_save_dir(self):
        """浏览选择保存目录"""
        cur = self.entry_save_dir.get().strip() or str(BASE_DIR)
        d = filedialog.askdirectory(initialdir=cur, title="选择录像保存目录")
        if d:
            self.entry_save_dir.delete(0, tk.END)
            self.entry_save_dir.insert(0, d)
            self.refresh_save_dir()

    def refresh_save_dir(self):
        """从输入框刷新保存目录；显式路径不可用时返回 False，不静默改写系统盘。"""
        p = self.entry_save_dir.get().strip()
        if not p:
            self.save_dir = BASE_DIR
            return True
        try:
            path = Path(p)
            path.mkdir(parents=True, exist_ok=True)
            self.save_dir = path
            return True
        except Exception as e:
            self.log(f"保存目录不可用，已阻止使用该路径: {p} ({e})")
            return False

    def get_save_dir(self) -> Path:
        """返回当前生效的保存根目录"""
        return self.save_dir

    def toggle_recording(self):
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording_thread()

    def start_recording_thread(self):
        rtsp_url = self.entry_rtsp.get().strip()
        if not rtsp_url:
            messagebox.showwarning("提示", "请输入有效的 RTSP 流地址")
            return
            
        try:
            max_gb = float(self.entry_max_gb.get().strip())
        except ValueError:
            messagebox.showwarning("提示", "录像配额必须是有效数字")
            return

        if not self.refresh_save_dir():
            messagebox.showerror("保存目录不可用", "请重新选择可写入的录像保存目录")
            self._set_status("保存目录不可用，未启动", "red")
            return

        try:
            volume_total_gib, _ = self._get_volume_usage_gib()
        except Exception as exc:
            messagebox.showerror("磁盘检查失败", f"无法读取保存目录所在磁盘的容量：{exc}")
            self._set_status("磁盘容量检查失败，未启动", "red")
            return

        quota_error = validate_recording_quota(max_gb, volume_total_gib)
        if quota_error:
            messagebox.showwarning("录像配额不可用", quota_error)
            self._set_status("录像配额不可用，未启动", "red")
            return

        self.save_config()
        self.is_recording = True
        self.btn_start.config(text="■ 停止录制", bg="red")
        self._set_status("正在启动 FFmpeg...", "#B26A00")
        self.log(f"=== 开始录制服务 ===")
        self.log(f"流地址: {self._redact_rtsp_url(rtsp_url)}")
        self.log(f"录像配额: {max_gb:g} GiB（健康水位 {max_gb * HEALTHY_WATERMARK_RATIO:.2f} GiB）")
        self.log(f"保存目录: {self.get_save_dir()}")
        
        self.record_thread = threading.Thread(target=self.recording_task, args=(rtsp_url, max_gb), daemon=True)
        self.record_thread.start()

    def stop_recording(self):
        if not self.is_recording and not self.record_thread:
            return
        self.is_recording = False
        self.btn_start.config(text="停止中...", state=tk.DISABLED, bg="gray")
        self._set_status("正在停止...", "#B26A00")
        self.log("正在停止录制服务，请稍候...")

        # 启动一个后台线程等待真正停止
        threading.Thread(target=self._wait_stop, daemon=True).start()

    def _wait_stop(self):
        self._stop_current_process()
        if self.record_thread:
            self.record_thread.join(timeout=12)
        if not self.closing:
            self.root.after(0, self._on_stopped)

    def _on_stopped(self):
        self.record_thread = None
        self.btn_start.config(text="▶ 启动录制", bg="green", state=tk.NORMAL)
        self._set_status("已停止", "gray")
        self.log("=== 已停止录制服务 ===")

    def _stop_current_process(self):
        with self._process_lock:
            process = self.current_process
        if process:
            process.stop()

    def _set_current_process(self, process):
        with self._process_lock:
            self.current_process = process

    def _clear_current_process(self, process):
        with self._process_lock:
            if self.current_process is process:
                self.current_process = None

    def _assign_process_to_job(self, process):
        try:
            return self._process_job.assign(process)
        except Exception as exc:
            self.log(f"子进程防遗留保护失效，本次继续运行: {exc}")
            self._process_job.close()
            return False

    # ================= 核心录制逻辑 =================

    def _iter_occupied_files(self):
        """遍历计入录像配额的文件：仅日期目录下可循环回收的 .ts 分片。"""
        save_root = self.get_save_dir()
        # 保存目录可能位于可移动介质，中途被移除时不要让录制线程崩溃
        if not save_root.is_dir():
            return
        for d in save_root.iterdir():
            try:
                if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name):
                    for f in d.glob("*.ts"):
                        if f.is_file():
                            yield f
            except Exception:
                continue

    def get_total_size_gb(self) -> float:
        """统计可循环回收录像分片的总占用（GiB）。"""
        total_bytes = 0
        for f in self._iter_occupied_files():
            try:
                total_bytes += f.stat().st_size
            except Exception:
                pass
        return total_bytes / (1024 ** 3)

    def _get_volume_usage_gib(self):
        usage = shutil.disk_usage(self.get_save_dir())
        divisor = 1024 ** 3
        return usage.total / divisor, usage.free / divisor

    def clean_old_files(self, max_gb):
        if not self.is_recording:
            return True

        current_gb = self.get_total_size_gb()
        volume_total_gib, volume_free_gib = self._get_volume_usage_gib()
        plan = build_cleanup_plan(max_gb, current_gb, volume_total_gib, volume_free_gib)
        if plan is None:
            return True

        self.log(
            f"[{datetime.now().strftime('%H:%M:%S')}] {plan.reason}："
            f"录像 {current_gb:.2f}/{max_gb:g} GiB，磁盘可用 {volume_free_gib:.2f} GiB；"
            f"开始批量删除旧片段，目标录像占用 {plan.target_managed_gib:.2f} GiB"
        )

        safe_cutoff = time.time() - DELETE_SAFE_SECONDS
        files = []
        # 只回收日期目录下的 .ts 分片，导出的 MP4 是用户主动产物，永不自动删除
        for date_dir in self.get_save_dir().iterdir():
            try:
                if not (date_dir.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_dir.name)):
                    continue
            except Exception:
                continue
            try:
                for f in date_dir.glob("*.ts"):
                    if f.is_file():
                        st = f.stat()
                        files.append((f, st.st_mtime, st.st_size))
            except Exception:
                continue

        # 按最后修改时间升序排列（最老的排前面），始终保护最新两个分片。
        files.sort(key=lambda x: x[1])
        protected_newest = {f.resolve() for f, _, _ in files[-PROTECTED_NEWEST_SEGMENTS:]}
        files = [
            item for item in files
            if item[1] < safe_cutoff
            and item[0].resolve() not in protected_newest
        ]
        
        deleted_bytes = 0
        target_release_bytes = plan.required_release_gib * (1024 ** 3)
        files_deleted_count = 0

        for f, _, size in files:
            # 如果释放的空间达标，或者服务已停止，则停止删除
            if not self.is_recording or deleted_bytes >= target_release_bytes:
                break

            try:
                # 租约检查与删除必须在同一把锁内；否则导出可能刚登记租约就被旧快照删掉。
                with self._lease_lock:
                    if f.resolve() in self._leased_segments:
                        continue
                    os.remove(f)  # 直接删除并释放文件系统中的逻辑空间，不进入回收站
                deleted_bytes += size
                files_deleted_count += 1
                
                # 顺手尝试移除失去所有录像文件的空日期目录
                parent_dir = f.parent
                if parent_dir.exists() and not any(parent_dir.iterdir()):
                    try:
                        os.rmdir(parent_dir)
                    except Exception:
                        pass
            except Exception as e:
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 删除旧录像 {f.name} 失败: {e}")

        if not self.is_recording:
            return False

        # 删除后以重新测得的目录占用和卷余量为准，不把文件标称大小当成完成证明。
        remaining_gib = self.get_total_size_gb()
        _, remaining_free_gib = self._get_volume_usage_gib()
        quota_safe = not plan.quota_triggered or remaining_gib <= plan.healthy_gib + 1e-9
        volume_safe = not plan.volume_triggered or remaining_free_gib >= plan.volume_floor_gib
        if quota_safe and volume_safe:
            measured_freed_gib = max(0.0, current_gb - remaining_gib)
            self.log(
                f"[{datetime.now().strftime('%H:%M:%S')}] 批量删除完成："
                f"删除 {files_deleted_count} 个旧片段，录像逻辑占用减少 {measured_freed_gib:.2f} GiB；"
                f"当前录像 {remaining_gib:.2f} GiB，磁盘可用 {remaining_free_gib:.2f} GiB"
            )
            return True

        exports_gb = self.get_exports_size_gb()
        detail = (
            f"清理后录像仍占 {remaining_gib:.2f} GiB，磁盘仅余 {remaining_free_gib:.2f} GiB；"
            f"受保护导出占 {exports_gb:.2f} GiB。"
        )
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 存储保护未能恢复安全水位：{detail}")
        raise StorageProtectionError(
            "无法通过删除旧录像恢复安全空间，已停止录制以防磁盘写满。"
            "请转移 exports/ 导出文件或清理磁盘上的其他内容。"
        )

    def get_exports_size_gb(self) -> float:
        """导出目录占用（GiB）"""
        exports_dir = self.get_save_dir() / "exports"
        if not exports_dir.is_dir():
            return 0.0
        total_bytes = 0
        try:
            for f in exports_dir.glob("*.mp4"):
                if f.is_file():
                    total_bytes += f.stat().st_size
        except Exception:
            pass
        return total_bytes / (1024 ** 3)
    # ================= 录像导出（MP4） =================

    def _list_date_dirs(self):
        """扫描所有录像日期目录（YYYY-MM-DD）"""
        dirs = []
        try:
            for d in self.get_save_dir().iterdir():
                if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name):
                    dirs.append(d.name)
        except Exception:
            pass
        return sorted(dirs)

    def _list_segments(self, date_str):
        """列出某天的所有分片，返回 [(Path, 'HH:MM:SS'), ...] 按时间升序"""
        segs = []
        day_dir = self.get_save_dir() / date_str
        try:
            for f in day_dir.glob("*.ts"):
                m = re.fullmatch(r"(\d{2})_(\d{2})_(\d{2})\.ts", f.name)
                if m:
                    segs.append((f, f"{m.group(1)}:{m.group(2)}:{m.group(3)}"))
        except Exception:
            pass
        segs.sort(key=lambda x: x[1])
        return segs

    def _select_and_lease_export_segments(self, date_str, start_t, end_t):
        """在清理使用的同一把锁内完成选择和租约登记。"""
        with self._lease_lock:
            all_pairs = self._list_segments(date_str)
            pairs = [(f, t) for f, t in all_pairs if start_t <= t <= end_t]
            excluded_active = None
            if self.is_recording and date_str == datetime.now().strftime("%Y-%m-%d") and all_pairs:
                # 分片从 FFmpeg 实际启动时刻命名，不假设落在 00/15/30/45 整刻钟。
                active_path = all_pairs[-1][0].resolve()
                before = len(pairs)
                pairs = [(f, t) for f, t in pairs if f.resolve() != active_path]
                if len(pairs) < before:
                    excluded_active = active_path

            chosen = [f for f, _ in pairs]
            if not chosen:
                raise RuntimeError(
                    "所选时间段内没有可导出的完整分片（最新分片可能仍在写入，请把结束分片往前选一格）"
                )

            leased = {f.resolve() for f in chosen}
            self._leased_segments.update(leased)
            return chosen, leased, excluded_active

    def _release_segment_leases(self, leased):
        if leased:
            with self._lease_lock:
                self._leased_segments.difference_update(leased)

    def _probe_stream_codecs(self, sample: Path):
        """用 ffmpeg -i 的输出探测视频/音频编码（无需 ffprobe）"""
        cmd = [self._get_ffmpeg_path(), "-hide_banner", "-i", str(sample)]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="ignore",
                                  creationflags=creationflags, timeout=15)
            err = proc.stderr or ""
        except Exception:
            return None, None
        v = re.search(r"Video:\s*([a-z0-9_]+)", err)
        a = re.search(r"Audio:\s*([a-z0-9_]+)", err)
        return (v.group(1) if v else None), (a.group(1) if a else None)

    def open_export_dialog(self):
        if self.exporting:
            messagebox.showinfo("提示", "正在导出中，请等待当前导出完成")
            return
        self.refresh_save_dir()
        date_dirs = self._list_date_dirs()
        if not date_dirs:
            messagebox.showinfo("提示", f"未找到任何录像日期目录（保存目录：{self.get_save_dir()}）")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("导出录像为 MP4")
        dlg.geometry("500x340")
        dlg.transient(self.root)
        pad = {"padx": 10, "pady": 6}

        tk.Label(dlg, text="录像日期:").grid(row=0, column=0, sticky=tk.W, **pad)
        cb_date = ttk.Combobox(dlg, values=date_dirs, state="readonly", width=22)
        cb_date.grid(row=0, column=1, columnspan=2, sticky=tk.W, **pad)
        cb_date.set(date_dirs[-1])

        tk.Label(dlg, text="开始分片:").grid(row=1, column=0, sticky=tk.W, **pad)
        cb_start = ttk.Combobox(dlg, state="readonly", width=22)
        cb_start.grid(row=1, column=1, columnspan=2, sticky=tk.W, **pad)

        tk.Label(dlg, text="结束分片:").grid(row=2, column=0, sticky=tk.W, **pad)
        cb_end = ttk.Combobox(dlg, state="readonly", width=22)
        cb_end.grid(row=2, column=1, columnspan=2, sticky=tk.W, **pad)

        tk.Label(dlg, text="（时间粒度为 15 分钟分片，结束分片会被完整包含）",
                 fg="gray").grid(row=3, column=0, columnspan=3, sticky=tk.W, padx=10)

        def refresh_times(*_):
            times = [t for _, t in self._list_segments(cb_date.get())]
            cb_start.config(values=times)
            cb_end.config(values=times)
            if times:
                cb_start.set(times[0])
                cb_end.set(times[-1])
            update_default_out()

        def default_out_name():
            s = cb_start.get().replace(":", "-") or "start"
            e = cb_end.get().replace(":", "-") or "end"
            return f"{cb_date.get()}_{s}_{e}.mp4"

        def update_default_out(*_):
            entry_out.delete(0, tk.END)
            entry_out.insert(0, str(self.get_save_dir() / "exports" / default_out_name()))

        cb_date.bind("<<ComboboxSelected>>", refresh_times)
        cb_start.bind("<<ComboboxSelected>>", update_default_out)
        cb_end.bind("<<ComboboxSelected>>", update_default_out)

        tk.Label(dlg, text="保存到:").grid(row=4, column=0, sticky=tk.W, **pad)
        entry_out = tk.Entry(dlg, width=42)
        entry_out.grid(row=4, column=1, sticky=tk.W, **pad)

        def browse_out():
            p = filedialog.asksaveasfilename(parent=dlg, defaultextension=".mp4",
                                             filetypes=[("MP4 视频", "*.mp4")],
                                             initialfile=default_out_name())
            if p:
                entry_out.delete(0, tk.END)
                entry_out.insert(0, p)

        tk.Button(dlg, text="浏览...", command=browse_out).grid(row=4, column=2, sticky=tk.W, padx=5)

        progress = ttk.Progressbar(dlg, length=460, mode="determinate", maximum=100)
        progress.grid(row=5, column=0, columnspan=3, padx=10, pady=(12, 2))
        lbl_prog = tk.Label(dlg, text="待导出", fg="gray")
        lbl_prog.grid(row=6, column=0, columnspan=3, sticky=tk.W, padx=10)

        btn_do = tk.Button(dlg, text="开始导出", width=15, bg="#1976D2", fg="white",
                           font=("", 10, "bold"))
        btn_do.grid(row=7, column=0, columnspan=3, pady=10)

        refresh_times()

        def on_progress(pct, text):
            def apply():
                progress["value"] = pct
                lbl_prog.config(text=text)
            self.root.after(0, apply)

        def on_done(err, out_path):
            def apply():
                self.exporting = False
                btn_do.config(state=tk.NORMAL, text="开始导出")
                if err:
                    lbl_prog.config(text="导出失败", fg="red")
                    messagebox.showerror("导出失败", err, parent=dlg)
                else:
                    progress["value"] = 100
                    lbl_prog.config(text="导出完成", fg="green")
                    messagebox.showinfo("导出完成", f"已导出到:\n{out_path}", parent=dlg)
            self.root.after(0, apply)

        def start_export():
            date_str = cb_date.get()
            start_t, end_t = cb_start.get(), cb_end.get()
            if not start_t or not end_t:
                messagebox.showwarning("提示", "请选择起止分片", parent=dlg)
                return
            if start_t > end_t:
                messagebox.showwarning("提示", "开始分片不能晚于结束分片", parent=dlg)
                return
            out_path = entry_out.get().strip()
            if not out_path:
                messagebox.showwarning("提示", "请选择输出文件路径", parent=dlg)
                return
            self.exporting = True
            btn_do.config(state=tk.DISABLED, text="导出中...")
            progress["value"] = 0
            lbl_prog.config(text="正在导出...", fg="gray")
            threading.Thread(target=self._export_worker,
                             args=(date_str, start_t, end_t, out_path, on_progress, on_done),
                             daemon=True).start()

        btn_do.config(command=start_export)

    def _export_worker(self, date_str, start_t, end_t, out_path, progress_cb, done_cb):
        """后台线程：concat 拼接分片并转封装为 MP4（视频不重新编码）"""
        list_file = None
        leased = set()
        try:
            chosen, leased, excluded_active = self._select_and_lease_export_segments(
                date_str, start_t, end_t
            )
            if excluded_active:
                self.log(
                    f"[{datetime.now().strftime('%H:%M:%S')}] 已跳过仍可能写入的最新分片 "
                    f"{excluded_active.name}，导出末尾将停在该分片之前"
                )

            vcodec, acodec = self._probe_stream_codecs(chosen[0])
            self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 导出探测: 视频={vcodec or '未知'}, 音频={acodec or '无'}")

            # 确保输出目录存在（用户可能改了路径）
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)

            # 临时 concat 清单放系统临时目录，避免 exe 位于只读路径（如 Program Files）时写入失败
            list_file = Path(tempfile.gettempdir()) / f"nanonvr_concat_{int(time.time() * 1000)}.txt"
            with open(list_file, "w", encoding="utf-8") as lf:
                for f in chosen:
                    # 统一使用正斜杠路径，规避 Windows 反斜杠在 concat demuxer 中的解析歧义
                    safe_path = str(f).replace("\\", "/")
                    lf.write("file '" + safe_path + "'\n")

            total_sec = max(1, len(chosen) * SEGMENT_DURATION)

            cmd = [self._get_ffmpeg_path(),
                   "-hide_banner", "-loglevel", "error",
                   "-f", "concat", "-safe", "0", "-i", str(list_file),
                   "-c:v", "copy"]
            # MP4 容器只安全放行 AAC 音频，其他编码（G.711/G.726 等）仅重编码音频，视频仍无损 copy
            if acodec == "aac":
                cmd += ["-c:a", "copy"]
            elif acodec:
                cmd += ["-c:a", "aac", "-b:a", "128k"]
            # H.265 需要 hvc1 标签才能被 QuickTime/部分播放器识别
            if vcodec in ("hevc", "h265"):
                cmd += ["-tag:v", "hvc1"]
            cmd += ["-movflags", "+faststart", "-y",
                    "-progress", "pipe:1", "-nostats", out_path]

            creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="ignore",
                                    creationflags=creationflags)
            self._assign_process_to_job(proc)

            # 后台抽干 stderr，防止管道缓冲区写满造成死锁
            err_buf = []
            def drain_stderr():
                try:
                    err_buf.append(proc.stderr.read() or "")
                except Exception:
                    pass
            drain = threading.Thread(target=drain_stderr, daemon=True)
            drain.start()

            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.strip().split("=")[1])
                        pct = min(99.0, us / 1_000_000 / total_sec * 100)
                        progress_cb(pct, f"正在导出... {pct:.0f}%")
                    except (ValueError, ZeroDivisionError):
                        pass
                elif line.startswith("progress=end"):
                    break

            proc.wait()
            drain.join(timeout=3)

            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg 导出失败: {(err_buf[0] if err_buf else '').strip()[:300] or '未知错误'}")

            size_mb = os.path.getsize(out_path) / (1024 ** 2)
            self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 导出完成: {out_path} ({size_mb:.1f}MB, {len(chosen)} 个分片)")
            progress_cb(100, "导出完成")
            done_cb(None, out_path)
        except Exception as e:
            self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 导出失败: {e}")
            done_cb(str(e), None)
        finally:
            self._release_segment_leases(leased)
            if list_file:
                try:
                    os.remove(list_file)
                except Exception:
                    pass

    def _get_ffmpeg_path(self) -> str:
        """获取 ffmpeg 可执行文件路径"""
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            bundled_ffmpeg = os.path.join(sys._MEIPASS, 'ffmpeg.exe')
            if os.path.exists(bundled_ffmpeg):
                return bundled_ffmpeg
        
        local_ffmpeg = BASE_DIR / "ffmpeg.exe"
        if local_ffmpeg.exists():
            return str(local_ffmpeg)
            
        return "ffmpeg"

    def _build_ffmpeg_cmd(self, rtsp_url, save_path: Path, timeout_option: str):
        output_template = str(save_path / "%H_%M_%S.ts")
        return [
            self._get_ffmpeg_path(),
            "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            timeout_option, FFMPEG_TIMEOUT_US,
            "-buffer_size", "10M",
            "-i", rtsp_url,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(SEGMENT_DURATION),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            "-strftime", "1",
            output_template,
        ]

    @staticmethod
    def _latest_segment_signature(save_path):
        latest = None
        try:
            for path in save_path.glob("*.ts"):
                if not path.is_file():
                    continue
                stat = path.stat()
                item = (stat.st_mtime_ns, path, stat.st_size)
                if latest is None or item[0] > latest[0]:
                    latest = item
        except Exception:
            return None, None

        if latest is None:
            return None, None
        mtime_ns, path, size = latest
        return path, (str(path.resolve()), size, mtime_ns)

    def _start_ffmpeg_with_timeout_fallback(self, rtsp_url, save_path: Path):
        timeout_candidates = ["-timeout", "-rw_timeout", "-stimeout"]
        for timeout_opt in timeout_candidates:
            if not self.is_recording:
                return None, None
                
            cmd = self._build_ffmpeg_cmd(rtsp_url, save_path, timeout_opt)
            try:
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                raw_process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=creationflags
                )
                self._assign_process_to_job(raw_process)
                probe = ManagedFfmpegProcess(raw_process)
            except FileNotFoundError:
                raise RuntimeError("未找到 ffmpeg 工具，请下载 ffmpeg 并将其加入环境变量 PATH。")

            time.sleep(3)
            
            if not self.is_recording:
                probe.stop()
                return None, None

            if probe.poll() is None:
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] ffmpeg 已启动")
                self._set_status("FFmpeg 已启动，等待录像数据...", "#B26A00")
                return probe, timeout_opt

            probe.stop()
            err = probe.recent_errors()

            lowered = err.lower()
            opt_name = timeout_opt.lstrip("-").lower()
            option_not_found = (
                ("option" in lowered and "not found" in lowered and opt_name in lowered)
                or ("unrecognized option" in lowered and opt_name in lowered)
            )

            if option_not_found:
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] ffmpeg 不支持 {timeout_opt}，重试中...")
                continue
                
            raise RuntimeError(f"ffmpeg 启动失败: {err.strip() or '未知错误'}")

        raise RuntimeError("ffmpeg 启动失败：版本不支持现有超时参数")

    def recording_task(self, rtsp_url, max_gb):
        while self.is_recording:
            planned_rollover = False
            process = None
            try:
                self.clean_old_files(max_gb)
                if not self.is_recording:
                    break

                today_str = datetime.now().strftime("%Y-%m-%d")
                save_path = self.get_save_dir() / today_str
                save_path.mkdir(exist_ok=True)

                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 录写入目录: {today_str}")
                
                process, _ = self._start_ffmpeg_with_timeout_fallback(rtsp_url, save_path)
                if not process:
                    continue

                self._set_current_process(process)
                current_day = today_str
                last_clean_ts = time.time()
                last_progress_check = 0.0
                watchdog = RecordingProgressWatchdog()
                watchdog.start(time.monotonic())

                while self.is_recording and process.poll() is None:
                    time.sleep(1)
                    now_ts = time.time()
                    monotonic_now = time.monotonic()

                    if monotonic_now - last_progress_check >= POLL_INTERVAL:
                        latest_path, signature = self._latest_segment_signature(save_path)
                        if watchdog.observe(signature, monotonic_now):
                            self.log(
                                f"[{datetime.now().strftime('%H:%M:%S')}] 录像文件已连续 "
                                f"{WRITE_STALL_TIMEOUT} 秒没有增长，主动重启 ffmpeg..."
                            )
                            self._set_status("录像无增长，正在重连...", "red")
                            process.stop()
                            break
                        if latest_path and signature:
                            latest_write = datetime.fromtimestamp(signature[2] / 1_000_000_000)
                            self._set_status(
                                f"录制中 · {latest_path.name} · 更新于 {latest_write.strftime('%H:%M:%S')}",
                                "green",
                            )
                        last_progress_check = monotonic_now

                    if now_ts - last_clean_ts >= CLEAN_INTERVAL:
                        self.clean_old_files(max_gb)
                        last_clean_ts = now_ts

                    new_day = datetime.now().strftime("%Y-%m-%d")
                    if new_day != current_day:
                        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 跨天切换录像目录...")
                        self._set_status("跨天切换录像目录...", "#B26A00")
                        planned_rollover = True
                        break

                process.stop()
                self._clear_current_process(process)

                if self.is_recording and not planned_rollover:
                    self._set_status("等待重连...", "#B26A00")
                    err_tail = process.recent_errors()
                    if err_tail.strip():
                        self.log(
                            f"[{datetime.now().strftime('%H:%M:%S')}] ffmpeg 退出信息: "
                            f"{self._redact_rtsp_url(err_tail.strip())}"
                        )

            except Exception as e:
                if process:
                    process.stop()
                    self._clear_current_process(process)
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 发生异常: {e}")
                self._set_status(f"已停止：{e}", "red")
                # 抛出窗口级错误提示（切回主UI线程显示）
                self.root.after(0, lambda err=str(e): messagebox.showerror("录制错误", err))
                # 自动停止录制并复位按钮
                self.root.after(0, self.stop_recording)
                break

            if self.is_recording and not planned_rollover:
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 将在 {RETRY_INTERVAL} 秒后尝试重连...")
                self._set_status(f"{RETRY_INTERVAL} 秒后重连...", "#B26A00")
                # 将阻塞睡眠分散，以便更快响应停止操作
                for _ in range(RETRY_INTERVAL):
                    if not self.is_recording:
                        break
                    time.sleep(1)

        self._stop_current_process()

    def on_closing(self):
        """关闭窗口时先可靠停止 FFmpeg，再销毁界面。"""
        if self.closing:
            return
        self.closing = True
        self.save_config()
        self.is_recording = False
        try:
            self.btn_start.config(text="正在安全退出...", state=tk.DISABLED, bg="gray")
        except Exception:
            pass
        self.log("正在安全停止录像进程...")
        threading.Thread(target=self._close_worker, daemon=True).start()

    def _close_worker(self):
        self._stop_current_process()
        if self.record_thread and self.record_thread is not threading.current_thread():
            self.record_thread.join(timeout=12)
        self._process_job.close()
        self.root.after(0, self.root.destroy)

if __name__ == "__main__":
    root = tk.Tk()
    app = MonitorApp(root)
    root.mainloop()
