"""Installer builds: the built-in AI (bundled llama-server) instead of LM Studio.

Runs the real app from a temporary "installed" folder that has engine/llama-server (a fake one, below),
models/llm/*.gguf (tiny files with real GGUF headers), bundled speaker-model files and bundle.json.
Checks the hardware fitting, the model list, starting/switching the server, writing with it, the
speaker labels without a token, and the Settings controls. No real model or GPU is needed."""
import json
import os
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = tempfile.mkdtemp(prefix="ss-installed-")
shutil.copy(os.path.join(os.path.dirname(HERE), "session_scribe.py"), APP)
os.environ["APPFILE"] = os.path.join(APP, "session_scribe.py")
LOG = os.path.join(APP, "fake-llama.log")
os.environ["FAKE_LLAMA_LOG"] = LOG

# ---- a fake llama-server: lists one integrated GPU, serves /health and /v1/chat/completions
os.makedirs(os.path.join(APP, "engine"))
FAKE = os.path.join(APP, "engine", "llama-server")
with open(FAKE, "w") as f:
    f.write(f"#!{sys.executable}\n" + r'''
import http.server, json, os, sys
args = sys.argv[1:]
if "--list-devices" in args:
    print("Available devices:")
    print("  Vulkan0: Intel(R) Iris(R) Xe Graphics (16384 MiB, 15360 MiB free)")
    sys.exit(0)
with open(os.environ["FAKE_LLAMA_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
port = int(args[args.index("--port") + 1]); alias = args[args.index("--alias") + 1]
class S(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _j(self, o, code=200):
        b = json.dumps(o).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._j({"status": "ok"} if self.path == "/health" else {"data": [{"id": alias}]})
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._j({"choices": [{"message": {"content": "BUILTIN:" + req["model"]}, "finish_reason": "stop"}]})
http.server.HTTPServer(("127.0.0.1", port), S).serve_forever()
''')
os.chmod(FAKE, 0o755)


def gguf(path, layers=40, heads=32, kv=8, emb=5120, ctx=131072, pad=0):
    """A GGUF header with Mistral-Small-like shapes (and a vocabulary array to skip over)."""
    def s(x):
        b = x.encode(); return struct.pack("<Q", len(b)) + b
    kvs = [("general.architecture", 8, s("llama")), ("tokenizer.ggml.tokens", 9,
           struct.pack("<IQ", 8, 200) + b"".join(s(f"tok{i}") for i in range(200)))]
    for k, v in (("block_count", layers), ("attention.head_count", heads), ("attention.head_count_kv", kv),
                 ("embedding_length", emb), ("context_length", ctx), ("attention.key_length", 128),
                 ("attention.value_length", 128)):
        kvs.append((f"llama.{k}", 4, struct.pack("<I", v)))
    body = b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs))
    for k, t, v in kvs:
        body += s(k) + struct.pack("<I", t) + v
    with open(path, "wb") as f:
        f.write(body + b"\0" * pad)


LLM = os.path.join(APP, "models", "llm")
os.makedirs(LLM)
gguf(os.path.join(LLM, "Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M.gguf"))
gguf(os.path.join(LLM, "Small-8B-Q4_K_M-00001-of-00002.gguf"), layers=32, emb=4096)
open(os.path.join(LLM, "Small-8B-Q4_K_M-00002-of-00002.gguf"), "wb").write(b"\0" * 1000)
gguf(os.path.join(LLM, "Half-Download-00001-of-00003.gguf"))          # parts missing: not listed
open(os.path.join(LLM, "mmproj-vision.gguf"), "wb").write(b"GGUF")     # vision add-on: not a model
for repo, fn in (("speaker-diarization-3.1", "config.yaml"), ("segmentation-3.0", "pytorch_model.bin")):
    d = os.path.join(APP, "models", "hf", "hub", f"models--pyannote--{repo}", "snapshots", "abc")
    os.makedirs(d)
    open(os.path.join(d, fn), "w").write("x")
json.dump({"edition": "Mistral Small 24B", "default_model": "Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M",
           "whisper_model": "large-v3-turbo"},
          open(os.path.join(APP, "bundle.json"), "w", encoding="utf-8-sig"))   # as PowerShell 5 writes it

sys.path.insert(0, HERE)
from harness import H  # noqa: E402


