from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def task(
    task_id: str,
    *,
    notes: str = "FAKE:pass",
    scope: list[str] | None = None,
    depends_on: list[str] | None = None,
    lane: str = "contained",
) -> dict[str, object]:
    output = f"outputs/{task_id}.txt"
    python = subprocess.list2cmdline([sys.executable])
    check = (
        f'{python} -c "from pathlib import Path; '
        f"raise SystemExit(Path(r'{output}').read_text() != 'good')\""
    )
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "goal": f"Create {output}",
        "lane": lane,
        "scope": scope or [output],
        "depends_on": depends_on or [],
        "checks": [check],
        "notes": notes,
        "status": "todo",
        "attempts": 0,
        "history": [],
    }


class DriverIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.external = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.invocations = Path(self.external.name) / "invocations.log"
        (self.root / "agents").mkdir()
        (self.root / "tests").mkdir()
        for name in ("driver.py",):
            shutil.copy2(SOURCE_ROOT / name, self.root / name)
        for name in ("PLANNER.md", "CODER.md", "LESSONS.md"):
            shutil.copy2(SOURCE_ROOT / "agents" / name, self.root / "agents" / name)
        (self.root / ".gitignore").write_text(
            "runs/\n.worktrees/\n", encoding="utf-8"
        )
        (self.root / "fake_agent.py").write_text(
            textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path
                import subprocess
                import sys

                role, task_id, attempt, task_json = sys.argv[1:5]
                log = Path(os.environ["FAKE_INVOCATION_LOG"])
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(f"{role}:{task_id}:{attempt}\\n")
                if role == "planner":
                    raise SystemExit(0)

                task = json.loads(task_json)
                notes = task["notes"]
                target = next(
                    path for path in task["scope"]
                    if not any(character in path for character in "*?[")
                )
                path = Path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                fail_until = 0
                marker = "FAKE:fail-until="
                if marker in notes:
                    fail_until = int(notes.split(marker, 1)[1].split()[0])
                path.write_text(
                    "bad" if int(attempt) <= fail_until else "good",
                    encoding="utf-8",
                )
                if "FAKE:outside" in notes:
                    Path("forbidden.txt").write_text("outside", encoding="utf-8")

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

    def write_config(self) -> None:
        python = json.dumps(sys.executable)
        fake = json.dumps("fake_agent.py")
        command = (
            f"[{python}, {fake}, \"{{role}}\", \"{{task_id}}\", "
            f"\"{{attempt}}\", \"{{task_json}}\"]"
        )
        (self.root / "driver.toml").write_text(
            textwrap.dedent(
                f"""
                [driver]
                integration_branch = "agent-integration"
                worktree_dir = ".worktrees"
                timeout_seconds = 30
                max_attempts = 3
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

    def write_plan(self, tasks: list[dict[str, object]]) -> None:
        plan = {"goal": "integration test", "tasks": tasks}
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
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["DRIVER_GIT"] = self.git
        environment["FAKE_INVOCATION_LOG"] = str(self.invocations)
        return subprocess.run(
            [sys.executable, "driver.py", *args],
            cwd=self.root,
            env=environment,
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

    def test_first_try_pass_is_merged(self) -> None:
        self.write_plan([task("A")])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("done", current["status"])
        self.assertEqual(0, current["attempts"])
        self.assertEqual("good", self.integration_file("outputs/A.txt"))

    def test_two_failures_then_passes(self) -> None:
        self.write_plan([task("A", notes="FAKE:fail-until=2")])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("done", current["status"])
        self.assertEqual(2, current["attempts"])
        self.assertEqual(2, len(current["history"]))
        self.assertEqual("good", self.integration_file("outputs/A.txt"))

    def test_three_failures_end_in_replan(self) -> None:
        self.write_plan([task("A", notes="FAKE:fail-until=3")])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("replan", current["status"])
        self.assertEqual(3, current["attempts"])
        self.assertEqual(3, len(current["history"]))
        self.assertIn("planner:planner:1", self.invocations.read_text(encoding="utf-8"))

    def test_out_of_scope_change_is_red(self) -> None:
        self.write_plan([task("A", notes="FAKE:outside")])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("replan", current["status"])
        self.assertEqual("scope_failed", current["history"][0]["event"])
        self.assertIn("forbidden.txt", current["history"][0]["evidence"])

    def test_locked_task_without_approval_does_not_run(self) -> None:
        self.write_plan([task("A", lane="locked", notes="")])
        self.commit_fixture()

        self.run_driver("run")

        current = self.plan_task("A")
        self.assertEqual("awaiting_approval", current["status"])
        invocations = (
            self.invocations.read_text(encoding="utf-8")
            if self.invocations.exists()
            else ""
        )
        self.assertNotIn("coder:A", invocations)
        self.assertNotEqual(
            0,
            self.run_git(
                "show-ref", "--verify", "--quiet", "refs/heads/task/A", check=False
            ).returncode,
        )

    def test_dependency_ordering(self) -> None:
        self.write_plan([task("A"), task("B", depends_on=["A"])])
        self.commit_fixture()

        self.run_driver("run", "--max-parallel", "2")

        lines = self.invocations.read_text(encoding="utf-8").splitlines()
        self.assertLess(lines.index("coder:A:1"), lines.index("coder:B:1"))
        self.assertEqual("done", self.plan_task("A")["status"])
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
        self.assertFalse((self.root / ".worktrees").exists())
        self.assertFalse((self.root / "runs").exists())


if __name__ == "__main__":
    unittest.main()
