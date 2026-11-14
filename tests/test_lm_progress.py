"""The progress bar follows LM Studio itself: its prompt-reading percentage (/api/v1/chat events), model
loading, thinking and the answer — never stopping short or going backwards; an older LM Studio without
/api/v1/chat falls back to the OpenAI-style API. Uses a stand-in LM Studio (no real model)."""
import sys, os, json, time, threading, http.server
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

MODE = {"native": True, "loaded": True}


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, o, code=200):
        b = json.dumps(o).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/v1/models"):
            return self._json({"models": [{"key": "m1", "loaded_instances": [{}] if MODE["loaded"] else []}]})
        self._json({"data": [{"id": "m1"}]})

    def _sse(self, lines):
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for ln, pause in lines:
            self.wfile.write(ln.encode()); self.wfile.flush(); time.sleep(pause)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path == "/api/v1/models/load":
            time.sleep(2.5)
            MODE["loaded"] = True
            return self._json({"ok": True})
        if self.path == "/api/v1/chat":
            if not MODE["native"]:
                return self._json({"error": "not found"}, 404)
            ev = lambda o: (f"event: {o['type']}\ndata: {json.dumps(o)}\n\n", 0.25)
            seq = [ev({"type": "chat.start"}), ev({"type": "prompt_processing.start"})]
            seq += [ev({"type": "prompt_processing.progress", "progress": p}) for p in (0.25, 0.5, 0.75, 1.0)]
            seq += [ev({"type": "prompt_processing.end"}), ev({"type": "reasoning.start"})]
            seq += [ev({"type": "reasoning.delta", "content": "thinking " * 20}) for _ in range(3)]
            seq += [ev({"type": "reasoning.end"}), ev({"type": "message.start"})]
            seq += [ev({"type": "message.delta", "content": "Sleep improved on weekends. " * 4}) for _ in range(4)]
            seq += [ev({"type": "message.end"}), ev({"type": "chat.end", "result": {"output": []}})]
            return self._sse(seq)
        # the OpenAI-style API
        chunk = lambda c: (f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n", 0.1)
        self._sse([chunk("Answer from the "), chunk("older API."), ("data: [DONE]\n\n", 0)])


def script(app, root, V, h):
    ss = h.ss
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = dict(ss.DEFAULT_CONFIG, lmstudio_url=f"http://127.0.0.1:{srv.server_port}/v1", model="m1", ai_engine="lmstudio")
    seen = []

    def prog(f):
        seen.append((time.time(), f))
    t0 = time.time()
    out = ss.llm_chat(cfg, "x" * 20000, prog, expected=400)
    fr = [f for _, f in seen]
    h.ok("the answer comes through LM Studio's own API", out.startswith("Sleep improved on weekends."), out[:60])
    h.ok("the bar only ever moves forward, and ends at 100%", fr == sorted(fr) and fr[-1] == 1.0, fr[-5:])
    mid = [f for t, f in seen if 0.3 < t - t0 < 1.2]
    h.ok("while LM Studio reads, the bar follows its percentage (not a guess)", mid and 0.05 < max(mid) < 0.95
         and len(set(round(f, 3) for f in fr)) >= 8, mid)
    h.ok("…and the thinking and answer keep it moving", len([f for f in fr if f > max(mid or [0])]) >= 5)

    # a note: the step's label carries LM Studio's own number (the bar is the whole task)
    labels = []
    st = ss._Steps(lambda text, pct: labels.append((text, pct)), [("Writing the progress note", 400, 20000),
                                                                 ("Writing the session overview", 400, 20000)])
    st.run(0, cfg, "x" * 20000)
    texts = [t for t, _ in labels]
    h.ok("the step's label shows LM Studio's reading percentage, then that it's writing",
         any("Writing the progress note (step 1 of 2)… reading 50%" == t for t in texts)
         and any(t.endswith("… writing") for t in texts), texts[:12])
    h.ok("…while the bar covers the whole task (this first step ends partway)",
         max(p for _, p in labels) < 80, max(p for _, p in labels))

    # the model has to be loaded first: the bar shows it, not 0%
    MODE["loaded"] = False
    ss._LM_LOADED.update(key=None, at=0)
    seen.clear(); t0 = time.time()
    ss.llm_chat(cfg, "x" * 20000, prog, expected=400)
    during = [f for t, f in seen if 1.2 < t - t0 < 2.4]
    h.ok("while the model loads, the bar moves (instead of sitting at 0%)",
         len(during) >= 2 and max(during) > min(during) > 0, during)
    h.ok("…and how long it took is learned", ss.timing_llm("m1").get("load", 0) > 2)

    # an older LM Studio without /api/v1/chat
    MODE["native"] = False
    ss._LM_NATIVE.clear()
    out = ss.llm_chat(cfg, "hello", None, expected=50)
    h.ok("an older LM Studio falls back to the OpenAI-style API", out == "Answer from the older API.", out)
    h.ok("…and it's remembered", list(ss._LM_NATIVE.values()) == [False])
    srv.shutdown()


H.start(script)
