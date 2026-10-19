#!/usr/bin/env python3
"""Durable, provider-neutral campaign and experiment lifecycle primitives."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Callable
import uuid


SCHEMA = "agent-loop-graph/v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TERMINAL_EXPERIMENTS = {"succeeded", "failed"}


class GraphError(RuntimeError):
    pass


def canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise GraphError(f"invalid {label}: {value!r}")


def _nonempty_strings(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise GraphError(f"{label} must be a non-empty list of strings")
    return value


class GraphStore:
    """Persist graph state and append-only lifecycle events in one JSON file."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], str],
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self.clock = clock
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def initialize(self) -> None:
        if self.path.exists():
            raise GraphError(f"state already exists: {self.path}")
        self._save({"schema": SCHEMA, "campaigns": {}, "events": []})

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._load())

    def create_campaign(
        self, campaign_id: str, intent: str, *, constraints: dict[str, Any] | None = None
    ) -> None:
        _identifier(campaign_id, "campaign id")
        if not isinstance(intent, str) or not intent.strip():
            raise GraphError("campaign intent is required")
        if constraints is not None and not isinstance(constraints, dict):
            raise GraphError("campaign constraints must be an object")
        state = self._load()
        if campaign_id in state["campaigns"]:
            raise GraphError(f"campaign already exists: {campaign_id}")
        state["campaigns"][campaign_id] = {
            "id": campaign_id,
            "intent": intent,
            "constraints": copy.deepcopy(constraints or {}),
            "status": "planning",
            "services": {},
            "experiments": {},
            "created_at": self.clock(),
            "completed_at": None,
        }
        self._event(state, campaign_id, None, None, "campaign_created", {})
        self._save(state)

    def add_experiment(
        self,
        campaign_id: str,
        experiment_id: str,
        hypothesis: str,
        acceptance: list[str],
        *,
        requires_services: list[str] | None = None,
    ) -> None:
        _identifier(experiment_id, "experiment id")
        if not isinstance(hypothesis, str) or not hypothesis.strip():
            raise GraphError("experiment hypothesis is required")
        _nonempty_strings(acceptance, "experiment acceptance")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        self._require(campaign["status"], {"planning", "running"}, "add experiment")
        required = list(requires_services or [])
        if (
            len(set(required)) != len(required)
            or not all(isinstance(item, str) and IDENTIFIER.fullmatch(item) for item in required)
        ):
            raise GraphError("required services must be a unique list of IDs")
        unknown = sorted(set(required) - set(campaign["services"]))
        if unknown:
            raise GraphError(f"unknown campaign services: {', '.join(unknown)}")
        if experiment_id in campaign["experiments"]:
            raise GraphError(f"experiment already exists: {experiment_id}")
        campaign["experiments"][experiment_id] = {
            "id": experiment_id,
            "hypothesis": hypothesis,
            "acceptance": list(acceptance),
            "requires_services": required,
            "status": "pending",
            "jobs": {},
            "created_at": self.clock(),
            "completed_at": None,
            "conclusion": None,
        }
        self._event(
            state, campaign_id, experiment_id, None, "experiment_added", {}
        )
        self._save(state)

    def add_experiment_bundle(
        self,
        campaign_id: str,
        experiment_id: str,
        hypothesis: str,
        acceptance: list[str],
        jobs: list[dict[str, Any]],
        *,
        requires_services: list[str] | None = None,
        source_sha256: str | None = None,
    ) -> None:
        _identifier(experiment_id, "experiment id")
        if not isinstance(hypothesis, str) or not hypothesis.strip():
            raise GraphError("experiment hypothesis is required")
        _nonempty_strings(acceptance, "experiment acceptance")
        if not isinstance(jobs, list) or not jobs:
            raise GraphError("experiment jobs must be a non-empty list")
        required = list(requires_services or [])
        if (
            len(set(required)) != len(required)
            or not all(
                isinstance(item, str) and IDENTIFIER.fullmatch(item)
                for item in required
            )
        ):
            raise GraphError("required services must be a unique list of IDs")
        if source_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", source_sha256
        ):
            raise GraphError("source SHA-256 must be lowercase hexadecimal")

        prepared_jobs: dict[str, dict[str, Any]] = {}
        for job in jobs:
            if not isinstance(job, dict) or set(job) != {
                "id",
                "kind",
                "depends_on",
                "acceptance",
            }:
                raise GraphError(
                    "jobs require id, kind, depends_on, and acceptance"
                )
            job_id = job["id"]
            _identifier(job_id, "job id")
            if job_id in prepared_jobs:
                raise GraphError(f"duplicate job id: {job_id}")
            if job["kind"] not in {"task", "service"}:
                raise GraphError("job kind must be task or service")
            dependencies = job["depends_on"]
            if (
                not isinstance(dependencies, list)
                or len(set(dependencies)) != len(dependencies)
                or not all(
                    isinstance(item, str) and IDENTIFIER.fullmatch(item)
                    for item in dependencies
                )
            ):
                raise GraphError("job dependencies must be a unique list of IDs")
            unknown = sorted(set(dependencies) - set(prepared_jobs))
            if unknown:
                raise GraphError(f"unknown dependencies: {', '.join(unknown)}")
            _nonempty_strings(job["acceptance"], "job acceptance")
            prepared_jobs[job_id] = {
                "id": job_id,
                "kind": job["kind"],
                "depends_on": list(dependencies),
                "acceptance": list(job["acceptance"]),
                "status": "pending",
                "service_state": "stopped" if job["kind"] == "service" else None,
                "attempts": [],
            }

        state = self._load()
        campaign = self._campaign(state, campaign_id)
        self._require(campaign["status"], {"planning", "running"}, "add experiment")
        unknown_services = sorted(set(required) - set(campaign["services"]))
        if unknown_services:
            raise GraphError(
                f"unknown campaign services: {', '.join(unknown_services)}"
            )
        if experiment_id in campaign["experiments"]:
            raise GraphError(f"experiment already exists: {experiment_id}")
        campaign["experiments"][experiment_id] = {
            "id": experiment_id,
            "hypothesis": hypothesis,
            "acceptance": list(acceptance),
            "requires_services": required,
            "status": "pending",
            "jobs": prepared_jobs,
            "created_at": self.clock(),
            "completed_at": None,
            "conclusion": None,
        }
        detail = {"source_sha256": source_sha256} if source_sha256 else {}
        self._event(
            state, campaign_id, experiment_id, None, "experiment_added", detail
        )
        for job in prepared_jobs.values():
            self._event(
                state,
                campaign_id,
                experiment_id,
                job["id"],
                "job_added",
                {"kind": job["kind"], "depends_on": job["depends_on"]},
            )
        self._save(state)

    def add_campaign_service(
        self, campaign_id: str, service_id: str, acceptance: list[str]
    ) -> None:
        _identifier(service_id, "service id")
        _nonempty_strings(acceptance, "service acceptance")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        self._require(campaign["status"], {"planning", "running"}, "add service")
        if service_id in campaign["services"]:
            raise GraphError(f"campaign service already exists: {service_id}")
        campaign["services"][service_id] = {
            "id": service_id,
            "acceptance": list(acceptance),
            "status": "pending",
            "service_state": "stopped",
            "attempts": [],
        }
        self._event(
            state, campaign_id, None, service_id, "campaign_service_added", {}
        )
        self._save(state)

    def add_job(
        self,
        campaign_id: str,
        experiment_id: str,
        job_id: str,
        *,
        kind: str,
        depends_on: list[str],
        acceptance: list[str],
    ) -> None:
        _identifier(job_id, "job id")
        if kind not in {"task", "service"}:
            raise GraphError("job kind must be task or service")
        if not isinstance(depends_on, list) or not all(
            isinstance(item, str) and IDENTIFIER.fullmatch(item)
            for item in depends_on
        ):
            raise GraphError("job dependencies must be a list of IDs")
        if len(set(depends_on)) != len(depends_on):
            raise GraphError("job dependencies must be unique")
        _nonempty_strings(acceptance, "job acceptance")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        self._require(campaign["status"], {"planning", "running"}, "add job")
        self._require(experiment["status"], {"pending", "running"}, "add job")
        jobs = experiment["jobs"]
        if job_id in jobs:
            raise GraphError(f"job already exists: {job_id}")
        unknown = sorted(set(depends_on) - set(jobs))
        if unknown:
            raise GraphError(f"unknown dependencies: {', '.join(unknown)}")
        jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "depends_on": list(depends_on),
            "acceptance": list(acceptance),
            "status": "pending",
            "service_state": "stopped" if kind == "service" else None,
            "attempts": [],
        }
        self._event(
            state,
            campaign_id,
            experiment_id,
            job_id,
            "job_added",
            {"kind": kind, "depends_on": list(depends_on)},
        )
        self._save(state)

    def start_campaign(self, campaign_id: str) -> None:
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        self._require(campaign["status"], {"planning"}, "start campaign")
        if not campaign["experiments"]:
            raise GraphError("campaign requires at least one experiment")
        campaign["status"] = "running"
        self._event(state, campaign_id, None, None, "campaign_started", {})
        self._save(state)

    def start_campaign_service(
        self,
        campaign_id: str,
        service_id: str,
        inputs: dict[str, Any],
    ) -> str:
        if not isinstance(inputs, dict):
            raise GraphError("service inputs must be an object")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        service = self._campaign_service(campaign, service_id)
        self._require(campaign["status"], {"running"}, "start service")
        self._require(service["status"], {"pending", "failed"}, "start service")
        attempt_id = self.id_factory()
        _identifier(attempt_id, "attempt id")
        if any(
            attempt_id == attempt["id"]
            for candidate in campaign["services"].values()
            for attempt in candidate["attempts"]
        ):
            raise GraphError(f"attempt id already exists: {attempt_id}")
        attempt = {
            "id": attempt_id,
            "fingerprint": canonical_hash(
                {
                    "campaign": campaign_id,
                    "service": service_id,
                    "inputs": inputs,
                }
            ),
            "inputs": copy.deepcopy(inputs),
            "status": "running",
            "evidence": [],
            "started_at": self.clock(),
            "completed_at": None,
        }
        service["attempts"].append(attempt)
        service["status"] = "running"
        service["service_state"] = "starting"
        self._event(
            state,
            campaign_id,
            None,
            service_id,
            "campaign_service_started",
            {"attempt_id": attempt_id, "fingerprint": attempt["fingerprint"]},
        )
        self._save(state)
        return attempt_id

    def mark_campaign_service_ready(
        self,
        campaign_id: str,
        service_id: str,
        attempt_id: str,
        *,
        evidence: list[str],
    ) -> None:
        _nonempty_strings(evidence, "service readiness evidence")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        service = self._campaign_service(campaign, service_id)
        attempt = self._active_attempt(service, attempt_id)
        self._require(service["service_state"], {"starting"}, "mark service ready")
        service["service_state"] = "ready"
        attempt["evidence"].extend(evidence)
        self._event(
            state,
            campaign_id,
            None,
            service_id,
            "campaign_service_ready",
            {"attempt_id": attempt_id},
        )
        self._save(state)

    def mark_campaign_service_uncertain(
        self,
        campaign_id: str,
        service_id: str,
        attempt_id: str,
        *,
        reason: str,
    ) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise GraphError("uncertain reason is required")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        service = self._campaign_service(campaign, service_id)
        attempt = self._active_attempt(service, attempt_id)
        attempt["status"] = "uncertain"
        service["status"] = "reconciling"
        service["service_state"] = "unknown"
        self._event(
            state,
            campaign_id,
            None,
            service_id,
            "campaign_service_uncertain",
            {"attempt_id": attempt_id, "reason": reason},
        )
        self._save(state)

    def reconcile_campaign_service(
        self,
        campaign_id: str,
        service_id: str,
        attempt_id: str,
        *,
        outcome: str,
        evidence: list[str],
    ) -> None:
        if outcome not in {"starting", "ready", "failed", "stopped"}:
            raise GraphError(
                "service reconciliation outcome must be starting, ready, failed, or stopped"
            )
        _nonempty_strings(evidence, "service reconciliation evidence")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        service = self._campaign_service(campaign, service_id)
        self._require(service["status"], {"reconciling"}, "reconcile service")
        attempt = self._attempt(service, attempt_id)
        self._require(attempt["status"], {"uncertain"}, "reconcile service")
        attempt["evidence"].extend(evidence)
        if outcome in {"starting", "ready"}:
            attempt["status"] = "running"
            service["status"] = "running"
            service["service_state"] = outcome
        else:
            attempt["status"] = "failed" if outcome == "failed" else "succeeded"
            attempt["completed_at"] = self.clock()
            service["status"] = attempt["status"]
            service["service_state"] = "unknown" if outcome == "failed" else "stopped"
        self._event(
            state,
            campaign_id,
            None,
            service_id,
            "campaign_service_reconciled",
            {"attempt_id": attempt_id, "outcome": outcome},
        )
        self._save(state)

    def stop_campaign_service(
        self,
        campaign_id: str,
        service_id: str,
        attempt_id: str,
        *,
        evidence: list[str],
    ) -> None:
        _nonempty_strings(evidence, "service stop evidence")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        service = self._campaign_service(campaign, service_id)
        attempt = self._active_attempt(service, attempt_id)
        self._require(
            service["service_state"], {"ready", "starting"}, "stop service"
        )
        service["service_state"] = "stopped"
        service["status"] = "succeeded"
        attempt.update(
            status="succeeded",
            evidence=attempt["evidence"] + list(evidence),
            completed_at=self.clock(),
        )
        self._event(
            state,
            campaign_id,
            None,
            service_id,
            "campaign_service_stopped",
            {"attempt_id": attempt_id},
        )
        self._save(state)

    def ready_jobs(self, campaign_id: str, experiment_id: str) -> list[str]:
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        if campaign["status"] != "running" or experiment["status"] not in {
            "pending",
            "running",
        }:
            return []
        if not self._required_services_ready(campaign, experiment):
            return []
        return [
            job_id
            for job_id, job in experiment["jobs"].items()
            if job["status"] in {"pending", "failed"}
            and all(
                self._dependency_ready(experiment["jobs"][dependency])
                for dependency in job["depends_on"]
            )
        ]

    def start_attempt(
        self,
        campaign_id: str,
        experiment_id: str,
        job_id: str,
        inputs: dict[str, Any],
    ) -> str:
        if not isinstance(inputs, dict):
            raise GraphError("attempt inputs must be an object")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        job = self._job(experiment, job_id)
        self._require(campaign["status"], {"running"}, "start attempt")
        self._require(experiment["status"], {"pending", "running"}, "start attempt")
        if not self._required_services_ready(campaign, experiment):
            raise GraphError("required campaign services are not ready")
        if job_id not in self._ready_job_ids(experiment):
            raise GraphError(f"job is not ready: {job_id}")
        attempt_id = self.id_factory()
        _identifier(attempt_id, "attempt id")
        if any(
            attempt_id == attempt["id"]
            for candidate in campaign["experiments"].values()
            for candidate_job in candidate["jobs"].values()
            for attempt in candidate_job["attempts"]
        ):
            raise GraphError(f"attempt id already exists: {attempt_id}")
        started_at = self.clock()
        attempt = {
            "id": attempt_id,
            "fingerprint": canonical_hash(
                {
                    "campaign": campaign_id,
                    "experiment": experiment_id,
                    "job": job_id,
                    "inputs": inputs,
                }
            ),
            "inputs": copy.deepcopy(inputs),
            "status": "running",
            "evidence": [],
            "started_at": started_at,
            "completed_at": None,
        }
        job["attempts"].append(attempt)
        job["status"] = "running"
        if job["kind"] == "service":
            job["service_state"] = "starting"
        experiment["status"] = "running"
        self._event(
            state,
            campaign_id,
            experiment_id,
            job_id,
            "attempt_started",
            {"attempt_id": attempt_id, "fingerprint": attempt["fingerprint"]},
        )
        self._save(state)
        return attempt_id

    def finish_task(
        self,
        campaign_id: str,
        experiment_id: str,
        job_id: str,
        attempt_id: str,
        *,
        succeeded: bool,
        evidence: list[str],
    ) -> None:
        _nonempty_strings(evidence, "task evidence")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        job = self._job(experiment, job_id)
        if job["kind"] != "task":
            raise GraphError("finish_task requires a task job")
        attempt = self._active_attempt(job, attempt_id)
        outcome = "succeeded" if succeeded else "failed"
        attempt.update(
            status=outcome,
            evidence=list(evidence),
            completed_at=self.clock(),
        )
        job["status"] = outcome
        self._event(
            state,
            campaign_id,
            experiment_id,
            job_id,
            "attempt_finished",
            {"attempt_id": attempt_id, "outcome": outcome},
        )
        self._save(state)

    def mark_service_ready(
        self,
        campaign_id: str,
        experiment_id: str,
        job_id: str,
        attempt_id: str,
        *,
        evidence: list[str],
    ) -> None:
        _nonempty_strings(evidence, "service readiness evidence")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        job = self._job(experiment, job_id)
        if job["kind"] != "service":
            raise GraphError("mark_service_ready requires a service job")
        attempt = self._active_attempt(job, attempt_id)
        self._require(job["service_state"], {"starting"}, "mark service ready")
        job["service_state"] = "ready"
        attempt["evidence"].extend(evidence)
        self._event(
            state,
            campaign_id,
            experiment_id,
            job_id,
            "service_ready",
            {"attempt_id": attempt_id},
        )
        self._save(state)

    def stop_service(
        self,
        campaign_id: str,
        experiment_id: str,
        job_id: str,
        attempt_id: str,
        *,
        evidence: list[str],
    ) -> None:
        _nonempty_strings(evidence, "service stop evidence")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        job = self._job(experiment, job_id)
        if job["kind"] != "service":
            raise GraphError("stop_service requires a service job")
        attempt = self._active_attempt(job, attempt_id)
        self._require(job["service_state"], {"ready", "starting"}, "stop service")
        job["service_state"] = "stopped"
        job["status"] = "succeeded"
        attempt.update(
            status="succeeded",
            evidence=attempt["evidence"] + list(evidence),
            completed_at=self.clock(),
        )
        self._event(
            state,
            campaign_id,
            experiment_id,
            job_id,
            "service_stopped",
            {"attempt_id": attempt_id},
        )
        self._save(state)

    def mark_uncertain(
        self,
        campaign_id: str,
        experiment_id: str,
        job_id: str,
        attempt_id: str,
        *,
        reason: str,
    ) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise GraphError("uncertain reason is required")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        job = self._job(experiment, job_id)
        attempt = self._active_attempt(job, attempt_id)
        attempt["status"] = "uncertain"
        job["status"] = "reconciling"
        self._event(
            state,
            campaign_id,
            experiment_id,
            job_id,
            "attempt_uncertain",
            {"attempt_id": attempt_id, "reason": reason},
        )
        self._save(state)

    def reconcile_attempt(
        self,
        campaign_id: str,
        experiment_id: str,
        job_id: str,
        attempt_id: str,
        *,
        outcome: str,
        evidence: list[str],
    ) -> None:
        if outcome not in {"running", "succeeded", "failed"}:
            raise GraphError("reconciliation outcome must be running, succeeded, or failed")
        _nonempty_strings(evidence, "reconciliation evidence")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        job = self._job(experiment, job_id)
        self._require(job["status"], {"reconciling"}, "reconcile attempt")
        attempt = self._attempt(job, attempt_id)
        self._require(attempt["status"], {"uncertain"}, "reconcile attempt")
        attempt["evidence"].extend(evidence)
        if outcome == "running":
            attempt["status"] = "running"
            job["status"] = "running"
        else:
            attempt["status"] = outcome
            attempt["completed_at"] = self.clock()
            job["status"] = outcome
            if job["kind"] == "service":
                job["service_state"] = "stopped" if outcome == "succeeded" else "unknown"
        self._event(
            state,
            campaign_id,
            experiment_id,
            job_id,
            "attempt_reconciled",
            {"attempt_id": attempt_id, "outcome": outcome},
        )
        self._save(state)

    def finish_experiment(
        self,
        campaign_id: str,
        experiment_id: str,
        *,
        outcome: str,
        conclusion: str,
    ) -> None:
        if outcome not in TERMINAL_EXPERIMENTS:
            raise GraphError("experiment outcome must be succeeded or failed")
        if not isinstance(conclusion, str) or not conclusion.strip():
            raise GraphError("experiment conclusion is required")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        experiment = self._experiment(campaign, experiment_id)
        self._require(experiment["status"], {"pending", "running"}, "finish experiment")
        if not experiment["jobs"]:
            raise GraphError("experiment requires at least one job")
        unfinished = [
            job["id"]
            for job in experiment["jobs"].values()
            if job["status"] != "succeeded"
        ]
        if unfinished:
            raise GraphError(f"experiment has unresolved jobs: {', '.join(unfinished)}")
        experiment.update(
            status=outcome,
            conclusion=conclusion,
            completed_at=self.clock(),
        )
        self._event(
            state,
            campaign_id,
            experiment_id,
            None,
            "experiment_finished",
            {"outcome": outcome},
        )
        self._save(state)

    def finish_campaign(self, campaign_id: str) -> None:
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        self._require(campaign["status"], {"running"}, "finish campaign")
        unresolved = [
            experiment["id"]
            for experiment in campaign["experiments"].values()
            if experiment["status"] not in TERMINAL_EXPERIMENTS
        ]
        if unresolved:
            raise GraphError(f"campaign has unresolved experiments: {', '.join(unresolved)}")
        active_services = [
            service["id"]
            for service in campaign["services"].values()
            if service["service_state"] != "stopped"
        ]
        if active_services:
            raise GraphError(
                f"campaign has active services: {', '.join(active_services)}"
            )
        campaign["status"] = "completed"
        campaign["completed_at"] = self.clock()
        self._event(state, campaign_id, None, None, "campaign_finished", {})
        self._save(state)

    def record_checkpoint(
        self, campaign_id: str, note: str, *, slide_ready: bool = False
    ) -> int:
        if not isinstance(note, str) or not note.strip():
            raise GraphError("checkpoint note is required")
        state = self._load()
        campaign = self._campaign(state, campaign_id)
        self._require(campaign["status"], {"running"}, "record checkpoint")
        self._event(
            state,
            campaign_id,
            None,
            None,
            "slide_checkpoint" if slide_ready else "progress_checkpoint",
            {"note": note},
        )
        sequence = state["events"][-1]["sequence"]
        self._save(state)
        return sequence

    def _ready_job_ids(self, experiment: dict[str, Any]) -> set[str]:
        return {
            job_id
            for job_id, job in experiment["jobs"].items()
            if job["status"] in {"pending", "failed"}
            and all(
                self._dependency_ready(experiment["jobs"][dependency])
                for dependency in job["depends_on"]
            )
        }

    @staticmethod
    def _required_services_ready(
        campaign: dict[str, Any], experiment: dict[str, Any]
    ) -> bool:
        return all(
            campaign["services"][service_id]["status"] == "running"
            and campaign["services"][service_id]["service_state"] == "ready"
            for service_id in experiment["requires_services"]
        )

    @staticmethod
    def _dependency_ready(job: dict[str, Any]) -> bool:
        return job["status"] == "succeeded" or (
            job["kind"] == "service"
            and job["status"] == "running"
            and job["service_state"] == "ready"
        )

    @staticmethod
    def _require(current: str, allowed: set[str], action: str) -> None:
        if current not in allowed:
            raise GraphError(
                f"cannot {action} while {current}; expected {', '.join(sorted(allowed))}"
            )

    @staticmethod
    def _campaign(state: dict[str, Any], campaign_id: str) -> dict[str, Any]:
        try:
            return state["campaigns"][campaign_id]
        except KeyError as exc:
            raise GraphError(f"unknown campaign: {campaign_id}") from exc

    @staticmethod
    def _experiment(
        campaign: dict[str, Any], experiment_id: str
    ) -> dict[str, Any]:
        try:
            return campaign["experiments"][experiment_id]
        except KeyError as exc:
            raise GraphError(f"unknown experiment: {experiment_id}") from exc

    @staticmethod
    def _campaign_service(
        campaign: dict[str, Any], service_id: str
    ) -> dict[str, Any]:
        try:
            return campaign["services"][service_id]
        except KeyError as exc:
            raise GraphError(f"unknown campaign service: {service_id}") from exc

    @staticmethod
    def _job(experiment: dict[str, Any], job_id: str) -> dict[str, Any]:
        try:
            return experiment["jobs"][job_id]
        except KeyError as exc:
            raise GraphError(f"unknown job: {job_id}") from exc

    @staticmethod
    def _attempt(job: dict[str, Any], attempt_id: str) -> dict[str, Any]:
        try:
            return next(
                attempt for attempt in job["attempts"] if attempt["id"] == attempt_id
            )
        except StopIteration as exc:
            raise GraphError(f"unknown attempt: {attempt_id}") from exc

    def _active_attempt(
        self, job: dict[str, Any], attempt_id: str
    ) -> dict[str, Any]:
        attempt = self._attempt(job, attempt_id)
        if attempt["status"] != "running" or job["status"] != "running":
            raise GraphError(f"attempt is not active: {attempt_id}")
        return attempt

    def _event(
        self,
        state: dict[str, Any],
        campaign_id: str,
        experiment_id: str | None,
        job_id: str | None,
        kind: str,
        detail: dict[str, Any],
    ) -> None:
        state["events"].append(
            {
                "sequence": len(state["events"]) + 1,
                "at": self.clock(),
                "campaign_id": campaign_id,
                "experiment_id": experiment_id,
                "job_id": job_id,
                "kind": kind,
                "detail": copy.deepcopy(detail),
            }
        )

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GraphError(f"cannot load graph state: {exc}") from exc
        if (
            not isinstance(state, dict)
            or state.get("schema") != SCHEMA
            or not isinstance(state.get("campaigns"), dict)
            or not isinstance(state.get("events"), list)
        ):
            raise GraphError("invalid graph state")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        temporary.replace(self.path)
