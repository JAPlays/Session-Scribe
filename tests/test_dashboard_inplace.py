"""Home: Hide, ✕, Hide list, Hide client items and ticking a follow-up update just those items — the page isn't
rebuilt — with the counts, the hidden total and the Unhide button kept current."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    ids = []
    for i in range(3):
        ids.append(V.save_client({"code": f"IP{i}", "followups": [
            {"id": f"f{i}a", "text": f"Call school {i}", "date": "2026-11-01", "done": False},
            {"id": f"f{i}b", "text": f"Send form {i}", "date": "2026-11-02", "done": False}],
            "homework": [{"id": f"h{i}", "title": f"Thought log {i}", "date": "2026-10-01", "status": "assigned"}]}))
    app.refresh_clients(); app.client_id = None; app.show_home(); h.pump(root, 1.5)
    page = app.page
    reg = page._dash_reg
    rows = lambda k: [d for d, x in reg["rows"].items() if x["key"] == k]
    keep = reg["rows"][rows("homework")[0]]["w"]          # a row nothing below touches

    def same_page(label):
        h.ok(label + " — the page wasn't rebuilt", app.page is page and keep.winfo_exists())

    def text():
        return h.texts(page)
    h.ok("6 follow-ups and 3 homework items to start with", len(rows("followups")) == 6 and len(rows("homework")) == 3)

    # Hide one follow-up
    d1 = rows("followups")[0]
    w1 = reg["rows"][d1]["w"]
    next(w for w in h.widgets(w1, "TButton") if w.cget("text") == "Hide").invoke(); h.pump(root, 0.5)
    same_page("Hide")
    h.ok("…its row is gone, the rest stay", not w1.winfo_exists() and len(rows("followups")) == 5)
    h.ok("…the counts follow: list header, tile, patient card, '1 hidden', Unhide (1)",
         "Follow-ups to do (5 items across 3 patients)" in text() and "· 1 hidden" in text()
         and "Unhide dismissed (1)" in text() and reg["unhide"].winfo_manager() and
         reg["tiles"]["followups"][1].cget("text") == "5", text()[:600])
    h.ok("…and it's saved as hidden", any(d1 in (V.load_client(i).get("dismissed_dashboard") or []) for i in ids))

    # ✕ the other follow-up of the same patient → that patient's card leaves the list
    cid1 = [i for i in ids if d1 in (V.load_client(i).get("dismissed_dashboard") or [])][0]
    d2 = next(d for d in rows("followups") if reg["rows"][d]["cid"] == cid1)
    card = reg["groups"][("followups", cid1)]["card"]
    next(w for w in h.widgets(reg["rows"][d2]["w"], "TButton") if w.cget("text") == "✕").invoke(); h.pump(root, 0.5)
    same_page("✕")
    h.ok("…an emptied patient card is removed", not card.winfo_exists() and ("followups", cid1) not in reg["groups"])
    h.ok("…and the header says 4 items across 2 patients", "Follow-ups to do (4 items across 2 patients)" in text())

    # tick a follow-up
    d3 = rows("followups")[0]
    cb = h.widgets(reg["rows"][d3]["w"], "TCheckbutton")[0]
    cb.invoke(); h.pump(root, 0.5)
    same_page("Ticking a follow-up")
    h.ok("…its row is gone and the follow-up is marked done", d3 not in reg["rows"] and any(
        x.get("done") for i in ids for x in V.load_client(i).get("followups") or []))

    # Hide client items (one patient's homework), then Hide list (the rest)
    cid2 = reg["rows"][rows("homework")[1]]["cid"]
    hdr_btn = next(w for w in h.widgets(reg["groups"][("homework", cid2)]["card"], "TButton")
                   if w.cget("text") == "Hide client items")
    hdr_btn.invoke(); h.pump(root, 0.5)
    same_page("Hide client items")
    h.ok("…that patient's homework is gone", ("homework", cid2) not in reg["groups"] and len(rows("homework")) == 2)
    sec = reg["secs"]["followups"]["hdr"]
    next(w for w in h.widgets(sec, "TButton") if w.cget("text") == "Hide list").invoke(); h.pump(root, 0.5)
    h.ok("Hide list removes the whole list, its tile and header", "followups" not in reg["secs"]
         and "followups" not in reg["tiles"] and not sec.winfo_exists() and app.page is page)
    hidden_now = reg["hidden"]
    h.ok("…and the hidden total counts only what's hidden (not the ✕'d or ticked ones)",
         hidden_now == sum(len(V.load_client(i).get("dismissed_dashboard") or []) for i in ids), hidden_now)

    # leaving Home and coming back reuses the updated page
    app.open_client(ids[0]); h.pump(root, 0.8)
    app.show_home(); h.pump(root, 0.8)
    h.ok("going to a patient and back shows the same (updated) page", app.page is page and len(rows("homework")) == 2)

    # Unhide brings them back (a redraw is right here: items come back)
    reg["unhide"].invoke(); h.pump(root, 1.5)
    h.ok("Unhide dismissed brings hidden items back", app.page._dash_reg["hidden"] == 0 and
         len([1 for x in app.page._dash_reg["rows"].values() if x["key"] == "homework"]) == 3)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
