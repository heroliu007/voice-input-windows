"""語音輸入（Windows 版）：按住快捷鍵說話，放開後自動貼到游標位置。

右 Ctrl  → 標點修整（補標點、去贅詞）
右 Shift → 匯整（依系統匣選單選的風格整理成訊息／筆記）
全程本地執行，不上傳語音。
"""
import atexit
import logging
import os
import queue
import sys
import threading
import time
import traceback
from collections import deque
from logging.handlers import RotatingFileHandler

import numpy as np

import core

# ---- 日誌：視窗版 exe 沒有 stdout，一律寫到檔案 ----
core.ensure_dirs()
_handler = RotatingFileHandler(core.LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
log = logging.getLogger("voice")
log.setLevel(logging.INFO)
log.addHandler(_handler)
if getattr(sys, "frozen", False) or sys.stdout is None:
    sys.stdout = sys.stderr = open(core.DATA_DIR / "stdout.log", "a", encoding="utf-8", buffering=1)


def logp(msg: str):
    log.info(msg)


# ============================================================
#  Windows 輔助
# ============================================================
def enable_dpi_awareness():
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def already_running() -> bool:
    """用具名 Mutex 避免重複開啟。"""
    try:
        import ctypes

        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\VoiceInputWinSingleInstance")
        globals()["_mutex"] = h  # 保持參照，程式結束才釋放
        return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, core.APP_NAME)
            return True
    except Exception:
        return False


def set_autostart(on: bool):
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                exe = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" "{os.path.abspath(__file__)}"'
                winreg.SetValueEx(k, core.APP_NAME, 0, winreg.REG_SZ, f'"{exe}"' if getattr(sys, "frozen", False) else exe)
            else:
                try:
                    winreg.DeleteValue(k, core.APP_NAME)
                except FileNotFoundError:
                    pass
    except Exception as e:
        logp(f"設定開機啟動失敗：{e}")


# ============================================================
#  介面（Tk 浮窗 + 系統匣）
# ============================================================
HUD_W, HUD_H = 240, 56
KEY_COLOR = "#010203"  # 透明色（圓角之外）

ui_queue: "queue.Queue" = queue.Queue()


def post(fn):
    """從任何執行緒要求主執行緒執行 fn。"""
    ui_queue.put(fn)


class Hud:
    def __init__(self, root):
        import tkinter as tk

        self.tk = tk
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", KEY_COLOR)
        except Exception:
            pass
        self.canvas = tk.Canvas(self.win, width=HUD_W, height=HUD_H, bg=KEY_COLOR, highlightthickness=0)
        self.canvas.pack()
        self.levels = deque([0.0] * 16, maxlen=16)
        self.mode = "idle"
        self.text = ""
        self.digest = False
        self.visible = False

    def show(self, mode: str, text: str = "", digest: bool = False):
        self.mode, self.text, self.digest = mode, text, digest
        if mode == "recording":
            self.levels.extend([0.0] * 16)
        if mode == "idle":
            self.win.withdraw()
            self.visible = False
            return
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{HUD_W}x{HUD_H}+{(sw - HUD_W) // 2}+{sh - HUD_H - 120}")
        self.win.deiconify()
        self.win.lift()
        self.visible = True
        self.draw()

    def push_level(self, level: float):
        self.levels.append(min(1.0, (level * 10) ** 0.6))
        if self.visible and self.mode == "recording":
            self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        r = HUD_H / 2
        fill = "#141414"
        c.create_oval(0, 0, HUD_H, HUD_H, fill=fill, outline=fill)
        c.create_oval(HUD_W - HUD_H, 0, HUD_W, HUD_H, fill=fill, outline=fill)
        c.create_rectangle(r, 0, HUD_W - r, HUD_H, fill=fill, outline=fill)
        if self.mode == "recording":
            color = "#66A6FF" if self.digest else "#FF5C5C"
            bar_w, gap = 5, 4
            n = len(self.levels)
            total = n * bar_w + (n - 1) * gap
            x0 = (HUD_W - total) / 2
            for i, lv in enumerate(self.levels):
                h = 6 + lv * (HUD_H - 20)
                x = x0 + i * (bar_w + gap)
                c.create_rectangle(x, (HUD_H - h) / 2, x + bar_w, (HUD_H + h) / 2, fill=color, outline=color)
        else:
            c.create_text(HUD_W / 2, HUD_H / 2, text=self.text, fill="white", font=("Microsoft JhengHei UI", 12))


def make_icon_image(state: str = "idle"):
    from PIL import Image, ImageDraw

    colors = {"idle": "#2E7D32", "recording": "#D32F2F", "digest": "#1565C0", "processing": "#EF6C00", "loading": "#757575"}
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=colors.get(state, "#2E7D32"))
    d.rounded_rectangle((25, 14, 39, 36), radius=7, fill="white")  # 麥克風本體
    d.arc((19, 24, 45, 46), start=0, end=180, fill="white", width=3)
    d.line((32, 46, 32, 52), fill="white", width=3)
    d.line((24, 52, 40, 52), fill="white", width=3)
    return img


