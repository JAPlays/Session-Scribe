"""Shared test harness: the real app under Xvfb with a mock LM Studio that records every prompt and
answers by matching words in the prompt. Usage:
    from harness import H
    H.start(script, responses={"skills feedback": "...", ...})  # script(app, root, V, h) runs inside the app
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

APPFILE = os.environ.get("APPFILE", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session_scribe.py"))
SCR = os.path.dirname(os.path.abspath(__file__))


class H:
    PROMPTS = []
    MODELS = []        # the model each AI request asked for
    FAIL_MODELS = set()  # models the mock AI refuses (as if they couldn't be loaded)
    RESP = []          # [(substring, reply)] — first match wins
    DEFAULT = "OK."
    ss = None
    results = []
    dialogs = []

    @classmethod
    def ok(cls, name, cond, extra=""):
        cls.results.append(bool(cond))
        print(("  PASS " if cond else "  FAIL ") + name + (f"   [{str(extra)[:400]}]" if extra and not cond else ""), flush=True)

    @classmethod
    def start(cls, script, responses=(), data_dir=None, home=None):
        cls.RESP = list(responses)
        FAKE = tempfile.mkdtemp(); os.makedirs(os.path.join(FAKE, "whisperx"))
        open(os.path.join(FAKE, "torch.py"), "w").write("__version__='f'\n")
        open(os.path.join(FAKE, "whisperx", "__init__.py"), "w").write("")
        sys.path.insert(0, FAKE)
        os.environ["HOME"] = home or tempfile.mkdtemp()
        data = data_dir or tempfile.mkdtemp()

        class LM(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _json(self, o):
                b = json.dumps(o).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def do_GET(self):
                self._json({"data": [{"id": "mock-model"}]})

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.MODELS.append(req.get("model"))
                if req.get("model") in cls.FAIL_MODELS:          # a model that can't be loaded
                    b = json.dumps({"error": f"model {req.get('model')} could not be loaded"}).encode()
                    self.send_response(400); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
                    return
                prompt = "\n".join(m["content"] for m in req["messages"])
                cls.PROMPTS.append(prompt)
                user = req["messages"][-1]["content"]
                text = next((r(user) if callable(r) else r for k, r in cls.RESP if k in user), cls.DEFAULT)
                self._json({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LM)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        import importlib.util
        spec = importlib.util.spec_from_file_location("ss", APPFILE)
        ss = importlib.util.module_from_spec(spec); sys.modules["ss"] = ss; spec.loader.exec_module(ss)
        cls.ss = ss
        ss.save_config(dict(ss.DEFAULT_CONFIG, data_dir=data, vault_dir=os.path.join(data, "vault"),
                            lmstudio_url=f"http://127.0.0.1:{srv.server_port}/v1", model="mock-model",
                            ai_names=False))   # most tests check the hidden-names path; test_names.py the other
        import tkinter as tk
        from tkinter import messagebox
        for n in ("showinfo", "showerror", "showwarning"):
            setattr(messagebox, n, (lambda n: lambda *a, **k: (cls.dialogs.append((n, str(a[1] if len(a) > 1 else ""))),
                                                               print(f"  DIALOG {n}: {str(a[1] if len(a) > 1 else '')[:200]}")))(n))
        messagebox.askyesno = lambda *a, **k: True
        messagebox.askokcancel = lambda *a, **k: True

        def run(root):
            def entries(w, out):
                for c in w.winfo_children():
                    if c.winfo_class() == "TEntry":
                        out.append(c)
                    entries(c, out)
                return out
            root.update()
            es = entries(root, [])
            for e in es[:2]:
                e.insert(0, "testpassword1")
            es[1].event_generate("<Return>")
            for _ in range(40):
                root.update(); time.sleep(0.05)
            if not hasattr(root, "_app"):      # the key press didn't reach the window (e.g. it isn't in front)
                def buttons(w, out):
                    for c in w.winfo_children():
                        if c.winfo_class() == "TButton":
                            out.append(c)
                        buttons(c, out)
                    return out
                for b in buttons(root, []):
                    if str(b.cget("text")) in ("Create vault", "Unlock"):
                        b.invoke()
                        break
                for _ in range(40):
                    root.update(); time.sleep(0.05)
            app = root._app
            app.lm_status = ["mock-model"]
            import gc
            V = next(o for o in gc.get_objects() if isinstance(o, ss.Vault) and getattr(o, "fernet", None) is not None)
            try:
                script(app, root, V, cls)
            except Exception:
                import traceback
                traceback.print_exc()
                cls.results.append(False)
            try:
                root.destroy()
            except Exception:
                pass
        tk.Tk.mainloop = lambda self, *a: run(self)
        ss.run_gui()
        bad = [d for d in cls.dialogs if d[0] == "showerror"]
        ok = all(cls.results) and cls.results and not bad
        print(f"\n{sum(cls.results)}/{len(cls.results)} checks passed" + (f"; error dialogs: {bad}" if bad else ""))
        sys.exit(0 if ok else 1)

    # ---- helpers usable inside a script
    @staticmethod
    def pump(root, sec, until=None):
        t = time.time()
        while time.time() - t < sec:
            root.update(); time.sleep(0.03)
            if until and until():
                return True
        return bool(until and until())

    @staticmethod
    def shot(name):
        try:                                   # screenshots need ImageMagick (Linux/Xvfb); skipped elsewhere
            os.makedirs(os.path.join(SCR, "screenshots"), exist_ok=True)
            subprocess.run(["import", "-window", "root", os.path.join(SCR, "screenshots", name)], check=False,
                           capture_output=True)
        except OSError:
            pass

    @staticmethod
    def widgets(root, cls_name=None):
        out = []

        def walk(w):
            for c in w.winfo_children():
                if cls_name is None or c.winfo_class() == cls_name:
                    out.append(c)
                walk(c)
        walk(root)
        return out

    @classmethod
    def button(cls, root, text):
        return next((w for w in cls.widgets(root, "TButton") if text in str(w.cget("text"))), None)

    @classmethod
    def texts(cls, root):
        out = []
        for w in cls.widgets(root):
            try:
                out.append(str(w.cget("text")))
            except Exception:
                pass
            if w.winfo_class() == "Text":
                out.append(w.get("1.0", "end"))
        return "\n".join(out)

    @classmethod
    def action(cls, root, text):
        """A button with this text, or else an entry with this text in one of the page's menus (Write ▾,
        Patient ▾, ⋯ …, including submenus) — something with .invoke(). None if there's neither."""
        b = next((w for w in cls.widgets(root, "TButton") if text in str(w.cget("text")) and w.winfo_ismapped()), None) \
            or cls.button(root, text)
        if b is not None:
            return b

        class Item:
            def __init__(self, m, i): self.m, self.i = m, i
            def invoke(self): return self.m.invoke(self.i)

        def search(m):
            for i in range((m.index("end") if m.index("end") is not None else -1) + 1):
                kind = m.type(i)
                if kind == "command" and text in m.entrycget(i, "label"):
                    return Item(m, i)
                if kind == "cascade":
                    found = search(m.nametowidget(m.entrycget(i, "menu")))
                    if found:
                        return found
            return None
        for mb in cls.widgets(root, "TMenubutton"):
            if not mb.winfo_ismapped() or not mb.cget("menu"):
                continue
            found = search(mb.nametowidget(mb.cget("menu")))
            if found:
                return found
        return None

    @staticmethod
    def select_tab(nb, label):
        for t in nb.tabs():
            if label in nb.tab(t, "text"):
                nb.select(t)
                return True
        return False

    @classmethod
    def notebooks(cls, root):
        return cls.widgets(root, "TNotebook")


