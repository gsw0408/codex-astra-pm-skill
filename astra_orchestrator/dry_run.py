"""Deterministic schema-v2 proof with no Codex, network, or experiment calls."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .audit import (
    AuditLog,
    canonical_json,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now,
    write_json_exclusive,
)
from .codex_cli import (
    BackendResult,
    BackendUsageLimitExceeded,
    FIXED_ROLE_SETTINGS,
    ScriptedBackend,
)
from .graph import OrchestrationRuntime, USAGE_LIMIT_STOP_CODE, initial_state
from .observability import LangSmithObserver
from .schema import SCHEMA_VERSION


ULTIMATE_PURPOSE = (
    "Prove the orchestration control policy without running real experiments or "
    "modifying approved project evidence."
)


def _spec(project: Path, *, sol_limit: int = 10) -> dict[str, Any]:
    return {
        "project_id": "synthetic-orchestrator-dry-run",
        "project_root": str(project.resolve()),
        "project_context": "Synthetic control-plane fixture; no real project work is authorized.",
        "ultimate_purpose": ULTIMATE_PURPOSE,
        "target_stage": "verified",
        "stages": [{
            "id": "verified",
            "goal": "Prove deterministic orchestration routes and durable evidence.",
            "acceptance_criteria": [
                "Scripted worker evidence is independently reviewed.",
                "Only Astra approves target-stage completion.",
            ],
            "requires_review": True,
        }],
        "experiment_plan": ["Use scripted control-plane fixtures only."],
        "datasets": ["Synthetic text fixture only."],
        "evaluation_criteria": ["Inspect durable route and session evidence."],
        "approaches": ["Deterministic ScriptedBackend execution."],
        "limits": {
            "max_sol_attempts_per_stage": sol_limit,
            "max_review_attempts_per_stage": 6,
            "max_transitions": 80,
            "max_model_errors": 3,
        },
    }


def _active_plan(spec: Mapping[str, Any], revision: str = "initial") -> dict[str, Any]:
    return {
        "revision_id": revision,
        "current_stage": "verified",
        "project_context": spec["project_context"],
        "target_stage": spec["target_stage"],
        "stages": deepcopy(spec["stages"]),
        "experiment_plan": list(spec["experiment_plan"]),
        "datasets": list(spec["datasets"]),
        "evaluation_criteria": list(spec["evaluation_criteria"]),
        "approaches": list(spec["approaches"]),
    }


def _strategy(issue: str, attempt: int, approach: str, difference: str) -> dict[str, Any]:
    return {
        "issue_id": issue,
        "strategy_id": f"{issue}-strategy-{attempt}",
        "attempt": attempt,
        "approach": approach,
        "rationale": f"Attempt bounded automatic recovery using {approach}.",
        "difference_from_prior": difference,
    }


def _issue_id(prefix: str, producer_id: str, call_sequence: int, role: str) -> str:
    """Mirror the controller's call-bound episode key for safe fixture scripting."""

    return f"{prefix}-{call_sequence:04d}-{role}-{producer_id}"


def _task(
    task_id: str,
    objective: str,
    *,
    revision: str = "initial",
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "task_id": task_id,
        "stage_id": "verified",
        "plan_revision_id": revision,
        "objective": objective,
        "acceptance_criteria": ["Produce inspectable synthetic control-plane evidence."],
        "constraints": ["Do not run Codex, network access, training, or experiments."],
        "targeted_checks": ["Inspect only supplied synthetic evidence."],
        "forbidden_areas": ["Real project code, data, experiments, and approved evidence."],
    }
    if recovery:
        value["recovery_strategy"] = dict(recovery)
    return value


def _luna_task(
    research_id: str,
    question: str,
    *,
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "research_id": research_id,
        "stage_id": "verified",
        "plan_revision_id": "initial",
        "question": question,
        "scope": ["Synthetic orchestration fixture and local evidence only."],
        "sources_to_consult": ["Supplied local synthetic evidence."],
        "deliverables": ["A source-bound factual summary for Astra."],
        "constraints": [
            "Research only; do not manage Sol, review milestones, or approve transitions."
        ],
    }
    if recovery:
        value["recovery_strategy"] = dict(recovery)
    return value


def _artifact(path: Path, description: str) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha256_file(path), "description": description}


def _sol_result(
    task: Mapping[str, Any], status: str, summary: str, artifact: Path | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "task_id": task["task_id"],
        "stage_id": task["stage_id"],
        "plan_revision_id": task["plan_revision_id"],
        "status": status,
        "summary": summary,
        "evidence": [],
        "evidence_artifact_paths": [],
        "known_limitations": ["Synthetic control-plane proof, not a scientific result."],
        "blockers": [],
        "user_actions_requested": [],
    }
    if artifact:
        value["evidence"] = ["A stable synthetic artifact is available for inspection."]
        value["evidence_artifact_paths"] = [_artifact(artifact, "Immutable synthetic evidence")]
    if status == "BLOCKED":
        value["blockers"] = ["The scripted automatic approach did not resolve the issue."]
    elif status == "NEEDS_USER":
        value["user_actions_requested"] = ["Complete the action described to Astra."]
    elif status == "FAILED":
        value["error"] = "The scripted recovery strategy failed safely."
    return value


