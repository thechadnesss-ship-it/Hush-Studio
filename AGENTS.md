# AGENTS.md — Hush Studio (instructions for Codex; Claude Code reads CLAUDE.md)

**Source of truth, in this order:**
1. `CLAUDE.md` — architecture, product rules, measured decisions. Read it fully before changing code.
   The core rule: encode each unique media piece once, build the long video by stream-copy. Never
   re-encode the full-length output without Sam's sign-off.
2. `ops/state.json` — tasks, priorities, budget. **Only edit it through `python ops/control.py`.**
3. The code and `pytest` (run `.venv\Scripts\python -m pytest -q`; must stay green).

## Working protocol (Claude and Codex both follow this)
- Set `AGENT=codex` (Claude uses `AGENT=claude`) so the log shows who did what.
- Start: `python ops/control.py list`, then `python ops/control.py next --owner codex` to claim work.
  Don't touch a task whose status is `doing` under another owner.
- Stay inside the claimed task's scope. If you find new work, `add` a task; don't just do it.
- Before finishing: tests green, then `python ops/control.py done <id> --note "what changed, files"`.
  Anything risky or needing Sam's judgment → `set <id> --status review --owner sam`.
- Record cost after each task: `python ops/control.py spend <id> <usd> --agent codex`.
  Check `python ops/control.py budget`; if remaining budget < the task's estimate, stop and set `blocked`.
- Facts learned (measurements, decisions) go into the matching section of `CLAUDE.md`, not into chat.
- Avoid editing the same file as the other agent's `doing` task; split by module (`core/`, `ui/`, `tests/`).
- Sam's dashboard: `python ops/control.py serve` (or double-click `ops\Control.bat`).
