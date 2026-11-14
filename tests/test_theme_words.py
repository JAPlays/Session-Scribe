"""'Words the patient uses most' counts only what the patient said: never a note's clinical wording
(psychotherapy, session, person-centered, patient's…); pasted transcripts without timestamps count too.
Made-up data only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

NOTE = ("Counselor provided person-centered psychotherapy. The patient's session focused on anxious feelings; "
        "patient reported worry and discussed coping. Psychotherapy session explored the patient's goals. ") * 3
TALK = ("Clinician: How was practice this week?\n"
        "Patient: Basketball practice was hard, I was anxious before the game.\n"
        "Clinician: What helped?\n"
        "Patient: Shooting free throws at practice calmed me. Basketball helps.\n")


def script(app, root, V, h):
    ss = h.ss
    cid = V.save_client({"code": "TW1"})
    for i in range(3):
        V.save_session({"meta": {"client_id": cid, "session_date": f"2026-10-0{i + 1}", "session_number": str(i + 1),
                                 "kind": "progress", "created": str(i)}, "note": NOTE})
    for i in range(3):
        V.save_session({"meta": {"client_id": cid, "session_date": f"2026-10-1{i + 1}", "session_number": str(i + 4),
                                 "kind": "progress", "created": str(i + 4)}, "note": NOTE, "transcript": TALK})
    th = ss.session_themes(V.load_client(cid), [(sid, d) for sid, d in V.sessions_for(cid)], "Clinician",
                           patient_label="Patient")
    words = [w.lower() for w, _, _ in th["words"]]
    h.ok("the patient's own words are listed", "basketball" in words and "practice" in words, words)
    bad = [w for w in words if w in ("psychotherapy", "session", "person-centered", "patient", "patient's",
                                     "counselor", "provided", "reported", "discussed", "explored", "focused")]
    h.ok("…and none of the note's wording", not bad, bad)
    h.ok("…not even the counselor's questions", "helped" not in words, words)
    themes = {t: total for t, _, total in th["themes"]}
    h.ok("themes still count sessions with only a note", themes.get("Anxiety & worry", 0) >= 6, themes)
    h.ok("a pasted transcript without timestamps is split by speaker",
         ss.patient_speech({"transcript": TALK, "note": "x"}, "Clinician", "Patient", note_ok=False).startswith(
             "Basketball practice was hard"))


H.start(script)
