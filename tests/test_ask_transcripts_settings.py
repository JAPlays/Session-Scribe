"""Ask AI reads the session transcripts too (not only the notes), and Settings keeps the technical options
(Hugging Face token, speech model, AI connection…) behind "Advanced settings". Made-up data only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "TR2", "goals": "Less worry"})
    note = ("Counselor and patient reviewed the week, worry about school, and coping skills; patient practiced "
            "breathing and agreed to keep a log. " * 25).strip()
    for i in range(30):
        day = f"2026-{9 + i // 28:02d}-{i % 28 + 1:02d}"
        talk = ("Clinician: How was the week?\nPatient: It was fine, school was a lot.\n" * 30)
        if i == 3:
            talk += "Patient: My grandfather gave me his purple bicycle and riding it calms me down.\n"
        V.save_session({"meta": {"client_id": cid, "session_date": day, "session_number": str(i + 1),
                                 "kind": "progress", "created": str(i)},
                        "note": note, "overview": "", "transcript": talk})
    app.refresh_clients(); app.open_client(cid, select="ask"); h.pump(root, 1)

    def ask(q):
        n0 = len(h.PROMPTS)
        before = len((V.load_client(cid).get("chats") or [{}])[0].get("messages", [])) if \
            V.load_client(cid).get("chats") else 0
        app._ask_send(V.load_client(cid), None, q)
        h.pump(root, 20, until=lambda: len(h.PROMPTS) > n0 and not app.busy)
        h.pump(root, 0.4)
        return h.PROMPTS[n0] if len(h.PROMPTS) > n0 else ""
    p = ask("How is the patient doing overall?")
    h.ok("a general question includes transcript excerpts as well as notes",
         "Transcript" in p and "Patient: It was fine" in p and "reviewed the week" in p, p[:200])
    p = ask("What did the patient say about the bicycle?")
    h.ok("a question about what was said finds it in an older transcript", "purple bicycle" in p)
    chats = V.load_client(cid).get("chats") or []
    h.ok("…and the answer lists transcript excerpts among its sources",
         any("transcript excerpt" in s for c in chats for m in c["messages"] for s in m.get("sources") or []))

    # Settings
    app.open_setup(); h.pump(root, 1.2)
    win = app.setup_win["win"]
    adv = next(w for w in h.widgets(win, "TLabelframe") if str(w.cget("text")) == "Advanced settings")
    token = next(w for w in h.widgets(win, "TEntry") if str(w.cget("show")) == "•")
    h.ok("Settings opens with Advanced settings closed", not adv.winfo_ismapped())
    h.ok("…the Hugging Face token, speech model and AI connection are inside it",
         str(token.winfo_parent()).startswith(str(adv)) and
         any("Speech model" in str(l.cget("text")) for l in h.widgets(adv, "TLabel")) and
         any(k in h.texts(adv) for k in ("LM Studio address", "AI model (built in)")) and
         "Condense transcripts over" in h.texts(adv), token.winfo_parent())
    h.ok("…while everyday ones stay outside it", "Theoretical orientation" in h.texts(win)
         and "Theoretical orientation" not in h.texts(adv) and "Date format" not in h.texts(adv))
    h.button(win, "Advanced settings").invoke(); h.pump(root, 0.4)
    h.ok("the Advanced settings button opens it", adv.winfo_ismapped())
    token.delete(0, "end"); token.insert(0, "hf_test_token_123")
    h.button(win, "Save preferences").invoke(); h.pump(root, 0.6)
    h.ok("a token entered there is still saved", app.settings.get("hf_token") == "hf_test_token_123")
    h.button(win, "Advanced settings").invoke(); h.pump(root, 0.3)
    h.ok("…and the button closes it again", not adv.winfo_ismapped())
    win.withdraw()
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
