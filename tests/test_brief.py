"""Feature 10: pre-session brief."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H
from datetime import date, timedelta

BRIEF = ("LAST TIME\n- 11/10: work stress and shame.\n\nOPEN THREADS\n- Walk after work?\n\nHOMEWORK AND FOLLOW-UPS TO CHECK\n"
         "- Thought log\n\nWHAT THE PLAN SAYS TO WORK ON\n- Sister relationship (not lately)\n\nWATCH FOR\n- PHQ-9 up\n\n"
         "WAYS TO OPEN\n- How did the walks go?")


def script(app, root, V, h):
    ss = h.ss
    today = date.today()
    plan = "Goal 1: Reduce anxiety at work\nGoal 2: Strengthen relationship with sister\n"
    cid = V.save_client({"code": "AB1", "names_to_hide": "Alex Rivera, Patient", "names_format": 2,
                         "treatment_plans": [{"version": 1, "text": plan, "created": "2026-10-01T10:00:00"}],
                         "homework": [{"id": "h1", "title": "Thought log", "date": "2026-11-03", "status": "assigned"}],
                         "followups": [{"id": "f1", "text": "Send sleep hygiene handout", "date": "2026-11-03", "done": False}],
                         "measures": [ss.measure_entry("PHQ-9", "2026-10-01", total=10), ss.measure_entry("PHQ-9", "2026-11-01", total=16)]})
    for i in range(4):
        V.save_session({"meta": {"client_id": cid, "session_date": (today - timedelta(days=28 - i * 7)).isoformat(),
                                 "session_number": str(i + 1), "kind": "progress", "created": "x"},
                        "note": f"Session {i + 1}: Alex Rivera discussed anxiety at work.",
                        "overview": "Overview of the latest." if i == 3 else "",
                        "goals_check": {"key": ss.goals_key(ss.patient_goals(V.load_client(cid))),
                                        "items": [{"goal": "Reduce anxiety at work", "addressed": True, "evidence": ""},
                                                  {"goal": "Strengthen relationship with sister", "addressed": False, "evidence": ""}]},
                        "review": {"feedback": "ONE THING TO PRACTICE NEXT SESSION\nReflect feelings before asking.\n"} if i == 3 else {}})
    app.refresh_clients(); app.open_client(cid); h.pump(root, 1)
    h.ok("the patient page has 'Prep for next session'", h.action(root, "Prep for next session") is not None)
    h.RESP[:] = [("PRE-SESSION BRIEF", BRIEF)]
    n0 = len(h.PROMPTS)
    h.action(root, "Prep for next session").invoke()
    h.pump(root, 20, lambda: not app.busy and V.list_reports("brief", cid))
    h.pump(root, 1)
    sent = "\n".join(h.PROMPTS[n0:])
    h.ok("names hidden", "Alex" not in sent and "[Patient]" in sent)
    h.ok("the brief reads the last 3 notes (not the oldest)", "Session 4:" in sent and "Session 2:" in sent and "Session 1:" not in sent)
    h.ok("...and the latest overview, goals (with the idle one marked), homework, follow-ups, scores and practice focus",
         "Overview of the latest." in sent and "(not worked on in recent sessions)" in sent and "Thought log" in sent
         and "Send sleep hygiene handout" in sent and "PHQ-9 16" in sent and "Reflect feelings before asking." in sent)
    reps = V.list_reports("brief", cid)
    h.ok("saved for this patient (encrypted)", reps and reps[0]["text"] == BRIEF and reps[0]["code"] == "AB1")
    wins = [w for w in h.widgets(root) if hasattr(w, "_doc")]
    h.ok("it opened to read", wins and "WAYS TO OPEN" in wins[-1]._doc["text"].get("1.0", "end"))
    h.shot("brief.png")
    for _ in range(6):
        app.prep_brief(V.load_client(cid)); h.pump(root, 10, lambda: not app.busy); h.pump(root, 0.3)
    h.ok("only the latest five briefs are kept", len(V.list_reports("brief", cid)) == 5)


H.start(script)
