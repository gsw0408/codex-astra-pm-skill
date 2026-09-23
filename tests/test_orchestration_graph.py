"""End-to-end graph tests using deterministic in-process role sessions only."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import get_type_hints
import unittest

from astra_orchestrator.audit import AuditLog, read_json, replace_json
from astra_orchestrator.cli import _record_outcome, _workflow_exit_code
from astra_orchestrator.codex_cli import (
    BackendResult,
    BackendUsageLimitExceeded,
    ScriptedBackend,
    USAGE_LIMIT_FAILURE_KIND,
)
from astra_orchestrator.dry_run import (
    _active_plan,
    _astra,
    _create_run,
    _end,
    _issue_id,
    _luna_task,
    _review,
    _review_package,
    _sol_result,
    _spec,
    _strategy,
    _task,
    run_dry_run,
)
from astra_orchestrator.graph import (
    OrchestrationRuntime,
    RuntimeState,
    USAGE_LIMIT_STOP_CODE,
    initial_state,
)
from astra_orchestrator.schema import ValidationError, WorkflowState


class OrchestrationGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="orchestration-graph-v2-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.artifact = self.project / "synthetic-evidence.txt"
        self.artifact.write_text("synthetic evidence only\n", encoding="utf-8")
        self.runs = self.root / "runs"
        self.runs.mkdir()

    def runtime(
        self,
        name: str,
        backend: ScriptedBackend,
        spec: dict | None = None,
    ) -> tuple[dict, OrchestrationRuntime]:
        selected = spec or _spec(self.project)
        run_dir = self.runs / name
        _create_run(run_dir, selected, name)
        return selected, OrchestrationRuntime(run_dir, backend)

    def usage_limit_error(self, role: str) -> BackendUsageLimitExceeded:
        return BackendUsageLimitExceeded(
            f"Synthetic {role} usage quota exhausted.",
            self.root / f"{role}-usage-limit-receipts",
            codex_error_info="UsageLimitExceeded",
        )

    def assert_usage_limit_stop(
        self,
        state: dict,
        *,
        role: str,
        expected_retry_counts: dict | None = None,
    ) -> None:
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["route"], "END")
        self.assertEqual(state["current_stage"], "verified")
        self.assertEqual(state["completed_stages"], [])
        self.assertFalse(state["target_stage_completed"])
        self.assertFalse(state["paused"])
        self.assertIsNone(state.get("pending_user_request"))
        self.assertFalse(state["must_pause_user"])
        self.assertEqual(state["recovery_attempts"], [])
        self.assertIsNone(state["recovery_issue_id"])
        self.assertEqual(state["model_error_count"], 0)
        self.assertEqual(state["last_error"]["code"], USAGE_LIMIT_STOP_CODE)
        self.assertEqual(state["last_error"]["role"], role)
        if expected_retry_counts is not None:
            self.assertEqual(state["retry_counts"], expected_retry_counts)

    def test_runtime_uses_authoritative_workflow_state_contract(self) -> None:
        self.assertIs(RuntimeState, WorkflowState)
        hints = get_type_hints(WorkflowState)
        for field in (
            "active_plan",
            "plan_change_history",
            "luna_session_ids",
            "recovery_attempts",
            "pending_user_request",
            "target_stage_completed",
        ):
            self.assertIn(field, hints)

    def test_astra_usage_limit_stops_without_retry_recovery_or_user(self) -> None:
        backend = ScriptedBackend({
            "astra": [self.usage_limit_error("astra")],
        })
        spec, runtime = self.runtime("usage-limit-astra", backend)
        expected_retries = initial_state(spec, "usage-limit-astra")["retry_counts"]
        with runtime:
            runtime.invoke(initial_state(spec, "usage-limit-astra"))
            state = runtime.status()

        self.assert_usage_limit_stop(
            state,
            role="astra",
            expected_retry_counts=expected_retries,
        )
        self.assertEqual([call.role for call in backend.calls], ["astra"])

    def test_persisted_usage_limit_process_receipt_stops_without_reexecution(self) -> None:
        backend = ScriptedBackend({
            "astra": [_astra("SOL", 1, sol_task=_task("unused", "Must not run."))],
        })
        spec, runtime = self.runtime("usage-limit-crash-window", backend)
        directory = runtime.audit.prepare_call(
            "0001-astra",
            "astra",
            {
                "schema_version": 2,
                "role": "astra",
                "call_id": "0001-astra",
                "prompt": "persisted crash-window fixture",
                "requested_session_id": None,
                "state_step": 0,
            },
            {"type": "object"},
        )
        runtime.audit.mark_call_running("0001-astra")
        replace_json(directory / "process.json", {
            "schema_version": 2,
            "role": "astra",
            "mode": "new",
            "session_id": "persisted-astra-usage-session",
            "backend_failure_kind": USAGE_LIMIT_FAILURE_KIND,
            "codex_error_info": "usage_limit_exceeded",
            "returncode": 1,
        })

        with runtime:
            runtime.invoke(initial_state(spec, "usage-limit-crash-window"))
            state = runtime.status()

        self.assert_usage_limit_stop(state, role="astra")
        self.assertEqual(backend.calls, [])
        self.assertEqual(
            state["astra_session_id"], "persisted-astra-usage-session",
        )
        self.assertEqual(read_json(directory / "status.json")["status"], "FAILED")
        self.assertEqual(
            read_json(directory / "error.json")["failure_kind"],
            USAGE_LIMIT_FAILURE_KIND,
        )

    def test_sol_usage_limit_is_terminal_and_never_reexecuted(self) -> None:
        task = _task("task-usage-stop", "Reach the synthetic usage hard stop.")
        backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 1, sol_task=task),
                _astra("SOL", 2, sol_task=task),
            ],
            "sol": [self.usage_limit_error("sol")],
        })
        spec, runtime = self.runtime("usage-limit-sol", backend)
        expected_retries = initial_state(spec, "usage-limit-sol")["retry_counts"]
        with runtime:
            runtime.invoke(initial_state(spec, "usage-limit-sol"))
            state = runtime.status()
            call_count = len(backend.calls)
            self.assertEqual(runtime.invoke(state), state)
            self.assertEqual(runtime.continue_after_restart(), state)
            self.assertEqual(len(backend.calls), call_count)

        self.assert_usage_limit_stop(
            state,
            role="sol",
            expected_retry_counts=expected_retries,
        )
        self.assertEqual([call.role for call in backend.calls], ["astra", "sol"])
        call_dir = self.runs / "usage-limit-sol" / "calls" / "0002-sol"
        self.assertEqual(read_json(call_dir / "status.json")["status"], "AMBIGUOUS")
        call_error = read_json(call_dir / "error.json")
        self.assertEqual(call_error["failure_kind"], USAGE_LIMIT_FAILURE_KIND)
        self.assertEqual(call_error["terminal_stop_code"], USAGE_LIMIT_STOP_CODE)

        reopened_backend = ScriptedBackend({
            "astra": [_astra("SOL", 3, sol_task=task)],
            "sol": [_sol_result(task, "DONE", "Must never execute.")],
        })
        with OrchestrationRuntime(
            self.runs / "usage-limit-sol", reopened_backend
        ) as reopened:
            reopened_state = reopened.continue_after_restart()
        self.assertEqual(reopened_state, state)
        self.assertEqual(reopened_backend.calls, [])

        run_dir = self.runs / "usage-limit-sol"
        _record_outcome(run_dir, state)
        final_receipt = read_json(run_dir / "final-receipt.json")
        resume_info = read_json(run_dir / "resume-info.json")
        self.assertEqual(final_receipt["last_error"]["code"], USAGE_LIMIT_STOP_CODE)
        self.assertEqual(final_receipt["last_error"]["role"], "sol")
        self.assertTrue(final_receipt["usage_limit_exhausted"])
        self.assertFalse(resume_info["resumable"])
        self.assertEqual(resume_info["last_error"]["code"], USAGE_LIMIT_STOP_CODE)
        self.assertTrue(resume_info["usage_limit_exhausted"])
        self.assertEqual(_workflow_exit_code(state), 1)

    def test_luna_usage_limit_stops_before_astra_can_continue(self) -> None:
        task = _luna_task(
            "research-usage-stop",
            "Inspect only the synthetic usage-limit test fixture.",
        )
        backend = ScriptedBackend({
            "astra": [
                _astra("LUNA", 1, luna_task=task),
                _astra("LUNA", 2, luna_task=task),
            ],
            "luna": [self.usage_limit_error("luna")],
        })
        spec, runtime = self.runtime("usage-limit-luna", backend)
        expected_retries = initial_state(spec, "usage-limit-luna")["retry_counts"]
        with runtime:
            runtime.invoke(initial_state(spec, "usage-limit-luna"))
            state = runtime.status()

        self.assert_usage_limit_stop(
            state,
            role="luna",
            expected_retry_counts=expected_retries,
        )
        self.assertEqual([call.role for call in backend.calls], ["astra", "luna"])
        self.assertEqual(state["luna_session_ids"], [])

    def test_reviewer_usage_limit_stops_without_review_retry(self) -> None:
        spec = _spec(self.project)
        plan = _active_plan(spec)
        task = _task("task-review-usage-stop", "Produce synthetic review evidence.")
        result = _sol_result(
            task,
            "MILESTONE_COMPLETE",
            "Synthetic milestone is ready for review.",
            self.artifact,
        )
        proposal = "Await a fresh independent review before any advancement."
        package = _review_package(
            "review-usage-stop",
            spec,
            plan,
            task,
            result,
            proposal,
            sol_attempts=1,
            task_attempts={task["task_id"]: 1},
        )
        backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 1, sol_task=task),
                _astra(
                    "REVIEW",
                    2,
                    proposed_next_plan=proposal,
                    review_package=package,
                ),
                _end(3, "review-usage-stop"),
            ],
            "sol": [result],
            "reviewer": [self.usage_limit_error("reviewer")],
        })
        _, runtime = self.runtime("usage-limit-reviewer", backend, spec)
        with runtime:
            runtime.invoke(initial_state(spec, "usage-limit-reviewer"))
            state = runtime.status()

        self.assert_usage_limit_stop(state, role="reviewer")
        self.assertEqual(
            [call.role for call in backend.calls],
            ["astra", "sol", "astra", "reviewer"],
        )
        self.assertEqual(state["retry_counts"]["sol_attempts_by_stage"], {"verified": 1})
        self.assertEqual(state["retry_counts"]["sol_attempts_by_task"], {task["task_id"]: 1})
        self.assertEqual(state["retry_counts"]["review_attempts_by_stage"], {})
        self.assertEqual(state["review_session_ids"], [])

    def test_astra_sol_reviewer_astra_pass_and_target_stop(self) -> None:
        spec = _spec(self.project)
        plan = _active_plan(spec)
        task = _task("task-pass", "Produce a reviewed synthetic milestone.")
        result = _sol_result(
            task, "MILESTONE_COMPLETE", "The synthetic milestone is ready.", self.artifact
        )
        proposal = "End only after a fresh independent PASS."
        package = _review_package(
            "review-pass",
            spec,
            plan,
            task,
            result,
            proposal,
            sol_attempts=1,
            task_attempts={task["task_id"]: 1},
        )
        backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 1, sol_task=task),
                _astra("REVIEW", 2, proposed_next_plan=proposal, review_package=package),
                _end(3, "review-pass"),
            ],
            "sol": [result],
            "reviewer": [_review("review-pass", "PASS")],
        })
        _, runtime = self.runtime("pass", backend, spec)
        with runtime:
            runtime.invoke(initial_state(spec, "pass"))
            state = runtime.status()

        self.assertEqual(state["status"], "COMPLETED")
        self.assertTrue(state["target_stage_completed"])
        self.assertEqual([call.role for call in backend.calls], [
            "astra", "sol", "astra", "reviewer", "astra",
        ])
        reviewer_call = next(call for call in backend.calls if call.role == "reviewer")
        self.assertEqual(reviewer_call.mode, "new")
        self.assertEqual(len(state["review_session_ids"]), 1)
        self.assertEqual(
            [item["sequence"] for item in state["transition_log"]],
            list(range(1, len(state["transition_log"]) + 1)),
        )
        self.assertEqual(state["transition_log"][-2]["reason"], "PASS")
        self.assertEqual(state["transition_log"][-1]["destination"], "END")

    def test_direct_paid_interrupt_uses_zero_recovery_attempts(self) -> None:
        request = {
            "request_id": "paid-resource-request",
            "category": "PAID_RESOURCE_APPROVAL",
            "prompt": "Approve the new paid synthetic resource.",
            "required_actions": ["Approve or decline the new cost."],
        }
        backend = ScriptedBackend({
            "astra": [_astra("USER", 1, user_request=request)],
        })
        spec, runtime = self.runtime("paid-user", backend)
        with runtime:
            runtime.invoke(initial_state(spec, "paid-user"))
            state = runtime.status()

        self.assertEqual(state["status"], "PAUSED_USER")
        self.assertEqual(state["pending_user_request"]["category"], "PAID_RESOURCE_APPROVAL")
        self.assertEqual(state["recovery_attempts"], [])
        self.assertEqual([call.role for call in backend.calls], ["astra"])

    def test_nonhuman_needs_user_cannot_escalate_before_five_failures(self) -> None:
        task = _task("task-premature", "Expose a machine-recoverable blocker.")
        result = _sol_result(task, "NEEDS_USER", "The request is not human-only.")
        request = {
            "request_id": "premature-exhaustion",
            "category": "AUTOMATIC_RECOVERY_EXHAUSTED",
            "prompt": "This escalation is premature.",
            "required_actions": ["Should never be requested."],
            "exhaustion_context": {
                "problem": "The synthetic task is blocked.",
                "why_blocked": "Automatic work appears exhausted.",
                "attempts": [
                    {
                        "attempt": number,
                        "strategy_id": f"premature-strategy-{number}",
                        "route": "LUNA",
                        "approach": f"Attempt {number}.",
                        "outcome": "Did not resolve the issue.",
                    }
                    for number in range(1, 6)
                ],
                "current_state": {
                    "stage_id": "verified", "target_stage": "verified",
                    "plan_revision_id": "initial", "summary": "No recovery was actually attempted.",
                },
                "risks_and_impact": "A premature pause would waste user time.",
                "resume_after_user": "Astra would evaluate the response.",
            },
        }
        backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 1, sol_task=task),
                _astra("USER", 2, user_request=request),
            ],
            "sol": [result],
        })
        spec, runtime = self.runtime("premature-exhaustion", backend)
        with runtime:
            runtime.invoke(initial_state(spec, "premature-exhaustion"))
            state = runtime.status()

        self.assertEqual(state["status"], "FAILED")
        self.assertIsNone(state.get("pending_user_request"))
        self.assertIn("exactly five", state["last_error"]["message"])
        self.assertEqual(state["recovery_attempts"], [])

    def test_exhaustion_request_must_match_persisted_attempts_and_stage(self) -> None:
        spec, runtime = self.runtime("exhaustion-summary-binding", ScriptedBackend({}))
        state = initial_state(spec, "exhaustion-summary-binding")
        state["recovery_issue_id"] = "issue-1"
        state["recovery_attempts"] = [
            {
                "issue_id": "issue-1", "attempt": number,
                "strategy_id": f"strategy-{number}", "route": "LUNA",
                "approach": f"Distinct attempt {number}.",
                "outcome": f"Attempt {number} failed.", "status": "FAILED",
            }
            for number in range(1, 6)
        ]
        request = {
            "category": "AUTOMATIC_RECOVERY_EXHAUSTED",
            "exhaustion_context": {
                "attempts": [
                    {key: attempt[key] for key in (
                        "attempt", "strategy_id", "route", "approach", "outcome"
                    )}
                    for attempt in state["recovery_attempts"]
                ],
                "current_state": {
                    "stage_id": "verified", "target_stage": "verified",
                    "plan_revision_id": "initial",
                },
            },
        }
        with runtime:
            runtime._validate_user_route(state, request)
            wrong_attempt = {
                **request,
                "exhaustion_context": {
                    **request["exhaustion_context"],
                    "attempts": [
                        *request["exhaustion_context"]["attempts"][:4],
                        {**request["exhaustion_context"]["attempts"][4], "outcome": "Invented success."},
                    ],
                },
            }
            with self.assertRaisesRegex(ValidationError, "persisted attempts"):
                runtime._validate_user_route(state, wrong_attempt)
            wrong_stage = {
                **request,
                "exhaustion_context": {
                    **request["exhaustion_context"],
                    "current_state": {
                        **request["exhaustion_context"]["current_state"],
                        "stage_id": "other-stage",
                    },
                },
            }
            with self.assertRaisesRegex(ValidationError, "current stage and plan"):
                runtime._validate_user_route(state, wrong_stage)

    def test_successful_automatic_recovery_closes_issue_before_five(self) -> None:
        spec = _spec(self.project)
        plan = _active_plan(spec)
        initial = _task("task-recoverable", "Report a machine-recoverable request.")
        initial_result = _sol_result(
            initial, "NEEDS_USER", "A local automatic fallback can resolve this."
        )
        issue = _issue_id("sol", initial["task_id"], 2, "sol")
        recovery = _task(
            "task-recovery",
            "Apply a distinct local fallback.",
            recovery=_strategy(issue, 1, "use the local fallback", "First strategy."),
        )
        recovery_result = _sol_result(recovery, "DONE", "The local fallback succeeded.")
        final_task = _task("task-after-recovery", "Produce milestone evidence after recovery.")
        final_result = _sol_result(
            final_task, "MILESTONE_COMPLETE", "Recovered milestone is ready.", self.artifact
        )
        proposal = "Review the recovered milestone."
        package = _review_package(
            "review-after-recovery",
            spec,
            plan,
            final_task,
            final_result,
            proposal,
            sol_attempts=2,
            task_attempts={initial["task_id"]: 1, final_task["task_id"]: 1},
        )
        backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 1, sol_task=initial),
                _astra("SOL", 2, sol_task=recovery),
                _astra("SOL", 3, sol_task=final_task),
                _astra("REVIEW", 4, proposed_next_plan=proposal, review_package=package),
                _end(5, "review-after-recovery"),
            ],
            "sol": [initial_result, recovery_result, final_result],
            "reviewer": [_review("review-after-recovery", "PASS")],
        })
        _, runtime = self.runtime("successful-recovery", backend, spec)
        with runtime:
            runtime.invoke(initial_state(spec, "successful-recovery"))
            state = runtime.status()

        self.assertEqual(state["status"], "COMPLETED")
        self.assertIsNone(state["recovery_issue_id"])
        self.assertEqual(len(state["recovery_attempts"]), 1)
        self.assertEqual(state["recovery_attempts"][0]["status"], "SUCCEEDED")
        self.assertEqual(state["retry_counts"]["sol_attempts_by_stage"], {"verified": 2})

    def test_stale_pass_cannot_advance_after_autonomous_plan_revision(self) -> None:
        spec = _spec(self.project)
        initial_plan = _active_plan(spec)
        first = _task("task-reviewed", "Produce evidence for the initial plan.")
        first_result = _sol_result(
            first, "MILESTONE_COMPLETE", "Initial evidence is ready.", self.artifact
        )
        proposal = "Review the initial plan evidence."
        package = _review_package(
            "review-old-plan",
            spec,
            initial_plan,
            first,
            first_result,
            proposal,
            sol_attempts=1,
            task_attempts={first["task_id"]: 1},
        )
        revision = {
            **initial_plan,
            "revision_id": "revised-plan",
            "datasets": ["A revised synthetic dataset."],
            "evaluation_criteria": ["A revised evidence criterion."],
            "approaches": ["A revised implementation approach."],
            "summary": "Change the mutable plan after the old PASS.",
            "rationale": "A genuine project reason requires new evidence.",
            "purpose_alignment": "The immutable synthetic proof purpose is preserved.",
            "preserves_ultimate_purpose": True,
        }
        revised_task = _task(
            "task-revised", "Produce evidence under the revised plan.", revision="revised-plan"
        )
        revised_result = _sol_result(
            revised_task,
            "MILESTONE_COMPLETE",
            "Revised but not independently reviewed evidence is ready.",
            self.artifact,
        )
        backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 1, sol_task=first),
                _astra("REVIEW", 2, proposed_next_plan=proposal, review_package=package),
                _astra("SOL", 3, plan_revision=revision, sol_task=revised_task),
                _end(4, "review-old-plan"),
            ],
            "sol": [first_result, revised_result],
            "reviewer": [_review("review-old-plan", "PASS")],
        })
        _, runtime = self.runtime("stale-pass", backend, spec)
        with runtime:
            runtime.invoke(initial_state(spec, "stale-pass"))
            state = runtime.status()

        self.assertEqual(state["status"], "FAILED")
        self.assertFalse(state["target_stage_completed"])
        self.assertEqual(state["project_spec"]["ultimate_purpose"], spec["ultimate_purpose"])
        self.assertEqual(state["active_plan"]["revision_id"], "revised-plan")
        self.assertEqual(state["active_plan"]["datasets"], ["A revised synthetic dataset."])
        self.assertIn("Reviewer PASS", state["last_error"]["message"])

    def test_failed_new_sol_session_is_persisted_for_corrective_resume(self) -> None:
        task = _task("task-malformed", "Return malformed output after creating a session.")
        issue = _issue_id("sol", task["task_id"], 2, "sol")
        recovery = _task(
            "task-correct-malformed",
            "Correct the malformed result in the same worker session.",
            recovery=_strategy(issue, 1, "repair structured output", "First repair strategy."),
        )
        malformed = BackendResult(
            role="sol",
            session_id="sol-created-before-validation-failure",
            mode="new",
            output={"not": "a SolResult"},
        )
        backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 1, sol_task=task),
                _astra("SOL", 2, sol_task=recovery),
            ],
            "sol": [malformed, _sol_result(recovery, "DONE", "Corrected output succeeded.")],
        })
        spec, runtime = self.runtime("failed-sol-session", backend)
        with runtime:
            runtime.invoke(initial_state(spec, "failed-sol-session"))
            state = runtime.status()

        sol_calls = [call for call in backend.calls if call.role == "sol"]
        self.assertEqual(sol_calls[1].mode, "resume")
        self.assertEqual(
            sol_calls[1].requested_session_id,
            "sol-created-before-validation-failure",
        )
        self.assertEqual(state["sol_session_id"], "sol-created-before-validation-failure")
        self.assertEqual(state["recovery_attempts"][0]["status"], "SUCCEEDED")
        self.assertEqual(state["status"], "FAILED")  # No later Astra script; bounded failure.

    def test_manifest_v1_and_run_dir_inside_worker_workspace_are_rejected(self) -> None:
        legacy_dir = self.runs / "legacy-v1"
        AuditLog.create(legacy_dir, {
            "schema_version": 1,
            "run_id": "legacy-v1",
            "project_root": str(self.project),
        })
        with self.assertRaisesRegex(ValueError, "expected 2"):
            OrchestrationRuntime(legacy_dir, ScriptedBackend({}))

        unsafe = self.project / "controller-evidence"
        spec = _spec(self.project)
        _create_run(unsafe, spec, "unsafe-run-dir")
        with self.assertRaisesRegex(ValueError, "outside project_root"):
            OrchestrationRuntime(unsafe, ScriptedBackend({}))

    def test_transition_limit_fails_without_fabricating_user_request(self) -> None:
        spec = _spec(self.project)
        spec["limits"]["max_transitions"] = 1
        task = _task("task-never-dispatched", "This task exceeds the hard budget.")
        backend = ScriptedBackend({
            "astra": [_astra("SOL", 1, sol_task=task)],
            "sol": [_sol_result(task, "DONE", "Must not be consumed.")],
        })
        _, runtime = self.runtime("transition-limit", backend, spec)
        with runtime:
            runtime.invoke(initial_state(spec, "transition-limit"))
            state = runtime.status()

        self.assertEqual(state["status"], "FAILED")
        self.assertIsNone(state.get("pending_user_request"))
        self.assertEqual([call.role for call in backend.calls], ["astra"])
        self.assertIn("transition budget", state["last_error"]["message"])

    def test_reviewer_and_model_error_bounds_fail_without_user_fabrication(self) -> None:
        spec = _spec(self.project)
        spec["limits"]["max_review_attempts_per_stage"] = 1
        plan = _active_plan(spec)
        initial = _task("task-review-limit", "Produce initial review evidence.")
        initial_result = _sol_result(
            initial, "MILESTONE_COMPLETE", "Initial evidence is ready.", self.artifact
        )
        first_proposal = "Review the initial evidence."
        first_package = _review_package(
            "review-limit-first",
            spec,
            plan,
            initial,
            initial_result,
            first_proposal,
            sol_attempts=1,
            task_attempts={initial["task_id"]: 1},
        )
        issue = _issue_id("review", "review-limit-first", 4, "reviewer")
        correction = _task(
            "task-review-limit-correction",
            "Correct the failed review.",
            recovery=_strategy(issue, 1, "correct the failed criterion", "First correction."),
        )
        correction_result = _sol_result(
            correction, "MILESTONE_COMPLETE", "Corrected evidence is ready.", self.artifact
        )
        second_proposal = "Request a second fresh review."
        second_package = _review_package(
            "review-limit-second",
            spec,
            plan,
            correction,
            correction_result,
            second_proposal,
            sol_attempts=1,
            review_attempts=1,
            task_attempts={initial["task_id"]: 1},
            correction_ready=True,
        )
        backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 1, sol_task=initial),
                _astra(
                    "REVIEW", 2,
                    proposed_next_plan=first_proposal,
                    review_package=first_package,
                ),
                _astra("SOL", 3, sol_task=correction),
                _astra(
                    "REVIEW", 4,
                    proposed_next_plan=second_proposal,
                    review_package=second_package,
                ),
            ],
            "sol": [initial_result, correction_result],
            "reviewer": [_review("review-limit-first", "FAIL")],
        })
        _, runtime = self.runtime("review-limit", backend, spec)
        with runtime:
            runtime.invoke(initial_state(spec, "review-limit"))
            state = runtime.status()
        self.assertEqual(state["status"], "FAILED")
        self.assertIsNone(state.get("pending_user_request"))
        self.assertEqual(sum(call.role == "reviewer" for call in backend.calls), 1)
        self.assertIn("maximum Reviewer attempts", state["last_error"]["message"])

        error_spec = _spec(self.project)
        error_spec["limits"]["max_model_errors"] = 1
        unused_backend = ScriptedBackend({
            "astra": [_astra("SOL", 1, sol_task=_task("unused", "Must not run."))],
        })
        error_state = initial_state(error_spec, "model-error-limit")
        error_state["model_error_count"] = 1
        _, error_runtime = self.runtime("model-error-limit", unused_backend, error_spec)
        with error_runtime:
            error_runtime.invoke(error_state)
            bounded = error_runtime.status()
        self.assertEqual(bounded["status"], "FAILED")
        self.assertEqual(unused_backend.calls, [])
        self.assertIsNone(bounded.get("pending_user_request"))
        self.assertIn("model/structured-output error limit", bounded["last_error"]["message"])

    def test_dry_run_receipt_proves_all_major_routes_without_real_work(self) -> None:
        output = self.root / "complete-dry-run"
        receipt = run_dry_run(output)
        persisted = read_json(output / "final-receipt.json")

        self.assertEqual(receipt["status"], "PASS")
        self.assertTrue(all(receipt["checks"].values()))
        self.assertEqual(receipt["codex_or_model_calls"], 0)
        self.assertEqual(receipt["network_calls"], 0)
        self.assertFalse(receipt["real_training_or_experiments_run"])
        self.assertEqual(
            receipt["synthetic_evidence_before_sha256"],
            receipt["synthetic_evidence_after_sha256"],
        )
        self.assertEqual(persisted, receipt)


if __name__ == "__main__":
    unittest.main()
