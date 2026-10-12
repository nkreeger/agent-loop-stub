#!/usr/bin/env python3
"""Headless planner/coder coordinator for Python 3.10+."""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
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
    "blocked",
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
    "approval",
    "replans",
}
STATE_FIELDS = {"status", "attempts", "history", "approval", "replans"}
OPTIONAL_TASK_FIELDS = {"protected"}
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
    protected = config["driver"].get("protected", ["tests/**"])
    if not isinstance(protected, list) or not all(
        isinstance(pattern, str) and pattern.strip() for pattern in protected
    ):
        raise DriverError("[driver].protected must be a list of non-empty patterns")
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
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError(f"cannot load {path}: {exc}") from exc
    validate_plan(plan)
    return plan


def parse_plan_bytes(payload: bytes, source: str) -> dict[str, Any]:
    try:
        plan = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriverError(f"invalid plan from {source}: {exc}") from exc
    validate_plan(plan)
    return plan


def validate_plan(plan: Any) -> None:
    if not isinstance(plan, dict):
        raise DriverError("plan.json must contain an object")
    if set(plan) != {"goal", "final_checks", "tasks"}:
        raise DriverError(
            "plan.json must contain exactly 'goal', 'final_checks', and 'tasks'"
        )
    if not isinstance(plan["goal"], str) or not plan["goal"].strip():
        raise DriverError("plan goal must be a non-empty string")
    if not isinstance(plan["final_checks"], list) or not all(
        isinstance(command, str) and command.strip()
        for command in plan["final_checks"]
    ):
        raise DriverError("plan final_checks must be a list of non-empty commands")
    if any(
        re.search(r"\bgit(?:\.exe)?\b[^\r\n]*\bpush\b", command, re.IGNORECASE)
        for command in plan["final_checks"]
    ):
        raise DriverError("plan final_checks must not run git push")
    if not isinstance(plan["tasks"], list):
        raise DriverError("plan tasks must be a list")

    ids: set[str] = set()
    for index, task in enumerate(plan["tasks"]):
        where = f"task #{index + 1}"
        if (
            not isinstance(task, dict)
            or not TASK_FIELDS <= set(task)
            or set(task) - TASK_FIELDS - OPTIONAL_TASK_FIELDS
        ):
            missing = TASK_FIELDS - set(task) if isinstance(task, dict) else TASK_FIELDS
            extra = (
                set(task) - TASK_FIELDS - OPTIONAL_TASK_FIELDS
                if isinstance(task, dict)
                else set()
            )
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
        if "protected" in task and (
            not isinstance(task["protected"], list)
            or not all(
                isinstance(pattern, str) and pattern.strip()
                for pattern in task["protected"]
            )
        ):
            raise DriverError(f"{task_id}.protected must be a list of non-empty patterns")
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
        if not isinstance(task["replans"], int) or isinstance(task["replans"], bool):
            raise DriverError(f"{task_id}.replans must be an integer")
        if task["replans"] < 0:
            raise DriverError(f"{task_id}.replans must not be negative")
        approval = task["approval"]
        if approval is not None and (
            not isinstance(approval, dict)
            or set(approval) != {"by", "at"}
            or not isinstance(approval["by"], str)
            or not approval["by"].strip()
            or not isinstance(approval["at"], str)
        ):
            raise DriverError(f"{task_id}.approval must be null or a by/at object")
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
        "**Final checks:**",
    ]
    lines.extend(f"- `{command}`" for command in plan["final_checks"])
    if not plan["final_checks"]:
        lines.append("- none")
    lines.extend(
        [
            "",
            "| ID | Status | Lane | Attempts | Replans | Depends on | Title |",
            "|---|---|---|---:|---:|---|---|",
        ]
    )
    for task in plan["tasks"]:
        dependencies = ", ".join(task["depends_on"]) or "-"
        lines.append(
            f"| `{task['id']}` | {task['status']} | {task['lane']} | "
            f"{task['attempts']} | {task['replans']} | {dependencies} | "
            f"{task['title']} |"
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
                f"- **Protected:** {', '.join(task.get('protected', [])) or 'driver defaults only'}",
                f"- **Depends on:** {', '.join(task['depends_on']) or 'none'}",
                "- **Checks:**",
            ]
        )
        lines.extend(f"  - `{command}`" for command in task["checks"])
        lines.extend(
            [
                f"- **Attempts:** {task['attempts']}",
                f"- **Replans:** {task['replans']}",
                f"- **Approval:** {task['approval']['by'] if task['approval'] else '-'}",
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
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
            creationflags=creation_flags,
        )
        output, _ = process.communicate(timeout=timeout)
        return_code = process.returncode
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        output, _ = process.communicate()
        output += f"\nTimed out after {timeout} seconds.\n"
        return_code = 124
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
    return return_code, output


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)


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


