#!/usr/bin/env python
"""Claude Code statusLine command: records the plan limits Claude Code reports to ops/claude_usage.json
so the control app can show LIVE Claude 5-hour / weekly usage. Prints a short status line too."""
import json, sys
from pathlib import Path

try:
    d = json.load(sys.stdin)
except ValueError:
    d = {}
here = Path(__file__).resolve().parent
try:
    import subprocess
    g = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=here,
                       capture_output=True, text=True, timeout=5).stdout.strip()
    out = (Path(g).parent / "ops") if g else here
except Exception:
    out = here
rl = d.get("rate_limits") or {}
if rl:
    try:
        (out / "claude_usage.json").write_text(json.dumps({"rate_limits": rl}), encoding="utf-8")
    except OSError:
        pass
f, w = rl.get("five_hour") or {}, rl.get("seven_day") or {}
parts = [(d.get("model") or {}).get("display_name", "Claude")]
if f: parts.append(f"5h {f.get('used_percentage', '?')}%")
if w: parts.append(f"wk {w.get('used_percentage', '?')}%")
print(" | ".join(parts))
