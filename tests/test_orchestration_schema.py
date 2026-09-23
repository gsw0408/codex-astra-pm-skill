import json
import unittest
from pathlib import Path

from astra_orchestrator.schema import (
    ASTRA_ROUTES,
    LUNA_STATUSES,
    REVIEWER_VERDICTS,
    SOL_STATUSES,
    ValidationError,
    schema_path,
    validate_astra_decision,
    validate_active_plan,
    validate_luna_result,
    validate_luna_task,
    validate_plan_revision,
    validate_project_spec,
    validate_recovery_attempt,
    validate_recovery_strategy,
    validate_review_package,
    validate_reviewer_result,
    validate_sol_result,
    validate_user_response,
)


def sol_task():
    return {
        "task_id": "task-1",
        "stage_id": "stage-1",
        "plan_revision_id": "plan-1",
        "objective": "Make the bounded change.",
        "acceptance_criteria": ["Targeted check passes."],
        "constraints": ["Do not run experiments."],
        "targeted_checks": ["python -m unittest tests.test_example"],
        "forbidden_areas": ["approved-evidence/"],
    }


def sol_result(status="MILESTONE_COMPLETE"):
    result = {
        "task_id": "task-1",
        "stage_id": "stage-1",
        "plan_revision_id": "plan-1",
        "status": status,
        "summary": "Bounded work finished.",
        "evidence": ["Targeted check passed."],
        "evidence_artifact_paths": ["artifacts/check.txt"] if status == "MILESTONE_COMPLETE" else [],
        "known_limitations": [],
        "blockers": [],
        "user_actions_requested": [],
    }
    if status == "BLOCKED":
        result["blockers"] = ["Local dependency is unavailable."]
    elif status == "NEEDS_USER":
        result["user_actions_requested"] = ["Upload the fixture."]
    elif status == "FAILED":
        result["error"] = "The bounded command failed."
    return result


def review_package():
    return {
        "review_id": "review-1",
        "stage_id": "stage-1",
        "plan_revision_id": "plan-1",
        "ultimate_purpose": "Prove deterministic orchestration routes.",
        "project_root": "/workspace/demo",
        "project_context": "A dry-run orchestration project.",
        "milestone_goal": "Prove milestone review routing.",
        "acceptance_criteria": ["Evidence is inspectable."],
        "sol_instructions": sol_task(),
        "actual_results": sol_result(),
        "evidence_artifact_paths": [
            {"path": "artifacts/check.txt", "sha256": "a" * 64}
        ],
        "known_limitations": ["No real experiment was run."],
        "current_state": {
            "active_plan": {
                "revision_id": "plan-1",
                "current_stage": "stage-1",
                "project_context": "A dry-run orchestration project.",
                "target_stage": "stage-2",
                "stages": [
                    {
                        "id": "stage-1",
                        "goal": "Prove milestone review routing.",
                        "acceptance_criteria": ["Evidence is inspectable."],
                        "requires_review": True,
                    },
                    {
                        "id": "stage-2",
                        "goal": "Finish the synthetic workflow.",
                        "acceptance_criteria": ["A final receipt exists."],
                        "requires_review": True,
                    },
                ],
                "experiment_plan": [],
                "datasets": [],
                "evaluation_criteria": [],
                "approaches": [],
            },
            "current_stage": "stage-1",
            "completed_stages": [],
            "target_stage": "stage-2",
            "target_stage_completed": False,
            "execution_limits": {
                "max_sol_attempts_per_stage": 3,
                "max_review_attempts_per_stage": 3,
                "max_transitions": 50,
                "max_model_errors": 3,
            },
            "retry_counts": {
                "sol_attempts_by_stage": {"stage-1": 1},
                "sol_attempts_by_task": {"task-1": 1},
                "review_attempts_by_stage": {},
            },
            "correction_required": False,
            "correction_ready": False,
            "last_sol_task_id": "task-1",
            "last_sol_status": "MILESTONE_COMPLETE",
        },
        "proposed_next_plan": "Advance only if the reviewer passes the evidence.",
    }


