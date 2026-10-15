import http.server
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402


class TextTests(unittest.TestCase):
    def test_clean_converts_to_traditional_and_removes_fillers(self):
        out = core.clean("今天的体育课，嗯，要练习羽毛球。")
        self.assertIn("體育課", out)
        self.assertNotIn("嗯", out)

    def test_clean_no_duplicate_punctuation_after_filler_removal(self):
        out = core.clean("體育課，嗯，練習羽球，呃，然後準備球拍。")
        self.assertNotIn("，，", out)
        self.assertEqual(out, "體育課，練習羽球，然後準備球拍。")

    def test_clean_leading_filler(self):
        self.assertEqual(core.clean("嗯，今天要上課"), "今天要上課")

    def test_clean_keeps_normal_words(self):
        self.assertIn("啊哈", core.clean("他說啊哈就走了"))

    def test_strip_markdown(self):
        out = core.strip_markdown("**重點**\n- 第一項\n# 標題\n`code`")
        self.assertNotIn("*", out)
        self.assertNotIn("#", out)
        self.assertNotIn("`", out)
        self.assertIn("・第一項", out)

    def test_hallucination(self):
        self.assertTrue(core.is_hallucination("字幕志愿者 杨栋梁"))
        self.assertFalse(core.is_hallucination("今天天氣很好，我們去打羽球吧，順便買點東西回家，明天還要上班"))


class AudioTests(unittest.TestCase):
    def test_resample_integer_ratio(self):
        a = np.ones(48000, dtype=np.float32)
        out = core.resample_to_16k(a, 48000)
        self.assertEqual(len(out), 16000)
        self.assertAlmostEqual(float(out.mean()), 1.0, places=5)

    def test_resample_non_integer_ratio(self):
        a = np.sin(np.linspace(0, 100, 44100)).astype(np.float32)
        out = core.resample_to_16k(a, 44100)
        self.assertAlmostEqual(len(out) / 16000, 1.0, delta=0.01)

    def test_resample_noop(self):
        a = np.zeros(1600, dtype=np.float32)
        self.assertEqual(len(core.resample_to_16k(a, 16000)), 1600)


class HardwareTests(unittest.TestCase):
    def test_parse_single_gpu(self):
        r = core.parse_nvidia_smi("NVIDIA GeForce RTX 4060, 8188\n")
        self.assertTrue(r["nvidia"])
        self.assertEqual(r["vram_mb"], 8188)

    def test_parse_picks_largest(self):
        r = core.parse_nvidia_smi("GPU A, 4096\nGPU B, 12288\n")
        self.assertEqual(r["vram_mb"], 12288)

    def test_parse_garbage(self):
        self.assertFalse(core.parse_nvidia_smi("N/A\n")["nvidia"])

    def test_plan_big_gpu(self):
        p = core.choose_plan({"nvidia": True, "name": "x", "vram_mb": 12000})
        self.assertEqual((p["device"], p["whisper"], p["llm"]), ("cuda", "large-v3-turbo", "7b"))

    def test_plan_mid_gpu(self):
        p = core.choose_plan({"nvidia": True, "name": "x", "vram_mb": 8000})
        self.assertEqual(p["llm"], "3b")

    def test_plan_small_gpu_no_llm(self):
        p = core.choose_plan({"nvidia": True, "name": "x", "vram_mb": 4096})
        self.assertEqual((p["device"], p["llm"]), ("cuda", None))

    def test_plan_no_gpu_uses_cpu(self):
        p = core.choose_plan({"nvidia": False, "name": "", "vram_mb": 0})
        self.assertEqual((p["device"], p["whisper"], p["llm"]), ("cpu", "small", None))

    def test_plan_llm_disabled(self):
        p = core.choose_plan({"nvidia": True, "name": "x", "vram_mb": 16000}, want_llm=False)
        self.assertIsNone(p["llm"])

    def test_download_size(self):
        p = core.choose_plan({"nvidia": True, "name": "x", "vram_mb": 12000})
        self.assertGreater(core.plan_download_mb(p), 7000)


