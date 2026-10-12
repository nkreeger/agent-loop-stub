# Lessons

Rules learned from earlier runs. The planner and every coder read this first.
One line per rule. Only the planner adds lines. Keep the file under ~50 lines, and merge or delete stale rules.

## Codebase
- {{e.g. All API handlers return errors through `utils/errors.py`; never raise raw exceptions.}}

## Process
- {{e.g. Run `make db-reset` before integration tests or they fail on stale fixtures.}}
