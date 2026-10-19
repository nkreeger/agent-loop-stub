# Campaign graphs and durable loops

`graph_loop.py` is a provider-neutral lifecycle layer for work that is larger
than one planner/coder pass. It complements `driver.py`; it does not replace
the existing worktree and scope enforcement.

## Graph

```text
campaign
  intent + constraints
  ├── retained service
  │     immutable attempts + ready/reconcile/stop lifecycle
  └── experiment
        hypothesis + acceptance + conclusion
        └── job
              task | service
              dependencies + acceptance
              └── attempt
                    immutable inputs + fingerprint + evidence
```

- A **campaign** preserves the human intent while the implementation graph
  evolves.
- An **experiment** tests one hypothesis and records a bounded conclusion.
- A **task** is a finite unit of work.
- A **service** may unblock dependent work when ready, but remains unresolved
  until it has stop evidence.
- A **campaign service** represents an environment shared by multiple
  experiments. It remains ready after an experiment concludes and blocks
  campaign completion until explicit stop evidence is recorded.
- An **attempt** is immutable history. A retry creates another attempt rather
  than rewriting the failed or interrupted one.

Dependencies name jobs in the same experiment. A successful task satisfies a
dependency. A service satisfies dependencies while it is ready, but the
experiment cannot finish until that service is stopped.

## Loop

```text
record intent
  -> add a bounded experiment and jobs
  -> start a ready job with immutable inputs
  -> record evidence
  -> succeed, fail, or mark the attempt uncertain
  -> reconcile uncertain work before retrying
  -> stop services
  -> conclude the experiment
  -> continue with the next evidence-driven experiment or finish
```

The state file includes an ordered event history and is replaced atomically.
`GraphStore` assumes one coordinator writes a state file; a deployment that
permits multiple coordinators must add an external lock or transactional store.
The module intentionally does not choose an agent provider, provision resources,
execute commands, authorize credentials, or publish changes. Callers own those
policy and adapter decisions.

`CampaignRuntime` supplies the local single-writer lock, external state
directory, append-only event journal, terminal-preserving adapter protocol and
timezone-aware status loop. It does not weaken the graph's reconciliation
rules.

## Retained environment flow

```text
authenticate
  -> create one original resource
  -> starting
  -> ready
  -> experiment A
  -> experiment B
  -> explicit stop
  -> campaign completion
```

Experiment completion never stops a campaign service. If create, status or
stop loses its result, the service becomes `reconciling`; a replacement
attempt cannot begin until the original identity is observed and recorded as
starting, ready, failed or stopped.

## Example

```python
from datetime import datetime, timezone
from pathlib import Path

from graph_loop import GraphStore

clock = lambda: datetime.now(timezone.utc).isoformat()
graph = GraphStore(Path(".agent-loop/graph.json"), clock=clock)
graph.initialize()
graph.create_campaign("release", "Ship a reviewed feature")
graph.add_experiment(
    "release",
    "implementation",
    "The proposed implementation satisfies the request",
    ["Unit and integration checks pass"],
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
    "release",
    "implementation",
    "code",
    {"plan_revision": "abc123"},
)
# Run `driver.py` or another reviewed adapter, then record its evidence.
graph.finish_task(
    "release",
    "implementation",
    "code",
    attempt,
    succeeded=True,
    evidence=["task checks passed"],
)
graph.finish_experiment(
    "release",
    "implementation",
    outcome="succeeded",
    conclusion="The implementation met the stated acceptance checks.",
)
graph.finish_campaign("release")
```

## Recovery contract

Process disappearance is not evidence that remote work stopped. When an
attempt's outcome is unknown, call `mark_uncertain`. The job becomes
`reconciling` and cannot start another attempt. A caller must inspect the
original work and call `reconcile_attempt` with evidence:

- `running`: continue observing the original attempt;
- `succeeded`: retain its result;
- `failed`: close it before starting a fresh attempt.

This contract prevents duplicate retries after a coordinator interruption.

## Composition with the task driver

Use a graph task attempt to pin the planner/coder plan revision and invocation
inputs. Let `driver.py` execute its normal scoped worktree loop. Record the
driver's check result and integration commit as attempt evidence.

For a long-lived helper, register a service instead. Mark it ready only after a
real health check, allow dependent tasks to run, and stop it with process or
adapter evidence before concluding the experiment.

The two layers keep separate responsibilities:

| Layer | Owns |
|---|---|
| `graph_loop.py` | Intent hierarchy, dependency readiness, attempts, recovery, service lifecycle, terminal gates |
| `driver.py` | Planner/coder prompts, worktrees, file scope, checks, retries, integration |

## Provenance

This module is independently written for this public repository from general
workflow concepts: explicit intent, directed acyclic work, durable state,
immutable attempts, reconciliation, evidence, and cleanup gates. It does not
copy source code, operational configuration, artifacts, or documentation from
another system.
