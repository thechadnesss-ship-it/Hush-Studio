# AGENTS.md — Hush Studio (instructions for Codex; Claude Code reads CLAUDE.md)

**Source of truth, in this order:**
1. `CLAUDE.md` — architecture, product rules, measured decisions. Read it fully before changing code.
   The core rule: encode each unique media piece once, build the long video by stream-copy. Never
   re-encode the full-length output without Sam's sign-off.
2. Tasks / priorities / budget — `python ops/control.py` (state lives in the **main checkout's**
   `ops/state.json`; every worktree reads and writes that same file). Never edit the JSON by hand.
3. The code and `pytest` (`.venv\Scripts\python -m pytest -q`; must stay green).

## Git: who works where
- **Claude Code** works in `C:\Users\chadj\pika_meditate` on branch `main`.
- **Codex** works in the worktree `C:\Users\chadj\pika_meditate_codex` on branch `codex`. Never commit to `main`.
- Codex: commit small and often on `codex`. Sam (or Claude) merges `codex` → `main` after review;
  Codex pulls changes with `git merge main` at the start of each task.
- Don't both edit the same module at once: split by owner (see the task list), and say in `notes` which files a task touches.

## Working protocol (Claude and Codex both follow this)
- Set `AGENT=codex` (Claude uses `AGENT=claude`) so the log shows who did what.
- Start: `python ops/control.py list`, then `python ops/control.py next --owner codex` to claim work.
  `next` refuses with `STOP` when your subscription usage is at/over the caps (default weekly 90%, 5 h 95%):
  stop and tell Sam, don't work around it. `python ops/control.py usage` shows live numbers.
- Don't touch a task whose status is `doing` under another owner. Stay inside the claimed task's scope;
  new work you discover → `add` a task, don't just do it.
- Finish: tests green, commit on your branch, then `python ops/control.py done <id> --note "what changed, files"`.
  The app records how much weekly allowance the task consumed automatically. Anything risky or needing
  Sam's judgment → `set <id> --status review --owner sam`.
- If your weekly usage is high, prefer small, well-scoped tasks and say so in the task notes when you're blocked by budget.
- Facts learned (measurements, decisions) go into the matching section of `CLAUDE.md`, not into chat.
- Sam's dashboard: double-click `ops\Control.bat` (live Claude + Codex usage, task board, caps).