class WheelTests(unittest.TestCase):
    def test_pick_wheel(self):
        data = {"urls": [
            {"filename": "pkg-1-py3-none-manylinux.whl", "packagetype": "bdist_wheel", "url": "u1"},
            {"filename": "pkg-1-py3-none-win_amd64.whl", "packagetype": "bdist_wheel", "url": "u2"},
        ]}
        self.assertEqual(core.pick_wheel(data), ("pkg-1-py3-none-win_amd64.whl", "u2"))

    def test_pick_wheel_missing(self):
        with self.assertRaises(core.DownloadError):
            core.pick_wheel({"urls": []})

    def test_extract_dlls(self):
        with tempfile.TemporaryDirectory() as d:
            whl = Path(d) / "x.whl"
            with zipfile.ZipFile(whl, "w") as z:
                z.writestr("nvidia/cublas/bin/cublas64_12.dll", b"AAA")
                z.writestr("nvidia/cublas/include/cublas.h", b"hdr")
                z.writestr("nvidia/cudnn/bin/cudnn64_9.dll", b"BBB")
            out = Path(d) / "out"
            self.assertEqual(core.extract_dlls(whl, out), 2)
            self.assertEqual((out / "cublas64_12.dll").read_bytes(), b"AAA")
            self.assertFalse((out / "cublas.h").exists())


class RangeHandler(http.server.BaseHTTPRequestHandler):
    PAYLOAD = bytes(range(256)) * 4000  # 約 1MB

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        data = self.PAYLOAD
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ChatHandler(http.server.BaseHTTPRequestHandler):
    REPLY = "你好，這是整理後的內容。"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        body = json.dumps({"choices": [{"message": {"content": self.REPLY}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class DownloadTests(unittest.TestCase):
    def test_full_download(self):
        srv = serve(RangeHandler)
        try:
            with tempfile.TemporaryDirectory() as d:
                dest = Path(d) / "a.bin"
                seen = []
                core.download_file(f"http://127.0.0.1:{srv.server_port}/f", dest, lambda a, b: seen.append((a, b)))
                self.assertEqual(dest.read_bytes(), RangeHandler.PAYLOAD)
                self.assertFalse(dest.with_suffix(".bin.part").exists())
                self.assertEqual(seen[-1][0], len(RangeHandler.PAYLOAD))
        finally:
            srv.shutdown()

    def test_resume_download(self):
        srv = serve(RangeHandler)
        try:
            with tempfile.TemporaryDirectory() as d:
                dest = Path(d) / "a.bin"
                part = Path(d) / "a.bin.part"
                part.write_bytes(RangeHandler.PAYLOAD[:300000])
                core.download_file(f"http://127.0.0.1:{srv.server_port}/f", dest)
                self.assertEqual(dest.read_bytes(), RangeHandler.PAYLOAD)
        finally:
            srv.shutdown()


class PolishTests(unittest.TestCase):
    class Fake:
        def __init__(self, reply=None, exc=None):
            self.reply, self.exc = reply, exc

        def chat(self, system, user, max_tokens):
            if self.exc:
                raise self.exc
            return self.reply

    def setUp(self):
        core.settings.update(core.DEFAULT_SETTINGS)
        self.raw = "今天體育課要練習高遠球請同學準備好球拍如果下雨就改室內"

    def quiet(self, *_):
        pass

    def test_punct_ok(self):
        s = self.Fake("今天體育課要練習高遠球，請同學準備好球拍。如果下雨，就改室內。")
        self.assertIn("，", core.polish(self.raw, "punct", s, self.quiet))

    def test_short_text_skips_llm(self):
        self.assertEqual(core.polish("你好", "punct", self.Fake("不該被用到"), self.quiet), "你好")

    def test_no_server_returns_raw(self):
        self.assertEqual(core.polish(self.raw, "digest", None, self.quiet), self.raw)

    def test_error_falls_back(self):
        s = self.Fake(exc=RuntimeError("boom"))
        self.assertEqual(core.polish(self.raw, "digest", s, self.quiet), self.raw)

    def test_punct_truncated_output_falls_back(self):
        self.assertEqual(core.polish(self.raw, "punct", self.Fake("好的"), self.quiet), self.raw)

    def test_digest_runaway_output_falls_back(self):
        self.assertEqual(core.polish(self.raw, "digest", self.Fake("字" * 2000), self.quiet), self.raw)

    def test_markdown_stripped(self):
        out = core.polish(self.raw, "digest", self.Fake("**通知**\n- 準備球拍"), self.quiet)
        self.assertNotIn("*", out)
        self.assertIn("・準備球拍", out)

    def test_ai_punct_off(self):
        core.settings["ai_punct"] = False
        self.assertEqual(core.polish(self.raw, "punct", self.Fake("不該被用到"), self.quiet), self.raw)

    def test_http_chat_roundtrip(self):
        srv = serve(ChatHandler)
        try:
            ls = core.LlamaServer(Path("x"), Path("y"))
            ls.port = srv.server_port
            self.assertEqual(ls.chat("sys", "user", 50), ChatHandler.REPLY)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
