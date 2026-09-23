"""Three-step, no-model proof of a real PostgreSQL notebook/Codespace handoff."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from uuid import uuid4

from dotenv import load_dotenv

from .audit import read_json, replace_json, write_json_exclusive
from .cli import _record_outcome
from .codex_cli import ScriptedBackend
from .dry_run import (
    _active_plan, _astra, _create_run, _end, _review, _review_package,
    _sol_result, _spec, _task,
)
from .graph import OrchestrationRuntime, initial_state
from .shared import SharedRunStore, SharedStateError


REQUEST_ID = "shared-probe-human-pause"
REVIEW_ID = "shared-probe-review"
ARTIFACT_TEXT = "Synthetic shared-resume fixture; no project experiment.\n"


class ProbeBoundary(BaseException):
    """Stop after the review checkpoint without executing a Reviewer."""


class StopBeforeReview(OrchestrationRuntime):
    def _review_node(self, state):
        raise ProbeBoundary()


def _fixture(run_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("probe run ID must use only letters, digits, dash, or underscore")
    root = (Path(tempfile.gettempdir()) / "astra-shared-probe" / run_id).resolve()
    repository = Path(__file__).resolve().parents[1]
    if root == repository or root.is_relative_to(repository):
        raise ValueError("synthetic probe fixture must stay outside the repository")
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    artifact = project / "evidence.txt"
    if artifact.exists():
        if artifact.read_text(encoding="utf-8") != ARTIFACT_TEXT:
            raise ValueError("existing synthetic probe artifact differs")
    else:
        artifact.write_text(ARTIFACT_TEXT, encoding="utf-8")
    return project, artifact


def _host_root(run_dir: Path, project: Path) -> None:
    path = run_dir / "host-roots.json"
    roots = read_json(path)["roots"]
    if str(project) not in roots:
        replace_json(path, {"roots": [*roots, str(project)]})


def _summary(runtime: OrchestrationRuntime, phase: str) -> dict:
    state = runtime.status()
    checkpoint = runtime.graph.get_state(runtime.config).config["configurable"]["checkpoint_id"]
    return {
        "run_id": runtime.run_id,
        "phase": phase,
        "status": state["status"],
        "route": state["route"],
        "checkpoint_id": checkpoint,
        "call_sequence": state["call_sequence"],
        "completed_receipts": len(list(runtime.audit.calls_dir.glob("*/receipt.json"))),
        "model_calls": 0,
        "real_experiments": False,
    }


def run(phase: str, run_id: str, run_dir: Path) -> dict:
    load_dotenv(override=False)
    url = os.environ.get("ASTRA_STATE_DATABASE_URL", "")
    if not url:
        raise SharedStateError("ASTRA_STATE_DATABASE_URL is required for the shared probe")
    project, artifact = _fixture(run_id)
    run_dir = run_dir.resolve()
    if phase == "start":
        spec = _spec(project)
        backend = ScriptedBackend({
            "astra": [_astra("USER", 1, user_request={
                "request_id": REQUEST_ID,
                "category": "AUTH_OR_HUMAN_ACTION_REQUIRED",
                "prompt": "Confirm this synthetic handoff fixture.",
                "required_actions": ["Confirm only this synthetic fixture."],
            })],
        })
        with SharedRunStore(run_id, url, run_dir) as store:
            if store.exists():
                raise ValueError("probe run ID already exists in shared storage")
            _create_run(run_dir, spec, run_id)
            write_json_exclusive(run_dir / "host-roots.json", {"roots": [str(project)]})
            store.save()
            with OrchestrationRuntime(run_dir, backend, shared_store=store, project_root=project) as runtime:
                runtime.invoke(initial_state(spec, run_id))
                summary = _summary(runtime, phase)
                state = runtime.status()
            if summary["status"] != "PAUSED_USER":
                raise AssertionError("synthetic start did not pause at USER")
            _record_outcome(run_dir, state)
            store.save()
        return summary

    with SharedRunStore(run_id, url, run_dir) as store:
        store.fetch(restore_sessions=False)
        if read_json(run_dir / "manifest.json")["run_id"] != run_id:
            raise ValueError("probe manifest run ID mismatch")
        _host_root(run_dir, project)
        store.save()
        if phase == "continue":
            spec = _spec(project)
            plan = _active_plan(spec)
            task = _task("shared-probe-task", "Produce synthetic handoff evidence.")
            result = _sol_result(task, "MILESTONE_COMPLETE", "Synthetic handoff evidence is ready.", artifact)
            proposal = "Complete only after a fresh independent PASS."
            packet = _review_package(
                REVIEW_ID, spec, plan, task, result, proposal,
                sol_attempts=1, task_attempts={task["task_id"]: 1},
            )
            backend = ScriptedBackend({
                "astra": [
                    _astra("SOL", 2, sol_task=task),
                    _astra("REVIEW", 3, proposed_next_plan=proposal, review_package=packet),
                ],
                "sol": [result],
            })
            with StopBeforeReview(run_dir, backend, shared_store=store, project_root=project) as runtime:
                if runtime.status()["status"] != "PAUSED_USER":
                    raise ValueError("probe continuation requires the original USER checkpoint")
                try:
                    runtime.resume({
                        "request_id": REQUEST_ID, "status": "PROVIDED",
                        "response": "synthetic confirmation", "evidence_artifact_paths": [],
                    })
                except ProbeBoundary:
                    pass
                summary = _summary(runtime, phase)
                state = runtime.status()
            if summary["route"] != "REVIEW" or summary["call_sequence"] != 4:
                raise AssertionError("synthetic continuation did not reach the review boundary")
        elif phase == "finish":
            backend = ScriptedBackend({
                "reviewer": [_review(REVIEW_ID, "PASS")],
                "astra": [_end(4, REVIEW_ID)],
            })
            with OrchestrationRuntime(run_dir, backend, shared_store=store, project_root=project) as runtime:
                if runtime.status()["route"] != "REVIEW":
                    raise ValueError("probe finish requires the saved review checkpoint")
                runtime.continue_after_restart()
                summary = _summary(runtime, phase)
                state = runtime.status()
            if summary["status"] != "COMPLETED" or summary["completed_receipts"] != 6:
                raise AssertionError("synthetic handoff did not finish without repeated role calls")
        else:
            raise ValueError("unsupported probe phase")
        _record_outcome(run_dir, state)
        store.save()
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("start", "continue", "finish"))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.phase != "start" and not args.run_id:
        parser.error("--run-id is required for continue and finish")
    try:
        summary = run(args.phase, args.run_id or str(uuid4()), args.run_dir)
    except Exception as error:
        print(f"shared probe error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
