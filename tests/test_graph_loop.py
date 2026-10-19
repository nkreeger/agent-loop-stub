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

    def test_campaign_service_is_retained_across_experiments(self) -> None:
        self.store.add_campaign_service(
            "C1",
            "environment",
            ["The assigned environment is ready and remains owned"],
        )
        for experiment in ("E2", "E3"):
            self.store.add_experiment(
                "C1",
                experiment,
                "The retained environment supports another experiment",
                ["The request passes"],
                requires_services=["environment"],
            )
            self.store.add_job(
                "C1",
                experiment,
                "smoke",
                kind="task",
                depends_on=[],
                acceptance=["A request succeeds"],
            )
        self.add_task("bootstrap")
        self.store.start_campaign("C1")

        bootstrap = self.store.start_attempt(
            "C1", "E1", "bootstrap", {"phase": "local"}
        )
        self.store.finish_task(
            "C1",
            "E1",
            "bootstrap",
            bootstrap,
            succeeded=True,
            evidence=["local preparation passed"],
        )
        self.store.finish_experiment(
            "C1", "E1", outcome="succeeded", conclusion="Prepared"
        )
        self.assertEqual([], self.store.ready_jobs("C1", "E2"))
        service_attempt = self.store.start_campaign_service(
            "C1", "environment", {"allocation": "owned-allocation"}
        )
        self.store.mark_campaign_service_ready(
            "C1",
            "environment",
            service_attempt,
            evidence=["environment identity is ready"],
        )

        for experiment in ("E2", "E3"):
            self.assertEqual(["smoke"], self.store.ready_jobs("C1", experiment))
            attempt = self.store.start_attempt(
                "C1", experiment, "smoke", {"request": experiment}
            )
            self.store.finish_task(
                "C1",
                experiment,
                "smoke",
                attempt,
                succeeded=True,
                evidence=["request passed"],
            )
            self.store.finish_experiment(
                "C1",
                experiment,
                outcome="succeeded",
                conclusion="Validated on the retained environment",
            )

        with self.assertRaisesRegex(GraphError, "active services: environment"):
            self.store.finish_campaign("C1")
        self.store.stop_campaign_service(
            "C1",
            "environment",
            service_attempt,
            evidence=["original environment is absent"],
        )
        self.store.finish_campaign("C1")

    def test_uncertain_campaign_service_must_be_reconciled(self) -> None:
        self.store.add_campaign_service(
            "C1", "environment", ["The environment is ready"]
        )
        self.add_task("A")
        self.store.start_campaign("C1")
        attempt = self.store.start_campaign_service(
            "C1", "environment", {"allocation": "owned-allocation"}
        )
        self.store.mark_campaign_service_uncertain(
            "C1",
            "environment",
            attempt,
            reason="coordinator stopped during create",
        )

        with self.assertRaisesRegex(GraphError, "reconciling"):
            self.store.start_campaign_service(
                "C1", "environment", {"allocation": "replacement"}
            )
        self.store.reconcile_campaign_service(
            "C1",
            "environment",
            attempt,
            outcome="ready",
            evidence=["the original environment identity is ready"],
        )
        service = self.store.snapshot()["campaigns"]["C1"]["services"]["environment"]
        self.assertEqual("ready", service["service_state"])

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

    def test_running_campaign_adds_experiment_bundle_atomically(self) -> None:
        self.add_task("A")
        self.store.start_campaign("C1")
        before = self.store.snapshot()

        with self.assertRaisesRegex(GraphError, "unknown dependencies"):
            self.store.add_experiment_bundle(
                "C1",
                "E2",
                "A follow-up tests the retained finding",
                ["Both jobs pass"],
                [
                    {
                        "id": "second",
                        "kind": "task",
                        "depends_on": ["missing"],
                        "acceptance": ["Evidence is recorded"],
                    }
                ],
            )
        self.assertEqual(before, self.store.snapshot())

        self.store.add_experiment_bundle(
            "C1",
            "E2",
            "A follow-up tests the retained finding",
            ["Both jobs pass"],
            [
                {
                    "id": "first",
                    "kind": "task",
                    "depends_on": [],
                    "acceptance": ["First evidence is recorded"],
                },
                {
                    "id": "second",
                    "kind": "task",
                    "depends_on": ["first"],
                    "acceptance": ["Second evidence is recorded"],
                },
            ],
            source_sha256="a" * 64,
        )
        state = self.store.snapshot()
        self.assertEqual(["first"], self.store.ready_jobs("C1", "E2"))
        self.assertEqual(
            "a" * 64,
            next(
                event["detail"]["source_sha256"]
                for event in state["events"]
                if event["kind"] == "experiment_added"
                and event["experiment_id"] == "E2"
            ),
        )


if __name__ == "__main__":
    unittest.main()
