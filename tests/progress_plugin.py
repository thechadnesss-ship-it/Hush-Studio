"""Optional live progress window for pytest:  pytest --ui

Shows an always-on-top window that says WHICH task/branch/worktree the run belongs to (so several
runs at once can be told apart), with a progress bar, pass/fail/skip counts, the running test,
per-file progress, failed tests so far, elapsed time and an ETA. The window is a separate process
(tkinter, stdlib) fed JSON lines over a pipe, so it can never slow down or crash the test run.
Per-test durations from the previous run are remembered in `.pytest_durations.json` so the ETA is
accurate from the first test; without history it falls back to the average so far.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HISTORY = _ROOT / ".pytest_durations.json"

_WINDOW = r'''
import json, sys, time, threading, queue, tkinter as tk
from tkinter import ttk

root = tk.Tk()
root.attributes("-topmost", True)
root.resizable(False, False)
F, FB, FS = ("Segoe UI", 10), ("Segoe UI", 10, "bold"), ("Segoe UI", 9)

def label(text="", font=F, fg="black", pady=0):
    l = tk.Label(root, text=text, font=font, fg=fg, anchor="w", justify="left", wraplength=560)
    l.pack(fill="x", padx=14, pady=pady)
    return l

head = label("Starting...", ("Segoe UI", 13, "bold"), "#7A23A1", (12, 0))
sub = label("", F, "#333")
ctx = label("", FS, "#666", (2, 6))
bar = ttk.Progressbar(root, length=560, mode="determinate")
bar.pack(padx=14, pady=4)
counts = label("")
current = label("", FS, "#555")
timing = label("", F, "black", (2, 0))
fails_title = label("", FB, "#B00020", (6, 0))
fails = label("", FS, "#B00020")
mods = label("", FS, "#444", (6, 8))

def fmt(s):
    s = max(0, int(round(s)))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

S = {"total": 0, "done": 0, "failed": 0, "eta": None, "eta_at": 0, "start": time.time(),
     "finished": False, "failed_names": [], "mods": {}, "mod_total": {}}

def refresh():
    if S["finished"]:
        return
    t = f"Elapsed {fmt(time.time() - S['start'])}"
    if S["eta"] is not None:
        t += f"   ·   ETA {fmt(S['eta'] - (time.time() - S['eta_at']))}"
        if S["done"] and S["total"]:
            t += f"   ·   {S['done'] * 100 // S['total']}%"
    timing.config(text=t)
    root.after(500, refresh)

def show_mods():
    rows = []
    for m, tot in S["mod_total"].items():
        d = S["mods"].get(m, 0)
        rows.append(f"{'✓' if d >= tot else '▶' if d else '·'} {m}  {d}/{tot}")
    mods.config(text="\n".join(rows[:14]) + (f"\n… +{len(rows) - 14} more files" if len(rows) > 14 else ""))

q = queue.Queue()
def reader():
    for line in sys.stdin:
        try: q.put(json.loads(line))
        except ValueError: pass
    q.put({"ev": "eof"})
threading.Thread(target=reader, daemon=True).start()

def pump():
    try:
        while True:
            m = q.get_nowait(); ev = m["ev"]
            if ev == "start":
                c = m["ctx"]
                S["total"] = m["total"]; bar["maximum"] = max(1, m["total"]); S["mod_total"] = m["mods"]
                head.config(text=c["task"] or c["branch"] or "Tests")
                root.title(f"Tests · {c['task_id'] or c['branch'] or c['dir']} · pid {c['pid']}")
                sub.config(text=c["task_title"])
                ctx.config(text=f"{c['agent']} · branch {c['branch']}\n{c['dir']}\n"
                                f"command: {c['cmd']}\nselection: {c['select']} · pid {c['pid']} · started {c['started']}")
                root.geometry(f"+{60 + (c['pid'] % 5) * 40}+{60 + (c['pid'] % 5) * 40}")
                show_mods()
            elif ev == "running":
                current.config(text="▶ " + m["name"])
            elif ev == "done":
                S.update(done=m["done"], failed=m["failed"], eta=m["eta"], eta_at=time.time())
                S["mods"][m["mod"]] = S["mods"].get(m["mod"], 0) + 1
                bar["value"] = m["done"]
                if m["bad"]:
                    S["failed_names"].append(m["bad"])
                counts.config(text=f"{m['done']} / {S['total']} done   ·   {m['failed']} failed   ·   "
                                   f"{m['skipped']} skipped", fg="#B00020" if m["failed"] else "black")
                if S["failed_names"]:
                    fails_title.config(text="Failed so far:")
                    fails.config(text="\n".join(S["failed_names"][-6:]))
                show_mods()
            elif ev in ("finish", "eof"):
                S["finished"] = True
                bad = S["failed"] or m.get("failed")
                head.config(fg="#B00020" if bad else "#1B7F3B")
                current.config(text="Finished: " + ("FAILURES (window closes in 3 min)" if bad else "all passed"))
                timing.config(text=f"Took {fmt(time.time() - S['start'])}")
                bar["value"] = bar["maximum"]
                root.after(4000 if not bad else 180000, root.destroy)  # failures linger 3 min
                return
    except queue.Empty:
        pass
    root.after(100, pump)

root.after(100, pump); refresh()
root.mainloop()
'''


def pytest_addoption(parser):
    parser.addoption("--ui", action="store_true", default=False,
                     help="force the live progress window on (it is on by default)")
    parser.addoption("--no-ui", action="store_true", default=False,
                     help="don't show the live progress window (or set HUSH_TEST_UI=0)")


def ui_enabled(config):
    """On by default so task/review runs show their progress; off for opt-outs and nested runs."""
    if config.getoption("--ui", default=False):
        return True
    if (config.getoption("--no-ui", default=False) or os.environ.get("HUSH_TEST_UI") == "0"
            or os.environ.get("HUSH_TEST_UI_ACTIVE")):  # a pytest run inside a test: no 2nd window
        return False
    opt = config.option
    return not (getattr(opt, "collectonly", False) or getattr(opt, "help", False))


def _git(*args):
    try:
        return subprocess.run(["git", *args], cwd=_ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def _task_title(task_id):
    """Look the task up in the plan; worktrees may not have a copy, so also try the main folder."""
    plan = Path("Docs") / "Engine Build Plan" / "engine_plan.json"
    bases = [_ROOT]
    first = _git("worktree", "list", "--porcelain").split("\n")[0]
    if first.startswith("worktree "):
        bases.append(Path(first[len("worktree "):]))
    for base in bases:
        try:
            tasks = json.loads((base / plan).read_text(encoding="utf-8")).get("tasks", [])
        except Exception:
            continue
        for t in tasks:
            if t.get("id") == task_id:
                return t.get("title", "")
    return ""


def _task_context(config):
    """Which task/branch/worktree this run belongs to, so several windows can be told apart."""
    branch = _git("branch", "--show-current")
    m = re.match(r"([\w-]+)/(T\d+)", branch or "")
    agent, tid = (m.group(1), m.group(2)) if m else ("", "")
    if not agent:  # fall back to the worktree folder name, e.g. hush-studio-claude
        agent = ("claude" if _ROOT.name.endswith("claude") else
                 "codex" if _ROOT.name.endswith("codex") else "main folder")
    title = _task_title(tid) if tid else ""
    args = list(config.invocation_params.args)
    return {"task": f"{tid}: {title}" if tid else "", "task_id": tid, "task_title": title,
            "branch": branch or "(detached)", "agent": agent, "dir": str(_ROOT), "pid": os.getpid(),
            "cmd": "pytest " + " ".join(args),
            "select": " ".join(a for a in args if a != "--ui") or "all tests (testpaths)",
            "started": time.strftime("%H:%M:%S")}


class ProgressUI:
    def __init__(self):
        self.proc = None
        self.hist = {}
        self.new = {}
        self.total = 0
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.remaining = []        # nodeids in the run
        self.t0 = time.time()

    def _send(self, **m):
        try:
            self.proc.stdin.write(json.dumps(m) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def pytest_collection_finish(self, session):
        try:
            self.hist = json.loads(_HISTORY.read_text())
        except Exception:
            self.hist = {}
        self.remaining = [i.nodeid for i in session.items]
        self.total = len(self.remaining)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            # stdout/stderr go to DEVNULL: a window that outlives pytest must not keep the caller's
            # output pipe open (Narthen / a reviewer would otherwise wait for it to close).
            self.proc = subprocess.Popen([sys.executable, "-c", _WINDOW], stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", creationflags=flags,
                                         env={**os.environ, "HUSH_TEST_UI_ACTIVE": "1"})
            os.environ["HUSH_TEST_UI_ACTIVE"] = "1"
        except Exception:
            self.proc = None
            return
        mods = {}
        for n in self.remaining:
            k = n.split("::")[0].split("/")[-1]
            mods[k] = mods.get(k, 0) + 1
        self._send(ev="start", total=self.total, mods=mods, ctx=_task_context(session.config))

    def pytest_runtest_logstart(self, nodeid, location):
        if self.proc:
            self._send(ev="running", name=nodeid.split("/")[-1][:90])

    def _eta(self):
        # known durations for remaining tests; unknown ones use the mean of what we've seen
        seen = list(self.new.values())
        mean = (sum(seen) / len(seen)) if seen else (
            sum(self.hist.values()) / len(self.hist) if self.hist else 1.0)
        # scale history by how this machine/run is behaving vs. last time
        pairs = [(d, self.hist[n]) for n, d in self.new.items() if self.hist.get(n, 0) > 0.05]
        ratio = (sum(a for a, _ in pairs) / sum(b for _, b in pairs)) if pairs else 1.0
        done = set(self.new)
        return sum(self.hist[n] * ratio if n in self.hist else mean
                   for n in self.remaining if n not in done)

    def pytest_runtest_logreport(self, report):
        d = self.new.get(report.nodeid, 0.0) + report.duration  # setup + call + teardown
        self.new[report.nodeid] = d
        if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
            self.done += 1
            self.failed += bool(report.failed)
            self.skipped += bool(report.skipped)
            if self.proc:
                self._send(ev="done", done=self.done, failed=self.failed, skipped=self.skipped,
                           eta=self._eta(), mod=report.nodeid.split("::")[0].split("/")[-1],
                           bad=report.nodeid.split("/")[-1] if report.failed else "")
        elif report.when == "teardown" and report.failed:
            self.failed += 1

    def pytest_sessionfinish(self, session, exitstatus):
        try:
            merged = {**self.hist, **{k: round(v, 3) for k, v in self.new.items()}}
            _HISTORY.write_text(json.dumps(merged))
        except Exception:
            pass
        if self.proc:
            self._send(ev="finish", failed=self.failed)
            try:
                self.proc.stdin.close()
            except Exception:
                pass
