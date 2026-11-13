"""Feature 20: caseload dashboard."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H
from datetime import date, timedelta


def script(app, root, V, h):
    ss = h.ss
    today = date.today()
    D = lambda n: (today - timedelta(days=n)).isoformat()
    a = V.save_client({"code": "AB1", "treatment_plans": [{"version": 1, "text": "Goal 1: Reduce anxiety\nGoal 2: Sleep",
                                                             "created": (today - timedelta(days=88)).isoformat() + "T10:00:00"}],
                       "measures": [ss.measure_entry("PHQ-9", D(60), total=8), ss.measure_entry("PHQ-9", D(5), total=15)],
                       "followups": [{"id": "f1", "text": "Send release form to school", "date": D(3), "done": False}],
                       "homework": [{"id": "h1", "title": "Thought log", "date": D(14), "status": "assigned"}]})
    b = V.save_client({"code": "CD2"})
    c = V.save_client({"code": "EF3", "archived": True})
    V.save_session({"meta": {"client_id": a, "session_date": D(20), "session_number": "1", "kind": "progress", "created": "x"},
                    "note": "n", "risk_check": {"key": "k", "safety_plan": "not mentioned", "items": [
                        {"type": "suicidal thoughts", "summary": "passive SI", "when": "", "action": "assessed"}]}})
    V.save_session({"meta": {"client_id": a, "session_date": D(6), "session_number": "2", "kind": "progress", "created": "x",
                             "needs_review": True, "review_reason": "speaker labels were guessed"}, "note": "n2"})
    V.save_session({"meta": {"client_id": a, "session_date": D(2), "session_number": "3", "kind": "progress", "created": "x"},
                    "note": "", "transcript": "[00:01] x"})
    for i in range(3):
        V.save_session({"meta": {"client_id": b, "session_date": D(30 - i * 7), "session_number": str(i + 1),
                                 "kind": "progress", "created": "x"}, "note": "x"})
    V.save_session({"meta": {"client_id": c, "session_date": D(1), "kind": "progress", "needs_review": True, "created": "x"}, "note": "x"})
    V.save_session({"meta": {"client_id": b, "session_date": D(9), "kind": "parent", "created": "x"},
                    "note": "Contact.\n\nFollow-up:\n- Call the father back\n"})
    app.refresh_clients(); app.show_home(); h.pump(root, 1)
    page = [w for w in h.widgets(root) if hasattr(w, "_dash")]
    h.ok("home shows the caseload dashboard", page and "Your caseload · 2 active patients" in h.texts(root))
    data = page[0]._dash
    codes = lambda k: [r["client"]["code"] for r in data[k]]
    h.ok("draft to review listed (archived patient left out)", codes("review") == ["AB1"])
    h.ok("session with a transcript but no note listed", codes("no_note") == ["AB1"])
    h.ok("plan review due soon, and a patient with 3 sessions and no plan", sorted(codes("plans")) == ["AB1", "CD2"])
    h.ok("recent risk + missing safety plan flagged", codes("risk").count("AB1") == 2)
    h.ok("PHQ-9 getting worse listed", codes("scores") == ["AB1"] and "8 → 15" in data["scores"][0]["text"])
    h.ok("open follow-ups from every patient (contact-note lists included)", sorted(r["text"] for r in data["followups"]) ==
         ["Call the father back", "Send release form to school"])
    h.ok("homework assigned two weeks ago is due a check", codes("homework") == ["AB1"])
    h.shot("dashboard.png")
    # tick a follow-up from the dashboard
    pg = [w for w in h.widgets(root) if hasattr(w, "_dash")][0]
    cbs = [w for w in h.widgets(pg, "TCheckbutton")]
    cbs[0].invoke(); h.pump(root, 1)
    page = [w for w in h.widgets(root) if hasattr(w, "_dash")]
    h.ok("ticking a follow-up on the dashboard marks it done", len(page[-1]._dash["followups"]) == 1)
    pg = [w for w in h.widgets(root) if hasattr(w, "_dash")][0]
    next(w for w in h.widgets(pg, "TButton") if w.cget("text") == "Open").invoke(); h.pump(root, 1)
    h.ok("Open goes to the patient", app.client_id in (a, b))


H.start(script)
