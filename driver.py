#!/usr/bin/env python3
"""Headless planner/coder coordinator for Python 3.10+."""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import copy
import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Iterable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent
PLAN_PATH = ROOT / "agents" / "plan.json"
PLAN_VIEW_PATH = ROOT / "agents" / "PLAN.md"
PLANNER_PATH = ROOT / "agents" / "PLANNER.md"
CODER_PATH = ROOT / "agents" / "CODER.md"
LESSONS_PATH = ROOT / "agents" / "LESSONS.md"
CONFIG_PATH = ROOT / "driver.toml"
LANES = {"contained", "wide", "locked"}
STATUSES = {
    "todo",
    "in_progress",
    "done",
    "needs_review",
    "replan",
    "awaiting_approval",
    "conflict",
}
TASK_FIELDS = {
    "id",
    "title",
    "goal",
    "lane",
    "scope",
    "depends_on",
    "checks",
    "notes",
    "status",
    "attempts",
    "history",
}
STATE_FIELDS = {"status", "attempts", "history"}
PRINT_LOCK = threading.Lock()


class DriverError(RuntimeError):
    pass


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        if tomllib is not None:
            with path.open("rb") as handle:
                config = tomllib.load(handle)
        else:
            config = parse_basic_toml(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DriverError(f"cannot load {path}: {exc}") from exc

    for section in ("driver", "planner", "coder"):
        if not isinstance(config.get(section), dict):
            raise DriverError(f"driver.toml is missing [{section}]")
    for role in ("planner", "coder"):
        command = config[role].get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise DriverError(f"[{role}].command must be a non-empty string array")
        if not isinstance(config[role].get("model"), str):
            raise DriverError(f"[{role}].model must be a string")
    return config


def parse_basic_toml(content: str) -> dict[str, Any]:
    config: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        line = strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name or "." in name:
                raise ValueError(f"unsupported section on line {line_number}")
            section = config.setdefault(name, {})
            continue
        if section is None or "=" not in line:
            raise ValueError(f"invalid TOML on line {line_number}")
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError(f"unsupported key on line {line_number}")
        try:
            section[key] = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"unsupported value on line {line_number}") from exc
    return config


def strip_toml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif character == "\\" and quote == '"':
            escaped = True
        elif quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "#":
            return line[:index]
    return line


def load_plan(path: Path = PLAN_PATH) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError(f"cannot load {path}: {exc}") from exc
    validate_plan(plan)
    return plan


