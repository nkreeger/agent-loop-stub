# planner-coder

A headless two-role agent harness: a **planner** writes a machine-readable plan,
and fresh **coder** processes implement one task per git worktree. `driver.py`,
not an agent, decides whether work passes.

## Layout

```
driver.py       # standard-library coordinator (Python 3.10+)
driver.toml     # planner/coder CLI adapters and runtime settings
agents/
  PLANNER.md    # planner prompt: writes plan.json, never product code
  CODER.md      # coder prompt: does one task and never edits plan state
  plan.json     # source of truth
  PLAN.md       # generated, read-only human view
  LESSONS.md    # durable rules for future runs
```

## Usage

```console
python3 driver.py plan "Add the requested feature"
python3 driver.py status
python3 driver.py run --max-parallel 2
python3 driver.py run --dry-run
```

The default adapter uses GitHub Copilot CLI prompt mode:
`copilot -p ... --model ... --allow-all-tools --deny-tool=... --no-ask-user`.
These flags were verified with `copilot --help`. Edit `driver.toml` to choose
separate planner and coder models or command templates; commented examples show
`claude -p` and `codex exec`.

`run` creates `agent-integration` from the current `HEAD` if needed. Each task
runs on `task/<id>` in its own directory under `.worktrees/`. The driver checks
the actual git diff against the declared scope and runs every task check.
Contained work is merged only into `agent-integration`; wide and approved
locked work is left unmerged as `needs_review`. The driver never pushes.

Failed commands and scope violations are recorded in `history`, including the
last 40 output lines. A task returns to `todo` for a fresh coder process and
becomes `replan` after three failures. Replanning waits for all in-flight work,
then asks the planner to rewrite or split only failed tasks.

## Plan format

`agents/plan.json` contains a top-level `goal` and `tasks`. Every task has
`id`, `title`, `goal`, `lane`, `scope`, `depends_on`, `checks`, `notes`,
`status`, `attempts`, and `history`. The driver rejects unknown dependencies,
cycles, empty checks, and empty scope. Only the driver transitions `status`,
increments `attempts`, or appends `history`; `agents/PLAN.md` is regenerated
after every state change.

Python 3.11+ reads configuration with the standard-library `tomllib`. Python
3.10 uses the driver's small standard-library parser for the simple shipped
configuration shape.
