"""語音輸入（Windows 版）核心邏輯：設定、硬體偵測、下載、AI 整理。

這個模組不依賴任何介面或作業系統專屬套件，可以在任何平台上做單元測試。
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "VoiceInput"
VERSION = "0.1.0"

# ---- 資料夾 ----
if os.name == "nt":
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
else:  # 只為了在其他平台跑測試
    DATA_DIR = Path.home() / f".{APP_NAME.lower()}"
MODELS_DIR = DATA_DIR / "models"
BIN_DIR = DATA_DIR / "bin"
CUDA_DIR = DATA_DIR / "cuda"
SETTINGS_PATH = DATA_DIR / "settings.json"
VOCAB_PATH = DATA_DIR / "vocab.txt"
PROMPTS_PATH = DATA_DIR / "prompts.json"
LOG_PATH = DATA_DIR / "voice_input.log"

# ---- 下載來源（皆已確認存在；llama.cpp 固定版本，避免新版改檔名）----
LLAMA_TAG = "b11643"
LLAMA_URLS = {
    "vulkan": f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/llama-{LLAMA_TAG}-bin-win-vulkan-x64.zip",
    "cpu": f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/llama-{LLAMA_TAG}-bin-win-cpu-x64.zip",
}
WHISPER_MODELS = {
    "large-v3-turbo": {
        "repo": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "files": ["config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"],
        "size_mb": 1620,
    },
    "small": {
        "repo": "Systran/faster-whisper-small",
        "files": ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"],
        "size_mb": 490,
    },
}
LLM_MODELS = {
    "7b": {
        "repo": "bartowski/Qwen2.5-7B-Instruct-GGUF",
        "file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        "size_mb": 4466,
    },
    "3b": {
        "repo": "bartowski/Qwen2.5-3B-Instruct-GGUF",
        "file": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        "size_mb": 1841,
    },
}
CUDA_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]  # PyPI 上的 NVIDIA 執行函式庫
CUDA_SIZE_MB = 1300

# ---- 辨識與整理設定 ----
SAMPLE_RATE = 16000
MIN_SECONDS = 0.4
MIN_RMS = 0.004
MIN_LLM_CHARS = 8
HOLD_SECONDS = 0.25  # 按住超過這個時間才算錄音，避免打字時誤觸

HALLUCINATIONS = ("字幕志愿者", "字幕志願者", "詞曲", "词曲", "請不吝點贊", "感谢观看", "謝謝觀看")
FILLERS = re.compile(r"(?:^|(?<=[，。、\s]))(?:嗯+|呃+|啊+|那個那個|就是說就是說)(?=[，。、\s]|$)")

_COMMON = (
    "只能使用口述中出現的資訊，日期、時間、人名與數字必須逐字照用口述原詞，"
    "不可自行添加日期、數字、祝福語或任何事實。純文字，不使用任何 Markdown 符號（不可出現 * # - `）。"
    "使用台灣繁體中文。只輸出結果本文，不要任何說明。"
)
STYLES = ["禮貌訊息", "輕鬆訊息", "條列筆記", "通知公告"]
PROMPTS = {
    "標點": (
        "你是語音輸入的文字修整工具。把使用者的口語逐字稿加上正確的全形標點符號、適當分段，"
        "修正明顯的同音錯字，刪除嗯、呃、那個等贅詞。不得增減或改寫原意，不要回答或回應內容。" + _COMMON
    ),
    "禮貌訊息": (
        "你是訊息撰寫助手。把口述內容整理成要傳給長輩、客戶或需要尊重的對象的訊息："
        "語氣禮貌、溫和、簡潔，以「您好，」開頭，結尾只能用「謝謝您」。"
        "內容以一到兩段通順的文字呈現，不要逐句條列；只有出現三項以上各自獨立的事項時，才用「1.」「2.」條列。"
        + _COMMON
    ),
    "輕鬆訊息": (
        "你是訊息撰寫助手。把口述內容整理成要傳給朋友、同學或同事的訊息：語氣自然親切、簡潔直接，"
        "不用敬語，不加開頭問候與結尾客套話。需要對方做的事用「1.」「2.」「3.」條列，一件事一行。" + _COMMON
    ),
    "條列筆記": (
        "你是個人筆記整理助手。把口述的零散想法整理成條理清楚的筆記：第一行寫一句重點，"
        "之後每個細節各佔一行，每行開頭都用「・」，合併重複內容。" + _COMMON
    ),
    "通知公告": (
        "你是通知撰寫助手。把口述內容整理成清楚的通知：第一行寫主旨，"
        "接著依序列出時間、地點、需要準備或注意的事項，每項一行並以「・」開頭，沒有提到的項目不要寫。" + _COMMON
    ),
}

DEFAULT_SETTINGS = {"style": "禮貌訊息", "ai_punct": True, "use_llm": True, "autostart": False}
settings = dict(DEFAULT_SETTINGS)


def ensure_dirs():
    for d in (DATA_DIR, MODELS_DIR, BIN_DIR, CUDA_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    ensure_dirs()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        for k, v in DEFAULT_SETTINGS.items():
            settings[k] = type(v)(data.get(k, v))
    except Exception:
        pass
    # 允許使用者在 prompts.json 覆寫或新增風格
    try:
        custom = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
        for name, text in custom.items():
            if isinstance(name, str) and isinstance(text, str) and text.strip():
                PROMPTS[name] = text
                if name != "標點" and name not in STYLES:
                    STYLES.append(name)
    except Exception:
        pass
    if settings["style"] not in STYLES:
        settings["style"] = STYLES[0]
    return settings


def save_settings():
    try:
        ensure_dirs()
        SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_vocab() -> str:
    """使用者自訂詞彙（vocab.txt，一行一個或用逗號分隔）。"""
    try:
        text = VOCAB_PATH.read_text(encoding="utf-8")
        words = [w.strip() for w in re.split(r"[\n,，、]", text) if w.strip() and not w.startswith("#")]
        return "、".join(words)
    except Exception:
        return ""


def initial_prompt() -> str:
    vocab = load_vocab()
    base = "以下是繁體中文的語音內容，請加上標點符號。"
    return base + (f"常見詞彙：{vocab}。" if vocab else "")


# ---- 文字處理 ----
_cc = None


def to_traditional(text: str) -> str:
    global _cc
    if _cc is None:
        from opencc import OpenCC

        _cc = OpenCC("s2twp")
    return _cc.convert(text)


def clean(text: str) -> str:
    text = to_traditional(text.strip())
    text = FILLERS.sub("", text)
    text = re.sub(r"([，。！？；：])[，、]+", r"\1", text)  # 去掉贅詞後留下的重複標點
    text = re.sub(r"，{2,}", "，", text).lstrip("，、 ")
    return re.sub(r"\s{2,}", " ", text).strip()


def strip_markdown(text: str) -> str:
    """LINE 與多數輸入框不吃 Markdown：移除模型偶爾帶出的符號。"""
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"(?m)^\s*#+\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "・", text)
    return text.strip()


def is_hallucination(text: str) -> bool:
    return any(h in text for h in HALLUCINATIONS) and len(text) < 20


def resample_to_16k(audio, sr: int):
    """把任意取樣率的單聲道音訊轉成 16kHz（語音辨識用）。"""
    import numpy as np

    audio = np.asarray(audio, dtype=np.float32).flatten()
    if sr == SAMPLE_RATE:
        return audio
    if sr % SAMPLE_RATE == 0:
        k = sr // SAMPLE_RATE
        n = (len(audio) // k) * k
        return audio[:n].reshape(-1, k).mean(axis=1)
    n_out = int(len(audio) * SAMPLE_RATE / sr)
    x_old = np.linspace(0, 1, num=len(audio), endpoint=False)
    x_new = np.linspace(0, 1, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


# ---- 硬體偵測與方案 ----
def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def detect_gpu() -> dict:
    """用 nvidia-smi 偵測 NVIDIA 顯卡與顯示記憶體（MB）。沒有或失敗就回傳 nvidia=False。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=_no_window(),
        )
        if out.returncode != 0:
            return {"nvidia": False, "name": "", "vram_mb": 0}
        return parse_nvidia_smi(out.stdout)
    except Exception:
        return {"nvidia": False, "name": "", "vram_mb": 0}


