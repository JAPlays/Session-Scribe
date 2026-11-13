"""Feature 8: risk timeline."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H
from datetime import date, timedelta


def answer(prompt):
    doc = prompt.split("Documentation (")[1].lower()
    items, sp = [], "not mentioned"
    if "denied si" in doc:
        items.append({"type": "screened — denied", "summary": "Patient denied SI/HI.", "when": "", "action": "screened"})
    if "wanted to die" in doc:
        items.append({"type": "suicidal thoughts", "summary": "[Patient] said she sometimes wanted to die last week, no plan.",
                      "when": "[12:05]", "action": "Assessed; safety plan created; crisis line given."})
        sp = "created"
    if "hit me" in doc:
        items.append({"type": "abuse or neglect", "summary": "Reported partner hit her.", "when": "", "action": "not documented"})
    return json.dumps({"items": items, "safety_plan": sp})


def script(app, root, V, h):
    ss = h.ss
    today = date.today()
    cid = V.save_client({"code": "AB1", "names_to_hide": "Alex Rivera, Patient", "names_format": 2})
    recs = [((today - timedelta(days=60)).isoformat(), "Patient denied SI/HI. Discussed school.", ""),
            ((today - timedelta(days=20)).isoformat(), "Alex Rivera disclosed passive SI.",
             "[12:00] Clinician: How have you been?\n[12:05] Patient: Honestly I wanted to die some days.\n[12:09] Clinician: Thank you for telling me."),
            ((today - timedelta(days=10)).isoformat(), "Explored hobbies. Mood brighter.", "[01:00] Patient: I painted all weekend."),
            ((today - timedelta(days=3)).isoformat(), "Patient reported partner hit me during an argument.", "")]
    sids = []
    for i, (day, note, tr) in enumerate(recs):
        sids.append(V.save_session({"meta": {"client_id": cid, "session_date": day, "session_number": str(i + 1),
                                             "kind": "progress", "created": "x"}, "note": note, "transcript": tr}))
    src = ss.risk_source(V.load_session(sids[1]))
    h.ok("only risk lines of the transcript are sent (with their neighbours)", "wanted to die" in src and "How have you been" in src)
    h.ok("a record with no risk words is skipped without the AI", not ss.RISK_RE.search(ss.risk_source(V.load_session(sids[2]))))
    app.refresh_clients(); app.open_client(cid, select="insights:risk"); h.pump(root, 1)
    h.ok("the Risk timeline tab opens with a check button", h.button(root, "Check 4 records") is not None)
    h.RESP[:] = [("list every mention of risk", answer)]
    n0 = len(h.PROMPTS)
    h.button(root, "Check 4 records").invoke()
    h.pump(root, 30, lambda: not app.busy and all(d.get("risk_check") for _, d in V.sessions_for(cid)))
    h.pump(root, 1)
    sent = "\n".join(h.PROMPTS[n0:])
    h.ok("the AI read only the 3 records that mention risk", len(h.PROMPTS) - n0 == 3, len(h.PROMPTS) - n0)
    h.ok("names hidden", "Alex" not in sent and "[Patient]" in sent)
    rs = ss.risk_summary(V.load_client(cid), V.sessions_for(cid))
    h.ok("timeline newest first: abuse, then suicidal thoughts", [r["type"] for r in rs["rows"]] == ["abuse or neglect", "suicidal thoughts"])
    h.ok("the denial is the latest screening (kept apart from the timeline)", rs["last_screen"] and "denied" in rs["last_screen"]["summary"])
    h.ok("safety plan found", rs["plans"] and rs["plans"][0][1] == "created")
    h.ok("both recent items flagged", len(rs["recent"]) == 2 and not rs["needs_plan"])
    t = h.texts(root)
    h.ok("the page shows screening and safety-plan lines", "Latest screening:" in t and "Safety plan: created" in t)
    h.ok("both items listed with what was done", "abuse or neglect" in t and "crisis line" in t and "What was done: not documented" in t
         and "[[" not in t)
    lb = [w for w in h.widgets(root) if type(w).__name__ == "NiceList" and any("Ask AI" in w.get(i) for i in range(w.size()))][0]
    h.ok("the patient's list flags recent risk", any("⚠ risk" in lb.get(i) for i in range(lb.size())))
    h.shot("risk_tab.png")
    # nothing re-read unless a record changes
    h.ok("checked records aren't read again", not ss.risk_todo(V.sessions_for(cid)))
    d = V.load_session(sids[2]); d["note"] += " Patient mentioned a safety plan review."; V.save_session(d, sids[2])
    h.ok("a changed record is checked again", len(ss.risk_todo(V.sessions_for(cid))) == 1)
    # no safety plan -> warning
    c2 = V.save_client({"code": "CD2"})
    V.save_session({"meta": {"client_id": c2, "session_date": (today - timedelta(days=2)).isoformat(), "kind": "progress",
                             "created": "x"}, "note": "x", "risk_check": {"key": "k", "items": [
                                 {"type": "self-harm", "summary": "cutting", "when": "", "action": "not documented"}],
                                 "safety_plan": "not mentioned"}})
    h.ok("recent risk with no safety plan is flagged", ss.risk_summary(V.load_client(c2), V.sessions_for(c2))["needs_plan"])


H.start(script)
