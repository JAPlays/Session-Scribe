"""Assessment library: scoring of each instrument against its published rules, separate child/adolescent/adult
lists, and the full flow — picker, full-screen patient run, password hand-back, results, trend, safety assessment."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    S = ss.score_assessment
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    A = lambda **kw: {str(k).lstrip("_"): v for k, v in kw.items()}

    # ---- scoring (published rules)
    phq = {str(i): v for i, v in enumerate([2, 2, 1, 1, 1, 2, 1, 1, 1], 1)}
    r = S("phq9", phq)
    h.ok("PHQ-9: total and band", r["total"] == 12 and r["band"] == "moderate", r)
    h.ok("PHQ-9: item 9 above 0 is flagged as risk", r["flags"] and r["risk"])
    h.ok("PHQ-9: no flag when item 9 is 0", not S("phq9", dict(phq, **{"9": 0}))["flags"])
    d = {str(i): 0 for i in range(1, 22)}
    for i in (3, 5, 10):
        d[str(i)] = 3                                   # depression items
    r = S("dass21", d)
    h.ok("DASS-21: subscale ×2 and its band", r["values"]["Depression"] == 18 and "moderate" in r["summary"], r)
    r = S("k10", {"1": 1, "2": 1, "4": 1, "5": 1, "7": 1, "8": 1, "9": 1, "10": 1})
    h.ok("K10: questions skipped after 'None of the time' count as 1", r["total"] == 10 and r["values"]["K6"] == 0, r)
    r = S("who5", {str(i): 2 for i in range(1, 6)})
    h.ok("WHO-5: ×4 percentage; below 13 = poor well-being", r["total"] == 40 and r["band"] == "poor well-being")
    pcl = {str(i): 0 for i in range(1, 21)}
    pcl.update({"1": 2, "6": 2, "8": 2, "9": 2, "15": 2, "16": 2})
    r = S("pcl5", pcl)
    h.ok("PCL-5: DSM-5 pattern met (1B 1C 2D 2E at 2+) though total under 31", "pattern" in " ".join(r["lines"])
         and "met." in " ".join(r["lines"]) and r["total"] == 12, r["lines"])
    h.ok("PCL-5: 31+ is probable PTSD", S("pcl5", {str(i): 2 for i in range(1, 21)})["band"] == "probable PTSD")
    h.ok("PC-PTSD-5: no exposure = 0", S("pcptsd5", {"exp": 0})["total"] == 0)
    h.ok("PC-PTSD-5: 4 of 5 yes = positive", S("pcptsd5", {"exp": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 0})["band"]
         == "positive")
    inst = ss.ASSESS_INSTRUMENTS["audit"]
    h.ok("AUDIT: a non-drinker skips to questions 9 and 10",
         [q["id"] for q in ss.assessment_visible(inst, {"1": 0})] == ["1", "9", "10"])
    r = S("audit", {"1": 4, "2": 2, "3": 2, "4": 1, "5": 1, "6": 0, "7": 1, "8": 1, "9": 0, "10": 4})
    h.ok("AUDIT: total, WHO zone and AUDIT-C", r["total"] == 16 and "Zone III" in r["summary"]
         and r["values"]["AUDIT-C"] == 8, r)
    asrs = {str(i): 0 for i in range(1, 19)}
    asrs.update({"1": 2, "2": 2, "3": 2, "4": 2, "5": 3, "6": 3})        # item 4 needs Often → not counted
    r = S("asrs", asrs)
    h.ok("ASRS: Part A uses the shaded thresholds (items 1–3 from Sometimes, 4–6 from Often)",
         r["values"]["Part A"] == 5 and "positive" in r["summary"], r)
    asrs["4"] = 2; asrs["5"] = 2; asrs["6"] = 2
    h.ok("…and 3 in range is negative", S("asrs", asrs)["values"]["Part A"] == 3)
    h.ok("GDS-15: reverse-keyed items", S("gds15", {"1": 0, "5": 0, "7": 0, "11": 0, "13": 0, "2": 1})["total"] == 6)
    bam = {"1": 22, "2": 5, "3": 4, "4": 3, "5": 2, "6": 0, "8": 15, "9": 22, "10": 4, "11": 2, "12": 8, "13": 20,
           "14": 30, "15": 8, "16": 10, "17": 15}
    r = S("bam", bam)
    h.ok("BAM: Use 4+5+6, Risk 1,2,3,8,11,15, Protective 9,10,12,13,14,16 (17 not scored)",
         r["values"] == {"Use": 5, "Risk": 56, "Protective": 94}, r["values"])
    h.ok("ASQ: all no = negative", S("asq", {"1": 0, "2": 0, "3": 0, "4": 0})["band"] == "negative")
    r = S("asq", {"1": 1, "2": 0, "3": 0, "4": 0, "5": 0})
    h.ok("ASQ: yes to 1–4, no to 5 = non-acute positive", r["band"] == "non-acute positive" and r["risk"])
    h.ok("ASQ: yes to 5 = acute positive", S("asq", {"1": 1, "2": 1, "3": 1, "4": 0, "5": 1})["band"] == "acute positive")
    h.ok("ASQ: question 5 only appears after a yes",
         "5" not in [q["id"] for q in ss.assessment_visible(ss.ASSESS_INSTRUMENTS["asq"], {"1": 0, "2": 0, "3": 0, "4": 0})])
    h.ok("C-SSRS: 1 or 2 only = low", S("cssrs", {"1": 1, "2": 0, "6": 0})["band"] == "low risk")
    h.ok("C-SSRS: 3 = moderate", S("cssrs", {"1": 1, "2": 1, "3": 1, "4": 0, "5": 0, "6": 0})["band"] == "moderate risk")
    h.ok("C-SSRS: 4/5 or behavior in past 3 months = high",
         S("cssrs", {"1": 1, "2": 1, "3": 0, "4": 1, "5": 0, "6": 0})["band"] == "high risk"
         and S("cssrs", {"1": 0, "2": 0, "6": 1, "6a": 1})["band"] == "high risk"
         and S("cssrs", {"1": 0, "2": 0, "6": 1, "6a": 0})["band"] == "moderate risk")
    h.ok("C-SSRS: 3–5 are only asked after a yes to 2",
         [q["id"] for q in ss.assessment_visible(ss.ASSESS_INSTRUMENTS["cssrs"], {"1": 1, "2": 0})] == ["1", "2", "6"])
    crafft = ss.ASSESS_INSTRUMENTS["crafft"]
    h.ok("CRAFFT: no use → only the CAR question after part A",
         [q["id"] for q in ss.assessment_visible(crafft, {"1": 0, "2": 0, "3": 0})] == ["1", "2", "3", "4"])
    r = S("crafft", {"1": 5, "2": 0, "3": 0, "4": 1, "5": 1, "6": 0, "7": 0, "8": 0, "9": 0})
    h.ok("CRAFFT: score counts yes in 4–9; 2+ positive; CAR flagged", r["total"] == 2 and r["band"] == "positive"
         and r["flags"], r)
    h.ok("S2BI: monthly use = higher risk", S("s2bi", {"1": 0, "2": 2, "3": 1, "4": 0, "5": 0, "6": 0, "7": 0})["band"]
         .startswith("Higher risk"))
    sc = {str(i): 0 for i in range(1, 42)}
    for i in (3, 10, 26, 32):
        sc[str(i)] = 2
    r = S("scared", sc)
    h.ok("SCARED: social subscale 8 is flagged", r["values"]["Social anxiety"] == 8
         and any("social anxiety disorder" in x for x in r["lines"]), r["lines"])
    y = {str(i): 0 for i in range(1, 18)}
    for i in (2, 6, 9):
        y[str(i)] = 2
    r = S("ypsc17", y)
    h.ok("Y-PSC-17: youth item order maps to the right subscales", r["values"]["Internalizing"] == 6
         and r["values"]["Attention"] == 0, r["values"])
    h.ok("SMFQ: 8+ = possible depression", S("smfq", {str(i): 1 if i <= 8 else 0 for i in range(1, 14)})["band"]
         == "possible depression")

    # ---- separate lists
    adult_only = {i for i, x in ss.ASSESS_INSTRUMENTS.items() if x["groups"] == ("adult",)}
    child = {i for i, x in ss.ASSESS_INSTRUMENTS.items() if "child" in x["groups"]}
    h.ok("the child list has no adult-only measures", not (adult_only & child) and "phq9" not in child)
    h.ok("PHQ-A is adolescent only; PHQ-9 adult only", ss.ASSESS_INSTRUMENTS["phqa"]["groups"] == ("adolescent",)
         and ss.ASSESS_INSTRUMENTS["phq9"]["groups"] == ("adult",))

    # ---- the full flow
    cid = V.save_client({"code": "AS1"})
    V.save_session({"meta": {"client_id": cid, "session_date": ss.date.today().isoformat(), "session_number": "4",
                             "kind": "progress", "created": "x"}, "note": "n"})
    app.refresh_clients(); app.open_client(cid, select="assess"); h.pump(root, 1)
    h.ok("the Assessments tab has Take assessment", h.button(app.page, "Take assessment") is not None)
    h.button(app.page, "Take assessment").invoke(); h.pump(root, 0.5)
    tw = app._take_win
    tw["group"].set("child"); h.pump(root, 0.2)
    kids = [c for p in tw["tv"].get_children() for c in tw["tv"].get_children(p)]
    h.ok("choosing Children lists only child measures", set(kids) == child, kids)
    tw["group"].set("adult"); h.pump(root, 0.2)
    tw["tv"].selection_set("phq9"); h.pump(root, 0.2)
    tw["start"](); h.pump(root, 1)
    run = app._assess_run
    W = run["win"]
    h.ok("the assessment opens full screen and on top", bool(W.attributes("-fullscreen")) and bool(W.attributes("-topmost")))
    W.tk.call(W.protocol("WM_DELETE_WINDOW"))        # what Alt+F4 / the close box run
    W.event_generate("<Escape>"); h.pump(root, 0.3)
    h.ok("Escape and the close box don't close it", W.winfo_exists())
    h.ok("the app won't auto-lock during the test", app._assessing)
    answers = [1, 2, 1, 1, 0, 1, 1, 0, 0]
    run["ask"]("1"); h.pump(root, 0.2)
    for i, val in enumerate(answers, 1):
        run["choose"](str(i), val, 0); h.pump(root, 0.15)
    h.pump(root, 0.3)
    run["choose"]("diff", 1, 0); h.pump(root, 0.5)
    h.ok("after the last question: hand-back screen, results hidden", "hand the computer back" in h.texts(W)
         and "/27" not in h.texts(W), h.texts(W)[:200])
    c = V.load_client(cid)
    e = (c.get("assessments") or [{}])[-1]
    h.ok("saved right away, scored, with a PHQ-9 score for the charts", e.get("result", {}).get("total") == 7
         and e.get("completed") and e.get("measure_id") and any(m["id"] == e["measure_id"] and m["total"] == 7
                                                               for m in c.get("measures") or []), e)
    run["unlock"](); h.pump(root, 0.3)
    st = run["state"]
    st["pw"].insert(0, "wrong"); st["pw"].event_generate("<Return>"); h.pump(root, 0.3)
    h.ok("a wrong password doesn't close it", W.winfo_exists() and "Incorrect password" in h.texts(W))
    st["pw"].insert(0, "testpassword1"); st["pw"].event_generate("<Return>"); h.pump(root, 1)
    h.ok("the vault password closes it and shows the results", not W.winfo_exists() and app._results_win["win"].winfo_exists()
         and "7/27 (mild)" in h.texts(app._results_win["win"]), h.texts(app._results_win["win"])[:300])
    h.ok("…linked to today's session", "Session 4" in h.texts(app._results_win["win"]))
    h.ok("auto-lock is back on", not app._assessing)
    app._results_win["win"].destroy()
    h.ok("the Assessments tab lists it once (no duplicate PHQ-9 row)", h.texts(app.page).count("7/27 (mild)") == 1)
    # a second administration → trend
    app.run_assessment(V.load_client(cid), "phq9", "adult"); h.pump(root, 0.5)
    run = app._assess_run
    run["ask"]("1")
    for i, val in enumerate([3, 3, 2, 2, 2, 2, 2, 1, 1], 1):
        run["choose"](str(i), val, 0); h.pump(root, 0.1)
    h.pump(root, 0.3); run["choose"]("diff", 2, 0); h.pump(root, 0.4)
    run["unlock"](); h.pump(root, 0.2)
    run["state"]["pw"].insert(0, "testpassword1"); run["state"]["pw"].event_generate("<Return>"); h.pump(root, 1)
    t = h.texts(app._results_win["win"])
    h.ok("second time: trend and the change since last time", "Trend" in t and "+11 since" in t and "worse" in t, t[-500:])
    h.ok("item 9 answered → flag and a button to start the safety assessment",
         "Item 9" in t and h.button(app._results_win["win"], "Brief Suicide Safety Assessment") is not None)
    h.button(app._results_win["win"], "Brief Suicide Safety Assessment").invoke(); h.pump(root, 0.5)
    b = app._bssa_win
    b["v"]["thoughts"].set("Yes"); b["v"]["now"].set("No"); b["v"]["disp"].set("further")
    b["v"]["plan_people"].insert("1.0", "Sister; 988")
    b["save"](); h.pump(root, 0.5)
    c = V.load_client(cid)
    bs = [a for a in c["assessments"] if a.get("instrument") == "bssa"]
    h.ok("the safety assessment is saved with its disposition", bs and "Further evaluation" in bs[0]["score"]
         and any("Sister; 988" in x for x in bs[0]["report"]), bs[:1])
    app._results_win["win"].destroy()
    # ASQ positive → Home dashboard risk
    app.run_assessment(V.load_client(cid), "asq", "adult"); h.pump(root, 0.4)
    run = app._assess_run
    run["ask"]("1")
    for q, val in (("1", 1), ("2", 0), ("3", 0), ("4", 0), ("5", 0)):
        run["choose"](q, val, 0); h.pump(root, 0.15)
    h.pump(root, 0.3); run["close"](); h.pump(root, 0.5)
    _ = [w.destroy() for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
    data = ss.caseload([(V.load_client(cid), V.sessions_for(cid))])
    h.ok("a positive ASQ shows under Risk on the Home dashboard", any("ASQ" in x["text"] for x in data["risk"]),
         [x["text"] for x in data["risk"]])
    h.ok("other questionnaires reach the AI's context (latest result)", "ASQ" in ss.measures_text(V.load_client(cid)))
    # stop early
    app.run_assessment(V.load_client(cid), "gad7", "adult"); h.pump(root, 0.4)
    run = app._assess_run
    run["ask"]("1"); run["choose"]("1", 2, 0); h.pump(root, 0.2)
    run["stop"](); h.pump(root, 0.2)
    h.ok("Stop goes to the hand-back screen", "hand the computer back" in h.texts(run["win"]))
    run["close"](); h.pump(root, 0.5)
    g = [a for a in V.load_client(cid)["assessments"] if a.get("instrument") == "gad7"]
    h.ok("a stopped test is kept as partial (when you choose to keep it), with no GAD-7 score added",
         g and not g[0]["completed"] and g[0]["answered"] == 1 and not g[0].get("measure_id"))
    _ = [w.destroy() for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
