from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from graph_loop import GraphError, GraphStore


class Values:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.index = 0

    def __call__(self) -> str:
        self.index += 1
        return f"{self.prefix}-{self.index}"


class GraphLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "graph.json"
        self.store = GraphStore(
            self.path,
            clock=Values("time"),
            id_factory=Values("attempt"),
        )
        self.store.initialize()
        self.store.create_campaign("C1", "Deliver a tested change")
        self.store.add_experiment(
            "C1", "E1", "The change solves the problem", ["All checks pass"]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_task(
        self, job_id: str, *, depends_on: list[str] | None = None
    ) -> None:
        self.store.add_job(
            "C1",
            "E1",
            job_id,
            kind="task",
            depends_on=depends_on or [],
            acceptance=["Evidence is recorded"],
        )

    def test_dependencies_become_ready_only_after_success(self) -> None:
        self.add_task("A")
        self.add_task("B", depends_on=["A"])
        self.store.start_campaign("C1")

        self.assertEqual(["A"], self.store.ready_jobs("C1", "E1"))
        attempt = self.store.start_attempt("C1", "E1", "A", {"revision": "one"})
        self.assertEqual([], self.store.ready_jobs("C1", "E1"))
        self.store.finish_task(
            "C1", "E1", "A", attempt, succeeded=True, evidence=["check passed"]
        )
        self.assertEqual(["B"], self.store.ready_jobs("C1", "E1"))

    def test_attempt_inputs_and_fingerprint_are_immutable_history(self) -> None:
        self.add_task("A")
        self.store.start_campaign("C1")
        attempt = self.store.start_attempt(
            "C1", "E1", "A", {"revision": "one", "options": ["safe"]}
        )
        before = self.store.snapshot()
        recorded = before["campaigns"]["C1"]["experiments"]["E1"]["jobs"]["A"][
            "attempts"
        ][0]

        self.store.finish_task(
            "C1", "E1", "A", attempt, succeeded=False, evidence=["check failed"]
        )
        retry = self.store.start_attempt(
            "C1", "E1", "A", {"revision": "two", "options": ["safe"]}
        )
        after = self.store.snapshot()
        attempts = after["campaigns"]["C1"]["experiments"]["E1"]["jobs"]["A"][
            "attempts"
        ]

        self.assertNotEqual(attempt, retry)
        self.assertEqual(recorded["inputs"], attempts[0]["inputs"])
        self.assertEqual(recorded["fingerprint"], attempts[0]["fingerprint"])
        self.assertNotEqual(attempts[0]["fingerprint"], attempts[1]["fingerprint"])

    def test_uncertain_attempt_must_reconcile_before_retry(self) -> None:
        self.add_task("A")
        self.store.start_campaign("C1")
        attempt = self.store.start_attempt("C1", "E1", "A", {"revision": "one"})
        self.store.mark_uncertain(
            "C1", "E1", "A", attempt, reason="coordinator stopped"
        )

        with self.assertRaisesRegex(GraphError, "job is not ready"):
            self.store.start_attempt("C1", "E1", "A", {"revision": "two"})

        self.store.reconcile_attempt(
            "C1",
            "E1",
            "A",
            attempt,
            outcome="failed",
            evidence=["worker is absent"],
        )
        retry = self.store.start_attempt("C1", "E1", "A", {"revision": "two"})
        self.assertNotEqual(attempt, retry)

    def test_ready_service_unblocks_dependents_but_must_stop(self) -> None:
        self.store.add_job(
            "C1",
            "E1",
            "server",
            kind="service",
            depends_on=[],
            acceptance=["Ready, healthy, and stopped"],
        )
        self.add_task("client", depends_on=["server"])
        self.store.start_campaign("C1")
        service_attempt = self.store.start_attempt(
            "C1", "E1", "server", {"revision": "one"}
        )
        self.store.mark_service_ready(
            "C1", "E1", "server", service_attempt, evidence=["health check passed"]
        )

        self.assertEqual(["client"], self.store.ready_jobs("C1", "E1"))
        client_attempt = self.store.start_attempt(
            "C1", "E1", "client", {"revision": "one"}
        )
        self.store.finish_task(
            "C1",
            "E1",
            "client",
            client_attempt,
            succeeded=True,
            evidence=["request passed"],
        )
        with self.assertRaisesRegex(GraphError, "unresolved jobs: server"):
            self.store.finish_experiment(
                "C1", "E1", outcome="succeeded", conclusion="Validated"
            )

        self.store.stop_service(
            "C1", "E1", "server", service_attempt, evidence=["process exited"]
        )
        self.store.finish_experiment(
            "C1", "E1", outcome="succeeded", conclusion="Validated"
        )
        self.store.finish_campaign("C1")
        self.assertEqual("completed", self.store.snapshot()["campaigns"]["C1"]["status"])

    def test_campaign_cannot_finish_with_unresolved_experiment(self) -> None:
        self.add_task("A")
        self.store.start_campaign("C1")

        with self.assertRaisesRegex(GraphError, "unresolved experiments: E1"):
            self.store.finish_campaign("C1")

    def test_empty_experiment_cannot_finish(self) -> None:
        self.store.start_campaign("C1")

        with self.assertRaisesRegex(GraphError, "requires at least one job"):
            self.store.finish_experiment(
                "C1", "E1", outcome="succeeded", conclusion="Nothing ran"
            )

    def test_events_are_ordered_and_survive_reload(self) -> None:
        self.add_task("A")
        self.store.start_campaign("C1")
        attempt = self.store.start_attempt("C1", "E1", "A", {"revision": "one"})
        self.store.finish_task(
            "C1", "E1", "A", attempt, succeeded=True, evidence=["check passed"]
        )

        reloaded = GraphStore(self.path, clock=Values("later"))
        events = reloaded.snapshot()["events"]
        self.assertEqual(list(range(1, len(events) + 1)), [e["sequence"] for e in events])
        self.assertEqual("attempt_finished", events[-1]["kind"])


if __name__ == "__main__":
    unittest.main()