def parse_nvidia_smi(stdout: str) -> dict:
    best = {"nvidia": False, "name": "", "vram_mb": 0}
    for line in stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[-1].isdigit():
            vram = int(parts[-1])
            if vram > best["vram_mb"]:
                best = {"nvidia": True, "name": ",".join(parts[:-1]), "vram_mb": vram}
    return best


def choose_plan(gpu: dict, want_llm: bool = True) -> dict:
    """依顯卡決定使用哪些模型。"""
    if gpu["nvidia"] and gpu["vram_mb"] >= 4000:
        plan = {"device": "cuda", "whisper": "large-v3-turbo", "compute": "int8_float16", "llm": None}
        if want_llm:
            if gpu["vram_mb"] >= 10000:
                plan["llm"] = "7b"
            elif gpu["vram_mb"] >= 5500:
                plan["llm"] = "3b"
        return plan
    # 沒有夠用的 NVIDIA 顯卡：用 CPU 跑較小的模型，不做 AI 整理
    return {"device": "cpu", "whisper": "small", "compute": "int8", "llm": None}


def plan_download_mb(plan: dict) -> int:
    total = WHISPER_MODELS[plan["whisper"]]["size_mb"]
    if plan["device"] == "cuda":
        total += CUDA_SIZE_MB
    if plan["llm"]:
        total += LLM_MODELS[plan["llm"]]["size_mb"] + 35
    return total


