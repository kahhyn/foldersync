# -*- coding: utf-8 -*-
"""
foldersync 同步控制台（图形界面）
- 立即同步：点按钮手动同步一次
- 自动同步：可开关，间隔自定（分钟），设置会记住
- 打包：python -m PyInstaller --onefile --windowed --name foldersync-gui foldersync_gui.py
  exe 需要与 config.toml 放在同一目录
"""

import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext, ttk

import foldersync as fs


def app_base_dir():
    # PyInstaller 打包后 __file__ 在临时解压目录，exe 场景取 exe 所在目录
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


class App:
    def __init__(self, root):
        self.root = root
        root.title("foldersync 同步控制台")
        root.geometry("620x460")
        root.minsize(520, 380)

        self.q = queue.Queue()
        self.syncing = False
        self.closing = False
        self.auto_on = False
        self.interval_min = 10
        self.next_ts = None
        self.quit_after_sync = False   # 有同步在跑时点了关闭：跑完这轮就退
        self.final_sync_started = False  # 关窗补同步进行中

        # ---- 状态区
        top = ttk.Frame(root, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        self.info_var = tk.StringVar(value="正在读取配置…")
        ttk.Label(top, textvariable=self.info_var).pack(anchor="w")

        # ---- 控制区
        ctrl = ttk.Frame(root, padding=(10, 4))
        ctrl.pack(fill="x")
        self.auto_var = tk.BooleanVar(value=False)
        self.auto_chk = ttk.Checkbutton(ctrl, text="自动同步", variable=self.auto_var,
                                        command=self.on_toggle_auto)
        self.auto_chk.pack(side="left")
        ttk.Label(ctrl, text="    间隔").pack(side="left")
        self.interval_var = tk.StringVar(value="10")
        self.spin = ttk.Spinbox(ctrl, from_=1, to=720, width=5,
                                textvariable=self.interval_var)
        self.spin.pack(side="left")
        ttk.Label(ctrl, text="分钟").pack(side="left")
        ttk.Button(ctrl, text="应用间隔", command=self.on_apply_interval).pack(side="left", padx=(6, 0))
        self.close_sync_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="关窗自动同步", variable=self.close_sync_var,
                        command=self.on_toggle_close_sync).pack(side="left", padx=(10, 0))
        self.sync_btn = ttk.Button(ctrl, text="立即同步", command=self.sync_now)
        self.sync_btn.pack(side="right")

        # ---- 状态行
        stat = ttk.Frame(root, padding=(10, 0))
        stat.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪。")
        ttk.Label(stat, textvariable=self.status_var).pack(anchor="w")

        # ---- 日志区
        logf = ttk.LabelFrame(root, text="同步日志", padding=(6, 4))
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.logbox = scrolledtext.ScrolledText(logf, height=12, state="disabled",
                                                font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)

        # ---- 引擎
        self.engine = None
        self.settings_path = None
        self.load_engine_and_settings()

        # 日志桥：foldersync.log() 同时送进界面
        self._orig_log = fs.log

        def bridged_log(msg):
            self._orig_log(msg)
            self.q.put(msg)

        fs.log = bridged_log

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll_queue()
        self.tick()

        auto = threading.Thread(target=self.auto_loop, daemon=True)
        auto.start()

    # ---------- 初始化

    def load_engine_and_settings(self):
        cfg_path = app_base_dir() / "config.toml"
        try:
            cfg = fs.load_config(str(cfg_path))
        except SystemExit as e:
            self.set_info("配置错误：%s" % e)
            return
        try:
            t = cfg["transport"]
            self.set_info("本地：%s    设备：%s    通道：%s"
                          % (cfg.get("local_folder"), cfg.get("device"), t.get("type")))
        except Exception:
            pass
        self.settings_path = fs.state_dir(cfg.get("profile", "default")) / "gui_settings.json"
        try:
            st = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.interval_min = max(1, min(720, int(st.get("interval_min", 10))))
            self.interval_var.set(str(self.interval_min))
            self.close_sync_var.set(bool(st.get("close_sync", True)))
        except Exception:
            pass
        try:
            self.engine = fs.Engine(cfg)
            self.append_log("引擎已就绪。上次日志见下方/日志文件。")
        except SystemExit as e:
            self.append_log("引擎初始化失败：%s" % e)
        except Exception as e:
            self.append_log("引擎初始化失败：%r" % e)

        try:
            st = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if st.get("auto"):
                self.auto_var.set(True)
                self.auto_on = True
        except Exception:
            pass

    def set_info(self, text):
        self.info_var.set(text)

    # ---------- 同步执行

    def sync_now(self):
        if self.syncing or self.engine is None:
            if self.engine is None:
                self.append_log("引擎未就绪，无法同步（检查 config.toml 与网络）。")
            return
        self.syncing = True
        self.sync_btn.state(["disabled"])
        self.status_var.set("同步中…")

        def work():
            try:
                s = self.engine.run_once()
                msg = ("上次同步 %s：推送 %d、拉取 %d、删除 %d、冲突 %d"
                       % (time.strftime("%H:%M:%S"), s["push"], s["pull"],
                          s["delete"], s["conflict"]))
                self.q.put("[状态] " + msg)
                self.root.after(0, lambda: self.status_var.set(msg))
            except Exception as e:
                self.root.after(0, lambda: self.status_var.set("同步出错，见日志"))
                self.q.put("[错误] 同步失败：%r" % e)
            finally:
                self.syncing = False
                self.root.after(0, lambda: self.sync_btn.state(["!disabled"]))
                if self.quit_after_sync and not self.final_sync_started:
                    self.root.after(0, self._force_quit)

        threading.Thread(target=work, daemon=True).start()

    # ---------- 自动同步

    def on_toggle_auto(self):
        self.auto_on = bool(self.auto_var.get())
        if self.auto_on:
            self.next_ts = time.time() + self.interval_min * 60
            self.append_log("自动同步已开启，间隔 %d 分钟。" % self.interval_min)
        else:
            self.next_ts = None
            self.append_log("自动同步已关闭。")
        self.save_settings()

    def on_apply_interval(self):
        try:
            v = max(1, min(720, int(self.interval_var.get())))
        except ValueError:
            self.append_log("间隔必须是 1~720 的整数分钟。")
            return
        self.interval_var.set(str(v))
        self.interval_min = v
        if self.auto_on:
            self.next_ts = time.time() + v * 60
        self.append_log("间隔已设为 %d 分钟。" % v)
        self.save_settings()

    def on_toggle_close_sync(self):
        self.append_log("关窗自动同步已%s。" % ("开启" if self.close_sync_var.get() else "关闭"))
        self.save_settings()

    def save_settings(self):
        if self.settings_path is None:
            return
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(
                {"auto": self.auto_on, "interval_min": self.interval_min,
                 "close_sync": bool(self.close_sync_var.get())},
                ensure_ascii=False), encoding="utf-8")
        except (OSError, tk.TclError):
            pass

    def auto_loop(self):
        while not self.closing:
            # 1 秒粒度等待，便于间隔修改即时生效
            waited = 0
            target = self.interval_min * 60
            while waited < target and not self.closing:
                time.sleep(1)
                waited += 1
                if not self.auto_on:
                    break
            if self.closing or not self.auto_on:
                continue
            if self.next_ts is None:
                self.next_ts = time.time()
            self.next_ts = time.time() + self.interval_min * 60
            if not self.syncing:
                self.root.after(0, self.sync_now)

    # ---------- 界面事件

    def poll_queue(self):
        if self.closing:
            return
        try:
            while True:
                line = self.q.get_nowait()
                self.append_log(line)
        except queue.Empty:
            pass
        self.root.after(300, self.poll_queue)

    def tick(self):
        if self.closing:
            return
        if self.auto_on and self.next_ts and not self.syncing:
            remain = int(self.next_ts - time.time())
            if remain > 0:
                m, s = divmod(remain, 60)
                self.status_var.set("距下次自动同步：%02d:%02d" % (m, s))
        self.root.after(1000, self.tick)

    def append_log(self, line):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", time.strftime("[%H:%M:%S] ") + line + "\n")
        if float(self.logbox.index("end-1c").split(".")[0]) > 800:
            self.logbox.delete("1.0", "200.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def on_close(self):
        # 关窗补同步进行中又点了一次关闭 → 强制退出
        if self.final_sync_started:
            self._force_quit()
            return
        # 还有同步在跑：等它跑完再退
        if self.syncing:
            self.quit_after_sync = True
            self.status_var.set("当前同步完成后退出…（再点一次关闭可立即退出）")
            return
        self.save_settings()
        # 开着"关窗自动同步"且本地有没上传的修改 → 先补一次
        if self.engine is not None and self.close_sync_var.get():
            try:
                pending = self.engine.has_local_changes()
            except Exception:
                pending = True
            if pending:
                self.final_sync_started = True
                self.syncing = True
                self.root.title("foldersync 同步控制台 — 关窗同步中…（再点关闭可跳过）")
                self.status_var.set("有未上传的修改，关窗同步中…（再点一次关闭可跳过）")
                threading.Thread(target=self._final_sync, daemon=True).start()
                return
        self._force_quit()

    def _final_sync(self):
        try:
            s = self.engine.run_once()
            self.q.put("[关窗同步] 完成：推送 %d、拉取 %d、删除 %d、冲突 %d"
                       % (s["push"], s["pull"], s["delete"], s["conflict"]))
        except Exception as e:
            self.q.put("[错误] 关窗同步失败：%r" % e)
        finally:
            self.syncing = False
            self.root.after(0, self._force_quit)

    def _force_quit(self):
        if self.closing:
            return
        self.closing = True
        self.save_settings()
        self.root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        style.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
