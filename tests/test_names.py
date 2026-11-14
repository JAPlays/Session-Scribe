"""Patient names for the AI: on by default (the AI is local), hidden for one patient or for everyone,
and always hidden where it matters regardless (supervision packet, preferences learned from edits)."""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H  # noqa: E402


def script(app, root, V, h):
    ss = h.ss
    h.ok("names go to the AI by default", ss.DEFAULT_CONFIG["ai_names"] is True and "ai_names" in ss.SHARED_KEYS)
    ss.set_ai_names(True)                                # (the harness starts with names hidden)
    today = date.today()

    def patient(code, **extra):
        cid = V.save_client(dict({"code": code, "names_to_hide": "Alex Rivera, Patient\nJordan, Sister",
                                  "names_format": 2}, **extra))
        for i in range(2):
            V.save_session({"meta": {"client_id": cid, "session_date": (today - timedelta(days=14 - i * 7)).isoformat(),
                                     "session_number": str(i + 1), "kind": "progress", "created": "x"},
                            "note": f"Session {i + 1}: Alex Rivera talked about Jordan and work stress."})
        return cid

    def brief_prompt(cid):
        n0 = len(h.PROMPTS)
        app.prep_brief(V.load_client(cid))
        h.pump(root, 20, lambda: not app.busy and len(h.PROMPTS) > n0)
        h.pump(root, 1)
        return "\n".join(h.PROMPTS[n0:])

    h.RESP[:] = [("PRE-SESSION BRIEF", "LAST TIME\n- Work stress.")]
    open_cid = patient("NM1")
    sent = brief_prompt(open_cid)
    # ([Patient] appears in the AI's standing instructions as an example; [Sister] only if hiding ran)
    h.ok("the AI reads the patient's and family's real names", "Alex Rivera" in sent and "Jordan" in sent
         and "[Sister]" not in sent, sent[-400:])
    h.ok("…and is told to use names (or placeholders, where a name is hidden)",
         "by the names used in the material" in ss.SYSTEM_PROMPT and "[Patient]" in ss.SYSTEM_PROMPT)

    hidden_cid = patient("NM2", hide_names=True)
    sent = brief_prompt(hidden_cid)
    h.ok("a patient set to keep names hidden: placeholders only", "Alex" not in sent and "Jordan" not in sent
         and "[Sister]" in sent, sent[-400:])

    c = V.load_client(open_cid)
    sessions = V.sessions_for(open_cid) if hasattr(V, "sessions_for") else [
        (sid, V.load_session(sid)) for sid in [s["id"] for s in V.list_sessions() if s.get("client_id") == open_cid]]
    docs = ss.supervision_inputs(c, sessions, "2000-01-01", "2100-01-01")
    h.ok("the supervision packet always hides names (it's shared with your supervisor)",
         docs and all("Alex" not in d and "[Patient]" in d for d in docs), docs[:1])
    settings = {}
    ss.record_edit(settings, c, "progress", "note", "Alex Rivera felt sad about Jordan.",
                   "Alex Rivera reported sadness about her sister Jordan.")
    pair = (settings.get("edit_pairs") or [{}])[-1]
    h.ok("what's learned from your edits (used for every patient) never keeps names",
         pair and "Alex" not in pair["draft"] + pair["final"] and "Jordan" not in pair["final"], pair)

    cfg = ss.load_config()
    cfg["ai_names"] = False
    ss.save_config(cfg)
    h.ok("Settings off: hidden for everyone", not ss.names_to_ai(c) and ss.client_hider(c)("Alex Rivera") == "[Patient]")
    h.ok("…and that's shared with your other computer (data folder settings)",
         ss._read_json(os.path.join(cfg["data_dir"], ss.SHARED_SETTINGS)).get("ai_names") is False)
    cfg["ai_names"] = True
    ss.save_config(cfg)
    h.ok("on again: names as they are", ss.client_hider(c)("Alex Rivera") == "Alex Rivera")

    import tkinter as tk
    seen = {}

    def look():                                        # the patient form is modal: inspect it, then close it
        boxes = [w for w in h.widgets(root, "TCheckbutton") if "Keep names hidden" in str(w.cget("text"))]
        seen["box"] = bool(boxes) and "selected" in boxes[0].state()
        for w in root.winfo_children():
            if isinstance(w, tk.Toplevel):
                w.destroy()
    root.after(1500, look)
    app.edit_client(V.load_client(hidden_cid))
    h.ok("the patient form has the per-patient switch, ticked for this patient", seen.get("box"), seen)

H.start(script)