def validate_plan(plan: Any) -> None:
    if not isinstance(plan, dict):
        raise DriverError("plan.json must contain an object")
    if set(plan) != {"goal", "tasks"}:
        raise DriverError("plan.json must contain exactly 'goal' and 'tasks'")
    if not isinstance(plan["goal"], str) or not plan["goal"].strip():
        raise DriverError("plan goal must be a non-empty string")
    if not isinstance(plan["tasks"], list):
        raise DriverError("plan tasks must be a list")

    ids: set[str] = set()
    for index, task in enumerate(plan["tasks"]):
        where = f"task #{index + 1}"
        if not isinstance(task, dict) or set(task) != TASK_FIELDS:
            missing = TASK_FIELDS - set(task) if isinstance(task, dict) else TASK_FIELDS
            extra = set(task) - TASK_FIELDS if isinstance(task, dict) else set()
            raise DriverError(
                f"{where} fields are invalid (missing={sorted(missing)}, extra={sorted(extra)})"
            )
        task_id = task["id"]
        if (
            not isinstance(task_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id)
            or task_id in ids
        ):
            raise DriverError(f"{where} has an invalid or duplicate id")
        ids.add(task_id)
        for field in ("title", "goal", "notes"):
            if not isinstance(task[field], str):
                raise DriverError(f"{task_id}.{field} must be a string")
        if not task["title"].strip() or not task["goal"].strip():
            raise DriverError(f"{task_id} title and goal must not be empty")
        if task["lane"] not in LANES:
            raise DriverError(f"{task_id}.lane must be one of {sorted(LANES)}")
        for field in ("scope", "depends_on", "checks", "history"):
            if not isinstance(task[field], list):
                raise DriverError(f"{task_id}.{field} must be a list")
        if not task["scope"] or not all(
            isinstance(item, str) and item.strip() for item in task["scope"]
        ):
            raise DriverError(f"{task_id}.scope must contain at least one path or glob")
        if not task["checks"] or not all(
            isinstance(item, str) and item.strip() for item in task["checks"]
        ):
            raise DriverError(f"{task_id}.checks must contain at least one command")
        if any(
            re.search(r"\bgit(?:\.exe)?\b[^\r\n]*\bpush\b", command, re.IGNORECASE)
            for command in task["checks"]
        ):
            raise DriverError(f"{task_id}.checks must not run git push")
        if not all(isinstance(item, str) and item for item in task["depends_on"]):
            raise DriverError(f"{task_id}.depends_on must contain task IDs")
        if task["status"] not in STATUSES:
            raise DriverError(f"{task_id}.status is invalid")
        if not isinstance(task["attempts"], int) or isinstance(task["attempts"], bool):
            raise DriverError(f"{task_id}.attempts must be an integer")
        if task["attempts"] < 0:
            raise DriverError(f"{task_id}.attempts must not be negative")
        if not all(isinstance(item, dict) for item in task["history"]):
            raise DriverError(f"{task_id}.history must contain objects")

    graph: dict[str, list[str]] = {}
    for task in plan["tasks"]:
        unknown = set(task["depends_on"]) - ids
        if unknown:
            raise DriverError(
                f"{task['id']} has unknown dependencies: {', '.join(sorted(unknown))}"
            )
        graph[task["id"]] = task["depends_on"]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise DriverError(f"dependency cycle detected at {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)


def write_plan(plan: dict[str, Any]) -> None:
    validate_plan(plan)
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(plan, indent=2, ensure_ascii=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=PLAN_PATH.parent, delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(PLAN_PATH)
    render_plan(plan)


def render_plan(plan: dict[str, Any]) -> None:
    lines = [
        "<!-- Generated by driver.py from agents/plan.json. Do not edit. -->",
        f"# Plan: {plan['goal']}",
        "",
        f"**Goal:** {plan['goal']}",
        "",
        "| ID | Status | Lane | Attempts | Depends on | Title |",
        "|---|---|---|---:|---|---|",
    ]
    for task in plan["tasks"]:
        dependencies = ", ".join(task["depends_on"]) or "-"
        lines.append(
            f"| `{task['id']}` | {task['status']} | {task['lane']} | "
            f"{task['attempts']} | {dependencies} | {task['title']} |"
        )
    for task in plan["tasks"]:
        lines.extend(
            [
                "",
                f"## {task['id']}: {task['title']}",
                "",
                f"- **Status:** {task['status']}",
                f"- **Lane:** {task['lane']}",
                f"- **Goal:** {task['goal']}",
                f"- **Scope:** {', '.join(f'`{item}`' for item in task['scope'])}",
                f"- **Depends on:** {', '.join(task['depends_on']) or 'none'}",
                "- **Checks:**",
            ]
        )
        lines.extend(f"  - `{command}`" for command in task["checks"])
        lines.extend(
            [
                f"- **Attempts:** {task['attempts']}",
                f"- **Notes:** {task['notes'] or '-'}",
                "- **History:**",
            ]
        )
        if task["history"]:
            for entry in task["history"]:
                summary = json.dumps(entry, ensure_ascii=True, sort_keys=True)
                lines.append(f"  - `{summary}`")
        else:
            lines.append("  - none")
    PLAN_VIEW_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def find_git() -> str:
    configured = os.environ.get("DRIVER_GIT")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("git")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        matches = sorted(
            Path(local).glob("github-copilot-git-*\\cmd\\git.exe"), reverse=True
        )
        if matches:
            return str(matches[0])
    raise DriverError("git executable not found; set DRIVER_GIT to its full path")


def command_text(command: Iterable[str]) -> str:
    return subprocess.list2cmdline(list(command)) if os.name == "nt" else shlex.join(command)


def print_command(command: Iterable[str], cwd: Path) -> None:
    with PRINT_LOCK:
        print(f"[{cwd}]$ {command_text(command)}")


def run_process(
    command: list[str],
    cwd: Path,
    timeout: int,
    log_path: Path | None = None,
) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = completed.stdout
        return_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        output += f"\nTimed out after {timeout} seconds.\n"
        return_code = 124
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
    return return_code, output


def git(
    git_executable: str,
    args: list[str],
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [git_executable, *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode:
        raise DriverError(
            f"git {' '.join(args)} failed in {cwd}:\n{completed.stdout.rstrip()}"
        )
    return completed


def expand_agent_command(
    role_config: dict[str, Any],
    prompt: str,
    role: str,
    task: dict[str, Any] | None = None,
    attempt: int = 1,
) -> list[str]:
    values = {
        "prompt": prompt,
        "model": role_config["model"],
        "role": role,
        "task_id": task["id"] if task else "planner",
        "attempt": str(attempt),
        "task_json": json.dumps(task or {}, separators=(",", ":")),
    }
    try:
        return [part.format_map(values) for part in role_config["command"]]
    except KeyError as exc:
        raise DriverError(f"unknown command template placeholder: {exc}") from exc


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def planner_prompt(goal: str, replan_tasks: list[dict[str, Any]] | None = None) -> str:
    mode = (
        "Create agents/plan.json for this goal."
        if replan_tasks is None
        else (
            "Rewrite or split only the tasks currently marked replan. Preserve every "
            "other task's planner-owned fields exactly. Do not change status, attempts, "
            "or history; the driver owns those fields.\n\nFailure history:\n"
            + json.dumps(replan_tasks, indent=2)
        )
    )
    return (
        PLANNER_PATH.read_text(encoding="utf-8")
        + "\n\nLESSONS.md:\n"
        + LESSONS_PATH.read_text(encoding="utf-8")
        + f"\n\nGOAL:\n{goal}\n\nDRIVER REQUEST:\n{mode}\n"
    )


def coder_prompt(task: dict[str, Any]) -> str:
    prior = task["history"][-1] if task["history"] else "none"
    return (
        CODER_PATH.read_text(encoding="utf-8")
        + "\n\nTASK JSON:\n"
        + json.dumps(task, indent=2)
        + "\n\nLESSONS.md:\n"
        + LESSONS_PATH.read_text(encoding="utf-8")
        + "\n\nPREVIOUS FAILURE EVIDENCE:\n"
        + json.dumps(prior, indent=2)
        + "\n"
    )


def invoke_planner(
    config: dict[str, Any],
    goal: str,
    run_dir: Path,
    replan_tasks: list[dict[str, Any]] | None = None,
) -> None:
    before = load_plan() if PLAN_PATH.exists() and replan_tasks is not None else None
    prompt = planner_prompt(goal, replan_tasks)
    command = expand_agent_command(config["planner"], prompt, "planner")
    label = "planner-replan-attempt-1.log" if replan_tasks else "planner-plan-attempt-1.log"
    code, output = run_process(
        command,
        ROOT,
        int(config["driver"].get("timeout_seconds", 1200)),
        run_dir / label,
    )
    if code:
        raise DriverError(f"planner exited {code}; see {run_dir / label}")
    candidate = load_plan()
    if before is not None:
        candidate = reconcile_replan(before, candidate)
        write_plan(candidate)
    else:
        write_plan(candidate)
    if output:
        print(f"Planner output saved to {run_dir / label}")


def reconcile_replan(
    before: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    if candidate["goal"] != before["goal"]:
        raise DriverError("planner changed the goal during task-only replanning")
    old_by_id = {task["id"]: task for task in before["tasks"]}
    replanning = {
        task["id"] for task in before["tasks"] if task["status"] == "replan"
    }
    new_by_id = {task["id"]: task for task in candidate["tasks"]}

    for task_id, old in old_by_id.items():
        if task_id in replanning:
            continue
        new = new_by_id.get(task_id)
        if new is None:
            raise DriverError(f"planner removed non-replan task {task_id}")
        for field in TASK_FIELDS - STATE_FIELDS:
            if new[field] != old[field]:
                raise DriverError(f"planner changed non-replan task {task_id}.{field}")
        for field in STATE_FIELDS:
            new[field] = copy.deepcopy(old[field])

    for task in candidate["tasks"]:
        old = old_by_id.get(task["id"])
        if old and task["id"] in replanning:
            changed = any(
                task[field] != old[field] for field in TASK_FIELDS - STATE_FIELDS
            )
            if changed:
                task["status"] = "todo"
                task["attempts"] = 0
                task["history"] = copy.deepcopy(old["history"]) + [
                    {
                        "event": "replanned",
                        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                ]
            else:
                for field in STATE_FIELDS:
                    task[field] = copy.deepcopy(old[field])
        elif old is None:
            task["status"] = "todo"
            task["attempts"] = 0
            task["history"] = []
    validate_plan(candidate)
    return candidate


def task_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["id"]: task for task in plan["tasks"]}


def ready_tasks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = task_map(plan)
    return [
        task
        for task in plan["tasks"]
        if task["status"] == "todo"
        and all(tasks[dependency]["status"] == "done" for dependency in task["depends_on"])
    ]


def safe_name(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", task_id)


def is_approved(task: dict[str, Any]) -> bool:
    return re.search(r"(?m)\bAPPROVED-BY:\s*\S+", task["notes"]) is not None


def branch_exists(git_executable: str, branch: str) -> bool:
    return (
        git(
            git_executable,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            ROOT,
            check=False,
        ).returncode
        == 0
    )


def ensure_worktree(
    git_executable: str, branch: str, path: Path, start_point: str | None = None
) -> None:
    if path.exists():
        actual = git(
            git_executable, ["branch", "--show-current"], path
        ).stdout.strip()
        if actual != branch:
            raise DriverError(
                f"worktree {path} is on {actual or 'detached HEAD'}, expected {branch}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not branch_exists(git_executable, branch):
        if start_point is None:
            raise DriverError(f"branch {branch} does not exist")
        git(git_executable, ["branch", branch, start_point], ROOT)
    git(git_executable, ["worktree", "add", str(path), branch], ROOT)


def changed_files(git_executable: str, worktree: Path, base: str) -> list[str]:
    paths: set[str] = set()
    for args in (
        ["diff", "--name-only", f"{base}...HEAD"],
        ["diff", "--name-only"],
        ["diff", "--name-only", "--cached"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        completed = git(git_executable, args, worktree)
        paths.update(line.strip().replace("\\", "/") for line in completed.stdout.splitlines())
    return sorted(path for path in paths if path)


def path_in_scope(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for raw in patterns:
        pattern = raw.replace("\\", "/")
        while pattern.startswith("./"):
            pattern = pattern[2:]
        if PurePosixPath(normalized).match(pattern):
            return True
        if not any(character in pattern for character in "*?["):
            prefix = pattern.rstrip("/") + "/"
            if normalized.startswith(prefix):
                return True
    return False


def tail(output: str, count: int = 40) -> str:
    return "\n".join(output.splitlines()[-count:])


def dirty_worktree(git_executable: str, worktree: Path) -> bool:
    return bool(git(git_executable, ["status", "--porcelain"], worktree).stdout.strip())


def run_shell_check(command: str, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, output + f"\nTimed out after {timeout} seconds.\n"


def execute_task(
    task: dict[str, Any],
    config: dict[str, Any],
    git_executable: str,
    worktree: Path,
    base: str,
    run_dir: Path,
) -> dict[str, Any]:
    attempt = task["attempts"] + 1
    timeout = int(config["driver"].get("timeout_seconds", 1200))
    command = expand_agent_command(
        config["coder"], coder_prompt(task), "coder", task, attempt
    )
    log_path = run_dir / f"{safe_name(task['id'])}-attempt-{attempt}.log"
    code, output = run_process(command, worktree, timeout, log_path)
    if code:
        return {
            "id": task["id"],
            "verdict": "red",
            "failure": {
                "event": "agent_failed",
                "attempt": attempt,
                "command": command_text(command),
                "exit_code": code,
                "evidence": tail(output),
                "log": str(log_path.relative_to(ROOT)),
            },
        }

    changed = changed_files(git_executable, worktree, base)
    outside = [path for path in changed if not path_in_scope(path, task["scope"])]
    if outside:
        return {
            "id": task["id"],
            "verdict": "red",
            "failure": {
                "event": "scope_failed",
                "attempt": attempt,
                "command": "scope check",
                "exit_code": 1,
                "evidence": "\n".join(outside),
                "log": str(log_path.relative_to(ROOT)),
            },
        }
    if dirty_worktree(git_executable, worktree):
        return {
            "id": task["id"],
            "verdict": "red",
            "failure": {
                "event": "uncommitted_changes",
                "attempt": attempt,
                "command": "git status --porcelain",
                "exit_code": 1,
                "evidence": tail(
                    git(git_executable, ["status", "--porcelain"], worktree).stdout
                ),
                "log": str(log_path.relative_to(ROOT)),
            },
        }
    if not changed:
        return {
            "id": task["id"],
            "verdict": "red",
            "failure": {
                "event": "no_changes",
                "attempt": attempt,
                "command": "git diff --name-only",
                "exit_code": 1,
                "evidence": "coder produced no changes relative to the task base",
                "log": str(log_path.relative_to(ROOT)),
            },
        }

    for check_command in task["checks"]:
        check_code, check_output = run_shell_check(check_command, worktree, timeout)
        if check_code:
            return {
                "id": task["id"],
                "verdict": "red",
                "failure": {
                    "event": "check_failed",
                    "attempt": attempt,
                    "command": check_command,
                    "exit_code": check_code,
                    "evidence": tail(check_output),
                    "log": str(log_path.relative_to(ROOT)),
                },
            }
    return {
        "id": task["id"],
        "verdict": "green",
        "attempt": attempt,
        "branch": f"task/{task['id']}",
        "worktree": str(worktree),
    }


def apply_failure(task: dict[str, Any], failure: dict[str, Any], max_attempts: int) -> None:
    task["attempts"] += 1
    failure["at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    task["history"].append(failure)
    task["status"] = "replan" if task["attempts"] >= max_attempts else "todo"


def merge_task(
    task: dict[str, Any],
    result: dict[str, Any],
    git_executable: str,
    integration_worktree: Path,
) -> None:
    completed = git(
        git_executable,
        ["merge", "--no-ff", "--no-edit", result["branch"]],
        integration_worktree,
        check=False,
    )
    if completed.returncode:
        git(git_executable, ["merge", "--abort"], integration_worktree, check=False)
        task["status"] = "conflict"
        task["history"].append(
            {
                "event": "merge_conflict",
                "attempt": result["attempt"],
                "command": f"git merge --no-ff --no-edit {result['branch']}",
                "exit_code": completed.returncode,
                "evidence": tail(completed.stdout),
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
        return
    task["status"] = "done"


def dry_run(plan: dict[str, Any], config: dict[str, Any], max_parallel: int) -> None:
    simulated = copy.deepcopy(plan)
    integration = config["driver"].get("integration_branch", "agent-integration")
    worktree_root = ROOT / config["driver"].get("worktree_dir", ".worktrees")
    print(f"DRY RUN: no commands will execute and no files will change (parallel={max_parallel})")
    while True:
        ready = ready_tasks(simulated)
        if not ready:
            break
        for task in ready[:max_parallel]:
            if task["lane"] == "locked" and not is_approved(task):
                print(f"{task['id']}: would mark awaiting_approval; coder would not run")
                task["status"] = "awaiting_approval"
                continue
            branch = f"task/{task['id']}"
            worktree = worktree_root / safe_name(task["id"])
            print_command(["git", "branch", branch, integration], ROOT)
            print_command(["git", "worktree", "add", str(worktree), branch], ROOT)
            command = expand_agent_command(
                config["coder"],
                coder_prompt(task),
                "coder",
                task,
                task["attempts"] + 1,
            )
            print_command(command, worktree)
            print(f"[{worktree}]$ scope-check against task base")
            for check_command in task["checks"]:
                print(f"[{worktree}]$ {check_command}")
            if task["lane"] == "contained":
                print_command(
                    ["git", "merge", "--no-ff", "--no-edit", branch],
                    worktree_root / "integration",
                )
                task["status"] = "done"
            else:
                print(f"{task['id']}: would mark needs_review and leave {branch} unmerged")
                task["status"] = "needs_review"
    blocked = [
        task["id"]
        for task in simulated["tasks"]
        if task["status"] == "todo"
    ]
    if blocked:
        print("No more runnable tasks; blocked todo tasks: " + ", ".join(blocked))


def run_command(args: argparse.Namespace) -> None:
    config = load_config()
    plan = load_plan()
    max_parallel = args.max_parallel or int(
        config["driver"].get("max_parallel", 1)
    )
    if max_parallel < 1:
        raise DriverError("--max-parallel must be at least 1")
    if args.dry_run:
        dry_run(plan, config, max_parallel)
        return

    git_executable = find_git()
    integration = str(config["driver"].get("integration_branch", "agent-integration"))
    start_branch = git(git_executable, ["branch", "--show-current"], ROOT).stdout.strip()
    if not start_branch:
        raise DriverError("run requires a named starting branch")
    if integration in {"main", "master", start_branch}:
        raise DriverError(
            "integration_branch must not be main, master, or the starting branch"
        )
    worktree_root = ROOT / str(config["driver"].get("worktree_dir", ".worktrees"))
    integration_worktree = worktree_root / "integration"
    ensure_worktree(git_executable, integration, integration_worktree, "HEAD")
    run_dir = ROOT / "runs" / timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = int(config["driver"].get("max_attempts", 3))

    while True:
        plan = load_plan()
        ready = ready_tasks(plan)
        if not ready:
            break

        runnable: list[tuple[dict[str, Any], Path, str]] = []
        for task in ready:
            live = task_map(plan)[task["id"]]
            if live["lane"] == "locked" and not is_approved(live):
                live["status"] = "awaiting_approval"
                live["history"].append(
                    {
                        "event": "approval_required",
                        "evidence": "notes must contain APPROVED-BY: <name>",
                        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                )
                write_plan(plan)
                continue
            branch = f"task/{live['id']}"
            worktree = worktree_root / safe_name(live["id"])
            integration_head = git(
                git_executable, ["rev-parse", integration], ROOT
            ).stdout.strip()
            ensure_worktree(git_executable, branch, worktree, integration_head)
            base = git(
                git_executable, ["merge-base", branch, integration], ROOT
            ).stdout.strip()
            live["status"] = "in_progress"
            write_plan(plan)
            runnable.append((copy.deepcopy(live), worktree, base))

        if not runnable:
            continue
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_parallel
        ) as executor:
            futures = [
                executor.submit(
                    execute_task,
                    task,
                    config,
                    git_executable,
                    worktree,
                    base,
                    run_dir,
                )
                for task, worktree, base in runnable
            ]
            results = [future.result() for future in futures]

        plan = load_plan()
        by_id = task_map(plan)
        for result in results:
            task = by_id[result["id"]]
            if result["verdict"] == "red":
                apply_failure(task, result["failure"], max_attempts)
            elif task["lane"] == "contained":
                merge_task(task, result, git_executable, integration_worktree)
            else:
                task["status"] = "needs_review"
            write_plan(plan)

        replanning = [task for task in plan["tasks"] if task["status"] == "replan"]
        if replanning:
            invoke_planner(config, plan["goal"], run_dir, replanning)

    remaining = [
        task["id"]
        for task in load_plan()["tasks"]
        if task["status"] not in {"done", "needs_review", "awaiting_approval", "conflict"}
    ]
    if remaining:
        print("No runnable tasks remain: " + ", ".join(remaining))


def plan_command(args: argparse.Namespace) -> None:
    config = load_config()
    run_dir = ROOT / "runs" / timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    invoke_planner(config, args.goal, run_dir)
    print(f"Validated {PLAN_PATH} and regenerated {PLAN_VIEW_PATH}")


def status_command(_: argparse.Namespace) -> None:
    plan = load_plan()
    headers = ("ID", "STATUS", "LANE", "ATTEMPTS", "DEPENDS ON", "TITLE")
    rows = [
        (
            task["id"],
            task["status"],
            task["lane"],
            str(task["attempts"]),
            ",".join(task["depends_on"]) or "-",
            task["title"],
        )
        for task in plan["tasks"]
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        if rows
        else len(headers[index])
        for index in range(len(headers))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a planner/coder agent loop with deterministic code gates."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="ask the planner to create plan.json")
    plan_parser.add_argument("goal", help="goal for the planner")
    plan_parser.set_defaults(handler=plan_command)
    run_parser = subparsers.add_parser("run", help="run all ready tasks")
    run_parser.add_argument("--max-parallel", type=int, default=None)
    run_parser.add_argument(
        "--dry-run", action="store_true", help="print actions without changing anything"
    )
    run_parser.set_defaults(handler=run_command)
    status_parser = subparsers.add_parser("status", help="show task status")
    status_parser.set_defaults(handler=status_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.handler(args)
        return 0
    except DriverError as exc:
        print(f"driver.py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
