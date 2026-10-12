# planner-coder

A minimal two-role agent harness: a **planner** that decides what gets built, and **coders** that each build one task in a fresh context.

## Layout

```
agents/
  PLANNER.md   # planner prompt: writes and maintains PLAN.md, never writes product code
  CODER.md     # coder prompt: does one task, stays in scope, returns a result packet
  PLAN.md      # shared task board (template)
  LESSONS.md   # rules learned from earlier runs (template)
```

## How it runs

1. Fill in the `{{...}}` placeholders in `agents/PLAN.md`, starting with the final checks.
2. Run the planner once with `agents/PLANNER.md` and your goal. It fills in `PLAN.md`.
3. For each `todo` task, start a **fresh** coder session with `agents/CODER.md` and the task ID.
4. Give each coder's result packet back to the planner. The planner marks the task done, returns only the failed task, or rewrites a task after 3 failures.

Use a strong model for the planner and a cheaper, faster model for coders.

## Principles

- Every task has checks that run as commands and pass or fail. "Looks good" is not a check.
- Coders never mark their own work done. The planner (or a script) reruns the checks.
- Add a dependency only when a task reads another task's output.
- Lanes: `contained` can merge on green, `wide` needs a human glance, `locked` needs human approval.
- Durable lessons go in `LESSONS.md`, so later runs start smarter.