def visible_files(git_executable: str, checkout: Path) -> list[str]:
    completed = subprocess.run(
        [
            git_executable,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=checkout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode:
        raise DriverError(
            f"cannot list checkout files in {checkout}: "
            f"{completed.stdout.decode(errors='replace')}"
        )
    return [
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in completed.stdout.split(b"\0")
        if item
    ]


def capture_checkout(git_executable: str, checkout: Path) -> dict[str, Any]:
    backup = tempfile.TemporaryDirectory(prefix="driver-checkout-")
    try:
        files: dict[str, tuple[int, int]] = {}
        for path in visible_files(git_executable, checkout):
            source = checkout / Path(path)
            if source.is_file():
                stat = source.stat()
                files[path] = (stat.st_size, stat.st_mtime_ns)
                target = Path(backup.name) / Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        return {
            "checkout": checkout,
            "head": git(git_executable, ["rev-parse", "HEAD"], checkout).stdout.strip(),
            "branch": git(
                git_executable, ["branch", "--show-current"], checkout
            ).stdout.strip(),
            "index": git(git_executable, ["write-tree"], checkout).stdout.strip(),
            "status": git(git_executable, ["status", "--porcelain=v1", "-z"], checkout).stdout,
            "files": files,
            "backup": backup,
        }
    except BaseException:
        backup.cleanup()
        raise


def file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def checkout_changes(
    git_executable: str, snapshot: dict[str, Any]
) -> list[str]:
    checkout = snapshot["checkout"]
    current_paths = set(visible_files(git_executable, checkout))
    old_paths = set(snapshot["files"])
    changes = current_paths ^ old_paths
    status = git(
        git_executable, ["status", "--porcelain=v1", "-z"], checkout
    ).stdout
    index = git(git_executable, ["write-tree"], checkout).stdout.strip()
    git_metadata_changed = status != snapshot["status"] or index != snapshot["index"]
    status_paths: set[str] = set()
    if git_metadata_changed:
        # Include both source and destination of porcelain rename/copy records.
        for record in (status + snapshot["status"]).split("\0"):
            if record:
                status_paths.add(record[3:] if len(record) > 2 and record[2] == " " else record)
    for path in current_paths & old_paths:
        current = checkout / Path(path)
        if not current.is_file():
            changes.add(path)
            continue
        stat = current.stat()
        metadata = (stat.st_size, stat.st_mtime_ns)
        if metadata != snapshot["files"][path] or path in status_paths:
            original = Path(snapshot["backup"].name) / Path(path)
            if file_digest(current) != file_digest(original):
                changes.add(path)
    if git(git_executable, ["rev-parse", "HEAD"], checkout).stdout.strip() != snapshot["head"]:
        changes.add("<HEAD>")
    if index != snapshot["index"]:
        changes.add("<INDEX>")
    branch = git(git_executable, ["branch", "--show-current"], checkout).stdout.strip()
    if branch != snapshot["branch"]:
        changes.add("<BRANCH>")
    return sorted(changes)


def restore_checkout(
    git_executable: str, snapshot: dict[str, Any], changes: list[str]
) -> None:
    checkout = snapshot["checkout"]
    current_paths = set(visible_files(git_executable, checkout))
    old_paths = set(snapshot["files"])
    for path in current_paths - old_paths:
        candidate = checkout / Path(path)
        if candidate.is_file() or candidate.is_symlink():
            candidate.unlink()
    for path in old_paths & set(changes):
        target = checkout / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(snapshot["backup"].name) / Path(path), target)
    if snapshot["branch"]:
        git(
            git_executable,
            ["symbolic-ref", "HEAD", f"refs/heads/{snapshot['branch']}"],
            checkout,
        )
        git(
            git_executable,
            ["update-ref", f"refs/heads/{snapshot['branch']}", snapshot["head"]],
            checkout,
        )
    git(git_executable, ["read-tree", snapshot["index"]], checkout)


def verify_and_restore_checkout(
    git_executable: str,
    snapshot: dict[str, Any],
    allowed: set[str] | None = None,
) -> list[str]:
    try:
        changes = checkout_changes(git_executable, snapshot)
        unauthorized = [
            path for path in changes if allowed is None or path not in allowed
        ]
        if changes:
            restore_checkout(git_executable, snapshot, changes)
        return unauthorized
    finally:
        snapshot["backup"].cleanup()


def configured_worktree_root(config: dict[str, Any]) -> Path:
    configured = config["driver"].get("worktree_dir")
    if configured:
        path = Path(str(configured))
        return path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    return ROOT.parent / f"{ROOT.name}-worktrees"


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
            "other task's planner-owned fields and final_checks exactly. Do not change "
            "status, attempts, history, approval, or replans; the driver owns those "
            "fields.\n\nFailure history:\n"
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
    plan: dict[str, Any],
    run_dir: Path,
    git_executable: str,
    replan_tasks: list[dict[str, Any]] | None = None,
    integration_worktree: Path | None = None,
) -> dict[str, Any]:
    prompt = planner_prompt(plan["goal"], replan_tasks)
    command = expand_agent_command(config["planner"], prompt, "planner")
    label = "planner-replan-attempt-1.log" if replan_tasks else "planner-plan-attempt-1.log"
    main_snapshot = capture_checkout(git_executable, ROOT)
    integration_snapshot = (
        capture_checkout(git_executable, integration_worktree)
        if integration_worktree and integration_worktree.exists()
        else None
    )
    code, output = run_process(
        command,
        ROOT,
        int(config["driver"].get("timeout_seconds", 1200)),
        run_dir / label,
    )
    candidate_payload = PLAN_PATH.read_bytes() if PLAN_PATH.exists() else b""
    main_violations = verify_and_restore_checkout(
        git_executable, main_snapshot, {"agents/plan.json"}
    )
    integration_violations = (
        verify_and_restore_checkout(git_executable, integration_snapshot)
        if integration_snapshot
        else []
    )
    if main_violations or integration_violations:
        evidence = ", ".join(main_violations + integration_violations)
        raise DriverError(
            f"planner changed files outside agents/plan.json: {evidence}"
        )
    if code:
        raise DriverError(f"planner exited {code}; see {run_dir / label}")
    candidate = parse_plan_bytes(candidate_payload, "planner")
    if replan_tasks is not None:
        candidate = reconcile_replan(plan, candidate)
    else:
        for task in candidate["tasks"]:
            task["status"] = "todo"
            task["attempts"] = 0
            task["history"] = []
            task["approval"] = None
            task["replans"] = 0
        validate_plan(candidate)
    write_plan(candidate)
    if output:
        print(f"Planner output saved to {run_dir / label}")
    return candidate