def recovery_strategy(attempt=1):
    return {
        "issue_id": "issue-1",
        "strategy_id": f"strategy-{attempt}",
        "attempt": attempt,
        "approach": "Use a distinct local evidence source.",
        "rationale": "The prior source did not resolve the blocker.",
        "difference_from_prior": "This strategy uses a different evidence source.",
    }


def luna_task():
    return {
        "research_id": "research-1",
        "stage_id": "stage-1",
        "plan_revision_id": "plan-1",
        "question": "Which primary source documents the required behavior?",
        "scope": ["Primary documentation only."],
        "sources_to_consult": ["Official project documentation."],
        "deliverables": ["Cited factual findings."],
        "constraints": ["Do not modify project files."],
    }


def luna_result(status="DONE"):
    result = {
        "research_id": "research-1",
        "stage_id": "stage-1",
        "plan_revision_id": "plan-1",
        "status": status,
        "summary": "Bounded research completed.",
        "findings": ["The primary source supports the behavior."] if status == "DONE" else [],
        "sources": [{"path": "https://example.test/primary"}] if status == "DONE" else [],
        "evidence_artifact_paths": [],
        "known_limitations": [],
        "blockers": [],
    }
    if status == "INSUFFICIENT_EVIDENCE":
        result["known_limitations"] = ["No primary source answered the question."]
    elif status == "BLOCKED":
        result["blockers"] = ["The source is unavailable."]
    elif status == "FAILED":
        result["error"] = "Research execution failed."
    return result


def plan_revision():
    return {
        "revision_id": "plan-2",
        "current_stage": "stage-1",
        "project_context": "A revised local dry run.",
        "target_stage": "stage-2",
        "stages": [
            {
                "id": "stage-1",
                "goal": "Implement with the revised approach.",
                "acceptance_criteria": ["Revised evidence exists."],
                "requires_review": True,
            },
            {
                "id": "stage-2",
                "goal": "Finish.",
                "acceptance_criteria": ["Target receipt exists."],
                "requires_review": True,
            },
        ],
        "experiment_plan": ["Run a safe revised experiment."],
        "datasets": ["dataset-v2"],
        "evaluation_criteria": ["Use the revised metric."],
        "approaches": ["Alternative implementation approach."],
        "summary": "Revise the mutable project plan.",
        "rationale": "The earlier approach was blocked.",
        "purpose_alignment": "The revision continues to serve the immutable purpose.",
        "preserves_ultimate_purpose": True,
    }


class ProjectSpecValidationTests(unittest.TestCase):
    def valid_spec(self):
        return {
            "project_id": "demo",
            "project_root": "/workspace/demo",
            "project_context": "A local dry run.",
            "ultimate_purpose": "Exercise deterministic routes.",
            "target_stage": "stage-2",
            "stages": [
                {
                    "id": "stage-1",
                    "goal": "Implement.",
                    "acceptance_criteria": ["Implementation evidence exists."],
                    "requires_review": True,
                },
                {
                    "id": "stage-2",
                    "goal": "Finish.",
                    "acceptance_criteria": ["Target receipt exists."],
                    "requires_review": True,
                },
            ],
            "limits": {
                "max_sol_attempts_per_stage": 3,
                "max_review_attempts_per_stage": 3,
                "max_transitions": 50,
                "max_model_errors": 3,
            },
        }

    def test_project_spec_encodes_review_policy_and_positive_limits(self):
        normalized = validate_project_spec(self.valid_spec())
        self.assertTrue(normalized["stages"][0]["requires_review"])
        self.assertEqual(normalized["target_stage"], "stage-2")
        self.assertEqual(normalized["ultimate_purpose"], "Exercise deterministic routes.")
        self.assertEqual(normalized["experiment_plan"], [])
        self.assertEqual(normalized["datasets"], [])

        invalid = self.valid_spec()
        invalid["limits"]["max_transitions"] = 0
        with self.assertRaisesRegex(ValidationError, "positive integer"):
            validate_project_spec(invalid)

    def test_project_spec_rejects_duplicate_or_unknown_target_stage(self):
        duplicate = self.valid_spec()
        duplicate["stages"][1]["id"] = "stage-1"
        with self.assertRaisesRegex(ValidationError, "duplicate id"):
            validate_project_spec(duplicate)

        missing_target = self.valid_spec()
        missing_target["target_stage"] = "not-present"
        with self.assertRaisesRegex(ValidationError, "must name a stage"):
            validate_project_spec(missing_target)

    def test_project_spec_requires_immutable_ultimate_purpose(self):
        legacy = self.valid_spec()
        legacy["original_goal"] = legacy.pop("ultimate_purpose")
        with self.assertRaisesRegex(ValidationError, "ultimate_purpose"):
            validate_project_spec(legacy)

    def test_active_plan_and_full_plan_revision_are_strict(self):
        revision = plan_revision()
        active = {
            key: value
            for key, value in revision.items()
            if key not in {
                "summary", "rationale", "purpose_alignment",
                "preserves_ultimate_purpose",
            }
        }
        self.assertEqual(validate_active_plan(active)["revision_id"], "plan-2")
        self.assertTrue(validate_plan_revision(revision)["preserves_ultimate_purpose"])

        purpose_change = dict(revision, ultimate_purpose="Replace the user purpose.")
        with self.assertRaisesRegex(ValidationError, "unknown field.*ultimate_purpose"):
            validate_plan_revision(purpose_change)
        false_attestation = dict(revision, preserves_ultimate_purpose=False)
        with self.assertRaisesRegex(ValidationError, "must be true"):
            validate_plan_revision(false_attestation)
        missing_stage = dict(revision, current_stage="removed")
        with self.assertRaisesRegex(ValidationError, "current_stage must name"):
            validate_plan_revision(missing_stage)
        unreachable = dict(revision, current_stage="stage-2", target_stage="stage-1")
        with self.assertRaisesRegex(ValidationError, "must not precede"):
            validate_plan_revision(unreachable)


