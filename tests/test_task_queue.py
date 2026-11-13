"""Queue window lists every task (running, paused, waiting) with pause/resume, reorder, run next, cancel."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

ran = []


def slow(name, secs=1.5):
    def fn(p):
        t = time.time()
        while time.time() - t < secs:
            p(f"{name} working", min(99, (time.time() - t) / secs * 100))
            time.sleep(0.05)
        return name
    return fn


def script(app, root, V, h):
    done = lambda r: ran.append(r)
    app.run_bg(slow("A", 2.5), done, label="writing the case overview for AB1", owner=("case", "1"), patient="AB1",
               pausable=True)
    for n in "BCD":
        h.ok(f"{n} waits in line", app.run_bg(slow(n, 0.6), done, label=f"task {n}", owner=(n.lower(), "1"),
                                                 patient="XY2") == "queued")
    app.open_queue(); h.pump(root, 0.5)
    q = app.queue_win
    tv = q["ttv"]
    rows = list(tv.get_children())
    h.ok("queue shows the running task and 3 waiting", rows[0] == "run" and len(rows) == 4, rows)
    h.ok("running row names patient and task", "Writing the case overview" in tv.item("run")["values"][1]
         and tv.item("run")["values"][0] == "AB1", tv.item("run")["values"])
    h.ok("header button mentions waiting tasks", "3 tasks waiting" in app.queue_btn.cget("text"), app.queue_btn.cget("text"))
    kB, kC, kD = rows[1], rows[2], rows[3]
    tv.selection_set(kC); h.pump(root, 0.2); h.shot("task_queue_busy.png")
    # pause B (waiting) so it's skipped
    tv.selection_set(kB); h.pump(root, 0.1)
    h.action(q["win"], "⏸  Pause").invoke() if False else None
    pause_btn = next(w for w in h.widgets(q["win"], "TButton") if "Pause" in str(w.cget("text")) and w.winfo_ismapped()
                     and str(w.cget("state")) != "disabled")
    pause_btn.invoke(); h.pump(root, 0.2)
    h.ok("paused waiting task shows as paused", "Paused" in str(tv.item(kB)["values"][2]), tv.item(kB)["values"])
    # move D to run next
    tv.selection_set(kD); h.pump(root, 0.1)
    h.button(q["win"], "Run next").invoke(); h.pump(root, 0.2)
    h.ok("Run next moves D to the top of the line", list(tv.get_children())[1] == kD, list(tv.get_children()))
    # move C up (above B)
    tv.selection_set(kC); h.pump(root, 0.1)
    h.button(q["win"], "▲").invoke(); h.pump(root, 0.2)
    h.ok("▲ moves C up", list(tv.get_children())[1:] == [kD, kC, kB], list(tv.get_children()))
    # pause the running one
    tv.selection_set("run"); h.pump(root, 0.6)
    pb = next(w for w in h.widgets(q["win"], "TButton") if "Pause" in str(w.cget("text")) and w.winfo_ismapped())
    h.ok("running task can be paused here", str(pb.cget("state")) != "disabled")
    pb.invoke(); h.pump(root, 0.3)
    h.ok("A is paused", app.paused_task is not None and "paused" in tv.get_children())
    # D then C run; B stays held
    h.pump(root, 6, lambda: ran == ["D", "C"])
    h.pump(root, 0.5)
    h.ok("D then C ran, paused B and paused A didn't", ran == ["D", "C"], ran)
    # resume A from the list
    tv.selection_set("paused"); h.pump(root, 0.1)
    h.button(q["win"], "Resume").invoke()
    h.pump(root, 8, lambda: "A" in ran)
    h.ok("A resumed and finished", "A" in ran, ran)
    h.pump(root, 0.5)
    h.ok("B still held after A", "B" not in ran and kB in tv.get_children())
    tv.selection_set(kB); h.pump(root, 0.1)
    h.button(q["win"], "Cancel").invoke(); h.pump(root, 0.5)
    h.ok("cancel removes it from the line", not app.waitlist and not tv.get_children(), (list(tv.get_children()), [tv.item(k)["values"] for k in tv.get_children()], ran))
    h.ok("empty message shows", "Nothing running or waiting" in h.texts(q["win"]))
    h.ok("header button back to normal", "waiting" not in app.queue_btn.cget("text"))
    h.shot("task_queue.png")


if __name__ == "__main__":
    H.start(script)