# ---- 下載 ----
class DownloadError(Exception):
    pass


def download_file(url: str, dest: Path, progress=None, retries: int = 3):
    """下載檔案，支援中斷續傳。progress(bytes_done, total_or_0) 會被持續呼叫。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last_err = None
    for attempt in range(retries):
        try:
            have = part.stat().st_size if part.exists() else 0
            req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
            if have:
                req.add_header("Range", f"bytes={have}-")
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = getattr(resp, "status", 200)
                if have and status != 206:  # 伺服器不支援續傳，重來
                    have = 0
                    part.unlink(missing_ok=True)
                length = int(resp.headers.get("Content-Length") or 0)
                total = have + length if length else 0
                with open(part, "ab" if have else "wb") as f:
                    done = have
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            if total and part.stat().st_size != total:
                raise DownloadError("檔案大小不符，下載可能不完整")
            os.replace(part, dest)
            return dest
        except Exception as e:  # 網路中斷等，稍待重試並續傳
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise DownloadError(f"下載失敗：{url}（{last_err}）")


def hf_url(repo: str, filename: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


def whisper_dir(name: str) -> Path:
    return MODELS_DIR / f"whisper-{name}"


def llm_path(key: str) -> Path:
    return MODELS_DIR / LLM_MODELS[key]["file"]


def llama_server_path() -> Path:
    return BIN_DIR / "llama" / "llama-server.exe"


def whisper_ready(name: str) -> bool:
    d = whisper_dir(name)
    return all((d / f).exists() for f in WHISPER_MODELS[name]["files"])


def ensure_whisper(name: str, progress=None):
    spec = WHISPER_MODELS[name]
    d = whisper_dir(name)
    for f in spec["files"]:
        target = d / f
        if not target.exists():
            download_file(hf_url(spec["repo"], f), target, progress)


def ensure_llm(key: str, progress=None):
    spec = LLM_MODELS[key]
    target = llm_path(key)
    if not target.exists():
        download_file(hf_url(spec["repo"], spec["file"]), target, progress)


def ensure_llama_server(progress=None, flavor: str = "vulkan"):
    exe = llama_server_path()
    if exe.exists():
        return exe
    zip_path = BIN_DIR / f"llama-{flavor}.zip"
    download_file(LLAMA_URLS[flavor], zip_path, progress)
    out = BIN_DIR / "llama"
    shutil.rmtree(out, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    zip_path.unlink(missing_ok=True)
    # 有些版本壓縮檔多一層資料夾
    if not exe.exists():
        for found in out.rglob("llama-server.exe"):
            for item in found.parent.iterdir():
                shutil.move(str(item), str(out / item.name))
            break
    if not exe.exists():
        raise DownloadError("解壓後找不到 llama-server.exe")
    return exe


def pick_wheel(pypi_json: dict) -> tuple[str, str]:
    """從 PyPI 的 JSON 找出 Windows 64 位元的 wheel，回傳 (檔名, 下載網址)。"""
    for f in pypi_json.get("urls", []):
        if "win_amd64" in f.get("filename", "") and f.get("packagetype") == "bdist_wheel":
            return f["filename"], f["url"]
    raise DownloadError("PyPI 上找不到 Windows 版的 wheel")


def extract_dlls(wheel_path: Path, dest: Path) -> int:
    """把 wheel（zip）裡 nvidia/*/bin/*.dll 全部攤平放到 dest。"""
    n = 0
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel_path) as z:
        for info in z.infolist():
            p = info.filename
            if p.lower().endswith(".dll") and "/bin/" in p:
                with z.open(info) as src, open(dest / Path(p).name, "wb") as out:
                    shutil.copyfileobj(src, out)
                n += 1
    return n


def cuda_ready() -> bool:
    return (CUDA_DIR / ".complete").exists()


def ensure_cuda_libs(progress=None):
    if cuda_ready():
        return
    for pkg in CUDA_PACKAGES:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30) as r:
            name, url = pick_wheel(json.loads(r.read().decode("utf-8")))
        wheel = BIN_DIR / name
        download_file(url, wheel, progress)
        if extract_dlls(wheel, CUDA_DIR) == 0:
            raise DownloadError(f"{pkg} 裡沒有找到 DLL")
        wheel.unlink(missing_ok=True)
    (CUDA_DIR / ".complete").write_text("ok")


def add_cuda_dll_dir():
    """讓 CTranslate2 找得到我們下載的 CUDA DLL。"""
    if CUDA_DIR.exists():
        os.environ["PATH"] = str(CUDA_DIR) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(CUDA_DIR))


# ---- 本地 LLM（llama-server）----
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    def __init__(self, exe: Path, model: Path):
        self.exe, self.model = Path(exe), Path(model)
        self.port = free_port()
        self.proc = None

    def start(self, timeout: float = 180):
        cmd = [
            str(self.exe), "-m", str(self.model), "--host", "127.0.0.1",
            "--port", str(self.port), "-ngl", "99", "-c", "4096",
        ]
        log = open(DATA_DIR / "llama-server.log", "wb")
        self.proc = subprocess.Popen(
            cmd, stdout=log, stderr=log, cwd=str(self.exe.parent), creationflags=_no_window()
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("llama-server 啟動後立刻結束，請看 llama-server.log")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(1)
        self.stop()
        raise RuntimeError("llama-server 啟動逾時")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None

    def chat(self, system: str, user: str, max_tokens: int) -> str:
        body = json.dumps({
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


def polish(raw: str, mode: str, server, log=print) -> str:
    """用本地 LLM 整理文字；任何異常都退回原文。mode: punct / digest。"""
    if server is None or len(raw) < MIN_LLM_CHARS:
        return raw
    if mode == "punct" and not settings["ai_punct"]:
        return raw
    key = "標點" if mode == "punct" else settings["style"]
    try:
        out = server.chat(PROMPTS[key], raw, max_tokens=min(800, len(raw) * 3 + 100))
        out = strip_markdown(to_traditional(out.strip()))
        if not out:
            return raw
        if mode == "punct" and not (0.5 * len(raw) <= len(out) <= 1.6 * len(raw) + 10):
            log(f"（AI 輸出長度異常，改用原文）{out}")
            return raw
        if mode == "digest" and len(out) > len(raw) * 3 + 60:
            log(f"（AI 輸出過長，改用原文）{out}")
            return raw
        return out
    except Exception as e:
        log(f"AI 整理失敗，改用原文：{e}")
        return raw
