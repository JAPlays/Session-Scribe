"""Feature 19: recurring themes per patient."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def mk(texts):
    out, t = [], 0.0
    for i, (spk, tx) in enumerate(texts):
        out.append({"speaker": spk, "start": t, "end": t + 5, "text": tx}); t += 6
    return out


def script(app, root, V, h):
    ss = h.ss
    cid = V.save_client({"code": "AB1", "names_to_hide": "Alex Rivera, Patient; Maria, Sister", "names_format": 2})
    roles = {"SPEAKER_00": "Clinician", "SPEAKER_01": "Patient"}
    sess = [
        [("SPEAKER_00", "How was work and your sleep this week?"), ("SPEAKER_01", "Work was awful, my manager yelled and I couldn't sleep. Maria called. Anxious about work all week.")],
        [("SPEAKER_00", "Tell me about your mother."), ("SPEAKER_01", "My mother and my sister Maria argue about my dad. Family stuff, the family is exhausting. I couldn't sleep again.")],
        [("SPEAKER_00", "What about work?"), ("SPEAKER_01", "Work is better, the manager apologized. I slept better, I went on a walk and journaling helped.")],
    ]
    for i, tx in enumerate(sess):
        lines = mk(tx)
        V.save_session({"meta": {"client_id": cid, "session_date": f"2026-10-{10 + i * 7}", "session_number": str(i + 1),
                                 "kind": "progress", "created": "x"}, "lines": lines, "roles": roles,
                        "transcript": ss.render_transcript(lines, roles), "note": "n"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-12", "kind": "parent", "created": "x"},
                    "note": "Parent called about work work work."})
    th = ss.session_themes(V.load_client(cid), V.sessions_for(cid), "Clinician")
    names = [t for t, _, _ in th["themes"]]
    print("   themes:", [(t, r) for t, r, _ in th["themes"]][:6]); print("   words:", th["words"][:8])
    h.ok("three sessions counted (contacts left out)", len(th["columns"]) == 3)
    h.ok("work, sleep and family found as themes", {"Work", "Sleep", "Family"} <= set(names))
    work = next(r for t, r, _ in th["themes"] if t == "Work")
    h.ok("work shows up in sessions 1 and 3, not 2", work[0] > 0 and work[1] == 0 and work[2] > 0)
    h.ok("the counselor's own words aren't counted", not any(w == "tell" for w, _, _ in th["words"]))
    h.ok("names on the hide list are left out", not any(w.lower() in ("maria", "alex") for w, _, _ in th["words"]))
    h.ok("recurring words across sessions are listed", any(w.startswith("work") for w, _, _ in th["words"])
         and any(w.startswith("manager") for w, _, _ in th["words"]))
    app.refresh_clients(); app.open_client(cid, select="insights:themes"); h.pump(root, 1)
    h.ok("the Themes tab shows the grids", "Most often: work" in h.texts(root) and len(h.widgets(root, "Canvas")) >= 2)
    h.shot("themes_tab.png")


H.start(script)