class SetupWindow:
    """首次下載視窗：說明大小、讓使用者選擇、顯示進度。"""

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.win = tk.Toplevel(root)
        self.win.title("語音輸入 - 首次設定")
        self.win.geometry("460x240")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        self.title = tk.Label(self.win, text="", font=("Microsoft JhengHei UI", 12, "bold"), wraplength=420, justify="left")
        self.title.pack(anchor="w", padx=20, pady=(18, 6))
        self.detail = tk.Label(self.win, text="", wraplength=420, justify="left", fg="#444")
        self.detail.pack(anchor="w", padx=20)
        self.bar = ttk.Progressbar(self.win, length=420, mode="determinate", maximum=100)
        self.buttons = tk.Frame(self.win)
        self.closed = False
        self.choice_event = threading.Event()
        self.choice = None

    def _on_close(self):
        self.closed = True
        self.choice = self.choice or "quit"
        self.choice_event.set()

    def ask(self, title: str, detail: str, options: list[tuple[str, str]]):
        self.title.config(text=title)
        self.detail.config(text=detail)
        for w in self.buttons.winfo_children():
            w.destroy()
        for label, value in options:
            self.tk.Button(self.buttons, text=label, command=lambda v=value: self._pick(v), padx=10, pady=4).pack(side="left", padx=6)
        self.buttons.pack(side="bottom", pady=16)

    def _pick(self, value):
        self.choice = value
        self.choice_event.set()

    def progress_mode(self, title: str):
        self.buttons.pack_forget()
        self.title.config(text=title)
        self.bar.pack(padx=20, pady=18)

    def update(self, detail: str, pct: float):
        self.detail.config(text=detail)
        self.bar["value"] = pct

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# ============================================================
#  應用程式主體
# ============================================================
class App:
    def __init__(self):
        import tkinter as tk
        from pynput import keyboard

        self.keyboard = keyboard
        self.root = tk.Tk()
        self.root.withdraw()
        self.hud = Hud(self.root)
        self.icon = None
        self.setup_win = None

        self.hotkeys = {keyboard.Key.ctrl_r: "punct", keyboard.Key.shift_r: "digest"}
        self.jobs: "queue.Queue" = queue.Queue()
        self.model = None
        self.server = None
        self.plan = None
        self.ready = False

        # 錄音狀態
        self.lock = threading.Lock()
        self.active_key = None
        self.active_mode = "punct"
        self.committed = False
        self.stream = None
        self.frames: list = []
        self.samplerate = core.SAMPLE_RATE
        self.level = 0.0
        self.hold_timer = None
        self.listener = None

    # ---------- 介面輔助 ----------
    def set_state(self, state: str, text: str = "", hide_after: float | None = None):
        def apply():
            digest = self.active_mode == "digest"
            if state == "recording":
                self.hud.show("recording", digest=digest)
            elif state in ("processing", "message"):
                self.hud.show(state, text)
            else:
                self.hud.show("idle")
            if self.icon:
                key = "digest" if (state == "recording" and digest) else state
                try:
                    self.icon.icon = make_icon_image(key if key in ("idle", "recording", "digest", "processing") else "idle")
                except Exception:
                    pass
            if hide_after:
                token = object()
                self._hide_token = token
                self.root.after(int(hide_after * 1000), lambda: self.hud.show("idle") if getattr(self, "_hide_token", None) is token else None)

        post(apply)

    def notify(self, message: str):
        try:
            if self.icon:
                self.icon.notify(message, "語音輸入")
        except Exception:
            pass

    # ---------- 錄音 ----------
    def _audio_callback(self, indata, frames, t, status):
        self.level = float(np.sqrt(np.mean(indata**2)))
        self.frames.append(indata.copy())

    def _open_stream(self):
        import sounddevice as sd

        def try_open():
            dev = sd.query_devices(kind="input")
            self.samplerate = int(dev["default_samplerate"])
            st = sd.InputStream(samplerate=self.samplerate, channels=1, dtype="float32", callback=self._audio_callback)
            st.start()
            return st

        try:
            return try_open()
        except Exception as e:
            logp(f"開啟麥克風失敗，重新偵測裝置後重試：{e}")
            try:  # 裝置清單在 PortAudio 初始化時就固定了，換麥克風後要重新初始化
                sd._terminate()
                sd._initialize()
                return try_open()
            except Exception as e2:
                logp(f"無法開啟麥克風：{e2}")
                return None

    def on_press(self, key):
        if key in self.hotkeys:
            with self.lock:
                if self.active_key is not None:
                    return
                stream = self._open_stream()
                if stream is None:
                    self.set_state("message", "無法開啟麥克風", hide_after=2.5)
                    return
                self.frames = []
                self.stream = stream
                self.active_key, self.active_mode, self.committed = key, self.hotkeys[key], False
                self.hold_timer = threading.Timer(core.HOLD_SECONDS, self._commit, args=(key,))
                self.hold_timer.start()
        else:
            # 按住快捷鍵期間又按了別的鍵（例如 Ctrl+C）：放棄這次錄音，不要誤觸
            with self.lock:
                if self.active_key is not None and not self.committed:
                    self._cancel_locked()

    def _commit(self, key):
        with self.lock:
            if self.active_key == key and not self.committed:
                self.committed = True
                logp(f"● 錄音中（{'匯整' if self.active_mode == 'digest' else '標點修整'}）")
                self.set_state("recording")

    def _cancel_locked(self):
        if self.hold_timer:
            self.hold_timer.cancel()
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None
        self.active_key = None
        self.committed = False
        self.frames = []

    def on_release(self, key):
        with self.lock:
            if key != self.active_key:
                return
            was_committed = self.committed
            frames = self.frames
            mode = self.active_mode
            sr = self.samplerate
            self._cancel_locked()
        if not was_committed or not frames:
            return
        audio = core.resample_to_16k(np.concatenate(frames), sr)
        if len(audio) < core.SAMPLE_RATE * core.MIN_SECONDS:
            self.set_state("message", "按住久一點再說話", hide_after=1.2)
            return
        if float(np.sqrt(np.mean(audio**2))) < core.MIN_RMS:
            logp("（沒有聲音，略過）")
            self.set_state("message", "沒收到聲音，請檢查麥克風與隱私權設定", hide_after=3.5)
            return
        self.jobs.put((audio, mode))

    # ---------- 貼上 ----------
    def paste(self, text: str):
        try:
            import pyperclip

            old = ""
            try:
                old = pyperclip.paste()
            except Exception:
                pass
            pyperclip.copy(text)
            time.sleep(0.08)
            ctrl = self.keyboard.Controller()
            with ctrl.pressed(self.keyboard.Key.ctrl):
                ctrl.press("v")
                ctrl.release("v")
            time.sleep(0.4)  # 等目標程式讀完剪貼簿
            if old:
                pyperclip.copy(old)
        except Exception as e:
            logp(f"貼上失敗：{e}")
            self.set_state("message", "貼上失敗（文字已在剪貼簿）", hide_after=2.5)

    # ---------- 辨識與整理 ----------
    def process(self, audio, mode: str):
        self.set_state("processing", "辨識中…")
        try:
            t0 = time.time()
            segments, _ = self.model.transcribe(
                audio, language="zh", initial_prompt=core.initial_prompt(),
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
                beam_size=5 if self.plan["device"] == "cuda" else 1,
            )
            raw = core.clean("".join(s.text for s in segments))
            logp(f"[辨識 {time.time() - t0:.2f}s] {raw}")
            if not raw or core.is_hallucination(raw):
                self.set_state("idle")
                return
            text = raw
            wants_ai = mode == "digest" or core.settings["ai_punct"]
            if self.server is not None and wants_ai and len(raw) >= core.MIN_LLM_CHARS:
                self.set_state("processing", "整理中…")
                t1 = time.time()
                text = core.polish(raw, mode, self.server, logp)
                logp(f"[整理 {time.time() - t1:.2f}s] {text}")
            self.paste(text)
            self.set_state("idle")
        except Exception as e:
            logp(f"轉錄失敗：{e}\n{traceback.format_exc()}")
            self.set_state("message", "轉錄失敗", hide_after=2.5)

    # ---------- 啟動流程（背景執行緒）----------
    def _ask_user(self, title, detail, options):
        done = threading.Event()

        def show():
            self.setup_win = self.setup_win or SetupWindow(self.root)
            self.setup_win.choice_event.clear()
            self.setup_win.choice = None
            self.setup_win.ask(title, detail, options)
            done.set()

        post(show)
        done.wait()
        self.setup_win.choice_event.wait()
        return self.setup_win.choice

    def _progress_fn(self, label: str):
        last = [0.0]

        def cb(done, total):
            now = time.time()
            if now - last[0] < 0.2 and done != total:
                return
            last[0] = now
            pct = (done / total * 100) if total else 0
            txt = f"{label}\n已下載 {done / 1e6:,.0f} MB" + (f" / {total / 1e6:,.0f} MB" if total else "")
            post(lambda: self.setup_win and self.setup_win.update(txt, pct))

        return cb

    def startup(self):
        try:
            self._startup()
        except Exception as e:
            logp(f"啟動失敗：{e}\n{traceback.format_exc()}")
            self.set_state("message", "啟動失敗，請看紀錄檔", hide_after=8)
            self.notify(f"啟動失敗：{e}")

    def _startup(self):
        gpu = core.detect_gpu()
        logp(f"顯卡：{gpu}")
        plan = core.choose_plan(gpu, core.settings["use_llm"])
        needs = []
        if plan["device"] == "cuda" and not core.cuda_ready():
            needs.append(core.CUDA_SIZE_MB)
        if not core.whisper_ready(plan["whisper"]):
            needs.append(core.WHISPER_MODELS[plan["whisper"]]["size_mb"])
        llm_mb = 0
        if plan["llm"] and not (core.llm_path(plan["llm"]).exists() and core.llama_server_path().exists()):
            llm_mb = core.LLM_MODELS[plan["llm"]]["size_mb"] + 35
            needs.append(llm_mb)

        if needs:
            total_gb = sum(needs) / 1024
            where = f"偵測到顯卡：{gpu['name']}（{gpu['vram_mb'] // 1024} GB）\n" if gpu["nvidia"] else "沒有偵測到 NVIDIA 顯卡，將使用 CPU 模式（速度較慢，不含 AI 整理）。\n"
            opts = [(f"下載並開始（約 {total_gb:.1f} GB）", "all")]
            if llm_mb:
                opts.append((f"只要語音轉文字（約 {(sum(needs) - llm_mb) / 1024:.1f} GB）", "no_llm"))
            opts.append(("取消", "quit"))
            choice = self._ask_user(
                "第一次使用，需要下載 AI 模型",
                f"{where}所有處理都在你的電腦上完成，不會上傳語音。\n只需下載一次，之後可離線使用。",
                opts,
            )
            if choice in (None, "quit"):
                post(self.quit)
                return
            if choice == "no_llm":
                core.settings["use_llm"] = False
                core.save_settings()
                plan = core.choose_plan(gpu, False)
            post(lambda: self.setup_win.progress_mode("正在下載，請保持網路連線"))

        self.plan = plan
        step = 0

        def stage(label):
            nonlocal step
            step += 1
            return self._progress_fn(f"({step}) {label}")

        if plan["device"] == "cuda":
            if not core.cuda_ready():
                core.ensure_cuda_libs(stage("下載 NVIDIA 執行函式庫"))
            core.add_cuda_dll_dir()
        if not core.whisper_ready(plan["whisper"]):
            core.ensure_whisper(plan["whisper"], stage("下載語音辨識模型"))
        if plan["llm"]:
            if not core.llama_server_path().exists():
                core.ensure_llama_server(stage("下載 AI 執行程式"))
            if not core.llm_path(plan["llm"]).exists():
                core.ensure_llm(plan["llm"], stage("下載 AI 整理模型"))

        post(lambda: self.setup_win and self.setup_win.update("正在載入模型…", 100))
        self.set_state("processing", "載入模型中…")
        self.load_models()

        if self.setup_win:
            post(self.setup_win.close)
        self.ready = True
        self.listener = self.keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self.listener.start()
        self.set_state("idle")
        self.notify("就緒。右 Ctrl＝標點修整，右 Shift＝匯整。")
        logp("就緒")

    def load_models(self):
        from faster_whisper import WhisperModel

        plan = self.plan
        try:
            self.model = WhisperModel(str(core.whisper_dir(plan["whisper"])), device=plan["device"], compute_type=plan["compute"])
            # 暖機：GPU 函式庫缺失或驅動太舊會在這裡才出錯
            list(self.model.transcribe(np.zeros(core.SAMPLE_RATE, dtype=np.float32), language="zh")[0])
        except Exception as e:
            if plan["device"] != "cuda":
                raise
            logp(f"GPU 載入失敗，改用 CPU：{e}")
            self.notify("顯卡無法使用（驅動可能太舊），改用 CPU 模式，速度會較慢。")
            plan = {"device": "cpu", "whisper": "small", "compute": "int8", "llm": None}
            if not core.whisper_ready("small"):
                core.ensure_whisper("small", self._progress_fn("下載 CPU 模式模型"))
            self.plan = plan
            self.model = WhisperModel(str(core.whisper_dir("small")), device="cpu", compute_type="int8")

        if plan["llm"]:
            try:
                srv = core.LlamaServer(core.llama_server_path(), core.llm_path(plan["llm"]))
                srv.start()
                atexit.register(srv.stop)
                self.server = srv
            except Exception as e:
                logp(f"AI 整理模型啟動失敗，將只做語音轉文字：{e}")
                self.notify("AI 整理無法啟動，將只做語音轉文字。")

    def worker_loop(self):
        while True:
            audio, mode = self.jobs.get()
            self.process(audio, mode)

    # ---------- 系統匣 ----------
    def build_tray(self):
        import pystray
        from pystray import MenuItem as Item

        def style_item(name):
            return Item(
                name,
                lambda icon, item, n=name: (core.settings.__setitem__("style", n), core.save_settings()),
                checked=lambda item, n=name: core.settings["style"] == n,
                radio=True,
            )

        def toggle_ai(icon, item):
            core.settings["ai_punct"] = not core.settings["ai_punct"]
            core.save_settings()

        def toggle_autostart(icon, item):
            on = not autostart_enabled()
            set_autostart(on)
            core.settings["autostart"] = on
            core.save_settings()

        def open_vocab(icon, item):
            if not core.VOCAB_PATH.exists():
                core.VOCAB_PATH.write_text(
                    "# 把常被聽錯的名字或專有名詞寫在這裡，一行一個（# 開頭是註解）\n", encoding="utf-8"
                )
            os.startfile(str(core.VOCAB_PATH))

        menu = pystray.Menu(
            Item("右 Ctrl：標點修整", None, enabled=False),
            Item("右 Shift：匯整", None, enabled=False),
            pystray.Menu.SEPARATOR,
            Item("匯整風格", pystray.Menu(*[style_item(s) for s in core.STYLES])),
            Item("標點修整也用 AI（較慢但更通順）", toggle_ai, checked=lambda i: core.settings["ai_punct"]),
            pystray.Menu.SEPARATOR,
            Item("開機自動啟動", toggle_autostart, checked=lambda i: autostart_enabled()),
            Item("編輯自訂詞彙", open_vocab),
            Item("開啟資料夾（含紀錄檔）", lambda i, it: os.startfile(str(core.DATA_DIR))),
            pystray.Menu.SEPARATOR,
            Item("結束", lambda i, it: post(self.quit)),
        )
        self.icon = pystray.Icon(core.APP_NAME, make_icon_image("loading"), "語音輸入", menu)
        self.icon.run_detached()

    def quit(self):
        try:
            if self.listener:
                self.listener.stop()
            if self.server:
                self.server.stop()
            if self.icon:
                self.icon.stop()
        finally:
            self.root.quit()

    # ---------- 主迴圈 ----------
    def pump(self):
        try:
            while True:
                ui_queue.get_nowait()()
        except queue.Empty:
            pass
        except Exception:
            logp(traceback.format_exc())
        if self.hud.visible and self.hud.mode == "recording":
            self.hud.push_level(self.level)
        self.root.after(33, self.pump)

    def run(self):
        self.build_tray()
        threading.Thread(target=self.startup, daemon=True).start()
        threading.Thread(target=self.worker_loop, daemon=True).start()
        self.root.after(33, self.pump)
        self.root.mainloop()


