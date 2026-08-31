import os
import sys
import subprocess
import time
import threading
import json
import re
import tempfile
import tkinter as tk
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
        self.root.geometry("640x480")
        self.root.minsize(540, 440)
        
        self.is_recording = False
        self.record_thread = None
        self.current_process = None
        self.exporting = False
        self.save_dir = BASE_DIR  # 录像与导出的保存根目录（可由用户配置，默认=程序所在目录）
        
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

        # 保存目录（ts 录像分片与 mp4 导出都落在此目录下，留空=程序所在目录）
        tk.Label(frame_config, text="保存目录:").grid(row=2, column=0, sticky=tk.W, pady=5)
        frame_save = tk.Frame(frame_config)
        frame_save.grid(row=2, column=1, sticky=tk.W, pady=5, padx=5)
        self.entry_save_dir = tk.Entry(frame_save, width=44)
        self.entry_save_dir.pack(side=tk.LEFT)
        tk.Button(frame_save, text="浏览...", command=self.browse_save_dir).pack(side=tk.LEFT, padx=5)

        # 启动 / 导出按键
        frame_btns = tk.Frame(frame_config)
        frame_btns.grid(row=3, column=0, columnspan=2, pady=10)

        self.btn_start = tk.Button(frame_btns, text="▶ 启动录制", command=self.toggle_recording, width=15, bg="green", fg="white", font=("", 10, "bold"))
        self.btn_start.pack(side=tk.LEFT, padx=5)

        self.btn_export = tk.Button(frame_btns, text="📤 导出录像", command=self.open_export_dialog, width=15, bg="#1976D2", fg="white", font=("", 10, "bold"))
        self.btn_export.pack(side=tk.LEFT, padx=5)

        # 开源与作者声明
        frame_notice = tk.Frame(frame_config)
        frame_notice.grid(row=4, column=0, columnspan=2, pady=(0, 5))
        
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
        """从输入框刷新 self.save_dir（仅主线程调用）。无效则回退到程序目录。"""
        p = self.entry_save_dir.get().strip()
        if not p:
            self.save_dir = BASE_DIR
            return
        try:
            path = Path(p)
            path.mkdir(parents=True, exist_ok=True)
            self.save_dir = path
        except Exception as e:
            self.log(f"保存目录不可用，回退到程序所在目录: {p} ({e})")
            self.save_dir = BASE_DIR

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
            messagebox.showwarning("提示", "最大占用空间必须是有效数字")
            return

        self.save_config()
        self.refresh_save_dir()
        self.is_recording = True
        self.btn_start.config(text="■ 停止录制", bg="red")
        self.log(f"=== 开始录制服务 ===")
        self.log(f"流地址: {rtsp_url}")
        self.log(f"空间限制: {max_gb} GB")
        self.log(f"保存目录: {self.get_save_dir()}")
        
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

    def _iter_occupied_files(self):
        """遍历所有计入配额占用的文件：日期目录下的 .ts 分片 + exports 目录下的导出 MP4"""
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
        exports_dir = save_root / "exports"
        if exports_dir.is_dir():
            try:
                for f in exports_dir.glob("*.mp4"):
                    if f.is_file():
                        yield f
            except Exception:
                pass

    def get_total_size_gb(self) -> float:
        """统计录像分片 + 导出产物的总占用（GB）"""
        total_bytes = 0
        for f in self._iter_occupied_files():
            try:
                total_bytes += f.stat().st_size
            except Exception:
                pass
        return total_bytes / (1024 ** 3)

    def clean_old_files(self, max_gb):
        current_gb = self.get_total_size_gb()
        if not self.is_recording or current_gb <= max_gb:
            return

        # 计算低水位目标：释放量为配额的 10%（限制在 2~10GB 区间），
        # 既保证腾出足够连续空闲区块，又避免小配额下释放过多导致有效录像时长过短
        release_gb = min(max(2.0, max_gb * 0.1), 10.0)
        target_gb = max_gb - release_gb
        
        self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 存储达到爆盘水位 ({current_gb:.2f}GB / {max_gb}GB)，开始批量物理抹除至健康水位 ({target_gb:.2f}GB)...")

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
            exports_gb = self.get_exports_size_gb()
            if exports_gb > 0:
                self.log(f"[{datetime.now().strftime('%H:%M:%S')}] 提示：导出目录 exports/ 已占用 {exports_gb:.2f}GB，这部分会计入配额但不会被自动清理，请手动转移或删除")

    def get_exports_size_gb(self) -> float:
        """导出目录占用（GB）"""
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
        try:
            pairs = [(f, t) for f, t in self._list_segments(date_str) if start_t <= t <= end_t]
            # 正在录制时，排除当前正在写入的分片，避免导出末尾出现半截 / 截断
            if self.is_recording:
                now = datetime.now()
                cur_slot = f"{now.hour:02d}:{(now.minute // 15 * 15):02d}:00"
                before = len(pairs)
                pairs = [(f, t) for f, t in pairs if t != cur_slot]
                if len(pairs) < before:
                    self.log(f"[{now.strftime('%H:%M:%S')}] 已跳过正在写入的当前分片 {cur_slot.replace(':', '_')}_00.ts，导出末尾将停在该分片之前")
            chosen = [f for f, _ in pairs]
            if not chosen:
                raise RuntimeError("所选时间段内没有可导出的完整分片（当前分片可能正在写入，请把结束分片往前选一格）")

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
                save_path = self.get_save_dir() / today_str
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
