from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from campaign_runtime import CampaignRuntime, RuntimeError
from graph_loop import GraphError


class CampaignRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.adapter = self.root / "adapter.py"
        self.adapter.write_text(
            textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                request = json.loads(Path(os.environ["AGENT_LOOP_REQUEST"]).read_text())
                states = {"start": "ready", "status": "ready", "stop": "stopped"}
                response = {
                    "schema": "agent-loop-adapter-response/v1",
                    "campaign_id": request["campaign_id"],
                    "service_id": request["service_id"],
                    "attempt_id": request["attempt_id"],
                    "action": request["action"],
                    "state": states[request["action"]],
                    "evidence": [f"{request['action']} passed"],
                    "identity": {"resource_uid": "uid-1"},
                }
                Path(os.environ["AGENT_LOOP_RESPONSE"]).write_text(
                    json.dumps(response) + "\\n"
                )
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.definition = self.root / "campaign.json"
        self.definition.write_text(
            json.dumps(
                {
                    "schema": "agent-loop-campaign/v1",
                    "id": "smoke",
                    "intent": "Keep one service alive across smoke and benchmark work",
                    "constraints": {"release": "explicit"},
                    "timezone": "America/Chicago",
                    "progress_interval_seconds": 120,
                    "services": [
                        {
                            "id": "environment",
                            "acceptance": ["The original environment is ready"],
                            "adapter": [sys.executable, str(self.adapter)],
                            "inputs": {"adapter_timeout_seconds": 30},
                        }
                    ],
                    "experiments": [
                        {
                            "id": "environment-smoke",
                            "hypothesis": "The retained environment is reusable",
                            "acceptance": ["The environment remains ready"],
                            "requires_services": ["environment"],
                            "jobs": [
                                {
                                    "id": "verify",
                                    "kind": "task",
                                    "depends_on": [],
                                    "acceptance": ["The environment identity is unchanged"],
                                }
                            ],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.runtime = CampaignRuntime(
            "example", "smoke", state_root=self.root / "state"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_state_and_events_live_outside_source_definition(self) -> None:
        self.runtime.initialize(self.definition)

        self.assertTrue(self.runtime.graph_path.is_file())
        self.assertTrue(self.runtime.events_path.is_file())
        self.assertFalse((self.definition.parent / "graph.json").exists())
        events = self.runtime.events_path.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(events), 5)

    def test_ready_service_is_retained_until_explicit_stop(self) -> None:
        self.runtime.initialize(self.definition)
        response = self.runtime.start_service("environment")

        self.assertEqual("ready", response["state"])
        service = self.runtime.snapshot()["campaigns"]["smoke"]["services"][
            "environment"
        ]
        self.assertEqual("running", service["status"])
        self.assertEqual("ready", service["service_state"])
        self.assertIn("America/Chicago", self.runtime._definition()["timezone"])

        self.runtime.complete_task(
            "environment-smoke",
            "verify",
            evidence=["the original environment identity is unchanged"],
        )
        self.runtime.finish_experiment(
            "environment-smoke",
            outcome="succeeded",
            conclusion="The retained environment smoke passed",
        )
        campaign = self.runtime.snapshot()["campaigns"]["smoke"]
        self.assertEqual(
            "succeeded", campaign["experiments"]["environment-smoke"]["status"]
        )
        self.assertEqual("ready", campaign["services"]["environment"]["service_state"])

        self.runtime.stop_service("environment")
        service = self.runtime.snapshot()["campaigns"]["smoke"]["services"][
            "environment"
        ]
        self.assertEqual("succeeded", service["status"])
        self.assertEqual("stopped", service["service_state"])

    def test_adapter_failure_requires_reconciliation(self) -> None:
        self.runtime.initialize(self.definition)
        definition = json.loads(self.runtime.definition_path.read_text())
        definition["services"][0]["adapter"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        self.runtime.definition_path.write_text(json.dumps(definition) + "\n")

        with self.assertRaisesRegex(RuntimeError, "status 7"):
            self.runtime.start_service("environment")
        service = self.runtime.snapshot()["campaigns"]["smoke"]["services"][
            "environment"
        ]
        self.assertEqual("reconciling", service["status"])
        self.assertEqual("unknown", service["service_state"])

        self.runtime.record_service_reconciliation(
            "environment",
            outcome="failed",
            evidence=["the original resource is absent"],
        )
        service = self.runtime.snapshot()["campaigns"]["smoke"]["services"][
            "environment"
        ]
        self.assertEqual("failed", service["status"])

    def test_dynamic_queue_checkpoint_and_attempt_recovery(self) -> None:
        self.runtime.initialize(self.definition)
        self.runtime.start_service("environment")
        follow_up = self.root / "follow-up.json"
        follow_up.write_text(
            json.dumps(
                {
                    "id": "follow-up",
                    "hypothesis": "The retained environment supports follow-up work",
                    "acceptance": ["The task records evidence"],
                    "requires_services": ["environment"],
                    "jobs": [
                        {
                            "id": "probe",
                            "kind": "task",
                            "depends_on": [],
                            "acceptance": ["The probe succeeds"],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual("follow-up", self.runtime.queue_experiment(follow_up))
        attempt = self.runtime.start_job(
            "follow-up", "probe", inputs={"revision": "one"}
        )
        self.runtime.mark_job_uncertain(
            "follow-up", "probe", attempt, reason="coordinator interrupted"
        )
        with self.assertRaisesRegex(GraphError, "job is not ready"):
            self.runtime.start_job(
                "follow-up", "probe", inputs={"revision": "replacement"}
            )
        self.runtime.reconcile_job(
            "follow-up",
            "probe",
            attempt,
            outcome="failed",
            evidence=["the original worker is absent"],
        )
        retry = self.runtime.start_job(
            "follow-up", "probe", inputs={"revision": "replacement"}
        )
        self.runtime.finish_task_attempt(
            "follow-up",
            "probe",
            retry,
            succeeded=True,
            evidence=["the replacement probe passed"],
        )
        checkpoint = self.runtime.checkpoint(
            "Follow-up probe passed", slide_ready=True
        )
        status = self.runtime.status_document()

        self.assertTrue(checkpoint.is_file())
        self.assertEqual("agent-loop-campaign-status/v1", status["schema"])
        self.assertEqual(1, status["queue_summary"]["succeeded"])
        self.assertIn("next_progress_due", status)
        self.assertIn("next_status_due", status)
        attempts = status["campaign"]["experiments"]["follow-up"]["jobs"]["probe"][
            "attempts"
        ]
        self.assertEqual(["failed", "succeeded"], [item["status"] for item in attempts])
        self.assertTrue((self.runtime.queue_root / "follow-up.json").is_file())


if __name__ == "__main__":
    unittest.main()