def sample_lines(minutes=24):
    """A believable two-person session with timings (Clinician = SPEAKER_00)."""
    script = [
        ("SPEAKER_00", "How have things been since we last met?"),
        ("SPEAKER_01", "Honestly it was a rough week. I felt anxious most days and I couldn't sleep."),
        ("SPEAKER_00", "Do you know what was going on when the anxiety was worst?"),
        ("SPEAKER_01", "Work mostly. My manager yelled at me in front of everyone and I felt so ashamed and alone."),
        ("SPEAKER_00", "That sounds really painful, being embarrassed in front of everyone."),
        ("SPEAKER_01", "Yeah. I don't know. Anyway, the weekend was fine I guess."),
        ("SPEAKER_00", "What did you do this weekend?"),
        ("SPEAKER_01", "I went hiking with my sister and it felt good, I was calm for once and we laughed a lot."),
        ("SPEAKER_00", "It sounds like being outside with her really helped you feel calmer."),
        ("SPEAKER_01", "It did. I want to do that more. I felt hopeful."),
        ("SPEAKER_00", "How could you build more of that into your week?"),
        ("SPEAKER_01", "Maybe a walk after work. I'm worried I'll be too tired and stressed though."),
    ]
    out, t = [], 0.0
    reps = max(1, int(minutes * 60 / (len(script) * 12)))
    for r in range(reps):
        for spk, text in script:
            d = 4.0 + len(text) / 18
            out.append({"speaker": spk, "start": round(t, 2), "end": round(t + d, 2), "text": text})
            t += d + 1.0
    return out
