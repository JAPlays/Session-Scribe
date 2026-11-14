"""Export: the contact log and the assessments can be included (made-up data only)."""
import sys, os, tempfile, zipfile, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    import tkinter.filedialog as fd
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "EXP1"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-02", "session_number": "1", "kind": "progress",
                             "created": "a"}, "note": "Progress note text."})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-04", "kind": "parent", "contact_with": "parent",
                             "contact_method": "Phone call", "created": "b"}, "note": "Mother called about forms."})
    app.comm_add(cid, {"date": "x", "with": "parent", "who": "", "method": "Email", "notes": "",
                       "text": "[2026-11-10 9:05 AM] Pat Parent → Counselor:\nSubject: Scheduling\nCould we move "
                               "Thursday to 5pm?\n\n[2026-11-10 2:30 PM] Counselor → Pat Parent:\nSubject: Re: "
                               "Scheduling\n5pm works."})
    app.comm_add(cid, {"date": "x", "with": "patient", "who": "", "method": "Text messages", "notes": "",
                       "text": "[2026-11-11 8:34 AM] Patient: Running late\n[2026-11-11 8:40 AM] Counselor: No problem"})
    app.comm_add(cid, {"date": "2026-11-12", "time": "10:15", "with": "parent", "who": "", "method": "Phone call",
                       "text": "Returned call about the release form.", "notes": ""})
    c = V.load_client(cid)
    ans1 = {str(i): 2 for i in range(1, 10)}
    ans2 = {str(i): 1 for i in range(1, 10)}
    c["assessments"] = [
        {"id": "a1", "name": "PHQ-9", "instrument": "phq9", "date": "2026-11-03", "time": "10:00", "answers": ans1,
         "result": ss.score_assessment("phq9", ans1), "administered": True, "completed": True, "added": "1"},
        {"id": "a2", "name": "PHQ-9", "instrument": "phq9", "date": "2026-11-17", "time": "10:00", "answers": ans2,
         "result": ss.score_assessment("phq9", ans2), "administered": True, "completed": True, "added": "2"},
        {"id": "a3", "name": "School behavior checklist", "date": "2026-11-05", "score": "Moderate concerns",
         "notes": "Given by the school counselor.", "added": "3"}]
    V.save_client(c)

    labels = dict(ss.EXPORT_KINDS)
    h.ok("the export list offers the contact log and assessments",
         labels.get("contacts") == "Contact log" and labels.get("assessments") == "Assessments")

    client, sessions = V.load_client(cid), V.sessions_for(cid)
    blocks = ss.collect_export(client, sessions, {"notes", "contacts", "assessments"})
    text = "\n".join(t for _, t in blocks)
    heads = [t for k, t in blocks if k == "h1"]
    h.ok("sections: the session, then Contact log, then Assessments", heads[-2:] == ["Contact log", "Assessments"]
         and any("Nov 2, 2026" in x or "2026" in x for x in heads[:-2]), heads)
    h2 = [t for k, t in blocks if k == "h2"]
    h2 = h2[1:]                                   # (the session's own "Progress note" heading)
    cl = h2[:h2.index(next(x for x in h2 if "PHQ-9" in x or "checklist" in x))]
    h.ok("the contact log: every contact oldest first (the contact note too), with how and with whom",
         len(cl) == 4 and "Parent contact note" in cl[0] and "Email with parent / guardian" in cl[1]
         and "Text messages with the patient" in cl[2] and "10:15 — Phone call" in cl[3], cl)
    h3 = [t for k, t in blocks if k == "h3"]
    h.ok("…each email under its own 'who → whom · date time' heading; texts as a list",
         "Pat Parent → Counselor  ·  " in h3[0] and "9:05 AM" in h3[0] and "Counselor → Pat Parent" in h3[1]
         and "8:34 AM   Patient: Running late\n8:40 AM   Counselor: No problem" in text
         and text.count("Subject: ") == 1, (h3[:3], text[-900:]))
    h.ok("…the contact note isn't repeated with the sessions",
         text.count("Mother called about forms.") == 1 and not any("Nov 4" in x for x in heads))
    ab = h2[len(cl):]
    h.ok("assessments oldest first, recorded ones too", [x.split(" — ")[0] for x in ab] ==
         ["PHQ-9", "School behavior checklist", "PHQ-9"], ab)
    h.ok("…with the result, the change since the last time and every answer",
         "Result: " in text and "Change: -9 since" in text and "Given by the school counselor." in text
         and "Moderate concerns" in text and "Answers" in h3 and re.search(r"(?m)^1\. .+ — ", text), text[-1500:])
    rng = ss.collect_export(client, sessions, {"contacts", "assessments"}, "2026-11-10", "2026-11-11")
    rt = "\n".join(t for _, t in rng)
    h.ok("a date range limits both", "Running late" in rt and "5pm works." in rt and "release form" not in rt
         and "PHQ-9" not in rt and "Mother called" not in rt, rt)
    only_notes = "\n".join(t for _, t in ss.collect_export(client, sessions, {"notes"}))
    h.ok("without them, nothing from the log or assessments (contact notes stay with notes, as before)",
         "Contact log" not in only_notes and "Assessments" not in only_notes and "Mother called" in only_notes)

    # the dialog, to Word and PDF
    d = tempfile.mkdtemp()
    for ext in ("docx", "pdf"):
        app.export_all(V.load_client(cid)); h.pump(root, 0.4)
        t = root._export_win._test
        for k, v in t["inc"].items():
            v.set(k in ("contacts", "assessments"))
        t["fmt"].set(ext)
        out = os.path.join(d, "x." + ext)
        fd.asksaveasfilename = lambda **k: out
        import tkinter.messagebox as mb
        mb.askyesno = lambda *a, **k: False
        t["go"](); h.pump(root, 0.3)
        ok = os.path.isfile(out)
        if ok and ext == "docx":
            doc = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
            ok = "Contact log" in doc and "Heading3" in doc and "Pat Parent → Counselor" in doc and "Assessments" in doc
        if ok and ext == "pdf":
            raw = open(out, "rb").read().decode("latin-1")
            ok = raw.startswith("%PDF") and "Contact log" in raw and "Pat Parent -> Counselor" in raw
        h.ok(f"exporting just those two from the dialog makes a {ext.upper()} with them", ok)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
