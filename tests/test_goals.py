"""Feature 7: goals check across notes."""
import sys, os, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

PLAN = ("TREATMENT PLAN\nGoal 1: Reduce anxiety at work\nObjective 1a: ...\nGoal 2: Improve sleep to 7 hours\n"
        "Goal 3: Strengthen relationship with sister\n")


def answer(prompt):
    note = prompt.split("Documentation (")[1].lower()
    return "Here you go:\n```json\n" + json.dumps({
        "1": {"addressed": "anxiety" in note, "evidence": "discussed anxiety at work" if "anxiety" in note else ""},
        "2": {"addressed": "sleep" in note, "evidence": "sleep routine" if "sleep" in note else ""},
        "3": {"addressed": False, "evidence": ""}}) + "\n```"


def script(app, root, V, h):
    ss = h.ss
    cid = V.save_client({"code": "AB1", "names_to_hide": "Alex Rivera, Patient", "names_format": 2,
                         "treatment_plans": [{"version": 1, "text": PLAN, "created": "2026-10-01T10:00:00"}]})
    notes = ["Alex Rivera talked about anxiety at work.", "Focused on sleep hygiene.", "Anxiety and sleep both discussed.",
             "Explored anxiety triggers at work.", "Reviewed sleep log; anxiety lower."]
    for i, n in enumerate(notes):
        V.save_session({"meta": {"client_id": cid, "session_date": f"2026-10-{10 + i * 7:02d}", "session_number": str(i + 1),
                                 "kind": "progress", "created": "x"}, "note": n})
    h.ok("llm_json reads JSON inside code fences and chatter", ss.llm_json('Sure! ```json\n{"1": {"addressed": true}}\n```')["1"]["addressed"] is True)
    h.ok("llm_json handles braces inside strings", ss.llm_json('x {"a": "has } brace", "b": 2} y') == {"a": "has } brace", "b": 2})
    app.refresh_clients(); app.open_client(cid); h.pump(root, 1)
    alltext = h.texts(root)
    lb = [w for w in h.widgets(root) if type(w).__name__ == "NiceList" and any("Ask AI" in w.get(i) for i in range(w.size()))][0]
    h.ok("the patient's list has 'Progress & risk'", any("Progress & risk" in lb.get(i) for i in range(lb.size())))
    app.open_client(cid, select="insights:goals"); h.pump(root, 1)
    h.ok("Progress & risk ▸ Goals & homework shows the goals check", h.button(root, "Check 5 notes") is not None)
    h.shot("goals_before.png")
    h.RESP[:] = [("For EACH goal", answer)]
    n0 = len(h.PROMPTS)
    h.button(root, "Check 5 notes").invoke()
    h.pump(root, 30, lambda: not app.busy and all((d.get("goals_check") or {}).get("items") for _, d in V.sessions_for(cid)))
    h.pump(root, 1)
    sent = "\n".join(h.PROMPTS[n0:])
    h.ok("one AI step per note, names hidden", len(h.PROMPTS) - n0 == 5 and "Alex" not in sent and "[Patient]" in sent)
    sm = ss.goals_summary(V.load_client(cid), V.sessions_for(cid))
    h.ok("goal 1 worked on in 4 of 5 sessions", sum(1 for _, _, hits in sm["columns"] if hits[0]) == 4)
    h.ok("goal 3 flagged as not worked on lately", [g for g, n, _ in sm["stale"]] == ["Strengthen relationship with sister"])
    h.ok("goals 1 and 2 aren't flagged", len(sm["stale"]) == 1)
    alltext = h.texts(root)
    h.ok("the warning is on the page", "hasn't come up in the last 5 sessions" in alltext)
    h.ok("the button says everything is checked", h.button(root, "All notes checked") is not None)
    lb = [w for w in h.widgets(root) if type(w).__name__ == "NiceList" and any("Ask AI" in w.get(i) for i in range(w.size()))][0]
    h.ok("the list entry shows the warning", any("1 goal idle" in lb.get(i) for i in range(lb.size())))
    h.shot("goals_after.png")
    # plan changes -> notes need checking again
    c = V.load_client(cid); c["treatment_plans"].append({"version": 2, "text": PLAN + "Goal 4: Return to the gym\n",
                                                         "created": "2026-11-01T10:00:00"}); V.save_client(c)
    goals, todo = ss.goals_todo(V.load_client(cid), V.sessions_for(cid))
    h.ok("after the plan changes, notes are checked again", len(goals) == 4 and len(todo) == 5)
    # profile goals used when there's no plan
    c2 = V.save_client({"code": "CD2", "goals": "- Sleep better\n- Fewer panic attacks"})
    h.ok("without a plan, the profile's goals are used", ss.patient_goals(V.load_client(c2)) == ["Sleep better", "Fewer panic attacks"])


H.start(script)
