"""Each patient's Ask AI builds a memory from the conversations (saved with the patient), uses it in later
questions, and lets the clinician see, edit, remove, add, forget or turn it off. Made-up data only."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

MEM = {"reply": {"add": [], "remove": []}}


def mem_reply(user):
    return json.dumps(MEM["reply"])


def script(app, root, V, h):
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "MEM1", "names_to_hide": "Robin Example, Patient", "names_format": 2})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-01", "session_number": "1",
                             "kind": "progress", "created": "a"}, "note": "Patient discussed sleep.",
                    "transcript": "Clinician: How is sleep?\nPatient: Better."})
    app.refresh_clients(); app.open_client(cid, select="ask"); h.pump(root, 1)

    def ask(q, chat=None):
        n0 = len(h.PROMPTS)
        app._ask_send(V.load_client(cid), chat, q)
        h.pump(root, 25, until=lambda: len(h.PROMPTS) > n0 and not app.busy and not app.waitlist)
        h.pump(root, 0.6)
        return h.PROMPTS[n0:]

    MEM["reply"] = {"add": [{"text": "Mother reported [Patient] started a new medication in early November.",
                             "from": "clinician"}], "remove": []}
    sent = ask("Robin's mom told me he started a new medication last week — what should I watch for?")
    mem = V.load_client(cid).get("ai_memory") or []
    h.ok("after an answer, what's worth remembering is kept with the patient (marked as from you)",
         len(mem) == 1 and mem[0]["from"] == "clinician" and "new medication" in mem[0]["text"], mem)
    mp = next((p for p in sent if "LATEST EXCHANGE" in p), "")
    h.ok("…the memory step reads the exchange with names hidden", mp and "Robin" not in mp and "[Patient]" in mp,
         mp[-300:])
    h.ok("…and the 🧠 button counts it", "Remembers (1)" in str(app._ask_view["mem_btn"].cget("text")))

    chat = V.load_client(cid)["chats"][0]["id"]
    MEM["reply"] = {"add": [{"text": "Medication started early November; watch sleep changes.", "from": "answer"}],
                    "remove": [1]}
    sent = ask("What should I watch for next session?", chat)
    q = next((p for p in sent if "QUESTION" in p), "")
    h.ok("later questions include what was learned", "WHAT YOU'VE LEARNED IN EARLIER CONVERSATIONS" in q
         and "started a new medication in early November" in q, q[:400])
    mem = V.load_client(cid)["ai_memory"]
    h.ok("an out-of-date item is replaced; one from an answer is marked so",
         len(mem) == 1 and mem[0]["from"] == "answer" and "watch sleep" in mem[0]["text"], mem)

    # telling it something (not a question): kept at once, no record search, no AI
    ss = h.ss
    h.ok("statements are told apart from questions and requests",
         ss.ask_is_statement("He goes by Bobby.") and ss.ask_is_statement("Remember that his mom works nights")
         and ss.ask_is_statement("FYI: prefers afternoon sessions") and ss.ask_is_statement("His dog died last week.")
         and not ss.ask_is_statement("How is sleep going") and not ss.ask_is_statement("Summarize the last 3 sessions")
         and not ss.ask_is_statement("Does he have a nickname?")
         and not ss.ask_is_statement("Give me an overview of his progress"))
    n0, m0 = len(h.PROMPTS), len(h.MODELS)
    t0 = __import__("time").time()
    app._ask_send(V.load_client(cid), chat, "Remember that Robin goes by Bobby at school.")
    h.pump(root, 0.5)
    mem = V.load_client(cid)["ai_memory"]
    msgs = V.load_client(cid)["chats"][0]["messages"]
    h.ok("telling it something is remembered at once, as from you, without asking the AI",
         len(h.MODELS) == m0 and mem[-1]["text"] == "Robin goes by Bobby at school." and mem[-1]["from"] == "clinician"
         and __import__("time").time() - t0 < 3, mem[-1])
    h.ok("…and the conversation says so", msgs[-2]["text"].startswith("Remember that") and
         msgs[-1]["text"].startswith("Got it — I'll remember") and "Remembers (2)" in
         str(app._ask_view["mem_btn"].cget("text")), msgs[-1]["text"])

    # the clinician's view
    app.ask_memory_dialog(V.load_client(cid)); h.pump(root, 0.4)
    mw = app._memory_win
    import tkinter.simpledialog as sd
    sd.askstring = lambda *a, **k: "Prefers sessions after school."
    mw["add"](); h.pump(root, 0.2)
    mem = V.load_client(cid)["ai_memory"]
    h.ok("you can add something to remember", len(mem) == 3 and mem[-1]["from"] == "clinician")
    mw["tv"].selection_set(mem[0]["id"])
    sd.askstring = lambda *a, **k: "Medication started early November."
    mw["edit"](); h.pump(root, 0.2)
    mem = V.load_client(cid)["ai_memory"]
    h.ok("…edit one (it becomes yours)", mem[0]["text"] == "Medication started early November."
         and mem[0]["from"] == "clinician")
    mw["tv"].selection_set(mem[1]["id"]); mw["remove"](); h.pump(root, 0.2)
    h.ok("…remove one", len(V.load_client(cid)["ai_memory"]) == 2)
    mw["learn"].set(False); mw["save"](V.load_client(cid)["ai_memory"]); h.pump(root, 0.2)
    h.ok("…and turn learning off for this patient", V.load_client(cid).get("ai_memory_off") is True)
    mw["win"].destroy()
    MEM["reply"] = {"add": [{"text": "Should not be stored.", "from": "clinician"}], "remove": []}
    sent = ask("Anything else?", chat)
    h.ok("with learning off, nothing new is remembered", not any("LATEST EXCHANGE" in p for p in sent)
         and len(V.load_client(cid)["ai_memory"]) == 2)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script, responses=[("LATEST EXCHANGE", mem_reply), ("QUESTION", "Answer from the record.")])
