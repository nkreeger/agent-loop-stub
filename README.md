# planner-coder

A headless two-role agent harness: a **planner** writes a machine-readable plan,
and fresh **coder** processes implement one task per git worktree. `driver.py`,
not an agent, decides whether work passes.

For work spanning multiple hypotheses or long-lived services,
[`graph_loop.py`](docs/graph-loop.md) adds a provider-neutral
campaign → experiment → job → attempt lifecycle. It records immutable attempts,
requires reconciliation before retrying uncertain work, and prevents completion
until services have stop evidence. It composes with the planner/coder driver
rather than replacing it.

## Choose the right layer

| Need | Use |
|---|---|
| Break one goal into scoped code changes and integrate passing work | `driver.py` |
| Coordinate several hypotheses, tasks, or long-lived services | `graph_loop.py` |
| Do both | Record a graph task attempt, run `driver.py`, then attach its checks and integration commit as evidence |

The graph lifecycle is:

```text
campaign → experiment → task/service → immutable attempt
```

A campaign preserves intent while its experiment graph evolves. Each experiment
tests one hypothesis. Tasks finish with evidence; services may unblock dependent
work when ready but must have stop evidence before an experiment can finish.
If an attempt's outcome becomes unknown, mark it uncertain and reconcile the
original work before starting a retry.

## Layout

```
driver.py                   # planner/coder coordinator (Python 3.10+)
driver.toml                 # planner/coder adapters and runtime settings
graph_loop.py               # campaign/experiment/job lifecycle
docs/graph-loop.md          # graph contract, recovery, and composition
scripts/check_public_content.py
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
python3 driver.py approve T3 --by "Reviewer Name"
python3 driver.py run --max-parallel 2
python3 driver.py run --dry-run
python3 -m unittest discover -s tests -q
python3 scripts/check_public_content.py
```

### Graph quick start

```python
from datetime import datetime, timezone
from pathlib import Path

from graph_loop import GraphStore

graph = GraphStore(
    Path(".agent-loop/graph.json"),
    clock=lambda: datetime.now(timezone.utc).isoformat(),
)
graph.initialize()
graph.create_campaign("release", "Ship a reviewed feature")
graph.add_experiment(
    "release",
    "implementation",
    "The implementation satisfies the request",
    ["All required checks pass"],
)
graph.add_job(
    "release",
    "implementation",
    "code",
    kind="task",
    depends_on=[],
    acceptance=["The task driver reports green"],
)
graph.start_campaign("release")

attempt = graph.start_attempt(
    "release", "implementation", "code", {"plan_revision": "abc123"}
)
# Run a reviewed adapter such as driver.py, then record actual evidence.
graph.finish_task(
    "release",
    "implementation",
    "code",
    attempt,
    succeeded=True,
    evidence=["checks passed at integration commit def456"],
)
graph.finish_experiment(
    "release",
    "implementation",
    outcome="succeeded",
    conclusion="The acceptance checks passed.",
)
graph.finish_campaign("release")
```

`GraphStore` atomically replaces its JSON state and records ordered events. It
assumes one writer; add an external lock or transactional store when multiple
coordinators may write the same graph. See
[`docs/graph-loop.md`](docs/graph-loop.md) for service and interruption recovery
examples.

### Public-content safety

The repository includes a generic scan for likely credentials, developer home
paths, and non-public URLs:

```console
python3 scripts/check_public_content.py
```

Projects can add private vocabulary without committing or printing it. Keep a
newline-delimited denylist outside the checkout and pass it at validation time:

```console
python3 scripts/check_public_content.py --deny-file /private/path/denylist.txt
```

The scanner reads the file but never copies its terms into the repository.
Treat it as one review gate, not proof that arbitrary content is safe to
publish.

The default adapter uses GitHub Copilot CLI prompt mode:
`copilot -p ... --model ... --allow-all-tools --deny-tool=... --no-ask-user`.
These flags were verified with `copilot --help`. Edit `driver.toml` to choose
separate planner and coder models or command templates; commented examples show
`claude -p` and `codex exec`.

`run` creates `agent-integration` from the current `HEAD` if needed. Each task
runs on `task/<id>` in its own directory under the configurable worktree root,
which defaults to `../<repository-name>-worktrees`. The driver checks the
actual git diff against the declared scope and runs every task check.
Contained work is merged only into `agent-integration`; wide and approved
locked work is left unmerged as `needs_review`. The driver never pushes.

Failed commands and scope violations are recorded in `history`, including the
last 40 output lines. A task returns to `todo` for a fresh coder process and
becomes `replan` after three failures. Replanning waits for all in-flight work,
then asks the planner to rewrite or split only failed tasks. Each task gets at
most `max_replans` planner passes before becoming `blocked`.

## Plan format

`agents/plan.json` contains top-level `goal`, `final_checks`, and `tasks`. Every task has
`id`, `title`, `goal`, `lane`, `scope`, `depends_on`, `checks`, `notes`,
`status`, `attempts`, `history`, `approval`, and `replans`. The driver rejects
unknown dependencies, cycles, empty checks, and empty scope. Only the driver
changes state fields; `agents/PLAN.md` is regenerated after every state change.
An optional per-task `protected` list supplements `[driver].protected`
(default `["tests/**"]`). Protected files require exact paths in `scope`;
scope globs or directory entries do not authorize changes to them.
Replanning that changes any planner-owned task field revokes its approval.

The in-memory plan is authoritative while a command runs. Agent calls are
wrapped in checkout snapshots: unauthorized main/integration changes are
restored and rejected, planner calls may change only `agents/plan.json`, and
coder changes to plan state are RED. Each coder batch shares one pair of
snapshots taken and verified on the main thread. Snapshots store recovery
copies on disk, compare Git and file metadata first, and hash only candidate
files. A batch with unauthorized changes is rejected before any merge.

Contained tasks use `git merge --no-ff --no-commit`, then run their checks on
the integration worktree. Green checks allow the merge commit; red checks
abort the merge without changing integration history, so a later retry can
merge normally. `final_checks` run after all tasks are done.

After task checks and after each integration merge commit or abort, the driver
restores the worktree using `git reset --hard HEAD` and `git clean -fd`.
Ignored files are preserved. Integration must be clean before every merge;
cleanup failures stop the run with an error.

Python 3.11+ reads configuration with the standard-library `tomllib`. Python
3.10 uses the driver's small standard-library parser for the simple shipped
configuration shape.
