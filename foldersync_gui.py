# -*- coding: utf-8 -*-
"""
foldersync 同步控制台（图形界面）
- 立即同步：点按钮手动同步一次
- 自动同步：可开关，间隔自定（分钟），设置会记住
- 关窗自动同步：关窗时如有未上传的修改，先补一次同步再退出（可关）
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
from tkinter import ttk

import foldersync as fs

# ---------------------------------------------------------------- 配色与字体

BG = "#f1f5f9"        # 窗口底
CARD = "#ffffff"      # 卡片底
BORDER = "#e2e8f0"    # 卡片描边
DARK = "#0f172a"      # 顶栏 / 日志台底色
SUB = "#64748b"       # 次要文字
INK = "#0f172a"       # 主要文字
ACCENT = "#2563eb"    # 主色（按钮）
ACCENT_DARK = "#1d4ed8"
GREEN = "#16a34a"
AMBER = "#d97706"
RED = "#dc2626"


def app_base_dir():
    # PyInstaller 打包后 __file__ 在临时解压目录，exe 场景取 exe 所在目录
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


class App:
    def __init__(self, root):
        self.root = root
        root.title("foldersync 同步控制台")
        root.geometry("660x540")
        root.minsize(560, 460)
        root.configure(bg=BG)

        self.f_title = ("Microsoft YaHei UI", 15, "bold")
        self.f_norm = ("Microsoft YaHei UI", 9)
        self.f_bold = ("Microsoft YaHei UI", 9, "bold")
        self.f_small = ("Microsoft YaHei UI", 8)
        self.f_log = ("Consolas", 9)

        # ---- 状态变量
        self.q = queue.Queue()
        self.syncing = False
        self.closing = False
        self.auto_on = False
        self.interval_min = 10
        self.next_ts = None
        self.quit_after_sync = False    # 有同步在跑时点了关闭：跑完这轮就退
        self.final_sync_started = False  # 关窗补同步进行中
        self.auto_var = tk.BooleanVar(value=False)
        self.close_sync_var = tk.BooleanVar(value=True)
        self.interval_var = tk.StringVar(value="10")
        self.status_var = tk.StringVar(value="就绪")

        # ---- 界面
        self._build_header()
        self._build_controls()
        self._build_status()
        self._build_log()

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
        threading.Thread(target=self.auto_loop, daemon=True).start()

    # ================================================================ 界面搭建

    def _btn_primary(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, font=self.f_bold,
                         bg=ACCENT, fg="white", activebackground=ACCENT_DARK,
                         activeforeground="white", relief="flat", bd=0,
                         cursor="hand2", padx=18, pady=5,
                         disabledforeground="#93c5fd")

    def _btn_sec(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, font=self.f_norm,
                         bg=CARD, fg="#334155", activebackground="#e2e8f0",
                         relief="flat", bd=0, cursor="hand2", padx=12, pady=4,
                         highlightthickness=1, highlightbackground=BORDER)

    def _chk(self, parent, text, var, cmd):
        return tk.Checkbutton(parent, text=text, variable=var, command=cmd,
                              font=self.f_norm, bg=CARD, fg=INK, bd=0,
                              activebackground=CARD, highlightthickness=0,
                              anchor="w", selectcolor="#f8fafc", cursor="hand2")

    def _build_header(self):
        hd = tk.Frame(self.root, bg=DARK)
        hd.pack(fill="x")
        row = tk.Frame(hd, bg=DARK)
        row.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(row, text="foldersync", font=self.f_title,
                 fg="#7dd3fc", bg=DARK).pack(side="left")
        tk.Label(row, text="  同步控制台", font=self.f_norm,
                 fg="#e2e8f0", bg=DARK).pack(side="left", pady=(6, 0))
        self.hdr_info = tk.Label(hd, text="正在读取配置…", font=self.f_small,
                                 fg="#94a3b8", bg=DARK)
        self.hdr_info.pack(fill="x", padx=16, pady=(0, 10))

    def _build_controls(self):
        card = tk.Frame(self.root, bg=CARD,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=14, pady=(14, 8))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)

        row1 = tk.Frame(inner, bg=CARD)
        row1.pack(fill="x")
        self._chk(row1, "自动同步", self.auto_var, self.on_toggle_auto).pack(side="left")
        tk.Label(row1, text="间隔", font=self.f_norm, fg=SUB,
                 bg=CARD).pack(side="left", padx=(14, 4))
        self.spin = ttk.Spinbox(row1, from_=1, to=720, width=5,
                                textvariable=self.interval_var, font=self.f_norm)
        self.spin.pack(side="left")
        tk.Label(row1, text="分钟", font=self.f_norm, fg=SUB,
                 bg=CARD).pack(side="left", padx=(4, 8))
        self._btn_sec(row1, "应用间隔", self.on_apply_interval).pack(side="left")
        self.sync_btn = self._btn_primary(row1, "立即同步", self.sync_now)
        self.sync_btn.pack(side="right")

        row2 = tk.Frame(inner, bg=CARD)
        row2.pack(fill="x", pady=(10, 0))
        self._chk(row2, "关窗自动同步（还有未上传的修改时，先补一次同步再退出）",
                  self.close_sync_var, self.on_toggle_close_sync).pack(side="left")

    def _build_status(self):
        srow = tk.Frame(self.root, bg=BG)
        srow.pack(fill="x", padx=18, pady=(2, 6))
        self.dot = tk.Label(srow, text="●", font=("Microsoft YaHei UI", 11),
                            fg=GREEN, bg=BG)
        self.dot.pack(side="left")
        tk.Label(srow, textvariable=self.status_var, font=self.f_norm,
                 fg=INK, bg=BG).pack(side="left", padx=(7, 0))

    def _build_log(self):
        card = tk.Frame(self.root, bg=CARD,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=14, pady=(2, 14))
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=14, pady=(10, 6))
        tk.Label(head, text="同步日志", font=self.f_bold,
                 fg=SUB, bg=CARD).pack(side="left")
        self.logbox = tk.Text(card, height=12, state="disabled", font=self.f_log,
                              bg=DARK, fg="#cbd5e1", relief="flat", bd=0,
                              padx=12, pady=8, selectbackground="#334155",
                              wrap="none")
        self.logbox.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        for tag, color in (("info", "#cbd5e1"), ("ok", "#4ade80"),
                           ("warn", "#fbbf24"), ("err", "#f87171")):
            self.logbox.tag_configure(tag, foreground=color)

    # ================================================================ 初始化

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
            self.set_status("引擎初始化失败（检查 config.toml）", "err")
        except Exception as e:
            self.append_log("引擎初始化失败：%r" % e)
            self.set_status("引擎初始化失败（检查 config.toml）", "err")

        try:
            st = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if st.get("auto"):
                self.auto_var.set(True)
                self.auto_on = True
        except Exception:
            pass

    def set_info(self, text):
        self.hdr_info.config(text=text)

    def set_status(self, text, kind="ok"):
        self.status_var.set(text)
        self.dot.config(fg={"ok": GREEN, "sync": AMBER,
                            "err": RED, "idle": SUB}[kind])

    # ================================================================ 同步执行

    def sync_now(self):
        if self.engine is None:
            self.append_log("引擎未就绪，无法同步（检查 config.toml 与网络）。")
            return
        if self.syncing:
            return
        self.syncing = True
        self.sync_btn.config(state="disabled", text="同步中…")
        self.set_status("同步中…", "sync")

        def work():
            try:
                s = self.engine.run_once()
                msg = ("上次同步 %s：推送 %d、拉取 %d、删除 %d、冲突 %d"
                       % (time.strftime("%H:%M:%S"), s["push"], s["pull"],
                          s["delete"], s["conflict"]))
                self.q.put("[状态] " + msg)
                self.root.after(0, lambda: self.set_status(msg, "ok"))
            except Exception as e:
                self.root.after(0, lambda: self.set_status("同步出错，见日志", "err"))
                self.q.put("[错误] 同步失败：%r" % e)
            finally:
                self.syncing = False
                self.root.after(0, lambda: self.sync_btn.config(
                    state="normal", text="立即同步"))
                if self.quit_after_sync and not self.final_sync_started:
                    self.root.after(0, self._force_quit)

        threading.Thread(target=work, daemon=True).start()

    # ================================================================ 自动同步

    def on_toggle_auto(self):
        self.auto_on = bool(self.auto_var.get())
        if self.auto_on:
            self.next_ts = time.time() + self.interval_min * 60
            self.append_log("自动同步已开启，间隔 %d 分钟。" % self.interval_min)
        else:
            self.next_ts = None
            self.set_status("自动同步已关闭。", "idle")
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

    # ================================================================ 界面事件

    def poll_queue(self):
        if self.closing:
            return
        try:
            while True:
                self.append_log(self.q.get_nowait())
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
                self.set_status("距下次自动同步：%02d:%02d" % (m, s), "idle")
        self.root.after(1000, self.tick)

    def append_log(self, line):
        tag = "info"
        if line.startswith("[错误]"):
            tag = "err"
        elif "冲突" in line:
            tag = "warn"
        elif line.startswith(("[状态]", "[关窗同步]")):
            tag = "ok"
        self.logbox.configure(state="normal")
        self.logbox.insert("end", time.strftime("[%H:%M:%S] ") + line + "\n", tag)
        if float(self.logbox.index("end-1c").split(".")[0]) > 800:
            self.logbox.delete("1.0", "200.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ================================================================ 关窗

    def on_close(self):
        # 关窗补同步进行中又点了一次关闭 → 强制退出
        if self.final_sync_started:
            self._force_quit()
            return
        # 还有同步在跑：等它跑完再退
        if self.syncing:
            self.quit_after_sync = True
            self.set_status("当前同步完成后退出…（再点一次关闭可立即退出）", "sync")
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
                self.set_status("有未上传的修改，关窗同步中…（再点一次关闭可跳过）", "sync")
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
        ttk.Style(root).theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
