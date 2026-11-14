"""New session without a recording: the box is where you write what happened, and the note is written from it;
with a recording it stays the optional "won't capture" box. Also: the Queue button's name and the Contact log's
oldest/newest order. Made-up data only."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

ACCOUNT = "Patient discussed conflict with a coworker; practiced box breathing; agreed to journal daily."


def script(app, root, V, h):
    import tkinter.filedialog as fd
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    h.ok("the header button just says Queue", str(app.queue_btn.cget("text")).strip() == "🎙  Queue",
         app.queue_btn.cget("text"))

    cid = V.save_client({"code": "WR1", "names_to_hide": "Alex Rivera, Patient", "names_format": 2})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-01", "session_number": "1", "kind": "progress",
                             "created": "a"}, "note": "First session."})
    app.refresh_clients(); app.open_client(cid); h.pump(root, 0.8)
    h.button(app.page, "New session").invoke(); h.pump(root, 0.6)
    page_text = h.texts(app.page)
    h.ok("no recording yet: the box asks what happened in the session",
         "What happened in the session?" in page_text and "Anything the recording won't capture" not in page_text)

    # choosing a recording puts the usual heading back
    rec = os.path.join(tempfile.mkdtemp(), "s.wav")
    open(rec, "wb").write(b"RIFF")
    fd.askopenfilename = lambda **k: rec
    h.button(app.page, "Choose file").invoke(); h.pump(root, 0.3)
    page_text = h.texts(app.page)
    h.ok("with a recording: 'Anything the recording won't capture? (optional)' as before",
         "Anything the recording won't capture? (optional)" in page_text and "What happened in the session?"
         not in page_text)
    h.button(app.page, "paste it instead").invoke(); h.pump(root, 0.2)
    h.button(app.page, "Use a recording instead").invoke(); h.pump(root, 0.2)
    app.discard_wizard(); h.pump(root, 0.5)

    # write it, and the note is written from it
    app.open_client(cid); h.pump(root, 0.5)
    h.button(app.page, "New session").invoke(); h.pump(root, 0.6)
    box = max((t for t in h.widgets(app.page, "Text") if t.winfo_ismapped()), key=lambda t: int(t.cget("height")))
    h.button(app.page, "Next").invoke(); h.pump(root, 0.3)
    h.ok("Next with nothing written asks for it", any("Write what happened" in d[1] for d in h.dialogs)
         and app.wizard["step"] == 0)
    box.insert("1.0", ACCOUNT + " Alex was engaged.")
    n0 = len(h.PROMPTS)
    h.button(app.page, "Next").invoke()
    h.pump(root, 20, until=lambda: app.wizard and app.wizard.get("note") and not app.busy)
    h.pump(root, 0.5)
    w = app.wizard
    prompts = h.PROMPTS[n0:]
    h.ok("it goes straight to writing the notes (no transcription)", w["step"] == 3 and w["note"] and prompts,
         (w["step"], len(prompts)))
    h.ok("…from the Counselor's written account, names hidden",
         all("written account of what happened in the session" in p and ACCOUNT in p and "Alex" not in p
             for p in prompts[:1]), prompts[0][-600:] if prompts else "")
    h.button(app.page, "Save session").invoke(); h.pump(root, 1)
    saved = [d for _, d in V.sessions_for(cid) if d["meta"].get("session_date") != "2026-11-01"]
    h.ok("saved with what you wrote and marked as written (no transcript)",
         len(saved) == 1 and saved[0]["meta"].get("source") == "written" and ACCOUNT in saved[0]["observations"]
         and not saved[0]["transcript"] and saved[0]["note"], saved[0]["meta"] if saved else None)

    # contact log order
    for d_, t_ in (("2026-11-03", "Oldest call."), ("2026-11-09", "Newest call.")):
        app.comm_add(cid, {"date": d_, "time": "", "with": "parent", "who": "", "method": "Phone call",
                           "text": t_, "notes": ""})
    app._contact_focus = None
    app.open_client(cid, select="comms"); h.pump(root, 0.8)
    vw = lambda: next(x for x in h.widgets(app.page) if hasattr(x, "_contacts"))
    view = lambda: vw()._contacts
    first = lambda: view()["list"].get(0)
    order_btn = lambda: next(b for b in h.widgets(vw(), "TButton") if "first" in str(b.cget("text")))
    h.ok("the contact log starts newest first, its button saying so",
         first().startswith("2026-11-09") and "Newest first" in str(order_btn().cget("text")), first())
    order_btn().invoke(); h.pump(root, 0.6)
    sel = view()["list"].curselection()
    h.ok("clicking it flips to oldest first (the newest stays selected), remembered",
         first().startswith("2026-11-03") and "Oldest first" in str(order_btn().cget("text")) and sel
         and view()["list"].get(sel[0]).startswith("2026-11-09") and h.ss.load_config().get("contact_sort") == "asc",
         (order_btn().cget("text"), sel and view()["list"].get(sel[0]), h.ss.load_config().get("contact_sort")))
    app.client_id = None; app.show_home(); h.pump(root, 0.4)
    app.open_client(cid, select="comms"); h.pump(root, 0.8)
    h.ok("…still oldest first when the patient is opened again", first().startswith("2026-11-03"))
    order_btn().invoke(); h.pump(root, 0.6)
    h.ok("…and back", first().startswith("2026-11-09") and "Newest first" in str(order_btn().cget("text")))
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
