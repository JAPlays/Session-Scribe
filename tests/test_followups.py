"""Feature 23: follow-up checklist."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

CONTACT = ("Counselor received an email from patient's mother about school refusal...\n\nFollow-up:\n"
           "- Counselor to send the school a release form\n- Mother to call the school counselor\n")


def found(prompt):
    assert "Alex" not in prompt
    return json.dumps({"items": [{"who": "counselor", "text": "Send [Patient] the sleep hygiene handout"},
                                 {"who": "patient", "text": "Try a walk after work three times"}]})


def script(app, root, V, h):
    ss = h.ss
    h.ok("Follow-up lists are read from contact notes", ss.followups_from_note(CONTACT) ==
         ["Counselor to send the school a release form", "Mother to call the school counselor"])
    h.ok("'None discussed' isn't a follow-up", ss.followups_from_note("x\n\nFollow-up:\n- None discussed\n") == [])
    cid = V.save_client({"code": "AB1", "names_to_hide": "Alex Rivera, Patient", "names_format": 2})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-05", "kind": "parent", "created": "x",
                             "contact_with": "parent"}, "note": CONTACT})
    sid = V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-10", "session_number": "3", "kind": "progress",
                                   "created": "x"}, "note": "Alex Rivera discussed sleep.",
                          "transcript": "[10:00] Clinician: I'll send you the sleep hygiene handout tonight.\n"
                                        "[10:05] Patient: Okay. I'll try a walk after work.\n[10:09] Clinician: How is your mom?"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-12", "session_number": "4", "kind": "progress",
                             "created": "x"}, "note": "Explored hobbies.", "transcript": "[01:00] Patient: I painted."})
    app.refresh_clients(); app.open_client(cid, select="insights:followups"); h.pump(root, 1)
    c = V.load_client(cid)
    h.ok("the contact note's follow-ups are on the checklist by themselves", sorted(x["text"] for x in c.get("followups", [])) ==
         ["Counselor to send the school a release form", "Mother to call the school counselor"])
    h.ok("only the session with commitments needs the AI", h.button(root, "Find follow-ups in 1 session") is not None)
    h.RESP[:] = [("FOLLOW-UPS that were agreed", found)]
    n0 = len(h.PROMPTS)
    h.button(root, "Find follow-ups in 1 session").invoke()
    h.pump(root, 20, lambda: not app.busy and len(V.load_client(cid).get("followups", [])) == 4); h.pump(root, 1)
    sent = "\n".join(h.PROMPTS[n0:])
    h.ok("the AI got the note and only the commitment lines (names hidden)", "I'll send you the sleep hygiene handout" in sent
         and "How is your mom" not in sent and "Alex" not in sent)
    texts = [x["text"] for x in V.load_client(cid)["followups"]]
    h.ok("found items added, marked by who", "Send [Patient] the sleep hygiene handout" in texts and
         "Patient: Try a walk after work three times" in texts)
    h.ok("scanned sessions aren't read again", h.button(root, "All sessions checked") is not None)
    lb = [w for w in h.widgets(root) if type(w).__name__ == "NiceList" and any("Ask AI" in w.get(i) for i in range(w.size()))][0]
    h.ok("the patient's list shows how many are open", any("4 to do" in lb.get(i) for i in range(lb.size())))
    h.shot("followups_tab.png")
    fid = V.load_client(cid)["followups"][0]["id"]
    app.set_followup(V.load_client(cid), fid, True); h.pump(root, 1)
    x = next(x for x in V.load_client(cid)["followups"] if x["id"] == fid)
    h.ok("ticking one marks it done with the date", x["done"] and x["done_at"])
    h.ok("re-reading the contact note doesn't add it twice", not ss.followups_sync(V.load_client(cid), V.sessions_for(cid)))
    h.ok("open follow-ups across patients", len(ss.open_followups([V.load_client(cid)])) == 3)


H.start(script)