def reconcile_replan(
    before: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    if candidate["goal"] != before["goal"]:
        raise DriverError("planner changed the goal during task-only replanning")
    if candidate["final_checks"] != before["final_checks"]:
        raise DriverError("planner changed final_checks during task-only replanning")
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
        for field in (TASK_FIELDS | OPTIONAL_TASK_FIELDS) - STATE_FIELDS:
            if new.get(field) != old.get(field):
                raise DriverError(f"planner changed non-replan task {task_id}.{field}")
        for field in STATE_FIELDS:
            new[field] = copy.deepcopy(old[field])

    for task in candidate["tasks"]:
        old = old_by_id.get(task["id"])
        if old and task["id"] in replanning:
            changed = any(
                task.get(field) != old.get(field)
                for field in (TASK_FIELDS | OPTIONAL_TASK_FIELDS) - STATE_FIELDS
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
                task["approval"] = None
                task["replans"] = old["replans"]
            else:
                for field in STATE_FIELDS:
                    task[field] = copy.deepcopy(old[field])
        elif old is None:
            task["status"] = "todo"
            task["attempts"] = 0
            task["history"] = []
            task["approval"] = None
            task["replans"] = 0
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
        if pattern.endswith("/**") and not any(
            character in pattern[:-3] for character in "*?["
        ):
            if normalized.startswith(pattern[:-2]):
                return True
        if PurePosixPath(normalized).match(pattern):
            return True
        if not any(character in pattern for character in "*?["):
            prefix = pattern.rstrip("/") + "/"
            if normalized.startswith(prefix):
                return True
    return False


def path_explicitly_in_scope(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        not any(character in pattern for character in "*?[")
        and normalized == pattern.replace("\\", "/").removeprefix("./")
        for pattern in patterns
    )


def protected_check_changes(
    changed: list[str], task: dict[str, Any], config: dict[str, Any]
) -> list[str]:
    patterns = config["driver"].get("protected", ["tests/**"]) + task.get("protected", [])
    return [
        path
        for path in changed
        if path_in_scope(path, patterns)
        and not path_explicitly_in_scope(path, task["scope"])
    ]


def tail(output: str, count: int = 40) -> str:
    return "\n".join(output.splitlines()[-count:])


def dirty_worktree(git_executable: str, worktree: Path) -> bool:
    return bool(git(git_executable, ["status", "--porcelain"], worktree).stdout.strip())


def run_shell_check(command: str, cwd: Path, timeout: int) -> tuple[int, str]:
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
            creationflags=creation_flags,
        )
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        output, _ = process.communicate()
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
    protected = protected_check_changes(changed, task, config)
    if protected:
        return {
            "id": task["id"],
            "verdict": "red",
            "failure": {
                "event": "check_protection_failed",
                "attempt": attempt,
                "command": "check file protection",
                "exit_code": 1,
                "evidence": "\n".join(protected),
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
    timeout: int,
) -> dict[str, Any] | None:
    completed = git(
        git_executable,
        ["merge", "--no-ff", "--no-commit", result["branch"]],
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
                "command": f"git merge --no-ff --no-commit {result['branch']}",
                "exit_code": completed.returncode,
                "evidence": tail(completed.stdout),
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
        return None
    for check_command in task["checks"]:
        check_code, check_output = run_shell_check(
            check_command, integration_worktree, timeout
        )
        if check_code:
            git(git_executable, ["merge", "--abort"], integration_worktree)
            return {
                "event": "integration_check_failed",
                "attempt": result["attempt"],
                "command": check_command,
                "exit_code": check_code,
                "evidence": tail(check_output),
                "branch": result["branch"],
            }
    git(git_executable, ["commit", "--no-edit"], integration_worktree)
    task["status"] = "done"
    return None


def run_final_checks(
    plan: dict[str, Any], integration_worktree: Path, timeout: int
) -> None:
    for command in plan["final_checks"]:
        code, output = run_shell_check(command, integration_worktree, timeout)
        if code:
            print(f"FINAL CHECK RED: {command}\n{tail(output)}")
            raise DriverError(f"final check failed: {command}")
        print(f"FINAL CHECK GREEN: {command}")


def recover_interrupted_tasks(plan: dict[str, Any]) -> bool:
    changed = False
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for task in plan["tasks"]:
        if task["status"] == "in_progress":
            task["status"] = "todo"
            task["history"].append(
                {
                    "event": "recovered_interrupted_run",
                    "evidence": "reset in_progress to todo at run startup",
                    "at": now,
                }
            )
            changed = True
    return changed


def dry_run(plan: dict[str, Any], config: dict[str, Any], max_parallel: int) -> None:
    simulated = copy.deepcopy(plan)
    integration = config["driver"].get("integration_branch", "agent-integration")
    worktree_root = configured_worktree_root(config)
    print(f"DRY RUN: no commands will execute and no files will change (parallel={max_parallel})")
    while True:
        ready = ready_tasks(simulated)
        if not ready:
            break
        for task in ready[:max_parallel]:
            if task["lane"] == "locked" and task["approval"] is None:
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
                    ["git", "merge", "--no-ff", "--no-commit", branch],
                    worktree_root / "integration",
                )
                for check_command in task["checks"]:
                    print(f"[{worktree_root / 'integration'}]$ {check_command}")
                print_command(
                    ["git", "commit", "--no-edit"], worktree_root / "integration"
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
    elif all(task["status"] == "done" for task in simulated["tasks"]):
        for command in simulated["final_checks"]:
            print(f"[{worktree_root / 'integration'}]$ {command}")


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
    worktree_root = configured_worktree_root(config)
    if worktree_root == ROOT or ROOT in worktree_root.parents:
        raise DriverError("worktree_dir must be outside the repository")
    integration_worktree = worktree_root / "integration"
    ensure_worktree(git_executable, integration, integration_worktree, "HEAD")
    run_dir = ROOT / "runs" / timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = int(config["driver"].get("max_attempts", 3))
    max_replans = int(config["driver"].get("max_replans", 2))
    timeout = int(config["driver"].get("timeout_seconds", 1200))
    if recover_interrupted_tasks(plan):
        write_plan(plan)

    while True:
        replanning = [task for task in plan["tasks"] if task["status"] == "replan"]
        if replanning:
            allowed: list[dict[str, Any]] = []
            for task in replanning:
                if task["replans"] >= max_replans:
                    task["status"] = "blocked"
                    task["history"].append(
                        {
                            "event": "replan_limit_reached",
                            "evidence": f"max_replans={max_replans}",
                            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        }
                    )
                else:
                    task["replans"] += 1
                    allowed.append(task)
            write_plan(plan)
            if allowed:
                plan = invoke_planner(
                    config,
                    plan,
                    run_dir,
                    git_executable,
                    allowed,
                    integration_worktree,
                )
                continue

        ready = ready_tasks(plan)
        if not ready:
            break

        runnable: list[tuple[dict[str, Any], Path, str]] = []
        for task in ready:
            live = task_map(plan)[task["id"]]
            if live["lane"] == "locked" and live["approval"] is None:
                live["status"] = "awaiting_approval"
                live["history"].append(
                    {
                        "event": "approval_required",
                        "evidence": "run driver.py approve <id> --by <name>",
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
        main_snapshot = capture_checkout(git_executable, ROOT)
        integration_snapshot = capture_checkout(git_executable, integration_worktree)
        try:
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
        finally:
            try:
                main_violations = verify_and_restore_checkout(git_executable, main_snapshot)
            finally:
                integration_violations = verify_and_restore_checkout(
                    git_executable, integration_snapshot
                )

        by_id = task_map(plan)
        violations = main_violations + integration_violations
        if violations:
            # A parallel batch cannot reliably attribute shared-checkout mutations.
            # Reject the entire batch before any merge or success transition.
            for result in results:
                task = by_id[result["id"]]
                apply_failure(
                    task,
                    {
                        "event": "state_integrity_failed",
                        "attempt": task["attempts"] + 1,
                        "command": "batch checkout integrity check",
                        "exit_code": 1,
                        "evidence": "\n".join(violations),
                    },
                    max_attempts,
                )
                write_plan(plan)
            raise DriverError("agent changed a protected checkout; run stopped")
        for result in results:
            task = by_id[result["id"]]
            if result["verdict"] == "red":
                apply_failure(task, result["failure"], max_attempts)
            elif task["lane"] == "contained":
                failure = merge_task(
                    task,
                    result,
                    git_executable,
                    integration_worktree,
                    timeout,
                )
                if failure:
                    apply_failure(task, failure, max_attempts)
            else:
                task["status"] = "needs_review"
            write_plan(plan)

    remaining = [
        task["id"]
        for task in plan["tasks"]
        if task["status"] != "done"
    ]
    if not remaining:
        run_final_checks(plan, integration_worktree, timeout)
    else:
        print("No runnable tasks remain: " + ", ".join(remaining))


def plan_command(args: argparse.Namespace) -> None:
    config = load_config()
    plan = load_plan()
    git_executable = find_git()
    run_dir = ROOT / "runs" / timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    plan["goal"] = args.goal
    integration_worktree = configured_worktree_root(config) / "integration"
    invoke_planner(
        config,
        plan,
        run_dir,
        git_executable,
        integration_worktree=integration_worktree,
    )
    print(f"Validated {PLAN_PATH} and regenerated {PLAN_VIEW_PATH}")


def approve_command(args: argparse.Namespace) -> None:
    if not args.by.strip():
        raise DriverError("--by must name an approver")
    plan = load_plan()
    tasks = task_map(plan)
    if args.task_id not in tasks:
        raise DriverError(f"unknown task: {args.task_id}")
    task = tasks[args.task_id]
    if task["lane"] != "locked":
        raise DriverError(f"{args.task_id} is not a locked task")
    task["approval"] = {
        "by": args.by.strip(),
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if task["status"] == "awaiting_approval":
        task["status"] = "todo"
    task["history"].append(
        {
            "event": "approved",
            "by": args.by.strip(),
            "at": task["approval"]["at"],
        }
    )
    write_plan(plan)
    print(f"Approved {args.task_id} by {args.by.strip()}")


def status_command(_: argparse.Namespace) -> None:
    plan = load_plan()
    headers = (
        "ID",
        "STATUS",
        "LANE",
        "ATTEMPTS",
        "REPLANS",
        "APPROVED BY",
        "DEPENDS ON",
        "TITLE",
    )
    rows = [
        (
            task["id"],
            task["status"],
            task["lane"],
            str(task["attempts"]),
            str(task["replans"]),
            task["approval"]["by"] if task["approval"] else "-",
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
    approve_parser = subparsers.add_parser(
        "approve", help="approve a locked task"
    )
    approve_parser.add_argument("task_id", help="locked task ID")
    approve_parser.add_argument("--by", required=True, help="approver name")
    approve_parser.set_defaults(handler=approve_command)
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
