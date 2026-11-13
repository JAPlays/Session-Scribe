"""Home dashboard: Hide, ✕, Unhide and ticking a follow-up keep the scroll position (no jump to the top)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H
from datetime import date, timedelta


def script(app, root, V, h):
    today = date.today()
    D = lambda n: (today - timedelta(days=n)).isoformat()
    for i in range(14):                                  # a caseload tall enough to scroll
        V.save_client({"code": f"PT{i:02d}",
                       "followups": [{"id": f"f{i}a", "text": f"Call school {i}", "date": D(3), "done": False},
                                     {"id": f"f{i}b", "text": f"Send form {i}", "date": D(4), "done": False}],
                       "homework": [{"id": f"h{i}", "title": f"Thought log {i}", "date": D(14), "status": "assigned"}]})
    root.geometry("1200x800")
    app.refresh_clients(); app.show_home(); h.pump(root, 1.5)

    def canvas():
        """The page's outermost scrolling canvas (made by scrollable)."""
        queue = [app.page]
        while queue:
            w = queue.pop(0)
            if w.winfo_class() == "Canvas" and getattr(w, "_scroll_wheel", None):
                return w
            queue.extend(w.winfo_children())
        return None

    def top_px():
        cv = canvas()
        sr = str(cv.cget("scrollregion")).split()
        return cv.yview()[0] * float(sr[3])

    def scroll_to(frac):
        canvas().yview_moveto(frac); h.pump(root, 0.3)

    def page_y(w):
        """Where a widget sits on the scrolled page (layout position, not screen position)."""
        cv, y = canvas(), 0
        while w is not None and w.master is not cv:
            y += w.winfo_y()
            w = w.master
        return y

    def on_screen(w):
        lo = top_px()
        return w.winfo_manager() and lo + 40 < page_y(w) < lo + canvas().winfo_height() - 40

    def visible(text):
        """A shown button with this text whose row is inside the visible part of the page."""
        return next((w for w in h.widgets(app.page, "TButton") if str(w.cget("text")) == text and on_screen(w)), None)

    h.ok("the dashboard is taller than the window", canvas() is not None and canvas().yview()[1] < 0.6,
         canvas() and canvas().yview())

    for label, text in (("Hide", "Hide"), ("✕", "✕")):
        scroll_to(0.5)
        before = top_px()
        b = visible(text)
        h.ok(f"a {label} button is on screen half way down", b is not None and before > 100, before)
        if b is None:
            continue
        b.invoke(); h.pump(root, 1.5)
        after = top_px()
        h.ok(f"{label} keeps the place on the page (was {before:.0f}px, now {after:.0f}px)",
             after > 100 and abs(after - before) < 120, (before, after))

    scroll_to(0.5)
    before = top_px()
    cb = next((w for w in h.widgets(app.page, "TCheckbutton") if on_screen(w)), None)
    h.ok("a follow-up checkbox is on screen half way down", cb is not None)
    if cb is not None:
        cb.invoke(); h.pump(root, 1.5)
        after = top_px()
        h.ok(f"ticking a follow-up keeps the place ({before:.0f}px → {after:.0f}px)",
             after > 100 and abs(after - before) < 120, (before, after))

    unhide = next((w for w in h.widgets(app.page, "TButton") if str(w.cget("text")).startswith("Unhide")), None)
    scroll_to(0.0)
    if unhide is not None:
        unhide.invoke(); h.pump(root, 1.5)
        h.ok("Unhide at the top stays at the top", top_px() < 5, top_px())


H.start(script)
