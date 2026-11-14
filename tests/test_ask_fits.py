"""Ask AI on a patient with a large record: what's sent always fits the model's context (even with
"Condense transcripts over" set very high), and an empty reply says why instead of saving nothing.
Made-up data only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

EMPTY = {"on": False}


def reply(user):
    return "" if EMPTY["on"] else "The patient has worked on sleep and school worry across sessions [2026-10-01]."


def script(app, root, V, h):
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    f = app._ask_send.__func__
    cfg = f.__closure__[f.__code__.co_freevars.index("cfg")].cell_contents
    h.ok("defaults are unchanged: a 16k model still takes 24,000-character parts",
         ss.ai_chunk_chars({"max_chars": 24000, "lm_context": 16384}) == 24000)
    h.ok("…but a setting larger than the model can read is capped",
         ss.ai_chunk_chars({"max_chars": 100000, "lm_context": 16384}) < 30000
         and ss.ai_prompt_chars({"lm_context": 16384}) < 16384 * 3)
    h.ok("…and a bigger context allows more", ss.ai_chunk_chars({"max_chars": 100000, "lm_context": 65536}) > 60000)

    cfg["max_chars"], cfg["lm_context"] = 100000, 16384
    cid = V.save_client({"code": "BIG1", "goals": "Sleep better; less worry about school."})
    long_note = ("Patient discussed sleep, school worry and family routines in detail. " * 60).strip()
    for i in range(40):
        V.save_session({"meta": {"client_id": cid, "session_date": f"2026-{8 + i // 28:02d}-{i % 28 + 1:02d}",
                                 "session_number": str(i + 1), "kind": "progress", "created": str(i)},
                        "note": long_note, "overview": long_note, "transcript": long_note * 3})
    app.refresh_clients(); app.open_client(cid, select="ask"); h.pump(root, 1)
    n0 = len(h.PROMPTS)
    app._ask_send(V.load_client(cid), None, "How has sleep changed over the sessions?")
    h.pump(root, 20, until=lambda: (V.load_client(cid).get("chats") or []) and not app.busy)
    h.pump(root, 0.5)
    sent = h.PROMPTS[n0] if len(h.PROMPTS) > n0 else ""
    limit = ss.ai_prompt_chars(cfg, 1800)
    h.ok("the question sent fits what the model can read (the record is far larger)",
         sent and len(sent) <= limit and "QUESTION" in sent, (len(sent), limit))
    chats = V.load_client(cid).get("chats") or []
    h.ok("…and the answer is saved", chats and chats[0]["messages"][-1]["text"].startswith("The patient has worked"))

    EMPTY["on"] = True
    app._ask_send(V.load_client(cid), chats[0]["id"], "And what about school?")
    h.pump(root, 20, until=lambda: not app.busy)
    h.pump(root, 0.5)
    msgs = V.load_client(cid)["chats"][0]["messages"]
    shown = app._ask_view["text"].get("1.0", "end")
    h.ok("an empty reply isn't saved as an answer…", len(msgs) == 2, len(msgs))
    h.ok("…it says what happened", "empty answer" in shown, shown[-300:])
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script, responses=[("QUESTION", reply)])
