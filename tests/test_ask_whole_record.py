"""Ask AI reads the whole record: assessments, progress & risk, the contact log, imported documents and the
registration form — not only notes and transcripts — and still fits the model. Made-up data only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "WR3", "goals": "Fewer panic attacks"})
    note = ("Counselor and patient reviewed the week and practiced grounding; patient agreed to keep a log. " * 30)
    talk = "Clinician: How was the week?\nPatient: School was a lot but I managed.\n" * 40
    for i in range(30):
        d = {"meta": {"client_id": cid, "session_date": f"2026-{9 + i // 28:02d}-{i % 28 + 1:02d}",
                      "session_number": str(i + 1), "kind": "progress", "created": str(i)},
             "note": note, "overview": "", "transcript": talk}
        if i == 5:
            d["risk_check"] = {"items": [{"type": "suicidal thoughts", "summary": "passive thoughts during panic, no "
                               "plan or intent", "when": "", "action": "assessed; safety plan reviewed"}],
                               "safety_plan": "reviewed"}
        V.save_session(d)
    c = V.load_client(cid)
    ans = {str(i): 2 for i in range(1, 10)}
    c["assessments"] = [{"id": "a1", "name": "PHQ-9", "instrument": "phq9", "date": "2026-10-02", "time": "10:00",
                         "answers": ans, "result": ss.score_assessment("phq9", ans), "administered": True,
                         "completed": True, "added": "1"}]
    c["homework"] = [{"id": "h1", "title": "Breathing log every evening", "status": "assigned", "date": "2026-10-03"}]
    c["followups"] = [{"id": "f1", "text": "Send the school release form", "date": "2026-10-04", "done": False}]
    c["communications"] = [{"id": "c1", "date": "2026-10-06", "time": "9:05 AM", "with": "parent", "who": "",
                            "method": "Email", "notes": "",
                            "text": "[2026-10-06 9:05 AM] Pat Parent → Counselor:\nHi, quick update about the week. "
                                    + "Things were busy at home. " * 12
                                    + "He was nervous about the field trip permission slip and the bus ride."}]
    c["contact_summaries"] = [{"id": "s1", "start": "2026-10-01", "end": "2026-10-31",
                               "text": "OVERVIEW\nThree contacts with the parent about school anxiety.",
                               "created": "x", "updated": "x", "needs_review": False}]
    c["documents"] = [{"id": "d1", "type": "Psychological evaluation", "date": "2026-08-20", "title": "Eval",
                       "text": "Psychological evaluation. WISC-V scores were average; anxiety noted at school."}]
    c["registration_info"] = ss.organize_registration("Child Registration", "2026-08-01", [
        ("Info", "Patient's Full Name", "Robin Example"), ("Home", "Is there a firearm in the home?", "No"),
        ("Info", "Reason(s) for beginning psychotherapy at this time", "Panic attacks at school")])
    V.save_client(c)
    app.refresh_clients(); app.open_client(cid, select="ask"); h.pump(root, 1)
    f = app._ask_send.__func__
    cfg = f.__closure__[f.__code__.co_freevars.index("cfg")].cell_contents

    def ask(q):
        n0 = len(h.PROMPTS)
        app._ask_send(V.load_client(cid), None, q)
        h.pump(root, 20, until=lambda: len(h.PROMPTS) > n0 and not app.busy)
        h.pump(root, 0.4)
        return h.PROMPTS[n0] if len(h.PROMPTS) > n0 else ""
    p = ask("Give me an overview of how this patient is doing.")
    checks = {"assessments": "PHQ-9" in p and "ASSESSMENTS" in p,
              "risk": "passive thoughts during panic" in p and "Safety plan reviewed" in p,
              "homework": "Breathing log every evening" in p, "follow-ups": "Send the school release form" in p,
              "contact log": "CONTACT LOG" in p and "Email with parent" in p,
              "contact summary": "Three contacts with the parent" in p,
              "documents": "WISC-V" in p, "registration": "Panic attacks at school" in p,
              "notes": "practiced grounding" in p, "transcripts": "School was a lot" in p}
    h.ok("a general question sees every part of the record", all(checks.values()),
         [k for k, v in checks.items() if not v])
    h.ok("…and still fits the model", len(p) <= ss.ai_prompt_chars(cfg, 1800), len(p))
    p = ask("What did mom say in her emails?")
    h.ok("a question about the emails gets the whole thread", "field trip permission slip" in p)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
