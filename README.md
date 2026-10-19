# Agent Loop Stub

A standard-library toolkit for building provider-neutral agent graphs and
durable execution loops.

The repository separates two concerns that are often mixed together:

1. **Graph orchestration** preserves intent across campaigns, experiments,
   dependency-ordered jobs, services, attempts, recovery, and evidence.
2. **Task execution** uses fresh planner and coder processes, isolated Git
   worktrees, declared file scope, executable checks, and driver-owned
   integration decisions.

Use either layer independently or compose them. No agent decides that its own
work passed.

## Architecture

### Durable intent graph

[`graph_loop.py`](docs/graph-loop.md) models:

```text
campaign → experiment → task/service → immutable attempt
```

- A **campaign** preserves the human intent and constraints while the
  investigation evolves.
- An **experiment** tests one hypothesis against explicit acceptance criteria.
- A **task** is finite work that finishes with evidence.
- A **service** may unblock dependent work when ready, but remains unresolved
  until it has stop evidence.
- An **attempt** pins its inputs and fingerprint. Retrying creates a new attempt
  instead of rewriting history.

If an attempt's outcome becomes unknown, the graph marks it `uncertain`. The
original work must be reconciled as running, succeeded, or failed before a retry
can begin. Experiments cannot finish over unresolved jobs, and campaigns cannot
finish over unresolved experiments.

### Planner/coder execution loop

[`driver.py`](driver.py) is one concrete task executor:

```text
goal → planner-owned task DAG → isolated coder worktrees
     → scope and command checks → driver-owned integration
```

The planner writes `agents/plan.json` but never product code. Each fresh coder
handles one task and cannot edit plan state. The driver validates dependencies,
protects the main and integration checkouts, checks actual diffs against
declared scope, runs commands, and decides whether contained work can merge.

The driver supports three task lanes:

| Lane | Behavior |
|---|---|
| `contained` | Merges into the integration branch after checks pass |
| `wide` | Runs checks but remains `needs_review` |
| `locked` | Requires driver-recorded approval, then remains `needs_review` |

## Quick start

Requirements are Python 3.10+ and Git. The shipped implementation uses only the
Python standard library.

```console
python3 -m unittest discover -s tests -q
python3 scripts/check_public_content.py
```

### Run the planner/coder loop

The default adapter invokes GitHub Copilot CLI prompt mode. Edit `driver.toml`
to use another command adapter.

```console
python3 driver.py plan "Add the requested feature"
python3 driver.py status
python3 driver.py approve T3 --by "Reviewer Name"
python3 driver.py run --max-parallel 2
```

Use `python3 driver.py run --dry-run` to inspect readiness without starting
coder processes. The driver never pushes.

### Create a durable graph

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
    acceptance=["The task executor reports green"],
)
graph.start_campaign("release")

attempt = graph.start_attempt(
    "release", "implementation", "code", {"plan_revision": "abc123"}
)
# Run a reviewed executor, then record its actual evidence.
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
assumes one writer. Add an external lock or transactional store when multiple
coordinators may write the same graph.

## Compose the layers

The graph layer does not execute commands, and the task driver does not own a
multi-experiment campaign. A composed caller can:

1. start a graph task attempt with the plan revision and invocation inputs;
2. run `driver.py` or another reviewed executor;
3. attach command results and the integration commit as attempt evidence;
4. reconcile the original executor after interruption before retrying;
5. conclude the experiment only after every task is resolved and every service
   is stopped.

This separation keeps intent, execution, and acceptance independently visible.

## Recovery and safety boundaries

- **One authority for state:** agents propose and implement; the graph store and
  task driver own lifecycle transitions.
- **No success-shaped retries:** uncertain work reconciles before replacement
  work starts.
- **Immutable history:** attempts retain their inputs, fingerprint, outcome, and
  evidence.
- **Explicit cleanup:** a ready service can satisfy dependencies, but cannot be
  treated as finished until stop evidence is recorded.
- **Scoped code changes:** coder diffs must remain within the planner's declared
  paths. Protected paths require exact scope entries.
- **Executable acceptance:** task checks and final checks are commands, not
  agent assertions.
- **No implicit publication:** neither layer pushes changes or provisions
  external resources.
- **Trusted adapters:** command and service adapters still require review. The
  framework is orchestration, not a shell sandbox or credential boundary.

## Planner/coder behavior

`run` creates `agent-integration` from the current `HEAD` if needed. Each task
runs on `task/<id>` in its own directory under the configurable worktree root,
which defaults to `../<repository-name>-worktrees`.

Failed commands and scope violations are recorded in task history, including
the last 40 output lines. A task returns to `todo` for a fresh coder process and
becomes `replan` after three failures. Replanning waits for in-flight work, then
asks the planner to rewrite or split failed tasks. Each task gets at most
`max_replans` planner passes before becoming `blocked`.

Contained tasks use `git merge --no-ff --no-commit`. Checks run on the
integration worktree before the merge commit. Red checks abort the merge
without changing integration history. After task checks and every merge or
abort, the driver restores the worktree to `HEAD` and removes non-ignored
untracked files. Cleanup failures stop the run.

The in-memory plan is authoritative during a command. Agent calls are wrapped
in checkout snapshots: unauthorized main or integration changes are restored
and rejected, planner calls may change only `agents/plan.json`, and coder
changes to plan state are red.

## Plan format

`agents/plan.json` contains `goal`, `final_checks`, and `tasks`. Each task has:

- planner-owned fields: `id`, `title`, `goal`, `lane`, `scope`, `depends_on`,
  `checks`, `notes`, and optional `protected`;
- driver-owned fields: `status`, `attempts`, `history`, `approval`, and
  `replans`.

The driver rejects unknown dependencies, cycles, empty checks, empty scope, and
push commands in checks. Changing a planner-owned field during replanning
revokes a locked task's prior approval. `agents/PLAN.md` is a generated human
view and must not be edited directly.

Python 3.11+ reads `driver.toml` with `tomllib`. Python 3.10 uses the included
parser for the shipped configuration shape. The file contains Copilot CLI
defaults and commented examples for other command-line agents.

## Public-content safety

The repository includes a generic scan for likely credentials, developer home
paths, and non-public URLs:

```console
python3 scripts/check_public_content.py
```

Projects can add private vocabulary without committing or printing it. Keep a
newline-delimited denylist outside the checkout and pass it only at validation
time:

```console
python3 scripts/check_public_content.py --deny-file /private/path/denylist.txt
```

The scanner reads the file but never copies its terms into the repository.
Treat it as one review gate, not proof that arbitrary content is safe to
publish.

## Repository layout

```text
graph_loop.py                   campaign/experiment/job lifecycle
driver.py                       planner/coder worktree executor
driver.toml                     executor adapters and runtime settings
docs/graph-loop.md              graph contract, recovery, and composition
scripts/check_public_content.py publication-safety helper
agents/
  PLANNER.md                    planner prompt
  CODER.md                      coder prompt
  LESSONS.md                    durable project-specific guidance
  plan.json                     machine-readable task plan and state
  PLAN.md                       generated human-readable plan
tests/
  test_graph_loop.py            graph lifecycle and recovery tests
  test_driver.py                task execution and integration tests
```

## Provenance

The graph lifecycle is independently written for this public repository from
general workflow concepts: explicit intent, directed acyclic work, durable
state, immutable attempts, reconciliation, evidence, and cleanup gates. It does
not copy source code, operational configuration, artifacts, or documentation
from another system.
