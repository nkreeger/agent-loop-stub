#!/usr/bin/env python3
"""Command-line interface for external campaign state."""

from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys

from campaign_runtime import CampaignRuntime, RuntimeError, load_definition
from graph_loop import GraphError


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser()
    command.add_argument("--state-root", type=Path)
    subcommands = command.add_subparsers(dest="command", required=True)

    initialize = subcommands.add_parser("init")
    initialize.add_argument("--project", required=True)
    initialize.add_argument("--definition", type=Path, required=True)

    for name in ("status", "watch"):
        selected = subcommands.add_parser(name)
        selected.add_argument("--project", required=True)
        selected.add_argument("--campaign", required=True)
        if name == "status":
            selected.add_argument("--json", action="store_true")

    checkpoint = subcommands.add_parser("checkpoint")
    checkpoint.add_argument("--project", required=True)
    checkpoint.add_argument("--campaign", required=True)
    checkpoint.add_argument("--note", required=True)
    checkpoint.add_argument("--slides", action="store_true")

    service = subcommands.add_parser("service")
    service.add_argument(
        "action", choices=("start", "reconcile", "record-reconciliation", "stop")
    )
    service.add_argument("--project", required=True)
    service.add_argument("--campaign", required=True)
    service.add_argument("--service", required=True)
    service.add_argument(
        "--outcome", choices=("starting", "ready", "failed", "stopped")
    )
    service.add_argument("--evidence", action="append", default=[])

    task = subcommands.add_parser("task")
    task.add_argument(
        "action", choices=("complete", "start", "finish", "uncertain", "reconcile")
    )
    task.add_argument("--project", required=True)
    task.add_argument("--campaign", required=True)
    task.add_argument("--experiment", required=True)
    task.add_argument("--job", required=True)
    task.add_argument("--attempt")
    task.add_argument("--inputs", type=json.loads, default={})
    task.add_argument("--evidence", action="append", default=[])
    task.add_argument("--reason")
    task.add_argument("--outcome", choices=("running", "succeeded", "failed"))
    task.add_argument("--failed", action="store_true")

    experiment = subcommands.add_parser("experiment")
    experiment.add_argument("action", choices=("add", "finish"))
    experiment.add_argument("--project", required=True)
    experiment.add_argument("--campaign", required=True)
    experiment.add_argument("--experiment")
    experiment.add_argument("--file", type=Path)
    experiment.add_argument("--outcome", choices=("succeeded", "failed"))
    experiment.add_argument("--conclusion")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            definition = load_definition(args.definition)
            runtime = CampaignRuntime(
                args.project, definition["id"], state_root=args.state_root
            )
            runtime.initialize(args.definition)
            print(runtime.render_status())
        else:
            runtime = CampaignRuntime(
                args.project, args.campaign, state_root=args.state_root
            )
            if args.command == "status":
                if args.json:
                    print(json.dumps(runtime.status_document(), indent=2, sort_keys=True))
                else:
                    print(runtime.render_status())
            elif args.command == "watch":
                runtime.watch()
            elif args.command == "checkpoint":
                path = runtime.checkpoint(args.note, slide_ready=args.slides)
                print(path)
            elif args.action == "start":
                runtime.start_service(args.service)
                print(runtime.render_status())
            elif args.action == "reconcile":
                runtime.reconcile_service(args.service)
                print(runtime.render_status())
            elif args.action == "record-reconciliation":
                if args.outcome is None or not args.evidence:
                    raise RuntimeError(
                        "record-reconciliation requires --outcome and --evidence"
                    )
                runtime.record_service_reconciliation(
                    args.service, outcome=args.outcome, evidence=args.evidence
                )
                print(runtime.render_status())
            elif args.action == "stop":
                runtime.stop_service(args.service)
                print(runtime.render_status())
            elif args.command == "task":
                if args.action == "complete":
                    if not args.evidence:
                        raise RuntimeError("task complete requires --evidence")
                    runtime.complete_task(
                        args.experiment, args.job, evidence=args.evidence
                    )
                elif args.action == "start":
                    print(
                        runtime.start_job(
                            args.experiment, args.job, inputs=args.inputs
                        )
                    )
                elif args.action == "finish":
                    if args.attempt is None or not args.evidence:
                        raise RuntimeError(
                            "task finish requires --attempt and --evidence"
                        )
                    runtime.finish_task_attempt(
                        args.experiment,
                        args.job,
                        args.attempt,
                        succeeded=not args.failed,
                        evidence=args.evidence,
                    )
                elif args.action == "uncertain":
                    if args.attempt is None or args.reason is None:
                        raise RuntimeError(
                            "task uncertain requires --attempt and --reason"
                        )
                    runtime.mark_job_uncertain(
                        args.experiment,
                        args.job,
                        args.attempt,
                        reason=args.reason,
                    )
                elif args.action == "reconcile":
                    if (
                        args.attempt is None
                        or args.outcome is None
                        or not args.evidence
                    ):
                        raise RuntimeError(
                            "task reconcile requires --attempt, --outcome, and --evidence"
                        )
                    runtime.reconcile_job(
                        args.experiment,
                        args.job,
                        args.attempt,
                        outcome=args.outcome,
                        evidence=args.evidence,
                    )
                print(runtime.render_status())
            elif args.command == "experiment":
                if args.action == "add":
                    if args.file is None:
                        raise RuntimeError("experiment add requires --file")
                    print(runtime.queue_experiment(args.file))
                else:
                    if (
                        args.experiment is None
                        or args.outcome is None
                        or args.conclusion is None
                    ):
                        raise RuntimeError(
                            "experiment finish requires --experiment, "
                            "--outcome, and --conclusion"
                        )
                    runtime.finish_experiment(
                        args.experiment,
                        outcome=args.outcome,
                        conclusion=args.conclusion,
                    )
                print(runtime.render_status())
        return 0
    except (GraphError, RuntimeError, OSError, ValueError) as exc:
        print(f"loopctl: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
