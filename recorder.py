import os
import sys
import subprocess
import time
import threading
import json
import tkinter as tk
from tkinter import messagebox, scrolledtext
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

# 获取正确的运行目录（兼容 PyInstaller 独立 exe 运行方式）
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent.absolute()
else:
    BASE_DIR = Path(__file__).parent.absolute()

CONFIG_FILE = BASE_DIR / "config.json"

class MonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NanoNVR")
        self.root.geometry("600x450")
        self.root.minsize(500, 400)
        
        self.is_recording = False
        self.record_thread = None
        self.current_process = None
        
        self.setup_ui()
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

        tk.Label(frame_config, text="最大空间(GB):").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.entry_max_gb = tk.Entry(frame_config, width=15)
        self.entry_max_gb.grid(row=1, column=1, sticky=tk.W, pady=5, padx=5)
        self.entry_max_gb.insert(0, "150")

        # 启动按键
        self.btn_start = tk.Button(frame_config, text="▶ 启动录制", command=self.toggle_recording, width=15, bg="green", fg="white", font=("", 10, "bold"))
        self.btn_start.grid(row=2, column=0, columnspan=2, pady=10)

        # 开源与作者声明
        frame_notice = tk.Frame(frame_config)
        frame_notice.grid(row=3, column=0, columnspan=2, pady=(0, 5))
        
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
        """将日志输出到界面"""
        def append():
            self.text_log.config(state=tk.NORMAL)
            self.text_log.insert(tk.END, message + "\n")
            self.text_log.see(tk.END)
            self.text_log.config(state=tk.DISABLED)
        self.root.after(0, append)
        print(message)

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
            except Exception as e:
                self.log(f"读取配置失败: {e}")

    def save_config(self):
        """保存配置"""
        data = {
            "rtsp_url": self.entry_rtsp.get().strip(),
            "max_gb": self.entry_max_gb.get().strip()
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.log(f"保存配置失败: {e}")

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
            messagebox.showwarning("提示", "最大占用空间必须是有效数字")
            return

        self.save_config()
        self.is_recording = True
        self.btn_start.config(text="■ 停止录制", bg="red")
        self.log(f"=== 开始录制服务 ===")
        self.log(f"流地址: {rtsp_url}")
        self.log(f"空间限制: {max_gb} GB")
        
        self.record_thread = threading.Thread(target=self.recording_task, args=(rtsp_url, max_gb), daemon=True)
        self.record_thread.start()

    def stop_recording(self):
        self.is_recording = False
        self.btn_start.config(text="停止中...", state=tk.DISABLED, bg="gray")
        self.log("正在停止录制服务，请稍候...")
        
        if self.current_process and self.current_process.poll() is None:
            try:
                self.current_process.terminate()
            except Exception:
                pass

        # 启动一个后台线程等待真正停止
        threading.Thread(target=self._wait_stop, daemon=True).start()

    def _wait_stop(self):
        if self.record_thread:
            self.record_thread.join(timeout=10)
        self.root.after(0, self._on_stopped)

    def _on_stopped(self):
        self.btn_start.config(text="▶ 启动录制", bg="green", state=tk.NORMAL)
        self.log("=== 已停止录制服务 ===")

    # ================= 核心录制逻辑 =================

    def get_total_size_gb(self) -> float:
        total_bytes = 0
        for f in BASE_DIR.rglob("*.ts"):
            try:
                if f.is_file():
                    total_bytes += f.stat().st_size
            except Exception:
                pass
        return total_bytes / (1024 ** 3)

    def clean_old_files(self, max_gb):
        current_gb = self.get_total_size_gb()
        if not self.is_recording or current_gb <= max_gb:
            return

        # 计算低水位目标：释放 5GB 或总配额的 10%（取较小值），确保腾出足够的连续空闲区块
        release_gb = min(5.0, max_gb * 0.1)
        target_gb = max_gb - release_gb
        
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 存储达到爆盘水位 ({current_gb:.2f}GB / {max_gb}GB)，开始批量物理抹除至健康水位 ({target_gb:.2f}GB)...")

        safe_cutoff = time.time() - DELETE_SAFE_SECONDS
        files = []
        for f in BASE_DIR.rglob("*.ts"):
            try:
                if f.is_file():
                    st = f.stat()
                    if st.st_mtime < safe_cutoff:
                        files.append((f, st.st_mtime, st.st_size))
            except Exception:
                continue

        # 按最后修改时间升序排列（最老的排前面）
        files.sort(key=lambda x: x[1])
        
        deleted_bytes = 0
        target_release_bytes = (current_gb - target_gb) * (1024 ** 3)
        files_deleted_count = 0

        for f, _, size in files:
            # 如果释放的空间达标，或者服务已停止，则停止删除
            if not self.is_recording or deleted_bytes >= target_release_bytes:
                break
                
            try:
                os.remove(f)  # 直接跳过回收站底层抹除
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
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 抹除旧录像 {f.name} 失败: {e}")

        if files_deleted_count > 0:
            freed_gb = deleted_bytes / (1024 ** 3)
            self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 批量抹除完成：共铲除 {files_deleted_count} 个陈旧片段，释放了 {freed_gb:.2f}GB 物理空间")
        else:
            self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 存储超限，但暂无足够老旧（过了安全缓冲期）的文件可供安全移除")
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

    def _start_ffmpeg_with_timeout_fallback(self, rtsp_url, save_path: Path):
        timeout_candidates = ["-timeout", "-rw_timeout", "-stimeout"]
        for timeout_opt in timeout_candidates:
            if not self.is_recording:
                return None, None
                
            cmd = self._build_ffmpeg_cmd(rtsp_url, save_path, timeout_opt)
            try:
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                probe = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=creationflags
                )
            except FileNotFoundError:
                raise RuntimeError("未找到 ffmpeg 工具，请下载 ffmpeg 并将其加入环境变量 PATH。")

            time.sleep(3)
            
            if not self.is_recording:
                probe.terminate()
                return None, None

            if probe.poll() is None:
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] ffmpeg 已启动")
                return probe, timeout_opt

            err = ""
            try:
                if probe.stderr:
                    err = probe.stderr.read() or ""
            except Exception:
                pass

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
            try:
                self.clean_old_files(max_gb)
                if not self.is_recording:
                    break

                today_str = datetime.now().strftime("%Y-%m-%d")
                save_path = BASE_DIR / today_str
                save_path.mkdir(exist_ok=True)

                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 录写入目录: {today_str}")
                
                process, _ = self._start_ffmpeg_with_timeout_fallback(rtsp_url, save_path)
                if not process:
                    continue
                    
                self.current_process = process
                current_day = today_str
                last_clean_ts = time.time()

                while self.is_recording and process.poll() is None:
                    time.sleep(1)
                    now_ts = time.time()

                    if now_ts - last_clean_ts >= CLEAN_INTERVAL:
                        self.clean_old_files(max_gb)
                        last_clean_ts = now_ts

                    new_day = datetime.now().strftime("%Y-%m-%d")
                    if new_day != current_day:
                        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 跨天重启 ffmpeg...")
                        process.terminate()
                        break

                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except:
                        process.kill()

                if self.is_recording:
                    err_tail = ""
                    try:
                        if process.stderr:
                            err_tail = process.stderr.read() or ""
                    except Exception:
                        pass

                    if err_tail.strip():
                        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 退出信息: {err_tail.strip()[:200]}")

            except Exception as e:
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 发生异常: {e}")
                # 抛出窗口级错误提示（切回主UI线程显示）
                self.root.after(0, lambda err=str(e): messagebox.showerror("录制错误", err))
                # 自动停止录制并复位按钮
                self.root.after(0, self.stop_recording)
                break

            if self.is_recording:
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 将在 {RETRY_INTERVAL} 秒后尝试重连...")
                # 将阻塞睡眠分散，以便更快响应停止操作
                for _ in range(RETRY_INTERVAL):
                    if not self.is_recording:
                        break
                    time.sleep(1)

    def on_closing(self):
        """关闭窗口时自动保存当前填写的配置并退出"""
        self.save_config()
        self.is_recording = False
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = MonitorApp(root)
    root.mainloop()
