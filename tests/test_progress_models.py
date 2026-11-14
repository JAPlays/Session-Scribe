"""Progress estimates follow the model actually doing the work (main or fast), count a reasoning model's
thinking, and don't learn a model-loading pause as its reading speed."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    f = app._ask_send.__func__
    cfg = f.__closure__[f.__code__.co_freevars.index("cfg")].cell_contents
    for _ in range(4):
        ss.record_llm("slow-main", 20000, 20000 / 850, 3000, 3000 / 20, 0)
        ss.record_llm("fast-mock", 20000, 20000 / 3300, 3000, 3000 / 95, 900)
    ss.record_llm("slow-main", 3000, 1, 300, 15, 0)            # the main model ran last
    main, fast = ss.timing_llm("slow-main"), ss.timing_llm("fast-mock")
    h.ok("each model's own speed is learned", 15 < main["gen"] < 25 and 80 < fast["gen"] < 110
         and 3000 < fast["prefill"] < 3600, (main, fast))
    h.ok("…and how much a reasoning model thinks first (none for the other)", 800 < fast["think"] < 1000
         and main["think"] < 1, (main.get("think"), fast.get("think")))

    # a step planned before it starts is re-planned with the model that runs it
    st = ss._Steps(lambda *a: None, [("Answering", 1400, 20000)])
    planned = st.secs[0]                                       # (planned with the last model: the slow one)
    cfg["fast_model"] = "fast-mock"
    st.run(0, app.fast_cfg(), "x" * 20000)
    h.ok("a fast-model step is planned with the fast model's speed (and its thinking)",
         st.model == "fast-mock" and st.rates["gen"] == fast["gen"]
         and abs(st.secs[0] - (20000 / fast["prefill"] + (1400 + fast["think"]) / fast["gen"])) < 0.5
         and st.secs[0] < planned / 2, (planned, st.secs[0]))
    cfg["fast_model"] = ""

    # LM Studio loading the model while answering: not learned as slow reading
    before = ss.timing_llm("fast-mock")["prefill"]
    ss.record_llm("fast-mock", 20000, 90, 3000, 3000 / 95, 900)          # 90 s before the first word
    h.ok("a start slowed by loading the model doesn't change the learned reading speed",
         ss.timing_llm("fast-mock")["prefill"] == before)

    # notes (queue estimates) use the main model's speed
    e_main = ss.estimate_write_secs("progress", 40000, model="slow-main")
    e_fast = ss.estimate_write_secs("progress", 40000, model="fast-mock")
    h.ok("note-writing estimates use the main model", e_main > 3 * e_fast, (e_main, e_fast))


H.start(script)
