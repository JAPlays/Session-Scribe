"""Patient page: an Assessments tab (scores + any other assessment, with its form) and a Termination tab
(the practice's termination letter, its reasons and the completed form, stored encrypted)."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "TR1", "names_to_hide": "Jordan Lee, Patient\nSam, Brother", "names_format": 2,
                         "measures": [ss.measure_entry("PHQ-9", "2026-10-01", total=12)]})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-02", "session_number": "1", "kind": "progress",
                             "created": "2026-10-02T10:00:00"}, "note": "Session note."})
    bare = V.save_client({"code": "TR2"})                       # no sessions yet
    tmp = tempfile.mkdtemp()
    form = os.path.join(tmp, "PCL-5 completed.txt")
    open(form, "w").write("PCL-5 total 42")
    letter_pdf = os.path.join(tmp, "Termination Letter.txt")
    open(letter_pdf, "w").write("Submitted termination form")
    app.refresh_clients()

    def lines():
        return h.texts(app.page)

    # ---- Assessments
    app.open_client(cid, select="assess"); h.pump(root, 1)
    lb = app.page._search["list"]
    rows = [lb.get(i) for i in range(lb.size())]
    h.ok("the patient list has Assessments", "🧾  Assessments  · 1" in rows, rows)
    h.ok("…and no Termination until a letter is filled out", not any("Termination" in r for r in rows), rows)
    h.ok("Assessments shows the PHQ-9 score", "PHQ-9" in lines() and "12  (moderate)" in lines())
    app.assessment_dialog(V.load_client(cid)); h.pump(root, 0.5)
    w = app._assess_win
    w["name"].set("PCL-5 (PTSD)"); w["date"].set("2026-11-01"); w["score"].set("42 — probable PTSD")
    w["notes"].insert("1.0", "Completed in session.")
    w["pick"]["path"] = form
    w["save"](); h.pump(root, 1)
    c = V.load_client(cid)
    a = (c.get("assessments") or [{}])[0]
    h.ok("an assessment is saved with its date, result and notes",
         a.get("name") == "PCL-5 (PTSD)" and a.get("date") == "2026-11-01" and a.get("score").startswith("42")
         and a.get("notes") == "Completed in session.", a)
    fe = next((x for x in c.get("files") or [] if x["id"] == a.get("file_id")), None)
    h.ok("…and its completed form is stored encrypted as an Assessment file",
         fe is not None and fe["kind"] == "assessment" and V.load_file(fe["id"]) == b"PCL-5 total 42")
    h.ok("the tab lists it with View/Open for the form", "PCL-5 (PTSD)" in lines() and "📎  PCL-5 completed.txt" in lines()
         and h.button(app.page, "View") is not None, lines()[-600:])
    vw = app.vault_file_view(fe); h.pump(root, 0.3)
    h.ok("View shows the form's text inside the app", vw is not None and "PCL-5 total 42" in h.texts(vw))
    vw.destroy()
    app.assessment_dialog(V.load_client(cid), a); h.pump(root, 0.3)
    app._assess_win["score"].set("38"); app._assess_win["save"](); h.pump(root, 1)
    c = V.load_client(cid)
    h.ok("editing updates it (no duplicate)", len(c["assessments"]) == 1 and c["assessments"][0]["score"] == "38"
         and c["assessments"][0]["file_id"] == a["file_id"])
    app.assessment_dialog(V.load_client(cid)); h.pump(root, 0.3)
    app._assess_win["name"].set("GAD-7"); app._assess_win["save"](); h.pump(root, 0.5)
    h.ok("choosing PHQ-9 or GAD-7 opens their own score form instead",
         any("add a score" in str(t.title()) for t in root.winfo_children() if hasattr(t, "title") and t.winfo_class() == "Toplevel"))
    for t in [t for t in root.winfo_children() if t.winfo_class() == "Toplevel"]:
        t.destroy()

    # ---- Termination
    app.open_client(cid); h.pump(root, 1)
    h.ok("the Patient menu has Termination letter…", h.action(app.page, "Termination letter") is not None)
    h.action(app.page, "Termination letter").invoke(); h.pump(root, 0.5)
    t = app._term_win
    h.ok("the patient's name is filled in from the names on file", t["name"].get() == "Jordan Lee", t["name"].get())
    t["date"].set("2026-11-13")
    t["reasons"]["Lack of Communication from Patient to Continue or Resume Treatment"].set(True)
    t["reasons"]["Other (see below)"].set(True)
    t["other"].insert("1.0", "Moved out of state.")
    t["save"](); h.pump(root, 0.5)
    h.ok("it asks for the therapist's name first", "termination" not in V.load_client(cid))
    t["therapist"].set("Jake W., MA, Counseling Extern")
    t["pick"]["path"] = letter_pdf
    t["archive"].set(True)
    t["save"](); h.pump(root, 1)
    c = V.load_client(cid)
    term = c.get("termination") or {}
    h.ok("the termination is saved with its date, reasons and therapist",
         term.get("date") == "2026-11-13" and len(term.get("reasons") or []) == 2
         and term.get("other") == "Moved out of state." and term.get("therapist").startswith("Jake W."), term)
    tf = next((x for x in c.get("files") or [] if x["id"] == term.get("file_id")), None)
    h.ok("…the completed form is stored encrypted", tf is not None and tf["kind"] == "termination"
         and V.load_file(tf["id"]) == b"Submitted termination form")
    h.ok("…and the patient is archived when asked", c.get("archived") is True)
    letter = app.termination_letter(term)
    h.ok("the letter has the practice's wording, name, reasons and signature",
         "concluding our therapeutic relationship" in letter and "Jordan Lee" in letter
         and "• Lack of Communication from Patient" in letter and "Moved out of state." in letter
         and letter.rstrip().endswith("Counseling Extern"))
    page = lines()
    h.ok("the tab shows the letter, the date chip and the form", "Terminated" in page and "Copy letter" in page
         and "📎  Termination Letter.txt" in page, page[:500])
    h.ok("the patient's header says Terminated", any(str(w.cget("text")).startswith("Terminated ")
                                                     for w in h.widgets(app.page, "TLabel")))
    h.ok("nothing about the termination went to the AI", not any("Jordan" in p or "concluding" in p for p in h.PROMPTS))

    # ---- a patient with no sessions can still open these tabs; the therapist name is remembered
    lb = app.page._search["list"]
    h.ok("once saved, the Termination tab appears", any(lb.get(i).startswith("📄  Termination") for i in range(lb.size())))
    app.open_client(bare, select="term"); h.pump(root, 1)
    h.ok("a patient without a letter (or sessions) has no Termination tab and doesn't fail",
         not any("Termination" in app.page._search["list"].get(i) for i in range(app.page._search["list"].size())))
    app.termination_dialog(V.load_client(bare)); h.pump(root, 0.3)
    h.ok("the therapist's name is filled in from last time", app._term_win["therapist"].get().startswith("Jake W."))
    app._term_win["win"].destroy()

    # ---- removing
    import tkinter.messagebox as mb
    app.open_client(cid, select="term"); h.pump(root, 0.5)
    app.termination_remove(V.load_client(cid)); h.pump(root, 0.5)
    c = V.load_client(cid)
    h.ok("removing the termination deletes it and its form", "termination" not in c
         and not any(x.get("kind") == "termination" for x in c.get("files") or []))
    app.assessment_remove(V.load_client(cid), c["assessments"][0]); h.pump(root, 0.5)
    c = V.load_client(cid)
    h.ok("removing an assessment deletes it and its form", not c.get("assessments")
         and not any(x.get("kind") == "assessment" for x in c.get("files") or []))
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
