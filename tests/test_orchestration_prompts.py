import unittest

from astra_orchestrator.prompts import (
    REVIEW_PACKAGE_FIELDS,
    build_astra_prompt,
    build_luna_prompt,
    build_reviewer_prompt,
    build_sol_prompt,
    make_review_package,
)


class OrchestrationPromptTests(unittest.TestCase):
    def test_astra_prompt_encodes_exact_user_recovery_and_luna_policy(self):
        prompt = build_astra_prompt(
            {"ultimate_purpose": "Keep the verified purpose."},
            latest_luna_result={"status": "DONE", "summary": "Sourced finding."},
        )

        for category in (
            "AUTH_OR_HUMAN_ACTION_REQUIRED",
            "PAID_RESOURCE_APPROVAL",
            "AUTOMATIC_RECOVERY_EXHAUSTED",
        ):
            self.assertIn(category, prompt)
        for legacy in (
            "MANUAL_UPLOAD",
            "SCOPE_EXPANSION",
            "AMBIGUOUS_OR_UNRECOVERABLE_STATE",
        ):
            self.assertNotIn(legacy, prompt)
        self.assertIn("maximum of 5 attempts", prompt)
        self.assertIn("unique strategy_id", prompt)
        self.assertIn("supply an exhaustion_context", prompt)
        self.assertIn("exact,\nactionable required_actions", prompt)
        self.assertIn("one self-contained USER prompt", prompt)
        self.assertIn("Do not expect the USER to reconstruct", prompt)
        self.assertIn("route LUNA", prompt)
        self.assertIn("completely fresh Luna session", prompt)
        self.assertIn("immutable ultimate_purpose exactly", prompt)
        self.assertIn("Never change the scientific or experimental\nplan merely", prompt)
        self.assertIn('"latest_luna_result"', prompt)

    def test_astra_prompt_makes_usage_exhaustion_a_controller_hard_stop(self):
        prompt = build_astra_prompt({
            "ultimate_purpose": "Keep the verified purpose.",
        })

        self.assertIn("controller-owned terminal condition", prompt)
        self.assertIn("not a USER category", prompt)
        self.assertIn("not an automatic-recovery problem", prompt)
        self.assertIn("ends the run without another role call", prompt)
        self.assertIn("consuming a recovery attempt", prompt)
        self.assertIn("requesting USER action", prompt)
        self.assertIn("redeeming or resetting usage credits", prompt)
        self.assertIn("purchasing credits", prompt)
        self.assertIn("scheduling an\nautomatic continuation", prompt)

    def test_astra_prompt_preserves_temporary_drive_handoff_policy(self):
        prompt = build_astra_prompt({"current_stage": "stage-1"})

        self.assertIn("Google Drive only for data or artifacts currently", prompt)
        self.assertIn("active review/resume evidence nor the sole copy", prompt)
        self.assertIn("filename, byte count, and SHA-256", prompt)
        self.assertIn("split the file into verified parts", prompt)
        self.assertIn("If space still cannot be made safely", prompt)
        self.assertIn("do not require separate\nUSER approval", prompt)

    def test_luna_prompt_is_fresh_research_only_and_returns_to_astra(self):
        task = {
            "research_id": "research-1",
            "stage_id": "stage-1",
            "plan_revision_id": "plan-1",
            "question": "Find primary evidence.",
            "scope": ["Official sources only."],
            "sources_to_consult": ["Primary sources."],
            "deliverables": ["Sourced findings."],
            "constraints": ["Read-only."],
        }
        prompt = build_luna_prompt({"current_stage": "stage-1"}, task)

        self.assertIn("completely fresh Luna session", prompt)
        self.assertIn("Research and Information-Gathering Specialist only", prompt)
        self.assertIn("Operate read-only", prompt)
        self.assertIn("Do not\nmanage or instruct Sol", prompt)
        self.assertIn("Astra\nalone evaluates and integrates", prompt)
        self.assertIn("DONE, INSUFFICIENT_EVIDENCE, BLOCKED, or FAILED", prompt)
        self.assertIn('"research_id": "research-1"', prompt)

    def test_sol_prompt_states_status_field_invariants(self):
        task = {
            "task_id": "task-1",
            "stage_id": "stage-1",
            "plan_revision_id": "plan-1",
        }
        prompt = build_sol_prompt({"current_stage": "stage-1"}, task)

        self.assertIn("DONE and MILESTONE_COMPLETE\nrequire empty blockers", prompt)
        self.assertIn("BLOCKED\nrequires nonempty blockers", prompt)
        self.assertIn("NEEDS_USER requires nonempty user_actions_requested", prompt)
        self.assertIn("FAILED\nrequires an error", prompt)
        self.assertIn("is BLOCKED, not DONE", prompt)

    def test_review_package_and_prompt_bind_purpose_and_plan_revision(self):
        package = make_review_package(
            review_id="review-1",
            stage_id="stage-1",
            project_root="/workspace/project",
            project_context="Synthetic context.",
            ultimate_purpose="Preserve the project's purpose.",
            plan_revision_id="plan-2",
            milestone_goal="Verify the milestone.",
            acceptance_criteria=["Evidence is inspectable."],
            sol_instructions={"task_id": "task-1"},
            actual_results={"status": "MILESTONE_COMPLETE"},
            evidence_artifact_paths=[{"path": "evidence.txt"}],
            known_limitations=[],
            current_state={"current_stage": "stage-1"},
            proposed_next_plan="Advance only after PASS.",
        )

        self.assertTrue(
            {"ultimate_purpose", "plan_revision_id"}.issubset(REVIEW_PACKAGE_FIELDS)
        )
        self.assertEqual(package["ultimate_purpose"], "Preserve the project's purpose.")
        self.assertEqual(package["plan_revision_id"], "plan-2")
        prompt = build_reviewer_prompt(package)
        self.assertIn("named plan_revision_id", prompt)
        self.assertIn("immutable ultimate_purpose", prompt)
        self.assertIn('"plan_revision_id": "plan-2"', prompt)


if __name__ == "__main__":
    unittest.main()
