"""Smooth scrolling: a wheel notch glides over several frames (instead of jumping), quick notches add up, a
scrollbar drag stops a glide; text boxes glide too, up and down. Made-up data only."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


class Ev:
    def __init__(self, w, delta):
        self.widget, self.delta, self.num = w, delta, None


def script(app, root, V, h):
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    root.geometry("1200x800+10+10")
    cid = V.save_client({"code": "SC1"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-01", "session_number": "1", "kind": "progress",
                             "created": "a"}, "note": "Note."})
    c = V.load_client(cid)
    c["assessments"] = [{"id": f"a{i}", "name": f"Checklist {i}", "date": f"2026-10-{i + 1:02d}", "score": "Mild",
                         "notes": "Given at school. " * 6, "added": str(i)} for i in range(28)]
    V.save_client(c)
    app.refresh_clients(); app.open_client(cid, select="assess"); h.pump(root, 1.5)
    canvas = next(w for w in h.widgets(app.page, "Canvas") if hasattr(w, "_scroll_wheel")
                  and w.yview() != (0.0, 1.0))
    h.pump(root, 2.5)                                  # (the page finishes laying itself out first)
    canvas.yview_moveto(0.0); h.pump(root, 0.3)
    H_ = float(str(canvas.cget("scrollregion")).split()[3])
    top = lambda: canvas.yview()[0] * H_
    t0 = top()
    canvas._scroll_wheel(Ev(canvas, -120))
    first = top() - t0
    samples = []
    end = time.time() + 0.6
    while time.time() < end:
        root.update(); samples.append(top()); time.sleep(0.005)
    moved = top() - t0
    notch = h.ss.__dict__.get("_px_probe")
    h.ok("one notch glides: a small first step, then several frames to arrive",
         0 < first < moved * 0.5 and len(set(round(s) for s in samples)) >= 5,
         (first, moved, sorted(set(round(s) for s in samples)), len(samples)))
    h.ok("…it eases in the same direction the whole way and lands about one notch down (100 px, scaled)",
         all(b >= a - 0.01 for a, b in zip(samples, samples[1:])) and 90 <= moved <= 320, moved)
    t1 = top()
    for _ in range(4):
        canvas._scroll_wheel(Ev(canvas, -120))
    h.pump(root, 0.8)
    h.ok("quick notches add up (4 go about 4× as far)", 3.3 * moved <= top() - t1 <= 4.6 * moved, (top() - t1, moved))
    canvas._scroll_wheel(Ev(canvas, 120 * 3)); h.pump(root, 0.02)
    sb = next(w for w in h.widgets(canvas.master, "TScrollbar"))
    sb.cget("command")
    root.tk.call(sb.cget("command"), "moveto", "0.0"); h.pump(root, 0.5)
    h.ok("dragging the scrollbar stops a glide (it stays where the scrollbar put it)", top() < 1.0, top())
    canvas._scroll_wheel(Ev(canvas, 120 * 50)); h.pump(root, 0.8)
    h.ok("it never goes past the top", canvas.yview()[0] == 0.0)

    # a long text box
    import tkinter as tk
    app.contact_log_dialog(V.load_client(cid)); h.pump(root, 0.4)
    t = app._contact_win["text"]
    t.insert("1.0", "\n".join(f"Line {i} of a long summary of the contact." for i in range(200)))
    h.pump(root, 0.3)
    t.yview_moveto(0.0); h.pump(root, 0.1)
    y0 = t.yview()[0]
    t.event_generate("<MouseWheel>", delta=-120)
    a1 = t.yview()[0]
    h.pump(root, 0.6)
    a2 = t.yview()[0]
    h.ok("a text box glides down too (part of the way first, then the rest)", y0 < a1 < a2, (y0, a1, a2))
    t.event_generate("<MouseWheel>", delta=120); h.pump(root, 0.6)
    h.ok("…and back up", t.yview()[0] < a2 * 0.5 + 1e-9, (t.yview()[0], a2))
    app._contact_win["win"].destroy()
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