def _luna_result(
    task: Mapping[str, Any], status: str, summary: str, artifact: Path
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "research_id": task["research_id"],
        "stage_id": task["stage_id"],
        "plan_revision_id": task["plan_revision_id"],
        "status": status,
        "summary": summary,
        "findings": [],
        "sources": [],
        "evidence_artifact_paths": [],
        "known_limitations": [],
        "blockers": [],
    }
    if status == "DONE":
        source = _artifact(artifact, "Synthetic local research source")
        value.update(
            findings=["The local fixture supports the bounded orchestration claim."],
            sources=[source],
            evidence_artifact_paths=[dict(source)],
        )
    elif status == "INSUFFICIENT_EVIDENCE":
        value["known_limitations"] = ["The selected sources did not answer the question."]
    elif status == "BLOCKED":
        value["blockers"] = ["The selected local inspection method could not recover evidence."]
    elif status == "FAILED":
        value["error"] = "The scripted research strategy failed safely."
    return value


def _review_package(
    review_id: str,
    spec: Mapping[str, Any],
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    result: Mapping[str, Any],
    proposal: str,
    *,
    sol_attempts: int,
    review_attempts: int = 0,
    task_attempts: Mapping[str, int] | None = None,
    correction_ready: bool = False,
) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "stage_id": "verified",
        "plan_revision_id": plan["revision_id"],
        "ultimate_purpose": spec["ultimate_purpose"],
        "project_root": spec["project_root"],
        "project_context": plan["project_context"],
        "milestone_goal": plan["stages"][0]["goal"],
        "acceptance_criteria": plan["stages"][0]["acceptance_criteria"],
        "sol_instructions": dict(task),
        "actual_results": dict(result),
        "evidence_artifact_paths": deepcopy(result["evidence_artifact_paths"]),
        "known_limitations": list(result["known_limitations"]),
        "current_state": {
            "active_plan": deepcopy(plan),
            "current_stage": "verified",
            "completed_stages": [],
            "target_stage": plan["target_stage"],
            "target_stage_completed": False,
            "execution_limits": deepcopy(spec["limits"]),
            "retry_counts": {
                "sol_attempts_by_stage": {"verified": sol_attempts},
                "sol_attempts_by_task": dict(task_attempts or {}),
                "review_attempts_by_stage": (
                    {"verified": review_attempts} if review_attempts else {}
                ),
            },
            "correction_required": False,
            "correction_ready": correction_ready,
            "last_sol_task_id": task["task_id"],
            "last_sol_status": result["status"],
        },
        "proposed_next_plan": proposal,
    }


def _review(review_id: str, verdict: str, revision: str = "initial") -> dict[str, Any]:
    return {
        "review_id": review_id,
        "stage_id": "verified",
        "plan_revision_id": revision,
        "ultimate_purpose": ULTIMATE_PURPOSE,
        "verdict": verdict,
        "summary": f"Independent scripted verdict: {verdict}.",
        "findings": ["A criterion is not demonstrated."] if verdict == "FAIL" else [],
        "evidence_inspected": ["synthetic-approved-evidence.txt"],
        "missing_evidence": ["Additional evidence is required."] if verdict == "NEEDS_EVIDENCE" else [],
        "known_limitations": ["Control-plane dry run only."],
    }


def _astra(route: str, number: int, **payload: Any) -> dict[str, Any]:
    return {
        "decision_id": f"decision-{number:02d}",
        "route": route,
        "reason": f"Scripted deterministic {route} decision {number}.",
        **payload,
    }


def _end(number: int, review_id: str) -> dict[str, Any]:
    return _astra(
        "END",
        number,
        end_reason="Astra approved the independently reviewed target stage.",
        target_stage_completed=True,
        stage_transition={
            "transition_id": f"target-complete-{number:02d}",
            "from_stage": "verified",
            "to_stage": None,
            "based_on_review_id": review_id,
            "reason": "A fresh Reviewer passed and Astra alone approved completion.",
            "approved": True,
        },
    )


def _script(role: str, session: str, output: Mapping[str, Any]) -> BackendResult:
    return BackendResult(
        role=role,  # type: ignore[arg-type]
        session_id=session,
        mode="new",
        output=dict(output),
        usage={"scripted": True, "model_calls": 0},
        command=("scripted-backend", role),
    )


def _scripts(
    scenario: str, role: str, outputs: Sequence[Mapping[str, Any]], *, fresh: bool = False
) -> list[BackendResult]:
    return [
        _script(
            role,
            f"dry-{scenario}-{role}-{index}" if fresh else f"dry-{scenario}-{role}",
            output,
        )
        for index, output in enumerate(outputs, 1)
    ]


