#!/usr/bin/env python3
"""External-state runtime for durable campaign graphs and retained services."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from graph_loop import GraphError, GraphStore


DEFINITION_SCHEMA = "agent-loop-campaign/v1"
ADAPTER_RESPONSE_SCHEMA = "agent-loop-adapter-response/v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class RuntimeError(GraphError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def default_state_root() -> Path:
    configured = os.environ.get("AGENT_LOOP_STATE_HOME")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "agent-loop-toolkit"
    return Path.home() / ".local" / "state" / "agent-loop-toolkit"


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise RuntimeError(f"invalid {label}: {value!r}")
    return value


def _strings(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise RuntimeError(f"{label} must be a non-empty string list")
    return value


def load_definition(path: Path) -> dict[str, Any]:
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read campaign definition {path}: {exc}") from exc
    required = {
        "schema",
        "id",
        "intent",
        "constraints",
        "timezone",
        "progress_interval_seconds",
        "services",
        "experiments",
    }
    allowed = required | {"status_interval_seconds"}
    if not isinstance(definition, dict) or not required.issubset(definition) or not set(
        definition
    ).issubset(allowed):
        raise RuntimeError(
            f"campaign definition fields must include {sorted(required)} "
            f"and may include status_interval_seconds"
        )
    if definition["schema"] != DEFINITION_SCHEMA:
        raise RuntimeError(f"unsupported campaign schema: {definition['schema']!r}")
    _identifier(definition["id"], "campaign id")
    if not isinstance(definition["intent"], str) or not definition["intent"].strip():
        raise RuntimeError("campaign intent is required")
    if not isinstance(definition["constraints"], dict):
        raise RuntimeError("campaign constraints must be an object")
    try:
        ZoneInfo(definition["timezone"])
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise RuntimeError(f"invalid campaign timezone: {definition['timezone']!r}") from exc
    interval = definition["progress_interval_seconds"]
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 30:
        raise RuntimeError("progress interval must be an integer of at least 30 seconds")
    status_interval = definition.get("status_interval_seconds", 7200)
    if (
        not isinstance(status_interval, int)
        or isinstance(status_interval, bool)
        or status_interval < interval
    ):
        raise RuntimeError(
            "status interval must be an integer no smaller than the progress interval"
        )

    service_ids: set[str] = set()
    for service in definition["services"]:
        if not isinstance(service, dict) or set(service) != {
            "id",
            "acceptance",
            "adapter",
            "inputs",
        }:
            raise RuntimeError(
                "each service requires id, acceptance, adapter, and inputs"
            )
        service_id = _identifier(service["id"], "service id")
        if service_id in service_ids:
            raise RuntimeError(f"duplicate service id: {service_id}")
        service_ids.add(service_id)
        _strings(service["acceptance"], f"{service_id} acceptance")
        _strings(service["adapter"], f"{service_id} adapter")
        if not isinstance(service["inputs"], dict):
            raise RuntimeError(f"{service_id} inputs must be an object")

    experiment_ids: set[str] = set()
    for experiment in definition["experiments"]:
        if not isinstance(experiment, dict) or set(experiment) != {
            "id",
            "hypothesis",
            "acceptance",
            "requires_services",
            "jobs",
        }:
            raise RuntimeError(
                "each experiment requires id, hypothesis, acceptance, "
                "requires_services, and jobs"
            )
        experiment_id = _identifier(experiment["id"], "experiment id")
        if experiment_id in experiment_ids:
            raise RuntimeError(f"duplicate experiment id: {experiment_id}")
        experiment_ids.add(experiment_id)
        if (
            not isinstance(experiment["hypothesis"], str)
            or not experiment["hypothesis"].strip()
        ):
            raise RuntimeError(f"{experiment_id} hypothesis is required")
        _strings(experiment["acceptance"], f"{experiment_id} acceptance")
        required_services = experiment["requires_services"]
        if (
            not isinstance(required_services, list)
            or len(set(required_services)) != len(required_services)
            or not all(item in service_ids for item in required_services)
        ):
            raise RuntimeError(f"{experiment_id} has invalid required services")
        job_ids: set[str] = set()
        for job in experiment["jobs"]:
            if not isinstance(job, dict) or set(job) != {
                "id",
                "kind",
                "depends_on",
                "acceptance",
            }:
                raise RuntimeError(
                    f"{experiment_id} jobs require id, kind, depends_on, and acceptance"
                )
            job_id = _identifier(job["id"], "job id")
            if job_id in job_ids:
                raise RuntimeError(f"duplicate job id in {experiment_id}: {job_id}")
            if job["kind"] not in {"task", "service"}:
                raise RuntimeError(f"invalid job kind: {job['kind']!r}")
            dependencies = job["depends_on"]
            if (
                not isinstance(dependencies, list)
                or len(set(dependencies)) != len(dependencies)
                or not all(item in job_ids for item in dependencies)
            ):
                raise RuntimeError(
                    f"{experiment_id}/{job_id} dependencies must name earlier jobs"
                )
            _strings(job["acceptance"], f"{experiment_id}/{job_id} acceptance")
            job_ids.add(job_id)
    if not definition["experiments"]:
        raise RuntimeError("campaign requires at least one experiment")
    return definition


class CampaignRuntime:
    def __init__(
        self,
        project: str,
        campaign_id: str,
        *,
        state_root: Path | None = None,
    ) -> None:
        self.project = _identifier(project, "project id")
        self.campaign_id = _identifier(campaign_id, "campaign id")
        self.root = (state_root or default_state_root()) / project / campaign_id
        self.definition_path = self.root / "definition.json"
        self.source_path = self.root / "source.json"
        self.graph_path = self.root / "graph.json"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "owner.lock"
        self.adapter_root = self.root / "adapter"
        self.queue_root = self.root / "queue"
        self.checkpoint_root = self.root / "checkpoints"

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    f"another coordinator owns {self.project}/{self.campaign_id}"
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps({"pid": os.getpid(), "acquired_at": now()}) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def initialize(self, source: Path) -> None:
        definition = load_definition(source)
        if definition["id"] != self.campaign_id:
            raise RuntimeError("definition campaign id does not match requested state")
        with self.lock():
            if (
                self.graph_path.exists()
                or self.definition_path.exists()
                or self.source_path.exists()
            ):
                raise RuntimeError(f"campaign state already exists: {self.root}")
            self.root.mkdir(parents=True, exist_ok=True)
            self.definition_path.write_text(
                json.dumps(definition, indent=2, ensure_ascii=True, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            self.source_path.write_text(
                json.dumps(
                    {
                        "definition": str(source.resolve()),
                        "root": str(source.resolve().parent),
                    },
                    indent=2,
                    ensure_ascii=True,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            graph = self._graph()
            graph.initialize()
            constraints = dict(definition["constraints"])
            constraints["definition_sha256"] = canonical_hash(definition)
            constraints["timezone"] = definition["timezone"]
            constraints["progress_interval_seconds"] = definition[
                "progress_interval_seconds"
            ]
            constraints["status_interval_seconds"] = definition.get(
                "status_interval_seconds", 7200
            )
            graph.create_campaign(
                self.campaign_id, definition["intent"], constraints=constraints
            )
            for service in definition["services"]:
                graph.add_campaign_service(
                    self.campaign_id, service["id"], service["acceptance"]
                )
            for experiment in definition["experiments"]:
                graph.add_experiment(
                    self.campaign_id,
                    experiment["id"],
                    experiment["hypothesis"],
                    experiment["acceptance"],
                    requires_services=experiment["requires_services"],
                )
                for job in experiment["jobs"]:
                    graph.add_job(
                        self.campaign_id,
                        experiment["id"],
                        job["id"],
                        kind=job["kind"],
                        depends_on=job["depends_on"],
                        acceptance=job["acceptance"],
                    )
            graph.start_campaign(self.campaign_id)
            self._sync_events(graph.snapshot())

    def snapshot(self) -> dict[str, Any]:
        return self._graph().snapshot()

    def queue_experiment(self, source: Path) -> str:
        try:
            experiment = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read experiment definition {source}: {exc}") from exc
        required = {
            "id",
            "hypothesis",
            "acceptance",
            "requires_services",
            "jobs",
        }
        if not isinstance(experiment, dict) or set(experiment) != required:
            raise RuntimeError(
                f"experiment definition fields must be exactly {sorted(required)}"
            )
        experiment_id = _identifier(experiment["id"], "experiment id")
        payload = json.dumps(
            experiment, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        source_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self.lock():
            self.queue_root.mkdir(parents=True, exist_ok=True)
            target = self.queue_root / f"{experiment_id}.json"
            if target.exists():
                raise RuntimeError(
                    f"experiment definition already exists: {experiment_id}"
                )
            graph = self._graph()
            graph.add_experiment_bundle(
                self.campaign_id,
                experiment_id,
                experiment["hypothesis"],
                experiment["acceptance"],
                experiment["jobs"],
                requires_services=experiment["requires_services"],
                source_sha256=source_sha256,
            )
            target.write_text(
                json.dumps(experiment, indent=2, ensure_ascii=True, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            self._sync_events(graph.snapshot())
        return experiment_id

    def start_service(self, service_id: str) -> dict[str, Any]:
        with self.lock():
            definition = self._definition()
            service = self._service_definition(definition, service_id)
            graph = self._graph()
            attempt_id = graph.start_campaign_service(
                self.campaign_id, service_id, service["inputs"]
            )
            self._sync_events(graph.snapshot())
            try:
                response = self._call_adapter(
                    service, service_id, attempt_id, "start"
                )
            except Exception as exc:
                graph.mark_campaign_service_uncertain(
                    self.campaign_id,
                    service_id,
                    attempt_id,
                    reason=f"adapter outcome unknown: {exc}",
                )
                self._sync_events(graph.snapshot())
                raise
            state = response["state"]
            if state == "ready":
                graph.mark_campaign_service_ready(
                    self.campaign_id,
                    service_id,
                    attempt_id,
                    evidence=response["evidence"],
                )
            elif state != "starting":
                graph.mark_campaign_service_uncertain(
                    self.campaign_id,
                    service_id,
                    attempt_id,
                    reason=f"unexpected start state: {state}",
                )
                self._sync_events(graph.snapshot())
                raise RuntimeError(f"adapter start returned invalid state: {state}")
            self._sync_events(graph.snapshot())
            return response

    def reconcile_service(self, service_id: str) -> dict[str, Any]:
        with self.lock():
            definition = self._definition()
            service_definition = self._service_definition(definition, service_id)
            graph = self._graph()
            service = graph.snapshot()["campaigns"][self.campaign_id]["services"][
                service_id
            ]
            if not service["attempts"]:
                raise RuntimeError(f"service has no attempt: {service_id}")
            attempt_id = service["attempts"][-1]["id"]
            response = self._call_adapter(
                service_definition, service_id, attempt_id, "status"
            )
            current = service["status"]
            state = response["state"]
            if current == "reconciling":
                outcome = {
                    "starting": "starting",
                    "ready": "ready",
                    "failed": "failed",
                    "stopped": "stopped",
                }.get(state)
                if outcome is None:
                    raise RuntimeError(f"adapter status returned invalid state: {state}")
                graph.reconcile_campaign_service(
                    self.campaign_id,
                    service_id,
                    attempt_id,
                    outcome=outcome,
                    evidence=response["evidence"],
                )
            elif current == "running" and service["service_state"] == "starting":
                if state == "ready":
                    graph.mark_campaign_service_ready(
                        self.campaign_id,
                        service_id,
                        attempt_id,
                        evidence=response["evidence"],
                    )
                elif state != "starting":
                    graph.mark_campaign_service_uncertain(
                        self.campaign_id,
                        service_id,
                        attempt_id,
                        reason=f"unexpected status state: {state}",
                    )
                    self._sync_events(graph.snapshot())
                    raise RuntimeError(f"adapter status returned invalid state: {state}")
            elif state != service["service_state"]:
                graph.mark_campaign_service_uncertain(
                    self.campaign_id,
                    service_id,
                    attempt_id,
                    reason=(
                        f"adapter observed {state}, graph recorded "
                        f"{service['service_state']}"
                    ),
                )
            self._sync_events(graph.snapshot())
            return response

    def stop_service(self, service_id: str) -> dict[str, Any]:
        with self.lock():
            definition = self._definition()
            service_definition = self._service_definition(definition, service_id)
            graph = self._graph()
            service = graph.snapshot()["campaigns"][self.campaign_id]["services"][
                service_id
            ]
            if service["status"] != "running" or not service["attempts"]:
                raise RuntimeError(f"service is not running: {service_id}")
            attempt_id = service["attempts"][-1]["id"]
            try:
                response = self._call_adapter(
                    service_definition, service_id, attempt_id, "stop"
                )
            except Exception as exc:
                graph.mark_campaign_service_uncertain(
                    self.campaign_id,
                    service_id,
                    attempt_id,
                    reason=f"stop outcome unknown: {exc}",
                )
                self._sync_events(graph.snapshot())
                raise
            if response["state"] != "stopped":
                graph.mark_campaign_service_uncertain(
                    self.campaign_id,
                    service_id,
                    attempt_id,
                    reason=f"unexpected stop state: {response['state']}",
                )
                self._sync_events(graph.snapshot())
                raise RuntimeError(
                    f"adapter stop returned invalid state: {response['state']}"
                )
            graph.stop_campaign_service(
                self.campaign_id,
                service_id,
                attempt_id,
                evidence=response["evidence"],
            )
            self._sync_events(graph.snapshot())
            return response

    def record_service_reconciliation(
        self,
        service_id: str,
        *,
        outcome: str,
        evidence: list[str],
    ) -> None:
        with self.lock():
            graph = self._graph()
            service = graph.snapshot()["campaigns"][self.campaign_id]["services"][
                service_id
            ]
            if service["status"] != "reconciling" or not service["attempts"]:
                raise RuntimeError(f"service is not awaiting reconciliation: {service_id}")
            graph.reconcile_campaign_service(
                self.campaign_id,
                service_id,
                service["attempts"][-1]["id"],
                outcome=outcome,
                evidence=evidence,
            )
            self._sync_events(graph.snapshot())

    def complete_task(
        self,
        experiment_id: str,
        job_id: str,
        *,
        evidence: list[str],
    ) -> None:
        with self.lock():
            graph = self._graph()
            attempt_id = graph.start_attempt(
                self.campaign_id,
                experiment_id,
                job_id,
                {"recorded_by": "campaign-runtime"},
            )
            graph.finish_task(
                self.campaign_id,
                experiment_id,
                job_id,
                attempt_id,
                succeeded=True,
                evidence=evidence,
            )
            self._sync_events(graph.snapshot())

    def start_job(
        self, experiment_id: str, job_id: str, *, inputs: dict[str, Any]
    ) -> str:
        with self.lock():
            graph = self._graph()
            attempt_id = graph.start_attempt(
                self.campaign_id, experiment_id, job_id, inputs
            )
            self._sync_events(graph.snapshot())
            return attempt_id

    def finish_task_attempt(
        self,
        experiment_id: str,
        job_id: str,
        attempt_id: str,
        *,
        succeeded: bool,
        evidence: list[str],
    ) -> None:
        with self.lock():
            graph = self._graph()
            graph.finish_task(
                self.campaign_id,
                experiment_id,
                job_id,
                attempt_id,
                succeeded=succeeded,
                evidence=evidence,
            )
            self._sync_events(graph.snapshot())

    def mark_job_uncertain(
        self, experiment_id: str, job_id: str, attempt_id: str, *, reason: str
    ) -> None:
        with self.lock():
            graph = self._graph()
            graph.mark_uncertain(
                self.campaign_id,
                experiment_id,
                job_id,
                attempt_id,
                reason=reason,
            )
            self._sync_events(graph.snapshot())

    def reconcile_job(
        self,
        experiment_id: str,
        job_id: str,
        attempt_id: str,
        *,
        outcome: str,
        evidence: list[str],
    ) -> None:
        with self.lock():
            graph = self._graph()
            graph.reconcile_attempt(
                self.campaign_id,
                experiment_id,
                job_id,
                attempt_id,
                outcome=outcome,
                evidence=evidence,
            )
            self._sync_events(graph.snapshot())

    def finish_experiment(
        self,
        experiment_id: str,
        *,
        outcome: str,
        conclusion: str,
    ) -> None:
        with self.lock():
            graph = self._graph()
            graph.finish_experiment(
                self.campaign_id,
                experiment_id,
                outcome=outcome,
                conclusion=conclusion,
            )
            self._sync_events(graph.snapshot())

    def render_status(self) -> str:
        status = self.status_document()
        campaign = status["campaign"]
        definition = self._definition()
        local = datetime.now(ZoneInfo(definition["timezone"])).isoformat()
        lines = [
            f"[{local}] {self.project}/{self.campaign_id}: {campaign['status']}",
        ]
        for service in campaign["services"].values():
            lines.append(
                f"service {service['id']}: {service['status']}/"
                f"{service['service_state']}"
            )
        for experiment in campaign["experiments"].values():
            jobs = ", ".join(
                f"{job['id']}={job['status']}" for job in experiment["jobs"].values()
            )
            lines.append(
                f"experiment {experiment['id']}: {experiment['status']}"
                + (f" ({jobs})" if jobs else "")
            )
        last = status["last_event"]
        lines.append(
            f"last event: #{last['sequence']} {last['kind']} at {last['at']}"
        )
        lines.append(f"next progress due: {status['next_progress_due']}")
        lines.append(f"next slide-ready status due: {status['next_status_due']}")
        lines.append(f"next: {status['next_action']}")
        return "\n".join(lines)

    def status_document(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        campaign = snapshot["campaigns"][self.campaign_id]
        events = snapshot["events"]
        started = next(
            event for event in events if event["kind"] == "campaign_started"
        )
        progress = next(
            (
                event
                for event in reversed(events)
                if event["kind"] in {"progress_checkpoint", "slide_checkpoint"}
            ),
            started,
        )
        status = next(
            (
                event
                for event in reversed(events)
                if event["kind"] == "slide_checkpoint"
            ),
            started,
        )
        constraints = campaign["constraints"]
        progress_due = datetime.fromisoformat(progress["at"]) + timedelta(
            seconds=constraints["progress_interval_seconds"]
        )
        status_due = datetime.fromisoformat(status["at"]) + timedelta(
            seconds=constraints["status_interval_seconds"]
        )
        jobs = [
            job
            for experiment in campaign["experiments"].values()
            for job in experiment["jobs"].values()
        ]
        return {
            "schema": "agent-loop-campaign-status/v1",
            "project": self.project,
            "campaign_id": self.campaign_id,
            "campaign": campaign,
            "constraints": constraints,
            "queue_summary": {
                state: sum(job["status"] == state for job in jobs)
                for state in (
                    "pending",
                    "running",
                    "reconciling",
                    "succeeded",
                    "failed",
                )
            },
            "last_event": events[-1],
            "next_progress_due": progress_due.isoformat(),
            "next_status_due": status_due.isoformat(),
            "next_action": self._next_action(campaign),
        }

    def checkpoint(self, note: str, *, slide_ready: bool = False) -> Path:
        with self.lock():
            graph = self._graph()
            sequence = graph.record_checkpoint(
                self.campaign_id, note, slide_ready=slide_ready
            )
            self._sync_events(graph.snapshot())
            document = self.status_document()
            self.checkpoint_root.mkdir(parents=True, exist_ok=True)
            target = self.checkpoint_root / f"{sequence:06d}.json"
            target.write_text(
                json.dumps(document, indent=2, ensure_ascii=True, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            return target

    def watch(self) -> None:
        definition = self._definition()
        interval = definition["progress_interval_seconds"]
        while True:
            print(self.render_status(), flush=True)
            time.sleep(interval)

    def _definition(self) -> dict[str, Any]:
        return load_definition(self.definition_path)

    @staticmethod
    def _next_action(campaign: dict[str, Any]) -> str:
        for service in campaign["services"].values():
            if service["status"] == "reconciling":
                return f"reconcile original service {service['id']}"
            if service["status"] in {"pending", "failed"}:
                return f"start service {service['id']}"
            if service["service_state"] == "starting":
                return f"observe service {service['id']} readiness"
        for experiment in campaign["experiments"].values():
            if experiment["status"] in {"pending", "running"}:
                for job in experiment["jobs"].values():
                    if job["status"] in {"pending", "failed"}:
                        return f"run {experiment['id']}/{job['id']}"
                return f"conclude experiment {experiment['id']}"
        if any(
            service["service_state"] == "ready"
            for service in campaign["services"].values()
        ):
            return "retained service is ready for another experiment or explicit final cleanup"
        return "finish campaign"

    def _graph(self) -> GraphStore:
        return GraphStore(self.graph_path, clock=now)

    def _service_definition(
        self, definition: dict[str, Any], service_id: str
    ) -> dict[str, Any]:
        _identifier(service_id, "service id")
        try:
            return next(
                service
                for service in definition["services"]
                if service["id"] == service_id
            )
        except StopIteration as exc:
            raise RuntimeError(f"unknown service: {service_id}") from exc

    def _call_adapter(
        self,
        service: dict[str, Any],
        service_id: str,
        attempt_id: str,
        action: str,
    ) -> dict[str, Any]:
        attempt_root = self.adapter_root / attempt_id
        attempt_root.mkdir(parents=True, exist_ok=True)
        request_path = attempt_root / f"{action}-request.json"
        response_path = attempt_root / f"{action}-response.json"
        request = {
            "schema": "agent-loop-adapter-request/v1",
            "project": self.project,
            "campaign_id": self.campaign_id,
            "service_id": service_id,
            "attempt_id": attempt_id,
            "action": action,
            "inputs": service["inputs"],
            "previous_response": self._previous_adapter_response(
                attempt_root, action
            ),
        }
        request_path.write_text(
            json.dumps(request, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        response_path.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment.update(
            AGENT_LOOP_REQUEST=str(request_path),
            AGENT_LOOP_RESPONSE=str(response_path),
        )
        completed = subprocess.run(
            service["adapter"],
            env=environment,
            cwd=self._source_root(),
            timeout=service["inputs"].get("adapter_timeout_seconds", 1800),
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(
                f"adapter {action} exited with status {completed.returncode}"
            )
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"adapter did not write a valid response: {exc}") from exc
        required = {
            "schema",
            "campaign_id",
            "service_id",
            "attempt_id",
            "action",
            "state",
            "evidence",
            "identity",
        }
        if not isinstance(response, dict) or set(response) != required:
            raise RuntimeError("adapter response fields are invalid")
        if response["schema"] != ADAPTER_RESPONSE_SCHEMA:
            raise RuntimeError("adapter response schema is invalid")
        for key, expected in {
            "campaign_id": self.campaign_id,
            "service_id": service_id,
            "attempt_id": attempt_id,
            "action": action,
        }.items():
            if response[key] != expected:
                raise RuntimeError(f"adapter response has wrong {key}")
        _strings(response["evidence"], "adapter evidence")
        if not isinstance(response["identity"], dict):
            raise RuntimeError("adapter identity must be an object")
        return response

    def _source_root(self) -> Path:
        try:
            source = json.loads(self.source_path.read_text(encoding="utf-8"))
            root = Path(source["root"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"cannot read campaign source metadata: {exc}") from exc
        if not root.is_dir():
            raise RuntimeError(f"campaign source root is unavailable: {root}")
        return root

    @staticmethod
    def _previous_adapter_response(
        attempt_root: Path, action: str
    ) -> dict[str, Any] | None:
        candidates = (
            ["status-response.json", "start-response.json"]
            if action == "status"
            else ["status-response.json", "start-response.json"]
            if action == "stop"
            else []
        )
        for name in candidates:
            path = attempt_root / name
            if path.exists():
                try:
                    response = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"cannot read previous adapter response: {exc}"
                    ) from exc
                if not isinstance(response, dict):
                    raise RuntimeError("previous adapter response is not an object")
                return response
        return None

    def _sync_events(self, snapshot: dict[str, Any]) -> None:
        existing = 0
        if self.events_path.exists():
            try:
                with self.events_path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            event = json.loads(line)
                            existing += 1
                            if event["sequence"] != existing:
                                raise RuntimeError("event journal sequence is invalid")
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                raise RuntimeError(f"cannot validate event journal: {exc}") from exc
        events = snapshot["events"]
        if existing > len(events):
            raise RuntimeError("event journal is ahead of graph state")
        if events[:existing] and events[existing - 1]["sequence"] != existing:
            raise RuntimeError("event journal does not match graph state")
        if existing == len(events):
            return
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            for event in events[existing:]:
                handle.write(
                    json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