class RoleOutputValidationTests(unittest.TestCase):
    def test_sol_statuses_and_authority_boundary(self):
        for status in SOL_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(validate_sol_result(sol_result(status))["status"], status)

        unauthorized = sol_result("DONE")
        unauthorized["route"] = "END"
        with self.assertRaisesRegex(ValidationError, "Astra authority"):
            validate_sol_result(unauthorized)

        invalid = sol_result("MILESTONE_COMPLETE")
        invalid["evidence_artifact_paths"] = []
        with self.assertRaisesRegex(ValidationError, "evidence artifact"):
            validate_sol_result(invalid)

    def test_recovery_strategy_and_attempt_are_bounded_to_five(self):
        self.assertEqual(validate_recovery_strategy(recovery_strategy(5))["attempt"], 5)
        for attempt in (0, 6):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(
                ValidationError, "1 through 5"
            ):
                validate_recovery_strategy(recovery_strategy(attempt))

        completed = {
            **recovery_strategy(2),
            "route": "LUNA",
            "outcome": "No sufficient primary evidence was found.",
            "status": "FAILED",
        }
        self.assertEqual(validate_recovery_attempt(completed)["route"], "LUNA")
        with self.assertRaisesRegex(ValidationError, "SOL or LUNA"):
            validate_recovery_attempt(dict(completed, route="USER"))

    def test_luna_task_and_results_have_no_astra_authority(self):
        task = luna_task()
        task["recovery_strategy"] = recovery_strategy()
        self.assertEqual(validate_luna_task(task)["research_id"], "research-1")
        for status in LUNA_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(validate_luna_result(luna_result(status))["status"], status)

        unauthorized = luna_result()
        unauthorized["route"] = "SOL"
        with self.assertRaisesRegex(ValidationError, "Astra authority"):
            validate_luna_result(unauthorized)
        missing_sources = luna_result()
        missing_sources["sources"] = []
        with self.assertRaisesRegex(ValidationError, "findings and sources"):
            validate_luna_result(missing_sources)

    def test_review_package_is_complete_and_cross_references_match(self):
        normalized = validate_review_package(review_package())
        self.assertEqual(normalized["actual_results"]["status"], "MILESTONE_COMPLETE")
        self.assertEqual(normalized["evidence_artifact_paths"][0]["sha256"], "a" * 64)

        mismatched = review_package()
        mismatched["actual_results"]["task_id"] = "different-task"
        with self.assertRaisesRegex(ValidationError, "task_id must match"):
            validate_review_package(mismatched)

        wrong_revision = review_package()
        wrong_revision["actual_results"]["plan_revision_id"] = "plan-other"
        with self.assertRaisesRegex(ValidationError, "plan_revision_id must match"):
            validate_review_package(wrong_revision)

        incomplete_state = review_package()
        incomplete_state["current_state"] = {"current_stage": "stage-1"}
        with self.assertRaisesRegex(ValidationError, "missing required field"):
            validate_review_package(incomplete_state)

    def test_review_ids_are_safe_artifact_directory_names(self):
        unsafe = review_package()
        unsafe["review_id"] = "../../outside"
        with self.assertRaisesRegex(ValidationError, "ASCII"):
            validate_review_package(unsafe)

    def test_reviewer_verdict_semantics_and_authority_boundary(self):
        base = {
            "review_id": "review-1",
            "stage_id": "stage-1",
            "plan_revision_id": "plan-1",
            "ultimate_purpose": "Prove deterministic orchestration routes.",
            "verdict": "PASS",
            "summary": "Evidence satisfies the criteria.",
            "findings": [],
            "evidence_inspected": ["artifacts/check.txt"],
            "missing_evidence": [],
            "known_limitations": [],
        }
        self.assertEqual(validate_reviewer_result(base)["verdict"], "PASS")

        for verdict, field in (("FAIL", "findings"), ("NEEDS_EVIDENCE", "missing_evidence")):
            invalid = dict(base, verdict=verdict, evidence_inspected=[])
            invalid[field] = []
            with self.subTest(verdict=verdict), self.assertRaises(ValidationError):
                validate_reviewer_result(invalid)

        fail_without_inspection = dict(
            base,
            verdict="FAIL",
            findings=["The inspected artifact contradicts the criterion."],
            evidence_inspected=[],
        )
        with self.assertRaisesRegex(ValidationError, "inspected evidence"):
            validate_reviewer_result(fail_without_inspection)

        unauthorized = dict(base, proposed_next_plan="Tell Sol to bypass the gate.")
        with self.assertRaisesRegex(ValidationError, "Astra authority"):
            validate_reviewer_result(unauthorized)

        unsafe = dict(base, review_id="..\\outside")
        with self.assertRaisesRegex(ValidationError, "ASCII"):
            validate_reviewer_result(unsafe)


