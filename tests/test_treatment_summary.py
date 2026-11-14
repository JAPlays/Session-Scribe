"""Write ▸ Letter… has a Summary of treatment (the agency's template: intake date, sessions, how often and how
long, diagnosis, presentation, goals, course and how it ended), letters can be saved on the clinician's own
Word letterhead, and notes / case overviews use what the clinician told Ask AI. Made-up data only."""
import sys, os, tempfile, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

LETTER = ("November 14, 2026\nTo Whom It May Concern,\n\nI write to you today to summarize the clinical course and "
          "treatment for [Patient], a patient currently under my care as a psychotherapist.\n\nSincerely,\n\n"
          "Sam Counselor\tDr. Supervisor\nPsychotherapist\tClinical Supervisor")


def script(app, root, V, h):
    ss = h.ss
    import tkinter.filedialog as fd
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "TS1", "names_to_hide": "Robin Example, Patient", "names_format": 2,
                         "goals": "Improve communication at home\nCoping skills for stress"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-01-31", "kind": "intake", "created": "a",
                             "duration": 3000}, "note": "Intake. Robin reports worry and frustration at home."})
    for i, day in enumerate(("2026-02-07", "2026-02-14", "2026-02-21", "2026-02-28", "2026-03-07"), 1):
        V.save_session({"meta": {"client_id": cid, "session_date": day, "session_number": str(i), "kind": "progress",
                                 "created": day, "duration": 2700}, "note": f"Session {i}: worked on communication."})
    sessions = V.sessions_for(cid)
    facts = ss.letter_facts(V.load_client(cid), sessions, ss.LETTER_TYPES["summary"][1])
    h.ok("the summary's facts: intake date, sessions after it, weekly, about 45 minutes, presentation, ongoing",
         "Intake assessment completed: 2026-01-31" in facts and "sessions after the intake: 5" in facts
         and "How often: weekly" in facts and "about 45 minutes" in facts and "worry and frustration" in facts
         and "Treatment is ongoing" in facts, facts)

    # the letter
    app.refresh_clients(); app.open_client(cid); h.pump(root, 0.6)
    win = app.letter_dialog(V.load_client(cid)); h.pump(root, 0.4)
    t = win._test
    t["kind"].set("summary"); h.pump(root, 0.1)
    h.ok("Summary of treatment is offered, with its parts ticked",
         all(t["parts"][k].get() for k in ("intake", "schedule", "presentation", "ending", "goals")))
    t["sig"].delete("1.0", "end"); t["sig"].insert("1.0", "Sam Counselor, Psychotherapist")
    t["consent"].set(True)
    n0 = len(h.PROMPTS)
    t["go"]()
    h.pump(root, 20, until=lambda: len(h.PROMPTS) > n0 and not app.busy)
    h.pump(root, 0.6)
    p = h.PROMPTS[n0] if len(h.PROMPTS) > n0 else ""
    h.ok("the AI is given the agency's summary structure and the facts (name hidden)",
         "summarize the clinical course and treatment for [Patient]" in p and "we do not release our progress notes"
         in p and "To Whom It May Concern" in p and "a patient currently under my care" in p
         and "Intake assessment completed" in p and "Robin" not in p, p[:300])
    reps = V.list_reports("letter", cid)
    h.ok("…and the letter is saved", reps and reps[0]["title"] == "Summary of treatment")

    # your letterhead
    d = tempfile.mkdtemp()
    tpl = os.path.join(d, "Letter template.docx")
    ss.write_docx(tpl, [("p", "Template body to be replaced")], footer="Example Agency · 1 Main St")
    fd.askopenfilename = lambda **k: tpl
    win = app.letter_dialog(V.load_client(cid)); h.pump(root, 0.3)
    win._test["pick_lh"](); h.pump(root, 0.2); win.destroy()
    h.ok("a Word template can be chosen as the letterhead", os.path.isfile(ss.letterhead_path()))
    dw = app.open_report(reps[0]["id"]); h.pump(root, 0.4)
    h.ok("a letter now offers 'Save on letterhead…'", h.button(dw, "Save on letterhead") is not None)
    out = os.path.join(d, "letter.docx")
    fd.asksaveasfilename = lambda **k: out
    app.save_on_letterhead(dw, LETTER, "letter"); dw.destroy()
    z = zipfile.ZipFile(out)
    doc = z.read("word/document.xml").decode("utf-8")
    h.ok("…the letter goes into the template: its text, the template's footer and page setup kept",
         "To Whom It May Concern," in doc and "Template body" not in doc and "word/footer1.xml" in z.namelist()
         and "footerReference" in doc and "<w:pgSz" in doc, doc[:300])
    h.ok("…signature columns kept as tabs", "<w:tab/>" in doc and "Clinical Supervisor" in doc)

    # what the clinician told Ask AI, in notes and case overviews
    c = V.load_client(cid)
    c["ai_memory"] = [{"id": "m1", "text": "Mother reported a new medication in early March.", "from": "clinician"},
                      {"id": "m2", "text": "Possibly avoidant attachment.", "from": "answer"}]
    V.save_client(c)
    ctx, used = ss.build_context(V.load_client(cid), [d_ for _, d_ in sessions], 2, lambda s: s or "")
    h.ok("notes get what you told Ask AI (not conclusions from AI answers), as background",
         "BACKGROUND THE CLINICIAN GAVE" in ctx and "new medication" in ctx and "avoidant" not in ctx
         and "what you told Ask AI" in used)
    n0 = len(h.PROMPTS)
    app.make_case_overview(V.load_client(cid))
    h.pump(root, 20, until=lambda: len(h.PROMPTS) > n0 and not app.busy)
    h.pump(root, 0.5)
    cp = h.PROMPTS[-1] if len(h.PROMPTS) > n0 else ""
    h.ok("…and so does the case overview", "new medication" in cp and "avoidant" not in cp)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script, responses=[("we do not release our progress notes", LETTER)])