def script(app, root, V, h):
    ss = h.ss
    GB = ss.GB
    h.ok("bundle detected and its model folders used", ss.BUNDLE.get("edition") == "Mistral Small 24B"
         and os.environ["HF_HOME"] == os.path.join(APP, "models", "hf"))
    h.ok("the built-in AI is the engine", ss.ai_engine(ss.load_config()) == "builtin")

    names = [m["name"] for m in ss.model_files()]
    h.ok("model list: whole and split models once each; partial downloads and vision add-ons left out",
         sorted(names) == ["Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M", "Small-8B-Q4_K_M"], names)

    info = ss.gguf_info(os.path.join(LLM, "Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M.gguf"))
    h.ok("model header read (layers, context, cache size per token)",
         info.get("layers") == 40 and info.get("ctx_train") == 131072
         and info.get("kv_bytes_per_token") == 40 * 8 * 256 * 2, info)

    big = dict(ss.find_model("Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M"), bytes=int(14.3 * GB))
    igpu = [{"name": "Intel(R) Iris(R) Xe Graphics", "total": 16 * GB, "free": 15 * GB, "integrated": True}]
    p32 = ss.plan_model(big, {"ram_total": 32 * GB, "gpus": igpu})
    h.ok("24B on 32 GB with integrated graphics: runs, marked slow, full 16K context, partly on the iGPU",
         p32["fit"] == "slow" and p32["ctx"] == 16384 and 0 < p32["gpu_layers"] < 40, p32)
    p16 = ss.plan_model(big, {"ram_total": 16 * GB, "gpus": igpu})
    h.ok("24B on 16 GB: won't fit", p16["fit"] == "no", p16)
    card = [{"name": "NVIDIA GeForce RTX 4090", "total": 24 * GB, "free": 23 * GB, "integrated": False}]
    p_card = ss.plan_model(big, {"ram_total": 64 * GB, "gpus": card})
    h.ok("24B with a 24 GB graphics card: fits well, every layer on the card",
         p_card["fit"] == "good" and p_card["gpu_layers"] == 40, p_card)
    small = dict(ss.find_model("Small-8B-Q4_K_M"), bytes=int(4.9 * GB))
    h.ok("8B on 16 GB with integrated graphics: fits well",
         ss.plan_model(small, {"ram_total": 16 * GB, "gpus": igpu})["fit"] == "good")

    hw = ss.detect_hardware(refresh=True)
    h.ok("hardware check asks llama-server which graphics it sees",
         hw["gpus"] and hw["gpus"][0]["integrated"] and hw["gpus"][0]["total"] == 16384 * 1024 ** 2, hw.get("gpus"))
    h.ok("speech model fitted to this computer", ss.recommend_whisper(
        {"ram_total": 32 * GB, "cpu_physical": 8}, ["large-v3-turbo"])[0] == "large-v3-turbo"
        and ss.recommend_whisper({"ram_total": 8 * GB, "cpu_physical": 4}, ["small.en", "large-v3-turbo"])[0] == "small.en")

    cfg = ss.load_config()
    msg = ss.auto_tune(cfg, refresh=True)
    h.ok("auto-tune keeps the installer's model when it can run and says what it chose",
         cfg["model"] == "Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M" and "This computer:" in (msg or ""), msg)
    h.ok("speaker labels work without a Hugging Face token (bundled model)", ss.speakers_on(""))

    out = ss.llm_chat(cfg, "Say hi.")
    h.ok("writing goes to the built-in server, which was started on 127.0.0.1",
         out == "BUILTIN:Mistral-Small-3.1-24B-Instruct-2503-Q4_K_M" and ss.ENGINE.running(), out)
    first = [json.loads(l) for l in open(LOG)][-1]
    h.ok("…with this computer's context and graphics settings, one request at a time",
         first[first.index("-c") + 1] == str(cfg["lm_context"]) and first[first.index("-ngl") + 1] == str(cfg["gpu_layers"])
         and first[first.index("--host") + 1] == "127.0.0.1" and first[first.index("--parallel") + 1] == "1", first)
    ss.llm_chat(cfg, "Again.")
    h.ok("the server is reused between steps", len(open(LOG).readlines()) == 1)
    cfg["model"] = "Small-8B-Q4_K_M"
    out = ss.llm_chat(cfg, "Switch.")
    last = [json.loads(l) for l in open(LOG)][-1]
    h.ok("choosing another model switches the server to it",
         out == "BUILTIN:Small-8B-Q4_K_M" and last[last.index("-m") + 1].endswith("00001-of-00002.gguf"), out)
    h.ok("the status check lists the models", ss.list_models(cfg) == names or sorted(ss.list_models(cfg)) == sorted(names))

    app.open_setup(); h.pump(root, 2)
    txt = h.texts(root)
    h.ok("Settings shows the model picker and this computer's summary",
         "AI model (built in)" in txt and "This computer" in txt and "Iris" in txt, txt[-600:])
    h.ok("…with how well the chosen model fits", any(lbl in txt for lbl in ("Fits well:", "Slow here:", "Won't fit:")))
    h.ok("…and no LM Studio address", "LM Studio address" not in txt)
    h.shot("builtin_ai_settings.png")
    ss.ENGINE.stop()
    h.ok("stopping the app's AI stops the server", not ss.ENGINE.running())


H.start(script)