class AstraAndUserValidationTests(unittest.TestCase):
    def test_each_route_requires_only_its_payload(self):
        decisions = {
            "SOL": {"sol_task": sol_task()},
            "LUNA": {"luna_task": luna_task()},
            "REVIEW": {"review_package": review_package()},
            "USER": {
                "user_request": {
                    "request_id": "request-1",
                    "category": "AUTH_OR_HUMAN_ACTION_REQUIRED",
                    "prompt": "Complete the required MFA challenge.",
                    "required_actions": ["Complete MFA outside the durable response."],
                }
            },
            "END": {
                "end_reason": "The requested target stage passed review.",
                "target_stage_completed": True,
                "stage_transition": {
                    "transition_id": "transition-1",
                    "from_stage": "stage-1",
                    "to_stage": None,
                    "based_on_review_id": "review-1",
                    "reason": "Target stage passed independent review.",
                    "approved": True,
                },
            },
        }
        for route in ASTRA_ROUTES:
            with self.subTest(route=route):
                decision = {
                    "decision_id": f"decision-{route.lower()}",
                    "route": route,
                    "reason": "Deterministic test route.",
                    **decisions[route],
                }
                self.assertEqual(validate_astra_decision(decision)["route"], route)

        conflict = {
            "decision_id": "decision-conflict",
            "route": "SOL",
            "reason": "Invalid mixed authority.",
            "sol_task": sol_task(),
            "review_package": review_package(),
        }
        with self.assertRaisesRegex(ValidationError, "forbids"):
            validate_astra_decision(conflict)

    def test_end_requires_confirmed_target_completion(self):
        decision = {
            "decision_id": "decision-end",
            "route": "END",
            "reason": "Attempted early stop.",
            "end_reason": "Not actually complete.",
            "target_stage_completed": False,
        }
        with self.assertRaisesRegex(ValidationError, "target_stage_completed=true"):
            validate_astra_decision(decision)

    def test_user_categories_are_exact_and_response_cannot_route_or_override_limits(self):
        for category in (
            "AUTH_OR_HUMAN_ACTION_REQUIRED",
            "PAID_RESOURCE_APPROVAL",
            "AUTOMATIC_RECOVERY_EXHAUSTED",
        ):
            decision = {
                "decision_id": f"decision-{category.lower()}",
                "route": "USER",
                "reason": "A permitted interruption.",
                "user_request": {
                    "request_id": f"request-{category.lower()}",
                    "category": category,
                    "prompt": "Perform the permitted user action.",
                    "required_actions": ["Complete the recorded action."],
                },
            }
            self.assertEqual(
                validate_astra_decision(decision)["user_request"]["category"], category
            )

        for category in (
            "MANUAL_UPLOAD",
            "CREDENTIALS_OR_PERMISSIONS",
            "SCOPE_EXPANSION",
            "AMBIGUOUS_OR_UNRECOVERABLE_STATE",
        ):
            invalid_decision = {
                "decision_id": "decision-old-user-category",
                "route": "USER",
                "reason": "Old category must fail.",
                "user_request": {
                    "request_id": "request-old-category",
                    "category": category,
                    "prompt": "Invalid.",
                    "required_actions": ["Invalid."],
                },
            }
            with self.subTest(category=category), self.assertRaisesRegex(
                ValidationError, "supported human-intervention reason"
            ):
                validate_astra_decision(invalid_decision)

        response = {
            "request_id": "request-limit",
            "status": "PROVIDED",
            "response": "The human-only action is complete.",
            "evidence_artifact_paths": [],
        }
        self.assertEqual(validate_user_response(response)["status"], "PROVIDED")
        invalid = dict(response, route="SOL")
        with self.assertRaisesRegex(ValidationError, "Astra authority"):
            validate_user_response(invalid)
        invalid_limit = dict(response, limit_overrides={"max_transitions": 50})
        with self.assertRaisesRegex(ValidationError, "unknown field"):
            validate_user_response(invalid_limit)

    def test_astra_may_attach_a_full_plan_revision_without_replacing_purpose(self):
        decision = {
            "decision_id": "decision-replan",
            "route": "LUNA",
            "reason": "Research the revised approach.",
            "luna_task": luna_task(),
            "plan_revision": plan_revision(),
        }
        normalized = validate_astra_decision(decision)
        self.assertEqual(normalized["plan_revision"]["datasets"], ["dataset-v2"])
        invalid = dict(decision)
        invalid["plan_revision"] = dict(
            plan_revision(), ultimate_purpose="Replace the purpose."
        )
        with self.assertRaisesRegex(ValidationError, "ultimate_purpose"):
            validate_astra_decision(invalid)


class BundledJsonSchemaTests(unittest.TestCase):
    def test_all_model_contract_schemas_are_bundled_json_objects(self):
        names = {
            "active_plan",
            "astra_decision",
            "luna_result",
            "luna_task",
            "plan_revision",
            "project_spec",
            "review_package",
            "reviewer_result",
            "sol_result",
            "user_response",
        }
        for name in names:
            with self.subTest(name=name):
                path = schema_path(name)
                self.assertTrue(path.is_absolute())
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
        with self.assertRaisesRegex(ValidationError, "unknown schema"):
            schema_path("missing")

        for name in ("astra_decision", "review_package"):
            schema = json.loads(schema_path(name).read_text(encoding="utf-8"))
            review_state = schema["$defs"]["reviewState"]
            self.assertIn("active_plan", review_state["required"])
            self.assertEqual(
                review_state["properties"]["active_plan"]["$ref"],
                "#/$defs/activePlan",
            )
        reviewer = json.loads(
            schema_path("reviewer_result").read_text(encoding="utf-8")
        )
        fail_rule = reviewer["allOf"][1]["then"]["properties"]
        self.assertEqual(fail_rule["evidence_inspected"]["minItems"], 1)


if __name__ == "__main__":
    unittest.main()
