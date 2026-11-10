#!/usr/bin/env python
"""Hush Studio control app: tasks, priorities, budget. Single source of truth = ops/state.json.

CLI (both Claude and Codex use this):
  python ops/control.py list [--status todo] [--owner codex]
  python ops/control.py next --owner claude        # claim highest-priority unblocked todo
  python ops/control.py add "title" -p 1 -o codex -e 2.50 [--dep T3] [--notes "..."]
  python ops/control.py set T5 --status review --owner sam -p 2
  python ops/control.py done T5 --note "what changed"
  python ops/control.py spend T5 0.84 --agent claude --note "tokens"
  python ops/control.py budget [--cap 100]
  python ops/control.py serve [--port 8765]         # dashboard at http://127.0.0.1:8765
Priority: 1 (highest) .. 4. Status: todo, doing, review, done, blocked. Owner: claude, codex, sam, any.
Set env AGENT=claude|codex so the log shows who did what.
"""
import argparse, json, os, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(__file__).with_name("state.json")
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


def load():
    if not STATE.exists():
        return {"budget": {"cap_usd": 100.0}, "next_id": 1, "tasks": [], "spend": [], "log": []}
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


def spent(s, tid=None):
    return round(sum(x["usd"] for x in s["spend"] if tid is None or x["task"] == tid), 4)


def log(s, who, msg):
    s["log"].append({"t": now(), "who": who, "msg": msg})
    s["log"] = s["log"][-500:]


def add(s, title, p=3, owner="any", est=0.0, dep=(), notes="", who="cli"):
    t = {"id": f"T{s['next_id']}", "title": title, "status": "todo", "priority": p, "owner": owner,
         "est_usd": est, "depends": list(dep), "notes": notes, "created": now(), "updated": now()}
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
    t["updated"] = now()
    log(s, who, f"{t['id']} " + ", ".join(f"{k}={v}" for k, v in kw.items() if v is not None))
    return t


def view(s):
    cap = s["budget"]["cap_usd"]
    used = spent(s)
    for t in s["tasks"]:
        t["spent_usd"] = spent(s, t["id"])
    by = {}
    for x in s["spend"]:
        by[x["agent"]] = round(by.get(x["agent"], 0) + x["usd"], 4)
    est = round(sum(t["est_usd"] for t in s["tasks"] if t["status"] != "done"), 2)
    return {**s, "summary": {"cap": cap, "spent": used, "remaining": round(cap - used, 2),
                             "open_estimate": est, "by_agent": by}}


