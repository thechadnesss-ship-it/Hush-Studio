#!/usr/bin/env python
"""Hush Studio control app: tasks, priorities, subscription budget. Single source of truth = ops/state.json.

CLI (both Claude and Codex use this):
  python ops/control.py list [--status todo] [--owner codex]
  python ops/control.py next --owner claude        # claim highest-priority unblocked todo
  python ops/control.py add "title" -p 1 -o codex -e 2.5 [--dep T3] [--notes "..."]   # -e = est. % of weekly allowance
  python ops/control.py set T5 --status review --owner sam -p 2
  python ops/control.py done T5 --note "what changed"
  python ops/control.py usage                       # LIVE subscription usage: Claude + Codex, 5h and weekly
  python ops/control.py budget [--weekly 90] [--five-hour 95]   # caps; `next` refuses past them
  python ops/control.py serve [--port 8765]         # dashboard at http://127.0.0.1:8765
Priority: 1 (highest) .. 4. Status: todo, doing, review, done, blocked. Owner: claude, codex, sam, any.
Set env AGENT=claude|codex so the log shows who did what.
"""
import argparse, json, os, subprocess, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

def _state_path():
    """One state file for every worktree: lives next to the MAIN checkout's ops/ folder."""
    here = Path(__file__).resolve().parent
    try:
        import subprocess
        g = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=here,
                           capture_output=True, text=True, timeout=5).stdout.strip()
        if g:
            return Path(g).parent / "ops" / "state.json"
    except Exception:
        pass
    return here / "state.json"


STATE = _state_path()
CLAUDE_SNAP = STATE.with_name("claude_usage.json")   # written by ops/claude_statusline.py
CODEX_EXE = Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe"
LOCK = STATE.with_suffix(".lock")
STATUSES = ["todo", "doing", "review", "done", "blocked"]
OWNERS = ["claude", "codex", "sam", "any"]
_tl = threading.Lock()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class locked:
    """Cross-process lock (claude + codex + dashboard may write at once)."""

    def __enter__(self):
        _tl.acquire()
        for _ in range(200):
            try:
                self.fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - LOCK.stat().st_mtime > 30:
                        LOCK.unlink()  # stale
                except OSError:
                    pass
                time.sleep(0.05)
        _tl.release()
        raise RuntimeError("state lock timeout")

    def __exit__(self, *a):
        os.close(self.fd)
        try:
            LOCK.unlink()
        except OSError:
            pass
        _tl.release()


# ---------------------------------------------------------------- live subscription usage
_cache = {}


def _cached(key, ttl, fn):
    t, v = _cache.get(key, (0, None))
    if time.time() - t > ttl:
        try:
            v = fn()
        except Exception as e:  # never let a usage probe break the app
            v = {"error": str(e)}
        _cache[key] = (time.time(), v)
    return v


def _win(w):
    return {"pct": w["usedPercent"], "mins": w["windowDurationMins"], "resets_at": w["resetsAt"]}


def codex_usage():
    """Ask Codex's own app-server for the live plan limits (same numbers as its usage screen)."""
    def go():
        p = subprocess.Popen([str(CODEX_EXE), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        try:
            send = lambda o: (p.stdin.write(json.dumps(o) + chr(10)), p.stdin.flush())
            send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "hush-control", "version": "1"}}})
            got = {}
            def rd():
                for line in p.stdout:
                    try: d = json.loads(line)
                    except ValueError: continue
                    if d.get("id") == 1 and "sent" not in got:
                        got["sent"] = 1
                        send({"method": "initialized"}); send({"id": 2, "method": "account/rateLimits/read"})
                    elif d.get("id") == 2:
                        got["r"] = d; return
            th = threading.Thread(target=rd, daemon=True); th.start(); th.join(15)
        finally:
            p.kill()
        r = got.get("r", {}).get("result")
        if not r: return {"error": "no answer from codex app-server"}
        rl = r["rateLimits"]
        return {"plan": rl.get("planType"), "five_hour": _win(rl["primary"]), "weekly": _win(rl["secondary"]),
                "limit_reached": rl.get("rateLimitReachedType"), "live": True}
    return _cached("codex", 60, go)