def _create_run(run_dir: Path, spec: Mapping[str, Any], run_id: str) -> None:
    AuditLog.create(
        run_dir,
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": utc_now(),
            "project_root": spec["project_root"],
            "spec_path": "spec.json",
            "spec_sha256": sha256_bytes(canonical_json(dict(spec))),
            "checkpoint_path": "checkpoint.sqlite",
            "backend": {
                "type": "scripted",
                "codex_invocations": 0,
                "role_settings": {k: dict(v) for k, v in FIXED_ROLE_SETTINGS.items()},
            },
        },
    )
    write_json_exclusive(run_dir / "spec.json", dict(spec))


def _trace(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    keys = ("sequence", "source", "destination", "reason", "plan_revision_id")
    return [{key: item[key] for key in keys} for item in state.get("transition_log", [])]


def _write_receipt(run_dir: Path, value: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path.name != "final-receipt.json" and path.suffix not in {".wal", ".shm"}:
            rows.append({"path": path.relative_to(run_dir).as_posix(), "sha256": sha256_file(path)})
    value["evidence_index"] = {
        "artifact_count": len(rows),
        "tree_sha256": sha256_bytes(canonical_json(rows)),
        "calls_path": "calls/",
        "reviews_path": "reviews/",
    }
    write_json_exclusive(run_dir / "final-receipt.json", value)
    return value


def _research_plan_pass(
    root: Path, project: Path, artifact: Path,
    *, observer: LangSmithObserver | None = None,
    fixture_usage: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    scenario = "research-plan-pass"
    run_dir = root / scenario
    spec = _spec(project)
    luna_task = _luna_task("research-plan-input", "Which bounded synthetic approach is supported?")
    luna_result = _luna_result(
        luna_task, "DONE", "Synthetic research findings returned only to Astra.", artifact
    )
    revision = {
        "revision_id": "research-informed",
        "current_stage": "verified",
        "project_context": "Astra updated the synthetic plan after evaluating Luna's findings.",
        "target_stage": "verified",
        "stages": [{
            "id": "verified",
            "goal": "Verify the research-informed synthetic orchestration route.",
            "acceptance_criteria": [
                "Luna findings return only to Astra.",
                "Sol evidence receives a fresh independent review.",
                "Only Astra approves target completion.",
            ],
            "requires_review": True,
        }],
        "experiment_plan": ["Use Luna findings to select a scripted fixture check."],
        "datasets": ["Revised synthetic fixture selection; no real data."],
        "evaluation_criteria": ["Require durable Luna, Sol, and Reviewer receipts."],
        "approaches": ["Research first, then bounded scripted implementation."],
        "summary": "Astra autonomously adopted a research-informed synthetic plan.",
        "rationale": "The revision improves evidence coverage without user approval.",
        "purpose_alignment": "It still proves control policy without real experiments.",
        "preserves_ultimate_purpose": True,
    }
    revised_active_plan = {
        key: deepcopy(revision[key])
        for key in (
            "revision_id", "current_stage", "project_context", "target_stage", "stages",
            "experiment_plan", "datasets", "evaluation_criteria", "approaches",
        )
    }
    task = _task(
        "task-research-informed",
        "Produce the synthetic milestone evidence selected by Astra.",
        revision="research-informed",
    )
    result = _sol_result(task, "MILESTONE_COMPLETE", "Synthetic milestone completed.", artifact)
    proposal = "Advance only after a fresh independent Reviewer passes the revised milestone."
    package = _review_package(
        "review-research-pass",
        spec,
        revised_active_plan,
        task,
        result,
        proposal,
        sol_attempts=1,
        task_attempts={task["task_id"]: 1},
    )
    script = {
        "astra": _scripts(scenario, "astra", [
            _astra("LUNA", 1, luna_task=luna_task),
            _astra("SOL", 2, plan_revision=revision, sol_task=task),
            _astra("REVIEW", 3, proposed_next_plan=proposal, review_package=package),
            _end(4, "review-research-pass"),
        ]),
        "luna": _scripts(scenario, "luna", [luna_result], fresh=True),
        "sol": _scripts(scenario, "sol", [result]),
        "reviewer": _scripts(
            scenario,
            "reviewer",
            [_review("review-research-pass", "PASS", "research-informed")],
            fresh=True,
        ),
    }
    if fixture_usage:
        for role, results in script.items():
            if role in fixture_usage:
                script[role] = [
                    replace(result, usage=dict(fixture_usage[role])) for result in results
                ]
    backend = ScriptedBackend(script)
    _create_run(run_dir, spec, "dry-research-plan-pass")
    with OrchestrationRuntime(run_dir, backend, observer=observer) as runtime:
        runtime.invoke(initial_state(spec, "dry-research-plan-pass"))
        state = runtime.status()
    return _write_receipt(run_dir, {
        "schema_version": SCHEMA_VERSION,
        "scenario": "fresh Luna, autonomous plan revision, Sol, fresh Reviewer PASS, Astra END",
        "status": state["status"],
        "target_stage_completed": state["target_stage_completed"],
        "ultimate_purpose_before": spec["ultimate_purpose"],
        "ultimate_purpose_after": state["project_spec"]["ultimate_purpose"],
        "active_plan_revision_id": state["active_plan"]["revision_id"],
        "plan_revision_count": len(state["plan_change_history"]),
        "plan_revision": state["plan_change_history"][0],
        "luna_session_ids": state["luna_session_ids"],
        "review_session_ids": state["review_session_ids"],
        "role_call_order": [call.role for call in backend.calls],
        "role_call_modes": [f"{call.role}:{call.mode}" for call in backend.calls],
        "transition_trace": _trace(state),
        "steps_within_limit": state["step_count"] <= spec["limits"]["max_transitions"],
    })


def _correction_routes(root: Path, project: Path, artifact: Path) -> dict[str, Any]:
    scenario = "correction-routes"
    run_dir = root / scenario
    spec = _spec(project)
    plan = _active_plan(spec)
    fail_issue = _issue_id("review", "review-fail", 4, "reviewer")
    evidence_issue = _issue_id("review", "review-needs-evidence", 8, "reviewer")
    initial = _task("task-initial", "Produce initial synthetic milestone evidence.")
    fix = _task(
        "task-fix",
        "Correct the independent review finding.",
        recovery=_strategy(
            fail_issue, 1, "repair the demonstrated criterion",
            "First corrective strategy for this review finding."
        ),
    )
    evidence = _task(
        "task-evidence",
        "Supply the independently requested evidence.",
        recovery=_strategy(
            evidence_issue, 1,
            "add an independently inspectable evidence pointer",
            "First strategy for the separate missing-evidence issue."
        ),
    )
    tasks = [initial, fix, evidence]
    results = [
        _sol_result(initial, "MILESTONE_COMPLETE", "Initial synthetic evidence.", artifact),
        _sol_result(fix, "MILESTONE_COMPLETE", "Corrective synthetic evidence.", artifact),
        _sol_result(evidence, "MILESTONE_COMPLETE", "Supplemented evidence.", artifact),
    ]
    proposals = [
        "Advance only on PASS; otherwise assign corrective work.",
        "Request another fresh review of the corrected evidence.",
        "Request another fresh review of the supplemented evidence.",
    ]
    # Recovery Sol tasks use the separate recovery budget. They do not change
    # ordinary retry_counts, so both corrective snapshots remain at one.
    packages = [
        _review_package(
            "review-fail", spec, plan, tasks[0], results[0], proposals[0],
            sol_attempts=1, task_attempts={initial["task_id"]: 1},
        ),
        _review_package(
            "review-needs-evidence", spec, plan, tasks[1], results[1], proposals[1],
            sol_attempts=1, review_attempts=1,
            task_attempts={initial["task_id"]: 1}, correction_ready=True,
        ),
        _review_package(
            "review-pass", spec, plan, tasks[2], results[2], proposals[2],
            sol_attempts=1, review_attempts=2,
            task_attempts={initial["task_id"]: 1}, correction_ready=True,
        ),
    ]
    backend = ScriptedBackend({
        "astra": _scripts(scenario, "astra", [
            _astra("SOL", 1, sol_task=tasks[0]),
            _astra("REVIEW", 2, proposed_next_plan=proposals[0], review_package=packages[0]),
            _astra("SOL", 3, sol_task=tasks[1]),
            _astra("REVIEW", 4, proposed_next_plan=proposals[1], review_package=packages[1]),
            _astra("SOL", 5, sol_task=tasks[2]),
            _astra("REVIEW", 6, proposed_next_plan=proposals[2], review_package=packages[2]),
            _end(7, "review-pass"),
        ]),
        "sol": _scripts(scenario, "sol", results),
        "reviewer": _scripts(scenario, "reviewer", [
            _review("review-fail", "FAIL"),
            _review("review-needs-evidence", "NEEDS_EVIDENCE"),
            _review("review-pass", "PASS"),
        ], fresh=True),
    })
    _create_run(run_dir, spec, "dry-correction-routes")
    with OrchestrationRuntime(run_dir, backend) as runtime:
        runtime.invoke(initial_state(spec, "dry-correction-routes"))
        state = runtime.status()
    trace = _trace(state)
    return _write_receipt(run_dir, {
        "schema_version": SCHEMA_VERSION,
        "scenario": "Reviewer FAIL and NEEDS_EVIDENCE corrective routes through Astra",
        "status": state["status"],
        "target_stage_completed": state["target_stage_completed"],
        "review_session_ids": state["review_session_ids"],
        "review_sessions_unique": len(state["review_session_ids"]) == len(set(state["review_session_ids"])) == 3,
        "review_call_modes": [call.mode for call in backend.calls if call.role == "reviewer"],
        "role_call_order": [call.role for call in backend.calls],
        "review_verdicts_returned_to_astra": [
            item["reason"] for item in trace
            if item["source"] == "REVIEW" and item["destination"] == "ASTRA"
        ],
        "ordinary_sol_retry_counts": state["retry_counts"],
        "recovery_attempts": state["recovery_attempts"],
        "transition_trace": trace,
    })


def _human_restart(root: Path, project: Path, artifact: Path) -> dict[str, Any]:
    scenario = "human-restart"
    run_dir = root / scenario
    spec = _spec(project)
    first_task = _task("task-human-only", "Detect the simulated MFA gate.")
    first_result = _sol_result(
        first_task, "NEEDS_USER", "The simulated MFA step is genuinely human-only."
    )
    request = {
        "request_id": "human-mfa-request",
        "category": "AUTH_OR_HUMAN_ACTION_REQUIRED",
        "prompt": "Complete the simulated MFA action outside the durable log.",
        "required_actions": ["Confirm only that the simulated MFA action completed."],
    }
    first_backend = ScriptedBackend({
        "astra": _scripts(scenario, "astra", [
            _astra("SOL", 1, sol_task=first_task),
            _astra("USER", 2, user_request=request),
        ]),
        "sol": _scripts(scenario, "sol", [first_result]),
    })
    _create_run(run_dir, spec, "dry-human-restart")
    with OrchestrationRuntime(run_dir, first_backend) as runtime:
        runtime.invoke(initial_state(spec, "dry-human-restart"))
        paused = runtime.status()

    resumed_task = _task("task-after-human", "Use the simulated post-MFA state.")
    resumed_result = _sol_result(
        resumed_task, "MILESTONE_COMPLETE", "Post-MFA synthetic milestone completed.", artifact
    )
    proposal = "Request a fresh review before Astra completes the target."
    package = _review_package(
        "review-after-human",
        spec,
        _active_plan(spec),
        resumed_task,
        resumed_result,
        proposal,
        sol_attempts=2,
        task_attempts={first_task["task_id"]: 1, resumed_task["task_id"]: 1},
    )
    second_backend = ScriptedBackend({
        "astra": _scripts(scenario, "astra", [
            _astra("SOL", 3, sol_task=resumed_task),
            _astra("REVIEW", 4, proposed_next_plan=proposal, review_package=package),
            _end(5, "review-after-human"),
        ]),
        "sol": _scripts(scenario, "sol", [resumed_result]),
        "reviewer": _scripts(
            scenario, "reviewer", [_review("review-after-human", "PASS")], fresh=True
        ),
    })
    with OrchestrationRuntime(run_dir, second_backend) as runtime:
        runtime.resume({
            "request_id": request["request_id"],
            "status": "PROVIDED",
            "response": "The simulated human-only action completed; no secret was recorded.",
            "evidence_artifact_paths": [],
        })
        finished = runtime.status()
    resumed_sol_calls = [call for call in second_backend.calls if call.role == "sol"]
    return _write_receipt(run_dir, {
        "schema_version": SCHEMA_VERSION,
        "scenario": "genuine human-only USER interrupt and restart-safe resume",
        "user_category": request["category"],
        "paused_status": paused["status"],
        "finished_status": finished["status"],
        "same_run_id": paused["run_id"] == finished["run_id"] == "dry-human-restart",
        "checkpoint_reopened": True,
        "automatic_recovery_attempts_before_pause": len(paused["recovery_attempts"]),
        "target_stage_completed": finished["target_stage_completed"],
        "astra_session_persisted": paused["astra_session_id"] == finished["astra_session_id"] == f"dry-{scenario}-astra",
        "sol_session_persisted": (
            len(resumed_sol_calls) == 1
            and resumed_sol_calls[0].mode == "resume"
            and resumed_sol_calls[0].requested_session_id == paused["sol_session_id"]
            == f"dry-{scenario}-sol"
        ),
        "sol_session_cleared_after_target_completion": finished.get("sol_session_id") is None,
        "resumed_role_modes": [f"{call.role}:{call.mode}" for call in second_backend.calls],
        "review_session_ids": finished["review_session_ids"],
        "transition_trace": _trace(finished),
    })


def _automatic_recovery_exhaustion(
    root: Path, project: Path, artifact: Path, *, observer: LangSmithObserver | None = None
) -> dict[str, Any]:
    scenario = "automatic-recovery-exhaustion"
    run_dir = root / scenario
    spec = _spec(project)
    first_task = _task(
        "task-auto-recovery",
        "Attempt a synthetic operation whose apparent user request is machine-recoverable.",
    )
    first_result = _sol_result(
        first_task,
        "NEEDS_USER",
        "Sol requested input, but Astra classifies it as automatically recoverable.",
    )
    # The controller keys recovery episodes to the producing call, preventing
    # a repeated task/review identifier from inheriting a stale retry history.
    issue = _issue_id("sol", first_task["task_id"], 2, "sol")
    strategies = [
        _strategy(issue, 1, "inspect existing local evidence with Luna", "Initial research strategy."),
        _strategy(issue, 2, "retry with a local fallback parser", "Implementation, not research."),
        _strategy(issue, 3, "cross-check alternate evidence with Luna", "Different source method."),
        _strategy(issue, 4, "reconstruct from the artifact hash", "Hash-based, not parsing."),
        _strategy(issue, 5, "audit local receipts for a recovery clue", "Receipt audit, not evidence."),
    ]
    luna_tasks = [
        _luna_task("recovery-research-1", "Can local evidence resolve the issue?", recovery=strategies[0]),
        _luna_task("recovery-research-3", "Can alternate evidence resolve it?", recovery=strategies[2]),
        _luna_task("recovery-research-5", "Can controller receipts resolve it?", recovery=strategies[4]),
    ]
    sol_tasks = [
        _task("recovery-sol-2", "Try the local fallback parser.", recovery=strategies[1]),
        _task("recovery-sol-4", "Try hash-based reconstruction.", recovery=strategies[3]),
    ]
    luna_results = [
        _luna_result(luna_tasks[0], "INSUFFICIENT_EVIDENCE", "No answer in first source set.", artifact),
        _luna_result(luna_tasks[1], "BLOCKED", "Alternate cross-check was blocked.", artifact),
        _luna_result(luna_tasks[2], "INSUFFICIENT_EVIDENCE", "No clue in receipts.", artifact),
    ]
    sol_results = [
        _sol_result(sol_tasks[0], "BLOCKED", "Fallback parser did not resolve the issue."),
        _sol_result(sol_tasks[1], "FAILED", "Hash reconstruction failed safely."),
    ]
    request = {
        "request_id": "automatic-recovery-exhausted",
        "category": "AUTOMATIC_RECOVERY_EXHAUSTED",
        "prompt": "Five distinct automatic recovery strategies failed; user direction is required.",
        "required_actions": ["Choose whether to stop or supply genuinely new information."],
    }
    backend = ScriptedBackend({
        "astra": _scripts(scenario, "astra", [
            _astra("SOL", 1, sol_task=first_task),
            _astra("LUNA", 2, luna_task=luna_tasks[0]),
            _astra("SOL", 3, sol_task=sol_tasks[0]),
            _astra("LUNA", 4, luna_task=luna_tasks[1]),
            _astra("SOL", 5, sol_task=sol_tasks[1]),
            _astra("LUNA", 6, luna_task=luna_tasks[2]),
            _astra("USER", 7, user_request=request),
        ]),
        "sol": _scripts(scenario, "sol", [first_result, *sol_results]),
        "luna": _scripts(scenario, "luna", luna_results, fresh=True),
    })
    _create_run(run_dir, spec, "dry-automatic-recovery-exhaustion")
    with OrchestrationRuntime(run_dir, backend, observer=observer) as runtime:
        runtime.invoke(initial_state(spec, "dry-automatic-recovery-exhaustion"))
        state = runtime.status()
    attempts = state["recovery_attempts"]
    return _write_receipt(run_dir, {
        "schema_version": SCHEMA_VERSION,
        "scenario": "non-human NEEDS_USER receives five distinct recoveries before USER",
        "status": state["status"],
        "user_category": state["pending_user_request"]["category"],
        "recovery_issue_id": state["recovery_issue_id"],
        "recovery_attempt_count": len(attempts),
        "recovery_attempts": attempts,
        "strategy_ids_unique": len({item["strategy_id"] for item in attempts}) == len(attempts),
        "approaches_unique": len({item["approach"].casefold() for item in attempts}) == len(attempts),
        "all_recoveries_failed": all(item["status"] == "FAILED" for item in attempts),
        "ordinary_sol_retry_counts": state["retry_counts"],
        "luna_session_ids": state["luna_session_ids"],
        "luna_sessions_unique": len(state["luna_session_ids"]) == len(set(state["luna_session_ids"])) == 3,
        "luna_call_modes": [call.mode for call in backend.calls if call.role == "luna"],
        "role_call_order": [call.role for call in backend.calls],
        "transition_trace": _trace(state),
    })


def _bounded_limit(root: Path, project: Path) -> dict[str, Any]:
    scenario = "bounded-limit"
    run_dir = root / scenario
    spec = _spec(project, sol_limit=1)
    first = _task("task-limit-1", "Complete one bounded non-milestone task.")
    rejected = _task("task-limit-2", "This ordinary second attempt must be rejected.")
    result = _sol_result(first, "DONE", "The single allowed Sol attempt completed.")
    backend = ScriptedBackend({
        "astra": _scripts(scenario, "astra", [
            _astra("SOL", 1, sol_task=first),
            _astra("SOL", 2, sol_task=rejected),
        ]),
        "sol": _scripts(scenario, "sol", [result]),
    })
    _create_run(run_dir, spec, "dry-bounded-limit")
    with OrchestrationRuntime(run_dir, backend) as runtime:
        runtime.invoke(initial_state(spec, "dry-bounded-limit"))
        state = runtime.status()
    return _write_receipt(run_dir, {
        "schema_version": SCHEMA_VERSION,
        "scenario": "ordinary Sol retry limit terminates without fabricated USER routing",
        "status": state["status"],
        "route": state["route"],
        "pending_user_request": state.get("pending_user_request"),
        "sol_limit": spec["limits"]["max_sol_attempts_per_stage"],
        "sol_calls": sum(call.role == "sol" for call in backend.calls),
        "no_second_sol_dispatch": sum(call.role == "sol" for call in backend.calls) == 1,
        "last_error": state["last_error"],
        "transition_trace": _trace(state),
    })


def _usage_limit_stop(
    root: Path, project: Path, *, observer: LangSmithObserver | None = None
) -> dict[str, Any]:
    scenario = "usage-limit-stop"
    run_dir = root / scenario
    spec = _spec(project)
    task = _task("task-quota-stop", "Stop when the Codex usage quota is exhausted.")
    backend = ScriptedBackend({
        "astra": _scripts(scenario, "astra", [
            _astra("SOL", 1, sol_task=task),
            _astra("SOL", 2, sol_task=task),
        ]),
        "sol": [BackendUsageLimitExceeded(
            "scripted Codex usage limit",
            run_dir / "injected-backend-receipt",
        )],
    })
    _create_run(run_dir, spec, "dry-usage-limit-stop")
    with OrchestrationRuntime(run_dir, backend, observer=observer) as runtime:
        runtime.invoke(initial_state(spec, "dry-usage-limit-stop"))
        state = runtime.status()
        calls_before_reinvoke = len(backend.calls)
        runtime.invoke()
        reinvoked = runtime.status()
    call_status = read_json(run_dir / "calls" / "0002-sol" / "status.json")
    return _write_receipt(run_dir, {
        "schema_version": SCHEMA_VERSION,
        "scenario": "verified Codex usage exhaustion stops without retry, reset, or USER",
        "status": state["status"],
        "route": state["route"],
        "pending_user_request": state.get("pending_user_request"),
        "last_error": state["last_error"],
        "recovery_attempts": state["recovery_attempts"],
        "retry_counts": state["retry_counts"],
        "model_error_count": state["model_error_count"],
        "role_call_order": [call.role for call in backend.calls],
        "call_status": call_status["status"],
        "terminal_reinvoke_was_noop": (
            len(backend.calls) == calls_before_reinvoke and reinvoked == state
        ),
        "transition_trace": _trace(state),
    })


def run_dry_run(output: Path) -> dict[str, Any]:
    """Run every major policy route with deterministic in-process outputs."""

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    project = output / "synthetic-project"
    project.mkdir()
    evidence = project / "synthetic-approved-evidence.txt"
    evidence.write_text("immutable synthetic orchestration evidence\n", encoding="utf-8")
    before_hash = sha256_file(evidence)

    research = _research_plan_pass(output, project, evidence)
    correction = _correction_routes(output, project, evidence)
    human = _human_restart(output, project, evidence)
    exhaustion = _automatic_recovery_exhaustion(output, project, evidence)
    bounded = _bounded_limit(output, project)
    usage_limit = _usage_limit_stop(output, project)
    after_hash = sha256_file(evidence)

    luna_sessions = [*research["luna_session_ids"], *exhaustion["luna_session_ids"]]
    reviewer_sessions = [
        *research["review_session_ids"],
        *correction["review_session_ids"],
        *human["review_session_ids"],
    ]
    correction_order = [
        "astra", "sol", "astra", "reviewer", "astra", "sol", "astra",
        "reviewer", "astra", "sol", "astra", "reviewer", "astra",
    ]
    research_trace = research["transition_trace"]
    correction_trace = correction["transition_trace"]
    checks = {
        "astra_sol_astra_loop": correction["role_call_order"][:3] == ["astra", "sol", "astra"],
        "fresh_luna_returns_only_to_astra": (
            len(luna_sessions) == len(set(luna_sessions)) == 4
            and all(
                item["destination"] == "ASTRA"
                for receipt in (research, exhaustion)
                for item in receipt["transition_trace"]
                if item["source"] == "LUNA"
            )
            and exhaustion["luna_call_modes"] == ["new", "new", "new"]
        ),
        "autonomous_plan_revision_preserves_ultimate_purpose": (
            research["ultimate_purpose_before"] == research["ultimate_purpose_after"]
            and research["active_plan_revision_id"] == "research-informed"
            and research["plan_revision_count"] == 1
            and research["plan_revision"]["preserves_ultimate_purpose"] is True
            and all(item["destination"] != "USER" for item in research_trace)
        ),
        "fresh_reviewer_sessions_never_reused": (
            len(reviewer_sessions) == len(set(reviewer_sessions)) == 5
            and correction["review_call_modes"] == ["new", "new", "new"]
        ),
        "fail_and_needs_evidence_route_through_astra": (
            correction["role_call_order"] == correction_order
            and correction["review_verdicts_returned_to_astra"] == ["FAIL", "NEEDS_EVIDENCE", "PASS"]
            and all(
                item["destination"] == "ASTRA"
                for item in correction_trace if item["source"] == "REVIEW"
            )
        ),
        "corrective_sol_uses_recovery_not_ordinary_retry_budget": (
            correction["ordinary_sol_retry_counts"]["sol_attempts_by_stage"] == {"verified": 1}
            and correction["ordinary_sol_retry_counts"]["sol_attempts_by_task"] == {"task-initial": 1}
            and len(correction["recovery_attempts"]) == 2
        ),
        "pass_allows_astra_only_advancement": (
            research["target_stage_completed"]
            and research_trace[-2]["source"] == "REVIEW"
            and research_trace[-2]["destination"] == "ASTRA"
            and research_trace[-2]["reason"] == "PASS"
            and research_trace[-1]["source"] == "ASTRA"
            and research_trace[-1]["destination"] == "END"
        ),
        "direct_human_only_interrupt_uses_zero_retries": (
            human["user_category"] == "AUTH_OR_HUMAN_ACTION_REQUIRED"
            and human["paused_status"] == "PAUSED_USER"
            and human["automatic_recovery_attempts_before_pause"] == 0
        ),
        "restart_resumes_same_workflow_and_sessions": (
            human["finished_status"] == "COMPLETED"
            and human["same_run_id"] and human["checkpoint_reopened"]
            and human["astra_session_persisted"] and human["sol_session_persisted"]
        ),
        "automatic_recovery_exhausts_exactly_five_distinct_strategies": (
            exhaustion["status"] == "PAUSED_USER"
            and exhaustion["user_category"] == "AUTOMATIC_RECOVERY_EXHAUSTED"
            and exhaustion["recovery_attempt_count"] == 5
            and exhaustion["strategy_ids_unique"] and exhaustion["approaches_unique"]
            and exhaustion["all_recoveries_failed"]
            and exhaustion["ordinary_sol_retry_counts"]["sol_attempts_by_stage"] == {"verified": 1}
        ),
        "fixed_role_settings": FIXED_ROLE_SETTINGS == {
            "astra": {"title": "Project Manager", "model": "gpt-6-astra", "reasoning_effort": "high"},
            "sol": {"title": "Implementation Worker", "model": "gpt-6-sol", "reasoning_effort": "high"},
            "reviewer": {"title": "Independent Reviewer", "model": "gpt-6-sol", "reasoning_effort": "high"},
            "luna": {"title": "Research and Information-Gathering Specialist", "model": "gpt-6-luna", "reasoning_effort": "xhigh"},
        },
        "limits_enforced_without_user_fabrication": (
            bounded["status"] == "FAILED" and bounded["route"] == "END"
            and bounded["pending_user_request"] is None and bounded["no_second_sol_dispatch"]
        ),
        "usage_limit_exhaustion_stops_without_retry_reset_or_user": (
            usage_limit["status"] == "FAILED"
            and usage_limit["route"] == "END"
            and usage_limit["pending_user_request"] is None
            and usage_limit["last_error"]["code"] == USAGE_LIMIT_STOP_CODE
            and usage_limit["recovery_attempts"] == []
            and usage_limit["retry_counts"] == {
                "sol_attempts_by_stage": {},
                "sol_attempts_by_task": {},
                "review_attempts_by_stage": {},
            }
            and usage_limit["model_error_count"] == 0
            and usage_limit["role_call_order"] == ["astra", "sol"]
            and usage_limit["call_status"] == "AMBIGUOUS"
            and usage_limit["terminal_reinvoke_was_noop"]
        ),
        "target_stage_stops_automatically": (
            research["status"] == "COMPLETED" and research["target_stage_completed"]
            and research_trace[-1]["destination"] == "END" and research["steps_within_limit"]
        ),
        "no_live_codex_model_network_or_experiment_calls": True,
        "approved_synthetic_evidence_unchanged": before_hash == after_hash,
    }
    scenarios = {
        "research_plan_pass": research,
        "correction_routes": correction,
        "user_restart": human,
        "automatic_recovery_exhaustion": exhaustion,
        "bounded_limit": bounded,
        "usage_limit_stop": usage_limit,
    }
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "fixed_role_settings": {key: dict(value) for key, value in FIXED_ROLE_SETTINGS.items()},
        "allowed_user_categories": [
            "AUTH_OR_HUMAN_ACTION_REQUIRED",
            "PAID_RESOURCE_APPROVAL",
            "AUTOMATIC_RECOVERY_EXHAUSTED",
        ],
        "scenarios": scenarios,
        "scripted_backend_only": True,
        "codex_or_model_calls": 0,
        "network_calls": 0,
        "real_training_or_experiments_run": False,
        "synthetic_evidence_before_sha256": before_hash,
        "synthetic_evidence_after_sha256": after_hash,
        "scenario_receipt_sha256": {
            key: sha256_file(output / directory / "final-receipt.json")
            for key, directory in (
                ("research_plan_pass", "research-plan-pass"),
                ("correction_routes", "correction-routes"),
                ("user_restart", "human-restart"),
                ("automatic_recovery_exhaustion", "automatic-recovery-exhaustion"),
                ("bounded_limit", "bounded-limit"),
                ("usage_limit_stop", "usage-limit-stop"),
            )
        },
    }
    write_json_exclusive(output / "final-receipt.json", receipt)
    return receipt
