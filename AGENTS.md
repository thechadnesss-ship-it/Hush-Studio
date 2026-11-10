# AGENTS.md — Hush Studio (instructions for Claude Code and Codex)

**Source of truth, in this order:**
1. `CLAUDE.md` — architecture, product rules, measured decisions. Read it fully before changing code.
   The core rule: encode each unique media piece once, build the long video by stream-copy. Never
   re-encode the full-length output without Sam's sign-off.
2. The plan — `Docs/Engine Build Plan/engine_plan.json`, managed by **Narthen Control**
   (repo `Narthen-Control`, local folder `C:\Users\chadj\Narthen-Control`). Tasks, priorities, tiers, decisions.
   Never edit the JSON by hand; use the plan tool below. The plan lives only in the main folder; every worktree
   reads and writes that same file, and the plan tool finds it for you.
3. The code and `pytest` (`.venv\Scripts\python -m pytest -q`; must stay green).

## Plan tool
Always call it by full path (it is not inside this repo), e.g.:

    py "C:\Users\chadj\Narthen-Control\Tools\build_engine_plan.py" --show T7
    py "C:\Users\chadj\Narthen-Control\Tools\build_engine_plan.py" --next Claude      # or Codex
    py "C:\Users\chadj\Narthen-Control\Tools\build_engine_plan.py" --set T7 status=Review notes="what changed; how verified"
    py "C:\Users\chadj\Narthen-Control\Tools\build_engine_plan.py" --log Claude "summary" --tasks T7

If `NARTHEN_PROJECT` is not already set in your shell, set it to `C:\Users\chadj\pika_meditate` first.
Never commit `engine_plan.json` on a task branch; it is committed on `main` only.

## Git: who works where
- One task per branch, `<agent>/<task id>-<slug>` (Narthen Control creates it when Sam presses Start).
- Each agent works in its own worktree beside the project folder: `C:\Users\chadj\hush-studio-claude` and
  `C:\Users\chadj\hush-studio-codex`. Narthen Control creates them on first use. Do not work in the main
  folder unless the task says so.
- Never commit to `main` from a task branch. Sam merges to `main` after review (Narthen Control: Run code
  review, then Merge & finish). Merge `main` into your branch at the start of a task.
- Do not both edit the same zone at once; stay inside the task's zone (see the plan's zones).

## Working protocol
- Start: `--next <your name>`, or the task Sam started for you. Read `--show <ID>` and the task's Key files; do not explore the whole repo.
- Respect the usage budget: if `--next` reports you are over budget for the tier, stop and tell Sam.
- Stay inside the claimed task's scope. New work you discover becomes a new task in the plan, not extra work in this one.
- Finish: acceptance criteria met, Verify command run, tests green, commit on your branch, then
  `--set <ID> status=Review notes="..."` and a `--log` entry. Anything needing Sam's judgment: add a decision, or set `status=Blocked` with the reason in notes.
- Facts learned (measurements, decisions) go into the matching section of `CLAUDE.md`, not into chat.
- Deleting files, rewriting git history and changing `CLAUDE.md`'s core rules need Sam's approval.
- Sam's control panel: the **Narthen Control** icon on her desktop.