def _claude_tokens():
    """Fallback: tokens from local Claude Code transcripts (all projects), deduped by message id."""
    root = Path.home() / ".claude" / "projects"
    cut5, cut7 = time.time() - 5 * 3600, time.time() - 7 * 86400
    seen, tot = set(), {"5h": 0, "7d": 0}
    for f in root.glob("*/*.jsonl"):
        if f.stat().st_mtime < cut7: continue
        for line in f.open(encoding="utf-8", errors="ignore"):
            if '"usage"' not in line: continue
            try: d = json.loads(line)
            except ValueError: continue
            m = d.get("message") or {}; u = m.get("usage")
            if not u or m.get("id") in seen: continue
            seen.add(m.get("id"))
            ts = time.mktime(time.strptime(d["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            n = u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            if ts >= cut7: tot["7d"] += n
            if ts >= cut5: tot["5h"] += n
    return tot


def claude_usage():
    """Plan % comes from Claude Code's statusline JSON (ops/claude_statusline.py snapshots it);
    token totals from transcripts are always available as a cross-check."""
    out = {"tokens": _cached("ctok", 60, _claude_tokens)}
    try:
        d = json.loads(CLAUDE_SNAP.read_text(encoding="utf-8"))
        rl = d.get("rate_limits") or {}
        age = time.time() - CLAUDE_SNAP.stat().st_mtime
        if rl.get("five_hour") and rl.get("seven_day"):
            out.update(five_hour={"pct": rl["five_hour"]["used_percentage"], "resets_at": rl["five_hour"].get("resets_at")},
                       weekly={"pct": rl["seven_day"]["used_percentage"], "resets_at": rl["seven_day"].get("resets_at")},
                       live=age < 600, age_s=int(age))
        else:
            out["note"] = "statusline snapshot has no rate_limits yet (send a message in Claude Code)"
    except FileNotFoundError:
        out["note"] = "no snapshot yet: open Claude Code in this project once so the statusline can record limits"
    return out


def all_usage():
    return {"claude": claude_usage(), "codex": codex_usage()}


def pct(u, agent, win):
    try: return u[agent][win]["pct"]
    except (KeyError, TypeError): return None


def over_cap(s, agent):
    """Budget guard: True (+reason) if this agent has used up its weekly/5h allowance cap."""
    u = all_usage(); b = s["budget"]
    for win, cap in (("weekly", b["weekly_cap_pct"]), ("five_hour", b["five_hour_cap_pct"])):
        v = pct(u, agent, win)
        if v is not None and v >= cap: return f"{agent} {win} usage {v}% >= cap {cap}%"
    return None


def load():
    if not STATE.exists():
        return {"budget": {"weekly_cap_pct": 90, "five_hour_cap_pct": 95}, "next_id": 1, "tasks": [], "spend": [], "log": []}
    return json.loads(STATE.read_text(encoding="utf-8"))


def save(s):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE)


def mutate(fn):
    with locked():
        s = load()
        r = fn(s)
        save(s)
        return r


def find(s, tid):
    for t in s["tasks"]:
        if t["id"].lower() == tid.lower():
            return t
    raise SystemExit(f"no such task {tid}")


def log(s, who, msg):
    s["log"].append({"t": now(), "who": who, "msg": msg})
    s["log"] = s["log"][-500:]


def add(s, title, p=3, owner="any", est=0.0, dep=(), notes="", who="cli"):
    t = {"id": f"T{s['next_id']}", "title": title, "status": "todo", "priority": p, "owner": owner,
         "est_pct": est, "start_pct": None, "used_pct": None, "depends": list(dep), "notes": notes, "created": now(), "updated": now()}
    s["next_id"] += 1
    s["tasks"].append(t)
    log(s, who, f"added {t['id']}: {title}")
    return t


def ready(s, t):
    done = {x["id"] for x in s["tasks"] if x["status"] == "done"}
    return t["status"] == "todo" and all(d in done for d in t["depends"])


def update(s, tid, who="cli", **kw):
    t = find(s, tid)
    for k, v in kw.items():
        if v is not None:
            t[k] = v
    ag = t["owner"] if t["owner"] in ("claude", "codex") else None
    if ag and kw.get("status") == "doing" and t.get("start_pct") is None:
        t["start_pct"] = pct(all_usage(), ag, "weekly")          # weekly % when work began
    if ag and kw.get("status") == "done" and t.get("start_pct") is not None:
        now_pct = pct(all_usage(), ag, "weekly")
        if now_pct is not None:
            t["used_pct"] = round(max(0, now_pct - t["start_pct"]), 1)  # weekly allowance this task consumed
    t["updated"] = now()
    log(s, who, f"{t['id']} " + ", ".join(f"{k}={v}" for k, v in kw.items() if v is not None))
    return t


def view(s):
    est = round(sum(t.get("est_pct", 0) for t in s["tasks"] if t["status"] != "done"), 1)
    return {**s, "usage": all_usage(), "open_estimate_pct": est}


def fmt(t):
    return (f"{t['id']:<5}P{t['priority']} {t['status']:<8}{t['owner']:<7}{t['est_pct']:>4g}%  {t['title']}"
            + (f"  (after {','.join(t['depends'])})" if t["depends"] else ""))


def show_usage(u):
    for ag in ("claude", "codex"):
        x = u[ag]
        if x.get("error"):
            print(f"{ag:<7}error: {x['error']}")
            continue
        if x.get("five_hour"):
            r = x["weekly"].get("resets_at")
            r = time.strftime("%a %H:%M", time.localtime(r)) if r else "?"
            print(f"{ag:<7}5h {x['five_hour']['pct']:>3.0f}%   week {x['weekly']['pct']:>3.0f}% (resets {r})"
                  + ("" if x.get("live", True) else "  [stale]") + (f"   plan {x['plan']}" if x.get("plan") else ""))
        else:
            print(f"{ag:<7}{x.get('note', 'n/a')}")
        if x.get("tokens"):
            print(f"        local tokens: 5h {x['tokens']['5h']:,}  7d {x['tokens']['7d']:,}")


def cli(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("list"); a.add_argument("--status"); a.add_argument("--owner")
    a = sp.add_parser("next"); a.add_argument("--owner", default="any")
    a = sp.add_parser("add"); a.add_argument("title"); a.add_argument("-p", type=int, default=3)
    a.add_argument("-o", default="any", choices=OWNERS); a.add_argument("-e", type=float, default=0.0)
    a.add_argument("--dep", action="append", default=[]); a.add_argument("--notes", default="")
    a = sp.add_parser("set"); a.add_argument("id"); a.add_argument("--status", choices=STATUSES)
    a.add_argument("--owner", choices=OWNERS); a.add_argument("-p", type=int, dest="priority")
    a.add_argument("--title"); a.add_argument("--notes"); a.add_argument("-e", type=float, dest="est_pct")
    a = sp.add_parser("done"); a.add_argument("id"); a.add_argument("--note", default="")
    sp.add_parser("usage")
    a = sp.add_parser("budget"); a.add_argument("--weekly", type=float)
    a.add_argument("--five-hour", type=float, dest="five")
    a = sp.add_parser("serve"); a.add_argument("--port", type=int, default=8765)
    for x in sp.choices.values():
        x.add_argument("--who", default=os.environ.get("AGENT", "cli"))
    n = ap.parse_args(argv)

    if n.cmd == "list":
        s = load()
        for t in sorted(s["tasks"], key=lambda t: (t["status"] == "done", t["priority"], int(t["id"][1:]))):
            if (not n.status or t["status"] == n.status) and (not n.owner or t["owner"] in (n.owner, "any")):
                print(fmt(t))
    elif n.cmd == "next":
        def f(s):
            if n.owner in ("claude", "codex"):
                why = over_cap(s, n.owner)
                if why:
                    return "STOP: " + why + " (raise with `budget`, or wait for the reset)"
            c = [t for t in s["tasks"] if ready(s, t) and t["owner"] in (n.owner, "any")]
            if not c:
                return None
            t = min(c, key=lambda t: (t["priority"], int(t["id"][1:])))
            update(s, t["id"], n.who, status="doing", owner=n.owner if n.owner != "any" else t["owner"])
            return t
        t = mutate(f)
        print(t if isinstance(t, str) else (fmt(t) + (f"\n  notes: {t['notes']}" if t["notes"] else "") if t else "nothing ready"))
    elif n.cmd == "add":
        print(fmt(mutate(lambda s: add(s, n.title, n.p, n.o, n.e, n.dep, n.notes, n.who))))
    elif n.cmd == "set":
        print(fmt(mutate(lambda s: update(s, n.id, n.who, status=n.status, owner=n.owner, priority=n.priority,
                                          title=n.title, notes=n.notes, est_pct=n.est_pct))))
    elif n.cmd == "done":
        def f(s):
            t = update(s, n.id, n.who, status="done")
            if n.note:
                t["notes"] = (t["notes"] + "\n" if t["notes"] else "") + f"[{now()} {n.who}] {n.note}"
            return t
        print(fmt(mutate(f)))
    elif n.cmd == "usage":
        show_usage(all_usage())
    elif n.cmd == "budget":
        def f(s):
            if n.weekly is not None:
                s["budget"]["weekly_cap_pct"] = n.weekly
            if n.five is not None:
                s["budget"]["five_hour_cap_pct"] = n.five
            return s["budget"]
        print(mutate(f))
        show_usage(all_usage())
    elif n.cmd == "serve":
        serve(n.port)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ct="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/state":
            self.send(200, view(load()))
        elif self.path in ("/", "/index.html"):
            self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        who = "sam"
        try:
            if self.path == "/api/add":
                mutate(lambda s: add(s, d["title"], int(d.get("priority", 3)), d.get("owner", "any"),
                                     float(d.get("est_pct", 0)), d.get("depends", []), d.get("notes", ""), who))
            elif self.path == "/api/set":
                tid = d.pop("id")
                keys = ("status", "owner", "priority", "notes", "est_pct", "title")
                mutate(lambda s: update(s, tid, who, **{k: d[k] for k in keys if k in d}))
            elif self.path == "/api/caps":
                def f(s):
                    s["budget"]["weekly_cap_pct"] = float(d["weekly"])
                    s["budget"]["five_hour_cap_pct"] = float(d["five"])
                mutate(f)
            else:
                return self.send(404, {"error": "not found"})
            self.send(200, view(load()))
        except (SystemExit, KeyError, ValueError) as e:
            self.send(400, {"error": str(e)})


def serve(port):
    print(f"Hush control: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Hush control</title><style>
:root{--p:#7A23A1;--l:#F5EFF9;--l2:#D2BFE1;font-family:Montserrat,system-ui,sans-serif}
body{padding:16px;background:#fff;color:#111;max-width:1100px;margin:auto}
h1{font-weight:300;color:var(--p)}.bar{background:var(--l);border-radius:12px;padding:12px 16px;margin-bottom:16px}
.urow{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}.ucard h3{margin:0 0 4px;color:var(--p);font-size:13px}.meter{height:10px;background:var(--l2);border-radius:5px;overflow:hidden;margin:8px 0}.meter i{display:block;height:100%;background:var(--p)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.col{background:var(--l);border-radius:12px;padding:10px;min-height:80px}.col h3{margin:0 0 8px;font-size:13px;text-transform:uppercase;color:var(--p)}
.card{background:#fff;border-radius:8px;padding:8px;margin-bottom:8px;font-size:13px;border-left:4px solid var(--l2)}
.card.p1{border-color:#c0392b}.card.p2{border-color:#e67e22}.meta{color:#666;font-size:11px;margin-top:4px}
select,input,button{font:inherit;font-size:12px;padding:3px 6px;border-radius:6px;border:1px solid var(--l2)}
button{background:var(--p);color:#fff;border:0;cursor:pointer;border-radius:999px;padding:4px 12px}
form{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}.log{font-size:11px;color:#555;max-height:160px;overflow:auto}
</style><h1>Hush Studio · control</h1><div class=bar id=bar></div>
<form id=f><input name=title placeholder="New task" required size=30><select name=priority><option>1<option>2<option selected>3<option>4</select>
<select name=owner><option>any<option>claude<option>codex<option>sam</select><input name=est_pct type=number step=.1 placeholder="est %" style="width:70px"><button>Add</button></form>
<div class=cols id=cols></div><h3>Log</h3><div class=log id=log></div>
<script>
const ST=["todo","doing","review","blocked","done"],OW=["any","claude","codex","sam"];
const post=(u,d)=>fetch(u,{method:"POST",body:JSON.stringify(d)}).then(r=>r.json()).then(render);
const sel=(o,v,id,k)=>`<select onchange="post('/api/set',{id:'${id}',${k}:this.value})">${o.map(x=>`<option ${x==v?"selected":""}>${x}</option>`).join("")}</select>`;
const esc=s=>String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const rst=t=>t?new Date(t*1000).toLocaleString([],{weekday:"short",hour:"2-digit",minute:"2-digit"}):"?";
const gauge=(n,w,cap)=>w?`<div>${n} <b>${w.pct}%</b> <span class=meta>resets ${rst(w.resets_at)}${cap?` · cap ${cap}%`:""}</span><div class=meter><i style="width:${Math.min(100,w.pct)}%;${cap&&w.pct>=cap?"background:#c0392b":""}"></i></div></div>`:"";
const ag=(n,x,b)=>`<div class=ucard><h3>${n}${x.plan?" · "+x.plan:""}${x.live===false?" · stale":""}</h3>`+(x.error?`<span class=meta>${esc(x.error)}</span>`:
 (gauge("5-hour",x.five_hour,b.five_hour_cap_pct)+gauge("Weekly",x.weekly,b.weekly_cap_pct)||`<span class=meta>${esc(x.note||"")}</span>`))+
 (x.tokens?`<div class=meta>local tokens 5h ${x.tokens["5h"].toLocaleString()} · 7d ${x.tokens["7d"].toLocaleString()}</div>`:"")+`</div>`;
function render(s){const u=s.usage,b=s.budget;
bar.innerHTML=`<div class=urow>${ag("Claude",u.claude,b)}${ag("Codex",u.codex,b)}</div><div class=meta>open task estimates: ${s.open_estimate_pct}% of a weekly allowance · `+
`<button onclick="let w=prompt('Weekly cap %',${b.weekly_cap_pct}),f=w&&prompt('5-hour cap %',${b.five_hour_cap_pct});f&&post('/api/caps',{weekly:w,five:f})">Set caps</button></div>`;
cols.innerHTML=ST.map(st=>`<div class=col><h3>${st}</h3>`+s.tasks.filter(t=>t.status==st).sort((a,b)=>a.priority-b.priority).map(t=>
`<div class="card p${t.priority}" title="${esc(t.notes)}"><b>${t.id}</b> ${esc(t.title)}<div class=meta>P${t.priority} · est ${t.est_pct}%${t.used_pct!=null?` · used ${t.used_pct}%`:''}${t.depends.length?" · after "+t.depends:""}</div>
<div class=meta>${sel(ST,t.status,t.id,"status")} ${sel(OW,t.owner,t.id,"owner")}</div></div>`).join("")+`</div>`).join("");
log.innerHTML=s.log.slice(-25).reverse().map(l=>`${l.t} <b>${esc(l.who)}</b> ${esc(l.msg)}`).join("<br>")}
f.onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(f));d.priority=+d.priority;d.est_pct=+d.est_pct||0;post("/api/add",d);f.reset()};
const poll=()=>fetch("/api/state").then(r=>r.json()).then(render);poll();
setInterval(()=>{if(!document.activeElement||document.activeElement.tagName!="SELECT")poll()},4000);
</script>"""

if __name__ == "__main__":
    cli(sys.argv[1:])
