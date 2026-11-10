# AGENTS.md — Hush Studio (instructions for Claude Code and Codex)

**Source of truth, in this order:**
1. `CLAUDE.md` — architecture, product rules, measured decisions. Read it fully before changing code.
   The core rule: encode each unique media piece once, build the long video by stream-copy. Never
   re-encode the full-length output without Sam's sign-off.
2. The plan — `Docs/Engine Build Plan/engine_plan.json`, managed by **Narthen Control**
   (repo `Narthen-Control`, local folder `C:\Users\chadj\Narthen-Control`). Tasks, priorities, tiers, decisions.
   Never edit the JSON by hand; use the plan tool below. The plan lives only in the main folder; every worktree
   reads and writes that same file, and the plan tool finds it for you.
3. The code and `pytest`; must stay green. Worktrees have no `.venv` of their own. From inside your worktree run
   `C:\Users\chadj\pika_meditate\.venv\Scripts\python.exe -m pytest -q` (it loads your worktree's code, not the main folder's).
   Do not install packages or create another venv.

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
  review, then Merge & finish). Merge `main` into your branch at the start of a task **and again just before setting status=Review**
  (resolve any conflicts and re-run the tests); otherwise Narthen Control's Merge & finish refuses with a conflict message.
- Do not both edit the same zone at once; stay inside the task's zone (see the plan's zones).

## Review and hand-back
- A reviewer does not change code. It records the outcome with the plan tool (full path, see above):
  clean: `--set <ID> status=Review notes="Review clean: ..."`;
  problems: `--request-changes <ID> "1) ... 2) ..." --by <Claude|Codex>`. That saves the findings on the task and sets
  status `Changes requested`.
- You cannot message the other agent. Never tell Sam "handed back" unless `--request-changes` succeeded; Sam presses
  **Send back** in Narthen Control, which reopens the owner on the same branch with the findings.
- The owner fixes only the listed findings, re-runs Verify and the tests, then `--set <ID> status=Review notes="what was fixed"`
  and a `--log` entry. The task is then re-reviewed.

## Working protocol
- Start: `--next <your name>`, or the task Sam started for you. Read `--show <ID>` and the task's Key files; do not explore the whole repo.
- Respect the usage budget: if `--next` reports you are over budget for the tier, stop and tell Sam.
- Stay inside the claimed task's scope. New work you discover becomes a new task in the plan, not extra work in this one.
- Finish: acceptance criteria met, Verify command run, tests green, commit on your branch, then
  `--set <ID> status=Review notes="..."` and a `--log` entry. Anything needing Sam's judgment: add a decision, or set `status=Blocked` with the reason in notes.
- Facts learned (measurements, decisions) go into the matching section of `CLAUDE.md`, not into chat.
- Deleting files, rewriting git history and changing `CLAUDE.md`'s core rules need Sam's approval.
- Sam's control panel: the **Narthen Control** icon on her desktop.
