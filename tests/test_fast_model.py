"""A fast model for Ask AI and the background checks (notes and case overviews stay with the main model), a
fallback to the main model when the fast one can't load, and Ask AI's record kept the same from question to
question (so LM Studio can reuse its reading). Made-up data only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    f = app._ask_send.__func__
    cfg = f.__closure__[f.__code__.co_freevars.index("cfg")].cell_contents
    h.ok("no fast model set: everything uses the main model", app.fast_cfg().get("model") == "mock-model"
         and "_fast_from" not in app.fast_cfg())

    cid = V.save_client({"code": "FM1", "goals": "Sleep better"})
    for i in range(6):
        V.save_session({"meta": {"client_id": cid, "session_date": f"2026-10-{i + 1:02d}", "session_number": str(i + 1),
                                 "kind": "progress", "created": str(i)},
                        "note": "Patient discussed sleep and school. " * 20,
                        "transcript": "Clinician: How is sleep?\nPatient: Better on weekends.\n" * 20})
    c = V.load_client(cid)
    c["homework"] = [{"id": "h1", "title": "Sleep diary", "status": "assigned", "date": "2026-10-03"}]
    V.save_client(c)
    app.refresh_clients(); app.open_client(cid, select="ask"); h.pump(root, 1)

    def ask(q, chat=None):
        n0, m0 = len(h.PROMPTS), len(h.MODELS)
        app._ask_send(V.load_client(cid), chat, q)
        h.pump(root, 20, until=lambda: len(h.PROMPTS) > n0 and not app.busy)
        h.pump(root, 0.4)
        return (h.PROMPTS[n0] if len(h.PROMPTS) > n0 else ""), h.MODELS[m0:]

    cfg["fast_model"] = "fast-mock"
    p1, models = ask("How is sleep going?")
    h.ok("with a fast model set, Ask AI uses it", models == ["fast-mock"], models)
    chat = V.load_client(cid)["chats"][0]["id"]
    p2, _ = ask("And what about school?", chat)
    stable = p1.index("SESSION INDEX")
    common = next((i for i, (a, b) in enumerate(zip(p1, p2)) if a != b), min(len(p1), len(p2)))
    h.ok("the record's opening part is identical between questions (LM Studio can reuse it)",
         common > stable and "PROGRESS & RISK" in p1[:common], (common, stable))

    m0 = len(h.MODELS)
    app.make_case_overview(V.load_client(cid))
    h.pump(root, 20, until=lambda: len(h.MODELS) > m0 and not app.busy)
    h.pump(root, 0.5)
    h.ok("the case overview stays with the main model", h.MODELS[m0:] and set(h.MODELS[m0:]) == {"mock-model"},
         h.MODELS[m0:])
    h.ok("the background checks use the fast model", app.fast_cfg()["model"] == "fast-mock")

    # the fast model can't be loaded: the main one answers instead
    cfg["fast_model"] = "broken-fast"
    h.FAIL_MODELS.add("broken-fast")
    p3, models = ask("Any homework open?", chat)
    msgs = V.load_client(cid)["chats"][0]["messages"]
    h.ok("if the fast model can't load, the main model answers (no error)",
         models == ["broken-fast", "mock-model"] and msgs[-1]["role"] == "ai" and len(msgs) == 6, (models, len(msgs)))
    cfg["fast_model"] = ""
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