# ============================================================
#  自我檢測（CI 用）：驗證打包後的 exe 所有元件都能載入並實際辨識
# ============================================================
def selftest() -> int:
    import tempfile
    from pathlib import Path

    lines = []
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            lines.append(f"OK   {name}: {fn()}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL {name}: {e!r}\n{traceback.format_exc()}")

    check("core.clean", lambda: core.clean("体育课，嗯，练习羽毛球"))
    check("numpy", lambda: np.__version__)
    check("sounddevice", lambda: __import__("sounddevice").__version__)
    check("pynput", lambda: __import__("pynput.keyboard", fromlist=["Key"]).Key.ctrl_r)
    check("pystray", lambda: __import__("pystray").__name__)
    check("pyperclip", lambda: __import__("pyperclip").__name__)
    check("tray icon image", lambda: make_icon_image("recording").size)

    def tk_check():
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        Hud(r).show("message", "測試")
        r.update()
        r.destroy()
        return "ok"

    check("tkinter+HUD", tk_check)

    def ct2():
        import ctranslate2

        return f"{ctranslate2.__version__}, cuda devices={ctranslate2.get_cuda_device_count()}"

    check("ctranslate2", ct2)

    def whisper_tiny():
        import faster_whisper
        from faster_whisper import WhisperModel

        assets = Path(faster_whisper.__file__).parent / "assets"
        names = sorted(p.name for p in assets.glob("*")) if assets.exists() else []
        if not any("silero" in n for n in names):
            raise RuntimeError(f"找不到 silero VAD 資源：{names}")
        d = Path(tempfile.mkdtemp())
        repo = "Systran/faster-whisper-tiny"
        for f in ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]:
            core.download_file(core.hf_url(repo, f), d / f)
        m = WhisperModel(str(d), device="cpu", compute_type="int8")
        t = np.linspace(0, 1, core.SAMPLE_RATE, endpoint=False)
        audio = (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        segs, _ = m.transcribe(audio, language="zh", vad_filter=True)
        list(segs)
        return f"載入與辨識流程正常；assets={names}"

    check("faster-whisper(tiny, CPU)", whisper_tiny)

    out = core.DATA_DIR / "selftest.txt"
    out.write_text("\n".join(lines) + f"\nRESULT: {'PASS' if ok else 'FAIL'}\n", encoding="utf-8")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    enable_dpi_awareness()
    if already_running():
        logp("已有另一個執行中的語音輸入，結束。")
        return
    core.load_settings()
    App().run()


if __name__ == "__main__":
    main()
