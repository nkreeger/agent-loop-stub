from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def task(
    task_id: str,
    *,
    notes: str = "FAKE:pass",
    scope: list[str] | None = None,
    depends_on: list[str] | None = None,
    lane: str = "contained",
    checks: list[str] | None = None,
    status: str = "todo",
) -> dict[str, object]:
    output = f"outputs/{task_id}.txt"
    python = subprocess.list2cmdline([sys.executable])
    default_check = (
        f'{python} -c "from pathlib import Path; '
        f"raise SystemExit(not Path(r'{output}').read_text().startswith('good'))\""
    )
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "goal": f"Create {output}",
        "lane": lane,
        "scope": scope or [output],
        "depends_on": depends_on or [],
        "checks": checks or [default_check],
        "notes": notes,
        "status": status,
        "attempts": 0,
        "history": [],
        "approval": None,
        "replans": 0,
    }


class DriverIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.external = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.external_root = Path(self.external.name)
        self.worktree_root = self.external_root / "worktrees"
        self.invocations = self.external_root / "invocations.log"
        self.child_marker = self.external_root / "child-finished"
        self.sleep_marker = self.external_root / "sleep-once"
        self.agent_pid = self.external_root / "agent.pid"
        self.final_marker = self.external_root / "final-check"
        (self.root / "agents").mkdir()
        shutil.copy2(SOURCE_ROOT / "driver.py", self.root / "driver.py")
        for name in ("PLANNER.md", "CODER.md", "LESSONS.md"):
            shutil.copy2(SOURCE_ROOT / "agents" / name, self.root / "agents" / name)
        (self.root / ".gitignore").write_text("runs/\n", encoding="utf-8")
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.root / "fake_agent.py").write_text(
            textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path
                import subprocess
                import sys
                import time

                role, task_id, attempt, task_json = sys.argv[1:5]
                log = Path(os.environ["FAKE_INVOCATION_LOG"])
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(f"{role}:{task_id}:{attempt}\\n")
                if role == "planner":
                    mode = os.environ.get("FAKE_PLANNER_MODE", "noop")
                    if mode == "edit-other":
                        Path("README.md").write_text("planner changed this\\n", encoding="utf-8")
                    if mode == "write-plan":
                        Path("agents/plan.json").write_text(
                            os.environ["FAKE_PLANNER_PLAN"], encoding="utf-8"
                        )
                    raise SystemExit(0)

                task = json.loads(task_json)
                notes = task["notes"]
                target = next(
                    path for path in task["scope"]
                    if not any(character in path for character in "*?[")
                ) if any(
                    not any(character in path for character in "*?[")
                    for path in task["scope"]
                ) else f"outputs/{task_id}.txt"
                path = Path(target)
                path.parent.mkdir(parents=True, exist_ok=True)

                if "FAKE:sleep-once" in notes:
                    marker = Path(os.environ["FAKE_SLEEP_MARKER"])
                    if not marker.exists():
                        marker.write_text("started", encoding="utf-8")
                        Path(os.environ["FAKE_AGENT_PID"]).write_text(
                            str(os.getpid()), encoding="utf-8"
                        )
                        time.sleep(60)
                if "FAKE:spawn-child" in notes:
                    child_code = (
                        "import pathlib,time; time.sleep(3); "
                        f"pathlib.Path({os.environ['FAKE_CHILD_MARKER']!r}).write_text('bad')"
                    )
                    subprocess.Popen([sys.executable, "-c", child_code])
                    time.sleep(60)

                fail_until = 0
                marker = "FAKE:fail-until="
                if marker in notes:
                    fail_until = int(notes.split(marker, 1)[1].split()[0])
                content = (
                    f"bad-{attempt}" if int(attempt) <= fail_until
                    else f"good-{attempt}"
                )
                path.write_text(content, encoding="utf-8")
                if "FAKE:outside" in notes:
                    Path("forbidden.txt").write_text(f"outside-{attempt}", encoding="utf-8")
                if "FAKE:edit-main-plan" in notes:
                    relative_plan = Path("../../agents/plan.json")
                    relative_plan.parent.mkdir(parents=True, exist_ok=True)
                    relative_plan.write_text('{"goal":"relative hijack"}\\n', encoding="utf-8")
                    main_plan = Path(os.environ["FAKE_MAIN_ROOT"]) / "agents" / "plan.json"
                    main_plan.write_text('{"goal":"hijacked"}\\n', encoding="utf-8")
                if "FAKE:edit-integration" in notes:
                    integration_readme = (
                        Path(os.environ["FAKE_INTEGRATION_ROOT"]) / "README.md"
                    )
                    integration_readme.write_text("coder changed this\\n", encoding="utf-8")

                git = os.environ["DRIVER_GIT"]
                subprocess.run([git, "add", "-A"], check=True)
                subprocess.run(
                    [git, "commit", "-m", f"{task_id} attempt {attempt}"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.git = self.find_git()
        self.write_config()
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Driver Tests")
        self.run_git("config", "user.email", "driver-tests@example.invalid")

    def tearDown(self) -> None:
        self.stop_recorded_agent()
        self.temporary.cleanup()
        self.external.cleanup()

    def find_git(self) -> str:
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
        self.fail("git is required for driver integration tests")

    def write_config(self, timeout: int = 30, max_replans: int = 2) -> None:
        python = json.dumps(sys.executable)
        fake = json.dumps(str(self.root / "fake_agent.py"))
        worktrees = json.dumps(str(self.worktree_root))
        command = (
            f"[{python}, {fake}, \"{{role}}\", \"{{task_id}}\", "
            f"\"{{attempt}}\", \"{{task_json}}\"]"
        )
        (self.root / "driver.toml").write_text(
            textwrap.dedent(
                f"""
                [driver]
                integration_branch = "agent-integration"
                worktree_dir = {worktrees}
                timeout_seconds = {timeout}
                max_attempts = 3
                max_replans = {max_replans}
                max_parallel = 2

                [planner]
                model = "fake"
                command = {command}

                [coder]
                model = "fake"
                command = {command}
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "DRIVER_GIT": self.git,
                "FAKE_INVOCATION_LOG": str(self.invocations),
                "FAKE_MAIN_ROOT": str(self.root),
                "FAKE_INTEGRATION_ROOT": str(self.worktree_root / "integration"),
                "FAKE_CHILD_MARKER": str(self.child_marker),
                "FAKE_SLEEP_MARKER": str(self.sleep_marker),
                "FAKE_AGENT_PID": str(self.agent_pid),
                "FAKE_FINAL_MARKER": str(self.final_marker),
            }
        )
        if extra:
            environment.update(extra)
        return environment

    def run_git(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.git, *args],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=check,
        )

    def write_plan(
        self,
        tasks: list[dict[str, object]],
        final_checks: list[str] | None = None,
    ) -> None:
        plan = {
            "goal": "integration test",
            "final_checks": final_checks or [],
            "tasks": tasks,
        }
        (self.root / "agents" / "plan.json").write_text(
            json.dumps(plan, indent=2) + "\n", encoding="utf-8"
        )
        (self.root / "agents" / "PLAN.md").write_text(
            "generated later\n", encoding="utf-8"
        )

    def commit_fixture(self) -> None:
        self.run_git("add", "-A")
        self.run_git("commit", "-m", "test fixture")

    def run_driver(
        self,
        *args: str,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "driver.py", *args],
            cwd=self.root,
            env=self.environment(extra_env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=check,
        )

    def read_plan(self) -> dict[str, object]:
        return json.loads(
            (self.root / "agents" / "plan.json").read_text(encoding="utf-8")
        )

    def plan_task(self, task_id: str) -> dict[str, object]:
        return next(
            item for item in self.read_plan()["tasks"] if item["id"] == task_id
        )

    def integration_file(self, path: str) -> str:
        return self.run_git("show", f"agent-integration:{path}").stdout

    def stop_recorded_agent(self) -> None:
        if not self.agent_pid.exists():
            return
        try:
            os.kill(int(self.agent_pid.read_text(encoding="utf-8")), signal.SIGTERM)
        except (OSError, ValueError):
            pass

    def test_first_try_pass_is_merged_and_worktrees_are_external(self) -> None:
        self.write_plan([task("A")])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("done", current["status"])
        self.assertEqual("good-1", self.integration_file("outputs/A.txt"))
        self.assertTrue((self.worktree_root / "A").exists())
        self.assertFalse((self.root / ".worktrees").exists())

    def test_two_failures_then_passes(self) -> None:
        self.write_plan([task("A", notes="FAKE:fail-until=2")])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("done", current["status"])
        self.assertEqual(2, current["attempts"])
        self.assertEqual(2, len(current["history"]))

    def test_coder_plan_edit_is_red_and_does_not_replace_state(self) -> None:
        self.write_plan([task("A", notes="FAKE:edit-main-plan")])
        self.commit_fixture()

        completed = self.run_driver("run", check=False)

        plan = self.read_plan()
        current = plan["tasks"][0]
        self.assertEqual(2, completed.returncode)
        self.assertIn("protected checkout", completed.stdout)
        self.assertEqual("integration test", plan["goal"])
        self.assertEqual("Task A", current["title"])
        self.assertEqual("state_integrity_failed", current["history"][0]["event"])
        self.assertIn("agents/plan.json", current["history"][0]["evidence"])

    def test_locked_task_requires_driver_owned_approval(self) -> None:
        self.write_plan([task("A", lane="locked", notes="APPROVED-BY: ignored")])
        self.commit_fixture()

        self.run_driver("run")
        self.assertEqual("awaiting_approval", self.plan_task("A")["status"])
        self.run_driver("approve", "A", "--by", "Ada")
        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("needs_review", current["status"])
        self.assertEqual("Ada", current["approval"]["by"])

    def test_planner_cannot_grant_approval(self) -> None:
        self.write_plan([])
        self.commit_fixture()
        planned = {
            "goal": "planned goal",
            "final_checks": [],
            "tasks": [task("A", lane="locked")],
        }
        planned["tasks"][0]["approval"] = {
            "by": "Planner",
            "at": "2026-01-01T00:00:00Z",
        }

        self.run_driver(
            "plan",
            "planned goal",
            extra_env={
                "FAKE_PLANNER_MODE": "write-plan",
                "FAKE_PLANNER_PLAN": json.dumps(planned),
            },
        )

        self.assertIsNone(self.plan_task("A")["approval"])

    def test_coder_cannot_change_integration_checkout(self) -> None:
        self.write_plan([task("A", notes="FAKE:edit-integration")])
        self.commit_fixture()

        completed = self.run_driver("run", check=False)

        self.assertEqual(2, completed.returncode)
        self.assertEqual(
            "state_integrity_failed", self.plan_task("A")["history"][0]["event"]
        )
        self.assertEqual(
            "fixture\n",
            (self.worktree_root / "integration" / "README.md").read_text(
                encoding="utf-8"
            ),
        )

    def test_interrupted_run_resets_in_progress_and_resumes(self) -> None:
        self.write_plan([task("A", notes="FAKE:sleep-once")])
        self.commit_fixture()
        process = subprocess.Popen(
            [sys.executable, "driver.py", "run"],
            cwd=self.root,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if (
                self.sleep_marker.exists()
                and self.plan_task("A")["status"] == "in_progress"
            ):
                break
            time.sleep(0.1)
        else:
            self.fail("driver did not reach in_progress")
        process.kill()
        process.communicate(timeout=5)
        self.stop_recorded_agent()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("done", current["status"])
        self.assertTrue(
            any(
                entry["event"] == "recovered_interrupted_run"
                for entry in current["history"]
            )
        )

    def test_planner_cannot_edit_non_plan_file(self) -> None:
        self.write_plan([])
        self.commit_fixture()
        before_plan = (self.root / "agents" / "plan.json").read_bytes()

        completed = self.run_driver(
            "plan",
            "new goal",
            check=False,
            extra_env={"FAKE_PLANNER_MODE": "edit-other"},
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("outside agents/plan.json", completed.stdout)
        self.assertEqual("fixture\n", (self.root / "README.md").read_text())
        self.assertEqual(before_plan, (self.root / "agents" / "plan.json").read_bytes())

    def test_integration_check_failure_reverts_merge_and_marks_red(self) -> None:
        python = subprocess.list2cmdline([sys.executable])
        check = (
            f'{python} -c "from pathlib import Path; '
            "raise SystemExit('worktrees/A' not in Path('.git').read_text().replace('\\\\\\\\','/'))\""
        )
        self.write_plan([task("A", checks=[check])])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertTrue(
            any(
                entry["event"] == "integration_check_failed"
                for entry in current["history"]
            )
        )
        self.assertNotEqual(
            0,
            self.run_git(
                "cat-file", "-e", "agent-integration:outputs/A.txt", check=False
            ).returncode,
        )

    def test_final_checks_run_and_are_reported(self) -> None:
        python = subprocess.list2cmdline([sys.executable])
        final = (
            f'{python} -c "import os; from pathlib import Path; '
            "Path(os.environ['FAKE_FINAL_MARKER']).write_text('ran')\""
        )
        self.write_plan([task("A")], [final])
        self.commit_fixture()

        completed = self.run_driver("run")

        self.assertTrue(self.final_marker.exists())
        self.assertIn("FINAL CHECK GREEN", completed.stdout)

    def test_timeout_kills_agent_process_group(self) -> None:
        self.write_config(timeout=1)
        self.write_plan([task("A", notes="FAKE:spawn-child")])
        self.commit_fixture()

        self.run_driver("run")
        time.sleep(4)

        self.assertFalse(self.child_marker.exists())
        self.assertEqual("blocked", self.plan_task("A")["status"])

    def test_replanning_stops_at_max_replans(self) -> None:
        self.write_config(max_replans=2)
        self.write_plan([task("A", notes="FAKE:fail-until=99")])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("blocked", current["status"])
        self.assertEqual(2, current["replans"])
        self.assertEqual("replan_limit_reached", current["history"][-1]["event"])

    def test_check_named_file_requires_explicit_scope(self) -> None:
        self.write_plan([task("A", scope=["outputs/**"])])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("check_protection_failed", current["history"][0]["event"])
        self.assertIn("outputs/A.txt", current["history"][0]["evidence"])

    def test_dependency_ordering(self) -> None:
        self.write_plan([task("A"), task("B", depends_on=["A"])])
        self.commit_fixture()

        self.run_driver("run", "--max-parallel", "2")

        lines = self.invocations.read_text(encoding="utf-8").splitlines()
        self.assertLess(lines.index("coder:A:1"), lines.index("coder:B:1"))
        self.assertEqual("done", self.plan_task("B")["status"])

    def test_plan_validation_rejects_dependency_cycle(self) -> None:
        self.write_plan(
            [task("A", depends_on=["B"]), task("B", depends_on=["A"])]
        )
        self.commit_fixture()

        completed = self.run_driver("status", check=False)

        self.assertEqual(2, completed.returncode)
        self.assertIn("dependency cycle", completed.stdout)

    def test_dry_run_changes_nothing(self) -> None:
        self.write_plan([task("A")])
        self.commit_fixture()
        before = self.run_git("status", "--porcelain").stdout

        completed = self.run_driver("run", "--dry-run")

        after = self.run_git("status", "--porcelain").stdout
        self.assertEqual(before, after)
        self.assertIn("DRY RUN", completed.stdout)
        self.assertFalse(self.worktree_root.exists())
        self.assertFalse((self.root / "runs").exists())


if __name__ == "__main__":
    unittest.main()
