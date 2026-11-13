"""Features 1, 3, 4: the Session review tab — session map, skills feedback, techniques used."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H, sample_lines

FEEDBACK = ("WHAT WENT WELL\n[00:21] \"That sounds really painful, being embarrassed in front of everyone.\" — named the feeling.\n\n"
            "MISSED OPPORTUNITIES\n[00:36] Patient: \"I don't know. Anyway...\" — moved on; could reflect the shame.\n\n"
            "WHERE THE PATIENT PULLED BACK\n[00:36] \"Anyway, the weekend was fine I guess.\" (hypothesis: shame)\n\n"
            "ONE THING TO PRACTICE NEXT SESSION\nStay with the feeling one beat longer.")
TECH = ("- Reflection of feeling — [00:21] \"That sounds really painful\" — named shame\n"
        "- Open-ended questions — [00:43] \"What did you do this weekend?\" — invited detail\n\n"
        "For the note: Person centered therapy was used to explore work stress; Counselor utilized reflection of feeling.")


def script(app, root, V, h):
    ss = h.ss
    cid = V.save_client({"code": "AB1", "names_to_hide": "Alex Rivera, Patient; Maria, Sister", "names_format": 2,
                         "goals": "Reduce anxiety"})
    lines = sample_lines(24)
    lines[7]["text"] = "I went hiking with Maria and it felt good, I was calm for once and we laughed a lot."
    roles = {"SPEAKER_00": "Clinician", "SPEAKER_01": "Patient"}
    sid = V.save_session({"meta": {"client_id": cid, "client_code": "AB1", "session_date": "2026-11-10",
                                   "session_number": "3", "kind": "progress", "created": "2026-11-10T10:00:00",
                                   "updated": "2026-11-10T10:00:00"},
                          "transcript": ss.render_transcript(lines, roles), "note": "Progress note text.",
                          "overview": "Overview text.", "lines": lines, "roles": roles, "diarized": True, "aligned": True})
    # --- the map, computed locally
    m = ss.session_map(lines, roles, "Clinician")
    print("   map:", ss.session_map_text(m).replace("\n", " | ")[:400])
    h.ok("map: talk time split between the two speakers adds to 100%", sum(s["pct"] for s in m["speakers"]) in (99, 100, 101))
    h.ok("map: counselor questions counted, open vs closed", m["questions"]["open"] > 0 and m["questions"]["closed"] > 0)
    h.ok("map: heavy moments found with quotes", m["heavy"] and any("ashamed" in x["quote"] or "anxious" in x["quote"] for x in m["heavy"]))
    h.ok("map: tone over time has points", len(m["tone"]) >= 8)
    h.ok("question kinds", ss.question_kind("How did that feel?") == "open" and ss.question_kind("Did you sleep?") == "closed"
         and ss.question_kind("So what happened next?") == "open")
    app.refresh_clients(); app.open_client(cid); h.pump(root, 1)
    nb = h.notebooks(root)[0]
    h.ok("the day page has a 'Session review' tab", h.select_tab(nb, "Session review"))
    h.pump(root, 1)
    h.ok("review tab shows the map and the button", h.button(root, "Write feedback & find techniques") is not None
         and any(w.winfo_class() == "Canvas" for w in h.widgets(root)))
    h.shot("review_empty.png")
    h.RESP[:] = [("supervisor reviewing", FEEDBACK), ("Identify the therapeutic techniques", TECH)]
    n0 = len(h.PROMPTS)
    h.button(root, "Write feedback & find techniques").invoke()
    h.pump(root, 30, lambda: not app.busy and (V.load_session(sid).get("review") or {}).get("techniques"))
    h.pump(root, 1)
    rv = V.load_session(sid).get("review") or {}
    sent = "\n".join(h.PROMPTS[n0:])
    h.ok("feedback and techniques saved with the session", rv.get("feedback") == FEEDBACK and rv.get("techniques") == TECH)
    h.ok("the AI never saw the patient's or sister's names", "Alex" not in sent and "Maria" not in sent and "[Sister]" in sent)
    h.ok("feedback prompt carries the measured numbers and timestamps", "Talk time" in sent and "open" in sent and "[00:" in sent)
    h.ok("techniques prompt uses SSC's vocabulary", "Reflection of meaning" in sent and "Person-centered therapy" in sent)
    h.ok("the note itself is unchanged", V.load_session(sid)["note"] == "Progress note text.")
    nb = h.notebooks(root)[0]
    sel = nb.tab(nb.select(), "text")
    h.ok("the page reopens on the Session review tab", "Session review" in sel, sel)
    alltext = h.texts(root)
    h.ok("feedback and techniques are shown", "WHAT WENT WELL" in alltext and "For the note:" in alltext)
    h.ok("search and export aren't affected (review isn't a chart document)",
         [l for l, _ in ss.record_docs(V.load_session(sid))] == ["Progress note", "Overview", "Transcript"])
    h.shot("review_done.png")
    # long session: read in parts
    big = sample_lines(80)
    sid2 = V.save_session({"meta": {"client_id": cid, "client_code": "AB1", "session_date": "2026-11-11", "session_number": "4",
                                    "kind": "progress", "created": "x", "updated": "x"},
                           "transcript": ss.render_transcript(big, roles), "note": "n", "lines": big, "roles": roles})
    n1 = len(h.PROMPTS)
    app.make_review(V.load_client(cid), sid2)
    h.pump(root, 40, lambda: not app.busy and (V.load_session(sid2).get("review") or {}).get("techniques"))
    rv2 = V.load_session(sid2).get("review") or {}
    h.ok("a long session is read in parts, each labelled", "Part 1 of" in rv2.get("feedback", "") and len(h.PROMPTS) - n1 >= 4,
         f"{len(h.PROMPTS) - n1} prompts")


H.start(script)