def fmt(t):
    return (f"{t['id']:<5}P{t['priority']} {t['status']:<8}{t['owner']:<7}${t['est_usd']:<6.2f} {t['title']}"
            + (f"  (after {','.join(t['depends'])})" if t["depends"] else ""))


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
    a.add_argument("--title"); a.add_argument("--notes"); a.add_argument("-e", type=float, dest="est_usd")
    a = sp.add_parser("done"); a.add_argument("id"); a.add_argument("--note", default="")
    a = sp.add_parser("spend"); a.add_argument("id"); a.add_argument("usd", type=float)
    a.add_argument("--agent", default=os.environ.get("AGENT", "claude")); a.add_argument("--note", default="")
    a = sp.add_parser("budget"); a.add_argument("--cap", type=float)
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
            c = [t for t in s["tasks"] if ready(s, t) and t["owner"] in (n.owner, "any")]
            if not c:
                return None
            t = min(c, key=lambda t: (t["priority"], int(t["id"][1:])))
            update(s, t["id"], n.who, status="doing", owner=n.owner if n.owner != "any" else t["owner"])
            return t
        t = mutate(f)
        print(fmt(t) + (f"\n  notes: {t['notes']}" if t["notes"] else "") if t else "nothing ready")
    elif n.cmd == "add":
        print(fmt(mutate(lambda s: add(s, n.title, n.p, n.o, n.e, n.dep, n.notes, n.who))))
    elif n.cmd == "set":
        print(fmt(mutate(lambda s: update(s, n.id, n.who, status=n.status, owner=n.owner, priority=n.priority,
                                          title=n.title, notes=n.notes, est_usd=n.est_usd))))
    elif n.cmd == "done":
        def f(s):
            t = update(s, n.id, n.who, status="done")
            if n.note:
                t["notes"] = (t["notes"] + "\n" if t["notes"] else "") + f"[{now()} {n.who}] {n.note}"
            return t
        print(fmt(mutate(f)))
    elif n.cmd == "spend":
        def f(s):
            find(s, n.id)
            s["spend"].append({"t": now(), "task": n.id.upper(), "agent": n.agent, "usd": n.usd, "note": n.note})
            log(s, n.who, f"spend ${n.usd:.2f} on {n.id} ({n.agent})")
            return view(s)["summary"]
        print(mutate(f))
    elif n.cmd == "budget":
        if n.cap is not None:
            mutate(lambda s: s["budget"].update(cap_usd=n.cap))
        print(view(load())["summary"])
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
                                     float(d.get("est_usd", 0)), d.get("depends", []), d.get("notes", ""), who))
            elif self.path == "/api/set":
                tid = d.pop("id")
                keys = ("status", "owner", "priority", "notes", "est_usd", "title")
                mutate(lambda s: update(s, tid, who, **{k: d[k] for k in keys if k in d}))
            elif self.path == "/api/spend":
                def f(s):
                    find(s, d["task"])
                    s["spend"].append({"t": now(), "task": d["task"].upper(), "agent": d.get("agent", "sam"),
                                       "usd": float(d["usd"]), "note": d.get("note", "")})
                mutate(f)
            elif self.path == "/api/cap":
                mutate(lambda s: s["budget"].update(cap_usd=float(d["cap"])))
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
.meter{height:10px;background:var(--l2);border-radius:5px;overflow:hidden;margin:8px 0}.meter i{display:block;height:100%;background:var(--p)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.col{background:var(--l);border-radius:12px;padding:10px;min-height:80px}.col h3{margin:0 0 8px;font-size:13px;text-transform:uppercase;color:var(--p)}
.card{background:#fff;border-radius:8px;padding:8px;margin-bottom:8px;font-size:13px;border-left:4px solid var(--l2)}
.card.p1{border-color:#c0392b}.card.p2{border-color:#e67e22}.meta{color:#666;font-size:11px;margin-top:4px}
select,input,button{font:inherit;font-size:12px;padding:3px 6px;border-radius:6px;border:1px solid var(--l2)}
button{background:var(--p);color:#fff;border:0;cursor:pointer;border-radius:999px;padding:4px 12px}
form{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}.log{font-size:11px;color:#555;max-height:160px;overflow:auto}
</style><h1>Hush Studio · control</h1><div class=bar id=bar></div>
<form id=f><input name=title placeholder="New task" required size=30><select name=priority><option>1<option>2<option selected>3<option>4</select>
<select name=owner><option>any<option>claude<option>codex<option>sam</select><input name=est_usd type=number step=.01 placeholder="est $" style="width:70px"><button>Add</button></form>
<div class=cols id=cols></div><h3>Log</h3><div class=log id=log></div>
<script>
const ST=["todo","doing","review","blocked","done"],OW=["any","claude","codex","sam"];
const post=(u,d)=>fetch(u,{method:"POST",body:JSON.stringify(d)}).then(r=>r.json()).then(render);
const sel=(o,v,id,k)=>`<select onchange="post('/api/set',{id:'${id}',${k}:this.value})">${o.map(x=>`<option ${x==v?"selected":""}>${x}</option>`).join("")}</select>`;
const esc=s=>String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
function render(s){const m=s.summary,pc=Math.min(100,m.spent/m.cap*100);
bar.innerHTML=`<b>$${m.spent.toFixed(2)}</b> of $${m.cap.toFixed(2)} spent · $${m.remaining.toFixed(2)} left · open estimates $${m.open_estimate.toFixed(2)} · `+Object.entries(m.by_agent).map(([a,v])=>`${a} $${v.toFixed(2)}`).join(", ")+
`<div class=meter><i style="width:${pc}%"></i></div><button onclick="let c=prompt('Budget cap USD',${m.cap});c&&post('/api/cap',{cap:c})">Set cap</button>`;
cols.innerHTML=ST.map(st=>`<div class=col><h3>${st}</h3>`+s.tasks.filter(t=>t.status==st).sort((a,b)=>a.priority-b.priority).map(t=>
`<div class="card p${t.priority}" title="${esc(t.notes)}"><b>${t.id}</b> ${esc(t.title)}<div class=meta>P${t.priority} · est $${t.est_usd.toFixed(2)} · spent $${t.spent_usd.toFixed(2)}${t.depends.length?" · after "+t.depends:""}</div>
<div class=meta>${sel(ST,t.status,t.id,"status")} ${sel(OW,t.owner,t.id,"owner")}</div></div>`).join("")+`</div>`).join("");
log.innerHTML=s.log.slice(-25).reverse().map(l=>`${l.t} <b>${esc(l.who)}</b> ${esc(l.msg)}`).join("<br>")}
f.onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(f));d.priority=+d.priority;d.est_usd=+d.est_usd||0;post("/api/add",d);f.reset()};
const poll=()=>fetch("/api/state").then(r=>r.json()).then(render);poll();
setInterval(()=>{if(!document.activeElement||document.activeElement.tagName!="SELECT")poll()},4000);
</script>"""

if __name__ == "__main__":
    cli(sys.argv[1:])
